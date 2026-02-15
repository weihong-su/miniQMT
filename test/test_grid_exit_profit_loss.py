"""
网格交易退出条件测试 - 盈亏退出

测试目标:
1. 止盈触发（达到target_profit）
2. 止损触发（达到stop_loss）
3. 配对操作检查（必须至少1买1卖）
4. 未配对时不检测盈亏
5. 盈亏比例计算验证

测试环境:
- Python虚拟环境: C:\\Users\\PC\\Anaconda3\\envs\\python39
- 使用Mock对象模拟QMT接口
- 闭市时间测试(绕过交易时间检查)

测试设计:
- 目标盈利: 10% (0.10)
- 止损: -10% (-0.10)
- max_investment: 10000元
- 测试场景:
  1. 无交易: buy_count=0, sell_count=0, 不触发
  2. 仅买入: buy_count=1, sell_count=0, 不触发
  3. 仅卖出: buy_count=0, sell_count=1, 不触发
  4. 配对且盈利9%: 不触发
  5. 配对且盈利10%: 触发止盈
  6. 配对且盈利11%: 触发止盈
  7. 配对且亏损9%: 不触发
  8. 配对且亏损10%: 触发止损
  9. 配对且亏损11%: 触发止损
"""

import unittest
import sys
import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock
from dataclasses import asdict
import json

# 添加项目根目录到路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# 导入配置和模块
import config
from logger import get_logger
from grid_trading_manager import GridTradingManager, GridSession
from grid_database import DatabaseManager
from position_manager import PositionManager

logger = get_logger(__name__)

# ==================== Mock类 ====================
class MockXtQuantTrader:
    """模拟XtQuantTrader"""
    def __init__(self):
        self.connected = True
        self.positions = {}
        self.account_info = {'cash': 100000.0, 'total_asset': 100000.0}

    def connect(self):
        self.connected = True
        return True

    def is_connected(self):
        return self.connected

    def query_stock_positions(self, account):
        return list(self.positions.values())

    def query_stock_asset(self, account):
        return self.account_info

    def set_position(self, stock_code, volume, cost_price):
        self.positions[stock_code] = {
            'm_strInstrumentID': stock_code,
            'm_nVolume': volume,
            'm_nCanUseVolume': volume,
            'm_dOpenPrice': cost_price
        }


class MockDataManager:
    """模拟DataManager"""
    def __init__(self):
        self.current_price = 10.00

    def get_latest_data(self, stock_code):
        return {stock_code: {'close': self.current_price}}

    def set_current_price(self, price):
        self.current_price = price


class MockPositionManager:
    """模拟持仓管理器"""
    def __init__(self, qmt_trader):
        self.qmt_trader = qmt_trader
        self.current_prices = {}

    def update_current_price(self, stock_code, price):
        self.current_prices[stock_code] = price

    def get_position(self, stock_code):
        positions = self.qmt_trader.query_stock_positions(None)
        for pos in positions:
            if pos['m_strInstrumentID'] == stock_code:
                current_price = self.current_prices.get(stock_code, pos['m_dOpenPrice'])
                return {
                    'stock_code': stock_code,
                    'volume': pos['m_nVolume'],
                    'can_use_volume': pos['m_nCanUseVolume'],
                    'cost_price': pos['m_dOpenPrice'],
                    'current_price': current_price,
                    'market_value': current_price * pos['m_nVolume']
                }
        return None

    def _increment_data_version(self):
        pass


