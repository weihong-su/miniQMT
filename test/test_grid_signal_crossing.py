"""
网格交易 - 档位穿越检测测试

测试网格档位穿越逻辑:
1. 上穿卖出档位检测
2. 下穿买入档位检测
3. 档位冷却机制 (60秒)
4. 网格档位动态计算

使用虚拟环境: C:\\Users\\PC\\Anaconda3\\envs\\python39
"""

import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime
import time
from grid_trading_manager import GridSession, PriceTracker, GridTradingManager
import config
from logger import get_logger

logger = get_logger(__name__)


class TestGridLevelCrossing(unittest.TestCase):
    """网格档位穿越检测测试"""

    def setUp(self):
        """初始化测试环境"""
        logger.info("=" * 80)
        logger.info(f"开始测试: {self._testMethodName}")
        logger.info("=" * 80)

        # Mock 依赖
        self.db_manager = MagicMock()
        self.position_manager = MagicMock()
        self.trading_executor = MagicMock()

        # 创建 GridTradingManager (禁用初始化加载)
        with patch.object(GridTradingManager, '_load_active_sessions', return_value=0):
            self.manager = GridTradingManager(
                self.db_manager,
                self.position_manager,
                self.trading_executor
            )

    def tearDown(self):
        """清理测试环境"""
        logger.info(f"测试完成: {self._testMethodName}")
        logger.info("")

    def test_grid_levels_calculation(self):
        """测试网格档位计算"""
        logger.info("[TEST] 测试网格档位计算")

        session = GridSession(
            id=1,
            stock_code='000001.SZ',
            center_price=10.0,
            current_center_price=10.0,
            price_interval=0.05  # ±5%
        )

        levels = session.get_grid_levels()

        self.assertAlmostEqual(levels['lower'], 9.5, places=2)  # 10.0 * (1 - 0.05) = 9.5
        self.assertAlmostEqual(levels['center'], 10.0, places=2)
        self.assertAlmostEqual(levels['upper'], 10.5, places=2)  # 10.0 * (1 + 0.05) = 10.5

        logger.info(f"[PASS] 网格档位计算正确: lower={levels['lower']:.2f}, center={levels['center']:.2f}, upper={levels['upper']:.2f}")

    def test_cross_upper_level_sell(self):
        """测试上穿卖出档位"""
        logger.info("[TEST] 测试上穿卖出档位")

        session = GridSession(
            id=1,
            stock_code='000001.SZ',
            center_price=10.0,
            current_center_price=10.0,
            price_interval=0.05,
            callback_ratio=0.005
        )

        tracker = PriceTracker(session_id=1, last_price=10.0)

        self.manager.sessions['000001.SZ'] = session
        self.manager.trackers[1] = tracker

        # 价格上穿卖出档位 (10.5)
        price = 10.6
        self.manager._check_level_crossing(session, tracker, price)

        # 验证追踪器状态
        self.assertEqual(tracker.direction, 'rising')
        self.assertEqual(tracker.crossed_level, 10.5)
        self.assertEqual(tracker.peak_price, 10.6)
        self.assertTrue(tracker.waiting_callback)

        logger.info(f"[PASS] 上穿卖出档位检测成功: crossed_level={tracker.crossed_level:.2f}, peak={tracker.peak_price:.2f}")

    def test_cross_lower_level_buy(self):
        """测试下穿买入档位"""
        logger.info("[TEST] 测试下穿买入档位")

        session = GridSession(
            id=1,
            stock_code='000001.SZ',
            center_price=10.0,
            current_center_price=10.0,
            price_interval=0.05,
            callback_ratio=0.005
        )

        tracker = PriceTracker(session_id=1, last_price=10.0)

        self.manager.sessions['000001.SZ'] = session
        self.manager.trackers[1] = tracker

        # 价格下穿买入档位 (9.5)
        price = 9.4
        self.manager._check_level_crossing(session, tracker, price)

        # 验证追踪器状态
        self.assertEqual(tracker.direction, 'falling')
        self.assertEqual(tracker.crossed_level, 9.5)
        self.assertEqual(tracker.valley_price, 9.4)
        self.assertTrue(tracker.waiting_callback)

        logger.info(f"[PASS] 下穿买入档位检测成功: crossed_level={tracker.crossed_level:.2f}, valley={tracker.valley_price:.2f}")

    def test_price_in_range_no_crossing(self):
        """测试价格在档位区间内不触发穿越"""
        logger.info("[TEST] 测试价格在档位区间内不触发穿越")

        session = GridSession(
            id=1,
            stock_code='000001.SZ',
            center_price=10.0,
            current_center_price=10.0,
            price_interval=0.05
        )

        tracker = PriceTracker(session_id=1, last_price=10.0)

        self.manager.sessions['000001.SZ'] = session
        self.manager.trackers[1] = tracker

        # 价格在 [9.5, 10.5] 区间内
        prices = [9.6, 9.8, 10.0, 10.2, 10.4]

        for price in prices:
            self.manager._check_level_crossing(session, tracker, price)

            # 不应触发穿越
            self.assertFalse(tracker.waiting_callback)
            self.assertIsNone(tracker.direction)

        logger.info("[PASS] 价格在档位区间内不触发穿越")

    def test_level_cooldown_mechanism(self):
        """测试档位冷却机制"""
        logger.info("[TEST] 测试档位冷却机制")

        session = GridSession(
            id=1,
            stock_code='000001.SZ',
            center_price=10.0,
            current_center_price=10.0,
            price_interval=0.05
        )

        tracker = PriceTracker(session_id=1, last_price=10.0)

        self.manager.sessions['000001.SZ'] = session
        self.manager.trackers[1] = tracker

        # 第一次穿越卖出档位
        self.manager._check_level_crossing(session, tracker, 10.6)
        self.assertTrue(tracker.waiting_callback)

        # 记录冷却时间
        cooldown_key = (1, 10.5)
        self.manager.level_cooldowns[cooldown_key] = time.time()

        # 重置追踪器模拟信号已处理
        tracker.reset(10.0)

        # 立即再次穿越同一档位,应被冷却阻止
        self.manager._check_level_crossing(session, tracker, 10.6)
        self.assertFalse(tracker.waiting_callback)  # 冷却期内,不应触发

        logger.info("[PASS] 档位冷却机制生效")

    def test_level_cooldown_expiration(self):
        """测试档位冷却过期后可再次触发"""
        logger.info("[TEST] 测试档位冷却过期后可再次触发")

        session = GridSession(
            id=1,
            stock_code='000001.SZ',
            center_price=10.0,
            current_center_price=10.0,
            price_interval=0.05
        )

        tracker = PriceTracker(session_id=1, last_price=10.0)

        self.manager.sessions['000001.SZ'] = session
        self.manager.trackers[1] = tracker

        # 设置过期的冷却时间 (61秒前)
        cooldown_key = (1, 10.5)
        self.manager.level_cooldowns[cooldown_key] = time.time() - 61

        # 穿越档位,冷却已过期,应触发
        self.manager._check_level_crossing(session, tracker, 10.6)
        self.assertTrue(tracker.waiting_callback)

        logger.info("[PASS] 档位冷却过期后可再次触发")

    def test_already_waiting_callback_no_new_crossing(self):
        """测试已经在等待回调时不再触发新穿越"""
        logger.info("[TEST] 测试已经在等待回调时不再触发新穿越")

        session = GridSession(
            id=1,
            stock_code='000001.SZ',
            center_price=10.0,
            current_center_price=10.0,
            price_interval=0.05
        )

        tracker = PriceTracker(
            session_id=1,
            last_price=10.6,
            peak_price=10.6,
            direction='rising',
            crossed_level=10.5,
            waiting_callback=True
        )

        self.manager.sessions['000001.SZ'] = session
        self.manager.trackers[1] = tracker

        # 价格继续上涨,但已经在等待回调,不应触发新穿越
        self.manager._check_level_crossing(session, tracker, 10.8)

        # 状态应保持不变
        self.assertEqual(tracker.crossed_level, 10.5)  # 档位不变
        self.assertTrue(tracker.waiting_callback)

        logger.info("[PASS] 等待回调期间不触发新穿越")

    def test_dynamic_center_price_adjustment(self):
        """测试中心价动态调整后的档位计算"""
        logger.info("[TEST] 测试中心价动态调整后的档位计算")

        session = GridSession(
            id=1,
            stock_code='000001.SZ',
            center_price=10.0,  # 初始中心价
            current_center_price=11.0,  # 动态调整后的中心价
            price_interval=0.05
        )

        levels = session.get_grid_levels()

        # 应使用 current_center_price 计算档位
        self.assertAlmostEqual(levels['lower'], 10.45, places=2)  # 11.0 * 0.95
        self.assertAlmostEqual(levels['center'], 11.0, places=2)
        self.assertAlmostEqual(levels['upper'], 11.55, places=2)  # 11.0 * 1.05

        logger.info(f"[PASS] 动态中心价档位计算正确: lower={levels['lower']:.2f}, center={levels['center']:.2f}, upper={levels['upper']:.2f}")


def run_tests():
    """运行所有测试"""
    logger.info("=" * 80)
    logger.info("开始网格档位穿越检测测试")
    logger.info("=" * 80)

    # 创建测试套件
    suite = unittest.TestLoader().loadTestsFromTestCase(TestGridLevelCrossing)

    # 运行测试
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    # 输出总结
    logger.info("=" * 80)
    logger.info(f"测试总结:")
    logger.info(f"  - 总测试数: {result.testsRun}")
    logger.info(f"  - 成功: {result.testsRun - len(result.failures) - len(result.errors)}")
    logger.info(f"  - 失败: {len(result.failures)}")
    logger.info(f"  - 错误: {len(result.errors)}")
    logger.info("=" * 80)

    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
