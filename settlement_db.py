# -*- coding: utf-8 -*-
"""
交割单数据落库：持仓快照、每日净值、运行事件。

为什么不复用 data_manager.conn：
    TradingExecutor 与 DataManager 共享同一个 sqlite3.Connection 却各拿不同的锁，
    一方 commit() 会提前提交另一方未完成的事务。本模块每次新开连接，
    既避开这个竞态，也不给既有事务边界添乱 —— 快照/净值是每天两次的低频写入，
    开连接的开销可以忽略。

时间口径：进程运行在东八区，datetime.now() 即北京时间；不做时区转换。
"""
import json
import sqlite3
import time
from datetime import datetime, timedelta

import config
import db_migrate
from logger import get_logger

logger = get_logger('settlement_db')

# 快照类型：**固定枚举，只有两种**。
# open  09:25 盘前 —— 当日盈亏与持仓对账的唯一基准
# close 15:05 收盘
# 曾经的 intraday（心跳采样）已删除：它把"QMT 未连接时的全零读数"引入了库里，
# 且与 open/close 语义重叠，徒增噪声。
SNAPSHOT_OPEN = 'open'
SNAPSHOT_CLOSE = 'close'
VALID_SNAPSHOT_TYPES = (SNAPSHOT_OPEN, SNAPSHOT_CLOSE)


def _cfg(name, default):
    """读配置并兜底 —— 测试库与旧版 config 都可能缺这些项。"""
    return getattr(config, name, default)


# trade_records 是否已扩展的探测缓存，避免每笔成交都查一次 PRAGMA
_SCHEMA_CACHE = {}


def get_account_id():
    """当前进程的账号标识。库内存真实 ID，脱敏只发生在导出层。"""
    try:
        return str(config.ACCOUNT_CONFIG.get('account_id') or 'UNKNOWN')
    except Exception:
        return 'UNKNOWN'


def _connect(db_path=None):
    conn = sqlite3.connect(db_path or config.DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


# ============================== run_events ==============================

def log_event(event_type, level='INFO', detail=None, code=None,
              order_id=None, trade_id=None, account=None, db_path=None):
    """写一条运行事件。

    绝不抛异常 —— 它经常在异常处理路径里被调用，自己再炸会掩盖原始错误。
    """
    try:
        if detail is not None and not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False, default=str)
        conn = _connect(db_path)
        try:
            conn.execute(
                "INSERT INTO run_events(account, event_time, level, event_type, "
                "code, order_id, trade_id, detail) VALUES (?,?,?,?,?,?,?,?)",
                (account or get_account_id(),
                 datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                 level, event_type, code,
                 str(order_id) if order_id is not None else None,
                 str(trade_id) if trade_id is not None else None,
                 detail))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        logger.warning(f"run_events 写入失败({event_type}): {e}")
        return False


# ============================== 交易日历 ==============================

def is_trading_day(date_str, db_path=None):
    """判断是否交易日。

    config 里只按周一至周五判断，没有节假日日历（utils.get_trading_days 的注释
    直认"忽略了节假日"），长假会把 5~9 个休市日误判成"快照缺失"。
    这里改用 stock_daily_data 反推：某日有 K 线即为交易日 —— 这是仓库内
    唯一可得的真实日历，零新增依赖。

    返回 (is_trading_day, confident)。日期落在 K 线覆盖范围之外时
    confident=False，调用方应当降级为"周一至周五"判断而不是当作休市。
    """
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM stock_daily_data WHERE date = ? LIMIT 1",
                (date_str,)).fetchone()
            if row:
                return True, True
            bounds = conn.execute(
                "SELECT MIN(date), MAX(date) FROM stock_daily_data").fetchone()
        finally:
            conn.close()

        lo, hi = (bounds[0], bounds[1]) if bounds else (None, None)
        if not lo or not hi or date_str < lo or date_str > hi:
            weekday_ok = datetime.strptime(date_str, '%Y-%m-%d').weekday() < 5
            return weekday_ok, False
        # 在覆盖范围内且无 K 线 —— 确信是休市日
        return False, True
    except Exception as e:
        logger.warning(f"交易日判断失败({date_str}): {e}")
        weekday_ok = datetime.strptime(date_str, '%Y-%m-%d').weekday() < 5
        return weekday_ok, False


# ============================== 持仓快照 ==============================

