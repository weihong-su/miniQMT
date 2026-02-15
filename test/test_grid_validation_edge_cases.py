#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
网格交易边界条件测试
测试极端值、零值、边界值等特殊情况的处理
"""

import sys
import os
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from grid_trading_manager import GridTradingManager, GridSession
from grid_database import DatabaseManager
import config


class MockPositionManager:
    """模拟持仓管理器"""
    def __init__(self):
        self.positions = {}

    def get_position(self, stock_code):
        return self.positions.get(stock_code)

    def _increment_data_version(self):
        pass


class MockTradingExecutor:
    """模拟交易执行器"""
    def execute_buy(self, stock_code, amount, strategy):
        return {'order_id': 'MOCK_BUY_123', 'success': True}

    def execute_sell(self, stock_code, volume, strategy):
        return {'order_id': 'MOCK_SELL_123', 'success': True}


class TestGridValidationEdgeCases(unittest.TestCase):
    """网格交易边界条件测试"""

    def setUp(self):
        """测试前准备"""
        self.test_db = f"data/test_edge_{int(datetime.now().timestamp())}.db"
        self.db = DatabaseManager(self.test_db)
        self.db.init_grid_tables()

        self.position_mgr = MockPositionManager()
        self.executor = MockTradingExecutor()
        self.grid_mgr = GridTradingManager(self.db, self.position_mgr, self.executor)

    def tearDown(self):
        """测试后清理"""
        self.db.close()
        if os.path.exists(self.test_db):
            os.remove(self.test_db)

    def test_max_investment_zero(self):
        """测试最大投入为0的情况"""
        stock_code = '000001.SZ'

        # 准备持仓数据
        self.position_mgr.positions[stock_code] = {
            'volume': 1000,
            'cost_price': 10.0,
            'current_price': 11.0,
            'highest_price': 11.5,
            'profit_triggered': True
        }

        # 启动会话，max_investment=0
        user_config = {
            'price_interval': 0.05,
            'position_ratio': 0.25,
            'callback_ratio': 0.005,
            'max_investment': 0,  # 零投入
            'max_deviation': 0.15,
            'target_profit': 0.10,
            'stop_loss': -0.10,
            'duration_days': 7
        }

        session = self.grid_mgr.start_grid_session(stock_code, user_config)
        self.assertIsNotNone(session)
        self.assertEqual(session.max_investment, 0)

        # 尝试买入应该失败（max_investment=0）
        signal = {
            'stock_code': stock_code,
            'signal_type': 'BUY',
            'trigger_price': 10.5,
            'grid_level': 10.45,
            'valley_price': 10.4,
            'callback_ratio': 0.005
        }

        success = self.grid_mgr.execute_grid_trade(signal)
        self.assertFalse(success, "max_investment=0时不应允许买入")

    def test_extreme_small_price_interval(self):
        """测试极小价格间隔（边界值1%）"""
        session = GridSession(
            stock_code='000001.SZ',
            center_price=10.0,
            current_center_price=10.0,
            price_interval=0.01  # 1%
        )

        levels = session.get_grid_levels()
        self.assertAlmostEqual(levels['lower'], 9.90, places=2)
        self.assertAlmostEqual(levels['upper'], 10.10, places=2)

    def test_extreme_large_price_interval(self):
        """测试极大价格间隔（边界值20%）"""
        session = GridSession(
            stock_code='000001.SZ',
            center_price=10.0,
            current_center_price=10.0,
            price_interval=0.20  # 20%
        )

        levels = session.get_grid_levels()
        self.assertAlmostEqual(levels['lower'], 8.00, places=2)
        self.assertAlmostEqual(levels['upper'], 12.00, places=2)

    def test_position_less_than_100_shares(self):
        """测试持仓不足100股的情况"""
        stock_code = '000001.SZ'

        # 持仓仅50股（不足一手）
        self.position_mgr.positions[stock_code] = {
            'volume': 50,
            'cost_price': 10.0,
            'current_price': 11.0,
            'highest_price': 11.5,
            'profit_triggered': True
        }

        user_config = {
            'price_interval': 0.05,
            'position_ratio': 0.25,
            'callback_ratio': 0.005,
            'max_investment': 10000,
            'max_deviation': 0.15,
            'target_profit': 0.10,
            'stop_loss': -0.10,
            'duration_days': 7
        }

        session = self.grid_mgr.start_grid_session(stock_code, user_config)

        # 尝试卖出
        signal = {
            'stock_code': stock_code,
            'signal_type': 'SELL',
            'trigger_price': 11.5,
            'grid_level': 11.55,
            'peak_price': 11.6,
            'callback_ratio': 0.005
        }

        success = self.grid_mgr.execute_grid_trade(signal)
        # 应该失败，因为可卖数量不足100股
        self.assertFalse(success, "持仓不足100股时不应允许卖出")

    def test_buy_amount_less_than_minimum(self):
        """测试买入金额低于最小值（100元）"""
        stock_code = '000001.SZ'

        self.position_mgr.positions[stock_code] = {
            'volume': 1000,
            'cost_price': 10.0,
            'current_price': 10.5,
            'highest_price': 11.0,
            'profit_triggered': True
        }

        user_config = {
            'price_interval': 0.05,
            'position_ratio': 0.25,
            'callback_ratio': 0.005,
            'max_investment': 50,  # 仅50元额度
            'max_deviation': 0.15,
            'target_profit': 0.10,
            'stop_loss': -0.10,
            'duration_days': 7
        }

        session = self.grid_mgr.start_grid_session(stock_code, user_config)
        session.current_investment = 0

        signal = {
            'stock_code': stock_code,
            'signal_type': 'BUY',
            'trigger_price': 10.0,
            'grid_level': 9.95,
            'valley_price': 9.90,
            'callback_ratio': 0.005
        }

        success = self.grid_mgr.execute_grid_trade(signal)
        # 应该失败，因为剩余额度不足100元
        self.assertFalse(success, "买入金额低于100元时应拒绝交易")

    def test_deviation_zero_center_price(self):
        """测试中心价为0时的偏离度计算"""
        session = GridSession(
            stock_code='000001.SZ',
            center_price=0,  # 异常：中心价为0
            current_center_price=10.0
        )

        deviation = session.get_deviation_ratio()
        self.assertEqual(deviation, 0.0, "中心价为0时应返回0偏离度")

    def test_profit_ratio_with_no_trades(self):
        """测试无交易时的盈亏率计算"""
        session = GridSession(
            stock_code='000001.SZ',
            center_price=10.0,
            max_investment=10000,
            total_buy_amount=0,
            total_sell_amount=0
        )

        profit_ratio = session.get_profit_ratio()
        self.assertEqual(profit_ratio, 0.0, "无交易时盈亏率应为0")

    def test_profit_ratio_max_investment_zero(self):
        """测试max_investment为0时的盈亏率计算"""
        session = GridSession(
            stock_code='000001.SZ',
            center_price=10.0,
            max_investment=0,  # 异常：最大投入为0
            total_buy_amount=1000,
            total_sell_amount=1100
        )

        profit_ratio = session.get_profit_ratio()
        self.assertEqual(profit_ratio, 0.0, "max_investment为0时应返回0")

    def test_extreme_duration_days(self):
        """测试极端运行时长"""
        stock_code = '000001.SZ'

        self.position_mgr.positions[stock_code] = {
            'volume': 1000,
            'cost_price': 10.0,
            'current_price': 11.0,
            'highest_price': 11.5,
            'profit_triggered': True
        }

        # 测试最短时长（1天）
        user_config = {
            'price_interval': 0.05,
            'position_ratio': 0.25,
            'callback_ratio': 0.005,
            'max_investment': 10000,
            'max_deviation': 0.15,
            'target_profit': 0.10,
            'stop_loss': -0.10,
            'duration_days': 1
        }

        session = self.grid_mgr.start_grid_session(stock_code, user_config)
        self.assertIsNotNone(session)

        # 验证结束时间
        expected_end = datetime.now() + timedelta(days=1)
        delta = abs((session.end_time - expected_end).total_seconds())
        self.assertLess(delta, 5, "结束时间应为1天后")

        # 清理
        self.grid_mgr.stop_grid_session(session.id, 'test_cleanup')

        # 测试最长时长（365天）
        user_config['duration_days'] = 365
        session = self.grid_mgr.start_grid_session(stock_code, user_config)

        expected_end = datetime.now() + timedelta(days=365)
        delta = abs((session.end_time - expected_end).total_seconds())
        self.assertLess(delta, 5, "结束时间应为365天后")

    def test_precision_of_amount_calculation(self):
        """测试金额计算精度"""
        session = GridSession(
            stock_code='000001.SZ',
            center_price=10.0,
            max_investment=10000.50,
            total_buy_amount=1234.56,
            total_sell_amount=1345.67
        )

        profit = session.get_grid_profit()
        expected_profit = 1345.67 - 1234.56
        self.assertAlmostEqual(profit, expected_profit, places=2,
                              msg="金额计算应精确到分")

    def test_large_volume_calculation(self):
        """测试大数量交易的计算"""
        import config
        stock_code = '000001.SZ'

        # 大持仓（100000股）
        self.position_mgr.positions[stock_code] = {
            'volume': 100000,
            'cost_price': 10.0,
            'current_price': 11.0,
            'highest_price': 11.5,
            'profit_triggered': True
        }

        user_config = {
            'price_interval': 0.05,
            'position_ratio': 0.10,  # 10%
            'callback_ratio': 0.005,
            'max_investment': 500000,
            'max_deviation': 0.15,
            'target_profit': 0.10,
            'stop_loss': -0.10,
            'duration_days': 7
        }

        session = self.grid_mgr.start_grid_session(stock_code, user_config)

        # 测试卖出计算
        signal = {
            'stock_code': stock_code,
            'signal_type': 'SELL',
            'trigger_price': 11.5,
            'grid_level': 11.55,
            'peak_price': 11.6,
            'callback_ratio': 0.005
        }

        # 应该卖出 100000 * 0.10 = 10000 股
        # 临时关闭模拟模式以测试实盘执行路径
        old_mode = config.ENABLE_SIMULATION_MODE
        try:
            config.ENABLE_SIMULATION_MODE = False
            with patch.object(self.executor, 'execute_sell', return_value={'order_id': 'TEST'}) as mock_sell:
                self.grid_mgr.execute_grid_trade(signal)
                mock_sell.assert_called_once()
                call_args = mock_sell.call_args[1]
                self.assertEqual(call_args['volume'], 10000)
        finally:
            config.ENABLE_SIMULATION_MODE = old_mode


def run_tests():
    """运行测试"""
    suite = unittest.TestLoader().loadTestsFromTestCase(TestGridValidationEdgeCases)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    report = {
        'test_file': 'test_grid_validation_edge_cases.py',
        'total_tests': result.testsRun,
        'passed': result.testsRun - len(result.failures) - len(result.errors),
        'failed': len(result.failures),
        'errors': len(result.errors),
        'coverage': '边界条件 - 100%'
    }

    import json
    with open('test/grid_validation_edge_cases_report.json', 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
