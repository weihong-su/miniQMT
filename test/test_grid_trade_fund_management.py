"""
网格交易资金管理测试

测试范围:
1. max_investment 限制验证
2. current_investment 准确性
3. 买入后资金占用
4. 卖出后资金释放
5. 达到限额后拒绝买入
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


class TestGridTradeFundManagement(unittest.TestCase):
    """网格交易资金管理测试"""

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

        self.manager.sessions["000001.SZ"] = session
        self.manager.trackers[session.id] = PriceTracker(session_id=session.id, last_price=10.0)

        return session

    def test_max_investment_limit(self):
        """测试1: max_investment限制验证"""
        print("\n========== 测试1: max_investment限制验证 ==========")

        # 创建会话，限额10000
        session = self._create_test_session(max_investment=10000, current_investment=0)

        # 执行多次买入，直到达到限额
        buy_signal = {'trigger_price': 10.0, 'grid_level': 'lower'}

        buy_count = 0
        max_attempts = 10

        for i in range(max_attempts):
            result = self.manager._execute_grid_buy(session, buy_signal)
            if result:
                buy_count += 1
                print(f"  第{buy_count}次买入成功, 当前投入={session.current_investment:.2f}/{session.max_investment:.2f}")
            else:
                print(f"  第{i+1}次买入被拒绝, 已达限额")
                break

        # 验证达到限额
        self.assertGreaterEqual(session.current_investment, session.max_investment * 0.95,
                               "当前投入应接近或达到限额")
        self.assertLessEqual(session.current_investment, session.max_investment,
                            "当前投入不应超过限额")

        print(f"[OK] 总买入{buy_count}次, 最终投入={session.current_investment:.2f}, 限额={session.max_investment:.2f}")

    def test_current_investment_accuracy(self):
        """测试2: current_investment准确性"""
        print("\n========== 测试2: current_investment准确性 ==========")

        session = self._create_test_session(max_investment=10000, current_investment=0)

        # 买入
        buy_signal = {'trigger_price': 10.0, 'grid_level': 'lower'}
        self.manager._execute_grid_buy(session, buy_signal)

        # 从数据库读取实际买入金额
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT SUM(amount) as total FROM grid_trades WHERE session_id=1 AND trade_type='BUY'")
        total_buy = cursor.fetchone()['total'] or 0

        # 验证current_investment等于买入总额
        self.assertAlmostEqual(session.current_investment, total_buy, places=2,
                              msg="current_investment应等于买入总额")

        print(f"[OK] 买入总额={total_buy:.2f}, current_investment={session.current_investment:.2f}")

    def test_buy_fund_occupation(self):
        """测试3: 买入后资金占用"""
        print("\n========== 测试3: 买入后资金占用 ==========")

        session = self._create_test_session(max_investment=10000, current_investment=0)

        # 记录买入前状态
        before_investment = session.current_investment

        # 执行买入
        buy_signal = {'trigger_price': 10.0, 'grid_level': 'lower'}
        result = self.manager._execute_grid_buy(session, buy_signal)
        self.assertTrue(result)

        # 记录买入后状态
        after_investment = session.current_investment

        # 验证资金占用增加
        self.assertGreater(after_investment, before_investment, "买入后资金占用应增加")

        # 从数据库验证
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT amount FROM grid_trades WHERE session_id=1 AND trade_type='BUY'")
        buy_amount = cursor.fetchone()['amount']

        expected_investment = before_investment + buy_amount
        self.assertAlmostEqual(session.current_investment, expected_investment, places=2)

        print(f"[OK] 买入前={before_investment:.2f}, 买入金额={buy_amount:.2f}, 买入后={after_investment:.2f}")

    def test_sell_fund_release(self):
        """测试4: 卖出后资金释放"""
        print("\n========== 测试4: 卖出后资金释放 ==========")

        # 先买入建立持仓
        session = self._create_test_session(max_investment=10000, current_investment=0)
        buy_signal = {'trigger_price': 10.0, 'grid_level': 'lower'}
        self.manager._execute_grid_buy(session, buy_signal)

        # 记录卖出前状态
        before_investment = session.current_investment
        print(f"  卖出前投入={before_investment:.2f}")

        # Mock持仓
        position = {
            'stock_code': '000001.SZ',
            'volume': 200,
            'cost_price': 10.0,
            'current_price': 10.5
        }
        self.position_manager.get_position.return_value = position

        # 执行卖出
        sell_signal = {'trigger_price': 10.5, 'grid_level': 'upper'}
        result = self.manager._execute_grid_sell(session, sell_signal)
        self.assertTrue(result)

        # 记录卖出后状态
        after_investment = session.current_investment
        print(f"  卖出后投入={after_investment:.2f}")

        # 验证资金释放
        self.assertLess(after_investment, before_investment, "卖出后资金占用应减少")

        # 从数据库验证
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT volume FROM grid_trades WHERE session_id=1 AND trade_type='SELL'")
        sell_volume = cursor.fetchone()['volume']

        # recovered_cost = sell_volume * cost_price
        recovered = sell_volume * 10.0
        expected_investment = max(0, before_investment - recovered)

        self.assertAlmostEqual(session.current_investment, expected_investment, places=2)

        print(f"[OK] 卖出{sell_volume}股, 回收成本={recovered:.2f}, 剩余投入={after_investment:.2f}")

    def test_reject_buy_when_limit_reached(self):
        """测试5: 达到限额后拒绝买入"""
        print("\n========== 测试5: 达到限额后拒绝买入 ==========")

        # 创建已达限额的会话
        session = self._create_test_session(max_investment=10000, current_investment=10000)

        # 尝试买入
        buy_signal = {'trigger_price': 10.0, 'grid_level': 'lower'}
        result = self.manager._execute_grid_buy(session, buy_signal)

        # 验证拒绝买入
        self.assertFalse(result, "达到限额应拒绝买入")
        self.assertEqual(session.buy_count, 0, "买入次数应为0")
        self.assertAlmostEqual(session.current_investment, 10000.0, places=2,
                              msg="投入应保持不变")

        print(f"[OK] 已达限额{session.max_investment:.2f}, 正确拒绝买入")

    def test_buy_sell_cycle_fund_tracking(self):
        """测试6: 买卖循环资金追踪"""
        print("\n========== 测试6: 买卖循环资金追踪 ==========")

        session = self._create_test_session(max_investment=10000, current_investment=0)

        # 第1次买入
        buy_signal = {'trigger_price': 10.0, 'grid_level': 'lower'}
        self.manager._execute_grid_buy(session, buy_signal)
        investment_after_buy1 = session.current_investment
        print(f"  第1次买入后投入={investment_after_buy1:.2f}")

        # Mock持仓
        position = {'volume': 200, 'cost_price': 10.0}
        self.position_manager.get_position.return_value = position

        # 第1次卖出
        sell_signal = {'trigger_price': 10.5, 'grid_level': 'upper'}
        self.manager._execute_grid_sell(session, sell_signal)
        investment_after_sell1 = session.current_investment
        print(f"  第1次卖出后投入={investment_after_sell1:.2f}")

        # 第2次买入
        self.manager._execute_grid_buy(session, buy_signal)
        investment_after_buy2 = session.current_investment
        print(f"  第2次买入后投入={investment_after_buy2:.2f}")

        # 第2次卖出
        position['volume'] = 400
        self.manager._execute_grid_sell(session, sell_signal)
        investment_after_sell2 = session.current_investment
        print(f"  第2次卖出后投入={investment_after_sell2:.2f}")

        # 验证资金追踪
        self.assertLess(investment_after_sell1, investment_after_buy1, "卖出后资金应减少")
        self.assertGreater(investment_after_buy2, investment_after_sell1, "买入后资金应增加")
        self.assertLess(investment_after_sell2, investment_after_buy2, "卖出后资金应减少")

        # 验证最终投入不为负
        self.assertGreaterEqual(session.current_investment, 0, "投入不应为负")

        print(f"[OK] 买卖循环资金追踪正确")

    def test_partial_investment_limit(self):
        """测试7: 部分投入限额测试"""
        print("\n========== 测试7: 部分投入限额测试 ==========")

        # 限额10000, 已投入9900
        session = self._create_test_session(max_investment=10000, current_investment=9900)

        # 尝试买入
        buy_signal = {'trigger_price': 10.0, 'grid_level': 'lower'}
        result = self.manager._execute_grid_buy(session, buy_signal)

        # 剩余100元 < 最小买入100元，应拒绝
        # 注: 代码中最小买入金额是100，但计算后volume可能为0
        if result:
            # 如果成功买入
            added = session.current_investment - 9900
            self.assertLessEqual(added, 100, "新增投入应不超过剩余额度")
            self.assertLessEqual(session.current_investment, 10000, "总投入不应超过限额")
            print(f"[OK] 剩余额度100元, 成功买入{added:.2f}元")
        else:
            # 拒绝买入
            self.assertEqual(session.current_investment, 9900, "投入应保持不变")
            print(f"[OK] 剩余额度100元, 正确拒绝买入")

    def test_zero_investment_after_full_exit(self):
        """测试8: 全部卖出后投入归零"""
        print("\n========== 测试8: 全部卖出后投入归零 ==========")

        # 买入200股，投入2000
        session = self._create_test_session(max_investment=10000, current_investment=0)
        buy_signal = {'trigger_price': 10.0, 'grid_level': 'lower'}
        self.manager._execute_grid_buy(session, buy_signal)

        investment_after_buy = session.current_investment
        print(f"  买入后投入={investment_after_buy:.2f}")

        # Mock持仓200股
        position = {'volume': 200, 'cost_price': 10.0}
        self.position_manager.get_position.return_value = position

        # 卖出200股，全部退出
        session.position_ratio = 1.0  # 卖出100%
        sell_signal = {'trigger_price': 10.5, 'grid_level': 'upper'}
        self.manager._execute_grid_sell(session, sell_signal)

        # 验证投入归零
        self.assertAlmostEqual(session.current_investment, 0.0, places=2,
                              msg="全部卖出后投入应归零")

        print(f"[OK] 全部卖出后投入={session.current_investment:.2f}")


def run_tests():
    """运行所有测试"""
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestGridTradeFundManagement)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    return result


if __name__ == '__main__':
    print("=" * 80)
    print("网格交易资金管理测试")
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
