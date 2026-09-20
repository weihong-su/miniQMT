"""
miniqmt_autobuy 配置解析模块。

读取 autobuy/miniqmt_autobuy.cfg (INI 格式) → AutoBuyConfig 数据类，提供默认值与校验。
同时提供 autobuy 专用的独立 logger (输出到 logs/miniqmt_autobuy.log)，
与主程序日志隔离，便于复盘。
"""
from __future__ import annotations

import configparser
import logging
import os
import re
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler

# 默认配置文件路径(autobuy 模块目录)，运行日志仍写入项目根目录。
MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(MODULE_DIR)
DEFAULT_CFG_PATH = os.path.join(MODULE_DIR, "miniqmt_autobuy.cfg")
LOG_PATH = os.environ.get(
    "MINIQMT_AUTOBUY_LOG_PATH",
    os.path.join(PROJECT_ROOT, "logs", "miniqmt_autobuy.log"),
)

# SQL 标识符白名单(防注入): 表名/字段名只允许字母数字下划线
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ---------------------------------------------------------------------------
# 独立 logger
# ---------------------------------------------------------------------------
def get_autobuy_logger(name: str = "autobuy") -> logging.Logger:
    """获取 autobuy 专用 logger，幂等地配置文件 + 控制台双输出。"""
    logger = logging.getLogger(name)
    if getattr(logger, "_autobuy_configured", False):
        return logger

    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 文件只记 INFO+，控制体积；DEBUG 级决策明细已落库 autobuy.db，无需重复落盘。
    file_handler = RotatingFileHandler(
        LOG_PATH, maxBytes=3 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    logger._autobuy_configured = True  # type: ignore[attr-defined]
    return logger


# ---------------------------------------------------------------------------
# 配置数据类
# ---------------------------------------------------------------------------
@dataclass
class AutoBuyConfig:
    # [web]
    base_url: str = "http://127.0.0.1:5000"
    api_token: str = ""
    timeout: float = 8.0

    # [pool]
    db_path: str = r"C:\github-repo\stockquant\chan.db"
    tables: list = field(default_factory=lambda: ["stg_chan", "zs_pool"])
    code_column: str = "code"
    date_column: str = "date"
    # 以运行日为基准，取前 N 个交易日的候选池记录
    latest_n_dates: int = 2

    # [filter]
    enable_turnover_rate: bool = True
    min_turnover_rate: float = 0.05
    volume_unit_multiplier: float = 100.0
    enable_volume_ratio: bool = False
    min_volume_ratio: float = 2.0
    # 近 N 个已收盘交易日的收盘量比须全部 >= 阈值。
    # 与 enable_volume_ratio 的区别: 后者用盘中累计量(分子分母时间跨度不对等，
    # 盘中早段必然偏低)；本条用已收盘完整交易日，口径对等且盘中不漂移。
    enable_recent_volume_ratio: bool = True
    recent_volume_ratio_days: int = 2
    min_recent_volume_ratio: float = 1.2
    volume_ratio_baseline_days: int = 5
    enable_pct_change: bool = False
    min_pct_change: float = 0.05
    enable_ma8_uptrend: bool = True
    enable_price_below_ma8_ratio: bool = True
    max_price_to_ma8_ratio: float = 1.07
    # 买入点须落在 MA20 的 [min, max] 偏离区间内(-0.03 = MA20 下方 3%)
    enable_ma20_range: bool = True
    min_price_to_ma20_deviation: float = -0.03
    max_price_to_ma20_deviation: float = 0.05
    skip_limit_up: bool = True
    # 跳过 ST/*ST/退市整理股: 退市风险 + 跌停幅度受限(创业板ST为20%)，
    # 跌停板上止损常常无法成交，实际亏损会远超 STOP_LOSS_RATIO
    skip_st: bool = True

    # [risk]
    dedup_by_position: bool = True
    dedup_window_days: int = 1
    max_buys_per_run: int = 1
    # 模拟运行: 除不发真实买入请求外，候选池/门禁/防重/条件检查/落库全部照常执行
    simulation_mode: bool = False

    # [schedule]
    mode: str = "both"
    daily_times: list = field(default_factory=lambda: [(14, 45)])
    interval_minutes: int = 30
    only_trade_time: bool = True

    def validate(self) -> None:
        """校验关键字段，非法时抛 ValueError。"""
        for name in ("code_column", "date_column"):
            val = getattr(self, name)
            if not _IDENT_RE.match(val):
                raise ValueError(f"[pool] {name} 非法标识符(仅允许字母数字下划线): {val!r}")
        if not self.tables:
            raise ValueError("[pool] tables 不能为空")
        for tbl in self.tables:
            if not _IDENT_RE.match(tbl):
                raise ValueError(f"[pool] table 非法标识符(仅允许字母数字下划线): {tbl!r}")
        if self.mode not in ("daily", "interval", "both"):
            raise ValueError(f"[schedule] mode 非法: {self.mode!r} (应为 daily/interval/both)")
        if self.latest_n_dates < 1:
            raise ValueError(f"[pool] latest_n_dates 必须 >= 1: {self.latest_n_dates}")
        if self.interval_minutes <= 0:
            raise ValueError(f"[schedule] interval_minutes 必须 > 0: {self.interval_minutes}")
        if self.max_buys_per_run < 1:
            raise ValueError(f"[risk] max_buys_per_run 必须 >= 1: {self.max_buys_per_run}")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError(f"[web] base_url 必须以 http(s):// 开头: {self.base_url!r}")
        if self.min_price_to_ma20_deviation > self.max_price_to_ma20_deviation:
            raise ValueError(
                f"[filter] MA20 区间下界不得大于上界: "
                f"{self.min_price_to_ma20_deviation} > {self.max_price_to_ma20_deviation}"
            )
        if self.recent_volume_ratio_days < 1:
            raise ValueError(
                f"[filter] recent_volume_ratio_days 必须 >= 1: {self.recent_volume_ratio_days}"
            )
        if self.volume_ratio_baseline_days < 1:
            raise ValueError(
                f"[filter] volume_ratio_baseline_days 必须 >= 1: {self.volume_ratio_baseline_days}"
            )


def _parse_daily_times(raw: str) -> list:
    """'09:35,14:45' → [(9,35),(14,45)]。非法项跳过。"""
    result = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            hh, mm = part.split(":")
            h, m = int(hh), int(mm)
            if 0 <= h <= 23 and 0 <= m <= 59:
                result.append((h, m))
        except ValueError:
            continue
    return result


def load_config(cfg_path: str = DEFAULT_CFG_PATH) -> AutoBuyConfig:
    """加载并校验配置文件。文件不存在则抛 FileNotFoundError。"""
    if not os.path.exists(cfg_path):
        raise FileNotFoundError(f"autobuy 配置文件不存在: {cfg_path}")

    parser = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    parser.read(cfg_path, encoding="utf-8")
    cfg = AutoBuyConfig()

    def g(section, option, fallback):
        return parser.get(section, option, fallback=fallback) if parser.has_section(section) else fallback

    def gb(section, option, fallback):
        if parser.has_section(section) and parser.has_option(section, option):
            return parser.getboolean(section, option, fallback=fallback)
        return fallback

    def gf(section, option, fallback):
        if parser.has_section(section) and parser.has_option(section, option):
            try:
                return parser.getfloat(section, option)
            except ValueError:
                return fallback
        return fallback

    def gi(section, option, fallback):
        if parser.has_section(section) and parser.has_option(section, option):
            try:
                return parser.getint(section, option)
            except ValueError:
                return fallback
        return fallback

    # [web]
    cfg.base_url = g("web", "base_url", cfg.base_url).strip().rstrip("/")
    # 环境变量覆写 base_url: 实际 Flask 端口由 WEB_SERVER_PORT + 账号索引决定，
    # 与 cfg 里的静态值常不一致。miniqmt.bat 启动时会探测真实端口并注入此变量。
    _env_base = os.environ.get("MINIQMT_AUTOBUY_BASE_URL", "").strip()
    if _env_base:
        cfg.base_url = _env_base.rstrip("/")
    cfg.api_token = g("web", "api_token", cfg.api_token).strip()
    # cfg 未填 token 时回退到主程序同源的 QMT_API_TOKEN，
    # 避免"web 启用了鉴权但 cfg 忘了填"导致持仓查询 401 → 整轮跳过买入。
    # autobuy 是独立进程，.env 不会自动进环境变量，故复用主 config 的加载器
    # (优先级：已存在的环境变量 > .env，与主程序一致)。
    if not cfg.api_token:
        try:
            from config import _load_dotenv_fallback
            _load_dotenv_fallback()
        except Exception:
            pass
        cfg.api_token = os.environ.get("QMT_API_TOKEN", "").strip()
    cfg.timeout = gf("web", "timeout", cfg.timeout)

    # [pool]
    cfg.db_path = g("pool", "db_path", cfg.db_path).strip()
    # 多表并集: 支持 tables=stg_chan, zs_pool; 向后兼容单 table=
    tables_raw = g("pool", "tables", "")
    if not tables_raw.strip():
        tables_raw = g("pool", "table", "")
    if tables_raw.strip():
        parsed_tables = [t.strip() for t in tables_raw.split(",") if t.strip()]
        if parsed_tables:
            cfg.tables = parsed_tables
    cfg.code_column = g("pool", "code_column", cfg.code_column).strip()
    # 向后兼容旧名 added_time_column
    cfg.date_column = g("pool", "date_column", g("pool", "added_time_column", cfg.date_column)).strip()
    # 向后兼容旧名 lookback_days
    cfg.latest_n_dates = gi("pool", "latest_n_dates", gi("pool", "lookback_days", cfg.latest_n_dates))

    # [filter]
    cfg.enable_turnover_rate = gb("filter", "enable_turnover_rate", cfg.enable_turnover_rate)
    cfg.min_turnover_rate = gf("filter", "min_turnover_rate", cfg.min_turnover_rate)
    cfg.volume_unit_multiplier = gf("filter", "volume_unit_multiplier", cfg.volume_unit_multiplier)
    cfg.enable_volume_ratio = gb("filter", "enable_volume_ratio", cfg.enable_volume_ratio)
    cfg.min_volume_ratio = gf("filter", "min_volume_ratio", cfg.min_volume_ratio)
    cfg.enable_recent_volume_ratio = gb(
        "filter", "enable_recent_volume_ratio", cfg.enable_recent_volume_ratio
    )
    cfg.recent_volume_ratio_days = gi(
        "filter", "recent_volume_ratio_days", cfg.recent_volume_ratio_days
    )
    cfg.min_recent_volume_ratio = gf(
        "filter", "min_recent_volume_ratio", cfg.min_recent_volume_ratio
    )
    cfg.volume_ratio_baseline_days = gi(
        "filter", "volume_ratio_baseline_days", cfg.volume_ratio_baseline_days
    )
    cfg.enable_pct_change = gb("filter", "enable_pct_change", cfg.enable_pct_change)
    cfg.min_pct_change = gf("filter", "min_pct_change", cfg.min_pct_change)
    cfg.enable_ma8_uptrend = gb("filter", "enable_ma8_uptrend", cfg.enable_ma8_uptrend)
    cfg.enable_price_below_ma8_ratio = gb("filter", "enable_price_below_ma8_ratio", cfg.enable_price_below_ma8_ratio)
    cfg.max_price_to_ma8_ratio = gf("filter", "max_price_to_ma8_ratio", cfg.max_price_to_ma8_ratio)
    cfg.enable_ma20_range = gb("filter", "enable_ma20_range", cfg.enable_ma20_range)
    cfg.min_price_to_ma20_deviation = gf(
        "filter", "min_price_to_ma20_deviation", cfg.min_price_to_ma20_deviation
    )
    cfg.max_price_to_ma20_deviation = gf(
        "filter", "max_price_to_ma20_deviation", cfg.max_price_to_ma20_deviation
    )
    cfg.skip_limit_up = gb("filter", "skip_limit_up", cfg.skip_limit_up)
    cfg.skip_st = gb("filter", "skip_st", cfg.skip_st)

    # [risk]
    cfg.dedup_by_position = gb("risk", "dedup_by_position", cfg.dedup_by_position)
    cfg.dedup_window_days = gi("risk", "dedup_window_days", cfg.dedup_window_days)
    cfg.max_buys_per_run = gi("risk", "max_buys_per_run", cfg.max_buys_per_run)
    cfg.simulation_mode = gb("risk", "simulation_mode", cfg.simulation_mode)

    # [schedule]
    cfg.mode = g("schedule", "mode", cfg.mode).strip().lower()
    daily_raw = g("schedule", "daily_times", "")
    if daily_raw.strip():
        parsed = _parse_daily_times(daily_raw)
        if parsed:
            cfg.daily_times = parsed
    cfg.interval_minutes = gi("schedule", "interval_minutes", cfg.interval_minutes)
    cfg.only_trade_time = gb("schedule", "only_trade_time", cfg.only_trade_time)

    cfg.validate()
    return cfg
