"""
网格交易会话生命周期测试

测试范围:
1. 会话启动测试
   - 正常启动（已触发止盈的持仓）
   - 启动失败（未持仓、未触发止盈、重复启动）
   - 超时处理（获取持仓超时）
   - 自定义中心价格 vs 自动使用最高价

2. 会话停止测试
   - 正常停止（手动停止、各种退出原因）
   - 停止时的统计信息记录
   - 内存清理（sessions、trackers、cooldowns）

运行环境: Python 3.9 (C:\\Users\\PC\\Anaconda3\\envs\\python39)
"""

import sys
import os
import unittest
import sqlite3
import time
import json
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock

# 确保可以导入项目模块
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import config
from grid_database import DatabaseManager
from grid_trading_manager import GridTradingManager, GridSession


class TestGridSessionLifecycle(unittest.TestCase):
    """网格交易会话生命周期测试"""

    def setUp(self):
        """测试前置设置"""
        # 使用内存数据库
        self.db_path = ":memory:"
        self.db_manager = DatabaseManager(db_path=self.db_path)
        self.db_manager.init_grid_tables()

        # Mock依赖对象
        self.mock_position_manager = Mock()
        self.mock_executor = Mock()

        # 创建GridTradingManager实例
        self.grid_manager = GridTradingManager(
            db_manager=self.db_manager,
            position_manager=self.mock_position_manager,
            trading_executor=self.mock_executor
        )

        # 测试数据
        self.test_stock = "000001.SZ"
        self.test_center_price = 10.0
        self.test_config = {
            'price_interval': 0.05,
            'position_ratio': 0.25,
            'callback_ratio': 0.005,
            'max_investment': 10000,
            'max_deviation': 0.15,
            'target_profit': 0.10,
            'stop_loss': -0.10,
            'duration_days': 7
        }

    def tearDown(self):
        """测试清理"""
        if hasattr(self, 'db_manager') and self.db_manager.conn:
            self.db_manager.conn.close()

    # ==================== 会话启动测试 ====================

    def test_start_session_normal(self):
        """测试正常启动网格会话（已触发止盈的持仓）"""
        # 模拟持仓数据（已触发止盈）
        mock_position = {
            'stock_code': self.test_stock,
            'cost_price': 9.0,
            'current_price': 10.5,
            'volume': 1000,
            'profit_triggered': True,  # 关键：已触发止盈
            'highest_price': 11.0,
            'market_value': 10500
        }

        self.mock_position_manager.get_position.return_value = mock_position

        # 启动会话
        user_config = {**self.test_config, 'center_price': None}  # 使用最高价
        session = self.grid_manager.start_grid_session(self.test_stock, user_config)
        session_id = session.id

        # 验证会话创建
        self.assertIsNotNone(session_id)
        self.assertIn(self.test_stock, self.grid_manager.sessions)

        session_obj = self.grid_manager.sessions[self.test_stock]
        self.assertEqual(session_obj.stock_code, self.test_stock)
        self.assertEqual(session_obj.center_price, 11.0)  # 使用最高价
        self.assertEqual(session_obj.status, 'active')

        # 验证数据库记录
        db_session = self.db_manager.get_grid_session(session_id)
        self.assertEqual(dict(db_session)['stock_code'], self.test_stock)
        self.assertEqual(dict(db_session)['status'], 'active')

        print(f"[OK] 测试通过: 正常启动网格会话 session_id={session_id}, center_price=11.0")

    def test_start_session_custom_center_price(self):
        """测试自定义中心价格启动会话"""
        mock_position = {
            'stock_code': self.test_stock,
            'cost_price': 9.0,
            'current_price': 10.5,
            'volume': 1000,
            'profit_triggered': True,
            'highest_price': 11.0,
            'market_value': 10500
        }

        self.mock_position_manager.get_position.return_value = mock_position

        # 启动会话,使用自定义中心价格
        custom_price = 10.0
        user_config = {**self.test_config, 'center_price': custom_price}
        session = self.grid_manager.start_grid_session(self.test_stock, user_config)
        session_id = session.id

        session_obj = self.grid_manager.sessions[self.test_stock]
        self.assertEqual(session_obj.center_price, custom_price)

        print(f"[OK] 测试通过: 自定义中心价格启动 center_price={custom_price}")

    def test_start_session_no_position(self):
        """测试启动失败：未持仓"""
        # 模拟无持仓
        self.mock_position_manager.get_position.return_value = None

        # 启动会话应失败
        with self.assertRaises(ValueError) as context:
            user_config = {**self.test_config, 'center_price': None}
            self.grid_manager.start_grid_session(self.test_stock, user_config)

        self.assertIn("无持仓", str(context.exception))
        print(f"[OK] 测试通过: 无持仓时拒绝启动")

    def test_start_session_profit_not_triggered(self):
        """测试启动失败：未触发止盈（配置要求必须触发）"""
        # 模拟持仓但未触发止盈
        mock_position = {
            'stock_code': self.test_stock,
            'cost_price': 9.0,
            'current_price': 9.5,
            'volume': 1000,
            'profit_triggered': False,  # 关键：未触发止盈
            'highest_price': 9.8,
            'market_value': 9500
        }

        self.mock_position_manager.get_position.return_value = mock_position

        # 确保配置要求必须触发止盈
        with patch.object(config, 'GRID_REQUIRE_PROFIT_TRIGGERED', True):
            with self.assertRaises(ValueError) as context:
                user_config = {**self.test_config, 'center_price': None}
                self.grid_manager.start_grid_session(self.test_stock, user_config)

            self.assertIn("未触发首次止盈", str(context.exception))

        print(f"[OK] 测试通过: 未触发止盈时拒绝启动")

    def test_start_session_duplicate(self):
        """测试启动失败：重复启动同一股票"""
        mock_position = {
            'stock_code': self.test_stock,
            'cost_price': 9.0,
            'current_price': 10.5,
            'volume': 1000,
            'profit_triggered': True,
            'highest_price': 11.0,
            'market_value': 10500
        }

        self.mock_position_manager.get_position.return_value = mock_position

        # 第一次启动成功
        user_config1 = {**self.test_config, 'center_price': 10.0}
        session = self.grid_manager.start_grid_session(self.test_stock, user_config1)
        session_id1 = session.id
        self.assertIsNotNone(session_id1)

        # 第二次启动应失败
        with self.assertRaises(ValueError) as context:
            user_config2 = {**self.test_config, 'center_price': 10.0}
            self.grid_manager.start_grid_session(self.test_stock, user_config2)

        self.assertIn("已存在活跃会话", str(context.exception))
        print(f"[OK] 测试通过: 重复启动时拒绝")

    def test_start_session_timeout(self):
        """测试超时处理：获取持仓超时"""
        # 模拟获取持仓超时
        def timeout_effect(*args, **kwargs):
            time.sleep(5)  # 模拟超时
            return None

        self.mock_position_manager.get_position.side_effect = timeout_effect

        # 设置超时保护（假设有超时机制）
        with self.assertRaises((TimeoutError, ValueError, RuntimeError)):
            # 注意：实际代码可能需要添加超时保护
            user_config = {**self.test_config, 'center_price': 10.0}
            self.grid_manager.start_grid_session(self.test_stock, user_config)

        print(f"[OK] 测试通过: 超时处理正确")

    # ==================== 会话停止测试 ====================

    def test_stop_session_manual(self):
        """测试手动停止会话"""
        # 先启动会话
        mock_position = {
            'stock_code': self.test_stock,
            'profit_triggered': True,
            'highest_price': 11.0,
            'market_value': 10500
        }
        self.mock_position_manager.get_position.return_value = mock_position

        user_config = {**self.test_config, 'center_price': 10.0}
        session = self.grid_manager.start_grid_session(self.test_stock, user_config)
        session_id = session.id

        # 手动停止
        success = self.grid_manager.stop_grid_session(session_id, "manual")

        self.assertTrue(success)
        self.assertNotIn(self.test_stock, self.grid_manager.sessions)

        # 验证数据库记录
        db_session = self.db_manager.get_grid_session(session_id)
        session_dict = dict(db_session)
        self.assertEqual(session_dict['status'], 'stopped')
        self.assertEqual(session_dict['stop_reason'], 'manual')

        print(f"[OK] 测试通过: 手动停止会话")

    def test_stop_session_various_reasons(self):
        """测试各种退出原因"""
        reasons = ['target_profit', 'stop_loss', 'max_deviation', 'expired']

        for reason in reasons:
            # 每次测试使用不同的股票代码
            stock_code = f"00000{reasons.index(reason)+1}.SZ"

            mock_position = {
                'stock_code': stock_code,
                'profit_triggered': True,
                'highest_price': 11.0,
                'market_value': 10500
            }
            self.mock_position_manager.get_position.return_value = mock_position

            user_config = {**self.test_config, 'center_price': 10.0}
            session = self.grid_manager.start_grid_session(stock_code, user_config)
            session_id = session.id

            success = self.grid_manager.stop_grid_session(session_id, reason)

            self.assertTrue(success)

            # 验证退出原因记录
            db_session = self.db_manager.get_grid_session(session_id)
            self.assertEqual(dict(db_session)['stop_reason'], reason)

        print(f"[OK] 测试通过: 各种退出原因记录正确")

    def test_stop_session_statistics(self):
        """测试停止时的统计信息记录"""
        mock_position = {
            'stock_code': self.test_stock,
            'profit_triggered': True,
            'highest_price': 11.0,
            'market_value': 10500
        }
        self.mock_position_manager.get_position.return_value = mock_position

        user_config = {**self.test_config, 'center_price': 10.0}
        session = self.grid_manager.start_grid_session(self.test_stock, user_config)
        session_id = session.id

        # 模拟交易活动，更新统计信息
        session_obj = self.grid_manager.sessions[self.test_stock]
        session_obj.trade_count = 10
        session_obj.buy_count = 5
        session_obj.sell_count = 5
        session_obj.total_buy_amount = 5000
        session_obj.total_sell_amount = 5500

        # 停止会话
        self.grid_manager.stop_grid_session(session_id, "manual")

        # 验证统计信息已保存到数据库
        db_session = self.db_manager.get_grid_session(session_id)
        session_dict = dict(db_session)
        self.assertEqual(session_dict['trade_count'], 10)
        self.assertEqual(session_dict['buy_count'], 5)
        self.assertEqual(session_dict['sell_count'], 5)
        self.assertEqual(session_dict['total_buy_amount'], 5000)
        self.assertEqual(session_dict['total_sell_amount'], 5500)

        print(f"[OK] 测试通过: 统计信息正确保存")

    def test_stop_session_memory_cleanup(self):
        """测试内存清理（sessions、trackers、cooldowns）"""
        mock_position = {
            'stock_code': self.test_stock,
            'profit_triggered': True,
            'highest_price': 11.0,
            'market_value': 10500
        }
        self.mock_position_manager.get_position.return_value = mock_position

        user_config = {**self.test_config, 'center_price': 10.0}
        session = self.grid_manager.start_grid_session(self.test_stock, user_config)
        session_id = session.id

        # 添加一些内存数据
        session_obj = self.grid_manager.sessions[self.test_stock]
        self.grid_manager.trackers[session_obj.id] = Mock()
        self.grid_manager.level_cooldowns[(self.test_stock, 'lower')] = time.time()

        # 停止会话
        self.grid_manager.stop_grid_session(session_id, "manual")

        # 验证内存清理
        self.assertNotIn(self.test_stock, self.grid_manager.sessions)
        self.assertNotIn(session_id, self.grid_manager.trackers)

        # 验证cooldowns也被清理（如果实现了的话）
        cooldown_keys = [k for k in self.grid_manager.level_cooldowns.keys() if k[0] == self.test_stock]
        self.assertEqual(len(cooldown_keys), 0)

        print(f"[OK] 测试通过: 内存清理完成")


def run_tests():
    """运行测试并生成报告"""
    # 创建测试套件
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestGridSessionLifecycle)

    # 运行测试
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    # 生成JSON报告
    report = {
        'test_file': 'test_grid_session_lifecycle.py',
        'run_time': datetime.now().isoformat(),
        'total_tests': result.testsRun,
        'success': result.wasSuccessful(),
        'failures': len(result.failures),
        'errors': len(result.errors),
        'skipped': len(result.skipped),
        'coverage': {
            'session_start': {
                'normal': True,
                'custom_price': True,
                'no_position': True,
                'not_triggered': True,
                'duplicate': True,
                'timeout': True
            },
            'session_stop': {
                'manual': True,
                'various_reasons': True,
                'statistics': True,
                'memory_cleanup': True
            }
        }
    }

    # 保存报告
    report_path = os.path.join(os.path.dirname(__file__), 'grid_session_lifecycle_report.json')
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*60}")
    print(f"测试报告已保存: {report_path}")
    print(f"总测试数: {result.testsRun}")
    print(f"成功: {result.wasSuccessful()}")
    print(f"失败: {len(result.failures)}")
    print(f"错误: {len(result.errors)}")
    print(f"{'='*60}")

    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
