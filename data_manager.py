"""
数据管理模块，负责历史数据的获取与存储
"""
import os
import pandas as pd
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
import threading
import xtquant.xtdata as xt
import Methods
import config
from logger import get_logger, suppress_stdout_stderr
# from realtime_data_manager import get_realtime_data_manager

# 获取logger
logger = get_logger("data_manager")


def _create_xtdata():
    """
    工厂函数：根据 config.ENABLE_XTQUANT_MANAGER 返回行情接口对象。

    Returns:
        XtDataAdapter: ENABLE_XTQUANT_MANAGER=True 时，返回 HTTP 适配器
        xtquant.xtdata module: ENABLE_XTQUANT_MANAGER=False 时，返回原始模块
    """
    if getattr(config, "ENABLE_XTQUANT_MANAGER", False):
        from xtquant_manager.client import XtQuantClient, ClientConfig, XtDataAdapter
        account_config = config.get_account_config()
        client = XtQuantClient(
            config=ClientConfig(
                base_url=getattr(config, "XTQUANT_MANAGER_URL", "http://127.0.0.1:8888"),
                account_id=account_config.get("account_id", ""),
                api_token=getattr(config, "XTQUANT_MANAGER_TOKEN", ""),
            )
        )
        return XtDataAdapter(client)
    else:
        import xtquant.xtdata as _xtdata
        return _xtdata


class MarketDataHealthTracker:
    """内存版行情源健康评分器，不落库，重启即清空。"""

    def __init__(self):
        self._events = defaultdict(
            lambda: deque(maxlen=getattr(config, "MARKET_HEALTH_MAX_EVENTS", 100))
        )
        self._lock = threading.Lock()

    def record(self, source, purpose, stock_code, ok, latency_ms=0, reason="", data_quality_ok=True):
        if not getattr(config, "MARKET_HEALTH_ENABLED", True):
            return

        key = (
            str(source or "unknown"),
            str(purpose or "unknown"),
            self._normalize_stock_code(stock_code),
        )
        event = {
            "ts": time.time(),
            "ok": bool(ok),
            "latency_ms": max(0, int(latency_ms or 0)),
            "reason": str(reason or ("success" if ok else "unknown")),
            "data_quality_ok": bool(data_quality_ok),
        }
        with self._lock:
            self._events[key].append(event)

    def get_score(self, source=None, purpose=None, stock_code=None):
        events = self._collect_events(source, purpose, stock_code)
        return self._score_events(events)

    def snapshot(self):
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            keys = list(self._events.keys())

        overall = self.get_score()
        sources = {}
        stocks = {}
        for source in sorted({k[0] for k in keys}):
            sources[source] = self.get_score(source=source)

        for stock_code in sorted({k[2] for k in keys}):
            stock_entry = stocks.setdefault(stock_code, {})
            for purpose in sorted({k[1] for k in keys if k[2] == stock_code}):
                purpose_entry = self.get_score(purpose=purpose, stock_code=stock_code)
                purpose_entry["sources"] = {}
                for source in sorted({k[0] for k in keys if k[1] == purpose and k[2] == stock_code}):
                    purpose_entry["sources"][source] = self.get_score(
                        source=source,
                        purpose=purpose,
                        stock_code=stock_code,
                    )
                stock_entry[purpose] = purpose_entry

        return {
            "enabled": getattr(config, "MARKET_HEALTH_ENABLED", True),
            "observe_only": getattr(config, "MARKET_HEALTH_OBSERVE_ONLY", True),
            "generated_at": generated_at,
            "overall": overall,
            "sources": sources,
            "stocks": stocks,
            "trading": {
                "min_score": getattr(config, "MARKET_HEALTH_TRADING_MIN_SCORE", 70),
                "allow_mootdx": getattr(config, "MARKET_HEALTH_ALLOW_MOOTDX_FOR_TRADING", False),
            },
        }

    def format_summary(self):
        snapshot = self.snapshot()
        sources = snapshot.get("sources", {})
        if not sources:
            return "行情健康: 暂无样本"

        parts = []
        bad_stocks = []
        unstable_score = getattr(config, "MARKET_HEALTH_UNSTABLE_SCORE", 40)
        for source, info in sorted(sources.items()):
            score = info.get("score")
            if source == "Mootdx" and score is None:
                parts.append(f"{source}=idle")
                continue
            score_text = "unknown" if score is None else str(score)
            parts.append(f"{source}={info.get('status', 'unknown')}({score_text})")

        for stock_code, purposes in snapshot.get("stocks", {}).items():
            realtime = purposes.get("realtime", {})
            score = realtime.get("score")
            if score is not None and score < unstable_score:
                bad_stocks.append(stock_code)

        if bad_stocks:
            parts.append("异常股票:" + ",".join(bad_stocks[:5]))
        return "行情健康: " + " | ".join(parts)

    def _collect_events(self, source=None, purpose=None, stock_code=None):
        stock_code = self._normalize_stock_code(stock_code) if stock_code else None
        now = time.time()
        window_seconds = getattr(config, "MARKET_HEALTH_WINDOW_SECONDS", 300)
        events = []
        with self._lock:
            for key, values in self._events.items():
                key_source, key_purpose, key_stock = key
                if source and key_source != source:
                    continue
                if purpose and key_purpose != purpose:
                    continue
                if stock_code and key_stock != stock_code:
                    continue
                events.extend(event for event in values if now - event["ts"] <= window_seconds)
        events.sort(key=lambda event: event["ts"])
        return events

    def _score_events(self, events):
        if not events:
            return {
                "score": None,
                "status": "unknown",
                "event_count": 0,
                "success_count": 0,
                "failure_count": 0,
                "consecutive_failures": 0,
                "last_success_at": None,
                "last_event_at": None,
                "avg_latency_ms": None,
                "last_reason": None,
            }

        total = len(events)
        success_events = [event for event in events if event["ok"] and event["data_quality_ok"]]
        success_count = len(success_events)
        failure_count = total - success_count

        if success_events:
            avg_latency = sum(event["latency_ms"] for event in success_events) / success_count
            last_success_ts = success_events[-1]["ts"]
            last_success_at = datetime.fromtimestamp(last_success_ts).strftime("%Y-%m-%d %H:%M:%S")
        else:
            avg_latency = None
            last_success_at = None

        consecutive_failures = 0
        for event in reversed(events):
            if event["ok"] and event["data_quality_ok"]:
                break
            consecutive_failures += 1

        base_result = {
            "event_count": total,
            "success_count": success_count,
            "failure_count": failure_count,
            "consecutive_failures": consecutive_failures,
            "last_success_at": last_success_at,
            "last_event_at": datetime.fromtimestamp(events[-1]["ts"]).strftime("%Y-%m-%d %H:%M:%S"),
            "avg_latency_ms": None if avg_latency is None else int(round(avg_latency)),
            "last_reason": events[-1]["reason"],
        }

        min_events = max(1, int(getattr(config, "MARKET_HEALTH_MIN_EVENTS", 3) or 1))
        if total < min_events:
            return {
                "score": None,
                "status": "unknown",
                **base_result,
            }

        success_rate_score = 100 * success_count / total
        quality_score = 100 * sum(1 for event in events if event["data_quality_ok"]) / total

        if success_events:
            latency_score = max(0, 100 - (avg_latency / 30))
            age = max(0, time.time() - success_events[-1]["ts"])
            window_seconds = max(1, getattr(config, "MARKET_HEALTH_WINDOW_SECONDS", 300))
            freshness_score = max(0, 100 - (age / window_seconds * 100))
        else:
            latency_score = 0
            freshness_score = 0

        consecutive_score = max(0, 100 - consecutive_failures * 25)
        score = (
            success_rate_score * 0.45
            + latency_score * 0.20
            + freshness_score * 0.20
            + quality_score * 0.10
            + consecutive_score * 0.05
        )
        score = int(round(max(0, min(100, score))))

        return {
            "score": score,
            "status": self._status_for_score(score),
            **base_result,
        }

    def _status_for_score(self, score):
        if score is None:
            return "unknown"
        if score >= getattr(config, "MARKET_HEALTH_HEALTHY_SCORE", 80):
            return "healthy"
        if score >= getattr(config, "MARKET_HEALTH_DEGRADED_SCORE", 60):
            return "degraded"
        if score >= getattr(config, "MARKET_HEALTH_UNSTABLE_SCORE", 40):
            return "unstable"
        return "down"

    def _normalize_stock_code(self, stock_code):
        if not stock_code:
            return "unknown"
        return str(stock_code).upper()


