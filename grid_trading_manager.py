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

    def __init__(self, db_manager, position_manager, trading_executor):
        self.db = db_manager
        self.position_manager = position_manager
        self.executor = trading_executor

        # 内存缓存
        self.sessions: Dict[str, GridSession] = {}
        self.trackers: Dict[int, PriceTracker] = {}
        self.level_cooldowns: Dict[tuple, float] = {}
        self.lock = threading.RLock()  # 使用可重入锁,支持嵌套调用

        # 初始化:从数据库加载活跃会话
        logger.info(f"[GRID] GridTradingManager.__init__: 初始化网格交易管理器")
        loaded_count = self._load_active_sessions()
        logger.info(f"[GRID] GridTradingManager.__init__: 初始化完成, 已加载 {loaded_count} 个活跃会话")

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
                    except:
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
                session_id = session_dict['id']
                logger.info(f"[GRID] >>> 开始处理会话 session_id={session_id}, stock_code={stock_code}")

                try:
                    # 1. 检查会话是否已过期
                    # BUG FIX: 使用session_dict而不是session_data
                    end_time = datetime.fromisoformat(session_dict['end_time'])
                    if datetime.now() > end_time:
                        # 先更新数据库状态
                        self.db.stop_grid_session(session_id, 'expired')

                        # 如果内存里已有该会话，做最小清理避免Web仍显示active
                        existing = self.sessions.get(stock_code)
                        if existing and existing.status == 'active':
                            logger.info(f"[GRID] 会话{session_id}({stock_code})已过期，清理内存会话")
                            # 仅做最小清理：从内存移除并触发版本更新
                            try:
                                del self.sessions[stock_code]
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
                        start_time=datetime.fromisoformat(session_dict['start_time']),
                        end_time=end_time
                    )
                    self.sessions[stock_code] = session

                    # 4. 重置PriceTracker(保守策略)
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

    def start_grid_session(self, stock_code: str, user_config: dict) -> GridSession:
        """启动网格交易会话（三阶段设计，避免AB-BA死锁）

        阶段1（锁外）：获取持仓数据并验证前置条件
        阶段2（锁内）：停止旧session、创建数据库记录、创建内存对象
        阶段3（锁外）：触发数据版本更新、打印成功日志
        """
        logger.info(f"[GRID] start_grid_session: ========== 开始启动会话 ==========")
        logger.info(f"[GRID] start_grid_session: stock_code={stock_code}")
        logger.info(f"[GRID] start_grid_session: user_config={user_config}")

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
            if stock_code in self.sessions:
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
            self.sessions[stock_code] = session
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
        """停止网格交易会话（公共接口，会获取锁）"""
        logger.info(f"[GRID] stop_grid_session: 开始停止会话 session_id={session_id}, reason={reason}")

        with self.lock:
            return self._stop_grid_session_unlocked(session_id, reason)

    def _stop_grid_session_unlocked(self, session_id: int, reason: str) -> dict:
        """停止网格交易会话（内部方法，调用者必须已持有锁）"""
        logger.info(f"[GRID] _stop_grid_session_unlocked: 开始停止会话 session_id={session_id}, reason={reason}")

        # 查找会话
        session = None
        for s in self.sessions.values():
            if s.id == session_id:
                session = s
                break

        if not session:
            logger.warning(f"[GRID] _stop_grid_session_unlocked: 会话{session_id}不存在, 无法停止")
            raise ValueError(f"会话{session_id}不存在")

        stock_code = session.stock_code
        logger.debug(f"[GRID] _stop_grid_session_unlocked: 找到会话 stock_code={stock_code}")

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
        if stock_code in self.sessions:
            session_obj = self.sessions[stock_code]
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
        if stock_code in self.sessions:
            del self.sessions[stock_code]
            logger.debug(f"[GRID] _stop_grid_session_unlocked: 从sessions中移除 {stock_code}")
        if session_id in self.trackers:
            del self.trackers[session_id]
            logger.debug(f"[GRID] _stop_grid_session_unlocked: 从trackers中移除 session_id={session_id}")

        # 清除冷却记录 (支持两种键格式)
        cooldown_keys_by_session = [k for k in self.level_cooldowns.keys() if k[0] == session_id]
        cooldown_keys_by_stock = [k for k in self.level_cooldowns.keys() if k[0] == stock_code]
        if cooldown_keys_by_session:
            logger.debug(f"[GRID] _stop_grid_session_unlocked: 清除 {len(cooldown_keys_by_session)} 个session_id档位冷却记录")
        for key in cooldown_keys_by_session:
            del self.level_cooldowns[key]
        if cooldown_keys_by_stock:
            logger.debug(f"[GRID] _stop_grid_session_unlocked: 清除 {len(cooldown_keys_by_stock)} 个stock_code档位冷却记录")
        for key in cooldown_keys_by_stock:
            del self.level_cooldowns[key]

        # 触发数据版本更新
        self.position_manager._increment_data_version()

        final_stats = {
            'stock_code': stock_code,
            'trade_count': session.trade_count,
            'profit_ratio': session.get_profit_ratio(),
            'stop_reason': reason
        }

        logger.info(f"[GRID] _stop_grid_session_unlocked: 停止完成! stock_code={stock_code}, reason={reason}, "
                   f"trade_count={session.trade_count}, profit={session.get_profit_ratio()*100:.2f}%")

        return final_stats

    def _check_exit_conditions(self, session: GridSession, current_price: float) -> Optional[str]:
        """检查退出条件,返回退出原因或None"""
        logger.debug(f"[GRID] _check_exit_conditions: session_id={session.id}, stock_code={session.stock_code}, current_price={current_price:.2f}")

        # 1. 偏离度检测
        if session.current_center_price and session.center_price:
            deviation = session.get_deviation_ratio()
            logger.debug(f"[GRID] _check_exit_conditions: 偏离度检测 deviation={deviation*100:.2f}%, max_deviation={session.max_deviation*100:.2f}%")
            if deviation > session.max_deviation:
                logger.warning(f"[GRID] _check_exit_conditions: {session.stock_code} 偏离度{deviation*100:.2f}%超过限制{session.max_deviation*100:.2f}%, 触发退出")
                return 'deviation'

        # 2. 盈亏检测：严格配对模式 - 必须至少完成1次买入+1次卖出
        if session.buy_count > 0 and session.sell_count > 0:
            profit_ratio = session.get_profit_ratio()
            logger.debug(f"[GRID] _check_exit_conditions: 盈亏检测 profit_ratio={profit_ratio*100:.2f}%, "
                        f"target={session.target_profit*100:.2f}%, stop_loss={session.stop_loss*100:.2f}%, "
                        f"buy_count={session.buy_count}, sell_count={session.sell_count}")

            # 止盈检测
            if profit_ratio >= session.target_profit:
                logger.info(f"[GRID] {session.stock_code} 达到目标盈利{profit_ratio*100:.2f}%, "
                           f"buy_count={session.buy_count}, sell_count={session.sell_count}")
                return 'target_profit'

            # 止损检测
            if profit_ratio <= session.stop_loss:
                logger.warning(f"[GRID] {session.stock_code} 触发止损{profit_ratio*100:.2f}%, "
                              f"buy_count={session.buy_count}, sell_count={session.sell_count}")
                return 'stop_loss'
        else:
            logger.debug(f"[GRID] _check_exit_conditions: 未完成配对操作(buy_count={session.buy_count}, "
                        f"sell_count={session.sell_count}), 跳过盈亏检测")

        # 3. 时间限制
        if session.end_time:
            remaining = session.end_time - datetime.now()
            logger.debug(f"[GRID] _check_exit_conditions: 时间检测 end_time={session.end_time}, remaining={remaining}")
            if datetime.now() > session.end_time:
                logger.info(f"[GRID] _check_exit_conditions: {session.stock_code} 达到运行时长限制, 触发退出")
                return 'expired'

        # 4. 持仓清空
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

        with self.lock:
            session = self.sessions.get(stock_code)
            if not session:
                logger.debug(f"[GRID] check_grid_signals: {stock_code} 无活跃会话, 返回None")
                return None
            if session.status != 'active':
                logger.debug(f"[GRID] check_grid_signals: {stock_code} 会话状态={session.status}, 非active, 返回None")
                return None

            logger.debug(f"[GRID] check_grid_signals: 找到活跃会话 session_id={session.id}, status={session.status}")

            # 1. 检查退出条件
            exit_reason = self._check_exit_conditions(session, current_price)
            if exit_reason:
                logger.info(f"[GRID] check_grid_signals: {stock_code} 触发退出条件 reason={exit_reason}")
                self.stop_grid_session(session.id, exit_reason)
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
            'timestamp': datetime.now().isoformat()
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

        # 更新数据库
        self.db.update_grid_session(session.id, {
            'current_center_price': trade_price
        })
        logger.debug(f"[GRID] _rebuild_grid: 数据库更新完成")

        levels = session.get_grid_levels()
        logger.info(f"[GRID] _rebuild_grid: 网格重建完成 {session.stock_code}, "
                   f"旧中心={old_center:.2f} -> 新中心={trade_price:.2f}, "
                   f"新档位=[{levels['lower']:.2f}, {levels['center']:.2f}, {levels['upper']:.2f}]")

    def execute_grid_trade(self, signal: dict) -> bool:
        """
        执行网格交易

        Args:
            signal: 网格交易信号

        Returns:
            执行是否成功
        """
        logger.info(f"[GRID] execute_grid_trade: 开始执行交易 signal={signal}")

        try:
            with self.lock:
                stock_code = signal['stock_code']
                session = self.sessions.get(stock_code)
                if not session:
                    logger.error(f"[GRID] execute_grid_trade: 会话不存在: {stock_code}")
                    return False

                signal_type = signal['signal_type']
                trigger_price = signal['trigger_price']
                logger.debug(f"[GRID] execute_grid_trade: session_id={session.id}, signal_type={signal_type}, trigger_price={trigger_price:.2f}")

                # 执行交易前的状态
                logger.debug(f"[GRID] execute_grid_trade: 交易前状态 trade_count={session.trade_count}, "
                            f"current_investment={session.current_investment:.2f}, profit_ratio={session.get_profit_ratio()*100:.2f}%")

                # 执行交易
                if signal_type == 'BUY':
                    logger.debug(f"[GRID] execute_grid_trade: 调用_execute_grid_buy")
                    success = self._execute_grid_buy(session, signal)
                elif signal_type == 'SELL':
                    logger.debug(f"[GRID] execute_grid_trade: 调用_execute_grid_sell")
                    success = self._execute_grid_sell(session, signal)
                else:
                    logger.error(f"[GRID] execute_grid_trade: 未知信号类型: {signal_type}")
                    return False

                if not success:
                    logger.warning(f"[GRID] execute_grid_trade: 交易执行失败 stock_code={stock_code}, signal_type={signal_type}")
                    return False

                # 设置档位冷却
                level = signal['grid_level']
                self.level_cooldowns[(session.id, level)] = time.time()
                logger.debug(f"[GRID] execute_grid_trade: 设置档位冷却 session_id={session.id}, level={level:.2f}")

                # 执行交易后的状态
                logger.debug(f"[GRID] execute_grid_trade: 交易后状态 trade_count={session.trade_count}, "
                            f"current_investment={session.current_investment:.2f}, profit_ratio={session.get_profit_ratio()*100:.2f}%")

                # 触发数据版本更新
                self.position_manager._increment_data_version()

                logger.info(f"[GRID] execute_grid_trade: 交易执行成功 stock_code={stock_code}, signal_type={signal_type}")
                return True

        except Exception as e:
            logger.error(f"[GRID] execute_grid_trade: 执行网格交易失败: {str(e)}", exc_info=True)
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

        # 1. 检查投入限额
        logger.debug(f"[GRID] _execute_grid_buy: 检查投入限额 current_investment={session.current_investment:.2f}, max_investment={session.max_investment:.2f}")
        if session.current_investment >= session.max_investment:
            logger.warning(f"[GRID] _execute_grid_buy: {stock_code} 达到最大投入限额{session.max_investment:.2f}, 跳过买入")
            return False

        # 2. 计算买入金额和数量
        remaining_investment = session.max_investment - session.current_investment
        # 单次买入金额 = min(剩余额度, 总额度的20%)
        # 注意: 这里使用固定比例0.2而非session.position_ratio,后者用于卖出计算
        target_buy_amount = session.max_investment * 0.2
        buy_amount = min(remaining_investment, target_buy_amount)
        logger.debug(f"[GRID] _execute_grid_buy: remaining_investment={remaining_investment:.2f}, "
                    f"target_buy_amount(20%)={target_buy_amount:.2f}, buy_amount={buy_amount:.2f}")

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

        if config.ENABLE_SIMULATION_MODE:
            trade_id = f"GRID_SIM_BUY_{int(time.time()*1000)}"
            logger.info(f"[GRID] _execute_grid_buy: [模拟]网格买入: {stock_code}, 数量={volume}, 价格={trigger_price:.2f}, trade_id={trade_id}")
        else:
            # 实盘买入
            logger.debug(f"[GRID] _execute_grid_buy: 调用executor.execute_buy 实盘买入")
            result = self.executor.execute_buy(
                stock_code=stock_code,
                amount=actual_amount,
                strategy=config.GRID_STRATEGY_NAME
            )
            if not result:
                logger.error(f"[GRID] _execute_grid_buy: 实盘网格买入失败: {stock_code}")
                return False
            trade_id = result.get('order_id', '')
            logger.info(f"[GRID] _execute_grid_buy: 实盘网格买入成功: {stock_code}, trade_id={trade_id}")

        # 4. 更新会话统计
        old_trade_count = session.trade_count
        old_buy_count = session.buy_count
        old_total_buy = session.total_buy_amount
        old_investment = session.current_investment

        session.trade_count += 1
        session.buy_count += 1
        session.total_buy_amount += actual_amount
        session.current_investment += actual_amount

        logger.debug(f"[GRID] _execute_grid_buy: 更新会话统计 trade_count {old_trade_count}->{session.trade_count}, "
                    f"buy_count {old_buy_count}->{session.buy_count}, "
                    f"total_buy {old_total_buy:.2f}->{session.total_buy_amount:.2f}, "
                    f"investment {old_investment:.2f}->{session.current_investment:.2f}")

        # 5. 记录交易
        trade_data = {
            'session_id': session.id,
            'stock_code': stock_code,
            'trade_type': 'BUY',
            'grid_level': signal['grid_level'],
            'trigger_price': trigger_price,
            'volume': volume,
            'amount': actual_amount,
            'valley_price': signal.get('valley_price'),
            'callback_ratio': round(signal.get('callback_ratio'), 4) if signal.get('callback_ratio') else None,  # 保留4位小数
            'trade_id': trade_id,
            'trade_time': datetime.now().isoformat(),
            'grid_center_before': session.current_center_price,
            'grid_center_after': trigger_price
        }
        logger.debug(f"[GRID] _execute_grid_buy: 记录交易 trade_data={trade_data}")
        self.db.record_grid_trade(trade_data)

        # 6. 更新数据库会话
        logger.debug(f"[GRID] _execute_grid_buy: 更新数据库会话")
        self.db.update_grid_session(session.id, {
            'trade_count': session.trade_count,
            'buy_count': session.buy_count,
            'total_buy_amount': session.total_buy_amount,
            'current_investment': session.current_investment
        })

        # 7. 重建网格
        logger.debug(f"[GRID] _execute_grid_buy: 重建网格")
        self._rebuild_grid(session, trigger_price)

        logger.info(f"[GRID] _execute_grid_buy: 网格买入成功! stock_code={stock_code}, volume={volume}, amount={actual_amount:.2f}, "
                   f"investment={session.current_investment:.2f}/{session.max_investment:.2f}, trade_id={trade_id}")

        return True

    def _execute_grid_sell(self, session: GridSession, signal: dict) -> bool:
        """执行网格卖出"""
        stock_code = session.stock_code
        trigger_price = signal['trigger_price']
        logger.info(f"[GRID] _execute_grid_sell: 开始执行 stock_code={stock_code}, trigger_price={trigger_price:.2f}")

        # 1. 获取当前持仓
        position = self.position_manager.get_position(stock_code)
        if not position:
            logger.error(f"[GRID] _execute_grid_sell: {stock_code} 持仓不存在")
            return False

        current_volume = position.get('volume', 0)
        cost_price = position.get('cost_price', trigger_price)
        logger.debug(f"[GRID] _execute_grid_sell: 当前持仓 volume={current_volume}, cost_price={cost_price:.2f}")

        if current_volume == 0:
            logger.warning(f"[GRID] _execute_grid_sell: {stock_code} 持仓为0, 跳过卖出")
            return False

        # 2. 计算卖出数量
        sell_volume = int(current_volume * session.position_ratio / 100) * 100
        logger.debug(f"[GRID] _execute_grid_sell: 计算卖出数量 position_ratio={session.position_ratio*100:.1f}%, 初步sell_volume={sell_volume}")

        if sell_volume == 0:
            sell_volume = 100  # 最少卖100股
            logger.debug(f"[GRID] _execute_grid_sell: 卖出数量为0, 调整为最小值100")

        if sell_volume > current_volume:
            sell_volume = int(current_volume / 100) * 100
            logger.debug(f"[GRID] _execute_grid_sell: 卖出数量超过持仓, 调整为{sell_volume}")

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
            # 实盘卖出
            logger.debug(f"[GRID] _execute_grid_sell: 调用executor.execute_sell 实盘卖出")
            result = self.executor.execute_sell(
                stock_code=stock_code,
                volume=sell_volume,
                strategy=config.GRID_STRATEGY_NAME
            )
            if not result:
                logger.error(f"[GRID] _execute_grid_sell: 实盘网格卖出失败: {stock_code}")
                return False
            trade_id = result.get('order_id', '')
            logger.info(f"[GRID] _execute_grid_sell: 实盘网格卖出成功: {stock_code}, trade_id={trade_id}")

        # 4. 更新会话统计
        old_trade_count = session.trade_count
        old_sell_count = session.sell_count
        old_total_sell = session.total_sell_amount
        old_investment = session.current_investment

        session.trade_count += 1
        session.sell_count += 1
        session.total_sell_amount += sell_amount
        # 卖出时减少投入(回收资金)
        recovered_cost = sell_volume * cost_price
        session.current_investment = max(0, session.current_investment - recovered_cost)

        logger.debug(f"[GRID] _execute_grid_sell: 更新会话统计 trade_count {old_trade_count}->{session.trade_count}, "
                    f"sell_count {old_sell_count}->{session.sell_count}, "
                    f"total_sell {old_total_sell:.2f}->{session.total_sell_amount:.2f}, "
                    f"investment {old_investment:.2f}->{session.current_investment:.2f}, recovered_cost={recovered_cost:.2f}")

        # 5. 记录交易
        trade_data = {
            'session_id': session.id,
            'stock_code': stock_code,
            'trade_type': 'SELL',
            'grid_level': signal['grid_level'],
            'trigger_price': trigger_price,
            'volume': sell_volume,
            'amount': sell_amount,
            'peak_price': signal.get('peak_price'),
            'callback_ratio': round(signal.get('callback_ratio'), 4) if signal.get('callback_ratio') else None,  # 保留4位小数
            'trade_id': trade_id,
            'trade_time': datetime.now().isoformat(),
            'grid_center_before': session.current_center_price,
            'grid_center_after': trigger_price
        }
        logger.debug(f"[GRID] _execute_grid_sell: 记录交易 trade_data={trade_data}")
        self.db.record_grid_trade(trade_data)

        # 6. 更新数据库会话
        logger.debug(f"[GRID] _execute_grid_sell: 更新数据库会话")
        self.db.update_grid_session(session.id, {
            'trade_count': session.trade_count,
            'sell_count': session.sell_count,
            'total_sell_amount': session.total_sell_amount,
            'current_investment': session.current_investment
        })

        # 7. 重建网格
        logger.debug(f"[GRID] _execute_grid_sell: 重建网格")
        self._rebuild_grid(session, trigger_price)

        profit = session.get_profit_ratio()
        logger.info(f"[GRID] _execute_grid_sell: 网格卖出成功! stock_code={stock_code}, volume={sell_volume}, amount={sell_amount:.2f}, "
                   f"profit={profit*100:.2f}%, trade_id={trade_id}")

        return True

    def get_session_stats(self, session_id: int) -> dict:
        """获取会话统计信息"""
        session = None
        for s in self.sessions.values():
            if s.id == session_id:
                session = s
                break

        if not session:
            return {}

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
            'start_time': session.start_time.isoformat() if session.start_time else None,
            'end_time': session.end_time.isoformat() if session.end_time else None
        }

    def get_trade_history(self, session_id: int, limit=50, offset=0) -> list:
        """获取交易历史"""
        return self.db.get_grid_trades(session_id, limit, offset)