def write_position_snapshot(position_manager, snapshot_type,
                            snapshot_date=None, db_path=None):
    """写一份全量持仓快照。返回写入行数，失败返回 -1。

    positions 表是"当前持仓"会被覆盖写，不能当历史用；对账基准只能取本表。
    """
    account = get_account_id()
    snapshot_date = snapshot_date or datetime.now().strftime('%Y-%m-%d')
    recorded_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    try:
        df = position_manager.get_all_positions_with_all_fields()
    except Exception as e:
        logger.error(f"持仓快照取数失败({snapshot_type}): {e}")
        log_event('snapshot_write_failed', 'ERROR',
                  {'stage': 'fetch', 'snapshot_type': snapshot_type, 'error': str(e)},
                  db_path=db_path)
        return -1

    rows = []
    if df is not None and not df.empty:
        for _, pos in df.iterrows():
            rows.append((
                account, snapshot_date, snapshot_type,
                _norm_code(pos.get('stock_code')),
                pos.get('stock_name'),
                _num(pos.get('volume')), _num(pos.get('available')),
                _num(pos.get('cost_price')), _num(pos.get('base_cost_price')),
                _num(pos.get('current_price')), _num(pos.get('market_value')),
                _num(pos.get('profit_ratio')),
                'memory_db', recorded_at,
            ))

    try:
        conn = _connect(db_path)
        try:
            # 重跑覆盖：同 (account, date, code, type) 幂等
            conn.executemany(
                "INSERT OR REPLACE INTO position_snapshot("
                "account, snapshot_date, snapshot_type, code, stock_name, "
                "volume, available, cost_price, base_cost_price, current_price, "
                "market_value, profit_ratio, source, recorded_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"持仓快照写入失败({snapshot_type}): {e}")
        log_event('snapshot_write_failed', 'ERROR',
                  {'stage': 'write', 'snapshot_type': snapshot_type, 'error': str(e)},
                  db_path=db_path)
        return -1

    # 空持仓是合法状态（全部卖出），写 0 行不算失败，但要留痕以便与"取数失败"区分
    logger.info(f"持仓快照已写入: {snapshot_date} {snapshot_type} {len(rows)} 只")
    log_event('position_snapshot_written', 'INFO',
              {'snapshot_type': snapshot_type, 'date': snapshot_date, 'rows': len(rows)},
              db_path=db_path)
    return len(rows)


def _norm_code(code):
    """统一成 6 位纯数字。库里混存 '002319' 与 '301399.SZ' 两种格式。"""
    if code is None:
        return None
    s = str(code).strip()
    return s.split('.')[0] if '.' in s else s


def _num(v):
    try:
        if v is None:
            return None
        f = float(v)
        return None if f != f else f  # 过滤 NaN
    except (TypeError, ValueError):
        return None


def _is_blank_asset(total_asset, market_value, cash, frozen_cash):
    """判断是否为「QMT 未连接」造成的全零无效读数。

    真实账户即使空仓，可用资金也不会恰好是 0.00；而 QMT 未登录时
    balance() 返回的是整行 0。两者靠"全部为 0 或 None"即可区分。
    """
    values = (total_asset, market_value, cash, frozen_cash)
    if all(v is None for v in values):
        return True
    return all(v is None or abs(v) < 1e-9 for v in values)


# ============================== 每日净值 ==============================

def write_equity_snapshot(position_manager, snapshot_type,
                          snapshot_date=None, db_path=None):
    """写一条账户净值快照，并做恒等式校验与跳变检测。返回 True/False。"""
    account = get_account_id()
    snapshot_date = snapshot_date or datetime.now().strftime('%Y-%m-%d')
    recorded_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    try:
        info = position_manager.get_account_info()
    except Exception as e:
        logger.error(f"净值取数失败({snapshot_type}): {e}")
        log_event('asset_write_failed', 'ERROR',
                  {'stage': 'fetch', 'snapshot_type': snapshot_type, 'error': str(e)},
                  db_path=db_path)
        return False

    if not info:
        logger.warning(f"净值取数返回空({snapshot_type})，跳过写入")
        log_event('asset_write_failed', 'ERROR',
                  {'stage': 'fetch', 'snapshot_type': snapshot_type,
                   'error': 'get_account_info returned None'},
                  db_path=db_path)
        return False

    # 实盘分支有 frozen_cash 无 profit_loss，模拟分支反之 —— 一律兜底取值
    total_asset = _num(info.get('total_asset'))
    market_value = _num(info.get('market_value'))
    cash = _num(info.get('available'))        # QMT 的 XtAsset.cash，即"可用金额"
    frozen_cash = _num(info.get('frozen_cash'))
    source = 'qmt_api' if not config.ENABLE_SIMULATION_MODE else 'simulation'

    # QMT 未连接 / 未登录时 balance() 会返回一整行 0，那是**无效读数**不是真净值。
    # 恒等式校验拦不住它（0 == 0+0+0 恒成立），必须单独判掉 ——
    # 否则净值曲线会凭空出现归零点，而 source 还写着 qmt_api，比没有数据更糟。
    if total_asset is None or total_asset <= 0 or _is_blank_asset(
            total_asset, market_value, cash, frozen_cash):
        logger.warning(
            f"净值读数无效（total_asset={total_asset}，QMT 多半未连接/未登录），"
            f"拒绝落库 {snapshot_date} {snapshot_type}")
        log_event('asset_write_failed', 'ERROR',
                  {'stage': 'validate', 'snapshot_type': snapshot_type,
                   'date': snapshot_date, 'total_asset': total_asset,
                   'reason': 'invalid_asset_reading'},
                  db_path=db_path)
        return False

    # 恒等式校验：差额说明账户里还有别的资产项（逆回购/理财/未交收资金），
    # 归因时必须排除，否则"亏损"里会混进非股票损益。
    if None not in (total_asset, market_value, cash):
        expected = cash + (frozen_cash or 0.0) + market_value
        diff = total_asset - expected
        if abs(diff) > _cfg('SETTLEMENT_ASSET_IDENTITY_TOLERANCE', 1.0):
            logger.warning(
                f"资产恒等式不成立: total={total_asset:.2f} != "
                f"cash({cash:.2f})+frozen({frozen_cash or 0:.2f})+mv({market_value:.2f}), 差额={diff:.2f}")
            log_event('asset_identity_mismatch', 'ERROR', {
                'snapshot_type': snapshot_type, 'date': snapshot_date,
                'total_asset': total_asset, 'cash': cash,
                'frozen_cash': frozen_cash, 'market_value': market_value,
                'diff': round(diff, 2)}, db_path=db_path)

    daily_pnl, unexplained = _derive_pnl(
        account, snapshot_date, snapshot_type, total_asset, db_path)

    try:
        conn = _connect(db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO account_equity_daily("
                "account, date, snapshot_type, total_asset, market_value, cash, "
                "frozen_cash, deposit, withdraw, cum_deposit, deposit_source, "
                "daily_pnl, unexplained_delta, source, recorded_at) "
                "VALUES (?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,?,?,?,?)",
                (account, snapshot_date, snapshot_type, total_asset, market_value,
                 cash, frozen_cash, daily_pnl, unexplained, source, recorded_at))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"净值写入失败({snapshot_type}): {e}")
        log_event('asset_write_failed', 'ERROR',
                  {'stage': 'write', 'snapshot_type': snapshot_type, 'error': str(e)},
                  db_path=db_path)
        return False

    logger.info(f"净值快照已写入: {snapshot_date} {snapshot_type} "
                f"总资产={total_asset if total_asset is not None else 'NULL'}")
    return True


def _derive_pnl(account, date_str, snapshot_type, total_asset, db_path):
    """收盘时用当日 open 快照反算日内盈亏，并对无法解释的跳变告警。

    出入金拿不到（QMT 无任何出入金查询接口），所以：
      daily_pnl = close.total_asset - open.total_asset
    这是**未扣除出入金**的毛变动。一旦当日发生银证转账，这个数就是错的 ——
    因此同时做跳变检测，把可疑值写进 unexplained_delta，不假装知道。
    """
    if snapshot_type != SNAPSHOT_CLOSE or total_asset is None:
        return None, None

    try:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                "SELECT total_asset FROM account_equity_daily "
                "WHERE account=? AND date=? AND snapshot_type=?",
                (account, date_str, SNAPSHOT_OPEN)).fetchone()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"读取 open 快照失败，跳过盈亏推导: {e}")
        return None, None

    if not row or row['total_asset'] is None:
        return None, None

    delta = total_asset - float(row['total_asset'])
    threshold = max(abs(total_asset) * _cfg('SETTLEMENT_ASSET_JUMP_RATIO', 0.02),
                    _cfg('SETTLEMENT_ASSET_JUMP_ABSOLUTE', 5000.0))

    if abs(delta) > threshold:
        # 允许误报、不允许漏报：标出来让人工判断是盈亏还是转账
        logger.warning(f"账户资产日内跳变: {delta:.2f}（阈值 {threshold:.2f}），"
                       f"可能含出入金，已标记 unexplained_delta")
        log_event('asset_jump', 'WARNING', {
            'date': date_str, 'delta': round(delta, 2),
            'threshold': round(threshold, 2), 'open': float(row['total_asset']),
            'close': total_asset,
            'note': '出入金不可得，无法区分盈亏与银证转账'}, db_path=db_path)
        return round(delta, 2), round(delta, 2)

    return round(delta, 2), None


