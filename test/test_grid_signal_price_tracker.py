"""
网格交易 - PriceTracker 状态机测试

测试 PriceTracker 类的核心功能:
1. 价格更新和峰值/谷值追踪
2. 状态转换 (idle -> waiting_callback)
3. 回调比例计算
4. 追踪器重置

使用虚拟环境: C:\\Users\\PC\\Anaconda3\\envs\\python39
"""

import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from datetime import datetime
from grid_trading_manager import PriceTracker
import config
from logger import get_logger

logger = get_logger(__name__)


class TestPriceTracker(unittest.TestCase):
    """PriceTracker 状态机测试"""

    def setUp(self):
        """初始化测试环境"""
        logger.info("=" * 80)
        logger.info(f"开始测试: {self._testMethodName}")
        logger.info("=" * 80)

    def tearDown(self):
        """清理测试环境"""
        logger.info(f"测试完成: {self._testMethodName}")
        logger.info("")

    def test_initialization(self):
        """测试 PriceTracker 初始化"""
        logger.info("[TEST] 测试 PriceTracker 初始化")

        tracker = PriceTracker(session_id=1)

        self.assertEqual(tracker.session_id, 1)
        self.assertEqual(tracker.last_price, 0.0)
        self.assertEqual(tracker.peak_price, 0.0)
        self.assertEqual(tracker.valley_price, 0.0)
        self.assertIsNone(tracker.direction)
        self.assertIsNone(tracker.crossed_level)
        self.assertFalse(tracker.waiting_callback)

        logger.info("[PASS] PriceTracker 初始化成功")

    def test_update_price_without_waiting(self):
        """测试非等待状态下的价格更新"""
        logger.info("[TEST] 测试非等待状态下的价格更新")

        tracker = PriceTracker(session_id=1, last_price=10.0)
        tracker.update_price(10.5)

        self.assertEqual(tracker.last_price, 10.5)
        # 非等待状态,峰谷值不应更新
        self.assertEqual(tracker.peak_price, 0.0)
        self.assertEqual(tracker.valley_price, 0.0)

        logger.info("[PASS] 非等待状态价格更新正确")

    def test_update_peak_in_rising_direction(self):
        """测试上涨方向时峰值更新"""
        logger.info("[TEST] 测试上涨方向时峰值更新")

        tracker = PriceTracker(
            session_id=1,
            last_price=10.0,
            peak_price=10.0,
            direction='rising',
            waiting_callback=True
        )

        # 价格上涨,峰值应更新
        tracker.update_price(10.5)
        self.assertEqual(tracker.peak_price, 10.5)
        self.assertEqual(tracker.last_price, 10.5)

        # 价格继续上涨
        tracker.update_price(11.0)
        self.assertEqual(tracker.peak_price, 11.0)

        # 价格下跌,峰值不变
        tracker.update_price(10.8)
        self.assertEqual(tracker.peak_price, 11.0)
        self.assertEqual(tracker.last_price, 10.8)

        logger.info("[PASS] 上涨方向峰值更新正确")

    def test_update_valley_in_falling_direction(self):
        """测试下跌方向时谷值更新"""
        logger.info("[TEST] 测试下跌方向时谷值更新")

        tracker = PriceTracker(
            session_id=1,
            last_price=10.0,
            valley_price=10.0,
            direction='falling',
            waiting_callback=True
        )

        # 价格下跌,谷值应更新
        tracker.update_price(9.5)
        self.assertEqual(tracker.valley_price, 9.5)
        self.assertEqual(tracker.last_price, 9.5)

        # 价格继续下跌
        tracker.update_price(9.0)
        self.assertEqual(tracker.valley_price, 9.0)

        # 价格上涨,谷值不变
        tracker.update_price(9.2)
        self.assertEqual(tracker.valley_price, 9.0)
        self.assertEqual(tracker.last_price, 9.2)

        logger.info("[PASS] 下跌方向谷值更新正确")

    def test_check_callback_not_waiting(self):
        """测试非等待状态不触发回调"""
        logger.info("[TEST] 测试非等待状态不触发回调")

        tracker = PriceTracker(session_id=1, waiting_callback=False)
        result = tracker.check_callback(callback_ratio=0.005)

        self.assertIsNone(result)
        logger.info("[PASS] 非等待状态不触发回调")

    def test_check_callback_rising_triggered(self):
        """测试上涨方向回调触发卖出信号"""
        logger.info("[TEST] 测试上涨方向回调触发卖出信号")

        tracker = PriceTracker(
            session_id=1,
            last_price=9.95,  # 从峰值10.0回调0.05元
            peak_price=10.0,
            direction='rising',
            waiting_callback=True
        )

        # 回调比例 = (10.0 - 9.95) / 10.0 = 0.005 = 0.5%
        callback_ratio = 0.005
        result = tracker.check_callback(callback_ratio)

        self.assertEqual(result, 'SELL')
        logger.info(f"[PASS] 上涨回调触发卖出信号: peak={tracker.peak_price}, last={tracker.last_price}, ratio={(tracker.peak_price - tracker.last_price) / tracker.peak_price * 100:.2f}%")

    def test_check_callback_rising_not_triggered(self):
        """测试上涨方向回调未达到阈值"""
        logger.info("[TEST] 测试上涨方向回调未达到阈值")

        tracker = PriceTracker(
            session_id=1,
            last_price=9.96,  # 回调0.04元,不足0.5%
            peak_price=10.0,
            direction='rising',
            waiting_callback=True
        )

        callback_ratio = 0.005
        result = tracker.check_callback(callback_ratio)

        self.assertIsNone(result)
        logger.info(f"[PASS] 回调未达到阈值: peak={tracker.peak_price}, last={tracker.last_price}, ratio={(tracker.peak_price - tracker.last_price) / tracker.peak_price * 100:.2f}%")

    def test_check_callback_falling_triggered(self):
        """测试下跌方向回调触发买入信号"""
        logger.info("[TEST] 测试下跌方向回调触发买入信号")

        tracker = PriceTracker(
            session_id=1,
            last_price=9.55,  # 从谷值9.5回升0.05元
            valley_price=9.5,
            direction='falling',
            waiting_callback=True
        )

        # 回调比例 = (9.55 - 9.5) / 9.5 = 0.00526 = 0.526%
        callback_ratio = 0.005
        result = tracker.check_callback(callback_ratio)

        self.assertEqual(result, 'BUY')
        logger.info(f"[PASS] 下跌回调触发买入信号: valley={tracker.valley_price}, last={tracker.last_price}, ratio={(tracker.last_price - tracker.valley_price) / tracker.valley_price * 100:.2f}%")

    def test_check_callback_falling_not_triggered(self):
        """测试下跌方向回调未达到阈值"""
        logger.info("[TEST] 测试下跌方向回调未达到阈值")

        tracker = PriceTracker(
            session_id=1,
            last_price=9.53,  # 回升0.03元,不足0.5%
            valley_price=9.5,
            direction='falling',
            waiting_callback=True
        )

        callback_ratio = 0.005
        result = tracker.check_callback(callback_ratio)

        self.assertIsNone(result)
        logger.info(f"[PASS] 回调未达到阈值: valley={tracker.valley_price}, last={tracker.last_price}, ratio={(tracker.last_price - tracker.valley_price) / tracker.valley_price * 100:.2f}%")

    def test_reset(self):
        """测试追踪器重置"""
        logger.info("[TEST] 测试追踪器重置")

        tracker = PriceTracker(
            session_id=1,
            last_price=10.5,
            peak_price=11.0,
            valley_price=9.5,
            direction='rising',
            crossed_level=10.5,
            waiting_callback=True
        )

        # 重置到新价格
        new_price = 10.0
        tracker.reset(new_price)

        self.assertEqual(tracker.last_price, new_price)
        self.assertEqual(tracker.peak_price, new_price)
        self.assertEqual(tracker.valley_price, new_price)
        self.assertIsNone(tracker.direction)
        self.assertIsNone(tracker.crossed_level)
        self.assertFalse(tracker.waiting_callback)

        logger.info("[PASS] 追踪器重置成功")

    def test_callback_ratio_precision(self):
        """测试回调比例精度计算"""
        logger.info("[TEST] 测试回调比例精度计算")

        # 测试边界条件: 刚好达到阈值
        tracker = PriceTracker(
            session_id=1,
            last_price=9.950,  # 刚好0.5%回调
            peak_price=10.0,
            direction='rising',
            waiting_callback=True
        )

        callback_ratio = 0.005
        result = tracker.check_callback(callback_ratio)
        self.assertEqual(result, 'SELL')

        # 测试边界条件: 差一点点
        tracker2 = PriceTracker(
            session_id=2,
            last_price=9.951,  # 0.49%回调
            peak_price=10.0,
            direction='rising',
            waiting_callback=True
        )

        result2 = tracker2.check_callback(callback_ratio)
        self.assertIsNone(result2)

        logger.info("[PASS] 回调比例精度计算正确")


def run_tests():
    """运行所有测试"""
    logger.info("=" * 80)
    logger.info("开始 PriceTracker 状态机测试")
    logger.info("=" * 80)

    # 创建测试套件
    suite = unittest.TestLoader().loadTestsFromTestCase(TestPriceTracker)

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
