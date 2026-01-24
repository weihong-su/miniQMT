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

    # 时间戳
    start_time: Optional[datetime] = None
    stop_time: Optional[datetime] = None
    stop_reason: Optional[str] = None

    def get_profit_ratio(self) -> float:
        """计算网格盈亏率"""
        if self.total_buy_amount == 0:
            return 0.0
        return (self.total_sell_amount - self.total_buy_amount) / self.total_buy_amount

    def get_deviation_ratio(self) -> float:
        """计算当前偏离度"""
        if self.center_price == 0 or self.current_center_price == 0:
            return 0.0
        return abs(self.current_center_price - self.center_price) / self.center_price

    def get_grid_levels(self) -> dict:
        """生成当前网格档位"""
        center = self.current_center_price or self.center_price
        return {
            'lower': center * (1 - self.price_interval),
            'center': center,
            'upper': center * (1 + self.price_interval)
        }


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

        if self.waiting_callback:
            if self.direction == 'rising' and new_price > self.peak_price:
                self.peak_price = new_price
            elif self.direction == 'falling' and new_price < self.valley_price:
                self.valley_price = new_price

    def check_callback(self, callback_ratio: float) -> Optional[str]:
        """检查是否触发回调,返回信号类型"""
        if not self.waiting_callback:
            return None

        if self.direction == 'rising':
            ratio = (self.peak_price - self.last_price) / self.peak_price
            if ratio >= callback_ratio:
                return 'SELL'

        elif self.direction == 'falling':
            ratio = (self.last_price - self.valley_price) / self.valley_price
            if ratio >= callback_ratio:
                return 'BUY'

        return None

    def reset(self, price: float):
        """重置追踪器"""
        self.last_price = price
        self.peak_price = price
        self.valley_price = price
        self.direction = None
        self.crossed_level = None
        self.waiting_callback = False


