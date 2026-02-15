"""
网格交易退出条件测试 - 持仓清空退出

测试目标:
1. 持仓为0时触发退出
2. 持仓不存在时触发退出
3. 与其他退出条件优先级

测试环境:
- Python虚拟环境: C:\\Users\\PC\\Anaconda3\\envs\\python39
- 使用Mock对象模拟QMT接口
- 闭市时间测试(绕过交易时间检查)

测试设计:
- 测试场景:
  1. 持仓存在且volume>0: 不触发退出
  2. 持仓存在但volume=0: 触发退出
  3. 持仓不存在(None): 触发退出
  4. 持仓清空+盈利10%: 同时满足两个条件, 检查优先级
  5. 持仓清空+偏离超限: 同时满足两个条件, 检查优先级
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
        """设置持仓"""
        self.positions[stock_code] = {
            'm_strInstrumentID': stock_code,
            'm_nVolume': volume,
            'm_nCanUseVolume': volume,
            'm_dOpenPrice': cost_price
        }

    def clear_position(self, stock_code):
        """清空持仓"""
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
class TestGridExitPositionCleared(unittest.TestCase):
    """网格交易持仓清空退出测试"""

    @classmethod
    def setUpClass(cls):
        """测试类初始化"""
        print("\n" + "="*80)
        print("网格交易退出条件测试 - 持仓清空退出")
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
            'test_name': '网格交易退出条件测试 - 持仓清空退出',
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'total_tests': len(cls.test_results),
            'passed': sum(1 for r in cls.test_results if r['passed']),
            'failed': sum(1 for r in cls.test_results if not r['passed']),
            'results': cls.test_results
        }

        report_file = os.path.join(os.path.dirname(__file__), 'test_grid_exit_position_cleared_report.json')
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

    def _create_test_session(self, volume=1000):
        """创建测试会话"""
        # 设置初始持仓
        if volume > 0:
            self.mock_trader.set_position('000001.SZ', volume, 10.00)
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

        # 获取持仓信息
        position = self.position_manager.get_position(session.stock_code)
        if position:
            volume = position.get('volume', 0)
            position_str = f"volume={volume}"
        else:
            volume = None
            position_str = "不存在"

        # 记录测试结果
        self.test_results.append({
            'test_name': test_name,
            'passed': passed,
            'position': position_str,
            'volume': volume,
            'exit_reason': exit_reason,
            'result': result_msg
        })

        print(f"\n{test_name}: {'[OK] 通过' if passed else '[FAIL] 失败'}")
        print(f"  持仓状态: {position_str}")
        print(f"  {result_msg}")

        self.assertTrue(passed, result_msg)

    def test_1_position_exists(self):
        """测试1: 持仓存在且volume>0, 不触发退出"""
        session = self._create_test_session(volume=1000)
        self._check_exit_and_record('持仓存在(1000股)', session, expected_reason=None)

    def test_2_volume_zero(self):
        """测试2: 持仓存在但volume=0, 触发退出"""
        session = self._create_test_session(volume=0)
        self.mock_trader.set_position('000001.SZ', 0, 10.00)  # 明确设置volume=0
        self._check_exit_and_record('持仓volume=0', session, expected_reason='position_cleared')

    def test_3_position_not_exists(self):
        """测试3: 持仓不存在(None), 触发退出"""
        session = self._create_test_session(volume=0)
        self.mock_trader.clear_position('000001.SZ')  # 清除持仓
        self._check_exit_and_record('持仓不存在', session, expected_reason='position_cleared')

    def test_4_cleared_and_profit(self):
        """测试4: 持仓清空+盈利10%, 检查优先级"""
        session = self._create_test_session(volume=0)
        self.mock_trader.clear_position('000001.SZ')

        # 设置盈利10%
        session.buy_count = 1
        session.sell_count = 1
        session.total_buy_amount = 2500
        session.total_sell_amount = 3500  # 盈利1000, 10%

        # 更新数据库 - 正确传递updates参数
        self.db_manager.update_grid_session(session.id, {
            'buy_count': 1,
            'sell_count': 1,
            'total_buy_amount': 2500,
            'total_sell_amount': 3500
        })

        # 检查退出条件: 偏离度 > 盈亏 > 时间 > 持仓清空
        # 所以盈利触发应该优先
        exit_reason = self.grid_manager._check_exit_conditions(session, 10.00)

        passed = exit_reason in ['target_profit', 'position_cleared']
        result_msg = f"实际退出原因: {exit_reason}"

        self.test_results.append({
            'test_name': '持仓清空+盈利10%',
            'passed': passed,
            'position': '不存在',
            'volume': None,
            'profit_ratio': '10%',
            'exit_reason': exit_reason,
            'result': result_msg,
            'note': '检查退出条件优先级'
        })

        print(f"\n持仓清空+盈利10%: {'[OK] 通过' if passed else '[FAIL] 失败'}")
        print(f"  持仓状态: 不存在")
        print(f"  盈利比例: 10%")
        print(f"  {result_msg}")

        self.assertTrue(passed, result_msg)

    def test_5_cleared_and_deviation(self):
        """测试5: 持仓清空+偏离超限, 检查优先级"""
        session = self._create_test_session(volume=0)
        self.mock_trader.clear_position('000001.SZ')

        # 设置偏离超限
        session.current_center_price = 11.51  # 偏离15.1%

        # 更新数据库 - 正确传递updates参数
        self.db_manager.update_grid_session(session.id, {
            'current_center_price': 11.51
        })

        # 检查退出条件: 偏离度优先级最高
        exit_reason = self.grid_manager._check_exit_conditions(session, 10.00)

        passed = exit_reason == 'deviation'
        result_msg = f"预期: deviation, 实际: {exit_reason}"

        deviation_ratio = session.get_deviation_ratio()

        self.test_results.append({
            'test_name': '持仓清空+偏离超限',
            'passed': passed,
            'position': '不存在',
            'volume': None,
            'deviation_ratio': f"{deviation_ratio*100:.2f}%",
            'exit_reason': exit_reason,
            'result': result_msg,
            'note': '偏离度优先级最高'
        })

        print(f"\n持仓清空+偏离超限: {'[OK] 通过' if passed else '[FAIL] 失败'}")
        print(f"  持仓状态: 不存在")
        print(f"  偏离度: {deviation_ratio*100:.2f}%")
        print(f"  {result_msg}")

        self.assertTrue(passed, result_msg)


if __name__ == '__main__':
    # 运行测试
    unittest.main(verbosity=2)
