"""
网格交易卖出执行测试

测试范围:
1. 正常卖出流程
2. 持仓数量检查
3. 卖出比例计算（position_ratio）
4. 最小卖出数量（100股）
5. 资金回收计算（current_investment更新）
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
from trading_executor import TradingExecutor
from position_manager import PositionManager


class TestGridTradeSell(unittest.TestCase):
    """网格交易卖出执行测试"""

    def setUp(self):
        """测试前准备"""
        # 使用内存数据库
        self.db = DatabaseManager(":memory:")
        self.db.init_grid_tables()

        # Mock position_manager 和 trading_executor
        self.position_manager = Mock(spec=PositionManager)
        self.executor = Mock(spec=TradingExecutor)

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

    def _create_test_session(self, max_investment=10000, current_investment=5000, position_ratio=0.25):
        """创建测试会话"""
        session = GridSession(
            id=None,
            stock_code="000001.SZ",
            status="active",
            center_price=10.0,
            current_center_price=10.0,
            price_interval=0.05,
            position_ratio=position_ratio,
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
        self.manager.trackers[session.id] = PriceTracker(session_id=session.id, last_price=10.5)

        return session

    def _mock_position(self, volume=1000, cost_price=10.0, available=None):
        """Mock持仓信息

        Args:
            volume: 总持仓量
            cost_price: 成本价
            available: 可卖数量（T+1规则）。若为 None 则默认等于 volume（无T+1限制）。
        """
        return {
            'stock_code': '000001.SZ',
            'volume': volume,
            'available': available if available is not None else volume,
            'cost_price': cost_price,
            'current_price': 10.5
        }

    def test_normal_sell_simulation_mode(self):
        """测试1: 正常卖出流程（模拟模式）"""
        print("\n========== 测试1: 正常卖出流程（模拟模式）==========")

        # 启用模拟模式
        config.ENABLE_SIMULATION_MODE = True

        # 创建会话
        session = self._create_test_session(
            max_investment=10000,
            current_investment=5000,
            position_ratio=0.25
        )

        # Mock持仓: 1000股, 成本价10.0
        position = self._mock_position(volume=1000, cost_price=10.0)
        self.position_manager.get_position.return_value = position

        # 构造卖出信号
        signal = {
            'stock_code': '000001.SZ',
            'signal_type': 'SELL',
            'trigger_price': 10.5,
            'grid_level': 'upper',
            'peak_price': 10.6,
            'callback_ratio': 0.005
        }

        # 执行卖出
        result = self.manager._execute_grid_sell(session, signal)

        # 验证结果
        self.assertTrue(result, "卖出应该成功")

        # 验证卖出数量计算
        # position_ratio = 0.25, volume = 1000
        # sell_volume = int(1000 * 0.25 / 100) * 100 = 200股
        expected_sell_volume = 200
        expected_sell_amount = expected_sell_volume * 10.5  # 2100

        # 验证资金回收
        # A-1修复：按实际卖出金额（trigger_price）回收，而非按成本价回收
        # recovered = sell_volume * trigger_price = 200 * 10.5 = 2100
        expected_investment = 5000 - expected_sell_amount  # 5000 - 2100 = 2900

        print(f"预期卖出数量: {expected_sell_volume}")
        print(f"预期卖出金额: {expected_sell_amount:.2f}")
        print(f"预期剩余投入: {expected_investment:.2f} (按trigger_price回收)")

        # 验证会话统计更新
        self.assertEqual(session.sell_count, 1, "卖出次数应为1")
        self.assertEqual(session.trade_count, 1, "交易次数应为1")
        self.assertAlmostEqual(session.total_sell_amount, expected_sell_amount, places=2,
                              msg="卖出总额应正确")
        self.assertAlmostEqual(session.current_investment, expected_investment, places=2,
                              msg="当前投入应正确回收")

        # 验证数据库记录
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT * FROM grid_trades WHERE session_id=1 AND trade_type='SELL'")
        trade = cursor.fetchone()
        self.assertIsNotNone(trade, "应有交易记录")
        self.assertEqual(trade['volume'], expected_sell_volume, "记录的数量应正确")
        self.assertAlmostEqual(trade['amount'], expected_sell_amount, places=2,
                              msg="记录的金额应正确")

        print(f"[OK] 卖出成功: 数量={expected_sell_volume}, 金额={expected_sell_amount:.2f}")
        print(f"[OK] 投入更新: {session.current_investment:.2f} (按trigger_price回收 {expected_sell_amount:.2f})")

    def test_sell_with_no_position(self):
        """测试2: 无持仓时拒绝卖出"""
        print("\n========== 测试2: 无持仓时拒绝卖出 ==========")

        config.ENABLE_SIMULATION_MODE = True

        session = self._create_test_session()

        # Mock无持仓
        self.position_manager.get_position.return_value = None

        signal = {
            'stock_code': '000001.SZ',
            'signal_type': 'SELL',
            'trigger_price': 10.5,
            'grid_level': 'upper'
        }

        # 执行卖出
        result = self.manager._execute_grid_sell(session, signal)

        # 验证拒绝卖出
        self.assertFalse(result, "应拒绝卖出")
        self.assertEqual(session.sell_count, 0, "卖出次数应为0")

        print(f"[OK] 正确拒绝卖出: 无持仓")

    def test_sell_with_zero_volume(self):
        """测试3: 持仓为0时拒绝卖出"""
        print("\n========== 测试3: 持仓为0时拒绝卖出 ==========")

        config.ENABLE_SIMULATION_MODE = True

        session = self._create_test_session()

        # Mock持仓为0
        position = self._mock_position(volume=0, cost_price=10.0)
        self.position_manager.get_position.return_value = position

        signal = {
            'stock_code': '000001.SZ',
            'signal_type': 'SELL',
            'trigger_price': 10.5,
            'grid_level': 'upper'
        }

        # 执行卖出
        result = self.manager._execute_grid_sell(session, signal)

        # 验证拒绝卖出
        self.assertFalse(result, "应拒绝卖出")
        self.assertEqual(session.sell_count, 0, "卖出次数应为0")

        print(f"[OK] 正确拒绝卖出: 持仓为0")

    def test_sell_volume_calculation(self):
        """测试4: 卖出比例计算（position_ratio）"""
        print("\n========== 测试4: 卖出比例计算（position_ratio）==========")

        config.ENABLE_SIMULATION_MODE = True

        # 测试不同比例
        test_cases = [
            (0.25, 1000, 200),  # 25% * 1000 = 250 -> 200股
            (0.50, 1000, 500),  # 50% * 1000 = 500股
            (0.10, 1000, 100),  # 10% * 1000 = 100股
            (0.05, 1000, 100),  # 5% * 1000 = 50 -> 最少100股
        ]

        for ratio, volume, expected_sell in test_cases:
            session = self._create_test_session(position_ratio=ratio)
            position = self._mock_position(volume=volume)
            self.position_manager.get_position.return_value = position

            signal = {'trigger_price': 10.5, 'grid_level': 'upper'}

            result = self.manager._execute_grid_sell(session, signal)
            self.assertTrue(result)

            # 从数据库读取实际卖出数量
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT volume FROM grid_trades WHERE session_id=? AND trade_type='SELL'", (session.id,))
            trade = cursor.fetchone()
            actual_sell = trade['volume'] if trade else 0

            self.assertEqual(actual_sell, expected_sell,
                           f"比例{ratio}, 持仓{volume}, 应卖出{expected_sell}股")
            self.assertEqual(actual_sell % 100, 0, "卖出数量应为100的倍数")

            print(f"[OK] 比例={ratio*100:.0f}%, 持仓={volume}, 卖出={actual_sell}股")

    def test_minimum_sell_volume(self):
        """测试5: 最小卖出数量（100股）"""
        print("\n========== 测试5: 最小卖出数量（100股）==========")

        config.ENABLE_SIMULATION_MODE = True

        # 测试场景1: 持仓不足100股
        session1 = self._create_test_session(position_ratio=0.25)
        position1 = self._mock_position(volume=50)  # 只有50股
        self.position_manager.get_position.return_value = position1

        signal = {'trigger_price': 10.5, 'grid_level': 'upper'}

        result1 = self.manager._execute_grid_sell(session1, signal)

        self.assertFalse(result1, "持仓不足100股应拒绝卖出")
        self.assertEqual(session1.sell_count, 0)
        print(f"[OK] 场景1: 持仓50股, 正确拒绝卖出")

        # 测试场景2: 计算卖出数量不足100股
        session2 = self._create_test_session(position_ratio=0.05)  # 5%
        position2 = self._mock_position(volume=1000)
        self.position_manager.get_position.return_value = position2

        result2 = self.manager._execute_grid_sell(session2, signal)

        # 1000 * 0.05 = 50 -> 取整为0 -> 调整为100股
        self.assertTrue(result2, "应自动调整为100股")

        cursor = self.db.conn.cursor()
        cursor.execute("SELECT volume FROM grid_trades WHERE session_id=? AND trade_type='SELL'", (session2.id,))
        trade = cursor.fetchone()
        self.assertEqual(trade['volume'], 100, "应卖出最小100股")
        print(f"[OK] 场景2: 计算卖出50股, 自动调整为100股")

    def test_sell_volume_exceeds_position(self):
        """测试6: 卖出数量超过持仓时自动调整"""
        print("\n========== 测试6: 卖出数量超过持仓时自动调整 ==========")

        config.ENABLE_SIMULATION_MODE = True

        # position_ratio=0.5, 但持仓只有300股
        session = self._create_test_session(position_ratio=0.5)
        position = self._mock_position(volume=300)
        self.position_manager.get_position.return_value = position

        signal = {'trigger_price': 10.5, 'grid_level': 'upper'}

        result = self.manager._execute_grid_sell(session, signal)
        self.assertTrue(result)

        # 应卖出 min(300*0.5=150, 300) -> 100股（向下取整）
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT volume FROM grid_trades WHERE session_id=1 AND trade_type='SELL'")
        trade = cursor.fetchone()
        self.assertEqual(trade['volume'], 100, "应卖出100股")

        print(f"[OK] 计算卖出150股超过持仓, 调整为100股")

    def test_fund_recovery_calculation(self):
        """测试7: 资金回收计算"""
        print("\n========== 测试7: 资金回收计算 ==========")

        config.ENABLE_SIMULATION_MODE = True

        # 初始投入5000
        session = self._create_test_session(
            max_investment=10000,
            current_investment=5000,
            position_ratio=0.25
        )

        # 持仓1000股, 成本价10.0
        position = self._mock_position(volume=1000, cost_price=10.0)
        self.position_manager.get_position.return_value = position

        # 卖出价10.5
        signal = {'trigger_price': 10.5, 'grid_level': 'upper'}

        result = self.manager._execute_grid_sell(session, signal)
        self.assertTrue(result)

        # 卖出200股
        # A-1修复：按实际卖出金额（trigger_price）回收
        # recovered = sell_volume * trigger_price = 200 * 10.5 = 2100
        # new_investment = 5000 - 2100 = 2900
        self.assertAlmostEqual(session.current_investment, 2900.0, places=2)

        # 卖出金额 = 200 * 10.5 = 2100
        self.assertAlmostEqual(session.total_sell_amount, 2100.0, places=2)

        print(f"[OK] 卖出200股, 按trigger_price(10.5)回收2100, 剩余投入2900")
        print(f"[OK] 卖出金额2100, 网格利润={session.get_grid_profit():.2f}")

    def test_real_mode_sell(self):
        """测试8: 实盘模式卖出"""
        print("\n========== 测试8: 实盘模式卖出 ==========")

        # 切换到实盘模式
        config.ENABLE_SIMULATION_MODE = False

        session = self._create_test_session()
        position = self._mock_position(volume=1000)
        self.position_manager.get_position.return_value = position

        signal = {'trigger_price': 10.5, 'grid_level': 'upper'}

        # Mock executor返回成功
        self.executor.sell_stock.return_value = {'order_id': 'REAL_SELL_12345'}

        result = self.manager._execute_grid_sell(session, signal)

        # 验证调用executor
        self.assertTrue(result)
        self.executor.sell_stock.assert_called_once()
        call_args = self.executor.sell_stock.call_args
        self.assertEqual(call_args[1]['stock_code'], '000001.SZ')
        self.assertEqual(call_args[1]['volume'], 200)
        self.assertEqual(call_args[1]['strategy'], config.GRID_STRATEGY_NAME)

        # 验证trade_id来自实盘
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT trade_id FROM grid_trades WHERE session_id=1")
        trade = cursor.fetchone()
        self.assertEqual(trade['trade_id'], 'REAL_SELL_12345')

        print(f"[OK] 实盘卖出成功, trade_id={trade['trade_id']}")

    def test_real_mode_sell_failure(self):
        """测试9: 实盘模式卖出失败"""
        print("\n========== 测试9: 实盘模式卖出失败 ==========")

        config.ENABLE_SIMULATION_MODE = False

        session = self._create_test_session(current_investment=5000)
        position = self._mock_position(volume=1000)
        self.position_manager.get_position.return_value = position

        signal = {'trigger_price': 10.5, 'grid_level': 'upper'}

        # Mock executor返回失败
        self.executor.sell_stock.return_value = None

        result = self.manager._execute_grid_sell(session, signal)

        # 验证卖出失败
        self.assertFalse(result, "实盘卖出失败应返回False")
        self.assertEqual(session.sell_count, 0, "卖出次数应为0")
        self.assertAlmostEqual(session.current_investment, 5000.0, places=2,
                              msg="投入应不变")

        # 数据库应无记录
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT COUNT(*) as cnt FROM grid_trades WHERE session_id=1")
        count = cursor.fetchone()['cnt']
        self.assertEqual(count, 0, "数据库应无交易记录")

        print(f"[OK] 实盘卖出失败, 正确回滚状态")

    # ==================== RISK-2：DB写入失败内存回滚测试（新增）====================
    def test_db_write_failure_rollback_sell(self):
        """RISK-2：DB写入失败时卖出内存状态回滚测试

        验证：record_grid_trade 抛出异常后，session 的 sell_count/current_investment 等
        统计字段应被完整回滚到执行前的状态，不留脏数据（尤其是资金回收不能虚增）。
        """
        print("\n========== RISK-2: DB写入失败内存回滚（卖出）==========")

        config.ENABLE_SIMULATION_MODE = True

        session = self._create_test_session(
            position_ratio=0.25,
            current_investment=5000.0
        )
        position = self._mock_position(volume=1000, cost_price=10.0)
        self.position_manager.get_position.return_value = position

        signal = {'trigger_price': 10.5, 'grid_level': 'upper',
                  'peak_price': 10.6, 'callback_ratio': 0.005}

        # 记录执行前状态
        before_trade_count = session.trade_count
        before_sell_count = session.sell_count
        before_total_sell = session.total_sell_amount
        before_investment = session.current_investment

        # Mock DB 的 record_grid_trade 抛出异常
        import sqlite3
        from unittest.mock import patch
        with patch.object(self.manager.db, 'record_grid_trade',
                          side_effect=sqlite3.OperationalError("database is locked")):
            result = self.manager._execute_grid_sell(session, signal)

        # 验证：返回 False
        self.assertFalse(result, "DB写入失败应返回 False")

        # 验证：内存状态完整回滚（卖出后资金回收不应生效）
        self.assertEqual(session.trade_count, before_trade_count,
                         "DB失败后 trade_count 应回滚")
        self.assertEqual(session.sell_count, before_sell_count,
                         "DB失败后 sell_count 应回滚")
        self.assertAlmostEqual(session.total_sell_amount, before_total_sell, places=4,
                               msg="DB失败后 total_sell_amount 应回滚")
        self.assertAlmostEqual(session.current_investment, before_investment, places=4,
                               msg="DB失败后 current_investment 应回滚（资金不应虚假回收）")

        print(f"[OK] DB写入失败后内存状态完整回滚: sell_count={session.sell_count}, "
              f"investment={session.current_investment:.2f}")

    def test_sell_t_plus_1_available_check(self):
        """A-3验证：卖出数量受 available（T+1可卖数量）限制

        场景：持仓 volume=1000, available=400（今日买入600股不可卖），
        position_ratio=0.25 → 应卖 min(1000*0.25, 400) 范围内的整百数。
        """
        print("\n========== A-3: T+1 available 检查 ==========")

        config.ENABLE_SIMULATION_MODE = True

        session = self._create_test_session(
            max_investment=10000,
            current_investment=5000,
            position_ratio=0.25
        )

        # 持仓1000股，但只有400股可卖（今日买入600股受T+1限制）
        position = self._mock_position(volume=1000, cost_price=10.0, available=400)
        self.position_manager.get_position.return_value = position

        signal = {
            'stock_code': '000001.SZ',
            'signal_type': 'SELL',
            'trigger_price': 10.5,
            'grid_level': 'upper',
            'peak_price': 10.6,
            'callback_ratio': 0.005
        }

        result = self.manager._execute_grid_sell(session, signal)
        self.assertTrue(result, "卖出应成功")

        # position_ratio=0.25, available=400 → min(100, 400×0.25=100)
        expected_sell_volume = 100  # min(int(400*0.25)//100*100, available)
        expected_sell_amount = expected_sell_volume * 10.5

        cursor = self.db.conn.cursor()
        cursor.execute("SELECT volume FROM grid_trades WHERE session_id=? AND trade_type='SELL'", (session.id,))
        trade = cursor.fetchone()
        actual_sell = trade['volume'] if trade else 0

        self.assertLessEqual(actual_sell, 400,
                             f"卖出数量{actual_sell}不应超过可卖数量400（T+1限制）")
        print(f"[OK] T+1检查: 实际卖出={actual_sell}股, 可卖=400股, volume=1000股")

    def test_sell_rejected_when_available_zero(self):
        """A-3验证：available=0（当日全仓买入）时拒绝卖出

        场景：volume=1000, available=0（今日全部买入），应拒绝卖出。
        """
        print("\n========== A-3: available=0 拒绝卖出 ==========")

        config.ENABLE_SIMULATION_MODE = True

        session = self._create_test_session(
            max_investment=10000,
            current_investment=5000
        )

        # 持仓1000股但全部不可卖（今日买入）
        position = self._mock_position(volume=1000, cost_price=10.0, available=0)
        self.position_manager.get_position.return_value = position

        signal = {
            'stock_code': '000001.SZ',
            'signal_type': 'SELL',
            'trigger_price': 10.5,
            'grid_level': 'upper',
            'peak_price': 10.6,
            'callback_ratio': 0.005
        }

        result = self.manager._execute_grid_sell(session, signal)
        self.assertFalse(result, "available=0 时应拒绝卖出（T+1限制）")
        self.assertEqual(session.sell_count, 0, "卖出次数应为0")
        print("[OK] available=0 时正确拒绝卖出")

    def test_sell_cooldown_prevents_rapid_repeated(self):
        """A-4/B-5验证：GRID_SELL_COOLDOWN 阻止短时间内连续卖出

        场景：第一次卖出成功后，GRID_SELL_COOLDOWN=60 秒内再次卖出应被阻止。
        """
        print("\n========== A-4/B-5: 卖出冷却机制 ==========")

        config.ENABLE_SIMULATION_MODE = True

        session = self._create_test_session(
            max_investment=10000,
            current_investment=5000,
            position_ratio=0.25
        )

        position = self._mock_position(volume=2000, cost_price=10.0)
        self.position_manager.get_position.return_value = position

        signal = {
            'stock_code': '000001.SZ',
            'signal_type': 'SELL',
            'trigger_price': 10.5,
            'grid_level': 'upper',
            'peak_price': 10.6,
            'callback_ratio': 0.005
        }

        with patch.object(config, 'GRID_SELL_COOLDOWN', 60):
            # 第一次卖出：应成功
            result1 = self.manager._execute_grid_sell(session, signal)
            self.assertTrue(result1, "第一次卖出应成功")

            # 第二次卖出：应被冷却阻止
            result2 = self.manager._execute_grid_sell(session, signal)
            self.assertFalse(result2,
                             "GRID_SELL_COOLDOWN 期间内，第二次卖出应被阻止")

        print("[OK] 卖出冷却机制验证通过")


def run_tests():
    """运行所有测试"""
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestGridTradeSell)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    return result


if __name__ == '__main__':
    print("=" * 80)
    print("网格交易卖出执行测试")
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