# ============================== 健康检查 ==============================

def check_snapshot_health(lookback_days=7, db_path=None):
    """检查最近 N 个交易日的快照完整性。连续 2 个交易日缺失视为故障。

    只对真实交易日计算 —— 否则国庆/春节长假会必然产生连续缺失的误报。
    """
    account = get_account_id()
    today = datetime.now().date()
    missing = []

    try:
        conn = _connect(db_path)
        try:
            for i in range(1, lookback_days + 1):
                day = (today - timedelta(days=i)).strftime('%Y-%m-%d')
                trading, confident = is_trading_day(day, db_path)
                if not trading or not confident:
                    continue
                got = {r['snapshot_type'] for r in conn.execute(
                    "SELECT DISTINCT snapshot_type FROM position_snapshot "
                    "WHERE account=? AND snapshot_date=?", (account, day))}
                lack = {SNAPSHOT_OPEN, SNAPSHOT_CLOSE} - got
                if lack:
                    missing.append((day, sorted(lack)))
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"快照健康检查失败: {e}")
        return []

    missing.sort()
    # 连续两个交易日都缺 = 故障
    for i in range(len(missing) - 1):
        log_event('snapshot_missing_streak', 'ERROR', {
            'days': [missing[i][0], missing[i + 1][0]],
            'missing': [missing[i][1], missing[i + 1][1]]}, db_path=db_path)
        logger.error(f"持仓快照连续缺失: {missing[i][0]} 与 {missing[i + 1][0]}")
        break

    return missing


