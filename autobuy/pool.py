"""
miniqmt_autobuy 候选池筛选。

从 cfg 指定的 SQLite 多表中读取"运行日前 N 个交易日"入池的股票代码。
判定口径: 以代码运行日期为基准，向前回溯 N 个交易日(周末自动跳过)，返回这些
交易日期的所有 code。date 字段要求为标准日期文本 'YYYY-MM-DD'。

候选池代码格式 (如 'sh.600025') 会统一转换为 miniQMT 系统标准格式 '600025.SH'。
"""
from __future__ import annotations

import os
import re
import sqlite3
from datetime import date, datetime, timedelta

from .config import AutoBuyConfig, PROJECT_ROOT, get_autobuy_logger

logger = get_autobuy_logger("autobuy.pool")

# 6 位证券代码
_DIGITS_RE = re.compile(r"\d{6}")
# 上交所代码前缀 (其余归深交所; 北交所由市场标记 bj 识别)
_SH_PREFIX3 = (
    "600", "601", "603", "605", "688", "689",
    "510", "511", "512", "513", "515", "516", "518", "501", "502",
    "113", "110", "118",
)


def normalize_code(code: str) -> str:
    """规范化代码用于防重/比较: 提取 6 位数字。

    兼容 'sh.600000' / '600000.SH' / 纯数字 '600000'，统一返回 '600000'，
    保证跨格式防重一致。
    """
    m = _DIGITS_RE.search(code or "")
    return m.group(0) if m else (code or "").strip().upper()


def to_xt_code(raw: str) -> str:
    """把候选池代码转成 miniQMT 系统标准格式 '600000.SH'。

    候选池实际格式为 'sh.600025' / 'sz.000626' (市场前缀在前)，而全系统使用
    '代码.SH' 格式 (见 Methods.add_xt_suffix)，两者不兼容，必须转换。
    兼容: 'sh.600025'->'600025.SH', '600025.SH'->'600025.SH',
          'sz.000626'->'000626.SZ', '600025'->'600025.SH'(按前缀推断)。
    无法识别 6 位数字时原样返回。
    """
    s = (raw or "").strip()
    m = _DIGITS_RE.search(s)
    if not m:
        return s
    num = m.group(0)
    low = s.lower()
    if "bj" in low:
        return f"{num}.BJ"
    if "sh" in low:
        return f"{num}.SH"
    if "sz" in low:
        return f"{num}.SZ"
    # 无市场标记: 按代码前缀推断 (与 Methods.add_xt_suffix 一致)
    if num[:3] in _SH_PREFIX3 or num[:2] == "11":
        return f"{num}.SH"
    if num[:2] in ("43", "83", "87", "88", "92"):  # 北交所常见前缀
        return f"{num}.BJ"
    return f"{num}.SZ"


def _coerce_date(reference_date=None) -> date:
    """把外部传入的日期统一转为 date，便于测试固定运行日。"""
    if reference_date is None:
        return date.today()
    if isinstance(reference_date, datetime):
        return reference_date.date()
    if isinstance(reference_date, date):
        return reference_date
    if isinstance(reference_date, str):
        return datetime.strptime(reference_date, "%Y-%m-%d").date()
    raise TypeError(f"不支持的 reference_date 类型: {type(reference_date)!r}")


def recent_trading_dates(n: int, reference_date=None) -> list:
    """返回运行日前最近 N 个交易日，按近到远排序。

    当前采用本地工作日口径(周一至周五)，能正确处理周末；A 股法定节假日可后续
    接入交易日历进一步精确化。
    """
    ref = _coerce_date(reference_date)
    result = []
    day = ref - timedelta(days=1)
    while len(result) < n:
        if day.weekday() < 5:
            result.append(day.strftime("%Y-%m-%d"))
        day -= timedelta(days=1)
    return result


def read_candidates(cfg: AutoBuyConfig, reference_date=None) -> list:
    """返回运行日前 N 个交易日入池的股票代码 (标准格式，跨表去重)。失败返回空列表。

    遍历 cfg.tables 每张表: 取目标交易日期内的所有 code，合并去重并转成系统标准
    格式。单表查询失败 (表不存在等) 记 warning 并跳过。
    """
    db_path = cfg.db_path
    if not os.path.isabs(db_path):
        db_path = os.path.join(PROJECT_ROOT, db_path)

    if not os.path.exists(db_path):
        logger.error(f"候选池数据库不存在: {db_path}")
        return []

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)  # 只读，避免误写
    except sqlite3.Error as e:
        logger.error(f"打开候选池数据库失败: {e}")
        return []

    codes = []
    seen = set()
    per_table = []
    target_dates = recent_trading_dates(cfg.latest_n_dates, reference_date)
    try:
        for tbl in cfg.tables:
            # 表名/字段名已在 AutoBuyConfig.validate() 做白名单校验，可安全拼接；值仍参数化绑定
            try:
                placeholders = ",".join("?" * len(target_dates))
                rows = conn.execute(
                    f"SELECT DISTINCT {cfg.code_column} AS code FROM {tbl} "
                    f"WHERE {cfg.date_column} IN ({placeholders})",
                    target_dates,
                ).fetchall()
            except sqlite3.Error as e:
                logger.warning(f"读取候选池表失败，跳过 (table={tbl}): {e}")
                continue

            hit = 0
            for (code,) in rows:
                if code is None:
                    continue
                code = str(code).strip()
                if not code:
                    continue
                key = normalize_code(code)
                if key not in seen:
                    seen.add(key)
                    codes.append(to_xt_code(code))  # 统一系统标准格式
                    hit += 1
            logger.debug(f"候选池表 {tbl}: 目标交易日 {target_dates} 命中 {hit} 只")
            per_table.append(f"{tbl}={hit}")
    finally:
        conn.close()

    logger.info(f"候选池筛选: 交易日 {target_dates} → {' '.join(per_table)} 合计 {len(codes)} 只")
    return codes
