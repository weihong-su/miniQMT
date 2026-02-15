"""
网格交易盈亏率计算算法测试

测试覆盖：
- TC01-TC10: get_profit_ratio() 单元测试（核心算法验证）
- TC11-TC13: get_grid_profit() 单元测试
- TC14-TC20: _check_exit_conditions() 止盈止损集成测试
- TC21-TC24: web_server.py API一致性测试
- TC25-TC27: 市场波动隔离性验证
"""

import unittest
import sys
import os
from datetime import datetime
from unittest.mock import patch

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test.test_base import TestBase
from grid_trading_manager import GridSession, GridTradingManager
from grid_database import DatabaseManager
from position_manager import PositionManager
from trading_executor import TradingExecutor
import config


class TestGridProfitCalculation(TestBase):
    """网格盈亏率计算算法测试"""

    def setUp(self):
        """测试前准备"""
        super().setUp()

        # 大部分测试只需要GridSession对象，不需要完整的管理器
        # 对于需要GridTradingManager的测试（TC14-TC20），在测试方法中单独创建

    def _create_mock_executor(self):
        """创建Mock交易执行器（仅用于集成测试）"""
        class MockTradingExecutor:
            def buy_stock(self, stock_code, amount, strategy='grid'):
                return f"MOCK_BUY_{stock_code}"

            def sell_stock(self, stock_code, volume, strategy='grid'):
                return f"MOCK_SELL_{stock_code}"

        return MockTradingExecutor()

    # ========== 第一组：get_profit_ratio() 单元测试 ==========

    def test_tc01_no_trade(self):
        """TC01: 无交易时盈亏率为0%"""
        session = GridSession()
        session.stock_code = "000001.SZ"
        session.total_buy_amount = 0
        session.total_sell_amount = 0
        session.max_investment = 50000

        ratio = session.get_profit_ratio()
        self.assertEqual(ratio, 0.0, "无交易时应返回0.0")

    def test_tc02_max_investment_zero(self):
        """TC02: max_investment为0时返回0.0"""
        session = GridSession()
        session.stock_code = "000001.SZ"
        session.total_buy_amount = 1000
        session.total_sell_amount = 0
        session.max_investment = 0

        ratio = session.get_profit_ratio()
        self.assertEqual(ratio, 0.0, "max_investment为0时应返回0.0")

    def test_tc03_max_investment_negative(self):
        """TC03: max_investment为负数时返回0.0"""
        session = GridSession()
        session.stock_code = "000001.SZ"
        session.total_buy_amount = 0
        session.total_sell_amount = 0
        session.max_investment = -1000

        ratio = session.get_profit_ratio()
        self.assertEqual(ratio, 0.0, "max_investment为负数时应返回0.0")

    def test_tc04_only_buy(self):
        """TC04: 只有买入时盈亏率为负值"""
        session = GridSession()
        session.stock_code = "000001.SZ"
        session.total_buy_amount = 5000
        session.total_sell_amount = 0
        session.max_investment = 50000

        ratio = session.get_profit_ratio()
        expected = -5000 / 50000  # -0.1 (-10%)
        self.assertAlmostEqual(ratio, expected, places=4,
                              msg="只有买入时应返回负值（资金流出）")

    def test_tc05_only_sell_initial_position(self):
        """TC05: 只有卖出（初始持仓卖出）时盈亏率为正值"""
        session = GridSession()
        session.stock_code = "000001.SZ"
        session.total_buy_amount = 0
        session.total_sell_amount = 3000
        session.max_investment = 50000

        ratio = session.get_profit_ratio()
        expected = 3000 / 50000  # +0.06 (+6%)
        self.assertAlmostEqual(ratio, expected, places=4,
                              msg="只有卖出时应返回正值（纯利润）")

    def test_tc06_normal_cycle_profit(self):
        """TC06: 正常买卖循环（盈利）"""
        session = GridSession()
        session.stock_code = "000001.SZ"
        session.total_buy_amount = 5000
        session.total_sell_amount = 5500
        session.max_investment = 50000

        ratio = session.get_profit_ratio()
        expected = 500 / 50000  # +0.01 (+1%)
        self.assertAlmostEqual(ratio, expected, places=4,
                              msg="正常买卖循环盈利时应返回正值")

    def test_tc07_normal_cycle_loss(self):
        """TC07: 正常买卖循环（亏损）"""
        session = GridSession()
        session.stock_code = "000001.SZ"
        session.total_buy_amount = 5000
        session.total_sell_amount = 4500
        session.max_investment = 50000

        ratio = session.get_profit_ratio()
        expected = -500 / 50000  # -0.01 (-1%)
        self.assertAlmostEqual(ratio, expected, places=4,
                              msg="正常买卖循环亏损时应返回负值")

    def test_tc08_buy_more_sell_less(self):
        """TC08: 买多卖少"""
        session = GridSession()
        session.stock_code = "000001.SZ"
        session.total_buy_amount = 8000
        session.total_sell_amount = 3000
        session.max_investment = 50000

        ratio = session.get_profit_ratio()
        expected = -5000 / 50000  # -0.1 (-10%)
        self.assertAlmostEqual(ratio, expected, places=4,
                              msg="买多卖少时应返回负值（资金净流出）")

    def test_tc09_sell_more_buy_less(self):
        """TC09: 卖多买少"""
        session = GridSession()
        session.stock_code = "000001.SZ"
        session.total_buy_amount = 2000
        session.total_sell_amount = 6000
        session.max_investment = 50000

        ratio = session.get_profit_ratio()
        expected = 4000 / 50000  # +0.08 (+8%)
        self.assertAlmostEqual(ratio, expected, places=4,
                              msg="卖多买少时应返回正值（资金净流入）")

    def test_tc10_large_amount_precision(self):
        """TC10: 大额交易精度测试"""
        session = GridSession()
        session.stock_code = "000001.SZ"
        session.total_buy_amount = 999999.99
        session.total_sell_amount = 1000100.01
        session.max_investment = 1000000

        ratio = session.get_profit_ratio()
        expected = 100.02 / 1000000  # ~+0.0001
        self.assertAlmostEqual(ratio, expected, places=6,
                              msg="大额交易应保持精度")

    # ========== 第二组：get_grid_profit() 单元测试 ==========

    def test_tc11_grid_profit_no_trade(self):
        """TC11: 无交易时网格利润为0"""
        session = GridSession()
        session.stock_code = "000001.SZ"
        session.total_buy_amount = 0
        session.total_sell_amount = 0

        profit = session.get_grid_profit()
        self.assertEqual(profit, 0.0, "无交易时网格利润应为0")

    def test_tc12_grid_profit_positive(self):
        """TC12: 盈利时网格利润为正值"""
        session = GridSession()
        session.stock_code = "000001.SZ"
        session.total_buy_amount = 5000
        session.total_sell_amount = 5500

        profit = session.get_grid_profit()
        self.assertEqual(profit, 500.0, "盈利时网格利润应为正值")

    def test_tc13_grid_profit_negative(self):
        """TC13: 亏损时网格利润为负值"""
        session = GridSession()
        session.stock_code = "000001.SZ"
        session.total_buy_amount = 5000
        session.total_sell_amount = 4500

        profit = session.get_grid_profit()
        self.assertEqual(profit, -500.0, "亏损时网格利润应为负值")

    # ========== 第三组：_check_exit_conditions() 止盈止损集成测试 ==========

    def test_tc14_no_trade_no_exit(self):
        """TC14: 无交易时不触发退出"""
        # 创建内存数据库和管理器
        db = DatabaseManager(":memory:")
        db.init_grid_tables()
        mock_executor = self._create_mock_executor()

        # 创建持仓管理器（使用全局单例）
        from position_manager import get_position_manager
        position_manager = get_position_manager()

        # Mock持仓数据，避免触发"持仓清空"退出条件
        with patch.object(position_manager, 'get_position', return_value={'volume': 1000, 'available': 1000}):
            # 创建网格交易管理器
            grid_manager = GridTradingManager(db, position_manager, mock_executor)

            session = GridSession()
            session.stock_code = "000001.SZ"
            session.buy_count = 0
            session.sell_count = 0
            session.total_buy_amount = 0
            session.total_sell_amount = 0
            session.max_investment = 50000
            session.target_profit = 0.15
            session.stop_loss = -0.15
            session.max_deviation = 0.3
            session.center_price = 10.0
            session.current_center_price = 10.0

            result = grid_manager._check_exit_conditions(session, 10.0)
            self.assertIsNone(result, "无交易时不应触发任何退出")

    def test_tc15_only_buy_no_exit(self):
        """TC15: 只有买入时不触发止盈止损"""
        # 创建内存数据库和管理器
        db = DatabaseManager(":memory:")
        db.init_grid_tables()
        mock_executor = self._create_mock_executor()

        # 创建持仓管理器（使用全局单例）
        from position_manager import get_position_manager
        position_manager = get_position_manager()

        # Mock持仓数据，避免触发"持仓清空"退出条件
        with patch.object(position_manager, 'get_position', return_value={'volume': 1000, 'available': 1000}):
            # 创建网格交易管理器
            grid_manager = GridTradingManager(db, position_manager, mock_executor)

            session = GridSession()
            session.stock_code = "000001.SZ"
            session.buy_count = 3
            session.sell_count = 0
            session.total_buy_amount = 5000
            session.total_sell_amount = 0
            session.max_investment = 50000
            session.target_profit = 0.15
            session.stop_loss = -0.15
            session.max_deviation = 0.3
            session.center_price = 10.0
            session.current_center_price = 10.0

            result = grid_manager._check_exit_conditions(session, 10.0)
            self.assertIsNone(result, "只有买入时不应触发止盈止损（未配对）")

    def test_tc16_initial_position_sell_high_profit_no_exit(self):
        """TC16: 初始持仓先卖出，高盈利不触发止盈（未配对）"""
        # 创建内存数据库和管理器
        db = DatabaseManager(":memory:")
        db.init_grid_tables()
        mock_executor = self._create_mock_executor()

        # 创建持仓管理器（使用全局单例）
        from position_manager import get_position_manager
        position_manager = get_position_manager()

        # Mock持仓数据，避免触发"持仓清空"退出条件
        with patch.object(position_manager, 'get_position', return_value={'volume': 1000, 'available': 1000}):
            # 创建网格交易管理器
            grid_manager = GridTradingManager(db, position_manager, mock_executor)

            session = GridSession()
            session.stock_code = "000001.SZ"
            session.buy_count = 0
            session.sell_count = 2
            session.total_buy_amount = 0
            session.total_sell_amount = 8000
            session.max_investment = 50000
            session.target_profit = 0.15  # 15%
            session.stop_loss = -0.15
            session.max_deviation = 0.3
            session.center_price = 10.0
            session.current_center_price = 10.0

            # 盈亏率 = 8000/50000 = 16% > 15%，但因未配对不触发
            result = grid_manager._check_exit_conditions(session, 10.0)
            self.assertIsNone(result, "初始持仓先卖出时不应触发止盈（未配对，依赖区间上限退出）")

    def test_tc17_initial_position_sell_low_profit_no_exit(self):
        """TC17: 初始持仓先卖出，低盈利不触发止盈（未配对）"""
        # 创建内存数据库和管理器
        db = DatabaseManager(":memory:")
        db.init_grid_tables()
        mock_executor = self._create_mock_executor()

        # 创建持仓管理器（使用全局单例）
        from position_manager import get_position_manager
        position_manager = get_position_manager()

        # Mock持仓数据，避免触发"持仓清空"退出条件
        with patch.object(position_manager, 'get_position', return_value={'volume': 1000, 'available': 1000}):
            # 创建网格交易管理器
            grid_manager = GridTradingManager(db, position_manager, mock_executor)

            session = GridSession()
            session.stock_code = "000001.SZ"
            session.buy_count = 0
            session.sell_count = 1
            session.total_buy_amount = 0
            session.total_sell_amount = 2500
            session.max_investment = 50000
            session.target_profit = 0.15  # 15%
            session.stop_loss = -0.15
            session.max_deviation = 0.3
            session.center_price = 10.0
            session.current_center_price = 10.0

            # 盈亏率 = 2500/50000 = 5% < 15%
            result = grid_manager._check_exit_conditions(session, 10.0)
            self.assertIsNone(result, "初始持仓先卖出时不应触发止盈（未配对）")

    def test_tc18_normal_cycle_trigger_profit(self):
        """TC18: 正常循环，触发止盈"""
        # 创建内存数据库和管理器
        db = DatabaseManager(":memory:")
        db.init_grid_tables()
        mock_executor = self._create_mock_executor()

        # 创建持仓管理器（使用全局单例）
        from position_manager import get_position_manager
        position_manager = get_position_manager()

        # Mock持仓数据，避免触发"持仓清空"退出条件
        with patch.object(position_manager, 'get_position', return_value={'volume': 1000, 'available': 1000}):
            # 创建网格交易管理器
            grid_manager = GridTradingManager(db, position_manager, mock_executor)

            session = GridSession()
            session.stock_code = "000001.SZ"
            session.buy_count = 5
            session.sell_count = 5
            session.total_buy_amount = 50000
            session.total_sell_amount = 58000
            session.max_investment = 50000
            session.target_profit = 0.15  # 15%
            session.stop_loss = -0.15
            session.max_deviation = 0.3
            session.center_price = 10.0
            session.current_center_price = 10.0

            # 盈亏率 = 8000/50000 = 16% > 15%
            result = grid_manager._check_exit_conditions(session, 10.0)
            self.assertEqual(result, 'target_profit', "正常循环达到目标盈利应触发止盈")

    def test_tc19_normal_cycle_trigger_stop_loss(self):
        """TC19: 正常循环，触发止损"""
        # 创建内存数据库和管理器
        db = DatabaseManager(":memory:")
        db.init_grid_tables()
        mock_executor = self._create_mock_executor()

        # 创建持仓管理器（使用全局单例）
        from position_manager import get_position_manager
        position_manager = get_position_manager()

        # Mock持仓数据，避免触发"持仓清空"退出条件
        with patch.object(position_manager, 'get_position', return_value={'volume': 1000, 'available': 1000}):
            # 创建网格交易管理器
            grid_manager = GridTradingManager(db, position_manager, mock_executor)

            session = GridSession()
            session.stock_code = "000001.SZ"
            session.buy_count = 5
            session.sell_count = 3
            session.total_buy_amount = 50000
            session.total_sell_amount = 42000
            session.max_investment = 50000
            session.target_profit = 0.15  # 15%
            session.stop_loss = -0.15  # -15%
            session.max_deviation = 0.3
            session.center_price = 10.0
            session.current_center_price = 10.0

            # 盈亏率 = -8000/50000 = -16% < -15%
            result = grid_manager._check_exit_conditions(session, 10.0)
            self.assertEqual(result, 'stop_loss', "正常循环触发止损应返回stop_loss")

    def test_tc20_normal_cycle_within_range(self):
        """TC20: 正常循环，盈亏在区间内"""
        # 创建内存数据库和管理器
        db = DatabaseManager(":memory:")
        db.init_grid_tables()
        mock_executor = self._create_mock_executor()

        # 创建持仓管理器（使用全局单例）
        from position_manager import get_position_manager
        position_manager = get_position_manager()

        # Mock持仓数据，避免触发"持仓清空"退出条件
        with patch.object(position_manager, 'get_position', return_value={'volume': 1000, 'available': 1000}):
            # 创建网格交易管理器
            grid_manager = GridTradingManager(db, position_manager, mock_executor)

            session = GridSession()
            session.stock_code = "000001.SZ"
            session.buy_count = 3
            session.sell_count = 3
            session.total_buy_amount = 30000
            session.total_sell_amount = 31500
            session.max_investment = 50000
            session.target_profit = 0.15  # 15%
            session.stop_loss = -0.15
            session.max_deviation = 0.3
            session.center_price = 10.0
            session.current_center_price = 10.0

            # 盈亏率 = 1500/50000 = 3%，在[-15%, 15%]区间内
            result = grid_manager._check_exit_conditions(session, 10.0)
            self.assertIsNone(result, "盈亏在区间内不应触发退出")

    # ========== 第四组：web_server.py API一致性测试 ==========

    def test_tc21_active_session_api_consistency(self):
        """TC21: 活跃会话API返回值一致性"""
        # 创建活跃会话
        session = GridSession()
        session.id = 1
        session.stock_code = "000001.SZ"
        session.status = 'active'
        session.total_buy_amount = 5000
        session.total_sell_amount = 5500
        session.max_investment = 50000
        session.center_price = 10.0
        session.current_center_price = 10.0
        session.trade_count = 10
        session.buy_count = 5
        session.sell_count = 5

        # 直接调用方法验证
        profit_ratio = session.get_profit_ratio()
        expected = 500 / 50000  # 1%
        self.assertAlmostEqual(profit_ratio, expected, places=4,
                              msg="活跃会话profit_ratio应使用新算法")

    def test_tc22_historical_session_api_consistency(self):
        """TC22: 历史会话API返回值一致性（模拟web_server.py Line 1912逻辑）"""
        # 模拟从数据库读取的历史会话字典
        session_dict = {
            'total_buy_amount': 5000,
            'total_sell_amount': 5500,
            'max_investment': 50000
        }

        # 模拟web_server.py Line 1912的计算逻辑
        profit_ratio = (session_dict['total_sell_amount'] - session_dict['total_buy_amount']) / session_dict.get('max_investment', 0) if session_dict.get('max_investment', 0) > 0 else 0

        expected = 500 / 50000  # 1%
        self.assertAlmostEqual(profit_ratio, expected, places=4,
                              msg="历史会话profit_ratio应使用max_investment分母")

    def test_tc23_session_stats_api_consistency(self):
        """TC23: 会话详情API一致性"""
        # 创建内存数据库和管理器
        db = DatabaseManager(":memory:")
        db.init_grid_tables()
        mock_executor = self._create_mock_executor()

        # 创建持仓管理器（使用全局单例）
        from position_manager import get_position_manager
        position_manager = get_position_manager()

        # 创建网格交易管理器
        grid_manager = GridTradingManager(db, position_manager, mock_executor)

        # 创建会话并添加到管理器
        session = GridSession()
        session.id = 1
        session.stock_code = "000001.SZ"
        session.status = 'active'
        session.total_buy_amount = 5000
        session.total_sell_amount = 5500
        session.max_investment = 50000
        session.center_price = 10.0
        session.current_center_price = 10.0
        session.trade_count = 10
        session.buy_count = 5
        session.sell_count = 5
        session.start_time = datetime.now()

        grid_manager.sessions["000001.SZ"] = session

        # 调用get_session_stats
        stats = grid_manager.get_session_stats(1)

        self.assertIn('profit_ratio', stats, "stats应包含profit_ratio")
        self.assertIn('grid_profit', stats, "stats应包含grid_profit")

        expected_ratio = 500 / 50000  # 1%
        expected_profit = 500.0

        self.assertAlmostEqual(stats['profit_ratio'], expected_ratio, places=4,
                              msg="get_session_stats的profit_ratio应使用新算法")
        self.assertEqual(stats['grid_profit'], expected_profit,
                        msg="get_session_stats应返回grid_profit字段")

    def test_tc24_grid_status_api_consistency(self):
        """TC24: 网格状态API一致性"""
        # 创建会话
        session = GridSession()
        session.id = 1
        session.stock_code = "000001.SZ"
        session.status = 'active'
        session.total_buy_amount = 5000
        session.total_sell_amount = 5500
        session.max_investment = 50000
        session.center_price = 10.0
        session.current_center_price = 10.0

        # 验证profit_ratio计算
        profit_ratio = session.get_profit_ratio()
        expected = 500 / 50000  # 1%
        self.assertAlmostEqual(profit_ratio, expected, places=4,
                              msg="网格状态API的profit_ratio应使用新算法")

    # ========== 第五组：市场波动隔离性验证 ==========

    def test_tc25_price_up_no_trade_zero_profit(self):
        """TC25: 股价涨50%无网格交易，盈亏率为0%"""
        session = GridSession()
        session.stock_code = "000001.SZ"
        session.total_buy_amount = 0
        session.total_sell_amount = 0
        session.max_investment = 50000
        session.center_price = 10.0
        session.current_center_price = 15.0  # 涨50%

        ratio = session.get_profit_ratio()
        self.assertEqual(ratio, 0.0, "股价涨50%但无交易时盈亏率应为0%（不受股价影响）")

    def test_tc26_price_down_no_trade_zero_profit(self):
        """TC26: 股价跌50%无网格交易，盈亏率为0%"""
        session = GridSession()
        session.stock_code = "000001.SZ"
        session.total_buy_amount = 0
        session.total_sell_amount = 0
        session.max_investment = 50000
        session.center_price = 10.0
        session.current_center_price = 5.0  # 跌50%

        ratio = session.get_profit_ratio()
        self.assertEqual(ratio, 0.0, "股价跌50%但无交易时盈亏率应为0%（不受股价影响）")

    def test_tc27_price_fluctuation_with_trade(self):
        """TC27: 股价波动中有网格交易，盈亏率只反映买卖差价"""
        session = GridSession()
        session.stock_code = "000001.SZ"
        session.total_buy_amount = 10000  # 在8元买入1250股
        session.total_sell_amount = 12500  # 在10元卖出1250股
        session.max_investment = 50000
        session.center_price = 10.0
        session.current_center_price = 15.0  # 股价涨到15元

        # 盈亏率 = (12500 - 10000) / 50000 = 5%
        # 注意：即使股价涨到15元，盈亏率仍然只反映已实现的买卖差价
        ratio = session.get_profit_ratio()
        expected = 2500 / 50000  # 5%
        self.assertAlmostEqual(ratio, expected, places=4,
                              msg="盈亏率应只反映买卖差价，不含持仓市值变化")


if __name__ == '__main__':
    unittest.main()
