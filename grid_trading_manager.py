"""
网格交易管理器模块

提供网格交易的核心功能:
- GridSession: 网格会话数据模型
- PriceTracker: 价格追踪状态机
- GridTradingManager: 网格交易管理器
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Dict, List
import threading
import time
import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError

import config
from logger import get_logger

logger = get_logger(__name__)


@dataclass
class GridSession:
    """网格交易会话"""
    id: Optional[int] = None
    stock_code: str = ""
    status: str = "active"

    # 价格配置
    center_price: float = 0.0
    current_center_price: float = 0.0
    price_interval: float = 0.05

    # 交易配置
    position_ratio: float = 0.25
    callback_ratio: float = 0.005

    # 资金配置
    max_investment: float = 0.0
    current_investment: float = 0.0

    # 退出配置
    max_deviation: float = 0.15
    target_profit: float = 0.10
    stop_loss: float = -0.10
    end_time: Optional[datetime] = None

    # 统计数据
    trade_count: int = 0
    buy_count: int = 0
    sell_count: int = 0
    total_buy_amount: float = 0.0
    total_sell_amount: float = 0.0
    total_buy_volume: int = 0
    total_sell_volume: int = 0

    # 时间戳
    start_time: Optional[datetime] = None
    stop_time: Optional[datetime] = None
    stop_reason: Optional[str] = None

    def get_profit_ratio(self) -> float:
        """
        计算网格盈亏率（基于max_investment）

        公式: (total_sell_amount - total_buy_amount) / max_investment

        设计原则:
        1. 只计算网格交易本身的现金流差额，完全隔离市场波动
        2. 分母使用max_investment，避免除零错误，且含义清晰（投入回报率）
        3. 无交易时返回0.0，避免误触发止盈止损
        """
        # max_investment为0说明配置异常，返回0.0
        if self.max_investment <= 0:
            logger.debug(f"[GRID] get_profit_ratio: stock_code={self.stock_code}, "
                        f"session_id={self.id}, max_investment={self.max_investment}, 返回0.0")
            return 0.0

        # 无任何交易时返回0.0（中性状态）
        if self.total_buy_amount == 0 and self.total_sell_amount == 0:
            logger.debug(f"[GRID] get_profit_ratio: stock_code={self.stock_code}, "
                        f"session_id={self.id}, 无交易记录, 返回0.0")
            return 0.0

        # 网格累计利润 = 卖出总额 - 买入总额
        grid_profit = self.total_sell_amount - self.total_buy_amount

        # 盈亏率 = 网格累计利润 / 最大投入额度
        ratio = grid_profit / self.max_investment

        logger.debug(f"[GRID] get_profit_ratio: stock_code={self.stock_code}, "
                    f"session_id={self.id}, sell={self.total_sell_amount:.2f}, "
                    f"buy={self.total_buy_amount:.2f}, grid_profit={grid_profit:.2f}, "
                    f"max_investment={self.max_investment:.2f}, ratio={ratio*100:.2f}%")
        return ratio

    def get_profit_ratio_by_market_value(self, position_volume: float, current_price: float) -> float:
        """
        以持仓市值为分母计算网格盈亏率（供 _check_exit_conditions 使用）

        公式: (total_sell_amount - total_buy_amount) / (position_volume * current_price)

        与 get_profit_ratio() 的区别:
          get_profit_ratio()      分母 = max_investment（固定）
          本方法                  分母 = 持仓市值（动态）

        动机: max_investment 通常远小于持仓市值，导致单次买入的净现金流出占比就
              超过 stop_loss 阈值（生产 Bug 2026-04-01：-19.04% ≤ -10% 即触发）。
              改用持仓市值后，止损衡量的是"网格净流出相对于整体持仓的风险占比"，
              单次买入不再误触发，多次买入叠加价格下跌时仍会正确触发（DESIGN-4保留）。

        降级条件: position_volume ≤ 0 或 current_price ≤ 0 → 降级为 get_profit_ratio()
        测试参考: test/test_grid_profit_ratio_fix.py
        """
        if position_volume > 0 and current_price > 0:
            position_market_value = position_volume * current_price
            grid_profit = self.total_sell_amount - self.total_buy_amount
            ratio = grid_profit / position_market_value
            logger.debug(
                f"[GRID] get_profit_ratio_by_market_value: stock_code={self.stock_code}, "
                f"grid_profit={grid_profit:.2f}, volume={position_volume:.0f}, "
                f"price={current_price:.2f}, market_value={position_market_value:.2f}, "
                f"ratio={ratio*100:.2f}%"
            )
            return ratio
        # 降级：无有效持仓数据，回退到 max_investment 分母
        logger.debug(
            f"[GRID] get_profit_ratio_by_market_value: 无有效持仓(volume={position_volume}, "
            f"price={current_price:.2f}), 降级为get_profit_ratio()"
        )
        return self.get_profit_ratio()

    def get_true_pnl_ratio(self, current_price: float, position_volume: float = 0) -> float:
        """
        True P&L ratio -- industry best practice (realized + unrealized)

        Formula:
          open_grid_volume = total_buy_volume - total_sell_volume
          true_pnl = (total_sell - total_buy) + open_grid_volume * current_price
          ratio = true_pnl / max_investment

        At purchase moment: true_pnl = 0 (cash out = position value)
        After price drop:   true_pnl < 0 (reflects actual loss)
        After round-trip:   true_pnl = realized profit (open_vol = 0)

        Fallback:
          If no volume tracking data (old sessions), uses
          get_profit_ratio_by_market_value() or get_profit_ratio()
        """
        if self.total_buy_volume > 0 or self.total_sell_volume > 0:
            open_volume = self.total_buy_volume - self.total_sell_volume
            realized = self.total_sell_amount - self.total_buy_amount
            unrealized = open_volume * current_price
            true_pnl = realized + unrealized
            if self.max_investment <= 0:
                logger.debug(
                    f"[GRID] get_true_pnl_ratio: max_investment=0, return 0.0"
                )
                return 0.0
            ratio = true_pnl / self.max_investment
            logger.debug(
                f"[GRID] get_true_pnl_ratio: stock_code={self.stock_code}, "
                f"realized={realized:.2f}, unrealized={unrealized:.2f}, "
                f"true_pnl={true_pnl:.2f}, open_vol={open_volume}, "
                f"price={current_price:.2f}, ratio={ratio*100:.2f}%"
            )
            return ratio
        # Fallback: old session without volume tracking
        logger.debug(
            f"[GRID] get_true_pnl_ratio: no volume data, "
            f"fallback to get_profit_ratio_by_market_value"
        )
        return self.get_profit_ratio_by_market_value(position_volume, current_price)

    def get_grid_profit(self) -> float:
        """
        获取网格累计利润（绝对金额）

        Returns:
            网格累计利润 = total_sell_amount - total_buy_amount
        """
        return self.total_sell_amount - self.total_buy_amount

    def get_deviation_ratio(self) -> float:
        """计算当前偏离度"""
        if self.center_price == 0 or self.current_center_price == 0:
            logger.debug(f"[GRID] get_deviation_ratio: stock_code={self.stock_code}, session_id={self.id}, "
                        f"center_price={self.center_price}, current_center={self.current_center_price}, 返回0.0")
            return 0.0
        deviation = abs(self.current_center_price - self.center_price) / self.center_price
        logger.debug(f"[GRID] get_deviation_ratio: stock_code={self.stock_code}, session_id={self.id}, "
                    f"center={self.center_price:.2f}, current={self.current_center_price:.2f}, deviation={deviation*100:.2f}%")
        return deviation

    def get_grid_levels(self) -> dict:
        """生成当前网格档位"""
        center = self.current_center_price or self.center_price
        levels = {
            'lower': center * (1 - self.price_interval),
            'center': center,
            'upper': center * (1 + self.price_interval)
        }
        logger.debug(f"[GRID] get_grid_levels: stock_code={self.stock_code}, session_id={self.id}, "
                    f"center={center:.2f}, interval={self.price_interval*100:.1f}%, "
                    f"lower={levels['lower']:.2f}, upper={levels['upper']:.2f}")
        return levels


@dataclass
class PriceTracker:
    """价格追踪器,用于检测回调"""
    session_id: int
    last_price: float = 0.0
    peak_price: float = 0.0
    valley_price: float = 0.0
    direction: Optional[str] = None
    crossed_level: Optional[float] = None
    waiting_callback: bool = False

    def update_price(self, new_price: float):
        """更新价格并追踪峰谷值"""
        self.last_price = new_price
        logger.debug(f"[GRID] PriceTracker.update_price: session_id={self.session_id}, new_price={new_price:.2f}, "
                    f"waiting_callback={self.waiting_callback}, direction={self.direction}")

        if self.waiting_callback:
            old_peak = self.peak_price
            old_valley = self.valley_price
            if self.direction == 'rising' and new_price > self.peak_price:
                self.peak_price = new_price
                logger.debug(f"[GRID] PriceTracker: 更新峰值 {old_peak:.2f} -> {new_price:.2f}")
            elif self.direction == 'falling' and new_price < self.valley_price:
                self.valley_price = new_price
                logger.debug(f"[GRID] PriceTracker: 更新谷值 {old_valley:.2f} -> {new_price:.2f}")

    def check_callback(self, callback_ratio: float) -> Optional[str]:
        """检查是否触发回调,返回信号类型"""
        if not self.waiting_callback:
            logger.debug(f"[GRID] PriceTracker.check_callback: session_id={self.session_id}, 未等待回调, 返回None")
            return None

        # 浮点数容差:仅用于补偿浮点计算误差
        # 设计理由:
        # 1. 解决浮点精度问题: 0.002999999999999936 vs 0.003
        # 2. 0.01%容差足以处理浮点运算误差,避免过度宽松
        FLOAT_TOLERANCE = 0.0001

        if self.direction == 'rising':
            if self.peak_price == 0:
                logger.warning(f"[GRID] PriceTracker.check_callback: session_id={self.session_id}, peak_price=0, 返回None")
                return None
            ratio = (self.peak_price - self.last_price) / self.peak_price
            logger.debug(f"[GRID] PriceTracker.check_callback: session_id={self.session_id}, direction=rising, "
                        f"peak={self.peak_price:.2f}, last={self.last_price:.2f}, ratio={ratio*100:.4f}%, "
                        f"threshold={callback_ratio*100:.2f}%")
            # 使用容差比较：ratio >= callback_ratio - FLOAT_TOLERANCE
            if ratio >= (callback_ratio - FLOAT_TOLERANCE):
                logger.debug(f"[GRID] PriceTracker.check_callback: 触发SELL信号 (ratio={ratio:.6f}, threshold-tolerance={callback_ratio - FLOAT_TOLERANCE:.6f})")
                return 'SELL'

        elif self.direction == 'falling':
            if self.valley_price == 0:
                logger.warning(f"[GRID] PriceTracker.check_callback: session_id={self.session_id}, valley_price=0, 返回None")
                return None
            ratio = (self.last_price - self.valley_price) / self.valley_price
            logger.debug(f"[GRID] PriceTracker.check_callback: session_id={self.session_id}, direction=falling, "
                        f"valley={self.valley_price:.2f}, last={self.last_price:.2f}, ratio={ratio*100:.4f}%, "
                        f"threshold={callback_ratio*100:.2f}%")
            # 使用容差比较：ratio >= callback_ratio - FLOAT_TOLERANCE
            if ratio >= (callback_ratio - FLOAT_TOLERANCE):
                logger.debug(f"[GRID] PriceTracker.check_callback: 触发BUY信号 (ratio={ratio:.6f}, threshold-tolerance={callback_ratio - FLOAT_TOLERANCE:.6f})")
                return 'BUY'

        logger.debug(f"[GRID] PriceTracker.check_callback: session_id={self.session_id}, 未触发信号")
        return None

    def reset(self, price: float):
        """重置追踪器"""
        logger.debug(f"[GRID] PriceTracker.reset: session_id={self.session_id}, price={price:.2f}, "
                    f"重置前: direction={self.direction}, crossed_level={self.crossed_level}, waiting_callback={self.waiting_callback}")
        self.last_price = price
        self.peak_price = price
        self.valley_price = price
        self.direction = None
        self.crossed_level = None
        self.waiting_callback = False


class GridTradingManager:
    """网格交易管理器"""

    @staticmethod
    def _extract_order_id(result) -> str:
        """兼容 executor 返回 dict/str 的订单号"""
        if isinstance(result, dict):
            order_id = result.get('order_id', '')
            return str(order_id) if order_id is not None else ''
        if result is not None and not isinstance(result, bool):
            return str(result)
        return ''

    def __init__(self, db_manager, position_manager, trading_executor):
        self.db = db_manager
        self.position_manager = position_manager
        self.executor = trading_executor

        # 内存缓存
        self.sessions: Dict[str, GridSession] = {}
        self.trackers: Dict[int, PriceTracker] = {}
        self.level_cooldowns: Dict[tuple, float] = {}
        self.last_buy_times: Dict[int, float] = {}  # {session_id: timestamp} 每次成功买入后记录时间，支持 GRID_BUY_COOLDOWN
        self.last_sell_times: Dict[int, float] = {}  # {session_id: timestamp} 每次成功卖出后记录时间，支持 GRID_SELL_COOLDOWN（A-4修复）
        self.last_sell_prices: Dict[int, float] = {}  # {session_id: trigger_price} 每次成功卖出时的触发价，支持自适应冷却缩短
        self.pending_grid_orders: Dict[str, dict] = {}  # 实盘委托待成交确认: {order_id: pending_info}
        self.submitting_grid_orders: Dict[str, dict] = {}  # 锁外下单保护: {submit_id: order_plan}
        self.lock = threading.RLock()  # 使用可重入锁,支持嵌套调用

        # 初始化:从数据库加载活跃会话
        logger.info(f"[GRID] GridTradingManager.__init__: 初始化网格交易管理器")
        loaded_count = self._load_active_sessions()
        pending_count = self._load_open_grid_orders()
        logger.info(f"[GRID] GridTradingManager.__init__: 初始化完成, 已加载 {loaded_count} 个活跃会话")
        if pending_count:
            logger.warning(f"[GRID] GridTradingManager.__init__: 恢复 {pending_count} 个未完成网格委托，等待成交/撤废单回报")

    @staticmethod
    def _normalize_code(stock_code: str) -> str:
        """统一股票代码格式：去除交易所后缀，用作 sessions 字典的 key。
        例: '600509.SH' -> '600509', '000001.SZ' -> '000001'
        positions 表存储无后缀代码，grid_trading_sessions 存储带后缀代码，
        此方法确保两者作为字典 key 时一致。
        """
        if stock_code and '.' in stock_code:
            return stock_code.split('.')[0]
        return stock_code

    def _load_active_sessions(self):
        """系统启动时从数据库加载活跃会话(保守恢复策略)"""
        logger.info("[GRID] 系统重启 - 开始恢复网格交易会话")

        try:
            active_sessions = self.db.get_active_grid_sessions()
            logger.info(f"[GRID] 从数据库查询到 {len(active_sessions)} 个活跃会话")

            # 详细日志：打印所有查询到的会话
            for idx, s in enumerate(active_sessions):
                s_dict = dict(s)
                end_time_str = s_dict.get('end_time', '')
                # 格式化时间：只显示到秒
                if end_time_str:
                    try:
                        end_time_dt = datetime.fromisoformat(end_time_str)
                        end_time_display = end_time_dt.strftime('%Y-%m-%d %H:%M:%S')
                    except (ValueError, TypeError) as fmt_err:
                        logger.debug(f"[GRID] 时间格式化失败: {fmt_err}")
                        end_time_display = end_time_str
                else:
                    end_time_display = 'N/A'

                logger.info(f"[GRID] 会话#{idx+1}: id={s_dict.get('id')}, "
                           f"stock={s_dict.get('stock_code')}, "
                           f"end_time={end_time_display}")

            recovered_count = 0
            stopped_count = 0

            for session_data in active_sessions:
                # CRITICAL FIX: 将sqlite3.Row转换为字典,避免"'sqlite3.Row' object has no attribute 'get'"错误
                session_dict = dict(session_data)
                stock_code = session_dict['stock_code']
                stock_code_key = self._normalize_code(stock_code)  # 用于 sessions 字典的统一 key
                session_id = session_dict['id']
                logger.info(f"[GRID] >>> 开始处理会话 session_id={session_id}, stock_code={stock_code}, key={stock_code_key}")

                try:
                    # 1. 检查会话是否已过期
                    # BUG FIX: 使用session_dict而不是session_data
                    end_time = datetime.fromisoformat(session_dict['end_time'])
                    if datetime.now() > end_time:
                        # 先更新数据库状态
                        self.db.stop_grid_session(session_id, 'expired')

                        # 如果内存里已有该会话，做最小清理避免Web仍显示active
                        existing = self.sessions.get(stock_code_key)
                        if existing and existing.status == 'active':
                            logger.info(f"[GRID] 会话{session_id}({stock_code})已过期，清理内存会话")
                            # 仅做最小清理：从内存移除并触发版本更新
                            try:
                                del self.sessions[stock_code_key]
                            except Exception:
                                pass
                            if session_id in self.trackers:
                                try:
                                    del self.trackers[session_id]
                                except Exception:
                                    pass
                            # 触发数据版本更新，确保前端刷新
                            try:
                                self.position_manager._increment_data_version()
                            except Exception:
                                pass

                        logger.info(f"[GRID] 会话{session_id}({stock_code})已过期,自动停止")
                        stopped_count += 1
                        continue

                    # 2. 检查持仓是否还存在（跳过以避免启动时阻塞）
                    # 修复: 启动时调用get_position可能导致阻塞30秒以上
                    # 策略: 先恢复会话,如果持仓已被清空,用户可以手动停止
                    position = None
                    # BUG FIX: 使用session_dict.get()而不是session_data.get()
                    current_price = session_dict.get('current_center_price', session_dict['center_price'])
                    logger.debug(f"[GRID] 跳过持仓检查以避免阻塞, 使用数据库价格: {current_price:.2f}")

                    # 3. 恢复GridSession对象
                    logger.debug(f"[GRID] 恢复会话对象 session_id={session_id}")
                    session = GridSession(
                        id=session_dict['id'],
                        stock_code=session_dict['stock_code'],
                        status=session_dict['status'],
                        center_price=session_dict['center_price'],
                        current_center_price=session_dict['current_center_price'],
                        price_interval=session_dict['price_interval'],
                        position_ratio=session_dict['position_ratio'],
                        callback_ratio=session_dict['callback_ratio'],
                        max_investment=session_dict['max_investment'],
                        current_investment=session_dict['current_investment'],
                        max_deviation=session_dict['max_deviation'],
                        target_profit=session_dict['target_profit'],
                        stop_loss=session_dict['stop_loss'],
                        trade_count=session_dict['trade_count'],
                        buy_count=session_dict['buy_count'],
                        sell_count=session_dict['sell_count'],
                        total_buy_amount=session_dict['total_buy_amount'],
                        total_sell_amount=session_dict['total_sell_amount'],
                        total_buy_volume=session_dict.get('total_buy_volume', 0),
                        total_sell_volume=session_dict.get('total_sell_volume', 0),
                        start_time=datetime.fromisoformat(session_dict['start_time']),
                        end_time=end_time
                    )

                    # ── V2 修复：DB 加载时校验 current_investment ───────────────────────────
                    # 场景：上次运行中买入成功但 DB 写入 current_investment 失败（磁盘/网络异常），
                    # 重启后 current_investment 偏低，如不校正将允许超出 max_investment 的额外买入。
                    # 保守策略：current_investment > max_investment 时，强制修正并写回 DB。
                    if session.max_investment > 0 and session.current_investment > session.max_investment:
                        logger.warning(
                            f"[GRID] DB 一致性修正 session_id={session_id} "
                            f"({session_dict['stock_code']}): "
                            f"current_investment({session.current_investment:.2f}) > "
                            f"max_investment({session.max_investment:.2f}), 修正为 max_investment"
                        )
                        session.current_investment = session.max_investment
                        try:
                            self.db.update_grid_session(session_id, {
                                'current_investment': session.max_investment
                            })
                        except Exception as db_err:
                            logger.warning(f"[GRID] DB 修正写回失败(可忽略，下次重启再修正): {db_err}")
                    self.sessions[stock_code_key] = session
                    # 使用数据库中保存的价格,避免在启动时调用position_manager
                    if position and isinstance(position, dict) and position.get('current_price'):
                        current_price = position.get('current_price')
                    else:
                        current_price = session.current_center_price
                    logger.debug(f"[GRID] 创建PriceTracker session_id={session_id}, current_price={current_price:.2f}")
                    self.trackers[session_id] = PriceTracker(
                        session_id=session_id,
                        last_price=current_price,
                        peak_price=current_price,
                        valley_price=current_price,
                        direction=None,
                        crossed_level=None,
                        waiting_callback=False
                    )

                    # 5. 清除档位冷却
                    cooldown_keys = [k for k in self.level_cooldowns.keys() if k[0] == session_id]
                    if cooldown_keys:
                        logger.debug(f"[GRID] 清除 {len(cooldown_keys)} 个档位冷却记录")
                    for key in cooldown_keys:
                        del self.level_cooldowns[key]

                    # 6. 记录恢复信息（简化版，避免调用get_profit_ratio导致阻塞）
                    logger.info(f"[GRID] 恢复会话: {stock_code}")
                    logger.info(f"[GRID]   - 会话ID: {session_id}")
                    logger.info(f"[GRID]   - 原始中心价: {session.center_price:.2f}元(锁定)")
                    logger.info(f"[GRID]   - 当前中心价: {session.current_center_price:.2f}元")
                    logger.info(f"[GRID]   - 当前市价: {current_price:.2f}元")
                    logger.info(f"[GRID]   - 累计交易: {session.trade_count}次(买{session.buy_count}/卖{session.sell_count})")
                    # 简化：不调用get_profit_ratio()避免递归日志调用
                    logger.info(f"[GRID]   - 网格盈亏: 计算中...")
                    logger.info(f"[GRID]   - 追踪器状态: 已重置(安全模式)")

                    levels = session.get_grid_levels()
                    logger.info(f"[GRID]   - 网格档位: {levels['lower']:.2f} / {levels['center']:.2f} / {levels['upper']:.2f}")

                    remaining_days = (end_time - datetime.now()).days
                    logger.info(f"[GRID]   - 剩余时长: {remaining_days}天")

                    recovered_count += 1

                except Exception as e:
                    logger.error(f"[GRID] 恢复会话{session_id}失败: {str(e)}, 自动停止会话")
                    try:
                        self.db.stop_grid_session(session_id, 'init_error')
                        stopped_count += 1
                    except:
                        pass

            logger.info(f"[GRID] 网格会话恢复完成: 恢复{recovered_count}个, 自动停止{stopped_count}个")

            return recovered_count

        except Exception as e:
            logger.error(f"[GRID] 加载活跃会话失败: {str(e)}")
            return 0

    def _load_open_grid_orders(self) -> int:
        """系统启动时恢复尚未终结的网格委托"""
        if not hasattr(self.db, 'get_open_grid_orders'):
            return 0

        try:
            open_orders = self.db.get_open_grid_orders()
        except Exception as e:
            logger.warning(f"[GRID] 恢复未完成网格委托失败: {e}")
            return 0

        recovered = 0
        for order in open_orders:
            stock_code = order.get('stock_code')
            session_id = order.get('session_id')
            session = (
                self.sessions.get(self._normalize_code(stock_code))
                or self.sessions.get(stock_code)
            )
            if not session or session.id != session_id or session.status != 'active':
                try:
                    self.db.update_grid_order(order['order_id'], {
                        'status': 'orphaned',
                        'last_error': 'active session not found during recovery'
                    })
                except Exception:
                    pass
                logger.warning(
                    f"[GRID] 未完成委托无法恢复，已标记orphaned order_id={order.get('order_id')}, "
                    f"session_id={session_id}, stock_code={stock_code}"
                )
                continue

            try:
                signal = json.loads(order.get('raw_signal') or '{}')
            except Exception:
                signal = {}
            if not signal:
                signal = {
                    'stock_code': stock_code,
                    'signal_type': order.get('side'),
                    'grid_level': session.current_center_price,
                    'trigger_price': order.get('expected_price'),
                    'session_id': session_id,
                }

            self.pending_grid_orders[str(order['order_id'])] = {
                'order_id': str(order['order_id']),
                'session_id': session_id,
                'stock_code': stock_code,
                'side': order.get('side'),
                'signal': signal,
                'requested_volume': int(order.get('requested_volume') or 0),
                'expected_price': float(order.get('expected_price') or 0),
                'filled_volume': int(order.get('filled_volume') or 0),
                'filled_amount': float(order.get('filled_amount') or 0),
                'confirmed_trade_ids': set(),
                'created_at': order.get('submitted_at') or datetime.now().isoformat()
            }
            recovered += 1

        return recovered

    def start_grid_session(self, stock_code: str, user_config: dict) -> GridSession:
        """启动网格交易会话（三阶段设计，避免AB-BA死锁）

        阶段1（锁外）：获取持仓数据并验证前置条件
        阶段2（锁内）：停止旧session、创建数据库记录、创建内存对象
        阶段3（锁外）：触发数据版本更新、打印成功日志
        """
        logger.info(f"[GRID] start_grid_session: ========== 开始启动会话 ==========")
        logger.info(f"[GRID] start_grid_session: stock_code={stock_code}")
        logger.debug(f"[GRID] start_grid_session: user_config={user_config}")
        # 统一 sessions 字典 key（去除交易所后缀）
        stock_code_key = self._normalize_code(stock_code)
        logger.info(f"[GRID] start_grid_session: stock_code_key={stock_code_key}")

        # ========== 阶段1: 锁外操作 - 获取持仓数据并验证 ==========
        logger.info(f"[GRID] start_grid_session: [阶段1] 获取持仓数据（锁外）...")

        # 使用ThreadPoolExecutor + 5秒超时避免阻塞
        position = None
        with ThreadPoolExecutor(max_workers=1) as executor:
            try:
                future = executor.submit(self.position_manager.get_position, stock_code)
                position = future.result(timeout=config.GRID_POSITION_QUERY_TIMEOUT)
            except TimeoutError:
                logger.error(f"[GRID] start_grid_session: [阶段1] 获取持仓超时({config.GRID_POSITION_QUERY_TIMEOUT}秒)，拒绝启动")
                raise RuntimeError(f"获取{stock_code}持仓信息超时，请稍后重试")
            except Exception as e:
                logger.error(f"[GRID] start_grid_session: [阶段1] 获取持仓失败: {str(e)}")
                raise

        # 验证持仓条件
        if not position:
            logger.warning(f"[GRID] start_grid_session: [阶段1] {stock_code}无持仓, 拒绝启动")
            raise ValueError(f"{stock_code}无持仓，无法启动网格交易")

        # 检查是否要求已触发止盈（可配置）
        if config.GRID_REQUIRE_PROFIT_TRIGGERED and not position.get('profit_triggered'):
            logger.warning(f"[GRID] start_grid_session: [阶段1] {stock_code}未触发止盈, 拒绝启动 (GRID_REQUIRE_PROFIT_TRIGGERED=True)")
            raise ValueError(f"{stock_code}未触发首次止盈，无法启动网格交易")

        logger.debug(f"[GRID] start_grid_session: [阶段1] 前置条件验证通过, volume={position.get('volume')}, profit_triggered={position.get('profit_triggered')}")

        # 确定中心价格
        user_center_price = user_config.get('center_price')
        highest_price = position.get('highest_price', 0)

        if user_center_price and user_center_price > 0:
            center_price = user_center_price
            logger.info(f"[GRID] start_grid_session: [阶段1] 使用用户自定义中心价格: {center_price:.2f}")
        elif highest_price > 0:
            center_price = highest_price
            logger.info(f"[GRID] start_grid_session: [阶段1] 使用历史最高价作为中心价格: {center_price:.2f}")
        else:
            logger.warning(f"[GRID] start_grid_session: [阶段1] 缺少有效的中心价格, 拒绝启动")
            raise ValueError(f"{stock_code}缺少有效的中心价格")

        # 预构建会话数据
        start_time = datetime.now()
        end_time = start_time + timedelta(days=user_config.get('duration_days', 7))
        current_price = position.get('current_price', highest_price)

        session_data = {
            'stock_code': stock_code,
            'center_price': center_price,
            'price_interval': user_config.get('price_interval', config.GRID_DEFAULT_PRICE_INTERVAL),
            'position_ratio': user_config.get('position_ratio', config.GRID_DEFAULT_POSITION_RATIO),
            'callback_ratio': user_config.get('callback_ratio', config.GRID_CALLBACK_RATIO),
            'max_investment': user_config.get('max_investment', 0),
            'max_deviation': user_config.get('max_deviation', config.GRID_MAX_DEVIATION_RATIO),
            'target_profit': user_config.get('target_profit', config.GRID_TARGET_PROFIT_RATIO),
            'stop_loss': user_config.get('stop_loss', config.GRID_STOP_LOSS_RATIO),
            'start_time': start_time.isoformat(),
            'end_time': end_time.isoformat(),
            'risk_level': user_config.get('risk_level', 'moderate'),
            'template_name': user_config.get('template_name')
        }
        logger.info(f"[GRID] start_grid_session: [阶段1] 完成，预构建会话数据完成")

        # ========== 阶段2: 锁内操作 - 停止旧session、创建记录 ==========
        logger.info(f"[GRID] start_grid_session: [阶段2] 尝试获取锁...")
        lock_acquired = self.lock.acquire(timeout=config.GRID_LOCK_ACQUIRE_TIMEOUT)
        if not lock_acquired:
            logger.error(f"[GRID] start_grid_session: [阶段2] 获取锁超时({config.GRID_LOCK_ACQUIRE_TIMEOUT}秒)! 拒绝启动")
            raise RuntimeError(f"网格交易启动失败：系统繁忙，请稍后重试")

        logger.info(f"[GRID] start_grid_session: [阶段2] 成功获取锁，开始处理...")
        session = None
        try:
            # 检查并停止旧session
            if stock_code_key in self.sessions:
                raise ValueError(f"{stock_code}已存在活跃会话，请先停止当前会话")

            # 创建数据库记录
            session_id = self.db.create_grid_session(session_data)
            logger.debug(f"[GRID] start_grid_session: [阶段2] 数据库创建成功, session_id={session_id}")

            # 创建内存对象
            session = GridSession(
                id=session_id,
                stock_code=stock_code,
                status='active',
                center_price=center_price,  # ✅ 使用阶段1确定的中心价格
                current_center_price=center_price,  # ✅ 初始化为相同值
                price_interval=session_data['price_interval'],
                position_ratio=session_data['position_ratio'],
                callback_ratio=session_data['callback_ratio'],
                max_investment=session_data['max_investment'],
                max_deviation=session_data['max_deviation'],
                target_profit=session_data['target_profit'],
                stop_loss=session_data['stop_loss'],
                start_time=start_time,
                end_time=end_time
            )
            self.sessions[stock_code_key] = session
            logger.debug(f"[GRID] start_grid_session: [阶段2] 内存会话对象创建完成")

            # 创建PriceTracker
            self.trackers[session_id] = PriceTracker(
                session_id=session_id,
                last_price=current_price,
                peak_price=current_price,
                valley_price=current_price
            )
            logger.debug(f"[GRID] start_grid_session: [阶段2] PriceTracker创建完成, current_price={current_price:.2f}")

        finally:
            self.lock.release()
            logger.info(f"[GRID] start_grid_session: [阶段2] 已释放锁")

        # ========== 阶段3: 锁外操作 - 后处理 ==========
        logger.info(f"[GRID] start_grid_session: [阶段3] 执行后处理...")

        # 触发数据版本更新
        self.position_manager._increment_data_version()

        # 打印成功日志
        levels = session.get_grid_levels()
        logger.info(f"[GRID] start_grid_session: ========== 启动成功 ==========")
        logger.info(f"[GRID] start_grid_session: 股票代码={stock_code}, 会话ID={session.id}")
        logger.info(f"[GRID] start_grid_session: 中心价={highest_price:.2f}, 档位间隔={session.price_interval*100:.1f}%")
        logger.info(f"[GRID] start_grid_session: 网格档位 lower={levels['lower']:.2f}, center={levels['center']:.2f}, upper={levels['upper']:.2f}")
        logger.info(f"[GRID] start_grid_session: 最大投入={session.max_investment:.2f}, 持仓比例={session.position_ratio*100:.1f}%")
        logger.info(f"[GRID] start_grid_session: 回调比例={session.callback_ratio*100:.2f}%, 最大偏离={session.max_deviation*100:.1f}%")
        logger.info(f"[GRID] start_grid_session: 目标盈利={session.target_profit*100:.1f}%, 止损={session.stop_loss*100:.1f}%")
        logger.info(f"[GRID] start_grid_session: 有效期至 {end_time.strftime('%Y-%m-%d %H:%M:%S')}")

        return session

    def stop_grid_session(self, session_id: int, reason: str) -> dict:
        """停止网格交易会话（公共接口，会获取锁）

        实盘成交确认模式下采用撤单闭环:
        1. 若存在提交中/待成交委托，先把会话置为 stopping，并对待成交委托发撤单请求。
        2. 等委托终态回调或锁外下单完成后，再真正清理内存会话。
        3. 无未完成委托时保持旧行为，立即停止。
        """
        logger.info(f"[GRID] stop_grid_session: 开始停止会话 session_id={session_id}, reason={reason}")

        cancel_orders = []
        with self.lock:
            session = self._find_session_by_id(session_id)
            if not session:
                logger.warning(f"[GRID] stop_grid_session: 会话{session_id}不存在, 无法停止")
                raise ValueError(f"会话{session_id}不存在")

            open_orders = [
                (order_id, pending)
                for order_id, pending in self.pending_grid_orders.items()
                if pending.get('session_id') == session_id
            ]
            submitting_orders = [
                plan
                for plan in self.submitting_grid_orders.values()
                if plan.get('session_id') == session_id
            ]

            if open_orders or submitting_orders:
                session.status = 'stopping'
                session.stop_reason = reason
                if hasattr(self.db, 'update_grid_session'):
                    self.db.update_grid_session(session_id, {
                        'status': 'stopping',
                        'stop_reason': reason
                    })

                for order_id, pending in open_orders:
                    pending['stop_requested'] = True
                    pending['stop_reason'] = reason
                    cancel_orders.append(order_id)
                    try:
                        if hasattr(self.db, 'update_grid_order'):
                            self.db.update_grid_order(order_id, {
                                'status': 'cancel_requested',
                                'last_error': f'stop requested: {reason}'
                            })
                    except Exception as db_err:
                        logger.warning(f"[GRID] stop_grid_session: 标记撤单请求失败 order_id={order_id}, err={db_err}")

                for plan in submitting_orders:
                    plan['stop_requested'] = True
                    plan['stop_reason'] = reason

                logger.warning(
                    f"[GRID] stop_grid_session: session_id={session_id} 进入stopping，"
                    f"待撤单={len(open_orders)}, 提交中={len(submitting_orders)}"
                )
            else:
                return self._stop_grid_session_unlocked(session_id, reason)

        cancel_ok = 0
        cancel_failed = 0
        for order_id in cancel_orders:
            if self._cancel_grid_order(order_id):
                cancel_ok += 1
            else:
                cancel_failed += 1
                with self.lock:
                    pending = self.pending_grid_orders.get(str(order_id))
                    if pending:
                        try:
                            if hasattr(self.db, 'update_grid_order'):
                                self.db.update_grid_order(order_id, {
                                    'status': 'cancel_failed',
                                    'last_error': f'cancel failed during stop: {reason}'
                                })
                        except Exception as db_err:
                            logger.warning(f"[GRID] stop_grid_session: 写入撤单失败状态失败 order_id={order_id}, err={db_err}")

        with self.lock:
            still_open = any(
                p.get('session_id') == session_id
                for p in self.pending_grid_orders.values()
            )
            still_submitting = any(
                p.get('session_id') == session_id
                for p in self.submitting_grid_orders.values()
            )
            if not still_open and not still_submitting:
                return self._stop_grid_session_unlocked(session_id, reason)

        return {
            'stock_code': session.stock_code,
            'trade_count': session.trade_count,
            'profit_ratio': session.get_profit_ratio(),
            'stop_reason': reason,
            'status': 'stopping',
            'pending_orders': len(cancel_orders),
            'cancel_requested': cancel_ok,
            'cancel_failed': cancel_failed
        }

    def _find_session_by_id(self, session_id: int) -> Optional[GridSession]:
        """按会话ID查找内存会话。"""
        for s in self.sessions.values():
            if s.id == session_id:
                return s
        return None

    def _cancel_grid_order(self, order_id: str) -> bool:
        """锁外发起网格委托撤单请求。"""
        order_id = str(order_id)
        try:
            if hasattr(self.executor, 'cancel_order'):
                ok = bool(self.executor.cancel_order(order_id))
            elif hasattr(self.executor, 'cancel_order_stock'):
                ok = self.executor.cancel_order_stock(order_id) == 0
            else:
                logger.error(f"[GRID] _cancel_grid_order: executor缺少撤单接口 order_id={order_id}")
                return False
            logger.info(f"[GRID] _cancel_grid_order: order_id={order_id}, ok={ok}")
            return ok
        except Exception as e:
            logger.error(f"[GRID] _cancel_grid_order: 撤单异常 order_id={order_id}, err={e}", exc_info=True)
            return False

    def _complete_stop_if_no_open_orders_unlocked(self, session_id: int) -> bool:
        """stopping 会话在所有未完成委托终结后自动完成停止。"""
        session = self._find_session_by_id(session_id)
        if not session or session.status != 'stopping':
            return False

        has_pending = any(
            p.get('session_id') == session_id
            for p in self.pending_grid_orders.values()
        )
        has_submitting = any(
            p.get('session_id') == session_id
            for p in self.submitting_grid_orders.values()
        )
        if has_pending or has_submitting:
            return False

        reason = session.stop_reason or 'stopped_after_orders_closed'
        self._stop_grid_session_unlocked(session_id, reason)
        logger.info(f"[GRID] stopping会话已完成停止 session_id={session_id}, reason={reason}")
        return True

    def _stop_grid_session_unlocked(self, session_id: int, reason: str) -> dict:
        """停止网格交易会话（内部方法，调用者必须已持有锁）"""
        logger.info(f"[GRID] _stop_grid_session_unlocked: 开始停止会话 session_id={session_id}, reason={reason}")

        # 查找会话
        session = self._find_session_by_id(session_id)

        if not session:
            logger.warning(f"[GRID] _stop_grid_session_unlocked: 会话{session_id}不存在, 无法停止")
            raise ValueError(f"会话{session_id}不存在")

        stock_code = session.stock_code
        stock_code_key = self._normalize_code(stock_code)  # 用于 sessions 字典操作
        logger.debug(f"[GRID] _stop_grid_session_unlocked: 找到会话 stock_code={stock_code}, key={stock_code_key}")

        # 记录停止前的统计信息
        logger.info(f"[GRID] _stop_grid_session_unlocked: 停止前统计:")
        logger.info(f"[GRID]   - 股票代码: {stock_code}")
        logger.info(f"[GRID]   - 总交易次数: {session.trade_count} (买入{session.buy_count}/卖出{session.sell_count})")
        logger.info(f"[GRID]   - 总买入金额: {session.total_buy_amount:.2f}")
        logger.info(f"[GRID]   - 总卖出金额: {session.total_sell_amount:.2f}")
        logger.info(f"[GRID]   - 网格盈亏: {session.get_profit_ratio()*100:.2f}%")
        logger.info(f"[GRID]   - 网格累计利润: {session.get_grid_profit():.2f}元")
        logger.info(f"[GRID]   - 最大投入额度: {session.max_investment:.2f}元")
        logger.info(f"[GRID]   - 当前投入: {session.current_investment:.2f}/{session.max_investment:.2f}")
        logger.info(f"[GRID]   - 中心价偏离: {session.get_deviation_ratio()*100:.2f}%")

        # 同步内存中的统计信息到数据库
        if stock_code_key in self.sessions:
            session_obj = self.sessions[stock_code_key]
            updates = {
                'trade_count': session_obj.trade_count,
                'buy_count': session_obj.buy_count,
                'sell_count': session_obj.sell_count,
                'total_buy_amount': session_obj.total_buy_amount,
                'total_sell_amount': session_obj.total_sell_amount,
                'current_investment': session_obj.current_investment
            }
            self.db.update_grid_session(session_id, updates)
            logger.debug(f"[GRID] _stop_grid_session_unlocked: 同步统计信息到数据库完成")

        # 更新数据库
        self.db.stop_grid_session(session_id, reason)
        logger.debug(f"[GRID] _stop_grid_session_unlocked: 数据库更新完成")

        # 从内存中移除
        if stock_code_key in self.sessions:
            del self.sessions[stock_code_key]
            logger.debug(f"[GRID] _stop_grid_session_unlocked: 从sessions中移除 {stock_code} (key={stock_code_key})")
        if session_id in self.trackers:
            del self.trackers[session_id]
            logger.debug(f"[GRID] _stop_grid_session_unlocked: 从trackers中移除 session_id={session_id}")

        # 清除冷却记录 (键格式为 (session_id: int, level_price: float))
        cooldown_keys = [k for k in self.level_cooldowns.keys() if k[0] == session_id]
        if cooldown_keys:
            logger.debug(f"[GRID] _stop_grid_session_unlocked: 清除 {len(cooldown_keys)} 个档位冷却记录")
        for key in cooldown_keys:
            del self.level_cooldowns[key]

        # 触发数据版本更新
        self.position_manager._increment_data_version()

        # ⭐ P0-2修复：清除该股票的网格信号，避免会话停止后信号仍在执行
        with self.position_manager.signal_lock:
            if stock_code in self.position_manager.latest_signals:
                signal_info = self.position_manager.latest_signals[stock_code]
                signal_type = signal_info.get('type', '')
                if signal_type.startswith('grid_'):
                    logger.info(f"[GRID] 会话停止，清除 {stock_code} 的网格信号: {signal_type}")
                    del self.position_manager.latest_signals[stock_code]

        final_stats = {
            'stock_code': stock_code,
            'trade_count': session.trade_count,
            'profit_ratio': session.get_profit_ratio(),
            'stop_reason': reason
        }

        logger.info(f"[GRID] _stop_grid_session_unlocked: 停止完成! stock_code={stock_code}, reason={reason}, "
                   f"trade_count={session.trade_count}, profit={session.get_profit_ratio()*100:.2f}%")

        return final_stats

    def _check_exit_conditions(self, session: GridSession, current_price: float,
                               position_snapshot=None) -> Optional[str]:
        """检查退出条件,返回退出原因或None

        Args:
            session: 网格会话对象
            current_price: 当前价格
            position_snapshot: 可选的持仓快照（锁外预取，避免锁内调用外部依赖）。
                               若为 None，则在方法内部直接调用 get_position()（向后兼容）。
        """
        logger.debug(f"[GRID] _check_exit_conditions: session_id={session.id}, stock_code={session.stock_code}, current_price={current_price:.2f}")

        # 1. 偏离度检测（双重保护）
        if session.current_center_price and session.center_price:
            # 网格漂移偏离：current_center 相对 initial_center 的偏移（多次同向交易后累计）
            drift_deviation = session.get_deviation_ratio()
            # 市价偏离：当前市价相对 current_center 的距离（捕捉单边行情未触发信号的情形）
            market_deviation = abs(current_price - session.current_center_price) / session.current_center_price
            deviation = max(drift_deviation, market_deviation)
            logger.debug(
                f"[GRID] _check_exit_conditions: 偏离度检测 "
                f"drift={drift_deviation*100:.2f}%, market={market_deviation*100:.2f}%, "
                f"max={session.max_deviation*100:.2f}%"
            )
            if deviation > session.max_deviation:
                logger.warning(
                    f"[GRID] _check_exit_conditions: {session.stock_code} "
                    f"偏离度{deviation*100:.2f}%超过限制{session.max_deviation*100:.2f}% "
                    f"(drift={drift_deviation*100:.2f}%, market={market_deviation*100:.2f}%), 触发退出"
                )
                return 'deviation'

        # 2. 盈亏检测:
        # - 止盈：要求已完成至少1次买入+1次卖出（闭环后再止盈）
        # - 止损：允许"仅买未卖"阶段触发，防止单边下跌持续买入导致风险扩大
        # True P&L 公式: (sell-buy) + open_grid_vol * price, denom=max_investment
        # 降级: 旧 session 无 volume 数据时自动回退到 market_value 分母
        # 保底机制: deviation 检测（第1步）负责极端单边下跌场景的退出保护
        # 测试参考: test_grid_exit_profit_loss.py, test_grid_bugfix_c1.py::TestDesign4StopLossWithoutSell
        #           test_grid_profit_ratio_fix.py, test_grid_true_pnl.py（True P&L验证）
        if session.buy_count > 0:
            _position_volume = position_snapshot.get('volume', 0) if position_snapshot else 0
            ledger_summary = None
            if hasattr(self.db, 'get_grid_ledger_summary'):
                try:
                    ledger_summary = self.db.get_grid_ledger_summary(session.id, current_price)
                except Exception as ledger_err:
                    logger.warning(f"[GRID] _check_exit_conditions: 账本盈亏汇总失败，降级旧口径: {ledger_err}")

            if ledger_summary and ledger_summary.get('has_ledger') and session.max_investment > 0:
                profit_ratio = ledger_summary['true_pnl'] / session.max_investment
                _pnl_label = (
                    f"ledger(open={ledger_summary['open_volume']}, "
                    f"realized={ledger_summary['realized_pnl']:.2f}, "
                    f"unrealized={ledger_summary['unrealized_pnl']:.2f})"
                )
            else:
                profit_ratio = session.get_true_pnl_ratio(current_price, _position_volume)
                _open_vol = session.total_buy_volume - session.total_sell_volume
                if session.total_buy_volume > 0 or session.total_sell_volume > 0:
                    _pnl_label = f"true_pnl(open_vol={_open_vol}, price={current_price:.2f})"
                elif _position_volume > 0:
                    _pnl_label = f"fallback_mv({_position_volume:.0f}x{current_price:.2f})"
                else:
                    _pnl_label = f"fallback_mi({session.max_investment:.0f})"
            _open_vol = session.total_buy_volume - session.total_sell_volume
            logger.debug(f"[GRID] _check_exit_conditions: profit_ratio={profit_ratio*100:.2f}% "
                        f"method={_pnl_label}, "
                        f"target={session.target_profit*100:.2f}%, stop_loss={session.stop_loss*100:.2f}%, "
                        f"buy_count={session.buy_count}, sell_count={session.sell_count}")

            # 止盈检测（需要买卖配对）
            if session.sell_count > 0 and profit_ratio >= session.target_profit:
                logger.info(f"[GRID] {session.stock_code} 达到目标盈利{profit_ratio*100:.2f}%, "
                           f"buy_count={session.buy_count}, sell_count={session.sell_count}")
                return 'target_profit'

            # 止损检测（允许仅买未卖阶段触发）
            if profit_ratio <= session.stop_loss:
                logger.warning(f"[GRID] {session.stock_code} 触发止损{profit_ratio*100:.2f}%, "
                              f"buy_count={session.buy_count}, sell_count={session.sell_count}")
                return 'stop_loss'
        else:
            logger.debug(f"[GRID] _check_exit_conditions: 未有买入记录, 跳过盈亏检测")

        # 3. 时间限制
        if session.end_time:
            remaining = session.end_time - datetime.now()
            logger.debug(f"[GRID] _check_exit_conditions: 时间检测 end_time={session.end_time}, remaining={remaining}")
            if datetime.now() > session.end_time:
                logger.info(f"[GRID] _check_exit_conditions: {session.stock_code} 达到运行时长限制, 触发退出")
                return 'expired'

        # 4. 持仓清空（优先使用锁外预取的快照，避免锁内调用外部依赖导致死锁）
        # A-3修复：若调用方提供了 position_snapshot，直接使用；否则降级为直接调用（向后兼容）。
        if position_snapshot is not None:
            position = position_snapshot
        else:
            position = self.position_manager.get_position(session.stock_code)
        volume = position.get('volume', 0) if position else 0
        logger.debug(f"[GRID] _check_exit_conditions: 持仓检测 volume={volume}")
        if not position or volume == 0:
            logger.info(f"[GRID] _check_exit_conditions: {session.stock_code} 持仓已清空, 触发退出")
            return 'position_cleared'

        logger.debug(f"[GRID] _check_exit_conditions: 未触发任何退出条件")
        return None

    def _check_level_crossing(self, session: GridSession, tracker: PriceTracker, price: float):
        """检查是否穿越档位"""
        levels = session.get_grid_levels()
        logger.debug(f"[GRID] _check_level_crossing: session_id={session.id}, stock_code={session.stock_code}, "
                    f"price={price:.2f}, levels=[{levels['lower']:.2f}, {levels['center']:.2f}, {levels['upper']:.2f}], "
                    f"waiting_callback={tracker.waiting_callback}")

        # 检查上穿(卖出档位)
        if price > levels['upper'] and not tracker.waiting_callback:
            logger.debug(f"[GRID] _check_level_crossing: 检测到上穿卖出档位 price={price:.2f} > upper={levels['upper']:.2f}")
            # 检查冷却
            if self._is_level_in_cooldown(session.id, levels['upper']):
                logger.debug(f"[GRID] _check_level_crossing: 卖出档位{levels['upper']:.2f}在冷却期, 跳过")
                return

            tracker.crossed_level = levels['upper']
            tracker.peak_price = price
            tracker.direction = 'rising'
            tracker.waiting_callback = True

            logger.info(f"[GRID] _check_level_crossing: {session.stock_code} 穿越卖出档位{levels['upper']:.2f}, "
                       f"price={price:.2f}, 等待回调{session.callback_ratio*100:.2f}%")

        # 检查下穿(买入档位)
        elif price < levels['lower'] and not tracker.waiting_callback:
            logger.debug(f"[GRID] _check_level_crossing: 检测到下穿买入档位 price={price:.2f} < lower={levels['lower']:.2f}")
            # Gap 2修复：max_investment 耗尽时跳过买入穿越检测。
            # 若不检查，买入失败后 tracker 虽重置为 waiting=False，但价格仍在下轨以下，
            # 下一个 tick 立刻又检测到穿越并设置 waiting=True，形成每 6 秒一次的慢速循环。
            if session.current_investment >= session.max_investment > 0:
                logger.warning(f"[GRID] _check_level_crossing: {session.stock_code} max_investment已耗尽"
                               f"({session.current_investment:.0f}/{session.max_investment:.0f}), "
                               f"跳过买入档位穿越检测，等待卖出后资金回收")
                return
            # 检查冷却
            if self._is_level_in_cooldown(session.id, levels['lower']):
                logger.debug(f"[GRID] _check_level_crossing: 买入档位{levels['lower']:.2f}在冷却期, 跳过")
                return

            tracker.crossed_level = levels['lower']
            tracker.valley_price = price
            tracker.direction = 'falling'
            tracker.waiting_callback = True

            logger.info(f"[GRID] _check_level_crossing: {session.stock_code} 穿越买入档位{levels['lower']:.2f}, "
                       f"price={price:.2f}, 等待回升{session.callback_ratio*100:.2f}%")
        else:
            logger.debug(f"[GRID] _check_level_crossing: 价格在档位区间内, 无穿越")

    def _is_level_in_cooldown(self, session_id: int, level_price: float) -> bool:
        """检查档位是否在冷却期"""
        key = (session_id, level_price)
        if key not in self.level_cooldowns:
            logger.debug(f"[GRID] _is_level_in_cooldown: session_id={session_id}, level={level_price:.2f}, 无冷却记录, 返回False")
            return False

        elapsed = time.time() - self.level_cooldowns[key]
        cooldown = config.GRID_LEVEL_COOLDOWN
        in_cooldown = elapsed < cooldown
        logger.debug(f"[GRID] _is_level_in_cooldown: session_id={session_id}, level={level_price:.2f}, "
                    f"elapsed={elapsed:.1f}s, cooldown={cooldown}s, in_cooldown={in_cooldown}")
        return in_cooldown

    def check_grid_signals(self, stock_code: str, current_price: float) -> Optional[dict]:
        """
        检查网格交易信号(在持仓监控线程中调用)

        Args:
            stock_code: 股票代码
            current_price: 当前价格

        Returns:
            网格交易信号字典或None
        """
        logger.debug(f"[GRID] check_grid_signals: stock_code={stock_code}, current_price={current_price:.2f}, "
                    f"active_sessions_count={len(self.sessions)}")

        # A-3修复: 锁外预取持仓，避免在持有 self.lock 时调用 position_manager.get_position()
        # 风险: _check_exit_conditions 内部（条件4）调用 get_position()，若 position_manager
        # 内部某方法先持 signal_lock 再请求 grid_manager.lock，将形成 AB-BA 死锁。
        # 修复: 在获取 self.lock 之前先读取持仓快照，通过参数传入 _check_exit_conditions，
        # 避免锁内执行可能引发锁序反转的外部调用。
        position_snapshot = None
        try:
            position_snapshot = self.position_manager.get_position(stock_code)
        except Exception as e:
            logger.warning(f"[GRID] check_grid_signals: 锁外预取持仓失败(将视为无持仓): {e}")

        with self.lock:
            session = self.sessions.get(self._normalize_code(stock_code))
            if not session:
                logger.debug(f"[GRID] check_grid_signals: {stock_code} 无活跃会话, 返回None")
                return None
            if session.status != 'active':
                logger.debug(f"[GRID] check_grid_signals: {stock_code} 会话状态={session.status}, 非active, 返回None")
                return None

            logger.debug(f"[GRID] check_grid_signals: 找到活跃会话 session_id={session.id}, status={session.status}")

            # 1. 检查退出条件（传入锁外预取的持仓快照）
            exit_reason = self._check_exit_conditions(session, current_price, position_snapshot=position_snapshot)
            if exit_reason:
                logger.info(f"[GRID] check_grid_signals: {stock_code} 触发退出条件 reason={exit_reason}")
                # RISK-4修复：捕获 ValueError，防止并发场景下（如 Web API 同时手动停止）
                # 第二次调用 stop_grid_session 因会话已消失而抛出未处理异常，导致持仓监控线程崩溃
                try:
                    self.stop_grid_session(session.id, exit_reason)
                except ValueError as e:
                    logger.warning(f"[GRID] check_grid_signals: 停止会话时会话已不存在（可能已被并发停止）: {e}")
                return None

            # 2. 更新价格追踪器
            tracker = self.trackers.get(session.id)
            if not tracker:
                logger.warning(f"[GRID] check_grid_signals: session_id={session.id} 无对应的PriceTracker, 返回None")
                return None

            tracker.update_price(current_price)

            # 3. 检查是否穿越新档位
            self._check_level_crossing(session, tracker, current_price)

            # 4. 检查回调触发
            signal_type = tracker.check_callback(session.callback_ratio)
            if signal_type:
                # ⭐ P1-1修复：信号去重机制 - 检查是否已有相同类型的信号
                with self.position_manager.signal_lock:
                    existing = self.position_manager.latest_signals.get(stock_code)
                    if existing and existing.get('type') == f'grid_{signal_type.lower()}':
                        logger.debug(f"[GRID] check_grid_signals: {stock_code} 已有 {signal_type} 信号，跳过重复生成")
                        return None

                logger.info(f"[GRID] check_grid_signals: {stock_code} 检测到信号 signal_type={signal_type}")
                return self._create_grid_signal(session, tracker, signal_type, current_price)

            logger.debug(f"[GRID] check_grid_signals: {stock_code} 本次检查无信号")
            return None

    def _create_grid_signal(self, session: GridSession, tracker: PriceTracker,
                           signal_type: str, current_price: float) -> dict:
        """创建网格交易信号"""
        logger.debug(f"[GRID] _create_grid_signal: session_id={session.id}, stock_code={session.stock_code}, "
                    f"signal_type={signal_type}, current_price={current_price:.2f}")

        signal = {
            'stock_code': session.stock_code,
            'strategy': config.GRID_STRATEGY_NAME,
            'signal_type': signal_type,
            'grid_level': tracker.crossed_level,
            'trigger_price': current_price,
            'session_id': session.id,
            'timestamp': datetime.now().isoformat(),
            'signal_source': 'grid_tracker',
            'require_price_recheck': True
        }

        if signal_type == 'SELL':
            signal['peak_price'] = tracker.peak_price
            # 防止除零错误
            if tracker.peak_price > 0:
                signal['callback_ratio'] = (tracker.peak_price - current_price) / tracker.peak_price
            else:
                signal['callback_ratio'] = 0.0
            logger.debug(f"[GRID] _create_grid_signal: SELL信号 peak_price={tracker.peak_price:.2f}, "
                        f"callback_ratio={signal['callback_ratio']*100:.2f}%")
        elif signal_type == 'BUY':
            signal['valley_price'] = tracker.valley_price
            # 防止除零错误
            if tracker.valley_price > 0:
                signal['callback_ratio'] = (current_price - tracker.valley_price) / tracker.valley_price
            else:
                signal['callback_ratio'] = 0.0
            logger.debug(f"[GRID] _create_grid_signal: BUY信号 valley_price={tracker.valley_price:.2f}, "
                        f"callback_ratio={signal['callback_ratio']*100:.2f}%")

        logger.info(f"[GRID] _create_grid_signal: 生成网格{signal_type}信号: {session.stock_code}, "
                   f"档位={tracker.crossed_level:.2f}, 触发价={current_price:.2f}, "
                   f"回调={signal.get('callback_ratio', 0)*100:.2f}%")

        return signal

    def _rebuild_grid(self, session: GridSession, trade_price: float):
        """交易后重建网格,以成交价为新中心"""
        logger.debug(f"[GRID] _rebuild_grid: session_id={session.id}, stock_code={session.stock_code}, trade_price={trade_price:.2f}")

        old_center = session.current_center_price
        session.current_center_price = trade_price
        logger.debug(f"[GRID] _rebuild_grid: 更新中心价 {old_center:.2f} -> {trade_price:.2f}")

        # 重置追踪器
        tracker = self.trackers.get(session.id)
        if tracker:
            logger.debug(f"[GRID] _rebuild_grid: 重置PriceTracker")
            tracker.reset(trade_price)
        else:
            logger.warning(f"[GRID] _rebuild_grid: session_id={session.id} 无对应的PriceTracker")

        # 更新数据库（独立保护: 失败时不回滚内存状态，交易统计已由RISK-1/RISK-2保障）
        # 网格中心价不一致会在下一笔交易时自动覆盖，风险可控
        try:
            self.db.update_grid_session(session.id, {
                'current_center_price': trade_price
            })
        except Exception as db_err:
            logger.error(f"[GRID] _rebuild_grid: DB更新center_price失败"
                        f"(内存已更新,下一笔交易时会覆盖): {db_err}")
        logger.debug(f"[GRID] _rebuild_grid: 数据库更新完成")

        levels = session.get_grid_levels()
        logger.info(f"[GRID] _rebuild_grid: 网格重建完成 {session.stock_code}, "
                   f"旧中心={old_center:.2f} -> 新中心={trade_price:.2f}, "
                   f"新档位=[{levels['lower']:.2f}, {levels['center']:.2f}, {levels['upper']:.2f}]")

    @staticmethod
    def _get_attr_or_key(obj, names, default=None):
        """从对象或字典中按多个候选字段读取值"""
        for name in names:
            if isinstance(obj, dict) and name in obj:
                return obj.get(name)
            if hasattr(obj, name):
                return getattr(obj, name)
        return default

    def _extract_price_from_quote(self, quote, stock_code: str):
        """从不同行情结构中提取最新价"""
        if not quote:
            return None

        data = quote
        if isinstance(quote, dict):
            normalized = self._normalize_code(stock_code)
            for key in (stock_code, normalized):
                nested = quote.get(key)
                if isinstance(nested, dict):
                    data = nested
                    break

        if isinstance(data, dict):
            for field in ('lastPrice', 'last_price', 'price', 'close', 'lastClose'):
                value = data.get(field)
                if value is not None:
                    try:
                        price = float(value)
                        return price if price > 0 else None
                    except (TypeError, ValueError):
                        continue
        return None

    def _get_latest_price_for_signal(self, stock_code: str, position_snapshot=None):
        """执行前获取实时行情价格；拿不到盘口价时不使用持仓缓存价替代"""
        try:
            data_manager = getattr(self.position_manager, 'data_manager', None)
            if data_manager and hasattr(data_manager, 'get_latest_data'):
                quote = data_manager.get_latest_data(stock_code)
                price = self._extract_price_from_quote(quote, stock_code)
                if price is not None:
                    return price
        except Exception as e:
            logger.warning(f"[GRID] _get_latest_price_for_signal: 获取行情失败 stock_code={stock_code}, err={e}")

        return None

    def _get_price_limits(self, stock_code: str):
        """获取标的当日涨停价/跌停价。

        来源: xtdata.get_instrument_detail 的 UpStopPrice/DownStopPrice 字段。
        获取失败时返回 (None, None)，由调用方 fail-open 处理。
        """
        try:
            data_manager = getattr(self.position_manager, 'data_manager', None)
            xt = getattr(data_manager, 'xt', None) if data_manager else None
            if xt is None or not hasattr(xt, 'get_instrument_detail'):
                return None, None
            detail = xt.get_instrument_detail(stock_code)
            if not isinstance(detail, dict):
                return None, None

            def _first_positive(keys):
                for k in keys:
                    v = detail.get(k)
                    if v is not None:
                        try:
                            fv = float(v)
                            if fv > 0:
                                return fv
                        except (TypeError, ValueError):
                            continue
                return None

            up_limit = _first_positive(('UpStopPrice', 'upStopPrice', 'HighLimit', '涨停价'))
            down_limit = _first_positive(('DownStopPrice', 'downStopPrice', 'LowLimit', '跌停价'))
            return up_limit, down_limit
        except Exception as e:
            logger.debug(f"[GRID] _get_price_limits: 获取涨跌停价失败 stock_code={stock_code}, err={e}")
            return None, None

    def _check_tradable(self, stock_code: str, signal_type: str, current_price):
        """实盘下单前涨跌停/停牌防护。

        守卫的核心价值是涨跌停拦截(executor 层不检查涨跌停)；停牌则有 executor
        盘口兜底(对手价模式下取不到盘口价会拒单)，故停牌判定从严，避免误伤降级场景：

        - 涨停板: 拦截买入(封板买不进/追涨)
        - 跌停板: 拦截卖出(封板卖不出)
        - 停牌: 仅当"标的明细可查(说明标的真实存在)但拿不到有效现价"时拦截；
                明细查不到(数据源降级/测试 mock)时 fail-open，交 executor 盘口兜底。
        - 涨跌停价获取失败: fail-open(放行)

        Returns:
            (是否可交易: bool, 原因: str)
        """
        up_limit, down_limit = self._get_price_limits(stock_code)
        has_limits = up_limit is not None or down_limit is not None

        # 停牌检测: 标的明细可查但无有效现价 → 疑似停牌
        if current_price is None or current_price <= 0:
            if has_limits:
                return False, "标的明细可查但无有效现价(疑似停牌)"
            return True, ""  # 明细不可查(降级/mock)，放行交 executor 兜底

        eps = getattr(config, 'GRID_PRICE_LIMIT_EPS', 0.001)
        if signal_type == 'BUY' and up_limit is not None and current_price >= up_limit - eps:
            return False, f"已涨停(现价{current_price:.2f}>=涨停{up_limit:.2f})，跳过买入"
        if signal_type == 'SELL' and down_limit is not None and current_price <= down_limit + eps:
            return False, f"已跌停(现价{current_price:.2f}<=跌停{down_limit:.2f})，跳过卖出"
        return True, ""

    def _parse_signal_timestamp(self, value):
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value)
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace('Z', '+00:00')).replace(tzinfo=None)
        raise ValueError(f"不支持的timestamp类型: {type(value)}")

    def _validate_grid_signal_before_execute(self, signal: dict, session: GridSession, latest_price=None) -> bool:
        """执行前复核网格信号，防止旧信号、会话错配和明显价格漂移"""
        if session.status != 'active':
            logger.warning(f"[GRID] signal validate: 会话非active session_id={session.id}, status={session.status}")
            return False

        signal_session_id = signal.get('session_id')
        if signal_session_id is not None and str(signal_session_id) != str(session.id):
            logger.warning(f"[GRID] signal validate: session_id不匹配 signal={signal_session_id}, current={session.id}")
            return False

        timestamp = signal.get('timestamp')
        if timestamp is not None:
            try:
                signal_time = self._parse_signal_timestamp(timestamp)
            except Exception as e:
                logger.warning(f"[GRID] signal validate: timestamp无效 timestamp={timestamp}, err={e}")
                return False

            max_age = getattr(config, 'GRID_SIGNAL_MAX_AGE_SECONDS', 60)
            if max_age and max_age > 0:
                age_seconds = (datetime.now() - signal_time).total_seconds()
                if age_seconds > max_age:
                    logger.warning(f"[GRID] signal validate: 信号过期 age={age_seconds:.1f}s > {max_age}s")
                    return False
                if age_seconds < -5:
                    logger.warning(f"[GRID] signal validate: 信号时间来自未来 age={age_seconds:.1f}s")
                    return False

        should_check_price_drift = (
            not getattr(config, 'ENABLE_SIMULATION_MODE', True)
            and (
                signal.get('require_price_recheck') is True
                or signal.get('signal_source') == 'grid_tracker'
            )
        )
        trigger_price = signal.get('trigger_price')
        max_drift = getattr(config, 'GRID_SIGNAL_MAX_PRICE_DRIFT_RATIO', 0.01)
        if should_check_price_drift and latest_price is not None and trigger_price and max_drift and max_drift > 0:
            try:
                trigger_price = float(trigger_price)
            except (TypeError, ValueError):
                logger.warning(f"[GRID] signal validate: trigger_price无效 value={trigger_price}")
                return False
            if trigger_price <= 0:
                logger.warning(f"[GRID] signal validate: trigger_price非正 value={trigger_price}")
                return False
            drift = abs(float(latest_price) - trigger_price) / trigger_price
            if drift > max_drift:
                logger.warning(
                    f"[GRID] signal validate: 价格漂移过大 stock_code={session.stock_code}, "
                    f"latest={latest_price:.4f}, trigger={trigger_price:.4f}, "
                    f"drift={drift*100:.2f}% > {max_drift*100:.2f}%"
                )
                return False
        elif should_check_price_drift and timestamp is not None and signal_session_id is not None:
            logger.debug(f"[GRID] signal validate: 未取得最新价，跳过价格漂移复核 stock_code={session.stock_code}")

        return True

    def _register_pending_grid_order(self, order_id: str, session: GridSession, signal: dict,
                                     side: str, volume: int, expected_price: float) -> None:
        normalized_order_id = str(order_id)
        pending_info = {
            'order_id': normalized_order_id,
            'session_id': session.id,
            'stock_code': session.stock_code,
            'side': side,
            'signal': dict(signal),
            'requested_volume': int(volume),
            'expected_price': float(expected_price),
            'filled_volume': 0,
            'filled_amount': 0.0,
            'confirmed_trade_ids': set(),
            'created_at': datetime.now().isoformat()
        }
        if hasattr(self.db, 'create_grid_order'):
            self.db.create_grid_order({
                'order_id': normalized_order_id,
                'session_id': session.id,
                'stock_code': session.stock_code,
                'side': side,
                'status': 'submitted',
                'requested_volume': int(volume),
                'expected_price': float(expected_price),
                'filled_volume': 0,
                'filled_amount': 0.0,
                'submitted_at': pending_info['created_at'],
                'raw_signal': json.dumps(dict(signal), ensure_ascii=False, default=str)
            })
        self.pending_grid_orders[normalized_order_id] = pending_info
        logger.info(
            f"[GRID] pending order registered: order_id={normalized_order_id}, "
            f"session_id={session.id}, side={side}, volume={volume}, price={expected_price:.2f}"
        )

    def _record_confirmed_grid_trade(self, session: GridSession, signal: dict, side: str,
                                     price: float, volume: int, trade_id: str) -> bool:
        """按真实成交回报落账，并在DB失败时回滚内存统计"""
        return self._record_confirmed_grid_trade_with_order(
            session=session,
            signal=signal,
            side=side,
            price=price,
            volume=volume,
            trade_id=trade_id
        )

    def _record_confirmed_grid_trade_with_order(self, session: GridSession, signal: dict, side: str,
                                                price: float, volume: int, trade_id: str,
                                                order_id: str = None,
                                                order_updates: dict = None) -> bool:
        """按真实成交回报落账，并在DB失败时回滚内存统计"""
        stock_code = session.stock_code
        price = float(price)
        volume = int(volume)
        amount = price * volume

        old_trade_count = session.trade_count
        old_buy_count = session.buy_count
        old_sell_count = session.sell_count
        old_total_buy = session.total_buy_amount
        old_total_sell = session.total_sell_amount
        old_total_buy_vol = session.total_buy_volume
        old_total_sell_vol = session.total_sell_volume
        old_investment = session.current_investment

        session.trade_count += 1
        if side == 'BUY':
            session.buy_count += 1
            session.total_buy_amount += amount
            session.total_buy_volume += volume
            session.current_investment += amount
            if session.current_investment > session.max_investment + 0.01:
                logger.error(
                    f"[GRID] confirmed buy hard cap: current_investment={session.current_investment:.4f} "
                    f"> max_investment={session.max_investment:.4f}, 修正至max_investment"
                )
                session.current_investment = session.max_investment
        else:
            session.sell_count += 1
            session.total_sell_amount += amount
            session.total_sell_volume += volume
            session.current_investment = max(0, session.current_investment - amount)

        trade_data = {
            'session_id': session.id,
            'stock_code': stock_code,
            'trade_type': side,
            'grid_level': signal.get('grid_level'),
            'trigger_price': price,
            'volume': volume,
            'amount': amount,
            'peak_price': signal.get('peak_price'),
            'valley_price': signal.get('valley_price'),
            'callback_ratio': round(signal.get('callback_ratio'), 4) if signal.get('callback_ratio') else None,
            'trade_id': trade_id,
            'trade_time': datetime.now().isoformat(),
            'grid_center_before': session.current_center_price,
            'grid_center_after': price
        }

        updates = {
            'trade_count': session.trade_count,
            'current_investment': session.current_investment
        }
        if side == 'BUY':
            updates.update({
                'buy_count': session.buy_count,
                'total_buy_amount': session.total_buy_amount,
                'total_buy_volume': session.total_buy_volume
            })
        else:
            updates.update({
                'sell_count': session.sell_count,
                'total_sell_amount': session.total_sell_amount,
                'total_sell_volume': session.total_sell_volume
            })

        try:
            if hasattr(self.db, 'record_grid_trade_and_update_session'):
                self.db.record_grid_trade_and_update_session(
                    trade_data,
                    updates,
                    order_id=order_id,
                    order_updates=order_updates
                )
            else:
                self.db.record_grid_trade(trade_data)
                self.db.update_grid_session(session.id, updates)
                if order_id and order_updates and hasattr(self.db, 'update_grid_order'):
                    self.db.update_grid_order(order_id, order_updates)
        except Exception as db_err:
            logger.error(f"[GRID] confirmed trade DB写入失败，回滚内存统计: {db_err}")
            session.trade_count = old_trade_count
            session.buy_count = old_buy_count
            session.sell_count = old_sell_count
            session.total_buy_amount = old_total_buy
            session.total_sell_amount = old_total_sell
            session.total_buy_volume = old_total_buy_vol
            session.total_sell_volume = old_total_sell_vol
            session.current_investment = old_investment
            return False

        self._rebuild_grid(session, price)
        try:
            self.position_manager._increment_data_version()
        except Exception:
            pass

        logger.info(
            f"[GRID] confirmed {side}: stock_code={stock_code}, volume={volume}, "
            f"price={price:.2f}, amount={amount:.2f}, trade_id={trade_id}"
        )
        return True

    def handle_deal_callback(self, trade) -> bool:
        """实盘成交回调确认网格委托；只有真实成交后才更新网格统计和交易表"""
        order_id = self._get_attr_or_key(trade, ('order_id', 'm_strOrderID', 'order_sys_id'))
        if order_id is None:
            return False
        order_id = str(order_id)

        with self.lock:
            pending = self.pending_grid_orders.get(order_id)
            if not pending:
                return False

            price = self._get_attr_or_key(trade, ('traded_price', 'm_dPrice', 'price'))
            volume = self._get_attr_or_key(trade, ('traded_volume', 'm_nVolume', 'volume'))
            stock_code = self._get_attr_or_key(trade, ('stock_code', 'm_strInstrumentID'), pending['stock_code'])
            raw_trade_id = self._get_attr_or_key(trade, ('trade_id', 'traded_id', 'm_strTradeID'))

            try:
                price = float(price)
                volume = int(volume)
            except (TypeError, ValueError):
                logger.warning(f"[GRID] handle_deal_callback: 成交价格/数量无效 order_id={order_id}, price={price}, volume={volume}")
                return False
            if price <= 0 or volume <= 0:
                logger.warning(f"[GRID] handle_deal_callback: 成交价格/数量非正 order_id={order_id}, price={price}, volume={volume}")
                return False

            if self._normalize_code(str(stock_code)) != self._normalize_code(pending['stock_code']):
                logger.warning(
                    f"[GRID] handle_deal_callback: 股票代码不匹配 order_id={order_id}, "
                    f"callback={stock_code}, pending={pending['stock_code']}"
                )
                return False

            remaining = pending['requested_volume'] - pending.get('filled_volume', 0)
            if remaining <= 0:
                self.pending_grid_orders.pop(order_id, None)
                return False
            confirmed_volume = min(volume, remaining)

            trade_id = str(raw_trade_id) if raw_trade_id else f"{order_id}_{pending.get('filled_volume', 0) + confirmed_volume}"
            confirmed_trade_ids = pending.setdefault('confirmed_trade_ids', set())
            if trade_id in confirmed_trade_ids:
                logger.warning(f"[GRID] handle_deal_callback: 重复成交回报已忽略 trade_id={trade_id}, order_id={order_id}")
                return False
            if hasattr(self.db, 'grid_trade_exists') and self.db.grid_trade_exists(trade_id):
                logger.warning(f"[GRID] handle_deal_callback: 成交已在DB落账，忽略重复回报 trade_id={trade_id}, order_id={order_id}")
                confirmed_trade_ids.add(trade_id)
                return False

            session = (
                self.sessions.get(self._normalize_code(pending['stock_code']))
                or self.sessions.get(pending['stock_code'])
            )
            if not session or session.status not in ('active', 'stopping'):
                logger.warning(f"[GRID] handle_deal_callback: 会话不存在或非active order_id={order_id}, session_id={pending['session_id']}")
                return False

            new_filled_volume = pending.get('filled_volume', 0) + confirmed_volume
            new_filled_amount = pending.get('filled_amount', 0.0) + price * confirmed_volume
            order_status = 'filled' if new_filled_volume >= pending['requested_volume'] else 'partial_filled'

            success = self._record_confirmed_grid_trade_with_order(
                session=session,
                signal=pending['signal'],
                side=pending['side'],
                price=price,
                volume=confirmed_volume,
                trade_id=trade_id,
                order_id=order_id,
                order_updates={
                    'status': order_status,
                    'filled_volume': new_filled_volume,
                    'filled_amount': new_filled_amount
                }
            )
            if not success:
                return False

            pending['filled_volume'] = new_filled_volume
            pending['filled_amount'] = new_filled_amount
            confirmed_trade_ids.add(trade_id)

            if pending['filled_volume'] >= pending['requested_volume']:
                self.pending_grid_orders.pop(order_id, None)
                logger.info(f"[GRID] handle_deal_callback: 委托已全部成交并移除 pending order_id={order_id}")
            else:
                logger.info(
                    f"[GRID] handle_deal_callback: 委托部分成交 order_id={order_id}, "
                    f"filled={pending['filled_volume']}/{pending['requested_volume']}"
                )
            self._complete_stop_if_no_open_orders_unlocked(pending['session_id'])
            return True

    def handle_order_callback(self, order) -> bool:
        """处理网格委托状态回报，撤单/废单/拒单时清理 pending 委托"""
        order_id = self._get_attr_or_key(order, ('order_id', 'm_strOrderSysID', 'order_sys_id'))
        if order_id is None:
            return False
        order_id = str(order_id)

        status = self._get_attr_or_key(order, ('order_status', 'm_nOrderStatus', 'status'))
        try:
            status = int(status)
        except (TypeError, ValueError):
            return False

        terminal_status_map = {
            53: 'partially_canceled',
            54: 'canceled',
            57: 'rejected',
        }
        if status not in terminal_status_map:
            return False

        with self.lock:
            pending = self.pending_grid_orders.get(order_id)
            if not pending:
                if hasattr(self.db, 'get_grid_order') and self.db.get_grid_order(order_id):
                    try:
                        self.db.update_grid_order(order_id, {
                            'status': terminal_status_map[status],
                            'last_error': f'order terminal status {status}'
                        })
                    except Exception as db_err:
                        logger.warning(f"[GRID] handle_order_callback: 更新历史委托状态失败 order_id={order_id}, err={db_err}")
                    return True
                return False

            filled_volume = int(pending.get('filled_volume') or 0)
            requested_volume = int(pending.get('requested_volume') or 0)
            new_status = terminal_status_map[status]
            if status == 53 and filled_volume > 0:
                new_status = 'partial_filled_canceled'
            elif status == 54 and filled_volume >= requested_volume > 0:
                new_status = 'filled'

            try:
                if hasattr(self.db, 'update_grid_order'):
                    self.db.update_grid_order(order_id, {
                        'status': new_status,
                        'filled_volume': filled_volume,
                        'filled_amount': float(pending.get('filled_amount') or 0.0),
                        'last_error': f'order terminal status {status}'
                    })
            except Exception as db_err:
                logger.error(f"[GRID] handle_order_callback: 更新委托终态失败 order_id={order_id}, err={db_err}")
                return False

            self.pending_grid_orders.pop(order_id, None)
            tracker = self.trackers.get(pending.get('session_id'))
            if tracker:
                tracker.waiting_callback = False
                tracker.crossed_level = None
            self._complete_stop_if_no_open_orders_unlocked(pending.get('session_id'))
            logger.warning(
                f"[GRID] handle_order_callback: 委托终态已处理 order_id={order_id}, "
                f"status={status}, mapped={new_status}, filled={filled_volume}/{requested_volume}"
            )
            return True

    def _get_reserved_buy_amount_unlocked(self, session_id: int) -> float:
        """统计已提交但尚未落账的网格买入预算占用。"""
        reserved = 0.0
        for pending in self.pending_grid_orders.values():
            if pending.get('session_id') == session_id and pending.get('side') == 'BUY':
                remaining_volume = int(pending.get('requested_volume') or 0) - int(pending.get('filled_volume') or 0)
                if remaining_volume > 0:
                    reserved += remaining_volume * float(pending.get('expected_price') or 0)
        for plan in self.submitting_grid_orders.values():
            if plan.get('session_id') == session_id and plan.get('side') == 'BUY':
                reserved += int(plan.get('volume') or 0) * float(plan.get('expected_price') or 0)
        return reserved

    def _get_reserved_sell_volume_unlocked(self, session_id: int) -> int:
        """统计已提交但尚未终结的网格卖出数量，避免锁外下单窗口重复卖出。"""
        reserved = 0
        for pending in self.pending_grid_orders.values():
            if pending.get('session_id') == session_id and pending.get('side') == 'SELL':
                remaining_volume = int(pending.get('requested_volume') or 0) - int(pending.get('filled_volume') or 0)
                if remaining_volume > 0:
                    reserved += remaining_volume
        for plan in self.submitting_grid_orders.values():
            if plan.get('session_id') == session_id and plan.get('side') == 'SELL':
                reserved += int(plan.get('volume') or 0)
        return reserved

    def _create_submit_id(self, session_id: int, side: str) -> str:
        return f"{session_id}:{side}:{int(time.time() * 1000000)}"

    def _build_grid_order_plan(self, session: GridSession, signal: dict, position_snapshot=None) -> Optional[dict]:
        """锁内生成下单计划；不调用任何券商接口。"""
        stock_code = session.stock_code
        trigger_price = float(signal['trigger_price'])
        signal_type = signal['signal_type']

        if session.status != 'active':
            logger.warning(f"[GRID] _build_grid_order_plan: 会话非active, status={session.status}")
            return None

        if signal_type == 'BUY':
            if session.max_investment <= 0:
                logger.error(f"[GRID] _build_grid_order_plan: {stock_code} max_investment无效")
                return None

            buy_cooldown = getattr(config, 'GRID_BUY_COOLDOWN', 0)
            if buy_cooldown > 0:
                elapsed = time.time() - self.last_buy_times.get(session.id, 0)
                if elapsed < buy_cooldown:
                    logger.warning(f"[GRID] _build_grid_order_plan: {stock_code} 买入冷却中, 剩余{buy_cooldown - elapsed:.0f}秒")
                    return None

            reserved_amount = self._get_reserved_buy_amount_unlocked(session.id)
            effective_investment = session.current_investment + reserved_amount
            if effective_investment >= session.max_investment:
                logger.warning(
                    f"[GRID] _build_grid_order_plan: {stock_code} 达到最大投入限额 "
                    f"current={session.current_investment:.2f}, reserved={reserved_amount:.2f}, "
                    f"max={session.max_investment:.2f}"
                )
                return None

            remaining_investment = session.max_investment - effective_investment
            buy_amount = min(remaining_investment, session.max_investment * session.position_ratio)
            if buy_amount < 100:
                logger.warning(f"[GRID] _build_grid_order_plan: {stock_code} 可用买入金额{buy_amount:.2f}不足100元")
                return None

            volume = (int(buy_amount / trigger_price) // 100) * 100
            if volume < 100:
                logger.warning(f"[GRID] _build_grid_order_plan: {stock_code} 买入数量{volume}不足100股")
                return None

            expected_amount = volume * trigger_price
            if expected_amount > remaining_investment + 0.01:
                logger.error(
                    f"[GRID] _build_grid_order_plan: HARD CAP 阻止超买 amount={expected_amount:.4f}, "
                    f"remaining={remaining_investment:.4f}"
                )
                return None

            confirm_by_deal = (
                not getattr(config, 'ENABLE_SIMULATION_MODE', True)
                and getattr(config, 'GRID_CONFIRM_LIVE_ORDER_BY_DEAL', True)
            )
            use_counterparty = (
                getattr(config, 'GRID_USE_COUNTERPARTY_PRICE', True)
                and confirm_by_deal
            )
            return {
                'submit_id': self._create_submit_id(session.id, 'BUY'),
                'session_id': session.id,
                'stock_code': stock_code,
                'side': 'BUY',
                'signal': dict(signal),
                'volume': volume,
                'expected_price': trigger_price,
                'order_price': None if use_counterparty else trigger_price,
                'confirm_by_deal': confirm_by_deal
            }

        if signal_type == 'SELL':
            sell_cooldown = getattr(config, 'GRID_SELL_COOLDOWN', 0)
            if sell_cooldown > 0:
                last_sell = self.last_sell_times.get(session.id, 0)
                elapsed = time.time() - last_sell
                if elapsed < sell_cooldown:
                    price_threshold = getattr(config, 'GRID_SELL_COOLDOWN_PRICE_THRESHOLD', 0.02)
                    last_sell_price = self.last_sell_prices.get(session.id, 0)
                    adaptive_allowed = (
                        price_threshold > 0 and last_sell_price > 0
                        and trigger_price > last_sell_price * (1 + price_threshold)
                        and elapsed >= sell_cooldown // 2
                    )
                    if not adaptive_allowed:
                        logger.warning(f"[GRID] _build_grid_order_plan: {stock_code} 卖出冷却中")
                        return None

            position = position_snapshot if position_snapshot is not None else self.position_manager.get_position(stock_code)
            if not position:
                logger.error(f"[GRID] _build_grid_order_plan: {stock_code} 持仓不存在")
                return None
            current_volume = int(position.get('volume', 0) or 0)
            available_volume = int(position.get('available', current_volume) or 0)
            if current_volume <= 0 or available_volume <= 0:
                logger.warning(f"[GRID] _build_grid_order_plan: {stock_code} 无可卖持仓")
                return None

            reserved_sell = self._get_reserved_sell_volume_unlocked(session.id)
            effective_available = max(0, available_volume - reserved_sell)
            if effective_available <= 0:
                logger.warning(
                    f"[GRID] _build_grid_order_plan: {stock_code} 可卖数量已被未完成网格卖单占用 "
                    f"available={available_volume}, reserved={reserved_sell}"
                )
                return None

            sell_volume = (int(effective_available * session.position_ratio) // 100) * 100
            if sell_volume == 0:
                sell_volume = 100
            if sell_volume > effective_available:
                sell_volume = (int(effective_available) // 100) * 100
            if sell_volume <= 0:
                logger.warning(f"[GRID] _build_grid_order_plan: {stock_code} 可卖数量不足100股")
                return None

            confirm_by_deal = (
                not getattr(config, 'ENABLE_SIMULATION_MODE', True)
                and getattr(config, 'GRID_CONFIRM_LIVE_ORDER_BY_DEAL', True)
            )
            use_counterparty = (
                getattr(config, 'GRID_USE_COUNTERPARTY_PRICE', True)
                and confirm_by_deal
            )
            return {
                'submit_id': self._create_submit_id(session.id, 'SELL'),
                'session_id': session.id,
                'stock_code': stock_code,
                'side': 'SELL',
                'signal': dict(signal),
                'volume': sell_volume,
                'expected_price': trigger_price,
                'order_price': None if use_counterparty else trigger_price,
                'confirm_by_deal': confirm_by_deal
            }

        logger.error(f"[GRID] _build_grid_order_plan: 未知信号类型 {signal_type}")
        return None

    def _submit_grid_order_outside_lock(self, plan: dict):
        """锁外调用券商下单接口。"""
        if plan['side'] == 'BUY':
            return self.executor.buy_stock(
                stock_code=plan['stock_code'],
                volume=plan['volume'],
                price=plan['order_price'],
                strategy=config.GRID_STRATEGY_NAME
            )
        return self.executor.sell_stock(
            stock_code=plan['stock_code'],
            volume=plan['volume'],
            price=plan['order_price'],
            strategy=config.GRID_STRATEGY_NAME
        )

    def _mark_order_accepted_unlocked(self, session: GridSession, plan: dict, trade_id: str) -> bool:
        """券商已接受委托后，锁内登记 pending 或按旧模式直接落账。"""
        side = plan['side']
        if side == 'BUY':
            self.last_buy_times[session.id] = time.time()
        else:
            self.last_sell_times[session.id] = time.time()
            self.last_sell_prices[session.id] = plan['expected_price']

        if plan.get('confirm_by_deal'):
            self._register_pending_grid_order(
                order_id=trade_id,
                session=session,
                signal=plan['signal'],
                side=side,
                volume=plan['volume'],
                expected_price=plan['expected_price']
            )
            return True

        return self._record_confirmed_grid_trade(
            session=session,
            signal=plan['signal'],
            side=side,
            price=plan['expected_price'],
            volume=plan['volume'],
            trade_id=trade_id
        )

    def _reset_tracker_after_failed_trade_unlocked(self, session: GridSession, signal_type: str):
        """交易失败后重置追踪器，等待价格重新穿越。"""
        tracker = self.trackers.get(session.id)
        if tracker:
            tracker.waiting_callback = False
            tracker.crossed_level = None
            logger.info(
                f"[GRID] execute_grid_trade: 交易失败，重置追踪器 waiting_callback=False "
                f"stock_code={session.stock_code}, signal_type={signal_type}"
            )

    def execute_grid_trade(self, signal: dict) -> bool:
        """
        执行网格交易

        Args:
            signal: 网格交易信号

        Returns:
            执行是否成功
        """
        logger.info(f"[GRID] execute_grid_trade: 开始执行交易 signal={signal}")

        session_id = None
        signal_type = signal.get('signal_type', '')
        stock_code = signal.get('stock_code', '')
        try:
            # 锁外预取外部依赖，避免在网格全局锁内调用持仓/行情/QMT接口。
            position_snapshot = None
            if signal_type == 'SELL' and stock_code:
                position_snapshot = self.position_manager.get_position(stock_code)
                logger.debug(f"[GRID] execute_grid_trade: RISK-3预取持仓 stock_code={stock_code}, "
                             f"snapshot={'有持仓' if position_snapshot else '无持仓'}")
            latest_price = self._get_latest_price_for_signal(stock_code, position_snapshot=position_snapshot) if stock_code else None
            tradable_result = (True, "")
            if not config.ENABLE_SIMULATION_MODE and getattr(config, 'GRID_ENABLE_PRICE_LIMIT_GUARD', True):
                tradable_result = self._check_tradable(stock_code, signal_type, latest_price)

            with self.lock:
                stock_code = signal['stock_code']
                session = self.sessions.get(self._normalize_code(stock_code))
                if not session:
                    logger.error(f"[GRID] execute_grid_trade: 会话不存在: {stock_code}")
                    return False

                signal_type = signal['signal_type']
                session_id = session.id
                trigger_price = signal['trigger_price']
                logger.debug(f"[GRID] execute_grid_trade: session_id={session.id}, signal_type={signal_type}, trigger_price={trigger_price:.2f}")

                if not self._validate_grid_signal_before_execute(signal, session, latest_price=latest_price):
                    logger.warning(f"[GRID] execute_grid_trade: 信号复核失败，拒绝执行 stock_code={stock_code}, signal_type={signal_type}")
                    return False

                tradable, reason = tradable_result
                if not tradable:
                    logger.warning(f"[GRID] execute_grid_trade: 涨跌停/停牌防护拦截 "
                                   f"stock_code={stock_code}, signal_type={signal_type}: {reason}")
                    return False

                # 执行交易前的状态
                logger.debug(f"[GRID] execute_grid_trade: 交易前状态 trade_count={session.trade_count}, "
                            f"current_investment={session.current_investment:.2f}, profit_ratio={session.get_profit_ratio()*100:.2f}%")

                plan = self._build_grid_order_plan(session, signal, position_snapshot=position_snapshot)
                if not plan:
                    logger.warning(f"[GRID] execute_grid_trade: 生成下单计划失败 stock_code={stock_code}, signal_type={signal_type}")
                    self._reset_tracker_after_failed_trade_unlocked(session, signal_type)
                    return False

                self.submitting_grid_orders[plan['submit_id']] = plan

            # 真正下单发生在锁外，避免QMT卡顿阻塞网格状态机。
            if config.ENABLE_SIMULATION_MODE:
                trade_id = f"GRID_SIM_{plan['side']}_{int(time.time()*1000)}"
                result = trade_id
            else:
                result = self._submit_grid_order_outside_lock(plan)
                if not result:
                    logger.error(f"[GRID] execute_grid_trade: 实盘网格{plan['side']}下单失败: {plan['stock_code']}")
                    result = None
                trade_id = self._extract_order_id(result)

            cancel_after_accept = None
            with self.lock:
                current_plan = self.submitting_grid_orders.pop(plan['submit_id'], plan)
                session = self._find_session_by_id(plan['session_id'])
                if not session:
                    logger.warning(f"[GRID] execute_grid_trade: 下单返回后会话已不存在 submit_id={plan['submit_id']}")
                    return False

                if not result or (plan.get('confirm_by_deal') and not trade_id):
                    logger.warning(f"[GRID] execute_grid_trade: 交易执行失败 stock_code={stock_code}, signal_type={signal_type}")
                    self._reset_tracker_after_failed_trade_unlocked(session, signal_type)
                    self._complete_stop_if_no_open_orders_unlocked(session.id)
                    return False

                success = self._mark_order_accepted_unlocked(session, plan, trade_id)
                if not success:
                    logger.warning(f"[GRID] execute_grid_trade: 交易落账/登记失败 stock_code={stock_code}, signal_type={signal_type}")
                    self._reset_tracker_after_failed_trade_unlocked(session, signal_type)
                    self._complete_stop_if_no_open_orders_unlocked(session.id)
                    return False

                if session.status == 'stopping' or current_plan.get('stop_requested'):
                    pending = self.pending_grid_orders.get(str(trade_id))
                    if pending:
                        pending['stop_requested'] = True
                        pending['stop_reason'] = session.stop_reason or current_plan.get('stop_reason')
                        cancel_after_accept = str(trade_id)
                        try:
                            if hasattr(self.db, 'update_grid_order'):
                                self.db.update_grid_order(cancel_after_accept, {
                                    'status': 'cancel_requested',
                                    'last_error': f"stop requested: {pending.get('stop_reason')}"
                                })
                        except Exception as db_err:
                            logger.warning(f"[GRID] execute_grid_trade: 下单后标记撤单请求失败 order_id={trade_id}, err={db_err}")

                cooldown_level = signal.get('grid_level')
                if cooldown_level is not None:
                    cooldown_key = (session.id, cooldown_level)
                    self.level_cooldowns[cooldown_key] = time.time()
                    logger.debug(f"[GRID] execute_grid_trade: 设置档位冷却 session_id={session.id}, "
                                f"level={cooldown_level:.2f} (触发档位价格), "
                                f"signal_type={signal_type}")
                else:
                    logger.warning(f"[GRID] execute_grid_trade: signal 中无 grid_level，跳过冷却设置")

                # 执行交易后的状态
                logger.debug(f"[GRID] execute_grid_trade: 交易后状态 trade_count={session.trade_count}, "
                            f"current_investment={session.current_investment:.2f}, profit_ratio={session.get_profit_ratio()*100:.2f}%")

                # 触发数据版本更新
                self.position_manager._increment_data_version()

                logger.info(f"[GRID] execute_grid_trade: 交易执行成功 stock_code={stock_code}, signal_type={signal_type}")

                if not self.pending_grid_orders.get(str(trade_id)):
                    self._complete_stop_if_no_open_orders_unlocked(session.id)

            if cancel_after_accept:
                if not self._cancel_grid_order(cancel_after_accept):
                    with self.lock:
                        try:
                            if hasattr(self.db, 'update_grid_order'):
                                self.db.update_grid_order(cancel_after_accept, {
                                    'status': 'cancel_failed',
                                    'last_error': 'cancel failed after stop requested'
                                })
                        except Exception as db_err:
                            logger.warning(f"[GRID] execute_grid_trade: 写入撤单失败状态失败 order_id={cancel_after_accept}, err={db_err}")
            return True

        except Exception as e:
            logger.error(f"[GRID] execute_grid_trade: 执行网格交易失败: {str(e)}", exc_info=True)
            # Gap 1修复：异常路径同样重置追踪器，防止与 success=False 路径不一致。
            # 若不重置，任何 DB/网络异常都会导致 tracker.waiting_callback 留在 True，
            # 重现无限重试死循环。
            try:
                if 'plan' in locals() and isinstance(plan, dict) and plan.get('submit_id'):
                    with self.lock:
                        self.submitting_grid_orders.pop(plan['submit_id'], None)
                stock_code_for_reset = signal.get('stock_code', '')
                if stock_code_for_reset:
                    with self.lock:
                        session_for_reset = self.sessions.get(self._normalize_code(stock_code_for_reset))
                        if session_for_reset:
                            self._reset_tracker_after_failed_trade_unlocked(session_for_reset, signal.get('signal_type', ''))
                            self._complete_stop_if_no_open_orders_unlocked(session_for_reset.id)
            except Exception as reset_err:
                logger.warning(f"[GRID] execute_grid_trade: 异常路径重置追踪器失败(可忽略): {reset_err}")
            return False

    def _execute_grid_buy(self, session: GridSession, signal: dict) -> bool:
        """执行网格买入"""
        stock_code = session.stock_code
        trigger_price = signal['trigger_price']
        logger.info(f"[GRID] _execute_grid_buy: 开始执行 stock_code={stock_code}, trigger_price={trigger_price:.2f}")

        # 0. 检查 max_investment 有效性
        if session.max_investment <= 0:
            logger.error(f"[GRID] _execute_grid_buy: {stock_code} max_investment={session.max_investment} 无效，无法执行买入")
            return False

        # 0.5 检查成功买入冷却时间 (GRID_BUY_COOLDOWN)
        # 防止9:25开盘后价格已低于下轨时，短时间内级联触发多次买入
        buy_cooldown = getattr(config, 'GRID_BUY_COOLDOWN', 0)
        if buy_cooldown > 0:
            last_buy = self.last_buy_times.get(session.id, 0)
            elapsed = time.time() - last_buy
            if elapsed < buy_cooldown:
                logger.warning(f"[GRID] _execute_grid_buy: {stock_code} 买入冷却中 "
                               f"(剩余{buy_cooldown - elapsed:.0f}秒), 跳过买入")
                return False

        # 1. 检查投入限额
        logger.debug(f"[GRID] _execute_grid_buy: 检查投入限额 current_investment={session.current_investment:.2f}, max_investment={session.max_investment:.2f}")
        if session.current_investment >= session.max_investment:
            logger.warning(f"[GRID] _execute_grid_buy: {stock_code} 达到最大投入限额{session.max_investment:.2f}, 跳过买入")
            return False

        # 2. 计算买入金额和数量
        remaining_investment = session.max_investment - session.current_investment
        # 单次买入金额 = min(剩余额度, 总额度 × position_ratio)
        # T-GAP-8/A-1修复：买入与卖出使用同一个 position_ratio 字段，语义统一为"每档交易比例"。
        # 原设计注释中说"买入固定用 max_investment×20%"是历史遗留，现改为以 position_ratio 为准：
        # - 用户可通过调整 position_ratio 同时控制买入每档额度和卖出每档数量。
        # - 默认 position_ratio=0.25 即单次买入不超过总额度 25%（最多 4 档），与原 20% 逻辑类似但更灵活。
        target_buy_amount = session.max_investment * session.position_ratio
        buy_amount = min(remaining_investment, target_buy_amount)
        logger.debug(f"[GRID] _execute_grid_buy: remaining_investment={remaining_investment:.2f}, "
                    f"target_buy_amount(position_ratio={session.position_ratio*100:.0f}%)={target_buy_amount:.2f}, buy_amount={buy_amount:.2f}")

        if buy_amount < 100:  # 最小买入金额
            logger.warning(f"[GRID] _execute_grid_buy: {stock_code} 可用买入金额{buy_amount:.2f}不足100元, 跳过买入")
            return False

        # 计算股数
        raw_volume = buy_amount / trigger_price  # 原始股数

        # 计算股数 (统一要求100股倍数)
        volume = (int(raw_volume) // 100) * 100
        min_volume = 100

        logger.debug(f"[GRID] _execute_grid_buy: 计算买入数量 raw_volume={raw_volume:.2f}, volume={volume}, min_volume={min_volume}")

        if volume < min_volume:
            logger.warning(f"[GRID] _execute_grid_buy: {stock_code} 买入数量{volume}不足{min_volume}股(原始={raw_volume:.2f}股), 跳过")
            return False

        # 3. 执行买入
        actual_amount = volume * trigger_price
        logger.debug(f"[GRID] _execute_grid_buy: 执行买入 volume={volume}, actual_amount={actual_amount:.2f}")

        # ── 硬上限校验 V3（防御性兜底，防浮点误差/逻辑bug） ──────────────────────────
        # 无论 buy_amount/remaining 计算链路是否有误，此处确保 actual_amount 不超过剩余额度。
        # 允许1分钱浮点误差容忍（0.01元），超出则中止本次买入。
        remaining_strict = session.max_investment - session.current_investment
        if actual_amount > remaining_strict + 0.01:
            logger.error(
                f"[GRID] _execute_grid_buy: HARD CAP 阻止超买 "
                f"stock_code={stock_code}, actual_amount={actual_amount:.4f} > "
                f"remaining={remaining_strict:.4f} (current={session.current_investment:.4f}, "
                f"max={session.max_investment:.4f})"
            )
            return False

        if config.ENABLE_SIMULATION_MODE:
            trade_id = f"GRID_SIM_BUY_{int(time.time()*1000)}"
            logger.info(f"[GRID] _execute_grid_buy: [模拟]网格买入: {stock_code}, 数量={volume}, 价格={trigger_price:.2f}, trade_id={trade_id}")
        else:
            # ── V1 修复：明确传入 volume+price，避免 executor 用市价重算量 ──────────────
            # 若只传 amount，executor 会用实时市价重算股数，当计算量≤0时强制设100股，
            # 可能导致实际下单金额远超 actual_amount（例如剩余50元却下了100股×市价的单）。
            # 解决方案：直接传 volume，价格按 order_price 下限价单。
            #
            # 对手价模式(GRID_USE_COUNTERPARTY_PRICE)：price=None 时 executor 自动取卖三价，
            # 提高成交概率(参考动态止盈下单逻辑)。仅在成交确认模式下启用——落账以真实成交价
            # 为准(handle_deal_callback)，volume 与硬上限以 trigger_price 估算、deal 回报修正。
            use_counterparty = (
                getattr(config, 'GRID_USE_COUNTERPARTY_PRICE', True)
                and getattr(config, 'GRID_CONFIRM_LIVE_ORDER_BY_DEAL', True)
            )
            order_price = None if use_counterparty else trigger_price
            logger.debug(f"[GRID] _execute_grid_buy: 调用executor.buy_stock 实盘买入 "
                         f"volume={volume}, price={'卖三价(对手价)' if order_price is None else f'{order_price:.2f}'}")
            result = self.executor.buy_stock(
                stock_code=stock_code,
                volume=volume,
                price=order_price,
                strategy=config.GRID_STRATEGY_NAME
            )
            if not result:
                logger.error(f"[GRID] _execute_grid_buy: 实盘网格买入失败: {stock_code}")
                return False
            trade_id = self._extract_order_id(result)
            logger.info(f"[GRID] _execute_grid_buy: 实盘网格买入成功: {stock_code}, trade_id={trade_id}")

            if getattr(config, 'GRID_CONFIRM_LIVE_ORDER_BY_DEAL', True):
                if not trade_id:
                    logger.error(f"[GRID] _execute_grid_buy: 实盘委托成功但缺少order_id，无法等待成交确认: {stock_code}")
                    return False
                self.last_buy_times[session.id] = time.time()
                logger.debug(f"[GRID] _execute_grid_buy: 实盘委托已登记冷却 last_buy_times[{session.id}]")
                self._register_pending_grid_order(
                    order_id=trade_id,
                    session=session,
                    signal=signal,
                    side='BUY',
                    volume=volume,
                    expected_price=trigger_price
                )
                logger.info(
                    f"[GRID] _execute_grid_buy: 实盘网格买入委托已提交，等待成交回调确认 "
                    f"stock_code={stock_code}, order_id={trade_id}, volume={volume}, price={trigger_price:.2f}"
                )
                return True

        # BUG-C1修复: 下单成功后立即记录冷却时间，防止DB写入失败时重复下单。
        # 背景: 原逻辑将 last_buy_times 更新置于 DB 写入成功之后（函数末尾）。
        # 若实盘模式下 QMT 已接受委托但后续 DB 写入抛出异常，except 块会回滚
        # 内存统计（current_investment 等），导致下一个 tick 重新检测到买入信号并
        # 再次下单，形成重复委托。
        # 修复方案: 将时间戳记录提前到订单确认后、DB 操作之前。即使 DB 失败，
        # GRID_BUY_COOLDOWN 保护依然有效，阻止在冷却期内重新触发买入。
        self.last_buy_times[session.id] = time.time()
        logger.debug(f"[GRID] _execute_grid_buy: BUG-C1修复 last_buy_times[{session.id}]已记录(DB写入前)")

        success = self._record_confirmed_grid_trade(
            session=session,
            signal=signal,
            side='BUY',
            price=trigger_price,
            volume=volume,
            trade_id=trade_id
        )
        if success:
            logger.info(f"[GRID] _execute_grid_buy: 网格买入成功! stock_code={stock_code}, volume={volume}, amount={actual_amount:.2f}, "
                       f"investment={session.current_investment:.2f}/{session.max_investment:.2f}, trade_id={trade_id}")
        return success

    def _execute_grid_sell(self, session: GridSession, signal: dict, position_snapshot=None) -> bool:
        """执行网格卖出

        Args:
            session: 网格会话
            signal: 交易信号
            position_snapshot: 持仓快照（由调用方在锁外预取，用于 RISK-3 死锁预防）；
                               若为 None 则在内部获取（兼容直接调用场景）。
        """
        stock_code = session.stock_code
        trigger_price = signal['trigger_price']
        logger.info(f"[GRID] _execute_grid_sell: 开始执行 stock_code={stock_code}, trigger_price={trigger_price:.2f}")

        # 0.5 检查成功卖出冷却时间 (GRID_SELL_COOLDOWN) - 对称于买入冷却 BUG-C1/A-4修复
        # 防止价格在上轨附近震荡时短时间内级联触发多次卖出
        sell_cooldown = getattr(config, 'GRID_SELL_COOLDOWN', 0)
        if sell_cooldown > 0:
            last_sell = self.last_sell_times.get(session.id, 0)
            elapsed = time.time() - last_sell
            if elapsed < sell_cooldown:
                # 自适应缩短：单边上涨行情中触发价明显高于上次成交价时，冷却期减半
                price_threshold = getattr(config, 'GRID_SELL_COOLDOWN_PRICE_THRESHOLD', 0.02)
                last_sell_price = self.last_sell_prices.get(session.id, 0)
                if (price_threshold > 0 and last_sell_price > 0
                        and trigger_price > last_sell_price * (1 + price_threshold)):
                    effective_cooldown = sell_cooldown // 2
                    if elapsed < effective_cooldown:
                        logger.warning(
                            f"[GRID] _execute_grid_sell: {stock_code} 卖出冷却中（自适应缩短至{effective_cooldown}秒）"
                            f" 剩余{effective_cooldown - elapsed:.0f}秒, "
                            f"触发价={trigger_price:.2f} 上次={last_sell_price:.2f} "
                            f"涨幅={(trigger_price/last_sell_price - 1)*100:.1f}%")
                        return False
                    else:
                        logger.info(
                            f"[GRID] _execute_grid_sell: {stock_code} 自适应冷却缩短生效 "
                            f"原{sell_cooldown}s→{effective_cooldown}s, "
                            f"触发价={trigger_price:.2f} 上次={last_sell_price:.2f} "
                            f"涨幅={(trigger_price/last_sell_price - 1)*100:.1f}%, "
                            f"已过{elapsed:.0f}s≥{effective_cooldown}s, 允许执行")
                else:
                    logger.warning(f"[GRID] _execute_grid_sell: {stock_code} 卖出冷却中 "
                                   f"(剩余{sell_cooldown - elapsed:.0f}秒), 跳过卖出")
                    return False

        # 1. 获取当前持仓（优先使用调用方预取的快照，避免在持有 self.lock 时再次获取锁）
        position = position_snapshot if position_snapshot is not None else self.position_manager.get_position(stock_code)
        if not position:
            logger.error(f"[GRID] _execute_grid_sell: {stock_code} 持仓不存在")
            return False

        current_volume = position.get('volume', 0)
        # A-3修复：T+1 规则 - 当日买入的股份 available=0，不可当日卖出。
        # 使用 available（可卖数量）而非 volume（总持仓）作为卖出上限，防止向 QMT 提交无效委托。
        # 若持仓字典中无 available 字段（如部分 mock 或旧版快照），则退化为 current_volume（向后兼容）。
        available_volume = position.get('available', current_volume)
        cost_price = position.get('cost_price', trigger_price)
        logger.debug(f"[GRID] _execute_grid_sell: 当前持仓 volume={current_volume}, "
                     f"available={available_volume}, cost_price={cost_price:.2f}")

        if current_volume == 0:
            logger.warning(f"[GRID] _execute_grid_sell: {stock_code} 持仓为0, 跳过卖出")
            return False

        if available_volume == 0:
            logger.warning(f"[GRID] _execute_grid_sell: {stock_code} 可卖数量为0"
                           f"（T+1限制：今日买入的{current_volume}股无法当日卖出）, 跳过卖出")
            return False

        # 2. 计算卖出数量
        # position_ratio 字段控制每次卖出可卖持仓的比例（买入使用相同字段，语义统一）。
        # A-3修复：基于 available_volume（可卖数量）而非 current_volume（总持仓），
        # 遵守 T+1 规则，避免包含当日买入股份导致无效委托。
        # BUG-1修复：改用 (int(x) // 100) * 100 形式，语义更清晰：
        # 先计算应卖股数（浮点转整数截断），再向下取整到100的倍数
        # 与买入逻辑的整百方式统一，两者数值等价但表达一致
        sell_volume = (int(available_volume * session.position_ratio) // 100) * 100
        logger.debug(f"[GRID] _execute_grid_sell: 计算卖出数量 position_ratio={session.position_ratio*100:.1f}%, "
                     f"available_volume={available_volume}, 初步sell_volume={sell_volume}")

        if sell_volume == 0:
            sell_volume = 100  # 最少卖100股
            logger.debug(f"[GRID] _execute_grid_sell: 卖出数量为0, 调整为最小值100")

        if sell_volume > available_volume:
            sell_volume = int(available_volume / 100) * 100
            logger.debug(f"[GRID] _execute_grid_sell: 卖出数量超过可卖持仓(T+1可卖={available_volume}), 调整为{sell_volume}")

        if sell_volume == 0:
            logger.warning(f"[GRID] _execute_grid_sell: {stock_code} 可卖数量不足100股, 跳过")
            return False

        # 3. 执行卖出
        sell_amount = sell_volume * trigger_price
        logger.debug(f"[GRID] _execute_grid_sell: 执行卖出 sell_volume={sell_volume}, sell_amount={sell_amount:.2f}")

        if config.ENABLE_SIMULATION_MODE:
            trade_id = f"GRID_SIM_SELL_{int(time.time()*1000)}"
            logger.info(f"[GRID] _execute_grid_sell: [模拟]网格卖出: {stock_code}, 数量={sell_volume}, 价格={trigger_price:.2f}, trade_id={trade_id}")
        else:
            # V1-SELL修复: 卖出明确传入 volume，避免 executor 用市价重算量。
            #
            # 对手价模式(GRID_USE_COUNTERPARTY_PRICE)：price=None 时 executor 自动取买三价，
            # 提高成交概率(参考动态止盈下单逻辑)。仅在成交确认模式下启用——落账以真实成交价
            # 为准(handle_deal_callback)，current_investment 回收额按 deal 回报精确统计，不失真。
            # 非确认模式下回退 trigger_price 限价，保持 V1-SELL 统计一致性。
            use_counterparty = (
                getattr(config, 'GRID_USE_COUNTERPARTY_PRICE', True)
                and getattr(config, 'GRID_CONFIRM_LIVE_ORDER_BY_DEAL', True)
            )
            order_price = None if use_counterparty else trigger_price
            logger.debug(f"[GRID] _execute_grid_sell: 调用executor.sell_stock 实盘卖出 "
                         f"volume={sell_volume}, price={'买三价(对手价)' if order_price is None else f'{order_price:.2f}'}")
            result = self.executor.sell_stock(
                stock_code=stock_code,
                volume=sell_volume,
                price=order_price,
                strategy=config.GRID_STRATEGY_NAME
            )
            if not result:
                logger.error(f"[GRID] _execute_grid_sell: 实盘网格卖出失败: {stock_code}")
                return False
            trade_id = self._extract_order_id(result)
            logger.info(f"[GRID] _execute_grid_sell: 实盘网格卖出成功: {stock_code}, trade_id={trade_id}")

            if getattr(config, 'GRID_CONFIRM_LIVE_ORDER_BY_DEAL', True):
                if not trade_id:
                    logger.error(f"[GRID] _execute_grid_sell: 实盘委托成功但缺少order_id，无法等待成交确认: {stock_code}")
                    return False
                self.last_sell_times[session.id] = time.time()
                self.last_sell_prices[session.id] = trigger_price
                logger.debug(f"[GRID] _execute_grid_sell: 实盘委托已登记冷却 last_sell_times[{session.id}]")
                self._register_pending_grid_order(
                    order_id=trade_id,
                    session=session,
                    signal=signal,
                    side='SELL',
                    volume=sell_volume,
                    expected_price=trigger_price
                )
                logger.info(
                    f"[GRID] _execute_grid_sell: 实盘网格卖出委托已提交，等待成交回调确认 "
                    f"stock_code={stock_code}, order_id={trade_id}, volume={sell_volume}, price={trigger_price:.2f}"
                )
                return True

        # A-4修复（BUG-C1对称）: 下单成功后立即记录卖出冷却时间，防止DB写入失败时重复下单。
        # 与买入 BUG-C1 修复完全对称：即使后续 DB 操作抛出异常并回滚内存统计，
        # GRID_SELL_COOLDOWN 保护依然有效，阻止在冷却期内再次触发卖出。
        self.last_sell_times[session.id] = time.time()
        self.last_sell_prices[session.id] = trigger_price  # 记录触发价，供自适应冷却缩短使用
        logger.debug(f"[GRID] _execute_grid_sell: A-4修复 last_sell_times[{session.id}]已记录(DB写入前)")

        success = self._record_confirmed_grid_trade(
            session=session,
            signal=signal,
            side='SELL',
            price=trigger_price,
            volume=sell_volume,
            trade_id=trade_id
        )
        if success:
            profit = session.get_profit_ratio()
            logger.info(f"[GRID] _execute_grid_sell: 网格卖出成功! stock_code={stock_code}, volume={sell_volume}, amount={sell_amount:.2f}, "
                       f"profit={profit*100:.2f}%, trade_id={trade_id}")
        return success

    def get_session_stats(self, session_id: int) -> dict:
        """获取会话统计信息"""
        session = None
        for s in self.sessions.values():
            if s.id == session_id:
                session = s
                break

        if not session:
            return {}

        ledger_summary = None
        if hasattr(self.db, 'get_grid_ledger_summary'):
            try:
                ledger_summary = self.db.get_grid_ledger_summary(
                    session.id,
                    session.current_center_price or session.center_price
                )
            except Exception as e:
                logger.debug(f"[GRID] get_session_stats: 获取账本摘要失败 session_id={session_id}, err={e}")

        return {
            'session_id': session.id,
            'stock_code': session.stock_code,
            'status': session.status,
            'center_price': session.center_price,
            'current_center_price': session.current_center_price,
            'grid_levels': session.get_grid_levels(),
            'trade_count': session.trade_count,
            'buy_count': session.buy_count,
            'sell_count': session.sell_count,
            'profit_ratio': session.get_profit_ratio(),
            'grid_profit': session.get_grid_profit(),
            'deviation_ratio': session.get_deviation_ratio(),
            'current_investment': session.current_investment,
            'max_investment': session.max_investment,
            'ledger_summary': ledger_summary,
            'start_time': session.start_time.isoformat() if session.start_time else None,
            'end_time': session.end_time.isoformat() if session.end_time else None
        }

    def get_trade_history(self, session_id: int, limit=50, offset=0) -> list:
        """获取交易历史"""
        return self.db.get_grid_trades(session_id, limit, offset)




