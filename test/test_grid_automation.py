"""
网格交易自动化回归测试

集成到统一测试框架中，验证网格交易核心逻辑：
- 网格session启动与停止
- 价格追踪器状态机
- 买入/卖出信号触发
- 网格重建机制
- 档位冷却机制
- 边界条件处理
"""

import unittest
import sys
import os
import time
from datetime import datetime, timedelta

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from logger import get_logger
from grid_database import DatabaseManager
from grid_trading_manager import GridTradingManager, GridSession

logger = get_logger(__name__)

# 测试配置
TEST_STOCK = "000001.SZ"
INITIAL_PRICE = 10.00
PRICE_INTERVAL = 0.05  # 5%
CALLBACK_RATIO = 0.005  # 0.5%


class MockPositionManager:
    """模拟PositionManager用于测试"""
    def __init__(self):
        self.positions = {}
        self.data_version = 0

    def get_position(self, stock_code):
        """返回模拟持仓"""
        return self.positions.get(stock_code, {
            "stock_code": stock_code,
            "volume": 1000,
            "current_price": INITIAL_PRICE,
            "cost_price": INITIAL_PRICE * 0.95,
            "profit_triggered": True,
            "highest_price": INITIAL_PRICE
        })

    def _increment_data_version(self):
        """模拟数据版本更新"""
        self.data_version += 1


class MockTradingExecutor:
    """模拟TradingExecutor用于测试"""
    def __init__(self):
        self.trades = []

    def execute_buy(self, stock_code, amount, strategy):
        """模拟买入"""
        trade = {
            "stock_code": stock_code,
            "amount": amount,
            "strategy": strategy,
            "order_id": f"BUY_{int(time.time()*1000)}",
            "timestamp": datetime.now().isoformat()
        }
        self.trades.append(trade)
        return trade

    def execute_sell(self, stock_code, volume, strategy):
        """模拟卖出"""
        trade = {
            "stock_code": stock_code,
            "volume": volume,
            "strategy": strategy,
            "order_id": f"SELL_{int(time.time()*1000)}",
            "timestamp": datetime.now().isoformat()
        }
        self.trades.append(trade)
        return trade


