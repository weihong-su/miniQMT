"""
网格交易统计更新测试

测试范围:
1. trade_count、buy_count、sell_count 更新
2. total_buy_amount、total_sell_amount 累计
3. 网格盈亏计算（get_profit_ratio）
4. 数据库记录完整性
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


class TestGridTradeStatistics(unittest.TestCase):
    """网格交易统计更新测试"""

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

    def _create_test_session(self, max_investment=10000):
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
            current_investment=0,
            start_time=datetime.now()
        )

        # 插入数据库
        session_dict = asdict(session)
        session.id = self.db.create_grid_session(session_dict)

        self.manager.sessions["000001.SZ"] = session
        self.manager.trackers[session.id] = PriceTracker(session_id=session.id, last_price=10.0)

        return session

    def test_trade_count_update(self):
        """测试1: trade_count更新"""
        print("\n========== 测试1: trade_count更新 ==========")

        session = self._create_test_session()

        # 初始应为0
        self.assertEqual(session.trade_count, 0)

        # 执行1次买入
        buy_signal = {'trigger_price': 10.0, 'grid_level': 'lower'}
        self.manager._execute_grid_buy(session, buy_signal)
        self.assertEqual(session.trade_count, 1, "买入后trade_count应为1")

        # Mock持仓
        position = {'volume': 200, 'cost_price': 10.0}
        self.position_manager.get_position.return_value = position

        # 执行1次卖出
        sell_signal = {'trigger_price': 10.5, 'grid_level': 'upper'}
        self.manager._execute_grid_sell(session, sell_signal)
        self.assertEqual(session.trade_count, 2, "卖出后trade_count应为2")

        # 再执行1次买入
        self.manager._execute_grid_buy(session, buy_signal)
        self.assertEqual(session.trade_count, 3, "再次买入后trade_count应为3")

        print(f"[OK] trade_count正确更新: {session.trade_count}")

    def test_buy_count_and_sell_count(self):
        """测试2: buy_count和sell_count更新"""
        print("\n========== 测试2: buy_count和sell_count更新 ==========")

        session = self._create_test_session()

        # 初始应为0
        self.assertEqual(session.buy_count, 0)
        self.assertEqual(session.sell_count, 0)

        # 执行2次买入
        buy_signal = {'trigger_price': 10.0, 'grid_level': 'lower'}
        self.manager._execute_grid_buy(session, buy_signal)
        self.manager._execute_grid_buy(session, buy_signal)

        self.assertEqual(session.buy_count, 2, "buy_count应为2")
        self.assertEqual(session.sell_count, 0, "sell_count应为0")

        # Mock持仓
        position = {'volume': 400, 'cost_price': 10.0}
        self.position_manager.get_position.return_value = position

        # 执行3次卖出
        sell_signal = {'trigger_price': 10.5, 'grid_level': 'upper'}
        for _ in range(3):
            self.manager._execute_grid_sell(session, sell_signal)

        self.assertEqual(session.buy_count, 2, "buy_count应保持为2")
        self.assertEqual(session.sell_count, 3, "sell_count应为3")
        self.assertEqual(session.trade_count, 5, "trade_count应为5")

        print(f"[OK] buy_count={session.buy_count}, sell_count={session.sell_count}, trade_count={session.trade_count}")

    def test_total_buy_amount_accumulation(self):
        """测试3: total_buy_amount累计"""
        print("\n========== 测试3: total_buy_amount累计 ==========")

        session = self._create_test_session(max_investment=10000)

        # 初始应为0
        self.assertEqual(session.total_buy_amount, 0)

        # 执行3次买入
        buy_signal = {'trigger_price': 10.0, 'grid_level': 'lower'}

        buy_amounts = []
        for i in range(3):
            before = session.total_buy_amount
            result = self.manager._execute_grid_buy(session, buy_signal)
            if result:
                after = session.total_buy_amount
                buy_amounts.append(after - before)
                print(f"  第{i+1}次买入: {after - before:.2f}, 累计={after:.2f}")

        # 验证累计
        expected_total = sum(buy_amounts)
        self.assertAlmostEqual(session.total_buy_amount, expected_total, places=2,
                              msg="total_buy_amount应等于各次买入之和")

        # 从数据库验证
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT SUM(amount) as total FROM grid_trades WHERE session_id=1 AND trade_type='BUY'")
        db_total = cursor.fetchone()['total'] or 0

        self.assertAlmostEqual(session.total_buy_amount, db_total, places=2,
                              msg="内存total_buy_amount应与数据库一致")

        print(f"[OK] total_buy_amount={session.total_buy_amount:.2f}, 数据库={db_total:.2f}")

    def test_total_sell_amount_accumulation(self):
        """测试4: total_sell_amount累计"""
        print("\n========== 测试4: total_sell_amount累计 ==========")

        session = self._create_test_session()

        # 先买入
        buy_signal = {'trigger_price': 10.0, 'grid_level': 'lower'}
        self.manager._execute_grid_buy(session, buy_signal)

        # 初始total_sell_amount应为0
        self.assertEqual(session.total_sell_amount, 0)

        # Mock持仓
        position = {'volume': 1000, 'cost_price': 10.0}
        self.position_manager.get_position.return_value = position

        # 执行3次卖出
        sell_signal = {'trigger_price': 10.5, 'grid_level': 'upper'}

        sell_amounts = []
        for i in range(3):
            before = session.total_sell_amount
            result = self.manager._execute_grid_sell(session, sell_signal)
            if result:
                after = session.total_sell_amount
                sell_amounts.append(after - before)
                print(f"  第{i+1}次卖出: {after - before:.2f}, 累计={after:.2f}")

        # 验证累计
        expected_total = sum(sell_amounts)
        self.assertAlmostEqual(session.total_sell_amount, expected_total, places=2,
                              msg="total_sell_amount应等于各次卖出之和")

        # 从数据库验证
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT SUM(amount) as total FROM grid_trades WHERE session_id=1 AND trade_type='SELL'")
        db_total = cursor.fetchone()['total'] or 0

        self.assertAlmostEqual(session.total_sell_amount, db_total, places=2,
                              msg="内存total_sell_amount应与数据库一致")

        print(f"[OK] total_sell_amount={session.total_sell_amount:.2f}, 数据库={db_total:.2f}")

    def test_grid_profit_calculation(self):
        """测试5: 网格盈亏计算（get_profit_ratio）"""
        print("\n========== 测试5: 网格盈亏计算（get_profit_ratio）==========")

        session = self._create_test_session(max_investment=10000)

        # 场景1: 无交易时应返回0
        profit_ratio = session.get_profit_ratio()
        self.assertAlmostEqual(profit_ratio, 0.0, places=4,
                              msg="无交易时盈亏率应为0")
        print(f"  场景1: 无交易, profit_ratio={profit_ratio:.4f}")

        # 场景2: 买入2000，卖出2100，盈利100
        buy_signal = {'trigger_price': 10.0, 'grid_level': 'lower'}
        self.manager._execute_grid_buy(session, buy_signal)  # 买入200股 * 10.0 = 2000

        position = {'volume': 200, 'cost_price': 10.0}
        self.position_manager.get_position.return_value = position

        session.position_ratio = 1.0  # 卖出100%
        sell_signal = {'trigger_price': 10.5, 'grid_level': 'upper'}
        self.manager._execute_grid_sell(session, sell_signal)  # 卖出200股 * 10.5 = 2100

        # 网格利润 = 2100 - 2000 = 100
        # 盈亏率 = 100 / 10000 = 0.01 = 1%
        profit_ratio = session.get_profit_ratio()
        expected_ratio = (session.total_sell_amount - session.total_buy_amount) / session.max_investment

        self.assertAlmostEqual(profit_ratio, expected_ratio, places=4,
                              msg="盈亏率计算应正确")

        print(f"  场景2: 买入={session.total_buy_amount:.2f}, 卖出={session.total_sell_amount:.2f}")
        print(f"         网格利润={session.get_grid_profit():.2f}, 盈亏率={profit_ratio*100:.2f}%")

    def test_database_record_completeness(self):
        """测试6: 数据库记录完整性"""
        print("\n========== 测试6: 数据库记录完整性 ==========")

        session = self._create_test_session()

        # 执行1次买入
        buy_signal = {
            'trigger_price': 10.0,
            'grid_level': 'lower',
            'valley_price': 9.9,
            'callback_ratio': 0.005
        }
        self.manager._execute_grid_buy(session, buy_signal)

        # 验证买入记录
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT * FROM grid_trades WHERE session_id=1 AND trade_type='BUY'")
        buy_record = cursor.fetchone()

        self.assertIsNotNone(buy_record, "应有买入记录")
        self.assertEqual(buy_record['stock_code'], '000001.SZ')
        self.assertEqual(buy_record['trade_type'], 'BUY')
        self.assertEqual(buy_record['grid_level'], 'lower')
        self.assertAlmostEqual(buy_record['trigger_price'], 10.0, places=2)
        self.assertIsNotNone(buy_record['volume'], "应记录买入数量")
        self.assertIsNotNone(buy_record['amount'], "应记录买入金额")
        self.assertIsNotNone(buy_record['trade_id'], "应记录trade_id")

        print(f"[OK] 买入记录完整: trade_id={buy_record['trade_id']}, volume={buy_record['volume']}, amount={buy_record['amount']:.2f}")

        # Mock持仓并执行卖出
        position = {'volume': 200, 'cost_price': 10.0}
        self.position_manager.get_position.return_value = position

        sell_signal = {
            'trigger_price': 10.5,
            'grid_level': 'upper',
            'peak_price': 10.6,
            'callback_ratio': 0.005
        }
        self.manager._execute_grid_sell(session, sell_signal)

        # 验证卖出记录
        cursor.execute("SELECT * FROM grid_trades WHERE session_id=1 AND trade_type='SELL'")
        sell_record = cursor.fetchone()

        self.assertIsNotNone(sell_record, "应有卖出记录")
        self.assertEqual(sell_record['stock_code'], '000001.SZ')
        self.assertEqual(sell_record['trade_type'], 'SELL')
        self.assertEqual(sell_record['grid_level'], 'upper')
        self.assertAlmostEqual(sell_record['trigger_price'], 10.5, places=2)
        self.assertIsNotNone(sell_record['volume'], "应记录卖出数量")
        self.assertIsNotNone(sell_record['amount'], "应记录卖出金额")
        self.assertIsNotNone(sell_record['trade_id'], "应记录trade_id")

        print(f"[OK] 卖出记录完整: trade_id={sell_record['trade_id']}, volume={sell_record['volume']}, amount={sell_record['amount']:.2f}")

    def test_session_database_sync(self):
        """测试7: 会话数据库同步"""
        print("\n========== 测试7: 会话数据库同步 ==========")

        session = self._create_test_session()

        # 执行买入
        buy_signal = {'trigger_price': 10.0, 'grid_level': 'lower'}
        self.manager._execute_grid_buy(session, buy_signal)

        # 从数据库读取会话统计
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT trade_count, buy_count, total_buy_amount, current_investment FROM grid_trading_sessions WHERE id=1")
        db_session = cursor.fetchone()

        # 验证同步
        self.assertEqual(db_session['trade_count'], session.trade_count,
                        "数据库trade_count应与内存一致")
        self.assertEqual(db_session['buy_count'], session.buy_count,
                        "数据库buy_count应与内存一致")
        self.assertAlmostEqual(db_session['total_buy_amount'], session.total_buy_amount, places=2,
                              msg="数据库total_buy_amount应与内存一致")
        self.assertAlmostEqual(db_session['current_investment'], session.current_investment, places=2,
                              msg="数据库current_investment应与内存一致")

        print(f"[OK] 会话统计同步: trade_count={session.trade_count}, buy_count={session.buy_count}")
        print(f"              total_buy={session.total_buy_amount:.2f}, investment={session.current_investment:.2f}")

    def test_profit_with_multiple_trades(self):
        """测试8: 多次交易后盈亏计算"""
        print("\n========== 测试8: 多次交易后盈亏计算 ==========")

        session = self._create_test_session(max_investment=10000)

        # 模拟多次买卖
        trades = [
            ('BUY', 10.0, 200, 2000),   # 买入200股 * 10.0
            ('SELL', 10.5, 200, 2100),  # 卖出200股 * 10.5
            ('BUY', 9.8, 200, 1960),    # 买入200股 * 9.8
            ('SELL', 10.3, 200, 2060),  # 卖出200股 * 10.3
        ]

        position = {'volume': 200, 'cost_price': 10.0}
        self.position_manager.get_position.return_value = position

        for trade_type, price, volume, amount in trades:
            if trade_type == 'BUY':
                signal = {'trigger_price': price, 'grid_level': 'lower'}
                self.manager._execute_grid_buy(session, signal)
            else:
                session.position_ratio = 1.0
                signal = {'trigger_price': price, 'grid_level': 'upper'}
                self.manager._execute_grid_sell(session, signal)

        # 计算预期盈亏
        # 买入总额: 2000 + 1960 = 3960
        # 卖出总额: 2100 + 2060 = 4160
        # 网格利润: 4160 - 3960 = 200
        # 盈亏率: 200 / 10000 = 2%

        grid_profit = session.get_grid_profit()
        profit_ratio = session.get_profit_ratio()

        self.assertAlmostEqual(grid_profit, 200.0, places=2,
                              msg="网格利润应为200")
        self.assertAlmostEqual(profit_ratio, 0.02, places=4,
                              msg="盈亏率应为2%")

        print(f"[OK] 执行{len(trades)}次交易")
        print(f"  买入总额={session.total_buy_amount:.2f}")
        print(f"  卖出总额={session.total_sell_amount:.2f}")
        print(f"  网格利润={grid_profit:.2f}")
        print(f"  盈亏率={profit_ratio*100:.2f}%")


def run_tests():
    """运行所有测试"""
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestGridTradeStatistics)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    return result


if __name__ == '__main__':
    print("=" * 80)
    print("网格交易统计更新测试")
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
