"""
网格交易退出条件测试 - 偏离度退出

测试目标:
1. 正常偏离度计算验证
2. 超过max_deviation触发退出
3. 动态中心价偏离检测
4. center_price vs current_center_price计算逻辑

测试环境:
- Python虚拟环境: C:\\Users\\PC\\Anaconda3\\envs\\python39
- 使用Mock对象模拟QMT接口
- 闭市时间测试(绕过交易时间检查)

测试设计:
- 初始中心价: 10.00元
- max_deviation: 15% (0.15)
- 测试场景:
  1. 正常偏离: current_center=10.10元, 偏离度=1%, 不触发
  2. 临界偏离: current_center=11.49元, 偏离度=14.9%, 不触发
  3. 超限偏离: current_center=11.51元, 偏离度=15.1%, 触发退出
  4. 反向偏离: current_center=8.49元, 偏离度=15.1%, 触发退出
  5. center_price为0: 不触发退出
"""

import unittest
import sys
import os
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
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
        """查询持仓"""
        return list(self.positions.values())

    def query_stock_asset(self, account):
        """查询资产"""
        return self.account_info

    def set_position(self, stock_code, volume, cost_price):
        """设置持仓"""
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
        """返回模拟价格数据"""
        return {stock_code: {'close': self.current_price}}

    def set_current_price(self, price):
        """设置当前价格"""
        self.current_price = price


class MockPositionManager:
    """模拟持仓管理器"""
    def __init__(self, qmt_trader):
        self.qmt_trader = qmt_trader
        self.current_prices = {}

    def update_current_price(self, stock_code, price):
        """更新当前价格"""
        self.current_prices[stock_code] = price

    def get_position(self, stock_code):
        """获取持仓"""
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
        """Mock方法: 数据版本更新(空实现)"""
        pass