# ==================== 测试类 ====================
class TestGridExitProfitLoss(unittest.TestCase):
    """网格交易盈亏退出测试"""

    @classmethod
    def setUpClass(cls):
        """测试类初始化"""
        print("\n" + "="*80)
        print("网格交易退出条件测试 - 盈亏退出")
        print("="*80)

        # 保存原始配置
        cls.original_simulation = config.ENABLE_SIMULATION_MODE
        cls.original_grid_enabled = config.ENABLE_GRID_TRADING
        cls.original_debug_simu = config.DEBUG_SIMU_STOCK_DATA

        # 设置测试配置
        config.ENABLE_SIMULATION_MODE = False
        config.ENABLE_GRID_TRADING = True
        config.DEBUG_SIMU_STOCK_DATA = True

        cls.test_results = []

    @classmethod
    def tearDownClass(cls):
        """测试类清理"""
        # 恢复原始配置
        config.ENABLE_SIMULATION_MODE = cls.original_simulation
        config.ENABLE_GRID_TRADING = cls.original_grid_enabled
        config.DEBUG_SIMU_STOCK_DATA = cls.original_debug_simu

        # 生成测试报告
        cls._generate_report()

    @classmethod
    def _generate_report(cls):
        """生成测试报告"""
        report = {
            'test_name': '网格交易退出条件测试 - 盈亏退出',
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'total_tests': len(cls.test_results),
            'passed': sum(1 for r in cls.test_results if r['passed']),
            'failed': sum(1 for r in cls.test_results if not r['passed']),
            'results': cls.test_results
        }

        report_file = os.path.join(os.path.dirname(__file__), 'test_grid_exit_profit_loss_report.json')
        with open(report_file, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        print(f"\n{'='*80}")
        print(f"测试报告已生成: {report_file}")
        print(f"总测试数: {report['total_tests']}, 通过: {report['passed']}, 失败: {report['failed']}")
        print(f"{'='*80}\n")

    def setUp(self):
        """每个测试前初始化"""
        # 清理数据库
        db_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'positions.db')
        if os.path.exists(db_path):
            os.remove(db_path)

        # 创建Mock对象
        self.mock_trader = MockXtQuantTrader()
        self.mock_data_manager = MockDataManager()

        # 创建数据库管理器
        self.db_manager = DatabaseManager()
        # 初始化网格交易表
        self.db_manager.init_grid_tables()

        # 创建持仓管理器
        self.position_manager = MockPositionManager(self.mock_trader)

        # 创建网格交易管理器
        self.grid_manager = GridTradingManager(
            db_manager=self.db_manager,
            position_manager=self.position_manager,
            trading_executor=None
        )

    def tearDown(self):
        """每个测试后清理"""
        if hasattr(self, 'db_manager'):
            self.db_manager.close()

    def _create_test_session(self, buy_count, sell_count, total_buy_amount, total_sell_amount):
        """创建测试会话"""
        # 设置初始持仓
        self.mock_trader.set_position('000001.SZ', 1000, 10.00)
        self.mock_data_manager.set_current_price(10.00)

        # 创建网格会话
        session = GridSession(
            stock_code='000001.SZ',
            center_price=10.00,
            current_center_price=10.00,
            price_interval=0.05,
            position_ratio=0.25,
            callback_ratio=0.005,
            max_investment=10000,
            max_deviation=0.15,
            target_profit=0.10,
            stop_loss=-0.10,
            buy_count=buy_count,
            sell_count=sell_count,
            total_buy_amount=total_buy_amount,
            total_sell_amount=total_sell_amount,
            start_time=datetime.now(),
            end_time=datetime.now() + timedelta(days=7)
        )

        # 插入数据库
        session_dict = asdict(session)
        session.id = self.db_manager.create_grid_session(session_dict)

        # 加载到内存
        self.grid_manager.sessions[session.stock_code] = session

        return session

    def _check_exit_and_record(self, test_name, session, expected_reason):
        """检查退出条件并记录结果"""
        exit_reason = self.grid_manager._check_exit_conditions(session, 10.00)

        # 验证结果
        passed = exit_reason == expected_reason
        result_msg = f"预期: {expected_reason}, 实际: {exit_reason}"

        # 计算盈亏比例
        profit_ratio = session.get_profit_ratio()

        # 记录测试结果
        self.test_results.append({
            'test_name': test_name,
            'passed': passed,
            'buy_count': session.buy_count,
            'sell_count': session.sell_count,
            'total_buy_amount': session.total_buy_amount,
            'total_sell_amount': session.total_sell_amount,
            'profit_ratio': f"{profit_ratio*100:.2f}%",
            'target_profit': f"{session.target_profit*100:.2f}%",
            'stop_loss': f"{session.stop_loss*100:.2f}%",
            'exit_reason': exit_reason,
            'result': result_msg
        })

        print(f"\n{test_name}: {'[OK] 通过' if passed else '[FAIL] 失败'}")
        print(f"  买入次数: {session.buy_count}, 卖出次数: {session.sell_count}")
        print(f"  买入总额: {session.total_buy_amount:.2f}, 卖出总额: {session.total_sell_amount:.2f}")
        print(f"  盈亏比例: {profit_ratio*100:.2f}%")
        print(f"  {result_msg}")

        self.assertTrue(passed, result_msg)

    def test_1_no_trades(self):
        """测试1: 无交易, 不触发退出"""
        session = self._create_test_session(
            buy_count=0,
            sell_count=0,
            total_buy_amount=0,
            total_sell_amount=0
        )
        self._check_exit_and_record('无交易', session, expected_reason=None)

    def test_2_only_buy(self):
        """测试2: 仅买入, 不触发退出"""
        session = self._create_test_session(
            buy_count=1,
            sell_count=0,
            total_buy_amount=2500,
            total_sell_amount=0
        )
        self._check_exit_and_record('仅买入', session, expected_reason=None)

    def test_3_only_sell(self):
        """测试3: 仅卖出, 不触发退出"""
        session = self._create_test_session(
            buy_count=0,
            sell_count=1,
            total_buy_amount=0,
            total_sell_amount=2500
        )
        self._check_exit_and_record('仅卖出', session, expected_reason=None)

    def test_4_profit_9_percent(self):
        """测试4: 配对且盈利9%, 不触发退出"""
        # 盈利9%: (sell - buy) / max_investment = 0.09
        # sell - buy = 900
        session = self._create_test_session(
            buy_count=1,
            sell_count=1,
            total_buy_amount=2500,
            total_sell_amount=3400  # 2500 + 900
        )
        self._check_exit_and_record('盈利9%', session, expected_reason=None)

    def test_5_profit_10_percent(self):
        """测试5: 配对且盈利10%, 触发止盈"""
        # 盈利10%: (sell - buy) / max_investment = 0.10
        # sell - buy = 1000
        session = self._create_test_session(
            buy_count=1,
            sell_count=1,
            total_buy_amount=2500,
            total_sell_amount=3500  # 2500 + 1000
        )
        self._check_exit_and_record('盈利10%', session, expected_reason='target_profit')

    def test_6_profit_11_percent(self):
        """测试6: 配对且盈利11%, 触发止盈"""
        # 盈利11%: (sell - buy) / max_investment = 0.11
        # sell - buy = 1100
        session = self._create_test_session(
            buy_count=1,
            sell_count=1,
            total_buy_amount=2500,
            total_sell_amount=3600  # 2500 + 1100
        )
        self._check_exit_and_record('盈利11%', session, expected_reason='target_profit')

    def test_7_loss_9_percent(self):
        """测试7: 配对且亏损9%, 不触发退出"""
        # 亏损9%: (sell - buy) / max_investment = -0.09
        # sell - buy = -900
        session = self._create_test_session(
            buy_count=1,
            sell_count=1,
            total_buy_amount=2500,
            total_sell_amount=1600  # 2500 - 900
        )
        self._check_exit_and_record('亏损9%', session, expected_reason=None)

    def test_8_loss_10_percent(self):
        """测试8: 配对且亏损10%, 触发止损"""
        # 亏损10%: (sell - buy) / max_investment = -0.10
        # sell - buy = -1000
        session = self._create_test_session(
            buy_count=1,
            sell_count=1,
            total_buy_amount=2500,
            total_sell_amount=1500  # 2500 - 1000
        )
        self._check_exit_and_record('亏损10%', session, expected_reason='stop_loss')

    def test_9_loss_11_percent(self):
        """测试9: 配对且亏损11%, 触发止损"""
        # 亏损11%: (sell - buy) / max_investment = -0.11
        # sell - buy = -1100
        session = self._create_test_session(
            buy_count=1,
            sell_count=1,
            total_buy_amount=2500,
            total_sell_amount=1400  # 2500 - 1100
        )
        self._check_exit_and_record('亏损11%', session, expected_reason='stop_loss')

    def test_10_multiple_trades_profit(self):
        """测试10: 多次交易且盈利10%, 触发止盈"""
        # 多次交易: buy_count=3, sell_count=2
        # 盈利10%: (sell - buy) / max_investment = 0.10
        session = self._create_test_session(
            buy_count=3,
            sell_count=2,
            total_buy_amount=7500,
            total_sell_amount=8500  # 7500 + 1000
        )
        self._check_exit_and_record('多次交易且盈利10%', session, expected_reason='target_profit')


if __name__ == '__main__':
    # 运行测试
    unittest.main(verbosity=2)