class GridTradingManager:
    """网格交易管理器"""

    def __init__(self, db_manager, position_manager, trading_executor):
        self.db = db_manager
        self.position_manager = position_manager
        self.executor = trading_executor

        # 内存缓存
        self.sessions: Dict[str, GridSession] = {}
        self.trackers: Dict[int, PriceTracker] = {}
        self.level_cooldowns: Dict[tuple, float] = {}
        self.lock = threading.Lock()

        # 初始化:从数据库加载活跃会话
        self._load_active_sessions()

    def _load_active_sessions(self):
        """系统启动时从数据库加载活跃会话(保守恢复策略)"""
        logger.info("=" * 60)
        logger.info("系统重启 - 开始恢复网格交易会话")
        logger.info("=" * 60)

        active_sessions = self.db.get_active_grid_sessions()
        recovered_count = 0
        stopped_count = 0

        for session_data in active_sessions:
            stock_code = session_data['stock_code']
            session_id = session_data['id']

            # 1. 检查会话是否已过期
            end_time = datetime.fromisoformat(session_data['end_time'])
            if datetime.now() > end_time:
                self.db.stop_grid_session(session_id, 'expired')
                logger.info(f"会话{session_id}({stock_code})已过期,自动停止")
                stopped_count += 1
                continue

            # 2. 检查持仓是否还存在
            position = self.position_manager.get_position(stock_code)
            if not position or position.get('volume', 0) == 0:
                self.db.stop_grid_session(session_id, 'position_cleared')
                logger.info(f"会话{session_id}({stock_code})持仓已清空,自动停止")
                stopped_count += 1
                continue

            # 3. 恢复GridSession对象
            session = GridSession(
                id=session_data['id'],
                stock_code=session_data['stock_code'],
                status=session_data['status'],
                center_price=session_data['center_price'],
                current_center_price=session_data['current_center_price'],
                price_interval=session_data['price_interval'],
                position_ratio=session_data['position_ratio'],
                callback_ratio=session_data['callback_ratio'],
                max_investment=session_data['max_investment'],
                current_investment=session_data['current_investment'],
                max_deviation=session_data['max_deviation'],
                target_profit=session_data['target_profit'],
                stop_loss=session_data['stop_loss'],
                trade_count=session_data['trade_count'],
                buy_count=session_data['buy_count'],
                sell_count=session_data['sell_count'],
                total_buy_amount=session_data['total_buy_amount'],
                total_sell_amount=session_data['total_sell_amount'],
                start_time=datetime.fromisoformat(session_data['start_time']),
                end_time=end_time
            )
            self.sessions[stock_code] = session

            # 4. 重置PriceTracker(保守策略)
            current_price = position.get('current_price', session.current_center_price)
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
            for key in cooldown_keys:
                del self.level_cooldowns[key]

            # 6. 记录恢复信息
            logger.info(f"恢复会话: {stock_code}")
            logger.info(f"  - 会话ID: {session_id}")
            logger.info(f"  - 原始中心价: {session.center_price:.2f}元(锁定)")
            logger.info(f"  - 当前中心价: {session.current_center_price:.2f}元")
            logger.info(f"  - 当前市价: {current_price:.2f}元")
            logger.info(f"  - 累计交易: {session.trade_count}次(买{session.buy_count}/卖{session.sell_count})")
            logger.info(f"  - 网格盈亏: {session.get_profit_ratio()*100:.2f}%")
            logger.info(f"  - 追踪器状态: 已重置(安全模式)")

            levels = session.get_grid_levels()
            logger.info(f"  - 网格档位: {levels['lower']:.2f} / {levels['center']:.2f} / {levels['upper']:.2f}")

            remaining_days = (end_time - datetime.now()).days
            logger.info(f"  - 剩余时长: {remaining_days}天")

            recovered_count += 1

        logger.info("=" * 60)
        logger.info(f"网格会话恢复完成: 恢复{recovered_count}个, 自动停止{stopped_count}个")
        logger.info("=" * 60)

        return recovered_count

    def start_grid_session(self, stock_code: str, user_config: dict) -> GridSession:
        """启动网格交易会话"""
        with self.lock:
            # 1. 验证前置条件
            if stock_code in self.sessions:
                raise ValueError(f"{stock_code}已有活跃的网格会话")

            position = self.position_manager.get_position(stock_code)
            if not position:
                raise ValueError(f"未持有{stock_code}")

            if not position.get('profit_triggered'):
                raise ValueError(f"{stock_code}未触发止盈,无法启动网格交易")

            # 2. 获取highest_price作为center_price
            highest_price = position.get('highest_price', 0)
            if highest_price == 0:
                raise ValueError(f"{stock_code}缺少最高价数据")

            # 3. 创建GridSession对象
            start_time = datetime.now()
            end_time = start_time + timedelta(days=user_config.get('duration_days', 7))

            session_data = {
                'stock_code': stock_code,
                'center_price': highest_price,
                'price_interval': user_config.get('price_interval', config.GRID_DEFAULT_PRICE_INTERVAL),
                'position_ratio': user_config.get('position_ratio', config.GRID_DEFAULT_POSITION_RATIO),
                'callback_ratio': user_config.get('callback_ratio', config.GRID_CALLBACK_RATIO),
                'max_investment': user_config.get('max_investment', 0),
                'max_deviation': user_config.get('max_deviation', config.GRID_MAX_DEVIATION_RATIO),
                'target_profit': user_config.get('target_profit', config.GRID_TARGET_PROFIT_RATIO),
                'stop_loss': user_config.get('stop_loss', config.GRID_STOP_LOSS_RATIO),
                'start_time': start_time.isoformat(),
                'end_time': end_time.isoformat()
            }

            # 4. 持久化到数据库
            session_id = self.db.create_grid_session(session_data)

            # 5. 创建内存对象
            session = GridSession(
                id=session_id,
                stock_code=stock_code,
                status='active',
                center_price=highest_price,
                current_center_price=highest_price,
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
            self.sessions[stock_code] = session

            # 6. 初始化PriceTracker
            current_price = position.get('current_price', highest_price)
            self.trackers[session_id] = PriceTracker(
                session_id=session_id,
                last_price=current_price,
                peak_price=current_price,
                valley_price=current_price
            )

            # 7. 触发数据版本更新
            self.position_manager._increment_data_version()

            logger.info(f"启动网格交易: {stock_code}, 中心价={highest_price:.2f}, "
                       f"档位间隔={session.price_interval*100:.1f}%")

            return session

    def stop_grid_session(self, session_id: int, reason: str) -> dict:
        """停止网格交易会话"""
        with self.lock:
            # 查找会话
            session = None
            for s in self.sessions.values():
                if s.id == session_id:
                    session = s
                    break

            if not session:
                raise ValueError(f"会话{session_id}不存在")

            # 更新数据库
            self.db.stop_grid_session(session_id, reason)

            # 从内存中移除
            stock_code = session.stock_code
            if stock_code in self.sessions:
                del self.sessions[stock_code]
            if session_id in self.trackers:
                del self.trackers[session_id]

            # 清除冷却记录
            cooldown_keys = [k for k in self.level_cooldowns.keys() if k[0] == session_id]
            for key in cooldown_keys:
                del self.level_cooldowns[key]

            # 触发数据版本更新
            self.position_manager._increment_data_version()

            final_stats = {
                'stock_code': stock_code,
                'trade_count': session.trade_count,
                'profit_ratio': session.get_profit_ratio(),
                'stop_reason': reason
            }

            logger.info(f"停止网格交易: {stock_code}, 原因={reason}, "
                       f"交易{session.trade_count}次, 盈亏{session.get_profit_ratio()*100:.2f}%")

            return final_stats

    def _check_exit_conditions(self, session: GridSession, current_price: float) -> Optional[str]:
        """检查退出条件,返回退出原因或None"""

        # 1. 偏离度检测
        if session.current_center_price and session.center_price:
            deviation = session.get_deviation_ratio()
            if deviation > session.max_deviation:
                logger.warning(f"{session.stock_code} 偏离度{deviation*100:.2f}%超过限制")
                return 'deviation'

        # 2. 盈亏检测
        if session.total_buy_amount > 0:
            profit_ratio = session.get_profit_ratio()
            if profit_ratio >= session.target_profit:
                logger.info(f"{session.stock_code} 达到目标盈利{profit_ratio*100:.2f}%")
                return 'target_profit'
            if profit_ratio <= session.stop_loss:
                logger.warning(f"{session.stock_code} 触发止损{profit_ratio*100:.2f}%")
                return 'stop_loss'

        # 3. 时间限制
        if session.end_time and datetime.now() > session.end_time:
            logger.info(f"{session.stock_code} 达到运行时长限制")
            return 'expired'

        # 4. 持仓清空
        position = self.position_manager.get_position(session.stock_code)
        if not position or position.get('volume', 0) == 0:
            logger.info(f"{session.stock_code} 持仓已清空")
            return 'position_cleared'

        return None

    def _check_level_crossing(self, session: GridSession, tracker: PriceTracker, price: float):
        """检查是否穿越档位"""
        levels = session.get_grid_levels()

        # 检查上穿(卖出档位)
        if price > levels['upper'] and not tracker.waiting_callback:
            # 检查冷却
            if self._is_level_in_cooldown(session.id, levels['upper']):
                return

            tracker.crossed_level = levels['upper']
            tracker.peak_price = price
            tracker.direction = 'rising'
            tracker.waiting_callback = True

            if config.GRID_LOG_LEVEL == "DEBUG":
                logger.debug(f"{session.stock_code} 穿越卖出档位{levels['upper']:.2f}, "
                            f"等待回调{session.callback_ratio*100:.2f}%")

        # 检查下穿(买入档位)
        elif price < levels['lower'] and not tracker.waiting_callback:
            # 检查冷却
            if self._is_level_in_cooldown(session.id, levels['lower']):
                return

            tracker.crossed_level = levels['lower']
            tracker.valley_price = price
            tracker.direction = 'falling'
            tracker.waiting_callback = True

            if config.GRID_LOG_LEVEL == "DEBUG":
                logger.debug(f"{session.stock_code} 穿越买入档位{levels['lower']:.2f}, "
                            f"等待回升{session.callback_ratio*100:.2f}%")

    def _is_level_in_cooldown(self, session_id: int, level_price: float) -> bool:
        """检查档位是否在冷却期"""
        key = (session_id, level_price)
        if key not in self.level_cooldowns:
            return False

        elapsed = time.time() - self.level_cooldowns[key]
        return elapsed < config.GRID_LEVEL_COOLDOWN

    def check_grid_signals(self, stock_code: str, current_price: float) -> Optional[dict]:
        """
        检查网格交易信号(在持仓监控线程中调用)

        Args:
            stock_code: 股票代码
            current_price: 当前价格

        Returns:
            网格交易信号字典或None
        """
        with self.lock:
            session = self.sessions.get(stock_code)
            if not session or session.status != 'active':
                return None

            # 1. 检查退出条件
            exit_reason = self._check_exit_conditions(session, current_price)
            if exit_reason:
                self.stop_grid_session(session.id, exit_reason)
                return None

            # 2. 更新价格追踪器
            tracker = self.trackers.get(session.id)
            if not tracker:
                return None

            tracker.update_price(current_price)

            # 3. 检查是否穿越新档位
            self._check_level_crossing(session, tracker, current_price)

            # 4. 检查回调触发
            signal_type = tracker.check_callback(session.callback_ratio)
            if signal_type:
                return self._create_grid_signal(session, tracker, signal_type, current_price)

            return None

    def _create_grid_signal(self, session: GridSession, tracker: PriceTracker,
                           signal_type: str, current_price: float) -> dict:
        """创建网格交易信号"""
        signal = {
            'stock_code': session.stock_code,
            'strategy': config.GRID_STRATEGY_NAME,
            'signal_type': signal_type,
            'grid_level': tracker.crossed_level,
            'trigger_price': current_price,
            'session_id': session.id,
            'timestamp': datetime.now().isoformat()
        }

        if signal_type == 'SELL':
            signal['peak_price'] = tracker.peak_price
            signal['callback_ratio'] = (tracker.peak_price - current_price) / tracker.peak_price
        elif signal_type == 'BUY':
            signal['valley_price'] = tracker.valley_price
            signal['callback_ratio'] = (current_price - tracker.valley_price) / tracker.valley_price

        logger.info(f"生成网格{signal_type}信号: {session.stock_code}, "
                   f"档位={tracker.crossed_level:.2f}, 触发价={current_price:.2f}, "
                   f"回调={signal.get('callback_ratio', 0)*100:.2f}%")

        return signal