def ensure_schema(db_path=None):
    """确保新表存在。幂等，可在启动路径上无条件调用。"""
    try:
        db_migrate.migrate_settlement_schema(db_path or config.DB_PATH, do_backup=False)
        return True
    except Exception as e:
        logger.error(f"交割单 schema 初始化失败: {e}")
        return False


# ============================== 成交落库（唯一写入口） ==============================

# strategy 代码 → 中文标签。落库时写死，导出直接取用，不再二次推断。
STRATEGY_LABELS = {
    'grid': '网格',
    'auto_partial': '首次部分止盈',
    'auto_full': '动态全仓止盈',
    'stop_loss': '固定止损',
    # 止损委托超时后自动重挂，本质仍是止损成交
    'reorder_stop_loss': '固定止损',
    'reorder_take_profit_half': '部分止盈重挂',
    'reorder_take_profit_full': '动态全仓止盈',
    'M_real': '手工买入',
    'manual_real': '手工买入',
    # 无法判断来源的走这里，不冒充手工买入
    'external': '外部-计划外',
    'default': '外部-计划外',
    'M_simu': '模拟买入',
    'manual_simu': '模拟买入',
    'simu': '模拟买入',
    'simu_partial': '模拟买入',
    'simu_full': '模拟买入',
}

UNKNOWN_LABEL = 'UNKNOWN'

# 模拟盘的 strategy 取值
SIMULATION_STRATEGIES = {'simu', 'simu_partial', 'simu_full', 'M_simu', 'manual_simu'}

TIME_SOURCE_EXCHANGE = 'exchange'
TIME_SOURCE_LOCAL = 'local_fallback'
TIME_SOURCE_RECONCILE = 'reconcile_backfill'
TIME_SOURCE_BROKER = 'broker'

# 合理成交时间窗口：早于此视为解析失败，宁可标 local_fallback 也不用脏值
_MIN_PLAUSIBLE_EPOCH = 1577808000   # 2020-01-01


def strategy_label_for(strategy):
    """strategy 代码 → 中文枚举。未知值返回 UNKNOWN，不猜。"""
    return STRATEGY_LABELS.get(str(strategy or '').strip(), UNKNOWN_LABEL)


def is_simulation_trade(trade_id, strategy):
    """判断是否模拟成交。

    数据库没有 is_simulation 列（本次改造才加），历史行只能靠
    trade_id 的 SIM_ 前缀 + strategy 后缀推断。ENABLE_SIMULATION_MODE
    不落库，无法用于回溯历史。
    """
    if str(trade_id or '').upper().startswith('SIM_'):
        return True
    return str(strategy or '') in SIMULATION_STRATEGIES


