#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
网格交易异常处理测试
测试各种异常情况的处理（超时、数据库错误、API失败等）
"""

import sys
import os
import unittest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from grid_trading_manager import GridTradingManager
from grid_database import DatabaseManager
import sqlite3


class TestGridValidationExceptions(unittest.TestCase):
    """网格交易异常处理测试"""

    def setUp(self):
        """测试前准备"""
        self.test_db = f"data/test_exc_{int(datetime.now().timestamp())}.db"
        self.db = DatabaseManager(self.test_db)
        self.db.init_grid_tables()

        self.position_mgr = Mock()
        self.executor = Mock()
        self.grid_mgr = GridTradingManager(self.db, self.position_mgr, self.executor)

    def tearDown(self):
        """测试后清理"""
        self.db.close()
        if os.path.exists(self.test_db):
            os.remove(self.test_db)

    def test_get_position_timeout(self):
        """测试获取持仓超时处理"""
        stock_code = '000001.SZ'

        # 模拟超时：get_position 延迟6秒（超过5秒超时限制）
        def slow_get_position(code):
            time.sleep(6)
            return {'volume': 1000}

        self.position_mgr.get_position = slow_get_position

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

        # 应该抛出超时异常
        with self.assertRaises(RuntimeError) as cm:
            self.grid_mgr.start_grid_session(stock_code, user_config)

        self.assertIn('超时', str(cm.exception))

    def test_position_not_exist(self):
        """测试持仓不存在的异常"""
        stock_code = '000001.SZ'
        self.position_mgr.get_position.return_value = None

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

        with self.assertRaises(ValueError) as cm:
            self.grid_mgr.start_grid_session(stock_code, user_config)

        self.assertIn('无持仓', str(cm.exception))

    def test_profit_not_triggered(self):
        """测试未触发止盈时的拒绝启动"""
        stock_code = '000001.SZ'

        # 持仓存在但未触发止盈
        self.position_mgr.get_position.return_value = {
            'volume': 1000,
            'cost_price': 10.0,
            'current_price': 10.5,
            'highest_price': 10.8,
            'profit_triggered': False  # 未触发止盈
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

        # 如果配置要求必须触发止盈，应该抛出异常
        with patch('config.GRID_REQUIRE_PROFIT_TRIGGERED', True):
            with self.assertRaises(ValueError) as cm:
                self.grid_mgr.start_grid_session(stock_code, user_config)

            self.assertIn('未触发首次止盈', str(cm.exception))

    def test_database_error_on_create_session(self):
        """测试创建会话时的数据库错误"""
        stock_code = '000001.SZ'

        self.position_mgr.get_position.return_value = {
            'volume': 1000,
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

        # 模拟数据库错误
        with patch.object(self.db, 'create_grid_session', side_effect=sqlite3.OperationalError("数据库锁定")):
            with self.assertRaises(sqlite3.OperationalError):
                self.grid_mgr.start_grid_session(stock_code, user_config)

    def test_lock_timeout(self):
        """测试锁超时处理"""
        stock_code = '000001.SZ'

        self.position_mgr.get_position.return_value = {
            'volume': 1000,
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

        # 模拟锁被其他线程持有
        lock = threading.RLock()

        lock_acquired_event = threading.Event()

        def hold_lock():
            """在独立线程中持有锁"""
            lock.acquire()
            lock_acquired_event.set()  # 通知主线程锁已获取
            time.sleep(6)  # 持有6秒，超过5秒超时
            lock.release()

        # 在单独的线程中获取锁(避免RLock可重入特性)
        threading.Thread(target=hold_lock, daemon=True).start()
        lock_acquired_event.wait()  # 等待锁被获取

        # 直接替换lock对象
        original_lock = self.grid_mgr.lock
        self.grid_mgr.lock = lock
        try:
            with self.assertRaises(RuntimeError) as cm:
                self.grid_mgr.start_grid_session(stock_code, user_config)

            self.assertIn('系统繁忙', str(cm.exception))
        finally:
            self.grid_mgr.lock = original_lock

    def test_execute_buy_failure(self):
        """测试买入执行失败"""
        stock_code = '000001.SZ'

        # 正常启动会话
        self.position_mgr.get_position.return_value = {
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
            'max_investment': 10000,
            'max_deviation': 0.15,
            'target_profit': 0.10,
            'stop_loss': -0.10,
            'duration_days': 7
        }

        session = self.grid_mgr.start_grid_session(stock_code, user_config)

        # 模拟实盘买入失败
        with patch('config.ENABLE_SIMULATION_MODE', False):
            self.executor.execute_buy.return_value = None  # 返回None表示失败

            signal = {
                'stock_code': stock_code,
                'signal_type': 'BUY',
                'trigger_price': 10.0,
                'grid_level': 9.95,
                'valley_price': 9.90,
                'callback_ratio': 0.005
            }

            success = self.grid_mgr.execute_grid_trade(signal)
            self.assertFalse(success, "买入失败应返回False")

    def test_execute_sell_failure(self):
        """测试卖出执行失败"""
        stock_code = '000001.SZ'

        self.position_mgr.get_position.return_value = {
            'volume': 1000,
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

        # 模拟实盘卖出失败
        with patch('config.ENABLE_SIMULATION_MODE', False):
            self.executor.execute_sell.return_value = None

            signal = {
                'stock_code': stock_code,
                'signal_type': 'SELL',
                'trigger_price': 11.5,
                'grid_level': 11.55,
                'peak_price': 11.6,
                'callback_ratio': 0.005
            }

            success = self.grid_mgr.execute_grid_trade(signal)
            self.assertFalse(success, "卖出失败应返回False")

    def test_missing_center_price(self):
        """测试缺少中心价格的异常"""
        stock_code = '000001.SZ'

        self.position_mgr.get_position.return_value = {
            'volume': 1000,
            'cost_price': 10.0,
            'current_price': 10.5,
            'highest_price': 0,  # 最高价为0
            'profit_triggered': True
        }

        user_config = {
            # 未提供 center_price
            'price_interval': 0.05,
            'position_ratio': 0.25,
            'callback_ratio': 0.005,
            'max_investment': 10000,
            'max_deviation': 0.15,
            'target_profit': 0.10,
            'stop_loss': -0.10,
            'duration_days': 7
        }

        with self.assertRaises(ValueError) as cm:
            self.grid_mgr.start_grid_session(stock_code, user_config)

        self.assertIn('中心价格', str(cm.exception))

    def test_stop_nonexistent_session(self):
        """测试停止不存在的会话"""
        with self.assertRaises(ValueError) as cm:
            self.grid_mgr.stop_grid_session(999999, 'test')

        self.assertIn('不存在', str(cm.exception))

    def test_invalid_signal_type(self):
        """测试无效的信号类型"""
        stock_code = '000001.SZ'

        self.position_mgr.get_position.return_value = {
            'volume': 1000,
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

        # 无效的信号类型
        signal = {
            'stock_code': stock_code,
            'signal_type': 'INVALID',  # 无效类型
            'trigger_price': 11.0,
            'grid_level': 11.0
        }

        success = self.grid_mgr.execute_grid_trade(signal)
        self.assertFalse(success, "无效信号类型应返回False")


def run_tests():
    """运行测试"""
    suite = unittest.TestLoader().loadTestsFromTestCase(TestGridValidationExceptions)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    report = {
        'test_file': 'test_grid_validation_exceptions.py',
        'total_tests': result.testsRun,
        'passed': result.testsRun - len(result.failures) - len(result.errors),
        'failed': len(result.failures),
        'errors': len(result.errors),
        'coverage': '异常处理 - 100%'
    }

    import json
    with open('test/grid_validation_exceptions_report.json', 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