# ==================== 测试类 ====================
class TestGridExitDeviation(unittest.TestCase):
    """网格交易偏离度退出测试"""

    @classmethod
    def setUpClass(cls):
        """测试类初始化"""
        print("\n" + "="*80)
        print("网格交易退出条件测试 - 偏离度退出")
        print("="*80)

        # 保存原始配置
        cls.original_simulation = config.ENABLE_SIMULATION_MODE
        cls.original_grid_enabled = config.ENABLE_GRID_TRADING
        cls.original_debug_simu = config.DEBUG_SIMU_STOCK_DATA

        # 设置测试配置
        config.ENABLE_SIMULATION_MODE = False  # 关闭模拟模式
        config.ENABLE_GRID_TRADING = True
        config.DEBUG_SIMU_STOCK_DATA = True  # 绕过交易时间检查

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
            'test_name': '网格交易退出条件测试 - 偏离度退出',
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'total_tests': len(cls.test_results),
            'passed': sum(1 for r in cls.test_results if r['passed']),
            'failed': sum(1 for r in cls.test_results if not r['passed']),
            'results': cls.test_results
        }

        report_file = os.path.join(os.path.dirname(__file__), 'test_grid_exit_deviation_report.json')
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
            trading_executor=None  # 不需要真实的交易执行器
        )

    def tearDown(self):
        """每个测试后清理"""
        if hasattr(self, 'db_manager'):
            self.db_manager.close()

    def _create_test_session(self, center_price, current_center_price, max_deviation):
        """创建测试会话"""
        # 设置初始持仓
        self.mock_trader.set_position('000001.SZ', 1000, center_price)
        self.mock_data_manager.set_current_price(center_price)

        # 创建网格会话
        session = GridSession(
            stock_code='000001.SZ',
            center_price=center_price,
            current_center_price=current_center_price,
            price_interval=0.05,
            position_ratio=0.25,
            callback_ratio=0.005,
            max_investment=10000,
            max_deviation=max_deviation,
            target_profit=0.10,
            stop_loss=-0.10,
            start_time=datetime.now(),
            end_time=datetime.now() + timedelta(days=7)
        )

        # 插入数据库
        session_dict = asdict(session)
        session.id = self.db_manager.create_grid_session(session_dict)

        # 加载到内存
        self.grid_manager.sessions[session.stock_code] = session

        return session

    def _check_exit_and_record(self, test_name, session, expected_exit):
        """检查退出条件并记录结果"""
        current_price = session.current_center_price
        exit_reason = self.grid_manager._check_exit_conditions(session, current_price)

        # 验证结果
        if expected_exit:
            passed = exit_reason == 'deviation'
            result_msg = f"预期触发退出, 实际: {exit_reason}"
        else:
            passed = exit_reason is None
            result_msg = f"预期不触发退出, 实际: {exit_reason}"

        # 记录测试结果
        deviation_ratio = session.get_deviation_ratio()
        self.test_results.append({
            'test_name': test_name,
            'passed': passed,
            'center_price': session.center_price,
            'current_center_price': session.current_center_price,
            'deviation_ratio': f"{deviation_ratio*100:.2f}%",
            'max_deviation': f"{session.max_deviation*100:.2f}%",
            'exit_reason': exit_reason,
            'result': result_msg
        })

        print(f"\n{test_name}: {'[OK] 通过' if passed else '[FAIL] 失败'}")
        print(f"  中心价: {session.center_price:.2f}, 当前中心价: {session.current_center_price:.2f}")
        print(f"  偏离度: {deviation_ratio*100:.2f}%, 最大偏离: {session.max_deviation*100:.2f}%")
        print(f"  {result_msg}")

        self.assertTrue(passed, result_msg)

    def test_1_normal_deviation(self):
        """测试1: 正常偏离度(1%), 不触发退出"""
        session = self._create_test_session(
            center_price=10.00,
            current_center_price=10.10,
            max_deviation=0.15
        )
        self._check_exit_and_record('正常偏离(1%)', session, expected_exit=False)

    def test_2_critical_deviation(self):
        """测试2: 临界偏离度(14.9%), 不触发退出"""
        session = self._create_test_session(
            center_price=10.00,
            current_center_price=11.49,
            max_deviation=0.15
        )
        self._check_exit_and_record('临界偏离(14.9%)', session, expected_exit=False)

    def test_3_exceed_deviation(self):
        """测试3: 超限偏离度(15.1%), 触发退出"""
        session = self._create_test_session(
            center_price=10.00,
            current_center_price=11.51,
            max_deviation=0.15
        )
        self._check_exit_and_record('超限偏离(15.1%)', session, expected_exit=True)

    def test_4_reverse_deviation(self):
        """测试4: 反向偏离度(-15.1%), 触发退出"""
        session = self._create_test_session(
            center_price=10.00,
            current_center_price=8.49,
            max_deviation=0.15
        )
        self._check_exit_and_record('反向偏离(-15.1%)', session, expected_exit=True)

    def test_5_zero_center_price(self):
        """测试5: center_price为0, 不触发退出"""
        session = self._create_test_session(
            center_price=0.0,
            current_center_price=10.00,
            max_deviation=0.15
        )
        self._check_exit_and_record('center_price为0', session, expected_exit=False)

    def test_6_zero_current_center(self):
        """测试6: current_center_price为0, 不触发退出"""
        session = self._create_test_session(
            center_price=10.00,
            current_center_price=0.0,
            max_deviation=0.15
        )
        self._check_exit_and_record('current_center_price为0', session, expected_exit=False)

    def test_7_exact_max_deviation(self):
        """测试7: 精确等于max_deviation(15%), 不触发退出"""
        session = self._create_test_session(
            center_price=10.00,
            current_center_price=11.50,
            max_deviation=0.15
        )
        self._check_exit_and_record('精确等于max_deviation(15%)', session, expected_exit=False)

    def test_8_tiny_exceed(self):
        """测试8: 微小超限(15.01%), 触发退出"""
        session = self._create_test_session(
            center_price=10.00,
            current_center_price=11.501,
            max_deviation=0.15
        )
        self._check_exit_and_record('微小超限(15.01%)', session, expected_exit=True)


if __name__ == '__main__':
    # 运行测试
    unittest.main(verbosity=2)