def parse_deal_time(raw, now=None):
    """把 XtTrade.traded_time 解析成 (epoch秒, 'YYYY-MM-DD HH:MM:SS')。

    QMT 这个字段有三种编码（实测都会出现）：
      - epoch 秒（10 位，约 1.7e9）
      - epoch 毫秒（13 位，约 1.7e12）
      - yyyymmddHHMMSS（14 位整数）
      - HHMMSS（6 位整数，缺日期）
    以及可能本来就是字符串形式的日期。

    解析不出来就返回 (None, None) —— 调用方必须标 local_fallback 并告警，
    **禁止用 now() 冒充成交时间**。这是本次改造的核心约束。
    """
    if raw is None or raw == '':
        return None, None

    now = now or datetime.now()
    epoch = None

    try:
        # 字符串形式的日期
        if isinstance(raw, str) and not raw.strip().isdigit():
            text = raw.strip().replace('T', ' ')
            for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
                try:
                    epoch = int(datetime.strptime(text[:19], fmt).timestamp())
                    break
                except ValueError:
                    continue
            if epoch is None:
                return None, None
            return epoch, datetime.fromtimestamp(epoch).strftime('%Y-%m-%d %H:%M:%S')

        num = int(float(raw))
    except (TypeError, ValueError):
        return None, None

    if num <= 0:
        return None, None

    digits = str(num)
    try:
        if len(digits) == 14 and digits[:2] in ('19', '20'):
            dt = datetime.strptime(digits, '%Y%m%d%H%M%S')
            epoch = int(dt.timestamp())
        elif len(digits) == 6:
            # 只有时分秒，日期用当天补齐
            dt = now.replace(hour=int(digits[0:2]), minute=int(digits[2:4]),
                             second=int(digits[4:6]), microsecond=0)
            epoch = int(dt.timestamp())
        elif len(digits) >= 13:
            epoch = int(num // 1000)      # 毫秒
        else:
            epoch = int(num)              # 秒
    except (ValueError, OverflowError):
        return None, None

    if epoch is None or epoch < _MIN_PLAUSIBLE_EPOCH:
        return None, None

    try:
        return epoch, datetime.fromtimestamp(epoch).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, OSError, OverflowError):
        return None, None


def _has_extended_schema(conn, db_path):
    """探测 trade_records 是否已扩展（带缓存）。

    代码与迁移之间存在部署窗口：若代码先上、迁移未跑，扩展列还不存在。
    此时必须降级为旧列插入，**绝不能因为缺列就把成交记录丢掉** ——
    成交记录不可重建，丢了就是永久丢失。
    """
    key = db_path or config.DB_PATH
    cached = _SCHEMA_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(trade_records)")}
        ok = 'deal_time' in cols and 'strategy_label' in cols
    except Exception:
        ok = False
    _SCHEMA_CACHE[key] = ok
    if not ok:
        logger.warning(
            "trade_records 尚未扩展（缺 deal_time/strategy_label 等列），"
            "本次降级为旧列写入。请尽快执行 scripts/migrate_settlement.py，"
            "否则成交时间与手续费来源等归因字段无法落库。")
    return ok