class DataManager:
    """数据管理类，处理历史行情数据的获取与存储"""
    
    def __init__(self):
        """初始化数据管理器"""
        # 创建数据目录
        if not os.path.exists(config.DATA_DIR):
            os.makedirs(config.DATA_DIR)
            
        # 连接数据库
        self.conn = self._connect_db()
        # 数据库操作锁，防止多线程并发访问同一连接导致事务冲突
        self._db_lock = threading.Lock()

        # 创建表结构
        self._create_tables()
        
        # 已订阅的股票代码列表
        self.subscribed_stocks = []

        # 股票名称缓存（在 __init__ 中预先创建，避免运行时懒加载触发 baostock）
        self.stock_names_cache = {}

        # baostock 连续失败冷却机制（防止网络异常时反复阻塞）
        self._bs_consecutive_failures = 0
        self._bs_cooldown_until = 0.0  # 冷却截止时间戳

        # Tushare 客户端惰性初始化与冷却机制（与 baostock 机制对称）
        self._tushare_pro = None
        self._tushare_token_attempted = False
        self._ts_consecutive_failures = 0
        self._ts_cooldown_until = 0.0  # 冷却截止时间戳

        # 行情源健康评分（仅内存，不持久化）
        self.market_health = MarketDataHealthTracker()

        # 历史数据更新节流与告警降噪状态
        self._history_update_attempts = {}
        self._history_invalid_date_warnings = {}

        # xtdata断连自动重连控制
        self._xtdata_reconnect_lock = threading.Lock()
        self._xtdata_last_reconnect_time = 0.0
        self._xtdata_reconnect_errors = 0
        self._xtdata_reconnect_threshold = getattr(config, "XTQUANT_DATA_RECONNECT_ON_ERRORS", 3)
        self._xtdata_reconnect_interval = getattr(config, "XTQUANT_DATA_RECONNECT_INTERVAL", 60)
        
        # # 初始化行情接口 
        self._init_xtquant()
        # self.realtime_manager = get_realtime_data_manager()        

        # 数据更新线程
        self.update_thread = None
        self.stop_flag = False

    def _init_xtquant(self):
        """初始化行情接口（根据 ENABLE_XTQUANT_MANAGER 选择本地或 HTTP 适配器）"""
        try:
            self.xt = _create_xtdata()

            if self.xt.connect():
                logger.info("行情服务连接成功")
            else:
                logger.error("行情服务连接失败")
                self.xt = None
                return

            # 验证连接状态
            self._verify_connection()

            # 订阅股票池，确保交易时段 get_full_tick 返回实时价格
            self._subscribe_stocks_to_xtdata(config.STOCK_POOL)

        except Exception as e:
            logger.error(f"初始化行情接口出错: {str(e)}")
            self.xt = None

    def reinit_xtquant(self):
        """重新初始化行情接口。
        用于 ENABLE_XTQUANT_MANAGER=True 时的延迟重连：
        web_server.py 模块加载时 XtQuantManager HTTP 服务尚未启动，导致首次连接失败。
        main.py 在 HTTP 服务启动后调用此方法补充初始化。
        """
        if self.xt is not None:
            return  # 已成功初始化，无需重连
        logger.info("行情接口重新初始化（XtQuantManager 服务已就绪）")
        self._init_xtquant()

    def _subscribe_stocks_to_xtdata(self, stock_list):
        """订阅股票池到 xtdata，保证交易时段 get_full_tick 返回实时推送价格。
        subscribe_quote(count=0) 表示只订阅实时推送，不拉取历史数据。

        注册 _on_xtdata_tick 回调，xtdata 推送的实时 tick 直接写入 _tick_cache，
        供 get_latest_data 在盘中优先读取（避免每轮轮询 get_full_tick 的线程池开销）。
        """
        if not self.xt or not stock_list:
            return
        ok, fail = 0, 0
        for stock in stock_list:
            code = self._adjust_stock(stock)
            try:
                self.xt.subscribe_quote(
                    code,
                    period='tick',
                    start_time='',
                    end_time='',
                    count=0,
                    callback=self._on_xtdata_tick,
                )
                if code not in self.subscribed_stocks:
                    self.subscribed_stocks.append(code)
                ok += 1
            except Exception as e:
                logger.warning(f"xtdata subscribe_quote 失败: {code} - {e}")
                fail += 1
        logger.info(f"xtdata 订阅股票池完成: 成功 {ok} 只，失败 {fail} 只，共 {ok+fail} 只")

    def _on_xtdata_tick(self, data):
        """xtdata tick 订阅回调：把 xtdata 推送的实时行情写入内存缓存。

        xtdata 在交易时段持续推送，盘中 get_latest_data 优先从此缓存读取，
        避免每轮轮询 get_full_tick 的线程池开销；缓存不可用时自动降级到原有轮询路径。
        """
        if not data or not isinstance(data, dict):
            return
        if not getattr(self, '_tick_cache', None):
            self._tick_cache = {}
        for stock_code, tick in data.items():
            if tick and isinstance(tick, dict):
                self._tick_cache[stock_code] = tick

    def _get_tick_from_cache(self, stock_code):
        """从 xtdata 推送缓存获取 tick 数据。

        缓存命中且 lastPrice > 0（有效价格）时直接返回，记录健康评分并重置 xtdata 失败计数。
        缓存命中但 lastPrice = 0 时清掉脏缓存让上层降级。
        缓存未命中返回 None。
        """
        cache = getattr(self, '_tick_cache', {}) or {}
        tick = cache.get(stock_code)
        if not tick or not isinstance(tick, dict):
            return None
        if tick.get('lastPrice', 0) <= 0:
            cache.pop(stock_code, None)
            return None
        self._record_market_health("xtdata", "realtime", stock_code, True, 0, reason="tick_cache")
        self._reset_xtdata_failure()
        return self._decorate_quote(tick, "xtdata", "realtime", stock_code, 0)

    def ensure_subscribed(self, stock_code):
        """确保股票已订阅到 xtdata 实时推送。
        盘中新持仓加入时调用，保证下一个心跳就能拿到实时价格。
        已订阅则直接返回；未订阅则立即订阅并追踪到 subscribed_stocks。
        """
        if not self.xt:
            return
        code = self._adjust_stock(stock_code)
        if code in self.subscribed_stocks:
            return  # 已订阅，无需重复操作
        try:
            self.xt.subscribe_quote(
                code,
                period='tick',
                start_time='',
                end_time='',
                count=0,
                callback=self._on_xtdata_tick,
            )
            self.subscribed_stocks.append(code)
            logger.info(f"xtdata 动态订阅新股票: {code}")
        except Exception as e:
            logger.warning(f"xtdata 动态订阅失败: {code} - {e}")

    def prune_untracked_stocks(self, active_stock_codes):
        """移除已不在运行态股票池中的订阅和 tick 缓存记录。"""
        active_codes = {
            self._adjust_stock(code)
            for code in (active_stock_codes or [])
            if code is not None and str(code).strip()
        }
        before = list(getattr(self, 'subscribed_stocks', []) or [])
        kept = [code for code in before if self._adjust_stock(code) in active_codes]
        removed = [code for code in before if self._adjust_stock(code) not in active_codes]
        if removed:
            self.subscribed_stocks = kept
            cache = getattr(self, '_tick_cache', None)
            if isinstance(cache, dict):
                for code in removed:
                    cache.pop(code, None)
                    cache.pop(str(code).split('.')[0], None)
            logger.info(f"已清理运行态股票池外的行情订阅缓存: {removed}")
        return removed

    def _verify_connection(self):
        """验证连接状态"""
        try:
            # 使用一个简单的测试来验证连接
            test_codes = ['000001.SZ']  # 测试股票
            test_data = self.xt.get_full_tick(test_codes)
            if test_data:
                logger.debug("xtquant连接状态验证成功")
                return True
            else:
                logger.warning("xtquant连接状态验证失败")
                return False
        except Exception as e:
            logger.warning(f"xtquant连接验证出错: {str(e)}")
            return False

    def _record_xtdata_failure(self, reason=""):
        """记录xtdata失败并按阈值触发重连"""
        self._xtdata_reconnect_errors += 1
        if self._xtdata_reconnect_errors >= self._xtdata_reconnect_threshold:
            if self._attempt_xtdata_reconnect(reason):
                self._xtdata_reconnect_errors = 0

    def _reset_xtdata_failure(self):
        """重置xtdata失败计数"""
        self._xtdata_reconnect_errors = 0

    def _attempt_xtdata_reconnect(self, reason=""):
        """尝试重连xtdata并恢复订阅"""
        if not self.xt:
            return False

        with self._xtdata_reconnect_lock:
            now = time.time()
            if now - self._xtdata_last_reconnect_time < self._xtdata_reconnect_interval:
                return False
            self._xtdata_last_reconnect_time = now

        try:
            reconnect_ok = False
            if hasattr(self.xt, "reconnect"):
                reconnect_ok = self.xt.reconnect()
            elif hasattr(self.xt, "connect"):
                reconnect_ok = self.xt.connect()

            if reconnect_ok:
                # 复订阅：优先使用已订阅列表，否则用股票池
                stocks = list(dict.fromkeys(self.subscribed_stocks)) if self.subscribed_stocks else list(config.STOCK_POOL)
                if stocks:
                    self._subscribe_stocks_to_xtdata(stocks)
                return True
            return False
        except Exception as e:
            logger.warning(f"xtdata重连失败: {e}")
            return False

    def _get_market_health(self):
        if not hasattr(self, "market_health") or self.market_health is None:
            self.market_health = MarketDataHealthTracker()
        return self.market_health

    def _record_market_health(self, source, purpose, stock_code, ok, latency_ms=0,
                              reason="", data_quality_ok=True):
        self._get_market_health().record(
            source=source,
            purpose=purpose,
            stock_code=stock_code,
            ok=ok,
            latency_ms=latency_ms,
            reason=reason,
            data_quality_ok=data_quality_ok,
        )

    def get_market_health_snapshot(self):
        return self._get_market_health().snapshot()

    def format_market_health_summary(self):
        return self._get_market_health().format_summary()

    def is_quote_tradable(self, stock_code, quote):
        if not getattr(config, "MARKET_HEALTH_ENABLED", True):
            return True
        if getattr(config, "MARKET_HEALTH_OBSERVE_ONLY", True):
            return True
        if not quote:
            return False

        source = quote.get("_source")
        if source == "Mootdx" and not getattr(config, "MARKET_HEALTH_ALLOW_MOOTDX_FOR_TRADING", False):
            return False

        score = quote.get("_health_score")
        if score is None:
            health = self._get_market_health().get_score(
                source=source,
                purpose=quote.get("_purpose", "realtime"),
                stock_code=stock_code,
            )
            score = health.get("score")
        if score is None:
            return False
        return score >= getattr(config, "MARKET_HEALTH_TRADING_MIN_SCORE", 70)

    def _decorate_quote(self, quote, source, purpose, stock_code, latency_ms=0):
        if not isinstance(quote, dict):
            return quote
        decorated = dict(quote)
        decorated["_source"] = source
        decorated["_purpose"] = purpose
        decorated["_latency_ms"] = int(max(0, latency_ms or 0))
        decorated["_health_score"] = self._get_market_health().get_score(
            source=source,
            purpose=purpose,
            stock_code=stock_code,
        ).get("score")
        return decorated

    def _connect_db(self):
        """连接SQLite数据库"""
        try:
            # ⭐ 超时优化：添加30秒超时和WAL模式
            conn = sqlite3.connect(
                config.DB_PATH,
                timeout=30.0,  # 30秒超时（默认5秒）
                check_same_thread=False
            )
            # 启用WAL模式，允许读写并发（减少锁冲突）
            conn.execute('PRAGMA journal_mode=WAL')
            logger.info(f"已连接数据库: {config.DB_PATH}")
            return conn
        except Exception as e:
            logger.error(f"连接数据库失败: {str(e)}")
            raise
    
    def _create_tables(self):
        """创建数据表结构"""
        cursor = self.conn.cursor()
        
        # 创建股票历史数据表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS stock_daily_data (
            stock_code TEXT,
            stock_name TEXT,            
            date TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            amount REAL,
            PRIMARY KEY (stock_code, date)
        )
        ''')
        
        # 创建指标数据表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS stock_indicators (
            stock_code TEXT,
            date TEXT,
            ma10 REAL,
            ma20 REAL,
            ma30 REAL,
            ma60 REAL,
            macd REAL,
            macd_signal REAL,
            macd_hist REAL,
            PRIMARY KEY (stock_code, date)
        )
        ''')
        
        # 创建交易记录表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS trade_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_code TEXT,
            stock_name TEXT,            
            trade_time TIMESTAMP,
            trade_type TEXT,  -- BUY, SELL
            price REAL,
            volume INTEGER,
            amount REAL,
            trade_id TEXT,
            commission REAL,
            strategy TEXT
        )
        ''')
        
        # 创建持仓表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS positions (
            stock_code TEXT PRIMARY KEY,
            stock_name TEXT,
            volume INTEGER,
            available REAL,
            cost_price REAL,
            base_cost_price REAL,
            current_price REAL,
            market_value REAL,
            profit_ratio REAL,
            last_update TIMESTAMP,
            open_date TIMESTAMP,
            profit_triggered BOOLEAN DEFAULT FALSE,
            highest_price REAL,
            stop_loss_price REAL,
            profit_breakout_triggered BOOLEAN DEFAULT FALSE,
            breakout_highest_price REAL
        )
        ''')

        # 创建网格交易表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS grid_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_code TEXT,
            grid_level INTEGER,
            buy_price REAL,
            sell_price REAL,
            volume INTEGER,
            status TEXT,  -- PENDING, ACTIVE, COMPLETED
            create_time TIMESTAMP,
            update_time TIMESTAMP
        )
        ''')

        # 盘前同步调度持久化（premarket_sync.PreMarketSyncScheduler 读写）
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS premarket_schedule (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            next_sync_time TIMESTAMP NOT NULL,
            last_sync_time TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')

        # 盘前同步历史（premarket_sync.record_sync_history 写入）
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS premarket_sync_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sync_time TIMESTAMP NOT NULL,
            configs_synced INTEGER DEFAULT 0,
            switches_synced INTEGER DEFAULT 0,
            xtdata_reconnected BOOLEAN DEFAULT 0,
            xttrader_reconnected BOOLEAN DEFAULT 0,
            connection_status TEXT,
            positions_synced BOOLEAN DEFAULT 0,
            errors TEXT,
            execution_time_ms INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')

        self.conn.commit()
        logger.info("数据表结构已创建")

        # 兼容老 DB：缺失列幂等补齐（CREATE TABLE IF NOT EXISTS 不会改已有表的 schema）
        self._migrate_legacy_schema()

        # 启动时修复可能损坏的索引（机器强制重启后可能出现索引损坏）
        self._repair_indexes_if_needed()

    def _migrate_legacy_schema(self):
        """幂等地为旧版 DB 补齐缺失字段。

        历史上 positions 表分批添加过 profit_breakout_triggered /
        breakout_highest_price，多账号下新建 data_<account_id>/trading.db
        如果首次创建时代码已有这两列就 OK；但已经在更早版本上创建过的
        老 DB（包括用户在跑的两个账号 DB），仍缺这两列，会导致
        position_manager 同步线程持续报 "no such column" 错误。
        """
        try:
            cursor = self.conn.cursor()
            cursor.execute("PRAGMA table_info(positions)")
            existing_cols = {row[1] for row in cursor.fetchall()}

            migrations = [
                ('profit_breakout_triggered', 'BOOLEAN DEFAULT FALSE'),
                ('breakout_highest_price',    'REAL'),
            ]
            for col, typedef in migrations:
                if col not in existing_cols:
                    cursor.execute(f"ALTER TABLE positions ADD COLUMN {col} {typedef}")
                    logger.info(f"DB迁移: positions 表已补齐字段 {col}")

            self.conn.commit()
        except Exception as e:
            logger.error(f"DB迁移失败（不影响运行,但会持续报字段缺失）: {e}")

    def _repair_indexes_if_needed(self):
        """检查并修复损坏的数据库索引（快速完整性检查）"""
        try:
            result = self.conn.execute("PRAGMA quick_check").fetchone()
            if result and result[0] != 'ok':
                logger.warning(f"检测到数据库索引异常: {result[0]}，执行 REINDEX 修复...")
                self.conn.execute("REINDEX")
                self.conn.commit()
                # 再次验证
                result2 = self.conn.execute("PRAGMA quick_check").fetchone()
                if result2 and result2[0] == 'ok':
                    logger.info("数据库索引修复成功")
                else:
                    logger.error(f"数据库索引修复后仍有异常: {result2[0] if result2 else '未知'}")
            else:
                logger.debug("数据库索引完整性检查通过")
        except Exception as e:
            logger.warning(f"数据库索引检查失败（不影响运行）: {str(e)}")
    
    # def _init_xtquant(self):
    #     """初始化迅投行情接口"""
    #     try:
    #         # 根据文档，首先调用connect连接到行情服务器
    #         if not xt.connect():
    #             logger.error("行情服务连接失败")
    #             return
                
    #         logger.info("行情服务连接成功")
            
    #         # 根据测试结果，我们不使用subscribe_quote方法（会失败）
    #         # 改为验证股票代码是否可以通过get_full_tick获取数据
    #         valid_stocks = []
    #         for stock_code in config.STOCK_POOL:
    #             try:
    #                 stock_code = self._adjust_stock(stock_code)
    #                 # 尝试adjust_stock(stock_code)
    #                 # 尝试获取Tick数据验证股票代码有效性
    #                 tick_data = xt.get_full_tick([stock_code])
    #                 if tick_data and stock_code in tick_data:
    #                     valid_stocks.append(stock_code)
    #                     logger.info(f"股票 {stock_code} 数据获取成功")
    #                 else:
    #                     logger.warning(f"无法获取 {stock_code} 的Tick数据")
    #             except Exception as e:
    #                 logger.warning(f"获取 {stock_code} 的Tick数据失败: {str(e)}")
            
    #         self.subscribed_stocks = valid_stocks
            
    #         if self.subscribed_stocks:
    #             logger.info(f"成功验证 {len(self.subscribed_stocks)} 只股票可获取数据")
    #         else:
    #             logger.warning("没有有效的股票，请检查股票代码格式")
                
    #     except Exception as e:
    #         logger.error(f"初始化迅投行情接口出错: {str(e)}")

    # # 股票代码转换
    # def _select_data_type(self, stock='600031'):
    #     '''
    #     选择数据类型
    #     '''
    #     return Methods.select_data_type(stock)
    
    def _adjust_stock(self, stock='600031.SH'):
        '''
        调整代码
        '''
        stock = str(stock).strip()
        lower_stock = stock.lower()
        if lower_stock.startswith('sh.'):
            return f"{stock.split('.', 1)[1].upper()}.SH"
        if lower_stock.startswith('sz.'):
            return f"{stock.split('.', 1)[1].upper()}.SZ"
        return Methods.add_xt_suffix(stock)

    def _normalize_date_arg(self, value):
        """把外部日期参数统一为 YYYY-MM-DD，便于比较和过滤。"""
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None

        compact = pd.Series([text]).str.replace(r'\D', '', regex=True).iloc[0]
        if len(compact) >= 8:
            parsed = pd.to_datetime(compact[:8], format='%Y%m%d', errors='coerce')
        else:
            parsed = pd.to_datetime(text, errors='coerce')

        if pd.isna(parsed):
            return None
        return parsed.strftime('%Y-%m-%d')

    def _format_xt_history_date(self, value):
        """把日期参数转换为 xtdata 需要的 YYYYMMDD。"""
        normalized = self._normalize_date_arg(value)
        if not normalized:
            return None
        return normalized.replace('-', '')

    def _filter_history_date_range(self, data_df, start_date=None, end_date=None):
        """按请求日期范围裁剪历史数据，主要用于 Mootdx 近 N 条返回后的增量过滤。"""
        if data_df is None or data_df.empty or 'date' not in data_df.columns:
            return data_df

        start = self._normalize_date_arg(start_date)
        end = self._normalize_date_arg(end_date)
        if not start and not end:
            return data_df

        work_df = data_df.copy()
        dates = pd.to_datetime(work_df['date'], errors='coerce')
        mask = pd.Series(True, index=work_df.index)
        if start:
            mask &= dates >= pd.Timestamp(start)
        if end:
            mask &= dates <= pd.Timestamp(end)
        return work_df.loc[mask].copy()

    def _should_log_invalid_history_date_warning(self, stock_code, source):
        """同一股票同一来源的非法日期告警限频，避免生产日志刷屏。"""
        interval = getattr(config, 'HISTORY_INVALID_DATE_LOG_INTERVAL', 600)
        if interval <= 0:
            return True

        if not hasattr(self, '_history_invalid_date_warnings'):
            self._history_invalid_date_warnings = {}

        key = (stock_code, source)
        now = time.time()
        last_time = self._history_invalid_date_warnings.get(key, 0)
        if now - last_time < interval:
            return False

        self._history_invalid_date_warnings[key] = now
        return True

    def _should_throttle_history_update(self, stock_code, start_date):
        """限制同一股票同一增量窗口的历史数据重复更新频率。"""
        interval = getattr(config, 'HISTORY_UPDATE_THROTTLE_SECONDS', 300)
        if interval <= 0:
            return False

        if not hasattr(self, '_history_update_attempts'):
            self._history_update_attempts = {}

        key = str(stock_code)
        start_key = start_date or ''
        now = time.time()
        last = self._history_update_attempts.get(key)
        if last and last.get('start_date') == start_key and now - last.get('time', 0) < interval:
            logger.debug(
                f"{stock_code} 历史数据更新节流中，"
                f"距离上次尝试 {now - last.get('time', 0):.1f}s，start={start_key or 'full'}"
            )
            return True

        self._history_update_attempts[key] = {'time': now, 'start_date': start_key}
        return False

    def _normalize_history_dates(self, data_df, stock_code, source='history'):
        """
        统一清洗历史行情日期列。

        xtdata/Mootdx 偶发返回非法日期（如 13598-74-57 15:00、0-00-00 15:00），
        不能让单行脏数据导致整只指数历史行情失败。
        """
        if data_df is None or data_df.empty:
            return data_df

        work_df = data_df.copy()
        if 'date' not in work_df.columns and 'time' in work_df.columns:
            work_df = work_df.rename(columns={'time': 'date'})

        if 'date' not in work_df.columns:
            return work_df

        raw_dates = work_df['date'].astype(str).str.strip()
        parsed = pd.Series(pd.NaT, index=work_df.index, dtype='datetime64[ns]')

        compact = raw_dates.str.replace(r'\D', '', regex=True)
        mask_yyyymmdd = compact.str.len().eq(8)
        if mask_yyyymmdd.any():
            parsed.loc[mask_yyyymmdd] = pd.to_datetime(
                compact.loc[mask_yyyymmdd],
                format='%Y%m%d',
                errors='coerce'
            )

        mask_yyyymmddhhmmss = compact.str.len().ge(14)
        if mask_yyyymmddhhmmss.any():
            parsed.loc[mask_yyyymmddhhmmss] = pd.to_datetime(
                compact.loc[mask_yyyymmddhhmmss].str.slice(0, 14),
                format='%Y%m%d%H%M%S',
                errors='coerce'
            )

        mask_epoch_ms = raw_dates.str.fullmatch(r'\d{13}', na=False)
        if mask_epoch_ms.any():
            parsed.loc[mask_epoch_ms] = pd.to_datetime(
                pd.to_numeric(compact.loc[mask_epoch_ms], errors='coerce'),
                unit='ms',
                utc=True,
                errors='coerce'
            ).dt.tz_convert('Asia/Shanghai').dt.tz_localize(None)

        mask_epoch_s = raw_dates.str.fullmatch(r'\d{10}', na=False)
        if mask_epoch_s.any():
            parsed.loc[mask_epoch_s] = pd.to_datetime(
                pd.to_numeric(compact.loc[mask_epoch_s], errors='coerce'),
                unit='s',
                utc=True,
                errors='coerce'
            ).dt.tz_convert('Asia/Shanghai').dt.tz_localize(None)

        fallback_mask = parsed.isna() & raw_dates.ne('') & raw_dates.str.lower().ne('nan')
        if fallback_mask.any():
            parsed.loc[fallback_mask] = pd.to_datetime(
                raw_dates.loc[fallback_mask],
                errors='coerce'
            )

        invalid_samples = raw_dates[
            parsed.isna() & raw_dates.ne('') & raw_dates.str.lower().ne('nan')
        ].head(3).tolist()

        initial_count = len(work_df)
        work_df['date'] = parsed.dt.strftime('%Y-%m-%d')
        work_df = work_df.dropna(subset=['date'])
        dropped = initial_count - len(work_df)

        if dropped > 0:
            if self._should_log_invalid_history_date_warning(stock_code, source):
                sample_text = f"，样例: {invalid_samples}" if invalid_samples else ""
                logger.warning(f"{stock_code} 过滤了 {dropped} 行非法历史日期数据({source}){sample_text}")
            else:
                logger.debug(f"{stock_code} 过滤了 {dropped} 行非法历史日期数据({source})，重复告警已降噪")

        return work_df

    def download_history_data(self, stock_code, period=None, start_date=None, end_date=None):
        """
        下载股票历史数据（优先 xtdata，失败后 fallback 到 Mootdx）

        即使 ENABLE_XTQUANT_MANAGER=False，本地 xtdata 连接可用时也优先使用 xtdata；
        Mootdx 仅作为兜底数据源，减少通达信源异常日期和网络超时对生产日志的影响。

        参数:
        stock_code (str): 股票代码
        period (str): 周期，默认为日线 'day'
        start_date (str): 开始日期，格式为'YYYYMMDD' 或 'YYYY-MM-DD'
        end_date (str): 结束日期，格式为'YYYYMMDD' 或 'YYYY-MM-DD'

        返回:
        pandas.DataFrame: 历史数据，若失败则返回None
        """
        # ── 历史数据源策略：xtdata 优先，Mootdx 兜底 ──
        _period_map = {
            'day': '1d', 'week': '1w', 'mon': '1mon',
            '5m': '5m', '15m': '15m', '30m': '30m', '1h': '1h',
            '1d': '1d',  # 直通
        }
        xt_period = _period_map.get(period or 'day', '1d')
        xt_start_date = self._format_xt_history_date(start_date)
        xt_end_date = self._format_xt_history_date(end_date)
        if not xt_start_date:
            xt_start_date = (datetime.now() - timedelta(days=90)).strftime('%Y%m%d')

        # ⚠️ 仅在 XtQuantManager 网关模式下用 xtdata 拉历史数据。
        # 标准模式(ENABLE_XTQUANT_MANAGER=False)走 Mootdx：部分 QMT 客户端的
        # xtdata.get_market_data_ex 会触发底层 BSON 断言 "u < 1000000"
        # (bsonobj.cpp) 直接 abort 整个进程，且 try/except 与超时均无法拦截。
        if getattr(config, 'ENABLE_XTQUANT_MANAGER', False) and self.xt:
            xt_df = self.download_history_xtdata(
                stock_code,
                period=xt_period,
                start_date=xt_start_date,
                end_date=xt_end_date,
            )
            if xt_df is not None and not xt_df.empty:
                return xt_df
            logger.debug(f"xtdata 未返回 {stock_code} 有效历史数据，fallback 到 Mootdx")

        # ── 标准模式：Tushare 优先 → Mootdx 兜底 ──
        # 仅在日线/周线/月线周期时走 Tushare（分钟线需单独购买 2000元/年权限）
        _is_daily_period = period in ('day', '1d', 'week', '1w', 'mon', '1mon')
        if _is_daily_period and getattr(config, 'ENABLE_TUSHARE_DATA_SOURCE', False):
            ts_start_date = start_date
            ts_end_date = end_date
            # 网关模式 xtdata 失败 fallthrough 时，日期格式已经是 YYYYMMDD
            # 标准模式直接走此路径时，日期可能为 YYYY-MM-DD 或 YYYYMMDD
            ts_df = self._download_history_tushare(
                stock_code,
                start_date=ts_start_date,
                end_date=ts_end_date,
            )
            if ts_df is not None and not ts_df.empty:
                return ts_df
            logger.debug(f"Tushare 未返回 {stock_code} 有效历史数据，fallback 到 Mootdx")

        try:
            import Methods  # Import the Methods module

            # Determine frequency code for Mootdx
            if period == 'day':
                freq = 9  # 日线
            elif period == 'week':
                freq = 5  # 周线
            elif period == 'mon':
                freq = 6  # 月线
            elif period == '5m':
                freq = 0  # 5分钟
            elif period == '15m':
                freq = 1  # 15分钟
            elif period == '30m':
                freq = 2  # 30分钟
            elif period == '1h':
                freq = 3  # 小时线
            else:
                freq = 9  # Default to 日线

            # Adjust stock code if necessary
            if stock_code.endswith((".SH", ".SZ")):
                stock_code = stock_code[:-3]  # Remove suffix

            # ⭐ 超时优化：为Mootdx调用添加超时保护
            # 注意：不使用 with 语句，避免 __exit__ 调用 shutdown(wait=True) 阻塞等待超时线程
            import concurrent.futures

            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(
                Methods.getStockData,
                code=stock_code,
                offset=60,
                freq=freq,
                adjustflag='qfq'  # 前复权
            )
            executor.shutdown(wait=False)  # 不等待线程，让其在后台独立结束
            try:
                df = future.result(timeout=10.0)  # 10秒超时（历史数据可以稍长）
            except concurrent.futures.TimeoutError:
                logger.warning(f"Mootdx: 下载 {stock_code} 历史数据超时（10秒）")
                return None
            except RuntimeError as e:
                # 捕获"cannot schedule new futures after interpreter shutdown"错误
                if "interpreter shutdown" in str(e).lower() or "shutdown" in str(e).lower():
                    logger.debug(f"[DATA] 解释器正在关闭，跳过下载 {stock_code} 历史数据")
                    return None
                raise

            if df is None or df.empty:
                logger.warning(f"使用Mootdx获取 {stock_code} 的历史数据为空")
                return None

            # Rename columns to match expected format
            df = df.rename(columns={
                'datetime': 'date',
                'open': 'open',
                'high': 'high',
                'low': 'low',
                'close': 'close',
                'volume': 'volume',
                'amount': 'amount'
            })

            df = self._normalize_history_dates(df, stock_code, source='Mootdx')
            if df.empty:
                logger.warning(f"使用Mootdx获取 {stock_code} 的历史数据清洗后为空")
                return None

            df = self._filter_history_date_range(df, start_date=start_date, end_date=end_date)
            if df.empty:
                logger.debug(f"Mootdx获取 {stock_code} 的历史数据无新增记录(start={start_date}, end={end_date})")
                return None

            # Ensure 'close' column is numeric
            df['close'] = pd.to_numeric(df['close'], errors='coerce')

            # Add stock_code column
            df['stock_code'] = stock_code

            # logger.info(f"成功使用Mootdx获取 {stock_code} 的历史数据, 共 {len(df)} 条记录")
            return df

        except Exception as e:
            # 区分正常关闭错误和真正的错误
            error_str = str(e).lower()
            if "interpreter shutdown" in error_str or "cannot schedule" in error_str:
                logger.debug(f"[DATA] 系统正在关闭，跳过下载 {stock_code} 历史数据")
                return None

            # 检查是否是长度不匹配错误
            if "Length mismatch" in str(e):
                logger.warning(f"下载 {stock_code} 数据时发生长度不匹配错误，使用默认数据")
                
                # 创建包含默认值的DataFrame
                df = pd.DataFrame({
                    'date': [datetime.now().strftime('%Y-%m-%d')],
                    'open': [0.0],
                    'high': [0.0],
                    'low': [0.0],
                    'close': [0.0],
                    'volume': [0],
                    'amount': [0],
                    'stock_code': [stock_code]
                })
            else:
                # 其他错误，记录并返回None
                logger.error(f"下载 {stock_code} 的历史数据时出错: {str(e)}")
                return None


    def download_history_xtdata(self, stock_code, period=None, start_date=None, end_date=None):
        """
        下载股票历史数据

        参数:
        stock_code (str): 股票代码（支持裸代码如 "002771" 或带后缀如 "002771.SZ"）
        period (str): 周期，默认为日线 '1d'
        start_date (str): 开始日期，格式为'20220101'
        end_date (str): 结束日期，格式为'20220101'

        返回:
        pandas.DataFrame: 历史数据，若失败则返回None
        """
        if not period:
            period = '1d'  # 修复bug: xtquant API要求使用'1d'而非'day'

        if not start_date:
            start_date = '20200101'  # 默认从2020年开始

        if not end_date:
            # 默认到今天
            end_date = datetime.now().strftime('%Y%m%d')

        # 如果 start_date > end_date（数据已是最新），跳过无效下载
        if start_date and end_date and start_date > end_date:
            logger.debug(f"跳过下载 {stock_code}: 已有最新数据 (start={start_date} > end={end_date})")
            return None

        # xtdata API 要求股票代码带交易所后缀（如 002771.SZ）
        # stock_pool.json 存储的是裸代码（如 "002771"），需要转换
        xt_stock_code = self._adjust_stock(stock_code)

        logger.info(f"下载 {xt_stock_code} 的历史数据, 周期: {period}, 从 {start_date} 到 {end_date}")

        if not self.xt:
            logger.debug(f"xtdata未连接，跳过下载 {xt_stock_code} 历史数据")
            return None

        try:
            # ⭐ 超时优化：为xtquant API调用添加超时保护
            import concurrent.futures

            # 首先使用XtQuant API下载数据到本地
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(
                self.xt.download_history_data,
                xt_stock_code,
                period=period,
                start_time=start_date,
                end_time=end_date,
                incrementally=True  # 使用增量下载
            )
            executor.shutdown(wait=False)  # 不等待线程，让其在后台独立结束
            try:
                future.result(timeout=15.0)  # 15秒超时
            except concurrent.futures.TimeoutError:
                logger.warning(f"xtquant: 下载 {xt_stock_code} 历史数据超时（15秒）")
                return None
            except RuntimeError as e:
                if "interpreter shutdown" in str(e).lower() or "shutdown" in str(e).lower():
                    logger.debug(f"[DATA] 解释器正在关闭，跳过下载 {xt_stock_code} 历史数据")
                    return None
                raise

            # 等待数据下载完成
            time.sleep(0.5)

            # 使用get_market_data_ex从本地获取下载的数据
            # 注意第一个参数是字段列表，可以为空
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(
                self.xt.get_market_data_ex,
                [],  # 空字段列表表示获取所有可用字段
                [xt_stock_code],
                period=period,
                start_time=start_date,
                end_time=end_date
            )
            executor.shutdown(wait=False)  # 不等待线程，让其在后台独立结束
            try:
                result = future.result(timeout=10.0)  # 10秒超时
            except concurrent.futures.TimeoutError:
                logger.warning(f"xtquant: 获取 {xt_stock_code} 历史数据超时（10秒）")
                return None
            except RuntimeError as e:
                if "interpreter shutdown" in str(e).lower() or "shutdown" in str(e).lower():
                    logger.debug(f"[DATA] 解释器正在关闭，跳过获取 {xt_stock_code} 历史数据")
                    return None
                raise

            if not result:
                logger.warning(f"获取 {xt_stock_code} 的历史数据为空")
                return None

            if xt_stock_code in result:
                stock_data = result[xt_stock_code]
                df = pd.DataFrame(stock_data)
            else:
                logger.warning(f"获取的数据中没有 {xt_stock_code}, 可用的键: {list(result.keys())}")
                if result:
                    first_key = list(result.keys())[0]
                    stock_data = result[first_key]
                    df = pd.DataFrame(stock_data)
                else:
                    return None
            
            df = self._normalize_history_dates(df, xt_stock_code, source='xtdata')
            
            if not df.empty:
                logger.info(f"成功下载 {xt_stock_code} 的历史数据, 共 {len(df)} 条记录")
                return df
            else:
                logger.warning(f"下载的 {xt_stock_code} 数据为空")
                return None

        except Exception as e:
            # 区分正常关闭错误和真正的错误
            error_str = str(e).lower()
            if "interpreter shutdown" in error_str or "cannot schedule" in error_str:
                logger.debug(f"[DATA] 系统正在关闭，跳过下载 {xt_stock_code} 历史数据")
                return None
            logger.error(f"下载 {xt_stock_code} 的历史数据时出错: {str(e)}")
            return None

    def warm_stock_name_cache(self, stock_codes=None):
        """
        预热股票名称缓存，从本地数据源（QMT持仓、xtdata）批量填充，避免运行时触发 baostock。

        参数:
            stock_codes: 要预热的股票代码列表，None 时使用 config.STOCK_POOL
        """
        if stock_codes is None:
            stock_codes = list(config.STOCK_POOL)

        filled = 0

        # 来源1：从 QMT 持仓数据获取名称（一次调用覆盖所有持仓股）
        try:
            from position_manager import get_position_manager
            pm = get_position_manager()
            if hasattr(pm, 'qmt_trader') and pm.qmt_trader:
                positions_df = pm.qmt_trader.position()
                if not positions_df.empty and '证券代码' in positions_df.columns and '证券名称' in positions_df.columns:
                    for _, row in positions_df.iterrows():
                        code_simple = str(row['证券代码'])
                        name = str(row['证券名称'])
                        # 匹配带后缀（如 301399.SZ）和不带后缀（301399）两种格式
                        for sc in stock_codes:
                            if sc.split('.')[0] == code_simple and sc not in self.stock_names_cache:
                                if self._cache_stock_name(sc, name):
                                    filled += 1
        except Exception as e:
            logger.debug(f"warm_stock_name_cache: QMT持仓来源跳过: {e}")

        # 来源2：从 xtdata instrument_detail 获取剩余未命中的股票名称
        missing = [sc for sc in stock_codes if sc not in self.stock_names_cache]
        if missing and self.xt is not None:
            try:
                for sc in missing:
                    try:
                        detail = self.xt.get_instrument_detail(sc)
                        if detail and isinstance(detail, dict):
                            name = detail.get('InstrumentName') or detail.get('instrumentName') or detail.get('name')
                            if self._cache_stock_name(sc, name):
                                filled += 1
                    except Exception:
                        pass
            except Exception as e:
                logger.debug(f"warm_stock_name_cache: xtdata来源跳过: {e}")

        logger.info(f"股票名称缓存预热完成: 共 {len(stock_codes)} 只，成功填充 {filled} 只，缓存大小 {len(self.stock_names_cache)}")

    @staticmethod
    def _is_valid_stock_name(stock_code, stock_name):
        """判断股票名称是否是可持久化的真实名称。"""
        if stock_name is None:
            return False
        name = str(stock_name).strip()
        if not name or name in ('--', 'None', 'nan'):
            return False
        code = str(stock_code).strip()
        code_simple = code.split('.')[0] if '.' in code else code
        name_simple = name.split('.')[0] if '.' in name else name
        return name_simple != code_simple

    def _cache_stock_name(self, stock_code, stock_name):
        """仅缓存真实名称，避免把查询失败时的股票代码固化进缓存。"""
        if not self._is_valid_stock_name(stock_code, stock_name):
            return None
        name = str(stock_name).strip()
        self.stock_names_cache[stock_code] = name
        try:
            xt_code = self._adjust_stock(stock_code)
            self.stock_names_cache[xt_code] = name
            self.stock_names_cache[xt_code.split('.')[0]] = name
        except Exception:
            pass
        return name

    def _get_stock_name_from_xtdata(self, stock_code):
        """优先从 xtdata 合约基础信息获取股票名称。"""
        if self.xt is None or not hasattr(self.xt, 'get_instrument_detail'):
            return None
        try:
            codes = []
            xt_code = self._adjust_stock(stock_code)
            codes.append(xt_code)
            raw_code = str(stock_code).strip()
            if raw_code not in codes:
                codes.append(raw_code)

            for code in codes:
                detail = self.xt.get_instrument_detail(code)
                if detail and isinstance(detail, dict):
                    stock_name = (
                        detail.get('InstrumentName')
                        or detail.get('instrumentName')
                        or detail.get('name')
                    )
                    cached_name = self._cache_stock_name(stock_code, stock_name)
                    if cached_name:
                        return cached_name
        except Exception as e:
            logger.debug(f"通过xtdata获取股票 {stock_code} 名称失败: {e}")
        return None

    def _get_tushare_pro(self):
        """
        Tushare Pro 客户端惰性初始化。

        仅在第一次调用时 import tushare + set_token + pro_api，
        后续调用直接返回已初始化的实例。

        Returns:
            tushare.pro_api 实例，或 None（token 为空/未安装/初始化失败）
        """
        if self._tushare_token_attempted:
            return self._tushare_pro
        self._tushare_token_attempted = True

        token = getattr(config, 'TUSHARE_TOKEN', '') or ''
        if not token:
            logger.debug("TUSHARE_TOKEN 为空，跳过 Tushare 数据源")
            return None

        try:
            import tushare as ts
            ts.set_token(token)
            self._tushare_pro = ts.pro_api()
            # 快速连通性探针：获取任意一只股票确认 token 有效
            import concurrent.futures
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(
                self._tushare_pro.query, 'stock_basic',
                exchange='', list_status='L',
                fields='ts_code'
            )
            executor.shutdown(wait=False)
            try:
                result = future.result(timeout=getattr(config, 'TUSHARE_STOCK_NAME_TIMEOUT', 5))
                if result is not None and not result.empty:
                    logger.info("Tushare Pro 初始化成功，token 验证通过")
                    return self._tushare_pro
                else:
                    logger.warning("Tushare Pro token 验证失败（返回空数据），跳过 Tushare 数据源")
                    self._tushare_pro = None
                    return None
            except concurrent.futures.TimeoutError:
                logger.warning("Tushare Pro 初始化超时，跳过 Tushare 数据源")
                self._tushare_pro = None
                return None
        except ImportError:
            logger.debug("tushare 包未安装，跳过 Tushare 数据源")
            return None
        except Exception as e:
            logger.warning(f"Tushare Pro 初始化失败: {e}")
            self._tushare_pro = None
            return None

    @staticmethod
    def _to_tushare_code(stock_code):
        """
        将 miniQMT 内部代码统一为 Tushare ts_code 格式。

        miniQMT 使用 000001.SZ / 600036.SH 等格式，
        Tushare 使用完全相同的 ts_code 格式，直接透传即可。

        处理裸代码（如 '002771'）或带 sh./sz. 前缀的情况。
        """
        code = str(stock_code).strip().upper()
        # 已有 .SH/.SZ 后缀 → 直接返回（Tushare 标准格式）
        if code.endswith(('.SH', '.SZ')):
            return code
        # sh.600036 / sz.002771 格式 → 转为 600036.SH / 002771.SZ
        if code.startswith('SH.') or code.startswith('SZ.'):
            return code[3:] + '.' + code[:2]
        # 裸代码：根据前缀判断交易所
        if code.startswith(('6', '5', '9')):
            return code + '.SH'
        else:
            return code + '.SZ'

    def _download_history_tushare(self, stock_code, start_date=None, end_date=None):
        """
        通过 Tushare Pro 获取日线历史数据。

        Args:
            stock_code: 股票代码（支持 000001.SZ / 600036.SH / 裸代码）
            start_date: 开始日期 YYYYMMDD
            end_date: 结束日期 YYYYMMDD

        Returns:
            pandas.DataFrame（列 date/open/high/low/close/volume/amount）或 None
        """
        # 冷却期检查
        if time.time() < self._ts_cooldown_until:
            logger.debug(f"Tushare 历史数据处于冷却期，跳过 {stock_code}")
            return None

        pro = self._get_tushare_pro()
        if pro is None:
            return None

        ts_code = self._to_tushare_code(stock_code)

        # 日期格式转换
        ts_start = start_date if start_date else (datetime.now() - timedelta(days=365)).strftime('%Y%m%d')
        ts_end = end_date if end_date else datetime.now().strftime('%Y%m%d')

        import concurrent.futures
        start_ts = time.time()

        try:
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(
                pro.daily,
                ts_code=ts_code,
                start_date=ts_start,
                end_date=ts_end,
            )
            executor.shutdown(wait=False)
            df = future.result(timeout=getattr(config, 'TUSHARE_API_TIMEOUT', 10))

            latency_ms = int((time.time() - start_ts) * 1000)

            if df is None or df.empty:
                self._record_market_health("Tushare", "history", stock_code, False, latency_ms, reason="empty")
                self._ts_consecutive_failures += 1
                self._check_tushare_cooldown()
                return None

            # 列重命名：Tushare daily() 返回 trade_date, open, high, low, close, vol, amount
            df = df.rename(columns={
                'trade_date': 'date',
                'vol': 'volume',
            })

            # 日期标准化：Tushare 返回 YYYYMMDD 格式字符串
            df['date'] = df['date'].astype(str).str.strip()

            df = self._normalize_history_dates(df, stock_code, source='Tushare')
            if df.empty:
                self._record_market_health("Tushare", "history", stock_code, False, latency_ms, reason="invalid_dates")
                self._ts_consecutive_failures += 1
                self._check_tushare_cooldown()
                return None

            # 筛选需要的列
            keep_cols = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount']
            available_cols = [c for c in keep_cols if c in df.columns]
            df = df[available_cols]

            df = self._filter_history_date_range(df, start_date=start_date, end_date=end_date)
            if df.empty:
                logger.debug(f"Tushare: {stock_code} 历史数据无新增记录(start={ts_start}, end={ts_end})")
                self._record_market_health("Tushare", "history", stock_code, True, latency_ms, reason="no_new_data")
                return None

            # 确保数值列类型正确
            for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')

            df['stock_code'] = stock_code

            # 成功：重置失败计数
            self._ts_consecutive_failures = 0
            self._ts_cooldown_until = 0.0
            self._record_market_health("Tushare", "history", stock_code, True, latency_ms, reason="success")
            logger.debug(f"Tushare: {stock_code} 获取 {len(df)} 条日线数据 (start={ts_start}, end={ts_end})")
            return df

        except concurrent.futures.TimeoutError:
            latency_ms = int((time.time() - start_ts) * 1000)
            self._record_market_health("Tushare", "history", stock_code, False, latency_ms, reason="timeout")
            logger.warning(f"Tushare: 下载 {stock_code} 历史数据超时")
            self._ts_consecutive_failures += 1
            self._check_tushare_cooldown()
            return None
        except Exception as e:
            latency_ms = int((time.time() - start_ts) * 1000)
            self._record_market_health("Tushare", "history", stock_code, False, latency_ms, reason="exception")
            logger.warning(f"Tushare: 下载 {stock_code} 历史数据异常: {e}")
            self._ts_consecutive_failures += 1
            self._check_tushare_cooldown()
            return None

    def _get_stock_name_from_tushare(self, stock_code):
        """
        通过 Tushare Pro stock_basic 查询股票名称。

        Args:
            stock_code: 股票代码

        Returns:
            str 股票名称，失败或不可用返回 None
        """
        # 冷却期检查
        if time.time() < self._ts_cooldown_until:
            return None

        pro = self._get_tushare_pro()
        if pro is None:
            return None

        ts_code = self._to_tushare_code(stock_code)
        import concurrent.futures
        start_ts = time.time()

        try:
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(
                pro.stock_basic,
                ts_code=ts_code,
                fields='ts_code,name',
            )
            executor.shutdown(wait=False)
            df = future.result(timeout=getattr(config, 'TUSHARE_STOCK_NAME_TIMEOUT', 5))

            latency_ms = int((time.time() - start_ts) * 1000)

            if df is not None and not df.empty:
                name = str(df.iloc[0].get('name', '')).strip()
                if self._is_valid_stock_name(stock_code, name):
                    self._record_market_health("Tushare", "stock_name", stock_code, True, latency_ms, reason="success")
                    # 成功：重置失败计数
                    self._ts_consecutive_failures = 0
                    self._ts_cooldown_until = 0.0
                    return self._cache_stock_name(stock_code, name)

            self._record_market_health("Tushare", "stock_name", stock_code, False, latency_ms, reason="empty_or_invalid")
            self._ts_consecutive_failures += 1
            self._check_tushare_cooldown()
            return None

        except concurrent.futures.TimeoutError:
            latency_ms = int((time.time() - start_ts) * 1000)
            self._record_market_health("Tushare", "stock_name", stock_code, False, latency_ms, reason="timeout")
            logger.warning(f"Tushare: 查询 {stock_code} 名称超时")
            self._ts_consecutive_failures += 1
            self._check_tushare_cooldown()
            return None
        except Exception as e:
            latency_ms = int((time.time() - start_ts) * 1000)
            self._record_market_health("Tushare", "stock_name", stock_code, False, latency_ms, reason="exception")
            logger.debug(f"Tushare: 查询 {stock_code} 名称异常: {e}")
            self._ts_consecutive_failures += 1
            self._check_tushare_cooldown()
            return None

    def _check_tushare_cooldown(self):
        """检查 Tushare 连续失败次数，达到阈值时进入冷却期。"""
        max_fail = getattr(config, 'TUSHARE_MAX_CONSECUTIVE_FAILURES', 3)
        if self._ts_consecutive_failures >= max_fail:
            cooldown = getattr(config, 'TUSHARE_RETRY_COOLDOWN', 300)
            self._ts_cooldown_until = time.time() + cooldown
            logger.warning(
                f"Tushare 连续失败 {self._ts_consecutive_failures} 次，"
                f"进入冷却期 {cooldown} 秒"
            )

    def _baostock_login_with_timeout(self, timeout=None):
        """带超时的 baostock login，防止网络异常时无限阻塞。

        baostock.bs.login() 内部使用 socket 连接，无超时参数。
        当服务器不可达时会阻塞到 OS 级 TCP 超时（~21秒），
        本方法通过线程 + future.result(timeout) 强制截断。
        """
        if timeout is None:
            timeout = getattr(config, 'BAOSTOCK_LOGIN_TIMEOUT', 5)

        import concurrent.futures
        bs = None
        try:
            import baostock as _bs
            bs = _bs
        except ImportError:
            return None, "baostock not installed"

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            # 新版 baostock(0.9.x) 收紧访问：登录前应用 API Key（已配置且版本支持时）
            try:
                import baostock_helper
                baostock_helper.apply_api_key(bs)
            except Exception:
                pass
            future = executor.submit(bs.login)
            lg = future.result(timeout=timeout)
            return lg, None
        except concurrent.futures.TimeoutError:
            logger.warning(f"baostock login 超时({timeout}秒)，强制放弃")
            return None, "timeout"
        except Exception as e:
            return None, str(e)
        finally:
            executor.shutdown(wait=False)

    def _baostock_logout_safe(self):
        """安全执行 baostock logout，抑制所有输出和异常。"""
        try:
            import baostock as bs
            with suppress_stdout_stderr():
                bs.logout()
        except Exception:
            pass

    def get_stock_name(self, stock_code):
        """
        获取股票名称

        参数:
        stock_code (str): 股票代码

        返回:
        str: 股票名称，如果未找到则返回股票代码
        """
        try:
            # 尝试从缓存获取名称
            if stock_code in self.stock_names_cache:
                cached_name = self.stock_names_cache[stock_code]
                if self._is_valid_stock_name(stock_code, cached_name):
                    return cached_name
                self.stock_names_cache.pop(stock_code, None)

            # 从QMT交易接口获取（仅实盘模式）
            try:
                # 模拟模式下跳过实盘API调用
                if hasattr(config, 'ENABLE_SIMULATION_MODE') and config.ENABLE_SIMULATION_MODE:
                    raise Exception("模拟模式，跳过QMT API")

                from position_manager import get_position_manager
                position_manager = get_position_manager()

                if hasattr(position_manager, 'qmt_trader') and position_manager.qmt_trader:
                    positions_df = position_manager.qmt_trader.position()
                    if not positions_df.empty and '证券代码' in positions_df.columns and '证券名称' in positions_df.columns:
                        # 简化股票代码以匹配
                        stock_code_simple = stock_code.split('.')[0] if '.' in stock_code else stock_code
                        stock_info = positions_df[positions_df['证券代码'] == stock_code_simple]
                        if not stock_info.empty:
                            stock_name = stock_info.iloc[0]['证券名称']
                            cached_name = self._cache_stock_name(stock_code, stock_name)
                            if cached_name:
                                return cached_name
            except Exception as e:
                logger.debug(f"通过qmt_trader获取股票名称出错: {str(e)}")

            stock_name = self._get_stock_name_from_xtdata(stock_code)
            if stock_name:
                return stock_name

            # ── Tushare Pro 股票名称查询（xtdata 之后、baostock 之前）──
            stock_name = self._get_stock_name_from_tushare(stock_code)
            if stock_name:
                return stock_name

            if not getattr(config, 'ENABLE_BAOSTOCK_STOCK_NAME_LOOKUP', False):
                return stock_code

            # 尝试使用baostock查询（带超时保护和冷却机制）
            try:
                import baostock as bs  # noqa: F401 - 检查是否安装

                # 检查冷却期：连续失败后暂时跳过 baostock
                cooldown = getattr(config, 'BAOSTOCK_RETRY_COOLDOWN', 300)
                max_failures = getattr(config, 'BAOSTOCK_MAX_CONSECUTIVE_FAILURES', 3)

                if time.time() < self._bs_cooldown_until:
                    # 冷却期内只临时降级返回代码，不污染名称缓存。
                    return stock_code

                # 带超时的 login
                lg, err = self._baostock_login_with_timeout()
                if lg is None or lg.error_code != '0':
                    if lg is None:
                        err_msg = err
                    else:
                        import baostock_helper
                        err_msg = baostock_helper.describe_login_error(lg.error_code, lg.error_msg)
                    logger.warning(f"baostock登录失败: {err_msg}")
                    self._bs_consecutive_failures += 1

                    if self._bs_consecutive_failures >= max_failures:
                        self._bs_cooldown_until = time.time() + cooldown
                        logger.warning(
                            f"baostock 连续失败 {self._bs_consecutive_failures} 次，"
                            f"进入冷却期 {cooldown} 秒"
                        )

                    self._baostock_logout_safe()
                    return stock_code

                # login 成功，重置失败计数
                self._bs_consecutive_failures = 0
                self._bs_cooldown_until = 0.0

                # 调整股票代码格式
                if '.' in stock_code:
                    formatted_code = stock_code
                else:
                    if stock_code.startswith(('600', '601', '603', '688', '510')):
                        formatted_code = f"sh.{stock_code}"
                    else:
                        formatted_code = f"sz.{stock_code}"

                # 查询股票基本信息
                rs = bs.query_stock_basic(code=formatted_code)
                if rs.error_code != '0':
                    logger.warning(f"查询股票基本信息失败: {rs.error_msg}")
                    self._baostock_logout_safe()
                    return stock_code

                # 获取结果
                data_list = []
                while (rs.error_code == '0') & rs.next():
                    data_list.append(rs.get_row_data())
                self._baostock_logout_safe()

                if data_list:
                    stock_name = data_list[0][1] if len(data_list[0]) > 1 else stock_code
                    cached_name = self._cache_stock_name(stock_code, stock_name)
                    if cached_name:
                        return cached_name

                return stock_code

            except ImportError:
                logger.warning("未安装baostock，无法获取股票名称")
                return stock_code
            except Exception as e:
                logger.error(f"获取股票 {stock_code} 名称时出错: {str(e)}")
                return stock_code

        except Exception as e:
            logger.error(f"获取股票 {stock_code} 名称时出错: {str(e)}")
            return stock_code
    
    def save_history_data(self, stock_code, data_df):
        """
        保存历史数据到数据库
        
        参数:
        stock_code (str): 股票代码
        data_df (pandas.DataFrame): 历史数据
        """
        if data_df is None or data_df.empty:
            logger.warning(f"没有 {stock_code} 的数据可保存")
            return
        
        try:
            # 立即创建工作副本
            work_df = data_df.copy()
            
            # 数据验证和清理
            required_columns = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount']
            missing_cols = [col for col in required_columns if col not in work_df.columns]
            if missing_cols:
                logger.error(f"{stock_code} 缺少必要列: {missing_cols}")
                return
            
            # 清理空数据
            initial_count = len(work_df)
            work_df = work_df.dropna(subset=['date'])
            final_count = len(work_df)
            
            if initial_count != final_count:
                logger.warning(f"{stock_code} 过滤了 {initial_count - final_count} 行空date数据")
            
            if work_df.empty:
                logger.warning(f"{stock_code} 无有效数据可保存")
                return
            
            # 数据处理
            work_df['stock_code'] = stock_code
            for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
                work_df[col] = pd.to_numeric(work_df[col], errors='coerce')

            # 方案A优化：使用逐行REPLACE避免主键冲突
            # 相比DELETE+INSERT，REPLACE在并发场景下更安全

            # 准备数据
            data_to_insert = list(zip(
                work_df['stock_code'].tolist(),
                work_df['date'].tolist(),
                work_df['open'].tolist(),
                work_df['high'].tolist(),
                work_df['low'].tolist(),
                work_df['close'].tolist(),
                work_df['volume'].tolist(),
                work_df['amount'].tolist()
            ))

            # 使用REPLACE INTO语句（SQLite特性，自动处理主键冲突）
            # _db_lock 保证多线程不并发访问 self.conn，避免隐式事务冲突
            with self._db_lock:
                with self.conn:
                    self.conn.executemany('''
                        REPLACE INTO stock_daily_data
                        (stock_code, date, open, high, low, close, volume, amount)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', data_to_insert)

            logger.debug(f"已保存 {stock_code} 的历史数据到数据库, 共 {len(data_to_insert)} 条记录（使用REPLACE模式避免主键冲突）")

        except Exception as e:
            logger.error(f"保存 {stock_code} 的历史数据时出错: {str(e)}")


    def get_latest_data(self, stock_code):
        """
        获取最新行情数据

        盘中优先走 xtdata tick 推送缓存（_on_xtdata_tick 写入，零延迟零线程池开销）；
        缓存未命中时回退到 xtdata get_full_tick 轮询 → Mootdx 兜底。
        非交易时段走 xtdata 快照 → Mootdx 兜底。

        参数:
        stock_code (str): 股票代码

        返回:
        dict: 最新行情数据
        """
        try:
            adjusted_code = self._adjust_stock(stock_code)

            # 在交易时间内，优先使用实时数据管理器
            if config.is_trade_time():
                # ── 路径 1（新）：xtdata tick 推送缓存（订阅 callback 写入）──
                tick = self._get_tick_from_cache(adjusted_code)
                if tick:
                    logger.debug(f"tick缓存: {stock_code} lastPrice={tick.get('lastPrice')}")
                    return tick

                # ── 路径 2（原有）：xtdata get_full_tick 轮询 ──
                try:
                    realtime_data = self.get_latest_xtdata(stock_code)
                    if realtime_data and realtime_data.get('lastPrice', 0) > 0:
                        logger.debug(f"XT获取 {stock_code} 实时数据 {realtime_data.get('lastPrice')}")
                        return realtime_data

                    # XtQuantManager 模式：lastPrice=0 时用 lastClose 替代，跳过 Mootdx
                    # 非交易时段 xtdata 不返回实时价，但 lastClose（昨收价）可靠，足以支撑持仓估值
                    if getattr(config, 'ENABLE_XTQUANT_MANAGER', False):
                        last_close = realtime_data.get('lastClose', 0) if realtime_data else 0
                        if last_close > 0:
                            realtime_data['lastPrice'] = last_close
                            logger.debug(
                                f"XtQuantManager: {stock_code} lastPrice=0，"
                                f"使用 lastClose={last_close} 作为参考价（非交易时段）"
                            )
                            return realtime_data
                        logger.debug(
                            f"XtQuantManager: {stock_code} 无有效价格（lastPrice=0, lastClose=0），跳过Mootdx"
                        )
                        return None

                    if realtime_data:
                        # 有数据但 lastPrice=0：判断是否已订阅
                        code = self._adjust_stock(stock_code)
                        if code in self.subscribed_stocks:
                            # 已订阅但价格为0，属于异常（推送延迟或连接不稳定），降级并警告
                            logger.warning(f"xtdata: {stock_code} 已订阅但 lastPrice=0，降级到Mootdx")
                        else:
                            # 未订阅（盘中新增持仓的典型情况），立即触发订阅，本次降级
                            self.ensure_subscribed(stock_code)
                            logger.info(f"xtdata: {stock_code} 未订阅(lastPrice=0)，已触发订阅，本次降级到Mootdx")
                    else:
                        # 空 dict：xt 连接超时或股票代码不存在，静默降级
                        logger.debug(f"xtdata: {stock_code} 无数据，降级到Mootdx")
                except Exception as e:
                    if getattr(config, 'ENABLE_XTQUANT_MANAGER', False):
                        logger.debug(f"XtQuantManager: 获取 {stock_code} 实时数据失败，返回None: {str(e)}")
                        return None
                    logger.debug(f"实时数据管理器获取{stock_code}失败，降级到Mootdx: {str(e)}")
                    
            # 非交易时段：优先尝试 xtdata get_full_tick（盘后返回收盘快照，lastClose 可靠）
            # 这样可以正确计算涨跌幅，不依赖 Mootdx 的 offset=2 返回行数是否足够
            if not config.is_trade_time() and self.xt:
                try:
                    xt_data = self.get_latest_xtdata(stock_code)
                    if xt_data and xt_data.get('lastPrice', 0) > 0 and xt_data.get('lastClose', 0) > 0:
                        logger.debug(f"非交易时段 xtdata 快照: {stock_code} lastPrice={xt_data.get('lastPrice')} lastClose={xt_data.get('lastClose')}")
                        return xt_data
                except Exception as e:
                    logger.debug(f"非交易时段 xtdata 获取失败，降级到Mootdx: {e}")

            # 继续尝试从Mootdx获取数据
            # Adjust stock code if necessary
            if stock_code.endswith((".SH", ".SZ")):
                stock_code = stock_code[:-3]  # Remove suffix

            # ⭐ 超时优化：为Mootdx降级路径添加超时保护
            # 注意：不使用 with 语句，避免 __exit__ 调用 shutdown(wait=True) 阻塞等待超时线程
            import concurrent.futures

            start_time = time.time()
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(
                Methods.getStockData,
                code=stock_code,
                offset=2,  # Get only the latest data
                freq=9,  # 日线
                adjustflag='qfq'
            )
            executor.shutdown(wait=False)  # 不等待线程，让其在后台独立结束
            try:
                df = future.result(timeout=5.0)  # 5秒超时
            except concurrent.futures.TimeoutError:
                latency_ms = int((time.time() - start_time) * 1000)
                self._record_market_health(
                    "Mootdx", "realtime", stock_code, False, latency_ms, reason="timeout"
                )
                logger.warning(f"Mootdx: 获取 {stock_code} 行情超时（5秒）")
                return None
            except RuntimeError as e:
                # 捕获"cannot schedule new futures after interpreter shutdown"错误
                if "interpreter shutdown" in str(e).lower() or "shutdown" in str(e).lower():
                    logger.debug(f"[DATA] 解释器正在关闭，跳过Mootdx获取 {stock_code} 行情")
                    return None
                raise

            if df is None or df.empty:
                latency_ms = int((time.time() - start_time) * 1000)
                self._record_market_health(
                    "Mootdx", "realtime", stock_code, False, latency_ms, reason="empty"
                )
                logger.warning(f"使用Mootdx获取 {stock_code} 的最新行情为空")
                return None

            if len(df) < 2:
                latency_ms = int((time.time() - start_time) * 1000)
                self._record_market_health(
                    "Mootdx", "realtime", stock_code, False, latency_ms, reason="insufficient_rows"
                )
                logger.warning(f"Mootdx: {stock_code} 数据行数不足({len(df)}行)，无法计算lastClose")
                return None

            # Extract the latest data
            latest_data = df.iloc[-1].to_dict()
            lastday_data = df.iloc[-2].to_dict()

            # Rename columns to match expected format
            latest_data = {
                'lastPrice': float(latest_data.get('close', 0)),
                'lastClose': float(lastday_data.get('close', 0)),
                'volume': float(latest_data.get('volume', 0)),
                'amount': float(latest_data.get('amount', 0)),
                'date': latest_data.get('datetime', None)
            }

            latency_ms = int((time.time() - start_time) * 1000)
            data_quality_ok = latest_data['lastPrice'] > 0 and latest_data['lastClose'] > 0
            self._record_market_health(
                "Mootdx",
                "realtime",
                stock_code,
                data_quality_ok,
                latency_ms,
                reason="success" if data_quality_ok else "invalid_price",
                data_quality_ok=data_quality_ok,
            )
            latest_data = self._decorate_quote(
                latest_data, "Mootdx", "realtime", stock_code, latency_ms
            )
            logger.debug(f"Mootdx:{stock_code} 最新行情: {latest_data}")
            return latest_data

        except Exception as e:
            # 区分正常关闭错误和真正的错误
            error_str = str(e).lower()
            if "interpreter shutdown" in error_str or "cannot schedule" in error_str:
                logger.debug(f"[DATA] 系统正在关闭，跳过获取 {stock_code} 行情")
                return None
            self._record_market_health(
                "Mootdx", "realtime", stock_code, False, 0, reason="exception"
            )
            logger.error(f"获取 {stock_code} 的latest_data出错: {str(e)}")
            return None


    def get_latest_xtdata(self, stock_code):
        """获取最新行情数据"""
        stock_code = self._adjust_stock(stock_code)

        if not self.xt:
            return {}

        try:
            # ⚠️ 检测Python解释器是否正在关闭
            # sys.is_finalizing 是函数对象（truthy），必须调用才能获取布尔值
            import sys
            if hasattr(sys, 'is_finalizing') and sys.is_finalizing():
                logger.debug(f"[DATA] 检测到解释器正在关闭，跳过获取 {stock_code} 行情")
                return {}

            # ⭐ 超时优化：添加超时保护
            # 注意：不使用 with 语句，避免 __exit__ 调用 shutdown(wait=True) 阻塞等待超时线程
            import concurrent.futures

            start_time = time.time()
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(self.xt.get_full_tick, [stock_code])
            executor.shutdown(wait=False)  # 不等待线程，让其在后台独立结束
            try:
                latest_quote = future.result(timeout=3.0)  # 3秒超时
            except concurrent.futures.TimeoutError:
                latency_ms = int((time.time() - start_time) * 1000)
                self._record_market_health(
                    "xtdata", "realtime", stock_code, False, latency_ms, reason="timeout"
                )
                logger.warning(f"xtdata: 获取 {stock_code} 行情超时（3秒）")
                self._record_xtdata_failure("timeout")
                return {}  # 返回空字典，与原逻辑一致
            except RuntimeError as e:
                # 捕获"cannot schedule new futures after interpreter shutdown"错误
                if "interpreter shutdown" in str(e).lower() or "shutdown" in str(e).lower():
                    logger.debug(f"[DATA] 解释器正在关闭，跳过获取 {stock_code} 行情")
                    return {}
                raise

            if not latest_quote or stock_code not in latest_quote:
                # 交易时段返回空是异常（WARNING）；非交易时段是正常行为（DEBUG），避免盘前/盘后刷屏
                if config.is_trade_time():
                    logger.warning(f"xtdata:未获取到 {stock_code} 的tick行情，返回值: {latest_quote}")
                else:
                    logger.debug(f"xtdata:未获取到 {stock_code} 的tick行情（非交易时段）")
                latency_ms = int((time.time() - start_time) * 1000)
                self._record_market_health(
                    "xtdata", "realtime", stock_code, False, latency_ms, reason="empty_quote"
                )
                self._record_xtdata_failure("empty_quote")
                return {}  # 返回空字典而不是None

            quote_data = latest_quote[stock_code]
            latency_ms = int((time.time() - start_time) * 1000)
            last_price = float(quote_data.get("lastPrice", 0) or 0)
            data_quality_ok = last_price > 0
            self._record_market_health(
                "xtdata",
                "realtime",
                stock_code,
                data_quality_ok,
                latency_ms,
                reason="success" if data_quality_ok else "invalid_price",
                data_quality_ok=data_quality_ok,
            )
            logger.debug(f"xtdata: {stock_code} 最新行情: {quote_data}")
            if data_quality_ok:
                self._reset_xtdata_failure()
            return self._decorate_quote(quote_data, "xtdata", "realtime", stock_code, latency_ms)

        except Exception as e:
            # 区分正常关闭错误和真正的错误
            error_str = str(e).lower()
            if "interpreter shutdown" in error_str or "cannot schedule" in error_str:
                logger.debug(f"[DATA] 系统正在关闭，跳过获取 {stock_code} 行情")
                return {}
            self._record_market_health(
                "xtdata", "realtime", stock_code, False, 0, reason="exception"
            )
            logger.error(f"xtdata: 获取 {stock_code} 的最新行情时出错: {str(e)}", exc_info=True)
            self._record_xtdata_failure("exception")
            return {}  # 返回空字典而不是None

    def get_history_data_from_db(self, stock_code, start_date=None, end_date=None):
        """
        从数据库获取历史数据

        参数:
        stock_code (str): 股票代码
        start_date (str): 开始日期，如 '2021-01-01'
        end_date (str): 结束日期，如 '2021-03-31'

        返回:
        pandas.DataFrame: 历史数据
        """
        query = "SELECT * FROM stock_daily_data WHERE stock_code=?"
        params = [stock_code]

        if start_date:
            query += " AND date>=?"
            params.append(start_date)

        if end_date:
            query += " AND date<=?"
            params.append(end_date)

        query += " ORDER BY date"

        try:
            with self._db_lock:
                df = pd.read_sql_query(query, self.conn, params=params)
            logger.debug(f"从数据库获取 {stock_code} 的历史数据, 共 {len(df)} 条记录")
            return df
        except Exception as e:
            logger.error(f"从数据库获取 {stock_code} 的历史数据时出错: {str(e)}")
            return pd.DataFrame()

    
    def update_all_stock_data(self):
        """更新所有股票的历史数据"""
        for stock_code in config.STOCK_POOL:
            self.update_stock_data(stock_code)
            # 避免请求过于频繁
            time.sleep(1)
    
    def update_stock_data(self, stock_code):
        """
        更新单只股票的数据
        
        参数:
        stock_code (str): 股票代码
        """
        # 从数据库获取最新的数据日期
        latest_date_query = "SELECT MAX(date) FROM stock_daily_data WHERE stock_code=?"
        with self._db_lock:
            cursor = self.conn.cursor()
            cursor.execute(latest_date_query, (stock_code,))
            result = cursor.fetchone()
        
        if result and result[0]:
            latest_date = self._normalize_date_arg(result[0]) or result[0]
            today = datetime.now().strftime('%Y-%m-%d')
            if latest_date >= today:
                logger.debug(f"{stock_code} 历史数据已最新(latest={latest_date})，跳过下载")
                return
            # 从最新日期的下一天开始获取
            start_date = (datetime.strptime(latest_date, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y%m%d')
            # logger.info(f"更新 {stock_code} 的数据，从 {start_date} 开始")
        else:
            # 如果没有历史数据，获取完整的历史数据
            start_date = None
            logger.info(f"获取 {stock_code} 的完整历史数据")

        if self._should_throttle_history_update(stock_code, start_date):
            return
        
        # 下载并保存数据
        data_df = self.download_history_data(stock_code, start_date=start_date)
        if data_df is not None and not data_df.empty:
            self.save_history_data(stock_code, data_df)
    
    def start_data_update_thread(self):
        """启动数据更新线程"""
        if not config.ENABLE_DATA_SYNC:
            logger.info("数据同步功能已关闭，不启动更新线程")
            return
            
        if self.update_thread and self.update_thread.is_alive():
            logger.warning("数据更新线程已在运行")
            return
            
        self.stop_flag = False
        self.update_thread = threading.Thread(target=self._data_update_loop)
        self.update_thread.daemon = True
        self.update_thread.start()
        logger.info("数据更新线程已启动")
    
    def stop_data_update_thread(self):
        """停止数据更新线程"""
        if self.update_thread and self.update_thread.is_alive():
            self.stop_flag = True
            self.update_thread.join(timeout=5)
            logger.info("数据更新线程已停止")
    
    def _data_update_loop(self):
        """数据更新循环"""
        while not self.stop_flag:
            try:
                # 判断是否在交易时间
                if config.is_trade_time():
                    if config.VERBOSE_LOOP_LOGGING or config.DEBUG:
                        logger.debug("开始更新所有股票数据")
                    self.update_all_stock_data()
                    if config.VERBOSE_LOOP_LOGGING or config.DEBUG:
                        logger.debug("股票数据更新完成")

                # 等待下一次更新
                for _ in range(config.UPDATE_INTERVAL):
                    if self.stop_flag:
                        break
                    time.sleep(1)
                    
            except Exception as e:
                logger.error(f"数据更新循环出错: {str(e)}")
                time.sleep(60)  # 出错后等待一分钟再继续
    
    def close(self):
        """关闭数据管理器"""
        self.stop_data_update_thread()
        
        if self.conn:
            self.conn.close()
            logger.info("数据库连接已关闭")

        # xtquant的xtdata模块不需要显式断开连接
        # 连接会在进程退出时自动释放
        logger.info("数据管理器已关闭")


# 单例模式
_instance = None

def get_data_manager():
    """获取DataManager单例"""
    global _instance
    if _instance is None:
        _instance = DataManager()
    return _instance
