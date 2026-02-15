"""
网格交易 - 信号集成测试

测试完整的价格波动和信号生成流程:
1. 完整的价格波动序列 (震荡、单边、大幅波动)
2. 多次买卖循环
3. 档位重建后信号检测
4. 边界价格处理

使用虚拟环境: C:\\Users\\PC\\Anaconda3\\envs\\python39
"""

import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta
import time
from grid_trading_manager import GridSession, PriceTracker, GridTradingManager
import config
from logger import get_logger

logger = get_logger(__name__)


class TestGridSignalIntegration(unittest.TestCase):
    """网格信号集成测试"""

    def setUp(self):
        """初始化测试环境"""
        logger.info("=" * 80)
        logger.info(f"开始测试: {self._testMethodName}")
        logger.info("=" * 80)

        # Mock 依赖
        self.db_manager = MagicMock()
        self.position_manager = MagicMock()
        self.trading_executor = MagicMock()

        # Mock get_position 返回有效持仓
        self.position_manager.get_position.return_value = {
            'stock_code': '000001.SZ',
            'volume': 1000,
            'current_price': 10.0
        }

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

    def test_oscillating_price_pattern(self):
        """测试震荡行情的信号生成"""
        logger.info("[TEST] 测试震荡行情的信号生成")

        session = GridSession(
            id=1,
            stock_code='000001.SZ',
            center_price=10.0,
            current_center_price=10.0,
            price_interval=0.05,  # ±5%
            callback_ratio=0.005,  # 0.5%
            max_deviation=0.15,
            target_profit=0.10,
            stop_loss=-0.10,
            end_time=datetime.now() + timedelta(days=1)
        )

        tracker = PriceTracker(session_id=1, last_price=10.0)
        self.manager.sessions['000001.SZ'] = session
        self.manager.trackers[1] = tracker

        # 模拟震荡行情: 10.0 -> 10.6 -> 10.54 (卖) -> 9.4 -> 9.45 (买) -> 10.6 -> 10.54 (卖)
        price_sequence = [
            (10.0, None),
            (10.2, None),
            (10.4, None),
            (10.6, None),  # 上穿卖出档位10.5
            (10.7, None),  # 更新峰值
            (10.545, 'SELL'),  # 回调0.5%,触发卖出
            (10.3, None),
            (10.0, None),
            (9.8, None),
            (9.4, None),  # 下穿买入档位9.5
            (9.35, None),  # 更新谷值
            (9.397, 'BUY'),  # 回升0.5%,触发买入
            (9.6, None),
            (10.0, None),
            (10.6, None),  # 再次上穿卖出档位
            (10.545, 'SELL'),  # 再次触发卖出
        ]

        signals_generated = []

        for price, expected_signal in price_sequence:
            signal = self.manager.check_grid_signals('000001.SZ', price)

            if expected_signal:
                self.assertIsNotNone(signal, f"价格{price}应触发{expected_signal}信号")
                self.assertEqual(signal['signal_type'], expected_signal)
                signals_generated.append(signal)
                logger.info(f"  - 价格{price:.2f}: 触发{expected_signal}信号, grid_level={signal['grid_level']:.2f}")

                # 模拟信号处理完成,重置追踪器
                tracker.reset(price)
            else:
                if signal:
                    logger.warning(f"  - 价格{price:.2f}: 意外触发信号 {signal['signal_type']}")
                self.assertIsNone(signal, f"价格{price}不应触发信号")

        # 验证生成了3个信号
        self.assertEqual(len(signals_generated), 3)
        self.assertEqual(signals_generated[0]['signal_type'], 'SELL')
        self.assertEqual(signals_generated[1]['signal_type'], 'BUY')
        self.assertEqual(signals_generated[2]['signal_type'], 'SELL')

        logger.info(f"[PASS] 震荡行情测试通过,共生成{len(signals_generated)}个信号")

    def test_uptrend_price_pattern(self):
        """测试单边上涨行情"""
        logger.info("[TEST] 测试单边上涨行情")

        session = GridSession(
            id=1,
            stock_code='000001.SZ',
            center_price=10.0,
            current_center_price=10.0,
            price_interval=0.05,
            callback_ratio=0.005,
            max_deviation=0.15,
            target_profit=0.10,
            stop_loss=-0.10,
            end_time=datetime.now() + timedelta(days=1)
        )

        tracker = PriceTracker(session_id=1, last_price=10.0)
        self.manager.sessions['000001.SZ'] = session
        self.manager.trackers[1] = tracker

        # 模拟单边上涨: 10.0 -> 11.0 -> 10.945 (卖) -> 12.0 -> 11.94 (卖)
        price_sequence = [
            (10.0, None),
            (10.3, None),
            (10.6, None),  # 上穿第一档10.5
            (10.9, None),
            (11.0, None),  # 峰值
            (10.945, 'SELL'),  # 回调0.5%
            (11.2, None),  # 价格继续上涨
            (11.55, None),  # 上穿新档位11.025 (但中心价可能已调整)
            (12.0, None),
            (11.94, None),  # 可能触发新卖出信号(取决于档位重建)
        ]

        signals_generated = []

        for price, expected_signal in price_sequence:
            signal = self.manager.check_grid_signals('000001.SZ', price)

            if signal:
                signals_generated.append(signal)
                logger.info(f"  - 价格{price:.2f}: 触发{signal['signal_type']}信号, grid_level={signal.get('grid_level', 'N/A'):.2f}")
                tracker.reset(price)

        self.assertGreater(len(signals_generated), 0, "单边上涨应触发至少1个卖出信号")
        self.assertEqual(signals_generated[0]['signal_type'], 'SELL')

        logger.info(f"[PASS] 单边上涨测试通过,共生成{len(signals_generated)}个信号")

    def test_downtrend_price_pattern(self):
        """测试单边下跌行情"""
        logger.info("[TEST] 测试单边下跌行情")

        session = GridSession(
            id=1,
            stock_code='000001.SZ',
            center_price=10.0,
            current_center_price=10.0,
            price_interval=0.05,
            callback_ratio=0.005,
            max_deviation=0.15,
            target_profit=0.10,
            stop_loss=-0.10,
            end_time=datetime.now() + timedelta(days=1)
        )

        tracker = PriceTracker(session_id=1, last_price=10.0)
        self.manager.sessions['000001.SZ'] = session
        self.manager.trackers[1] = tracker

        # 模拟单边下跌: 10.0 -> 9.0 -> 9.045 (买)
        price_sequence = [
            (10.0, None),
            (9.7, None),
            (9.4, None),  # 下穿买入档位9.5
            (9.1, None),
            (9.0, None),  # 谷值
            (9.045, 'BUY'),  # 回升0.5%
        ]

        signals_generated = []

        for price, expected_signal in price_sequence:
            signal = self.manager.check_grid_signals('000001.SZ', price)

            if expected_signal:
                self.assertIsNotNone(signal)
                self.assertEqual(signal['signal_type'], expected_signal)
                signals_generated.append(signal)
                logger.info(f"  - 价格{price:.2f}: 触发{expected_signal}信号")
                tracker.reset(price)

        self.assertEqual(len(signals_generated), 1)
        self.assertEqual(signals_generated[0]['signal_type'], 'BUY')

        logger.info(f"[PASS] 单边下跌测试通过")

    def test_multiple_buy_sell_cycles(self):
        """测试多次买卖循环"""
        logger.info("[TEST] 测试多次买卖循环")

        session = GridSession(
            id=1,
            stock_code='000001.SZ',
            center_price=10.0,
            current_center_price=10.0,
            price_interval=0.05,
            callback_ratio=0.005,
            max_deviation=0.20,  # 更大的偏离容忍度
            target_profit=0.20,  # 较高的止盈目标
            stop_loss=-0.20,
            end_time=datetime.now() + timedelta(days=7)
        )

        tracker = PriceTracker(session_id=1, last_price=10.0)
        self.manager.sessions['000001.SZ'] = session
        self.manager.trackers[1] = tracker

        # 模拟5个完整循环
        cycles = [
            # 循环1
            [(10.6, 'upper'), (10.545, 'SELL'), (9.4, 'lower'), (9.447, 'BUY')],
            # 循环2
            [(10.6, 'upper'), (10.545, 'SELL'), (9.4, 'lower'), (9.447, 'BUY')],
            # 循环3
            [(10.6, 'upper'), (10.545, 'SELL'), (9.4, 'lower'), (9.447, 'BUY')],
        ]

        total_signals = 0

        for cycle_idx, cycle in enumerate(cycles):
            logger.info(f"  --- 循环 {cycle_idx + 1} ---")

            for price, action in cycle:
                signal = self.manager.check_grid_signals('000001.SZ', price)

                if action in ['SELL', 'BUY']:
                    self.assertIsNotNone(signal, f"循环{cycle_idx+1}: 价格{price}应触发{action}信号")
                    self.assertEqual(signal['signal_type'], action)
                    total_signals += 1
                    logger.info(f"    价格{price:.2f}: 触发{action}信号")
                    tracker.reset(price)
                    # 模拟冷却时间经过
                    time.sleep(0.01)

        # 应生成 3个循环 * 2个信号 = 6个信号
        self.assertEqual(total_signals, 6)

        logger.info(f"[PASS] 多次买卖循环测试通过,共{total_signals}个信号")

    def test_level_rebuild_after_deviation(self):
        """测试档位重建后的信号检测"""
        logger.info("[TEST] 测试档位重建后的信号检测")

        session = GridSession(
            id=1,
            stock_code='000001.SZ',
            center_price=10.0,
            current_center_price=10.0,
            price_interval=0.05,
            callback_ratio=0.005,
            max_deviation=0.15,
            target_profit=0.10,
            stop_loss=-0.10,
            end_time=datetime.now() + timedelta(days=1)
        )

        tracker = PriceTracker(session_id=1, last_price=10.0)
        self.manager.sessions['000001.SZ'] = session
        self.manager.trackers[1] = tracker

        # 初始档位: lower=9.5, center=10.0, upper=10.5
        levels = session.get_grid_levels()
        self.assertAlmostEqual(levels['upper'], 10.5, places=2)

        # 模拟档位重建(中心价从10.0调整到11.0)
        logger.info("  - 模拟中心价调整: 10.0 -> 11.0")
        session.current_center_price = 11.0

        # 新档位: lower=10.45, center=11.0, upper=11.55
        new_levels = session.get_grid_levels()
        self.assertAlmostEqual(new_levels['upper'], 11.55, places=2)
        logger.info(f"  - 新档位: lower={new_levels['lower']:.2f}, upper={new_levels['upper']:.2f}")

        # 价格上穿新的卖出档位
        tracker.reset(11.0)
        signal = self.manager.check_grid_signals('000001.SZ', 11.6)
        self.assertIsNone(signal)  # 穿越档位,但未回调

        signal = self.manager.check_grid_signals('000001.SZ', 11.542)
        self.assertIsNotNone(signal)
        self.assertEqual(signal['signal_type'], 'SELL')
        self.assertAlmostEqual(signal['grid_level'], 11.55, places=2)

        logger.info(f"[PASS] 档位重建后信号检测正确")

    def test_extreme_price_volatility(self):
        """测试极端价格波动"""
        logger.info("[TEST] 测试极端价格波动")

        session = GridSession(
            id=1,
            stock_code='000001.SZ',
            center_price=10.0,
            current_center_price=10.0,
            price_interval=0.05,
            callback_ratio=0.005,
            max_deviation=0.30,  # 高容忍度
            target_profit=0.50,
            stop_loss=-0.30,
            end_time=datetime.now() + timedelta(days=1)
        )

        tracker = PriceTracker(session_id=1, last_price=10.0)
        self.manager.sessions['000001.SZ'] = session
        self.manager.trackers[1] = tracker

        # 模拟极端波动: 涨停 -> 跌停
        price_sequence = [
            10.0,
            10.5,  # 上穿
            11.0,  # 涨停
            10.945,  # 回调触发卖出
            10.0,
            9.5,  # 下穿
            9.0,  # 跌停
            9.045,  # 回升触发买入
        ]

        signals = []
        for price in price_sequence:
            signal = self.manager.check_grid_signals('000001.SZ', price)
            if signal:
                signals.append(signal)
                logger.info(f"  - 价格{price:.2f}: 触发{signal['signal_type']}信号")
                tracker.reset(price)

        # 应触发2个信号
        self.assertEqual(len(signals), 2)
        self.assertEqual(signals[0]['signal_type'], 'SELL')
        self.assertEqual(signals[1]['signal_type'], 'BUY')

        logger.info(f"[PASS] 极端波动测试通过")

    def test_boundary_price_handling(self):
        """测试边界价格处理"""
        logger.info("[TEST] 测试边界价格处理")

        session = GridSession(
            id=1,
            stock_code='000001.SZ',
            center_price=10.0,
            current_center_price=10.0,
            price_interval=0.05,
            callback_ratio=0.005,
            max_deviation=0.15,
            target_profit=0.10,
            stop_loss=-0.10,
            end_time=datetime.now() + timedelta(days=1)
        )

        tracker = PriceTracker(session_id=1, last_price=10.0)
        self.manager.sessions['000001.SZ'] = session
        self.manager.trackers[1] = tracker

        # 测试刚好在档位边界
        levels = session.get_grid_levels()

        # 价格刚好等于上档位
        signal = self.manager.check_grid_signals('000001.SZ', levels['upper'])
        self.assertIsNone(signal)  # 等于不触发穿越

        # 价格超过一点点
        signal = self.manager.check_grid_signals('000001.SZ', levels['upper'] + 0.01)
        self.assertIsNone(signal)  # 穿越但未回调

        # 测试回调比例边界
        tracker.peak_price = 10.6
        tracker.direction = 'rising'
        tracker.waiting_callback = True

        # 刚好达到回调阈值
        callback_price = 10.6 * (1 - 0.005)
        tracker.update_price(callback_price)
        signal_type = tracker.check_callback(0.005)
        self.assertEqual(signal_type, 'SELL')

        logger.info(f"[PASS] 边界价格处理正确")


def run_tests():
    """运行所有测试"""
    logger.info("=" * 80)
    logger.info("开始网格信号集成测试")
    logger.info("=" * 80)

    # 创建测试套件
    suite = unittest.TestLoader().loadTestsFromTestCase(TestGridSignalIntegration)

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