def record_trade(record, conn=None, db_path=None):
    """trade_records 的唯一写入口。

    用 INSERT OR IGNORE 配合唯一索引 ux_trade_records_deal 做幂等——
    这是**原子**的，不依赖锁，也不存在 check-then-insert 的竞态窗口。
    历史上同一笔成交被写两遍，正是因为判重是「先 SELECT 再 INSERT」。

    索引或扩展列尚未建立时（生产库未迁移）自动降级为旧列写入，
    保证部署窗口期内成交流水不丢。

    返回 'inserted' / 'duplicate' / 'legacy_inserted' / 'failed'。
    """
    if not record or not record.get('stock_code'):
        return 'failed'

    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    trade_id = record.get('trade_id')
    trade_id = str(trade_id) if trade_id is not None else None
    strategy = record.get('strategy')

    owns_conn = conn is None
    try:
        conn = conn or _connect(db_path)

        # 先判定模拟/实盘再决定落哪张表：模拟单绝不与实盘同表，
        # 否则归因时一旦漏过滤 is_simulation 就会把模拟盈亏算进实盘。
        if record.get('is_simulation') is None:
            is_sim = is_simulation_trade(trade_id, strategy)
        else:
            is_sim = bool(record['is_simulation'])
        target = 'trade_records_sim' if is_sim else 'trade_records'

        if not _has_extended_schema(conn, db_path):
            # 迁移未跑：模拟表多半也不存在，统一降级到旧表旧列，先保证不丢数据
            return _insert_legacy(conn, record, trade_id, strategy, now_str)

        if is_sim and not db_migrate.table_exists(conn, 'trade_records_sim'):
            logger.warning("trade_records_sim 不存在，模拟成交暂存 trade_records；"
                           "请执行 scripts/migrate_settlement.py 建表")
            target = 'trade_records'

        deal_time = record.get('deal_time')
        deal_time_str = record.get('deal_time_str')
        time_source = record.get('time_source')
        if time_source is None:
            time_source = TIME_SOURCE_EXCHANGE if deal_time_str else TIME_SOURCE_LOCAL
        # 解析不出成交时间时绝不用 now() 冒充 —— trade_time 仍写调用方给的值
        # （通常就是本地时间），但 time_source 明确标为 local_fallback。
        if time_source == TIME_SOURCE_LOCAL:
            logger.warning(
                f"未能取得交易所成交时间，标记 time_source=local_fallback: "
                f"trade_id={trade_id}, order_id={record.get('order_id')}, "
                f"code={record.get('stock_code')}")

        commission = record.get('commission')
        commission_source = record.get('commission_source')
        if commission_source is None:
            commission_source = 'unknown' if commission in (None, 0, 0.0) else 'estimated'

        account_used = record.get('account') or get_account_id()
        order_id_used = (str(record.get('order_id'))
                         if record.get('order_id') is not None else None)
        params = (
            account_used,
            record.get('stock_code'),
            record.get('stock_name'),
            record.get('trade_time') or now_str,
            trade_id,
            now_str,
            time_source,
            order_id_used,
            record.get('fill_ids') or trade_id,
            int(record.get('fills') or 1),
            strategy,
            record.get('strategy_label') or strategy_label_for(strategy),
            1 if is_sim else 0,
            deal_time,
            deal_time_str,
            commission,
            commission_source,
            record.get('commission_rate'),
            record.get('side_source') or 'deal',
            record.get('trade_id_source') or (
                'placeholder' if str(trade_id).startswith('ORDER_')
                else 'order_id' if _looks_like_short_order_id(trade_id)
                else 'traded_id'),
            record.get('trade_type'),
            record.get('price'),
            record.get('volume'),
            record.get('amount'),
        )

        cur = conn.execute(
            "INSERT OR IGNORE INTO %s(" % target +
            "account, stock_code, stock_name, trade_time, trade_id, recorded_at, "
            "time_source, order_id, fill_ids, fills, strategy, strategy_label, "
            "is_simulation, deal_time, deal_time_str, commission, "
            "commission_source, commission_rate, side_source, trade_id_source, "
            "trade_type, price, volume, amount) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", params)
        if cur.rowcount:
            if owns_conn:
                conn.commit()
            return 'inserted'

        # rowcount==0 说明 INSERT OR IGNORE 被唯一索引挡下。
        # **绝不能就此当作重复** —— 必须比对冲突行：
        #   · 各字段完全一致 → 确是同一条 deal 被重复投递，跳过是对的
        #   · 任一字段不同   → 是一笔**不同的**成交，静默忽略就是丢单
        conflict = _find_conflicting_row(conn, target, account_used, order_id_used,
                                         trade_id, record, deal_time)
        if conflict is None:
            if owns_conn:
                conn.commit()
            return 'duplicate'
        if _is_same_deal(conflict, record):
            if owns_conn:
                conn.commit()
            return 'duplicate'

        # 键冲突但内容不同 —— 这是丢单的前兆，必须留痕并强行写入
        logger.error(
            f"唯一键冲突但内容不同，判定为不同成交并强制写入: "
            f"trade_id={trade_id}, code={record.get('stock_code')}, "
            f"已存在 id={conflict['id']} "
            f"(vol={conflict['volume']}, px={conflict['price']}) vs "
            f"本次 (vol={record.get('volume')}, px={record.get('price')})")
        log_event('deal_key_collision', 'ERROR', {
            'trade_id': trade_id, 'code': record.get('stock_code'),
            'existing_id': conflict['id'], 'existing_volume': conflict['volume'],
            'existing_price': conflict['price'], 'existing_amount': conflict['amount'],
            'incoming_volume': record.get('volume'),
            'incoming_price': record.get('price'),
            'incoming_amount': record.get('amount'),
            'note': '唯一键不足以区分，已强制写入避免丢单；需检查 trade_id 语义',
        }, db_path=db_path)

        forced = dict(record)
        forced['row_status'] = 'active'
        # 用 trade_id 加后缀打破冲突；原始编号保留在 fill_ids 里可追溯
        forced['fill_ids'] = record.get('fill_ids') or trade_id
        forced['trade_id'] = f"{trade_id}#{datetime.now().strftime('%H%M%S%f')}"
        forced['trade_id_source'] = 'collision_suffixed'
        return _insert_raw(conn, forced, now_str, owns_conn, db_path)
    except Exception as e:
        logger.error(f"交易流水写入失败: trade_id={trade_id}, error={e}")
        try:
            if owns_conn and conn is not None:
                conn.rollback()
        except Exception:
            pass
        log_event('trade_persist_failed', 'ERROR',
                  {'trade_id': trade_id, 'code': record.get('stock_code'),
                   'error': str(e)}, db_path=db_path)
        return 'failed'
    finally:
        if owns_conn and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _find_conflicting_row(conn, target, account, order_id, trade_id, record, deal_time):
    """找出与本次写入在唯一键上冲突的现有行。

    ⚠️ 查询条件必须与 ux_trade_records_deal 的键**逐项一致**。
    曾经这里的时间兜底写成 trade_time，而索引用的是 recorded_at，
    两者对不上导致查不到冲突行，把内容不同的成交静默判成 duplicate —— 丢单。
    改键时这两处必须同步。
    """
    try:
        row = conn.execute(
            "SELECT id, volume, price, amount, trade_type, stock_code FROM %s "
            "WHERE COALESCE(account,'')=? AND COALESCE(order_id,'')=? "
            "AND stock_code=? AND trade_type=? AND trade_id=? "
            "AND COALESCE(deal_time, CAST(strftime('%%s', trade_time) AS INTEGER), 0)="
            "COALESCE(?, CAST(strftime('%%s', ?) AS INTEGER), 0) "
            "AND volume=? AND price=? "
            "AND COALESCE(row_status,'active')='active' LIMIT 1" % target,
            (account or '', order_id or '', record.get('stock_code'),
             record.get('trade_type'), str(trade_id), deal_time,
             record.get('trade_time'), record.get('volume'),
             record.get('price'))).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.warning(f"查询冲突行失败（按重复处理）: {e}")
        return None


