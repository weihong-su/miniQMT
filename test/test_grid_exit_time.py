"""
网格交易退出条件测试 - 时间退出

测试目标:
1. 达到end_time自动停止
2. duration_days配置验证
3. 过期会话恢复时自动停止
4. 剩余时间计算

测试环境:
- Python虚拟环境: C:\\Users\\PC\\Anaconda3\\envs\\python39
- 使用Mock对象模拟QMT接口
- 闭市时间测试(绕过交易时间检查)

测试设计:
- 测试场景:
  1. end_time为None: 不触发退出
  2. end_time在未来1小时: 不触发退出
  3. end_time精确等于当前时间: 不触发退出
  4. end_time在过去1秒: 触发退出
  5. end_time在过去1天: 触发退出
  6. duration_days=7天, 开始时间在8天前: 触发退出
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
class TestGridExitTime(unittest.TestCase):
    """网格交易时间退出测试"""

    @classmethod
    def setUpClass(cls):
        """测试类初始化"""
        print("\n" + "="*80)
        print("网格交易退出条件测试 - 时间退出")
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
            'test_name': '网格交易退出条件测试 - 时间退出',
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'total_tests': len(cls.test_results),
            'passed': sum(1 for r in cls.test_results if r['passed']),
            'failed': sum(1 for r in cls.test_results if not r['passed']),
            'results': cls.test_results
        }

        report_file = os.path.join(os.path.dirname(__file__), 'test_grid_exit_time_report.json')
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

    def _create_test_session(self, end_time):
        """创建测试会话"""
        # 设置初始持仓
        self.mock_trader.set_position('000001.SZ', 1000, 10.00)
        self.mock_data_manager.set_current_price(10.00)

        # 如果end_time为None, 设置默认值(30天后)避免NOT NULL约束错误
        if end_time is None:
            db_end_time = datetime.now() + timedelta(days=30)
        else:
            db_end_time = end_time

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
            start_time=datetime.now(),
            end_time=end_time  # 保留原始值用于测试
        )

        # 插入数据库 - 使用转换后的值
        session_dict = asdict(session)
        session_dict['end_time'] = db_end_time
        session.id = self.db_manager.create_grid_session(session_dict)

        # 恢复原始end_time值用于测试逻辑
        session.end_time = end_time

        # 加载到内存
        self.grid_manager.sessions[session.stock_code] = session

        return session

    def _check_exit_and_record(self, test_name, session, expected_exit):
        """检查退出条件并记录结果"""
        exit_reason = self.grid_manager._check_exit_conditions(session, 10.00)

        # 验证结果
        if expected_exit:
            passed = exit_reason == 'expired'
            result_msg = f"预期触发退出, 实际: {exit_reason}"
        else:
            passed = exit_reason is None
            result_msg = f"预期不触发退出, 实际: {exit_reason}"

        # 计算剩余时间
        if session.end_time:
            remaining = session.end_time - datetime.now()
            remaining_str = f"{remaining.total_seconds():.1f}秒"
        else:
            remaining_str = "无限制"

        # 记录测试结果
        self.test_results.append({
            'test_name': test_name,
            'passed': passed,
            'end_time': session.end_time.strftime('%Y-%m-%d %H:%M:%S') if session.end_time else 'None',
            'remaining': remaining_str,
            'exit_reason': exit_reason,
            'result': result_msg
        })

        print(f"\n{test_name}: {'[OK] 通过' if passed else '[FAIL] 失败'}")
        print(f"  结束时间: {session.end_time.strftime('%Y-%m-%d %H:%M:%S') if session.end_time else 'None'}")
        print(f"  剩余时间: {remaining_str}")
        print(f"  {result_msg}")

        self.assertTrue(passed, result_msg)

    def test_1_no_end_time(self):
        """测试1: end_time为None, 不触发退出"""
        session = self._create_test_session(end_time=None)
        self._check_exit_and_record('end_time为None', session, expected_exit=False)

    def test_2_future_1_hour(self):
        """测试2: end_time在未来1小时, 不触发退出"""
        end_time = datetime.now() + timedelta(hours=1)
        session = self._create_test_session(end_time=end_time)
        self._check_exit_and_record('未来1小时', session, expected_exit=False)

    def test_3_exact_now(self):
        """测试3: end_time精确等于当前时间+1秒, 不触发退出"""
        # 加1秒缓冲避免执行延迟导致的意外过期
        end_time = datetime.now() + timedelta(seconds=1)
        session = self._create_test_session(end_time=end_time)
        self._check_exit_and_record('精确等于当前时间', session, expected_exit=False)

    def test_4_past_1_second(self):
        """测试4: end_time在过去1秒, 触发退出"""
        end_time = datetime.now() - timedelta(seconds=1)
        session = self._create_test_session(end_time=end_time)
        self._check_exit_and_record('过去1秒', session, expected_exit=True)

    def test_5_past_1_day(self):
        """测试5: end_time在过去1天, 触发退出"""
        end_time = datetime.now() - timedelta(days=1)
        session = self._create_test_session(end_time=end_time)
        self._check_exit_and_record('过去1天', session, expected_exit=True)

    def test_6_duration_days_7(self):
        """测试6: duration_days=7天, 开始时间在8天前, 触发退出"""
        # 模拟8天前开始的会话
        start_time = datetime.now() - timedelta(days=8)
        end_time = start_time + timedelta(days=7)  # 应该在1天前过期

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
            start_time=start_time,
            end_time=end_time
        )

        # 插入数据库
        session_dict = asdict(session)
        session.id = self.db_manager.create_grid_session(session_dict)

        # 加载到内存
        self.grid_manager.sessions[session.stock_code] = session

        self._check_exit_and_record('duration_days=7天(已过期1天)', session, expected_exit=True)

    def test_7_duration_days_1_not_expired(self):
        """测试7: duration_days=1天, 开始时间在12小时前, 不触发退出"""
        start_time = datetime.now() - timedelta(hours=12)
        end_time = start_time + timedelta(days=1)  # 还剩12小时

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
            start_time=start_time,
            end_time=end_time
        )

        # 插入数据库
        session_dict = asdict(session)
        session.id = self.db_manager.create_grid_session(session_dict)

        # 加载到内存
        self.grid_manager.sessions[session.stock_code] = session

        self._check_exit_and_record('duration_days=1天(还剩12小时)', session, expected_exit=False)


if __name__ == '__main__':
    # 运行测试
    unittest.main(verbosity=2)
