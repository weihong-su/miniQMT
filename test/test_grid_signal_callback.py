"""
网格交易 - 回调触发测试

测试回调机制触发买卖信号:
1. 上涨后回调触发卖出信号
2. 下跌后回调触发买入信号
3. 回调比例配置验证
4. 信号创建完整性检查

使用虚拟环境: C:\\Users\\PC\\Anaconda3\\envs\\python39
"""

import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime
from grid_trading_manager import GridSession, PriceTracker, GridTradingManager
import config
from logger import get_logger

logger = get_logger(__name__)


class TestGridCallback(unittest.TestCase):
    """网格回调触发测试"""

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

    def test_rising_callback_triggers_sell_signal(self):
        """测试上涨回调触发卖出信号"""
        logger.info("[TEST] 测试上涨回调触发卖出信号")

        session = GridSession(
            id=1,
            stock_code='000001.SZ',
            center_price=10.0,
            current_center_price=10.0,
            price_interval=0.05,
            callback_ratio=0.005  # 0.5%回调
        )

        tracker = PriceTracker(
            session_id=1,
            last_price=10.6,
            peak_price=10.6,
            direction='rising',
            crossed_level=10.5,
            waiting_callback=True
        )

        # 价格从峰值10.6回调到10.545 (回调0.52%)
        tracker.update_price(10.545)

        signal_type = tracker.check_callback(session.callback_ratio)

        self.assertEqual(signal_type, 'SELL')

        # 创建完整信号
        signal = self.manager._create_grid_signal(session, tracker, 'SELL', 10.545)

        self.assertEqual(signal['stock_code'], '000001.SZ')
        self.assertEqual(signal['signal_type'], 'SELL')
        self.assertEqual(signal['strategy'], config.GRID_STRATEGY_NAME)
        self.assertEqual(signal['grid_level'], 10.5)
        self.assertEqual(signal['trigger_price'], 10.545)
        self.assertEqual(signal['peak_price'], 10.6)
        self.assertEqual(signal['session_id'], 1)
        self.assertIn('timestamp', signal)

        logger.info(f"[PASS] 上涨回调触发卖出信号: peak={tracker.peak_price:.2f}, trigger={signal['trigger_price']:.2f}, callback={(tracker.peak_price - signal['trigger_price']) / tracker.peak_price * 100:.2f}%")

    def test_falling_callback_triggers_buy_signal(self):
        """测试下跌回调触发买入信号"""
        logger.info("[TEST] 测试下跌回调触发买入信号")

        session = GridSession(
            id=1,
            stock_code='000001.SZ',
            center_price=10.0,
            current_center_price=10.0,
            price_interval=0.05,
            callback_ratio=0.005  # 0.5%回调
        )

        tracker = PriceTracker(
            session_id=1,
            last_price=9.4,
            valley_price=9.4,
            direction='falling',
            crossed_level=9.5,
            waiting_callback=True
        )

        # 价格从谷值9.4回升到9.448 (回升0.51%)
        tracker.update_price(9.448)

        signal_type = tracker.check_callback(session.callback_ratio)

        self.assertEqual(signal_type, 'BUY')

        # 创建完整信号
        signal = self.manager._create_grid_signal(session, tracker, 'BUY', 9.448)

        self.assertEqual(signal['stock_code'], '000001.SZ')
        self.assertEqual(signal['signal_type'], 'BUY')
        self.assertEqual(signal['strategy'], config.GRID_STRATEGY_NAME)
        self.assertEqual(signal['grid_level'], 9.5)
        self.assertEqual(signal['trigger_price'], 9.448)
        self.assertEqual(signal['valley_price'], 9.4)
        self.assertEqual(signal['session_id'], 1)
        self.assertIn('timestamp', signal)

        logger.info(f"[PASS] 下跌回调触发买入信号: valley={tracker.valley_price:.2f}, trigger={signal['trigger_price']:.2f}, callback={(signal['trigger_price'] - tracker.valley_price) / tracker.valley_price * 100:.2f}%")

    def test_insufficient_callback_no_signal(self):
        """测试回调不足不触发信号"""
        logger.info("[TEST] 测试回调不足不触发信号")

        session = GridSession(
            id=1,
            stock_code='000001.SZ',
            callback_ratio=0.005  # 0.5%回调
        )

        # 上涨方向,回调不足
        tracker_rising = PriceTracker(
            session_id=1,
            last_price=10.58,  # 回调0.19%
            peak_price=10.6,
            direction='rising',
            waiting_callback=True
        )

        signal = tracker_rising.check_callback(session.callback_ratio)
        self.assertIsNone(signal)

        # 下跌方向,回调不足
        tracker_falling = PriceTracker(
            session_id=2,
            last_price=9.42,  # 回升0.21%
            valley_price=9.4,
            direction='falling',
            waiting_callback=True
        )

        signal = tracker_falling.check_callback(session.callback_ratio)
        self.assertIsNone(signal)

        logger.info("[PASS] 回调不足时不触发信号")

    def test_callback_ratio_variations(self):
        """测试不同回调比例配置"""
        logger.info("[TEST] 测试不同回调比例配置")

        callback_ratios = [0.003, 0.005, 0.010, 0.020]  # 0.3%, 0.5%, 1.0%, 2.0%

        for ratio in callback_ratios:
            tracker = PriceTracker(
                session_id=1,
                last_price=10.0,
                peak_price=10.0,
                direction='rising',
                waiting_callback=True
            )

            # 计算需要的回调价格
            callback_price = 10.0 * (1 - ratio)

            tracker.update_price(callback_price)
            signal = tracker.check_callback(ratio)

            self.assertEqual(signal, 'SELL')

            logger.info(f"  - 回调比例{ratio*100:.2f}%: peak={tracker.peak_price:.2f}, callback_price={callback_price:.2f}, 触发SELL信号")

        logger.info("[PASS] 不同回调比例配置测试通过")

    def test_signal_timestamp_format(self):
        """测试信号时间戳格式"""
        logger.info("[TEST] 测试信号时间戳格式")

        session = GridSession(
            id=1,
            stock_code='000001.SZ'
        )

        tracker = PriceTracker(
            session_id=1,
            crossed_level=10.5
        )

        signal = self.manager._create_grid_signal(session, tracker, 'SELL', 10.545)

        # 验证时间戳格式
        timestamp = signal['timestamp']
        self.assertIsInstance(timestamp, str)

        # 能解析为 datetime
        dt = datetime.fromisoformat(timestamp)
        self.assertIsInstance(dt, datetime)

        logger.info(f"[PASS] 信号时间戳格式正确: {timestamp}")

    def test_continuous_price_updates_before_callback(self):
        """测试回调触发前的连续价格更新"""
        logger.info("[TEST] 测试回调触发前的连续价格更新")

        tracker = PriceTracker(
            session_id=1,
            last_price=10.0,
            peak_price=10.0,
            direction='rising',
            waiting_callback=True
        )

        callback_ratio = 0.005

        # 价格持续上涨,峰值不断更新
        prices = [10.1, 10.2, 10.3, 10.4, 10.5]

        for price in prices:
            tracker.update_price(price)
            self.assertEqual(tracker.peak_price, price)

            signal = tracker.check_callback(callback_ratio)
            self.assertIsNone(signal)  # 未回调,不触发信号

        # 价格开始回调,但不足
        tracker.update_price(10.48)
        signal = tracker.check_callback(callback_ratio)
        self.assertIsNone(signal)  # 回调0.38%,不足0.5%

        # 继续回调,达到阈值 (peak=10.5, last=10.4475 → 回调0.5%)
        tracker.update_price(10.4475)
        signal = tracker.check_callback(callback_ratio)
        self.assertEqual(signal, 'SELL')  # 回调0.5%,触发信号

        logger.info(f"[PASS] 连续价格更新测试通过: 最终peak={tracker.peak_price:.2f}, last={tracker.last_price:.2f}")

    def test_signal_completeness(self):
        """测试信号包含所有必需字段"""
        logger.info("[TEST] 测试信号包含所有必需字段")

        session = GridSession(
            id=123,
            stock_code='600036.SH',
            center_price=15.0,
            callback_ratio=0.005
        )

        tracker = PriceTracker(
            session_id=123,
            peak_price=16.0,
            valley_price=14.0,
            crossed_level=15.75
        )

        # 创建卖出信号
        sell_signal = self.manager._create_grid_signal(session, tracker, 'SELL', 15.92)

        required_fields = ['stock_code', 'strategy', 'signal_type', 'grid_level', 'trigger_price', 'session_id', 'timestamp']
        for field in required_fields:
            self.assertIn(field, sell_signal, f"信号缺少必需字段: {field}")

        # 卖出信号应包含 peak_price
        self.assertIn('peak_price', sell_signal)
        self.assertEqual(sell_signal['peak_price'], 16.0)

        # 创建买入信号
        buy_signal = self.manager._create_grid_signal(session, tracker, 'BUY', 14.07)

        for field in required_fields:
            self.assertIn(field, buy_signal, f"信号缺少必需字段: {field}")

        # 买入信号应包含 valley_price
        self.assertIn('valley_price', buy_signal)
        self.assertEqual(buy_signal['valley_price'], 14.0)

        logger.info("[PASS] 信号字段完整性验证通过")


def run_tests():
    """运行所有测试"""
    logger.info("=" * 80)
    logger.info("开始网格回调触发测试")
    logger.info("=" * 80)

    # 创建测试套件
    suite = unittest.TestLoader().loadTestsFromTestCase(TestGridCallback)

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
