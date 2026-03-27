"""
网格交易正常功能场景测试 - 市场行为模拟

测试目标:
1. 持续下跌场景: 激活后股价持续下跌, 触发多次买入, 最终偏离度超限退出
2. 持续上涨场景: 激活后股价持续上涨, 触发多次卖出, 最终偏离度超限退出
3. 多区间震荡场景: 股价在多个网格区间内震荡, 触发至少20次交易操作, 最终止盈退出

测试设计原则:
- 使用精确计算的价格序列, 确保信号触发的可重复性
- 禁用买入冷却(GRID_BUY_COOLDOWN=0)和档位冷却(GRID_LEVEL_COOLDOWN=0)以加速测试
- 模拟模式(ENABLE_SIMULATION_MODE=True)避免依赖真实交易接口
- MockPositionManager 始终返回有效持仓, 防止 position_cleared 意外触发

价格机制说明:
- 穿越下轨(price < lower): 设置 direction=falling, 等待反弹
- 反弹 >= callback_ratio: 触发 BUY 信号, 网格以买入价重建
- 穿越上轨(price > upper): 设置 direction=rising, 等待回调
- 回调 >= callback_ratio: 触发 SELL 信号, 网格以卖出价重建
- 偏离度 = max(drift_deviation, market_deviation), 超过 max_deviation 时退出
"""

import threading
import unittest
import os
import sys
from datetime import datetime, timedelta
from dataclasses import asdict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import config
from grid_trading_manager import GridTradingManager, GridSession, PriceTracker
from grid_database import DatabaseManager
from logger import get_logger

logger = get_logger(__name__)


# ==================== Mock 类 ====================

class MockPositionManager:
    """
    模拟持仓管理器

    始终返回有效持仓(volume=1000), 防止 position_cleared 退出条件
    意外干扰市场行为场景测试.
    """

    def __init__(self, volume=1000, cost_price=9.50):
        self._volume = volume
        self._cost_price = cost_price
        self.signal_lock = threading.RLock()
        self.latest_signals = {}

    def get_position(self, stock_code):
        if self._volume <= 0:
            return None
        return {
            'stock_code': stock_code,
            'volume': self._volume,
            'can_use_volume': self._volume,
            'cost_price': self._cost_price,
            'current_price': self._cost_price,
            'market_value': self._cost_price * self._volume,
        }

    def _increment_data_version(self):
        pass


# ==================== 工具函数 ====================

def _create_session(db, stock_code='000001.SZ', center_price=10.00,
                    max_investment=10000, max_deviation=0.15,
                    target_profit=1.0, stop_loss=-0.99,
                    interval=0.05, callback=0.005):
    """创建并持久化一个网格会话, 返回已分配 id 的 GridSession 对象."""
    session = GridSession(
        stock_code=stock_code,
        center_price=center_price,
        current_center_price=center_price,
        price_interval=interval,
        position_ratio=0.25,
        callback_ratio=callback,
        max_investment=max_investment,
        max_deviation=max_deviation,
        target_profit=target_profit,
        stop_loss=stop_loss,
        start_time=datetime.now() - timedelta(days=1),
        end_time=datetime.now() + timedelta(days=30),
    )
    session_dict = asdict(session)
    session.id = db.create_grid_session(session_dict)
    return session


# ==================== 测试类 ====================