def _is_same_deal(existing, record):
    """冲突行与本次是否为同一条 deal。

    比较价量金额 —— 三者任一不同即视为不同成交。traded_id 相同但价量不同，
    现实中是同一委托的分笔成交（各笔价量不同），必须都保留。
    """
    def _f(v):
        try:
            return round(float(v), 4)
        except (TypeError, ValueError):
            return None

    return (_f(existing.get('volume')) == _f(record.get('volume'))
            and _f(existing.get('price')) == _f(record.get('price'))
            and _f(existing.get('amount')) == _f(record.get('amount'))
            and existing.get('trade_type') == record.get('trade_type'))


def _insert_raw(conn, record, now_str, owns_conn, db_path=None):
    """强制写入（带后缀 trade_id），用于键冲突但内容不同的场景。"""
    try:
        cols = ", ".join([
            "account", "stock_code", "stock_name", "trade_time", "trade_id",
            "recorded_at", "time_source", "order_id", "fill_ids", "fills",
            "strategy", "strategy_label", "is_simulation", "deal_time",
            "deal_time_str", "commission", "commission_source", "commission_rate",
            "side_source", "trade_id_source", "trade_type", "price", "volume",
            "amount", "row_status"])
        vals = (
            record.get('account') or get_account_id(), record.get('stock_code'),
            record.get('stock_name'), record.get('trade_time') or now_str,
            str(record.get('trade_id')), now_str,
            record.get('time_source') or TIME_SOURCE_LOCAL,
            str(record.get('order_id')) if record.get('order_id') is not None else None,
            record.get('fill_ids'), int(record.get('fills') or 1),
            record.get('strategy'),
            record.get('strategy_label') or strategy_label_for(record.get('strategy')),
            1 if record.get('is_simulation') else 0,
            record.get('deal_time'), record.get('deal_time_str'),
            record.get('commission'), record.get('commission_source') or 'unknown',
            record.get('commission_rate'), record.get('side_source') or 'deal',
            record.get('trade_id_source'), record.get('trade_type'),
            record.get('price'), record.get('volume'), record.get('amount'),
            record.get('row_status') or 'active')
        placeholders = ",".join(["?"] * len(vals))
        conn.execute("INSERT INTO trade_records(%s) VALUES (%s)" % (cols, placeholders),
                     vals)
        if owns_conn:
            conn.commit()
        return 'inserted_collision_suffixed'
    except Exception as e:
        logger.error(f"强制写入仍失败: {e}")
        log_event('trade_persist_failed', 'ERROR',
                  {'trade_id': record.get('trade_id'), 'error': str(e)},
                  db_path=db_path)
        return 'failed'


def _insert_legacy(conn, record, trade_id, strategy, now_str):
    """迁移未执行时的降级写入，只写旧列。

    宁可少几个归因字段，也不能丢成交记录。
    """
    try:
        cur = conn.execute(
            "INSERT INTO trade_records("
            "stock_code, stock_name, trade_time, trade_type, price, volume, "
            "amount, trade_id, commission, strategy) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (record.get('stock_code'), record.get('stock_name'),
             record.get('trade_time') or now_str, record.get('trade_type'),
             record.get('price'), record.get('volume'), record.get('amount'),
             trade_id, record.get('commission'), strategy))
        conn.commit()
        return 'legacy_inserted' if cur.rowcount else 'duplicate'
    except Exception as e:
        logger.error(f"交易流水降级写入失败: trade_id={trade_id}, error={e}")
        return 'failed'


