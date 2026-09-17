"""
XtQuantAccount — 单账号封装

将一个 miniQMT 账号实例的完整生命周期封装在此类中：
- 连接管理（connect / disconnect / reconnect）
- 所有 xttrader + xtdata 操作（统一超时保护 + 指标记录）
- 健康探测（is_healthy / ping）
- 指数退避重连
"""
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .exceptions import (
    AccountNotFoundError,
    XtQuantCallError,
    XtQuantConnectionError,
    XtQuantTimeoutError,
)
from .metrics import MetricsCollector
from .timeout import call_with_timeout

try:
    from logger import get_logger
    logger = get_logger("xqm_acct")
except Exception:
    import logging
    logger = logging.getLogger("xtquant_manager.account")

try:
    from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
    from xtquant.xttype import StockAccount
    _XTQUANT_AVAILABLE = True
except ImportError:
    XtQuantTrader = None
    XtQuantTraderCallback = None
    StockAccount = None
    _XTQUANT_AVAILABLE = False


@dataclass
class AccountConfig:
    """账号配置"""
    account_id: str
    qmt_path: str
    account_type: str = "STOCK"
    session_id: Optional[int] = None          # None = 随机生成
    call_timeout: float = 3.0                  # 普通调用超时
    download_timeout: float = 30.0             # 历史数据下载超时
    connect_timeout: float = 30.0              # xttrader.connect() 超时保护（秒）
    reconnect_base_wait: float = 60.0          # 重连基础等待（秒）
    max_reconnect_attempts: int = 5             # 最大重连次数（超过后仍重试但不计次）
    ping_stock: str = "000001.SZ"              # 心跳探测使用的股票代码
    ping_staleness_threshold: float = 300.0    # 超过此秒数未成功 ping，触发例行真实探测


