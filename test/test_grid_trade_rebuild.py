"""
网格重建测试

测试范围:
1. 交易后中心价更新
2. PriceTracker 重置
3. 新档位计算验证
4. 数据库同步
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import unittest
from unittest.mock import Mock
from datetime import datetime
from dataclasses import asdict

import config
from grid_trading_manager import GridSession, GridTradingManager, PriceTracker
from grid_database import DatabaseManager


class TestGridTradeRebuild(unittest.TestCase):
    """网格重建测试"""

    def setUp(self):
        """测试前准备"""
        # 使用内存数据库
        self.db = DatabaseManager(":memory:")
        self.db.init_grid_tables()

        # Mock position_manager 和 trading_executor
        self.position_manager = Mock()
        self.executor = Mock()

        # 创建管理器
        self.manager = GridTradingManager(
            db_manager=self.db,
            position_manager=self.position_manager,
            trading_executor=self.executor
        )

        # 启用模拟模式
        config.ENABLE_SIMULATION_MODE = True

    def tearDown(self):
        """测试后清理"""
        if hasattr(self, 'db') and self.db:
            self.db.close()

    def _create_test_session(self, center_price=10.0, price_interval=0.05):
        """创建测试会话"""
        session = GridSession(
            id=None,
            stock_code="000001.SZ",
            status="active",
            center_price=center_price,
            current_center_price=center_price,
            price_interval=price_interval,
            position_ratio=0.25,
            callback_ratio=0.005,
            max_investment=10000,
            current_investment=0,
            start_time=datetime.now()
        )

        # 插入数据库
        session_dict = asdict(session)
        session.id = self.db.create_grid_session(session_dict)

        self.manager.sessions["000001.SZ"] = session
        self.manager.trackers[session.id] = PriceTracker(
            session_id=session.id,
            last_price=center_price,
            peak_price=center_price,
            valley_price=center_price
        )

        return session

    def test_center_price_update_after_buy(self):
        """测试1: 买入后中心价更新"""
        print("\n========== 测试1: 买入后中心价更新 ==========")

        session = self._create_test_session(center_price=10.0, price_interval=0.05)

        # 初始中心价
        original_center = session.current_center_price
        print(f"  初始中心价: {original_center:.2f}")

        # 执行买入
        buy_signal = {'trigger_price': 9.5, 'grid_level': 'lower'}
        result = self.manager._execute_grid_buy(session, buy_signal)
        self.assertTrue(result)

        # 验证中心价更新
        new_center = session.current_center_price
        self.assertAlmostEqual(new_center, 9.5, places=2,
                              msg="买入后中心价应更新为成交价")

        print(f"  买入后中心价: {new_center:.2f}")
        print(f"[OK] 中心价正确更新: {original_center:.2f} -> {new_center:.2f}")

    def test_center_price_update_after_sell(self):
        """测试2: 卖出后中心价更新"""
        print("\n========== 测试2: 卖出后中心价更新 ==========")

        session = self._create_test_session(center_price=10.0, price_interval=0.05)

        # 先买入建仓
        buy_signal = {'trigger_price': 10.0, 'grid_level': 'center'}
        self.manager._execute_grid_buy(session, buy_signal)

        # 记录买入后中心价
        center_after_buy = session.current_center_price
        print(f"  买入后中心价: {center_after_buy:.2f}")

        # Mock持仓
        position = {'volume': 200, 'cost_price': 10.0}
        self.position_manager.get_position.return_value = position

        # 执行卖出
        sell_signal = {'trigger_price': 10.5, 'grid_level': 'upper'}
        result = self.manager._execute_grid_sell(session, sell_signal)
        self.assertTrue(result)

        # 验证中心价更新
        new_center = session.current_center_price
        self.assertAlmostEqual(new_center, 10.5, places=2,
                              msg="卖出后中心价应更新为成交价")

        print(f"  卖出后中心价: {new_center:.2f}")
        print(f"[OK] 中心价正确更新: {center_after_buy:.2f} -> {new_center:.2f}")

    def test_price_tracker_reset(self):
        """测试3: PriceTracker重置"""
        print("\n========== 测试3: PriceTracker重置 ==========")

        session = self._create_test_session(center_price=10.0)

        # 获取tracker
        tracker = self.manager.trackers[1]

        # 设置tracker状态
        tracker.direction = 'rising'
        tracker.crossed_level = 10.5
        tracker.waiting_callback = True
        tracker.peak_price = 10.6
        tracker.valley_price = 9.8

        print(f"  重置前: direction={tracker.direction}, waiting_callback={tracker.waiting_callback}")
        print(f"         peak={tracker.peak_price:.2f}, valley={tracker.valley_price:.2f}")

        # 执行买入（会触发重建）
        buy_signal = {'trigger_price': 9.5, 'grid_level': 'lower'}
        result = self.manager._execute_grid_buy(session, buy_signal)
        self.assertTrue(result)

        # 验证tracker重置
        self.assertIsNone(tracker.direction, "direction应重置为None")
        self.assertIsNone(tracker.crossed_level, "crossed_level应重置为None")
        self.assertFalse(tracker.waiting_callback, "waiting_callback应重置为False")
        self.assertAlmostEqual(tracker.last_price, 9.5, places=2,
                              msg="last_price应重置为成交价")
        self.assertAlmostEqual(tracker.peak_price, 9.5, places=2,
                              msg="peak_price应重置为成交价")
        self.assertAlmostEqual(tracker.valley_price, 9.5, places=2,
                              msg="valley_price应重置为成交价")

        print(f"  重置后: direction={tracker.direction}, waiting_callback={tracker.waiting_callback}")
        print(f"         last={tracker.last_price:.2f}, peak={tracker.peak_price:.2f}, valley={tracker.valley_price:.2f}")
        print(f"[OK] PriceTracker正确重置")

    def test_grid_levels_recalculation(self):
        """测试4: 新档位计算验证"""
        print("\n========== 测试4: 新档位计算验证 ==========")

        session = self._create_test_session(center_price=10.0, price_interval=0.05)

        # 初始档位
        old_levels = session.get_grid_levels()
        print(f"  初始档位: lower={old_levels['lower']:.2f}, center={old_levels['center']:.2f}, upper={old_levels['upper']:.2f}")

        # 执行买入，成交价9.5
        buy_signal = {'trigger_price': 9.5, 'grid_level': 'lower'}
        result = self.manager._execute_grid_buy(session, buy_signal)
        self.assertTrue(result)

        # 新档位
        new_levels = session.get_grid_levels()
        print(f"  新档位: lower={new_levels['lower']:.2f}, center={new_levels['center']:.2f}, upper={new_levels['upper']:.2f}")

        # 验证新档位
        expected_center = 9.5
        expected_lower = 9.5 * (1 - 0.05)  # 9.025
        expected_upper = 9.5 * (1 + 0.05)  # 9.975

        self.assertAlmostEqual(new_levels['center'], expected_center, places=2,
                              msg="新中心价应为成交价")
        self.assertAlmostEqual(new_levels['lower'], expected_lower, places=2,
                              msg="新下档应正确计算")
        self.assertAlmostEqual(new_levels['upper'], expected_upper, places=2,
                              msg="新上档应正确计算")

        print(f"[OK] 新档位正确计算")

    def test_database_sync_after_rebuild(self):
        """测试5: 重建后数据库同步"""
        print("\n========== 测试5: 重建后数据库同步 ==========")

        session = self._create_test_session(center_price=10.0)

        # 执行买入
        buy_signal = {'trigger_price': 9.8, 'grid_level': 'lower'}
        result = self.manager._execute_grid_buy(session, buy_signal)
        self.assertTrue(result)

        # 从数据库读取
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT current_center_price FROM grid_trading_sessions WHERE id=1")
        db_center = cursor.fetchone()['current_center_price']

        # 验证同步
        self.assertAlmostEqual(db_center, session.current_center_price, places=2,
                              msg="数据库current_center_price应与内存同步")
        self.assertAlmostEqual(db_center, 9.8, places=2,
                              msg="数据库应记录新中心价")

        print(f"  内存中心价: {session.current_center_price:.2f}")
        print(f"  数据库中心价: {db_center:.2f}")
        print(f"[OK] 数据库正确同步")

    def test_multiple_trades_center_evolution(self):
        """测试6: 多次交易中心价演化"""
        print("\n========== 测试6: 多次交易中心价演化 ==========")

        session = self._create_test_session(center_price=10.0, price_interval=0.05)

        centers = [session.current_center_price]
        print(f"  初始中心价: {centers[0]:.2f}")

        # 模拟多次交易
        trades = [
            ('BUY', 9.8),
            ('SELL', 10.2),
            ('BUY', 9.9),
            ('SELL', 10.3),
        ]

        position = {'volume': 200, 'cost_price': 10.0}
        self.position_manager.get_position.return_value = position

        for i, (trade_type, price) in enumerate(trades, 1):
            if trade_type == 'BUY':
                signal = {'trigger_price': price, 'grid_level': 'lower'}
                result = self.manager._execute_grid_buy(session, signal)
            else:
                session.position_ratio = 1.0
                signal = {'trigger_price': price, 'grid_level': 'upper'}
                result = self.manager._execute_grid_sell(session, signal)

            self.assertTrue(result)
            centers.append(session.current_center_price)
            print(f"  第{i}次交易({trade_type}): 成交价={price:.2f}, 新中心价={session.current_center_price:.2f}")

        # 验证每次交易后中心价都更新
        for i, (trade_type, price) in enumerate(trades):
            self.assertAlmostEqual(centers[i+1], price, places=2,
                                  msg=f"第{i+1}次交易后中心价应为{price}")

        print(f"[OK] 中心价演化路径: {' -> '.join([f'{c:.2f}' for c in centers])}")

    def test_grid_levels_with_different_intervals(self):
        """测试7: 不同档位间隔的新档位计算"""
        print("\n========== 测试7: 不同档位间隔的新档位计算 ==========")

        test_intervals = [0.03, 0.05, 0.10]

        for interval in test_intervals:
            session = self._create_test_session(center_price=10.0, price_interval=interval)

            # 执行买入
            buy_signal = {'trigger_price': 9.5, 'grid_level': 'lower'}
            self.manager._execute_grid_buy(session, buy_signal)

            # 验证新档位
            levels = session.get_grid_levels()
            expected_lower = 9.5 * (1 - interval)
            expected_upper = 9.5 * (1 + interval)

            self.assertAlmostEqual(levels['lower'], expected_lower, places=2)
            self.assertAlmostEqual(levels['upper'], expected_upper, places=2)

            print(f"  间隔{interval*100:.0f}%: lower={levels['lower']:.2f}, center={levels['center']:.2f}, upper={levels['upper']:.2f}")

        print(f"[OK] 不同间隔的档位计算正确")

    def test_rebuild_preserves_session_statistics(self):
        """测试8: 重建不影响会话统计"""
        print("\n========== 测试8: 重建不影响会话统计 ==========")

        session = self._create_test_session(center_price=10.0)

        # 执行买入
        buy_signal = {'trigger_price': 9.8, 'grid_level': 'lower'}
        self.manager._execute_grid_buy(session, buy_signal)

        # 记录统计
        trade_count = session.trade_count
        buy_count = session.buy_count
        total_buy = session.total_buy_amount
        investment = session.current_investment

        print(f"  重建后统计: trade_count={trade_count}, buy_count={buy_count}")
        print(f"            total_buy={total_buy:.2f}, investment={investment:.2f}")

        # 验证统计未被重置
        self.assertEqual(session.trade_count, 1, "trade_count应保持")
        self.assertEqual(session.buy_count, 1, "buy_count应保持")
        self.assertGreater(session.total_buy_amount, 0, "total_buy_amount应保持")
        self.assertGreater(session.current_investment, 0, "current_investment应保持")

        # 验证只有中心价和tracker被重置
        self.assertAlmostEqual(session.current_center_price, 9.8, places=2,
                              msg="中心价应更新")

        tracker = self.manager.trackers[1]
        self.assertIsNone(tracker.direction, "tracker.direction应重置")
        self.assertFalse(tracker.waiting_callback, "tracker.waiting_callback应重置")

        print(f"[OK] 重建只更新中心价和tracker,不影响统计")


def run_tests():
    """运行所有测试"""
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestGridTradeRebuild)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    return result


if __name__ == '__main__':
    print("=" * 80)
    print("网格重建测试")
    print("=" * 80)

    result = run_tests()

    print("\n" + "=" * 80)
    print("测试汇总:")
    print(f"总测试数: {result.testsRun}")
    print(f"成功: {result.testsRun - len(result.failures) - len(result.errors)}")
    print(f"失败: {len(result.failures)}")
    print(f"错误: {len(result.errors)}")
    print("=" * 80)

    sys.exit(0 if result.wasSuccessful() else 1)