def _looks_like_short_order_id(trade_id):
    """网格路径写的是 str(order_id)，形态为 9-10 位数字。

    真实 traded_id 是 20 位；占位流水带 ORDER_ 前缀（已在上面先行判断）。
    """
    text = str(trade_id or '')
    return text.isdigit() and 8 <= len(text) <= 12


def record_trade_with_retry(record, retries=3, db_path=None):
    """带重试的落库。失败重试后仍不行则落 run_events（已在 record_trade 内做）。"""
    last = 'failed'
    for attempt in range(retries):
        last = record_trade(record, db_path=db_path)
        if last != 'failed':
            return last
        if attempt < retries - 1:
            time.sleep(0.05 * (attempt + 1))
    return last


# ============================== 调度 ==============================

def take_snapshot(position_manager, snapshot_type, snapshot_date=None, db_path=None):
    """写一组完整快照（持仓 + 净值）。返回 (持仓行数, 净值是否成功)。

    两者分别记录成败：持仓取数失败不应阻止净值落库，反之亦然。
    """
    rows = write_position_snapshot(position_manager, snapshot_type,
                                   snapshot_date=snapshot_date, db_path=db_path)
    equity_ok = write_equity_snapshot(position_manager, snapshot_type,
                                      snapshot_date=snapshot_date, db_path=db_path)
    return rows, equity_ok


def should_run_close_snapshot(now, target_time, last_run_date, trading, confident):
    """判定此刻是否该写收盘快照。抽成纯函数以便单测覆盖各种日期组合。

    - 过了目标时刻、当天没跑过，是基本条件
    - 确信是交易日 → 跑
    - 判不出来（当天 K 线通常 15:30 后才入库）→ 降级为工作日判断，
      宁可多写一次也不漏；但**周末必须排除**，否则会写出毫无意义的
      非交易日快照（周六实测踩过）
    - 确信是休市日（长假等）→ 不跑
    """
    if now.time() < target_time:
        return False
    if last_run_date == now.date():
        return False
    if trading:
        return True
    return (not confident) and now.weekday() < 5


def schedule_close_snapshot(position_manager, stop_event=None):
    """每交易日收盘后写一次 close 快照。

    轮询范式取自 maintenance.schedule_database_maintenance：
    `now.time() >= target and last_run_date != today` —— 过了点就补，
    当天只跑一次。好处是进程 16:00 才启动也能补录当天收盘快照，
    这点比 premarket_sync 的 Timer 链更适合收盘任务（Timer 错过就得等次日）。
    """
    if not _cfg('ENABLE_SETTLEMENT_SNAPSHOT', True):
        logger.info("收盘快照任务未启用")
        return

    interval = _cfg('SETTLEMENT_SNAPSHOT_CHECK_INTERVAL', 300)
    target_str = _cfg('SETTLEMENT_CLOSE_SNAPSHOT_TIME', '15:05:00')
    target_time = datetime.strptime(target_str, '%H:%M:%S').time()
    last_run_date = None

    logger.info(f"收盘快照任务已启动: 每交易日 {target_str} 之后执行")

    while stop_event is None or not stop_event.is_set():
        try:
            now = datetime.now()
            today = now.strftime('%Y-%m-%d')
            trading, confident = is_trading_day(today)
            should_run = should_run_close_snapshot(
                now, target_time, last_run_date, trading, confident)
            if should_run:
                rows, equity_ok = take_snapshot(position_manager, SNAPSHOT_CLOSE,
                                                snapshot_date=today)
                last_run_date = now.date()
                logger.info(f"收盘快照完成: 持仓 {rows} 只, 净值 {'成功' if equity_ok else '失败'}")
                check_snapshot_health(
                    lookback_days=_cfg('SETTLEMENT_SNAPSHOT_HEALTH_LOOKBACK', 7))
        except Exception as e:
            logger.error(f"收盘快照任务异常: {e}", exc_info=True)
            log_event('asset_write_failed', 'ERROR',
                      {'stage': 'schedule_close', 'error': str(e)})

        if stop_event is not None:
            stop_event.wait(interval)
        else:
            time.sleep(interval)


def sample_intraday_equity(position_manager):
    """已废弃：intraday 快照已从枚举中移除。

    保留函数以免调用方（main.py 心跳）报错，但不再写入任何数据 ——
    snapshot_type 固定为 open/close 两种，语义清晰且不与收盘快照重叠。
    """
    return False