class TestGridTradingAutomation(unittest.TestCase):
    """网格交易自动化测试套件"""

    @classmethod
    def setUpClass(cls):
        """测试类初始化"""
        logger.info("="*60)
        logger.info("网格交易自动化测试开始")
        logger.info("="*60)

        # 创建测试环境
        cls.db_manager = DatabaseManager()
        cls.position_manager = MockPositionManager()
        cls.trading_executor = MockTradingExecutor()
        cls.grid_manager = GridTradingManager(
            db_manager=cls.db_manager,
            position_manager=cls.position_manager,
            trading_executor=cls.trading_executor
        )

    def setUp(self):
        """每个测试用例前的准备"""
        # 清理测试session
        try:
            if hasattr(self, 'test_session') and self.test_session:
                self.grid_manager.stop_grid_session(self.test_session.id, "test_cleanup")
        except:
            pass
        self.test_session = None

    def tearDown(self):
        """每个测试用例后的清理"""
        # 清理测试session
        try:
            if self.test_session:
                self.grid_manager.stop_grid_session(self.test_session.id, "test_completed")
        except:
            pass

    def test_01_create_grid_session(self):
        """测试1: 创建网格交易session"""
        logger.info("测试1: 创建网格交易session")

        user_config = {
            "center_price": INITIAL_PRICE,
            "price_interval": PRICE_INTERVAL,
            "position_ratio": 0.25,
            "callback_ratio": CALLBACK_RATIO,
            "max_investment": 10000,
            "max_deviation": 0.15,
            "target_profit": 0.10,
            "stop_loss": -0.10,
            "duration_days": 7,
            "risk_level": "moderate"
        }

        session = self.grid_manager.start_grid_session(TEST_STOCK, user_config)
        self.test_session = session

        # 验证session创建
        self.assertIsNotNone(session, "session应该成功创建")
        self.assertIsNotNone(session.id, "session ID应该已分配")
        self.assertEqual(session.stock_code, TEST_STOCK, "股票代码应该正确")
        self.assertEqual(session.status, "active", "session状态应该为active")
        self.assertEqual(session.center_price, INITIAL_PRICE, "中心价格应该正确")

        # 验证网格档位
        levels = session.get_grid_levels()
        expected_lower = INITIAL_PRICE * (1 - PRICE_INTERVAL)
        expected_upper = INITIAL_PRICE * (1 + PRICE_INTERVAL)

        self.assertAlmostEqual(levels['lower'], expected_lower, places=2,
                              msg="下档位应该正确")
        self.assertAlmostEqual(levels['upper'], expected_upper, places=2,
                              msg="上档位应该正确")

    def test_02_sell_signal_trigger(self):
        """测试2: 卖出信号触发（上穿+回调）"""
        logger.info("测试2: 卖出信号触发")

        # 创建session
        user_config = {
            "center_price": INITIAL_PRICE,
            "price_interval": PRICE_INTERVAL,
            "callback_ratio": CALLBACK_RATIO,
            "max_investment": 10000
        }
        session = self.grid_manager.start_grid_session(TEST_STOCK, user_config)
        self.test_session = session

        levels = session.get_grid_levels()
        upper_level = levels['upper']

        # 步骤1: 价格上穿
        price_1 = upper_level * 1.01
        signal_1 = self.grid_manager.check_grid_signals(TEST_STOCK, price_1)
        self.assertIsNone(signal_1, "上穿后应该等待回调，不立即触发信号")

        # 验证PriceTracker状态
        tracker = self.grid_manager.trackers.get(session.id)
        self.assertTrue(tracker.waiting_callback, "应该进入等待回调状态")
        self.assertEqual(tracker.direction, "rising", "方向应该为上涨")

        # 步骤2: 价格回调触发信号
        price_2 = price_1 * 1.005  # 继续上涨
        price_3 = price_2 * (1 - CALLBACK_RATIO - 0.001)  # 回调超过阈值

        self.grid_manager.check_grid_signals(TEST_STOCK, price_2)
        signal_3 = self.grid_manager.check_grid_signals(TEST_STOCK, price_3)

        self.assertIsNotNone(signal_3, "回调应该触发SELL信号")
        self.assertEqual(signal_3.get('signal_type'), 'SELL', "信号类型应该为SELL")
        self.assertEqual(signal_3.get('stock_code'), TEST_STOCK, "股票代码应该正确")

    def test_03_buy_signal_trigger(self):
        """测试3: 买入信号触发（下穿+回升）"""
        logger.info("测试3: 买入信号触发")

        # 创建session
        user_config = {
            "center_price": INITIAL_PRICE,
            "price_interval": PRICE_INTERVAL,
            "callback_ratio": CALLBACK_RATIO,
            "max_investment": 10000
        }
        session = self.grid_manager.start_grid_session(TEST_STOCK, user_config)
        self.test_session = session

        levels = session.get_grid_levels()
        lower_level = levels['lower']

        # 步骤1: 价格下穿
        price_1 = lower_level * 0.99
        signal_1 = self.grid_manager.check_grid_signals(TEST_STOCK, price_1)
        self.assertIsNone(signal_1, "下穿后应该等待回升，不立即触发信号")

        # 验证PriceTracker状态
        tracker = self.grid_manager.trackers.get(session.id)
        self.assertTrue(tracker.waiting_callback, "应该进入等待回调状态")
        self.assertEqual(tracker.direction, "falling", "方向应该为下跌")

        # 步骤2: 价格回升触发信号
        price_2 = price_1 * 0.995  # 继续下跌
        price_3 = price_2 * (1 + CALLBACK_RATIO + 0.001)  # 回升超过阈值

        self.grid_manager.check_grid_signals(TEST_STOCK, price_2)
        signal_3 = self.grid_manager.check_grid_signals(TEST_STOCK, price_3)

        self.assertIsNotNone(signal_3, "回升应该触发BUY信号")
        self.assertEqual(signal_3.get('signal_type'), 'BUY', "信号类型应该为BUY")

    def test_04_grid_rebuild_mechanism(self):
        """测试4: 网格重建机制"""
        logger.info("测试4: 网格重建机制")

        # 创建session
        user_config = {
            "center_price": INITIAL_PRICE,
            "price_interval": PRICE_INTERVAL,
            "max_investment": 10000
        }
        session = self.grid_manager.start_grid_session(TEST_STOCK, user_config)
        self.test_session = session

        original_center = session.current_center_price
        new_trade_price = INITIAL_PRICE * 1.045

        # 执行网格重建
        self.grid_manager._rebuild_grid(session, new_trade_price)

        # 验证中心价更新
        self.assertEqual(session.current_center_price, new_trade_price,
                        "中心价应该更新为成交价")

        # 验证PriceTracker重置
        tracker = self.grid_manager.trackers.get(session.id)
        self.assertFalse(tracker.waiting_callback, "PriceTracker应该已重置")
        self.assertIsNone(tracker.direction, "方向应该已清空")

        # 验证新档位
        new_levels = session.get_grid_levels()
        expected_lower = new_trade_price * (1 - PRICE_INTERVAL)
        expected_upper = new_trade_price * (1 + PRICE_INTERVAL)

        self.assertAlmostEqual(new_levels['lower'], expected_lower, places=2,
                              msg="新下档位应该正确")
        self.assertAlmostEqual(new_levels['upper'], expected_upper, places=2,
                              msg="新上档位应该正确")

    def test_05_cooldown_mechanism(self):
        """测试5: 档位冷却机制"""
        logger.info("测试5: 档位冷却机制")

        # 创建session
        user_config = {
            "center_price": INITIAL_PRICE,
            "price_interval": PRICE_INTERVAL,
            "max_investment": 10000
        }
        session = self.grid_manager.start_grid_session(TEST_STOCK, user_config)
        self.test_session = session

        test_level = INITIAL_PRICE * 1.05

        # 清空冷却记录（避免历史干扰）
        self.grid_manager.level_cooldowns.clear()

        # 首次检查应该无冷却
        in_cooldown_1 = self.grid_manager._is_level_in_cooldown(session.id, test_level)
        self.assertFalse(in_cooldown_1, "首次检查应该无冷却")

        # 设置冷却
        self.grid_manager.level_cooldowns[(session.id, test_level)] = time.time()

        # 立即检查应该在冷却期
        in_cooldown_2 = self.grid_manager._is_level_in_cooldown(session.id, test_level)
        self.assertTrue(in_cooldown_2, "设置后应该在冷却期")

        # 模拟冷却过期
        self.grid_manager.level_cooldowns[(session.id, test_level)] = \
            time.time() - config.GRID_LEVEL_COOLDOWN - 1

        in_cooldown_3 = self.grid_manager._is_level_in_cooldown(session.id, test_level)
        self.assertFalse(in_cooldown_3, "冷却过期后应该可再次触发")

    def test_06_boundary_conditions(self):
        """测试6: 边界条件处理"""
        logger.info("测试6: 边界条件处理")

        # 创建session
        user_config = {
            "center_price": INITIAL_PRICE,
            "price_interval": PRICE_INTERVAL,
            "callback_ratio": CALLBACK_RATIO,
            "max_investment": 10000
        }
        session = self.grid_manager.start_grid_session(TEST_STOCK, user_config)
        self.test_session = session

        levels = session.get_grid_levels()
        tracker = self.grid_manager.trackers.get(session.id)
        tracker.reset(INITIAL_PRICE)

        # 边界1: 价格恰好等于档位
        signal_exact = self.grid_manager.check_grid_signals(TEST_STOCK, levels['upper'])
        self.assertIsNone(signal_exact, "价格等于档位不应触发穿越（需严格大于）")

        # 边界2: 价格略超档位
        price_over = levels['upper'] + 0.01
        signal_over = self.grid_manager.check_grid_signals(TEST_STOCK, price_over)
        self.assertIsNone(signal_over, "略超档位应触发穿越检测但未回调")
        self.assertTrue(tracker.waiting_callback, "应该进入等待回调状态")

        # 边界3: 回调恰好等于阈值
        callback_price = price_over * (1 - CALLBACK_RATIO)
        signal_callback = self.grid_manager.check_grid_signals(TEST_STOCK, callback_price)
        self.assertIsNotNone(signal_callback, "回调恰好等于阈值应该触发信号")

    def test_07_execute_trade(self):
        """测试7: 执行网格交易"""
        logger.info("测试7: 执行网格交易")

        # 创建session
        user_config = {
            "center_price": INITIAL_PRICE,
            "price_interval": PRICE_INTERVAL,
            "max_investment": 10000
        }
        session = self.grid_manager.start_grid_session(TEST_STOCK, user_config)
        self.test_session = session

        # 创建卖出信号
        sell_signal = {
            'stock_code': TEST_STOCK,
            'strategy': config.GRID_STRATEGY_NAME,
            'signal_type': 'SELL',
            'grid_level': INITIAL_PRICE * 1.05,
            'trigger_price': INITIAL_PRICE * 1.045,
            'peak_price': INITIAL_PRICE * 1.05,
            'callback_ratio': 0.006,
            'session_id': session.id,
            'timestamp': datetime.now().isoformat()
        }

        # 执行交易
        success = self.grid_manager.execute_grid_trade(sell_signal)
        self.assertTrue(success, "卖出交易应该执行成功")

        # 验证数据库记录
        trades = self.grid_manager.db.get_grid_trades(session.id)
        self.assertGreater(len(trades), 0, "应该有交易记录保存到数据库")


def suite():
    """创建测试套件"""
    test_suite = unittest.TestSuite()
    test_suite.addTest(unittest.makeSuite(TestGridTradingAutomation))
    return test_suite


if __name__ == '__main__':
    # 单独运行此测试模块
    runner = unittest.TextTestRunner(verbosity=2)
    runner.run(suite())