class XtQuantAccount:
    """
    单账号封装，线程安全。

    - 所有对外 API 调用都通过 _call() 包装，保证超时保护 + 指标记录
    - connect() 失败返回 False，不抛异常
    - reconnect() 使用指数退避，最大等待 1 小时
    """

    # position() 返回的 DataFrame 列名（与 easy_qmt_trader 保持一致）
    POSITION_COLUMNS = [
        "账号类型", "资金账号", "证券代码", "股票余额", "可用余额",
        "成本价", "参考成本价", "市值", "选择", "持股天数", "交易状态", "明细",
        "证券名称", "冻结数量", "市价", "盈亏", "盈亏比(%)",
        "当日买入", "当日卖出",
    ]

    def __init__(self, config: AccountConfig):
        self.config = config
        self.metrics = MetricsCollector()

        # QMT 对象（连接成功后设置）
        self._xt_trader = None
        self._acc = None
        self._xtdata = None  # xtquant.xtdata 模块引用

        # 连接状态
        self._connected = False
        self._conn_lock = threading.RLock()
        self._reconnecting = False
        self._reconnect_attempts = 0
        self._last_ping_ok_time: Optional[float] = None
        self._connected_at: Optional[float] = None

        # 外部成交回调列表
        self._trade_callbacks: List[Any] = []

        # 外部断连回调列表（on_disconnected 触发时调用）
        self._disconnect_callbacks: List[Any] = []

        # 重连中断事件：disconnect() 调用时 set()，允许 reconnect() 提前退出等待
        self._reconnect_abort = threading.Event()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """
        连接 xttrader + xtdata。
        失败返回 False，不抛异常。
        """
        with self._conn_lock:
            if self._connected:
                return True
            return self._do_connect()

    def _do_connect(self) -> bool:
        """内部：实际执行连接逻辑（调用方持有锁）"""
        try:
            # 1. 连接 xtdata 行情
            xtdata_ok = self._connect_xtdata()
            if not xtdata_ok:
                logger.warning(f"[{self._id()}] xtdata 连接失败，继续尝试 xttrader")

            # 2. 连接 xttrader 交易
            xttrader_ok = self._connect_xttrader()
            if not xttrader_ok:
                logger.error(f"[{self._id()}] xttrader 连接失败")
                return False

            self._connected = True
            self._connected_at = time.time()
            self._last_ping_ok_time = time.time()
            logger.info(f"[{self._id()}] 连接成功")
            return True

        except Exception as e:
            logger.error(f"[{self._id()}] 连接异常: {e}")
            return False

    def _connect_xtdata(self) -> bool:
        """连接 xtdata 行情接口"""
        try:
            import xtquant.xtdata as xt
            result = xt.connect()
            if result:
                self._xtdata = xt
                logger.debug(f"[{self._id()}] xtdata 连接成功")
                return True
            else:
                logger.warning(f"[{self._id()}] xtdata connect() 返回 False")
                return False
        except ImportError:
            logger.warning(f"[{self._id()}] xtquant 未安装，xtdata 不可用")
            return False
        except Exception as e:
            logger.warning(f"[{self._id()}] xtdata 连接异常: {e}")
            return False

    def _connect_xttrader(self) -> bool:
        """连接 xttrader 交易接口（含超时保护，参照 easy_qmt_trader.py:314-340）"""
        if not _XTQUANT_AVAILABLE:
            logger.warning(f"[{self._id()}] xtquant 未安装，xttrader 不可用")
            return False
        xt_trader = None
        try:
            session_id = self.config.session_id
            if session_id is None:
                session_id = random.randint(100000, 999999)

            xt_trader = XtQuantTrader(self.config.qmt_path, session_id)
            acc = StockAccount(
                account_id=self.config.account_id,
                account_type=self.config.account_type,
            )

            # 注册回调（如果有外部回调）
            self._setup_callbacks(xt_trader)

            xt_trader.start()

            # ── 超时保护：在独立线程中调用 connect()，防止 QMT 未启动时永久阻塞 ──
            connect_result_holder = [None]
            connect_error_holder = [None]

            def _do_connect():
                try:
                    connect_result_holder[0] = xt_trader.connect()
                except Exception as e:
                    connect_error_holder[0] = e

            connect_thread = threading.Thread(target=_do_connect, daemon=True)
            connect_thread.start()
            connect_thread.join(self.config.connect_timeout)

            if connect_thread.is_alive():
                logger.error(
                    f"[{self._id()}] xttrader.connect() 超时 "
                    f"({self.config.connect_timeout}s)，中止连接"
                )
                try:
                    xt_trader.stop()
                except Exception:
                    pass
                return False

            if connect_error_holder[0] is not None:
                logger.error(
                    f"[{self._id()}] xttrader.connect() 异常: "
                    f"{connect_error_holder[0]}"
                )
                try:
                    xt_trader.stop()
                except Exception:
                    pass
                return False
            # ── 超时保护结束 ──

            connect_result = connect_result_holder[0]
            if connect_result == 0:
                subscribe_result = xt_trader.subscribe(acc)
                logger.info(
                    f"[{self._id()}] xttrader 连接成功, "
                    f"订阅结果={subscribe_result}"
                )
                self._xt_trader = xt_trader
                self._acc = acc
                return True
            else:
                logger.error(
                    f"[{self._id()}] xttrader 连接失败, 结果={connect_result}"
                )
                try:
                    xt_trader.stop()
                except Exception:
                    pass
                return False

        except Exception as e:
            logger.error(f"[{self._id()}] xttrader 连接异常: {e}")
            if xt_trader is not None:
                try:
                    xt_trader.stop()
                except Exception:
                    pass
            return False

    def _setup_callbacks(self, xt_trader) -> None:
        """注册交易回调和断连回调"""
        try:
            if XtQuantTraderCallback is None:
                return

            class _Callback(XtQuantTraderCallback):
                def __init__(self_cb, account_obj):
                    super().__init__()
                    self_cb._account = account_obj

                def on_disconnected(self_cb):
                    """
                    QMT 进程崩溃或网络中断时由 xtquant 主动回调。
                    立即更新连接状态，无需等待 HealthMonitor 轮询（快则 <1s）。
                    参照 easy_qmt_trader.py:31-42 的已验证实现。
                    """
                    acct = self_cb._account
                    logger.error(
                        f"[{acct._id()}] QMT 连接断开（on_disconnected），"
                        f"立即重置连接状态"
                    )
                    acct._on_disconnect_event()

                def on_stock_trade(self_cb, trade):
                    for cb in self_cb._account._trade_callbacks:
                        try:
                            cb(trade)
                        except Exception as ex:
                            logger.warning(f"成交回调异常: {ex}")

            callback = _Callback(self)
            xt_trader.register_callback(callback)
        except Exception as e:
            logger.warning(f"[{self._id()}] 注册回调异常: {e}")

    def disconnect(self) -> None:
        """断开连接，释放资源。若有重连在等待中，立即中断。"""
        self._reconnect_abort.set()  # 中断任何正在等待的 reconnect()
        with self._conn_lock:
            self._connected = False
            if self._xt_trader is not None:
                try:
                    self._xt_trader.stop()
                except Exception:
                    pass
                self._xt_trader = None
            self._acc = None
            self._xtdata = None
            logger.info(f"[{self._id()}] 已断开连接")

    def reconnect(self) -> bool:
        """
        指数退避重连。
        wait = min(base * 2^attempt, 3600)
        返回 True = 重连成功。
        """
        with self._conn_lock:
            if self._reconnecting:
                logger.debug(f"[{self._id()}] 正在重连中，跳过")
                return False
            self._reconnecting = True

        try:
            wait = min(
                self.config.reconnect_base_wait * (2 ** self._reconnect_attempts),
                3600,
            )
            logger.warning(
                f"[{self._id()}] 等待 {wait:.0f}s 后重连 "
                f"(第 {self._reconnect_attempts + 1} 次)"
            )
            # 清除旧的 abort 信号，再等待（防止被上次 disconnect() 的遗留信号立即跳过）
            self._reconnect_abort.clear()
            self._reconnect_abort.wait(wait)  # 可被 disconnect() 立即中断

            # 先断开旧连接
            with self._conn_lock:
                self._connected = False
                if self._xt_trader is not None:
                    try:
                        self._xt_trader.stop()
                    except Exception:
                        pass
                    self._xt_trader = None
                    self._acc = None

            # 重新连接
            with self._conn_lock:
                success = self._do_connect()

            if success:
                self._reconnect_attempts = 0
                logger.info(f"[{self._id()}] 重连成功")
            else:
                self._reconnect_attempts += 1
                logger.error(
                    f"[{self._id()}] 重连失败 (第 {self._reconnect_attempts} 次失败)"
                )
            return success

        finally:
            self._reconnecting = False

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------

    def is_healthy(self) -> bool:
        """
        快速内存检查（无 I/O）。

        这里只判断连接对象和连接标志是否处于可用状态，不把 ping 过期当作
        “不健康”。ping 过期只表示需要做一次例行真实探测，由 needs_ping()
        单独表达，避免健康账号每 5 分钟产生一次误导性的 Level 0 失败日志。
        """
        if not self._connected or self._xt_trader is None:
            return False
        if self._last_ping_ok_time is None:
            return False
        return True

    def needs_ping(self) -> bool:
        """
        是否需要执行例行真实探测。

        该方法只用于 HealthMonitor 的正常巡检路径：连接状态仍健康，但距离
        上次 ping 成功已经超过阈值，需要主动探测 xtdata + xttrader，及时发现
        QMT 进程崩溃、xttrader 隐性断连等问题。
        """
        if not self.is_healthy():
            return False
        elapsed = time.time() - self._last_ping_ok_time
        return elapsed >= self.config.ping_staleness_threshold

    def ping(self) -> bool:
        """
        真实探测：同时验证 xtdata 和 xttrader 连接。

        xtdata 探测：调用 get_full_tick，验证行情接口是否存活。
        xttrader 探测：调用 query_stock_asset，验证交易接口是否存活。

        QMT 进程重启后 xttrader 会断开，但 xtdata 可能因缓存仍能返回数据。
        若仅探测 xtdata 会漏判 xttrader 断连，因此必须同时探测两者。

        xttrader 探测失败时主动重置 _connected=False，确保 is_healthy()
        下次返回 False，从而触发 HealthMonitor 的 Level 2 重连。
        """
        # 1. 探测 xtdata（行情接口）
        xtdata_ok = False
        try:
            result = self.get_full_tick([self.config.ping_stock])
            xtdata_ok = bool(result)
        except Exception:
            pass

        # 2. 探测 xttrader（交易接口）—— QMT 重启后此处会失败
        xttrader_ok = False
        if self._xt_trader is not None and self._connected:
            asset = self._call(
                self._xt_trader.query_stock_asset,
                self._acc,
                op="ping_trader",
                default=None,
            )
            xttrader_ok = asset is not None

        ok = xtdata_ok and xttrader_ok
        if ok:
            self._last_ping_ok_time = time.time()
        elif not xttrader_ok and self._connected:
            # xttrader 断连但 _connected 仍为 True（QMT 崩溃场景）
            # 主动重置，使 is_healthy() 下次返回 False，确保进入 Level 2 重连
            self._connected = False
            logger.warning(f"[{self._id()}] ping 检测到 xttrader 断连，重置连接状态")
        return ok

    @property
    def last_ping_ok_time(self) -> Optional[float]:
        return self._last_ping_ok_time

    # ------------------------------------------------------------------
    # 行情操作
    # ------------------------------------------------------------------

    def get_full_tick(self, stock_codes: List[str]) -> dict:
        """获取全推行情，失败返回空 dict"""
        if self._xtdata is None:
            return {}
        return self._call(
            self._xtdata.get_full_tick,
            stock_codes,
            op="get_full_tick",
            default={},
        )

    def get_instrument_detail(self, stock_code: str) -> dict:
        """获取证券信息（含名称），失败返回空 dict。
        xtdata 本地缓存调用，无网络开销。"""
        if self._xtdata is None:
            return {}
        return self._call(
            self._xtdata.get_instrument_detail,
            stock_code,
            op="get_instrument_detail",
            default={},
        )

    def get_market_data_ex(
        self,
        fields: list,
        stock_list: List[str],
        period: str = "1d",
        start_time: str = "20200101",
        end_time: str = "",
    ) -> dict:
        """获取历史行情数据，失败返回空 dict"""
        if self._xtdata is None:
            return {}
        if not end_time:
            from datetime import datetime
            end_time = datetime.now().strftime("%Y%m%d")
        result = self._call(
            self._xtdata.get_market_data_ex,
            fields, stock_list,
            period=period, start_time=start_time, end_time=end_time,
            op="get_market_data_ex",
            timeout=self.config.download_timeout,
            default={},
        )
        if not result:
            return {}
        # xtquant 返回 dict[stock_code, DataFrame]，需转为 JSON 可序列化的 dict-of-dicts
        serializable = {}
        for code, data in result.items():
            if hasattr(data, "to_dict"):
                serializable[code] = data.to_dict()
            else:
                serializable[code] = data
        return serializable

    def download_history_data(
        self,
        stock_code: str,
        period: str = "1d",
        start_time: str = "20200101",
        end_time: str = "",
    ) -> bool:
        """下载历史数据到本地，成功返回 True"""
        if self._xtdata is None:
            return False
        if not end_time:
            from datetime import datetime
            end_time = datetime.now().strftime("%Y%m%d")
        try:
            self._call(
                self._xtdata.download_history_data,
                stock_code,
                period=period, start_time=start_time, end_time=end_time,
                incrementally=True,
                op="download_history_data",
                timeout=self.config.download_timeout,
                default=None,
            )
            return True
        except (XtQuantTimeoutError, XtQuantCallError):
            return False

    # ------------------------------------------------------------------
    # 交易操作
    # ------------------------------------------------------------------

    def order_stock(
        self,
        stock_code: str,
        order_type: int,
        order_volume: int,
        price_type: int,
        price: float,
        strategy_name: str = "",
        order_remark: str = "",
    ) -> int:
        """下单（同步），成功返回 order_id (>0)，失败返回 -1"""
        if not self._check_trader():
            return -1
        result = self._call(
            self._xt_trader.order_stock,
            self._acc, stock_code, order_type, order_volume, price_type, price,
            strategy_name, order_remark,
            op="order_stock",
            default=-1,
        )
        return result if result is not None else -1

    def cancel_order(self, order_id: int) -> int:
        """撤单，返回 0=成功，非 0=失败"""
        if not self._check_trader():
            return -1
        result = self._call(
            self._xt_trader.cancel_order_stock,
            self._acc, order_id,
            op="cancel_order",
            default=-1,
        )
        return result if result is not None else -1

    def query_positions(self) -> List[dict]:
        """
        查询持仓，返回 list[dict]（与 easy_qmt_trader.position() 的行数据兼容）。
        失败返回空列表。
        """
        if not self._check_trader():
            return []
        positions = self._call(
            self._xt_trader.query_stock_positions,
            self._acc,
            op="query_positions",
            default=[],
        )
        if not positions:
            return []
        result = []
        for pos in positions:
            stock_name = (
                getattr(pos, "m_strInstrumentName", None)
                or getattr(pos, "stock_name", None)
                or getattr(pos, "instrument_name", None)
            )
            result.append({
                "账号类型": pos.account_type,
                "资金账号": pos.account_id,
                "证券代码": str(pos.stock_code)[:6],
                "股票余额": pos.volume,
                "可用余额": pos.can_use_volume,
                "成本价": pos.open_price,
                "参考成本价": pos.open_price,
                "市值": pos.market_value,
                "选择": None,
                "持股天数": None,
                "交易状态": None,
                "明细": None,
                "证券名称": stock_name,
                "冻结数量": pos.volume - pos.can_use_volume,
                "市价": None,
                "盈亏": None,
                "盈亏比(%)": None,
                "当日买入": None,
                "当日卖出": None,
            })
        return result

    def query_asset(self) -> dict:
        """查询账户资产，失败返回空 dict"""
        if not self._check_trader():
            return {}
        asset = self._call(
            self._xt_trader.query_stock_asset,
            self._acc,
            op="query_asset",
            default=None,
        )
        if asset is None:
            return {}
        return {
            "账号类型": asset.account_type,
            "资金账户": asset.account_id,
            "可用金额": asset.cash,
            "冻结金额": asset.frozen_cash,
            "持仓市值": asset.market_value,
            "总资产": asset.total_asset,
        }

    def query_orders(self) -> List[dict]:
        """查询当日委托，失败返回空列表"""
        if not self._check_trader():
            return []
        orders = self._call(
            self._xt_trader.query_stock_orders,
            self._acc,
            op="query_orders",
            default=[],
        )
        if not orders:
            return []
        result = []
        for o in orders:
            result.append({
                "账号类型": o.account_type,
                "资金账号": o.account_id,
                "证券代码": str(o.stock_code)[:6],
                "订单编号": o.order_id,
                "委托类型": o.order_type,
                "委托数量": o.order_volume,
                "委托价格": o.price,
                "委托状态": o.order_status,
                # 监控端需要这三项才能展示在途进度与挂单时间
                "成交数量": getattr(o, "traded_volume", 0) or 0,
                "报单时间": getattr(o, "order_time", None),
                "状态描述": getattr(o, "status_msg", "") or "",
            })
        return result

    def query_trades(self) -> List[dict]:
        """查询当日成交，失败返回空列表"""
        if not self._check_trader():
            return []
        trades = self._call(
            self._xt_trader.query_stock_trades,
            self._acc,
            op="query_trades",
            default=[],
        )
        if not trades:
            return []
        result = []
        for t in trades:
            result.append({
                "账号类型": t.account_type,
                "资金账号": t.account_id,
                "证券代码": str(t.stock_code)[:6],
                "委托类型": t.order_type,
                "成交编号": t.traded_id,
                "成交数量": t.traded_volume,
                "成交价格": t.traded_price,
                "成交金额": t.traded_amount,
            })
        return result

    def register_trade_callback(self, cb) -> None:
        """注册成交回调，连接后触发"""
        self._trade_callbacks.append(cb)

    def register_disconnect_callback(self, cb) -> None:
        """
        注册断连事件回调。
        QMT 连接断开时（on_disconnected 触发）立即调用 cb()。
        主要供 HealthMonitor 注册以即时重置冷却计时器。
        """
        self._disconnect_callbacks.append(cb)

    def _on_disconnect_event(self) -> None:
        """内部断连事件处理，由 on_disconnected 回调和 _simulate_disconnect 共同调用"""
        with self._conn_lock:
            self._connected = False
            self._last_ping_ok_time = None
        for cb in self._disconnect_callbacks:
            try:
                cb()
            except Exception as ex:
                logger.error(f"[{self._id()}] 断连回调异常: {ex}")

    def _simulate_disconnect(self) -> None:
        """
        仅供测试使用：模拟 on_disconnected 回调触发。
        生产代码通过 _Callback.on_disconnected() 自动触发。
        """
        self._on_disconnect_event()

    # ------------------------------------------------------------------
    # 状态快照（供 /health 和 /metrics 使用）
    # ------------------------------------------------------------------

    def get_state(self) -> dict:
        """返回账号状态快照"""
        return {
            "account_id": self.config.account_id,
            "connected": self._connected,
            "reconnecting": self._reconnecting,
            "reconnect_attempts": self._reconnect_attempts,
            "last_ping_ok_time": self._last_ping_ok_time,
            "connected_at": self._connected_at,
            "xtdata_available": self._xtdata is not None,
            "xttrader_available": self._xt_trader is not None,
        }

    def get_metrics(self) -> dict:
        """返回指标快照"""
        return self.metrics.snapshot()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _id(self) -> str:
        """日志用账号 ID（部分脱敏）"""
        aid = self.config.account_id
        if len(aid) > 4:
            return aid[:4] + "***" + aid[-1]
        return aid

    @property
    def is_reconnecting(self) -> bool:
        """是否正在执行重连流程（含指数退避等待阶段）"""
        return self._reconnecting

    def _check_trader(self) -> bool:
        """检查 xt_trader 是否可用"""
        if self._xt_trader is None or not self._connected:
            logger.warning(f"[{self._id()}] xt_trader 未连接，跳过操作")
            return False
        return True

    def _call(self, func, *args, op: str = "unknown", timeout: float = None,
              default=None, **kwargs):
        """
        统一调用入口：超时保护 + 指标记录。

        Args:
            func: 要调用的函数
            *args: 位置参数
            op: 操作名称（用于指标分组）
            timeout: 超时秒数（默认使用 config.call_timeout）
            default: 异常时的返回值
            **kwargs: 关键字参数

        Returns:
            func 的返回值，或 default（发生异常时）
        """
        if timeout is None:
            timeout = self.config.call_timeout

        t0 = time.monotonic()
        try:
            result = call_with_timeout(func, *args, timeout=timeout, **kwargs)
            latency_ms = (time.monotonic() - t0) * 1000
            self.metrics.record_call(op, success=True, latency_ms=latency_ms)
            return result

        except XtQuantTimeoutError as e:
            latency_ms = (time.monotonic() - t0) * 1000
            self.metrics.record_call(
                op, success=False, latency_ms=latency_ms,
                is_timeout=True, error_msg=str(e),
            )
            logger.warning(f"[{self._id()}] {op} 超时: {e}")
            return default

        except XtQuantCallError as e:
            latency_ms = (time.monotonic() - t0) * 1000
            self.metrics.record_call(
                op, success=False, latency_ms=latency_ms, error_msg=str(e),
            )
            logger.error(f"[{self._id()}] {op} 失败: {e}")
            return default

        except Exception as e:
            latency_ms = (time.monotonic() - t0) * 1000
            self.metrics.record_call(
                op, success=False, latency_ms=latency_ms, error_msg=str(e),
            )
            logger.error(f"[{self._id()}] {op} 意外异常: {e}")
            return default
