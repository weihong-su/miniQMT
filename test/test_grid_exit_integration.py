"""
网格交易退出条件测试 - 集成测试

测试目标:
1. 多条件同时满足时的优先级
2. 退出原因记录准确性
3. 退出后数据清理
4. 停止统计信息输出

测试环境:
- Python虚拟环境: C:\\Users\\PC\\Anaconda3\\envs\\python39
- 使用Mock对象模拟QMT接口
- 闭市时间测试(绕过交易时间检查)

退出条件优先级（按检查顺序）:
1. 偏离度检测 (deviation)
2. 盈亏检测 (target_profit / stop_loss)
3. 时间限制 (expired)
4. 持仓清空 (position_cleared)

测试场景:
1. 所有条件都满足: 验证偏离度优先
2. 盈亏+时间+持仓: 验证盈亏优先
3. 时间+持仓: 验证时间优先
4. 正常退出后数据清理验证
5. stop_reason字段准确性
6. 停止统计信息输出验证
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

    def clear_position(self, stock_code):
        if stock_code in self.positions:
            del self.positions[stock_code]


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
class TestGridExitIntegration(unittest.TestCase):
    """网格交易退出条件集成测试"""

    @classmethod
    def setUpClass(cls):
        """测试类初始化"""
        print("\n" + "="*80)
        print("网格交易退出条件测试 - 集成测试")
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
            'test_name': '网格交易退出条件测试 - 集成测试',
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'total_tests': len(cls.test_results),
            'passed': sum(1 for r in cls.test_results if r['passed']),
            'failed': sum(1 for r in cls.test_results if not r['passed']),
            'results': cls.test_results,
            'priority_order': [
                '1. 偏离度检测 (deviation)',
                '2. 盈亏检测 (target_profit / stop_loss)',
                '3. 时间限制 (expired)',
                '4. 持仓清空 (position_cleared)'
            ]
        }

        report_file = os.path.join(os.path.dirname(__file__), 'test_grid_exit_integration_report.json')
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

    def test_1_all_conditions_met(self):
        """测试1: 所有条件都满足, 验证偏离度优先"""
        print("\n测试1: 所有条件都满足")

        # 清空持仓
        self.mock_trader.clear_position('000001.SZ')
        self.mock_data_manager.set_current_price(10.00)

        # 创建会话: 偏离超限 + 盈利10% + 时间过期 + 持仓清空
        session = GridSession(
            stock_code='000001.SZ',
            center_price=10.00,
            current_center_price=11.51,  # 偏离15.1%
            price_interval=0.05,
            position_ratio=0.25,
            callback_ratio=0.005,
            max_investment=10000,
            max_deviation=0.15,
            target_profit=0.10,
            stop_loss=-0.10,
            buy_count=1,
            sell_count=1,
            total_buy_amount=2500,
            total_sell_amount=3500,  # 盈利10%
            start_time=datetime.now() - timedelta(days=8),
            end_time=datetime.now() - timedelta(days=1)  # 已过期
        )

        session_dict = asdict(session)
        session.id = self.db_manager.create_grid_session(session_dict)
        self.grid_manager.sessions[session.stock_code] = session

        # 检查退出条件
        exit_reason = self.grid_manager._check_exit_conditions(session, 10.00)

        passed = exit_reason == 'deviation'
        result_msg = f"预期: deviation (偏离度优先), 实际: {exit_reason}"

        self.test_results.append({
            'test_name': '所有条件都满足',
            'passed': passed,
            'conditions': '偏离超限+盈利10%+时间过期+持仓清空',
            'expected': 'deviation',
            'actual': exit_reason,
            'result': result_msg
        })

        print(f"  {'[OK] 通过' if passed else '[FAIL] 失败'}: {result_msg}")
        self.assertTrue(passed, result_msg)

    def test_2_profit_time_position(self):
        """测试2: 盈亏+时间+持仓, 验证盈亏优先"""
        print("\n测试2: 盈亏+时间+持仓")

        # 清空持仓
        self.mock_trader.clear_position('000001.SZ')
        self.mock_data_manager.set_current_price(10.00)

        # 创建会话: 盈利10% + 时间过期 + 持仓清空
        session = GridSession(
            stock_code='000001.SZ',
            center_price=10.00,
            current_center_price=10.00,  # 偏离度正常
            price_interval=0.05,
            position_ratio=0.25,
            callback_ratio=0.005,
            max_investment=10000,
            max_deviation=0.15,
            target_profit=0.10,
            stop_loss=-0.10,
            buy_count=1,
            sell_count=1,
            total_buy_amount=2500,
            total_sell_amount=3500,  # 盈利10%
            start_time=datetime.now() - timedelta(days=8),
            end_time=datetime.now() - timedelta(days=1)  # 已过期
        )

        session_dict = asdict(session)
        session.id = self.db_manager.create_grid_session(session_dict)
        self.grid_manager.sessions[session.stock_code] = session

        # 检查退出条件
        exit_reason = self.grid_manager._check_exit_conditions(session, 10.00)

        passed = exit_reason == 'target_profit'
        result_msg = f"预期: target_profit (盈亏优先), 实际: {exit_reason}"

        self.test_results.append({
            'test_name': '盈亏+时间+持仓',
            'passed': passed,
            'conditions': '盈利10%+时间过期+持仓清空',
            'expected': 'target_profit',
            'actual': exit_reason,
            'result': result_msg
        })

        print(f"  {'[OK] 通过' if passed else '[FAIL] 失败'}: {result_msg}")
        self.assertTrue(passed, result_msg)

    def test_3_time_and_position(self):
        """测试3: 时间+持仓, 验证时间优先"""
        print("\n测试3: 时间+持仓")

        # 清空持仓
        self.mock_trader.clear_position('000001.SZ')
        self.mock_data_manager.set_current_price(10.00)

        # 创建会话: 时间过期 + 持仓清空
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
            buy_count=0,  # 无交易
            sell_count=0,
            total_buy_amount=0,
            total_sell_amount=0,
            start_time=datetime.now() - timedelta(days=8),
            end_time=datetime.now() - timedelta(days=1)  # 已过期
        )

        session_dict = asdict(session)
        session.id = self.db_manager.create_grid_session(session_dict)
        self.grid_manager.sessions[session.stock_code] = session

        # 检查退出条件
        exit_reason = self.grid_manager._check_exit_conditions(session, 10.00)

        passed = exit_reason == 'expired'
        result_msg = f"预期: expired (时间优先), 实际: {exit_reason}"

        self.test_results.append({
            'test_name': '时间+持仓',
            'passed': passed,
            'conditions': '时间过期+持仓清空',
            'expected': 'expired',
            'actual': exit_reason,
            'result': result_msg
        })

        print(f"  {'[OK] 通过' if passed else '[FAIL] 失败'}: {result_msg}")
        self.assertTrue(passed, result_msg)

    def test_4_stop_and_cleanup(self):
        """测试4: 正常退出后数据清理验证"""
        print("\n测试4: 退出后数据清理")

        # 设置持仓
        self.mock_trader.set_position('000001.SZ', 1000, 10.00)
        self.mock_data_manager.set_current_price(10.00)

        # 创建会话 (end_time不能为None, 设置默认值)
        session = GridSession(
            stock_code='000001.SZ',
            center_price=10.00,
            current_center_price=11.51,  # 偏离15.1%
            price_interval=0.05,
            position_ratio=0.25,
            callback_ratio=0.005,
            max_investment=10000,
            max_deviation=0.15,
            target_profit=0.10,
            stop_loss=-0.10,
            start_time=datetime.now(),
            end_time=datetime.now() + timedelta(days=7)
        )

        session_dict = asdict(session)
        session.id = self.db_manager.create_grid_session(session_dict)
        self.grid_manager.sessions[session.stock_code] = session
        self.grid_manager.trackers[session.id] = MagicMock()

        # 停止会话
        result = self.grid_manager.stop_grid_session(session.id, 'deviation')

        # 验证清理
        session_in_memory = '000001.SZ' in self.grid_manager.sessions
        tracker_in_memory = session.id in self.grid_manager.trackers
        stop_reason_correct = result['stop_reason'] == 'deviation'

        passed = not session_in_memory and not tracker_in_memory and stop_reason_correct
        result_msg = f"sessions清理: {not session_in_memory}, trackers清理: {not tracker_in_memory}, stop_reason正确: {stop_reason_correct}"

        self.test_results.append({
            'test_name': '退出后数据清理',
            'passed': passed,
            'sessions_cleared': not session_in_memory,
            'trackers_cleared': not tracker_in_memory,
            'stop_reason_correct': stop_reason_correct,
            'result': result_msg
        })

        print(f"  {'[OK] 通过' if passed else '[FAIL] 失败'}: {result_msg}")
        self.assertTrue(passed, result_msg)

    def test_5_stop_reason_accuracy(self):
        """测试5: stop_reason字段准确性"""
        print("\n测试5: stop_reason字段准确性")

        test_cases = [
            ('deviation', {'current_center_price': 11.51}),
            ('target_profit', {'buy_count': 1, 'sell_count': 1, 'total_buy_amount': 2500, 'total_sell_amount': 3500}),
            ('stop_loss', {'buy_count': 1, 'sell_count': 1, 'total_buy_amount': 2500, 'total_sell_amount': 1500}),
            ('expired', {'end_time': datetime.now() - timedelta(days=1)}),
        ]

        for expected_reason, params in test_cases:
            # 清理
            self.setUp()

            # 设置持仓
            self.mock_trader.set_position('000001.SZ', 1000, 10.00)
            if expected_reason == 'position_cleared':
                self.mock_trader.clear_position('000001.SZ')

            # 创建会话 (end_time不能为None, 设置默认值)
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
                start_time=datetime.now(),
                end_time=datetime.now() + timedelta(days=7)  # 默认值
            )

            # 应用参数
            for key, value in params.items():
                setattr(session, key, value)

            session_dict = asdict(session)
            session.id = self.db_manager.create_grid_session(session_dict)
            self.grid_manager.sessions[session.stock_code] = session

            # 停止会话
            result = self.grid_manager.stop_grid_session(session.id, expected_reason)

            # 验证stop_reason
            passed = result['stop_reason'] == expected_reason
            result_msg = f"预期: {expected_reason}, 实际: {result['stop_reason']}"

            self.test_results.append({
                'test_name': f'stop_reason准确性-{expected_reason}',
                'passed': passed,
                'expected': expected_reason,
                'actual': result['stop_reason'],
                'result': result_msg
            })

            print(f"  {expected_reason}: {'[OK] 通过' if passed else '[FAIL] 失败'}")
            self.assertTrue(passed, result_msg)

            # 清理
            self.tearDown()


if __name__ == '__main__':
    # 运行测试
    unittest.main(verbosity=2)
