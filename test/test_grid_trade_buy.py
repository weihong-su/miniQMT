"""
网格交易买入执行测试

测试范围:
1. 正常买入流程
2. 投入限额检查（max_investment）
3. 买入金额计算（单次不超过20%）
4. 股数取整（100股倍数）
5. 最小买入金额限制（100元）
6. 模拟模式 vs 实盘模式
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import unittest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime
import sqlite3
from dataclasses import asdict

import config
from grid_trading_manager import GridSession, GridTradingManager, PriceTracker
from grid_database import DatabaseManager


class TestGridTradeBuy(unittest.TestCase):
    """网格交易买入执行测试"""

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

        # 保存原始配置
        self.original_simulation_mode = config.ENABLE_SIMULATION_MODE

    def tearDown(self):
        """测试后清理"""
        # 恢复配置
        config.ENABLE_SIMULATION_MODE = self.original_simulation_mode

        # 关闭数据库
        if hasattr(self, 'db') and self.db:
            self.db.close()

    def _create_test_session(self, max_investment=10000, current_investment=0):
        """创建测试会话"""
        session = GridSession(
            id=None,
            stock_code="000001.SZ",
            status="active",
            center_price=10.0,
            current_center_price=10.0,
            price_interval=0.05,
            position_ratio=0.25,
            callback_ratio=0.005,
            max_investment=max_investment,
            current_investment=current_investment,
            start_time=datetime.now()
        )

        # 插入数据库
        session_dict = asdict(session)
        session.id = self.db.create_grid_session(session_dict)

        # 注册到manager
        self.manager.sessions["000001.SZ"] = session
        self.manager.trackers[session.id] = PriceTracker(session_id=session.id, last_price=9.5)

        return session

    def test_normal_buy_simulation_mode(self):
        """测试1: 正常买入流程（模拟模式）"""
        print("\n========== 测试1: 正常买入流程（模拟模式）==========")

        # 启用模拟模式
        config.ENABLE_SIMULATION_MODE = True

        # 创建会话
        session = self._create_test_session(max_investment=10000, current_investment=0)

        # 构造买入信号
        signal = {
            'stock_code': '000001.SZ',
            'signal_type': 'BUY',
            'trigger_price': 9.5,
            'grid_level': 'lower',
            'valley_price': 9.4,
            'callback_ratio': 0.005
        }

        # 执行买入
        result = self.manager._execute_grid_buy(session, signal)

        # 验证结果
        self.assertTrue(result, "买入应该成功")

        # 验证金额计算
        expected_buy_amount = 10000 * 0.2  # 单次不超过20%
        expected_volume = int(expected_buy_amount / 9.5 / 100) * 100  # 取整到100股
        expected_actual_amount = expected_volume * 9.5

        print(f"预期买入金额: {expected_buy_amount:.2f}")
        print(f"预期买入数量: {expected_volume}")
        print(f"预期实际金额: {expected_actual_amount:.2f}")

        # 验证会话统计更新
        self.assertEqual(session.buy_count, 1, "买入次数应为1")
        self.assertEqual(session.trade_count, 1, "交易次数应为1")
        self.assertAlmostEqual(session.total_buy_amount, expected_actual_amount, places=2,
                              msg="买入总额应正确")
        self.assertAlmostEqual(session.current_investment, expected_actual_amount, places=2,
                              msg="当前投入应正确")

        # 验证数据库记录
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT * FROM grid_trades WHERE session_id=1 AND trade_type='BUY'")
        trade = cursor.fetchone()
        self.assertIsNotNone(trade, "应有交易记录")
        self.assertEqual(trade['volume'], expected_volume, "记录的数量应正确")
        self.assertAlmostEqual(trade['amount'], expected_actual_amount, places=2,
                              msg="记录的金额应正确")

        print(f"[OK] 买入成功: 数量={expected_volume}, 金额={expected_actual_amount:.2f}")
        print(f"[OK] 投入更新: {session.current_investment:.2f}/{session.max_investment:.2f}")

    def test_buy_with_max_investment_reached(self):
        """测试2: 达到最大投入限额时拒绝买入"""
        print("\n========== 测试2: 达到最大投入限额时拒绝买入 ==========")

        config.ENABLE_SIMULATION_MODE = True

        # 创建已达到限额的会话
        session = self._create_test_session(max_investment=10000, current_investment=10000)

        signal = {
            'stock_code': '000001.SZ',
            'signal_type': 'BUY',
            'trigger_price': 9.5,
            'grid_level': 'lower'
        }

        # 执行买入
        result = self.manager._execute_grid_buy(session, signal)

        # 验证拒绝买入
        self.assertFalse(result, "应拒绝买入")
        self.assertEqual(session.buy_count, 0, "买入次数应为0")
        self.assertEqual(session.current_investment, 10000, "投入金额应不变")

        print(f"[OK] 正确拒绝买入: investment={session.current_investment}/{session.max_investment}")

    def test_buy_amount_calculation(self):
        """测试3: 买入金额计算（单次不超过20%）"""
        print("\n========== 测试3: 买入金额计算（单次不超过20%）==========")

        config.ENABLE_SIMULATION_MODE = True

        # 测试场景1: 剩余投入充足
        session = self._create_test_session(max_investment=10000, current_investment=0)
        signal = {'trigger_price': 10.0, 'grid_level': 'lower'}

        result = self.manager._execute_grid_buy(session, signal)
        self.assertTrue(result)

        # 应买入 10000*0.2 = 2000元 -> 200股
        self.assertEqual(session.buy_count, 1)
        self.assertAlmostEqual(session.current_investment, 2000.0, places=2)
        print(f"[OK] 场景1: 剩余充足, 投入={session.current_investment:.2f}, 应为2000.0")

        # 测试场景2: 剩余投入有限 (改为剩余8000,期望买入2000)
        session2 = self._create_test_session(max_investment=10000, current_investment=8000)
        signal2 = {'trigger_price': 10.0, 'grid_level': 'lower'}

        result2 = self.manager._execute_grid_buy(session2, signal2)
        self.assertTrue(result2)

        # 剩余2000, 应买入2000 -> 200股 = 2000元
        self.assertEqual(session2.buy_count, 1)
        added_investment = session2.current_investment - 8000
        self.assertAlmostEqual(added_investment, 2000.0, places=2)
        print(f"[OK] 场景2: 剩余有限, 新增投入={added_investment:.2f}, 应为2000.0")

    def test_volume_rounding(self):
        """测试4: 股数取整（100股倍数）"""
        print("\n========== 测试4: 股数取整（100股倍数）==========")

        config.ENABLE_SIMULATION_MODE = True

        # 测试不同价格下的取整 (向下取整到100的倍数)
        test_cases = [
            (10.0, 10000, 200),   # 10000*0.2/10 = 200股
            (9.5, 10000, 200),    # 10000*0.2/9.5 = 210.526 -> 向下取整200股
            (11.2, 10000, 100),   # 10000*0.2/11.2 = 178.57 -> 向下取整100股
        ]

        for price, max_inv, expected_vol in test_cases:
            session = self._create_test_session(max_investment=max_inv, current_investment=0)
            signal = {'trigger_price': price, 'grid_level': 'lower'}

            result = self.manager._execute_grid_buy(session, signal)

            # 从数据库读取实际买入数量
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT volume FROM grid_trades WHERE session_id=? AND trade_type='BUY'", (session.id,))
            trade = cursor.fetchone()
            actual_vol = trade['volume'] if trade else 0

            self.assertEqual(actual_vol, expected_vol,
                           f"价格{price}, 最大投入{max_inv}, 应买入{expected_vol}股")

            print(f"[OK] 价格={price}, 最大投入={max_inv}, 买入={actual_vol}股")

    def test_minimum_buy_amount(self):
        """测试5: 最小买入金额限制（100元）"""
        print("\n========== 测试5: 最小买入金额限制（100元）==========")

        config.ENABLE_SIMULATION_MODE = True

        # 测试场景1: 剩余投入不足100元
        session = self._create_test_session(max_investment=10000, current_investment=9950)
        signal = {'trigger_price': 10.0, 'grid_level': 'lower'}

        result = self.manager._execute_grid_buy(session, signal)

        self.assertFalse(result, "剩余投入不足100元应拒绝买入")
        self.assertEqual(session.buy_count, 0)
        print(f"[OK] 场景1: 剩余{10000-9950}元, 正确拒绝买入")

        # 测试场景2: 计算买入数量不足100股
        session2 = self._create_test_session(max_investment=10000, current_investment=0)
        signal2 = {'trigger_price': 100.0, 'grid_level': 'lower'}  # 高价导致买不足100股

        # max_investment * 0.2 / 100.0 = 2000/100 = 20股 -> 向下取整为0 -> 拒绝
        result2 = self.manager._execute_grid_buy(session2, signal2)

        self.assertFalse(result2, "买入数量不足100股应拒绝")
        self.assertEqual(session2.buy_count, 0)
        print(f"[OK] 场景2: 价格{signal2['trigger_price']}, 买入不足100股, 正确拒绝")

    def test_real_mode_buy(self):
        """测试6: 实盘模式买入"""
        print("\n========== 测试6: 实盘模式买入 ==========")

        # 切换到实盘模式
        config.ENABLE_SIMULATION_MODE = False

        session = self._create_test_session(max_investment=10000, current_investment=0)
        signal = {'trigger_price': 10.0, 'grid_level': 'lower'}

        # Mock executor返回成功
        self.executor.execute_buy.return_value = {'order_id': 'REAL_BUY_12345'}

        result = self.manager._execute_grid_buy(session, signal)

        # 验证调用executor
        self.assertTrue(result)
        self.executor.execute_buy.assert_called_once()
        call_args = self.executor.execute_buy.call_args
        self.assertEqual(call_args[1]['stock_code'], '000001.SZ')
        self.assertAlmostEqual(call_args[1]['amount'], 2000.0, places=2)
        self.assertEqual(call_args[1]['strategy'], config.GRID_STRATEGY_NAME)

        # 验证trade_id来自实盘
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT trade_id FROM grid_trades WHERE session_id=1")
        trade = cursor.fetchone()
        self.assertEqual(trade['trade_id'], 'REAL_BUY_12345')

        print(f"[OK] 实盘买入成功, trade_id={trade['trade_id']}")

    def test_real_mode_buy_failure(self):
        """测试7: 实盘模式买入失败"""
        print("\n========== 测试7: 实盘模式买入失败 ==========")

        config.ENABLE_SIMULATION_MODE = False

        session = self._create_test_session(max_investment=10000, current_investment=0)
        signal = {'trigger_price': 10.0, 'grid_level': 'lower'}

        # Mock executor返回失败
        self.executor.execute_buy.return_value = None

        result = self.manager._execute_grid_buy(session, signal)

        # 验证买入失败
        self.assertFalse(result, "实盘买入失败应返回False")
        self.assertEqual(session.buy_count, 0, "买入次数应为0")
        self.assertEqual(session.current_investment, 0, "投入应不变")

        # 数据库应无记录
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT COUNT(*) as cnt FROM grid_trades WHERE session_id=1")
        count = cursor.fetchone()['cnt']
        self.assertEqual(count, 0, "数据库应无交易记录")

        print(f"[OK] 实盘买入失败, 正确回滚状态")

    def test_invalid_max_investment(self):
        """测试8: max_investment无效时拒绝买入"""
        print("\n========== 测试8: max_investment无效时拒绝买入 ==========")

        config.ENABLE_SIMULATION_MODE = True

        # 测试场景1: max_investment=0
        session1 = self._create_test_session(max_investment=0, current_investment=0)
        signal = {'trigger_price': 10.0, 'grid_level': 'lower'}

        result1 = self.manager._execute_grid_buy(session1, signal)
        self.assertFalse(result1, "max_investment=0应拒绝买入")
        print(f"[OK] max_investment=0, 正确拒绝")

        # 测试场景2: max_investment<0
        session2 = self._create_test_session(max_investment=-1000, current_investment=0)
        result2 = self.manager._execute_grid_buy(session2, signal)
        self.assertFalse(result2, "max_investment<0应拒绝买入")
        print(f"[OK] max_investment=-1000, 正确拒绝")


def run_tests():
    """运行所有测试"""
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestGridTradeBuy)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    return result


if __name__ == '__main__':
    print("=" * 80)
    print("网格交易买入执行测试")
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
