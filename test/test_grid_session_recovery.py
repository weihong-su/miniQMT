"""
网格交易会话恢复测试

测试范围:
1. 系统重启后恢复活跃会话
2. 过期会话自动停止
3. 数据一致性验证

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


class TestGridSessionRecovery(unittest.TestCase):
    """网格交易会话恢复测试"""

    def setUp(self):
        """测试前置设置"""
        # 使用临时数据库文件（模拟持久化）
        self.db_path = os.path.join(os.path.dirname(__file__), 'test_grid_recovery.db')
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

        self.db_manager = DatabaseManager(db_path=self.db_path)
        self.db_manager.init_grid_tables()

        # Mock依赖对象
        self.mock_position_manager = Mock()
        self.mock_executor = Mock()

        # 测试数据
        self.test_stock1 = "000001.SZ"
        self.test_stock2 = "000002.SZ"

    def tearDown(self):
        """测试清理"""
        if hasattr(self, 'db_manager') and self.db_manager.conn:
            self.db_manager.conn.close()

        # 删除测试数据库文件
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _create_test_session(self, stock_code, end_time_offset_days=7):
        """
        创建测试会话（直接写入数据库，模拟系统重启前的状态）

        Args:
            stock_code: 股票代码
            end_time_offset_days: 结束时间偏移（天数，正数表示未来）
        """
        cursor = self.db_manager.conn.cursor()

        start_time = datetime.now()
        end_time = start_time + timedelta(days=end_time_offset_days)

        cursor.execute("""
            INSERT INTO grid_trading_sessions (
                stock_code, status, center_price, current_center_price,
                price_interval, position_ratio, callback_ratio,
                max_investment, current_investment,
                max_deviation, target_profit, stop_loss,
                trade_count, buy_count, sell_count,
                total_buy_amount, total_sell_amount,
                start_time, end_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            stock_code, 'active', 10.0, 10.0,
            0.05, 0.25, 0.005,
            10000, 0,
            0.15, 0.10, -0.10,
            0, 0, 0,
            0, 0,
            start_time.isoformat(), end_time.isoformat()
        ))

        self.db_manager.conn.commit()
        return cursor.lastrowid

    def test_recover_active_sessions(self):
        """测试系统重启后恢复活跃会话"""
        # 1. 创建两个活跃会话（模拟系统重启前的状态）
        session_id1 = self._create_test_session(self.test_stock1, end_time_offset_days=7)
        session_id2 = self._create_test_session(self.test_stock2, end_time_offset_days=3)

        # 2. 模拟系统重启：创建新的GridTradingManager实例
        grid_manager = GridTradingManager(
            db_manager=self.db_manager,
            position_manager=self.mock_position_manager,
            trading_executor=self.mock_executor
        )

        # 3. 验证会话已恢复到内存
        self.assertEqual(len(grid_manager.sessions), 2)
        self.assertIn(self.test_stock1, grid_manager.sessions)
        self.assertIn(self.test_stock2, grid_manager.sessions)

        # 4. 验证会话数据完整性
        session1 = grid_manager.sessions[self.test_stock1]
        self.assertEqual(session1.stock_code, self.test_stock1)
        self.assertEqual(session1.status, 'active')
        self.assertEqual(session1.center_price, 10.0)

        session2 = grid_manager.sessions[self.test_stock2]
        self.assertEqual(session2.stock_code, self.test_stock2)

        # 5. 验证PriceTracker已创建
        self.assertIn(session1.id, grid_manager.trackers)
        self.assertIn(session2.id, grid_manager.trackers)

        print(f"[OK] 测试通过: 恢复2个活跃会话")

    def test_auto_stop_expired_sessions(self):
        """测试过期会话自动停止"""
        # 1. 创建一个过期会话（end_time在过去）
        session_id = self._create_test_session(self.test_stock1, end_time_offset_days=-1)

        # 2. 模拟系统重启
        grid_manager = GridTradingManager(
            db_manager=self.db_manager,
            position_manager=self.mock_position_manager,
            trading_executor=self.mock_executor
        )

        # 3. 验证过期会话未恢复到内存
        self.assertNotIn(self.test_stock1, grid_manager.sessions)

        # 4. 验证数据库中会话状态已更新为stopped
        db_session = self.db_manager.get_grid_session(session_id)
        session_dict = dict(db_session)
        self.assertEqual(session_dict['status'], 'stopped')
        self.assertEqual(session_dict['stop_reason'], 'expired')

        print(f"[OK] 测试通过: 过期会话自动停止")

    def test_data_consistency_after_recovery(self):
        """测试数据一致性验证"""
        # 1. 创建会话并设置统计数据
        session_id = self._create_test_session(self.test_stock1, end_time_offset_days=7)

        # 更新统计数据（模拟会话运行过程中的交易）
        cursor = self.db_manager.conn.cursor()
        cursor.execute("""
            UPDATE grid_trading_sessions
            SET trade_count = ?, buy_count = ?, sell_count = ?,
                total_buy_amount = ?, total_sell_amount = ?
            WHERE id = ?
        """, (10, 5, 5, 5000, 5500, session_id))
        self.db_manager.conn.commit()

        # 2. 模拟系统重启
        grid_manager = GridTradingManager(
            db_manager=self.db_manager,
            position_manager=self.mock_position_manager,
            trading_executor=self.mock_executor
        )

        # 3. 验证内存中的会话数据与数据库一致
        session = grid_manager.sessions[self.test_stock1]
        self.assertEqual(session.trade_count, 10)
        self.assertEqual(session.buy_count, 5)
        self.assertEqual(session.sell_count, 5)
        self.assertEqual(session.total_buy_amount, 5000)
        self.assertEqual(session.total_sell_amount, 5500)

        # 4. 验证盈利计算正确
        profit_ratio = session.get_profit_ratio()
        expected_ratio = (5500 - 5000) / 10000  # (卖-买) / max_investment
        self.assertAlmostEqual(profit_ratio, expected_ratio, places=4)

        print(f"[OK] 测试通过: 数据一致性验证通过")

    def test_recovery_with_mixed_sessions(self):
        """测试混合场景：活跃会话 + 过期会话"""
        # 1. 创建3个会话：2个活跃 + 1个过期
        active_id1 = self._create_test_session("000001.SZ", end_time_offset_days=7)
        active_id2 = self._create_test_session("000002.SZ", end_time_offset_days=3)
        expired_id = self._create_test_session("000003.SZ", end_time_offset_days=-1)

        # 2. 模拟系统重启
        grid_manager = GridTradingManager(
            db_manager=self.db_manager,
            position_manager=self.mock_position_manager,
            trading_executor=self.mock_executor
        )

        # 3. 验证只恢复2个活跃会话
        self.assertEqual(len(grid_manager.sessions), 2)
        self.assertIn("000001.SZ", grid_manager.sessions)
        self.assertIn("000002.SZ", grid_manager.sessions)
        self.assertNotIn("000003.SZ", grid_manager.sessions)

        # 4. 验证过期会话已标记为stopped
        expired_session = self.db_manager.get_grid_session(expired_id)
        self.assertEqual(dict(expired_session)['status'], 'stopped')

        print(f"[OK] 测试通过: 混合场景恢复正确")

    def test_recovery_skip_position_check(self):
        """测试恢复时跳过持仓检查（避免启动阻塞）"""
        # 1. 创建会话
        session_id = self._create_test_session(self.test_stock1, end_time_offset_days=7)

        # 2. 模拟持仓查询超时
        def slow_get_position(*args, **kwargs):
            time.sleep(10)  # 模拟慢响应
            return None

        self.mock_position_manager.get_position.side_effect = slow_get_position

        # 3. 模拟系统重启（应该快速完成，不等待持仓查询）
        start_time = time.time()
        grid_manager = GridTradingManager(
            db_manager=self.db_manager,
            position_manager=self.mock_position_manager,
            trading_executor=self.mock_executor
        )
        elapsed_time = time.time() - start_time

        # 4. 验证启动时间 < 2秒（说明跳过了持仓查询）
        self.assertLess(elapsed_time, 2.0)

        # 5. 验证会话仍成功恢复
        self.assertIn(self.test_stock1, grid_manager.sessions)

        print(f"[OK] 测试通过: 恢复时跳过持仓检查，启动时间={elapsed_time:.2f}秒")

    def test_recovery_preserves_timestamps(self):
        """测试恢复时保留时间戳"""
        # 1. 创建会话
        session_id = self._create_test_session(self.test_stock1, end_time_offset_days=7)

        # 获取原始时间戳
        original_session = self.db_manager.get_grid_session(session_id)
        original_start_time = dict(original_session)['start_time']
        original_end_time = dict(original_session)['end_time']

        # 2. 等待0.05秒后模拟系统重启
        time.sleep(0.05)
        grid_manager = GridTradingManager(
            db_manager=self.db_manager,
            position_manager=self.mock_position_manager,
            trading_executor=self.mock_executor
        )

        # 3. 验证时间戳未改变
        session = grid_manager.sessions[self.test_stock1]
        self.assertEqual(session.start_time.isoformat(), original_start_time)
        self.assertEqual(session.end_time.isoformat(), original_end_time)

        print(f"[OK] 测试通过: 时间戳保留正确")


def run_tests():
    """运行测试并生成报告"""
    # 创建测试套件
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestGridSessionRecovery)

    # 运行测试
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    # 生成JSON报告
    report = {
        'test_file': 'test_grid_session_recovery.py',
        'run_time': datetime.now().isoformat(),
        'total_tests': result.testsRun,
        'success': result.wasSuccessful(),
        'failures': len(result.failures),
        'errors': len(result.errors),
        'skipped': len(result.skipped),
        'coverage': {
            'recovery': {
                'active_sessions': True,
                'expired_sessions': True,
                'data_consistency': True,
                'mixed_sessions': True,
                'skip_position_check': True,
                'preserve_timestamps': True
            }
        }
    }

    # 保存报告
    report_path = os.path.join(os.path.dirname(__file__), 'grid_session_recovery_report.json')
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