class TestGridScenarioMarketBehavior(unittest.TestCase):
    """网格交易正常功能场景测试 - 市场行为模拟"""

    @classmethod
    def setUpClass(cls):
        print('\n' + '=' * 80)
        print('网格交易正常功能场景测试 - 市场行为模拟')
        print('=' * 80)

    def setUp(self):
        """每个测试前准备 - 使用内存数据库, 覆盖影响测试速度的配置项."""
        self.db = DatabaseManager(':memory:')
        self.db.init_grid_tables()
        self.position_manager = MockPositionManager(volume=1000, cost_price=9.50)
        self.grid_manager = GridTradingManager(
            db_manager=self.db,
            position_manager=self.position_manager,
            trading_executor=None,
        )

        # 保存并覆盖配置
        self._orig_sim = config.ENABLE_SIMULATION_MODE
        self._orig_buy_cooldown = getattr(config, 'GRID_BUY_COOLDOWN', 0)
        self._orig_sell_cooldown = getattr(config, 'GRID_SELL_COOLDOWN', 0)
        self._orig_level_cooldown = getattr(config, 'GRID_LEVEL_COOLDOWN', 60)
        self._orig_require_profit = getattr(config, 'GRID_REQUIRE_PROFIT_TRIGGERED', True)

        config.ENABLE_SIMULATION_MODE = True   # 不依赖真实交易接口
        config.GRID_BUY_COOLDOWN = 0           # 禁用买入冷却以支持快速连续买入
        config.GRID_SELL_COOLDOWN = 0          # 禁用卖出冷却以支持快速连续卖出（A-4修复引入）
        config.GRID_LEVEL_COOLDOWN = 0         # 禁用档位冷却以支持价格序列快速推进
        config.GRID_REQUIRE_PROFIT_TRIGGERED = False  # 不要求已触发止盈

    def tearDown(self):
        """每个测试后恢复配置."""
        config.ENABLE_SIMULATION_MODE = self._orig_sim
        config.GRID_BUY_COOLDOWN = self._orig_buy_cooldown
        config.GRID_SELL_COOLDOWN = self._orig_sell_cooldown
        config.GRID_LEVEL_COOLDOWN = self._orig_level_cooldown
        config.GRID_REQUIRE_PROFIT_TRIGGERED = self._orig_require_profit
        if hasattr(self, 'db'):
            self.db.close()

    # -------- 内部辅助 --------

    def _init_session(self, session):
        """将会话注入管理器并创建对应的价格追踪器."""
        key = self.grid_manager._normalize_code(session.stock_code)
        self.grid_manager.sessions[key] = session
        tracker = PriceTracker(
            session_id=session.id,
            last_price=session.current_center_price,
            peak_price=session.current_center_price,
            valley_price=session.current_center_price,
        )
        self.grid_manager.trackers[session.id] = tracker

    def _simulate_prices(self, stock_code, prices):
        """
        驱动价格序列, 自动处理信号检测和交易执行.

        每个 tick 调用 check_grid_signals; 若产生信号则立即执行 execute_grid_trade.
        一旦会话从 sessions 中移除(退出条件触发), 立即停止迭代.

        Returns:
            (buy_count, sell_count): 本次模拟中成功执行的买入/卖出次数.
        """
        buys, sells = 0, 0
        key = self.grid_manager._normalize_code(stock_code)
        for price in prices:
            if key not in self.grid_manager.sessions:
                break
            signal = self.grid_manager.check_grid_signals(stock_code, price)
            if signal:
                success = self.grid_manager.execute_grid_trade(signal)
                if success:
                    if signal['signal_type'] == 'BUY':
                        buys += 1
                    else:
                        sells += 1
        return buys, sells

    # -------- 测试用例 --------

    def test_1_continuous_fall_triggers_deviation_exit(self):
        """
        场景1: 持续下跌 → 3次买入 → 网格漂移偏离度超限 → deviation退出

        价格序列设计 (center=10.00, interval=5%, callback=0.5%):
          轮次1: 9.40(穿下轨9.50) → 9.45(反弹0.53%→BUY#1)
            买入后 center=9.45, drift=5.5%, new_lower=8.978
          轮次2: 8.90(穿下轨8.978) → 8.95(反弹0.56%→BUY#2)
            买入后 center=8.95, drift=10.5%, new_lower=8.503
          轮次3: 8.42(穿下轨8.503) → 8.47(反弹0.59%→BUY#3)
            买入后 center=8.47, drift=15.3% > max_deviation(15%)
          结算tick: 8.44 → drift=15.3%>15% → EXIT deviation

        验证:
          - 完成至少3次买入 (纯买入, 无卖出)
          - 会话在偏离度超限后退出
        """
        stock_code = '000001.SZ'
        session = _create_session(
            self.db, stock_code=stock_code,
            center_price=10.00, max_investment=10000,
            max_deviation=0.15,
            target_profit=1.0,    # 设置为100%, 不干扰测试
            stop_loss=-0.99,      # 严格配对模式: 无卖出时不触发
        )
        self._init_session(session)

        price_sequence = [
            # 轮次1: 穿越下轨9.50 → BUY#1 at 9.45 → center=9.45, drift=5.5%
            9.40,   # 穿下轨 9.50 → valley=9.40, waiting_callback=True
            9.45,   # 反弹 (9.45-9.40)/9.40=0.53% → BUY#1 执行
            # 轮次2: 穿越下轨8.978(=9.45*0.95) → BUY#2 at 8.95 → center=8.95, drift=10.5%
            8.90,   # 穿下轨 8.978 → valley=8.90
            8.95,   # 反弹 (8.95-8.90)/8.90=0.56% → BUY#2 执行
            # 轮次3: 穿越下轨8.503(=8.95*0.95) → BUY#3 at 8.47 → center=8.47, drift=15.3%
            8.42,   # 穿下轨 8.503 → valley=8.42
            8.47,   # 反弹 (8.47-8.42)/8.42=0.59% → BUY#3 执行 (drift将变为15.3%)
            # 结算tick: drift_deviation = |8.47-10.00|/10.00 = 15.3% > 15% → EXIT
            8.44,   # 触发偏离度检测 → stop_grid_session('deviation')
        ]

        buys, sells = self._simulate_prices(stock_code, price_sequence)

        key = self.grid_manager._normalize_code(stock_code)
        session_exited = key not in self.grid_manager.sessions

        print(f'\n场景1(持续下跌): buys={buys}, sells={sells}, exited={session_exited}')

        self.assertGreaterEqual(buys, 3,
            f'持续下跌应触发至少3次买入, 实际: {buys}次')
        self.assertEqual(sells, 0,
            f'纯下跌趋势不应产生卖出信号, 实际: {sells}次')
        self.assertTrue(session_exited,
            '股价累计下跌15.3%超过偏离度限制15%, 会话应自动退出')

    def test_2_continuous_rise_triggers_deviation_exit(self):
        """
        场景2: 持续上涨 → 4次卖出 → 网格漂移偏离度超限 → deviation退出

        价格序列设计 (center=10.00, interval=5%, callback=0.5%):
          轮次1: 10.55(穿上轨10.50) → 10.49(回调0.57%→SELL#1)
            卖出后 center=10.49, drift=4.9%, new_upper=11.015
          轮次2: 11.02(穿上轨11.015) → 10.96(回调0.54%→SELL#2)
            卖出后 center=10.96, drift=9.6%, new_upper=11.508
          轮次3: 11.52(穿上轨11.508) → 11.45(回调0.61%→SELL#3)
            卖出后 center=11.45, drift=14.5%, new_upper=12.023
          轮次4: 12.03(穿上轨12.023) → 11.96(回调0.58%→SELL#4)
            卖出后 center=11.96, drift=19.6% > max_deviation(15%)
          结算tick: 11.94 → drift=19.6%>15% → EXIT deviation

        验证:
          - 完成至少4次卖出 (纯卖出, 无买入)
          - 会话在偏离度超限后退出
        """
        stock_code = '000001.SZ'
        session = _create_session(
            self.db, stock_code=stock_code,
            center_price=10.00, max_investment=10000,
            max_deviation=0.15,
            target_profit=1.0,    # 无买入故profit_ratio=0, 不触发
            stop_loss=-0.99,      # 严格配对模式: 无买入时不触发
        )
        self._init_session(session)

        price_sequence = [
            # 轮次1: 穿越上轨10.50 → SELL#1 at 10.49 → center=10.49, drift=4.9%
            10.55,  # 穿上轨 10.50 → peak=10.55, waiting_callback=True
            10.49,  # 回调 (10.55-10.49)/10.55=0.57% → SELL#1 执行
            # 轮次2: 穿越上轨11.015(=10.49*1.05) → SELL#2 at 10.96 → center=10.96, drift=9.6%
            11.02,  # 穿上轨 11.015 → peak=11.02
            10.96,  # 回调 (11.02-10.96)/11.02=0.54% → SELL#2 执行
            # 轮次3: 穿越上轨11.508(=10.96*1.05) → SELL#3 at 11.45 → center=11.45, drift=14.5%
            11.52,  # 穿上轨 11.508 → peak=11.52
            11.45,  # 回调 (11.52-11.45)/11.52=0.61% → SELL#3 执行
            # 轮次4: 穿越上轨12.023(=11.45*1.05) → SELL#4 at 11.96 → center=11.96, drift=19.6%
            12.03,  # 穿上轨 12.023 → peak=12.03
            11.96,  # 回调 (12.03-11.96)/12.03=0.58% → SELL#4 执行 (drift将变为19.6%)
            # 结算tick: drift_deviation = |11.96-10.00|/10.00 = 19.6% > 15% → EXIT
            11.94,  # 触发偏离度检测 → stop_grid_session('deviation')
        ]

        buys, sells = self._simulate_prices(stock_code, price_sequence)

        key = self.grid_manager._normalize_code(stock_code)
        session_exited = key not in self.grid_manager.sessions

        print(f'\n场景2(持续上涨): buys={buys}, sells={sells}, exited={session_exited}')

        self.assertGreaterEqual(sells, 4,
            f'持续上涨应触发至少4次卖出, 实际: {sells}次')
        self.assertEqual(buys, 0,
            f'纯上涨趋势不应产生买入信号, 实际: {buys}次')
        self.assertTrue(session_exited,
            '股价累计上涨19.6%超过偏离度限制15%, 会话应自动退出')

    def test_3_oscillating_market_triggers_20_trades_then_target_profit_exit(self):
        """
        场景3: 多区间震荡 → 10买+10卖(共20次操作) → 累计止盈10% → target_profit退出

        价格序列设计 (center=10.00, interval=5%, callback=0.5%, max_investment=10000):
          每个周期 = [9.40, 9.45, 10.00, 9.95]
            9.40: 穿越下轨 → valley=9.40
            9.45: 反弹0.53% → BUY at 9.45 (volume=200, amount=1890), center→9.45
            10.00: 穿越新上轨9.923(=9.45*1.05) → peak=10.00
            9.95: 回调0.50% → SELL at 9.95 (volume=200, amount=1990), center→9.95
          每周期净利润: 200*(9.95-9.45) = 100元 → 累计利润率=1%/周期

          10个完整周期后:
            total_buy  = 10 * 1890 = 18900
            total_sell = 10 * 1990 = 19900
            profit_ratio = (19900-18900)/10000 = 10% >= target_profit(10%)

          第41个tick(9.90): 触发 target_profit 退出检测

        关键设计特性:
          - 网格在每个买入后重建到9.45, 每个卖出后重建到9.95
          - 下一个买入仍能穿越9.95*0.95=9.4525, 因此9.40价格有效
          - 周期完全可重复, 状态稳定
          - max_deviation=50%(宽松), stop_loss=-50%(宽松), 不干扰震荡

        验证:
          - 买入次数 >= 10 (双向交易均发生)
          - 卖出次数 >= 10
          - 总交易次数 >= 20
          - 会话在累计利润达10%后自动退出
        """
        stock_code = '000001.SZ'
        session = _create_session(
            self.db, stock_code=stock_code,
            center_price=10.00, max_investment=10000,
            max_deviation=0.50,   # 宽松偏离限制(50%), 不干扰震荡测试
            target_profit=0.10,   # 10%时触发止盈退出
            stop_loss=-0.50,      # 宽松止损(周期中间状态最低约-9.9%)
        )
        self._init_session(session)

        # 每个周期产生: BUY at 9.45 (amount≈1890) + SELL at 9.95 (amount≈1990)
        # 10个周期 → profit = 10*(1990-1890) = 1000 → profit_ratio = 10%
        single_cycle = [9.40, 9.45, 10.00, 9.95]
        price_sequence = single_cycle * 10   # 10个周期 = 40个价格点
        price_sequence.append(9.90)          # 第41个tick: 触发 target_profit 退出检测

        buys, sells = self._simulate_prices(stock_code, price_sequence)
        total_trades = buys + sells

        key = self.grid_manager._normalize_code(stock_code)
        session_exited = key not in self.grid_manager.sessions

        print(f'\n场景3(多区间震荡): buys={buys}, sells={sells}, total={total_trades}, exited={session_exited}')

        self.assertGreaterEqual(total_trades, 20,
            f'震荡市场应触发至少20次交易操作, 实际: {total_trades}次 (买{buys}+卖{sells})')
        self.assertGreater(buys, 0,
            '震荡市场应包含买入操作')
        self.assertGreater(sells, 0,
            '震荡市场应包含卖出操作')
        self.assertTrue(session_exited,
            f'累计盈利10%后会话应自动退出, 实际trades={total_trades}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
