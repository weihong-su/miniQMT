"""
网格交易实时模拟测试脚本

测试目标：
1. 关闭模拟交易模式（ENABLE_SIMULATION_MODE = False）
2. 开启全局监控总开关（ENABLE_MONITORING = True）
3. 模拟单只股票实时走势
4. 触发至少一次网格买入和网格卖出
5. 100%功能覆盖测试

测试环境：
- Python虚拟环境: python39
- xtquant库: xtquant目录
- 闭市时间测试
"""

import unittest
import sys
import os
import time
import threading
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from logger import get_logger
from grid_database import DatabaseManager
from grid_trading_manager import GridTradingManager, GridSession, PriceTracker

logger = get_logger(__name__)

# ==================== 测试配置 ====================
TEST_STOCK = "000001.SZ"
INITIAL_PRICE = 10.00
PRICE_INTERVAL = 0.05  # 5%
CALLBACK_RATIO = 0.005  # 0.5%
MAX_INVESTMENT = 10000

# 价格模拟序列（设计用于触发买入和卖出）
# 注意：网格会在每次交易后重建，中心价格会更新为成交价
PRICE_SEQUENCE = [
    # 阶段1: 初始价格
    {"time": 0, "price": 10.00, "desc": "初始价格"},

    # 阶段2: 下跌穿越下档位 (10.00 * 0.95 = 9.50)
    {"time": 1, "price": 9.80, "desc": "下跌中"},
    {"time": 2, "price": 9.60, "desc": "下跌中"},
    {"time": 3, "price": 9.45, "desc": "穿越下档位 9.50"},
    {"time": 4, "price": 9.40, "desc": "继续下跌（谷值）"},

    # 阶段3: 回升触发买入信号 (9.40 * 1.005 = 9.447)
    {"time": 5, "price": 9.45, "desc": "开始回升"},
    {"time": 6, "price": 9.48, "desc": "回调0.5%，触发BUY信号"},
    # 买入后网格重建，新中心价=9.48，上档位=9.48*1.05=9.954

    # 阶段4: 上涨穿越新上档位 (9.954)
    {"time": 7, "price": 9.60, "desc": "继续上涨"},
    {"time": 8, "price": 9.80, "desc": "继续上涨"},
    {"time": 9, "price": 9.96, "desc": "穿越上档位 9.954"},
    {"time": 10, "price": 10.10, "desc": "继续上涨（峰值）"},

    # 阶段5: 回落触发卖出信号 (10.10 * 0.995 = 10.0495)
    {"time": 11, "price": 10.08, "desc": "开始回落"},
    {"time": 12, "price": 10.04, "desc": "回调0.5%，触发SELL信号"},

    # 阶段6: 稳定
    {"time": 13, "price": 10.00, "desc": "价格稳定"},
]


class MockXtQuantTrader:
    """模拟 xtquant 交易接口"""
    def __init__(self):
        self.orders = []
        self.positions = {
            TEST_STOCK: {
                "stock_code": TEST_STOCK,
                "volume": 1000,
                "available": 1000,
                "cost_price": 9.50,
                "current_price": INITIAL_PRICE
            }
        }
        self.is_connected_flag = True

    def connect(self):
        """模拟连接"""
        self.is_connected_flag = True
        return True

    def is_connected(self):
        """模拟连接状态"""
        return self.is_connected_flag

    def order_stock(self, account, stock_code, order_type, volume, price, strategy_name="", order_remark=""):
        """模拟下单"""
        order_id = f"ORDER_{int(time.time()*1000)}"
        order = {
            "order_id": order_id,
            "stock_code": stock_code,
            "order_type": order_type,
            "volume": volume,
            "price": price,
            "strategy": strategy_name,
            "timestamp": datetime.now().isoformat()
        }
        self.orders.append(order)
        logger.info(f"[MOCK] 模拟下单: {order}")
        return order_id

    def query_stock_positions(self, account):
        """模拟查询持仓"""
        return list(self.positions.values())

    def query_stock_asset(self, account):
        """模拟查询资产"""
        return {
            "cash": 100000,
            "total_asset": 110000,
            "market_value": 10000
        }


class MockPositionManager:
    """模拟 PositionManager"""
    def __init__(self):
        self.positions = {}
        self.data_version = 0
        self.current_price = INITIAL_PRICE
        self.lock = threading.RLock()

    def get_position(self, stock_code):
        """返回模拟持仓"""
        with self.lock:
            if stock_code not in self.positions:
                self.positions[stock_code] = {
                    "stock_code": stock_code,
                    "volume": 1000,
                    "available": 1000,
                    "cost_price": 9.50,
                    "current_price": self.current_price,
                    "market_value": 1000 * self.current_price,
                    "profit_ratio": (self.current_price - 9.50) / 9.50,
                    "profit_triggered": True,
                    "highest_price": 10.50,
                    "stop_loss_price": 9.00
                }
            else:
                # 更新当前价格
                self.positions[stock_code]["current_price"] = self.current_price
                self.positions[stock_code]["market_value"] = self.positions[stock_code]["volume"] * self.current_price
                self.positions[stock_code]["profit_ratio"] = (self.current_price - self.positions[stock_code]["cost_price"]) / self.positions[stock_code]["cost_price"]

            return self.positions[stock_code]

    def update_current_price(self, stock_code, price):
        """更新当前价格"""
        with self.lock:
            self.current_price = price
            if stock_code in self.positions:
                self.positions[stock_code]["current_price"] = price
                self.positions[stock_code]["market_value"] = self.positions[stock_code]["volume"] * price
                self.positions[stock_code]["profit_ratio"] = (price - self.positions[stock_code]["cost_price"]) / self.positions[stock_code]["cost_price"]

    def _increment_data_version(self):
        """模拟数据版本更新"""
        self.data_version += 1


class MockTradingExecutor:
    """模拟 TradingExecutor"""
    def __init__(self, position_manager):
        self.trades = []
        self.position_manager = position_manager

    def execute_buy(self, stock_code, amount, strategy):
        """模拟买入"""
        current_price = self.position_manager.current_price
        volume = int(amount / current_price / 100) * 100

        trade = {
            "stock_code": stock_code,
            "amount": amount,
            "volume": volume,
            "price": current_price,
            "strategy": strategy,
            "order_id": f"BUY_{int(time.time()*1000)}",
            "timestamp": datetime.now().isoformat()
        }
        self.trades.append(trade)
        logger.info(f"[MOCK] 执行买入: {trade}")

        # 更新持仓
        position = self.position_manager.get_position(stock_code)
        position["volume"] += volume
        position["available"] += volume

        return trade

    def execute_sell(self, stock_code, volume, strategy):
        """模拟卖出"""
        current_price = self.position_manager.current_price
        amount = volume * current_price

        trade = {
            "stock_code": stock_code,
            "volume": volume,
            "amount": amount,
            "price": current_price,
            "strategy": strategy,
            "order_id": f"SELL_{int(time.time()*1000)}",
            "timestamp": datetime.now().isoformat()
        }
        self.trades.append(trade)
        logger.info(f"[MOCK] 执行卖出: {trade}")

        # 更新持仓
        position = self.position_manager.get_position(stock_code)
        position["volume"] -= volume
        position["available"] -= volume

        return trade


class TestGridRealtimeSimulation(unittest.TestCase):
    """网格交易实时模拟测试套件"""

    @classmethod
    def setUpClass(cls):
        """测试类初始化"""
        logger.info("="*80)
        logger.info("网格交易实时模拟测试开始")
        logger.info("="*80)

        # 保存原始配置
        cls.original_simulation_mode = config.ENABLE_SIMULATION_MODE
        cls.original_monitoring = config.ENABLE_MONITORING
        cls.original_grid_trading = config.ENABLE_GRID_TRADING

        # 设置测试配置
        config.ENABLE_SIMULATION_MODE = False  # 关闭模拟模式
        config.ENABLE_MONITORING = True  # 开启监控
        config.ENABLE_GRID_TRADING = True  # 开启网格交易

        logger.info(f"配置设置: ENABLE_SIMULATION_MODE={config.ENABLE_SIMULATION_MODE}")
        logger.info(f"配置设置: ENABLE_MONITORING={config.ENABLE_MONITORING}")
        logger.info(f"配置设置: ENABLE_GRID_TRADING={config.ENABLE_GRID_TRADING}")

        # 创建测试环境
        cls.db_manager = DatabaseManager()
        # 初始化网格交易表
        cls.db_manager.init_grid_tables()
        cls.position_manager = MockPositionManager()
        cls.trading_executor = MockTradingExecutor(cls.position_manager)
        cls.grid_manager = GridTradingManager(
            db_manager=cls.db_manager,
            position_manager=cls.position_manager,
            trading_executor=cls.trading_executor
        )

        # 测试统计
        cls.test_results = {
            "total_tests": 0,
            "passed_tests": 0,
            "failed_tests": 0,
            "coverage": {}
        }

    @classmethod
    def tearDownClass(cls):
        """测试类清理"""
        # 恢复原始配置
        config.ENABLE_SIMULATION_MODE = cls.original_simulation_mode
        config.ENABLE_MONITORING = cls.original_monitoring
        config.ENABLE_GRID_TRADING = cls.original_grid_trading

        logger.info("="*80)
        logger.info("网格交易实时模拟测试结束")
        logger.info("="*80)

        # 生成测试报告
        cls._generate_test_report()

    def setUp(self):
        """每个测试用例前的准备"""
        self.test_session = None
        self.test_results["total_tests"] += 1

    def tearDown(self):
        """每个测试用例后的清理"""
        try:
            if self.test_session:
                self.grid_manager.stop_grid_session(self.test_session.id, "test_completed")
        except:
            pass

    def test_01_configuration_check(self):
        """测试1: 配置检查"""
        logger.info("\n" + "="*80)
        logger.info("测试1: 配置检查")
        logger.info("="*80)

        try:
            # 验证配置
            self.assertFalse(config.ENABLE_SIMULATION_MODE, "模拟模式应该关闭")
            self.assertTrue(config.ENABLE_MONITORING, "监控应该开启")
            self.assertTrue(config.ENABLE_GRID_TRADING, "网格交易应该开启")

            logger.info("[OK] 配置检查通过")
            self.test_results["passed_tests"] += 1
            self.test_results["coverage"]["configuration"] = True
        except Exception as e:
            logger.error(f"[FAIL] 配置检查失败: {str(e)}")
            self.test_results["failed_tests"] += 1
            self.test_results["coverage"]["configuration"] = False
            raise

    def test_02_create_grid_session(self):
        """测试2: 创建网格交易会话"""
        logger.info("\n" + "="*80)
        logger.info("测试2: 创建网格交易会话")
        logger.info("="*80)

        try:
            user_config = {
                "center_price": INITIAL_PRICE,
                "price_interval": PRICE_INTERVAL,
                "position_ratio": 0.25,
                "callback_ratio": CALLBACK_RATIO,
                "max_investment": MAX_INVESTMENT,
                "max_deviation": 0.15,
                "target_profit": 0.10,
                "stop_loss": -0.10,
                "duration_days": 7,
                "risk_level": "moderate"
            }

            session = self.grid_manager.start_grid_session(TEST_STOCK, user_config)
            self.test_session = session

            # 验证session创建
            self.assertIsNotNone(session, "session应该成功创建")
            self.assertIsNotNone(session.id, "session ID应该已分配")
            self.assertEqual(session.stock_code, TEST_STOCK, "股票代码应该正确")
            self.assertEqual(session.center_price, INITIAL_PRICE, "中心价格应该正确")

            logger.info(f"[OK] 网格会话创建成功: session_id={session.id}")
            logger.info(f"  - 股票代码: {session.stock_code}")
            logger.info(f"  - 中心价格: {session.center_price}")
            logger.info(f"  - 价格间隔: {session.price_interval*100}%")
            logger.info(f"  - 回调比例: {session.callback_ratio*100}%")

            self.test_results["passed_tests"] += 1
            self.test_results["coverage"]["session_creation"] = True
        except Exception as e:
            logger.error(f"[FAIL] 创建网格会话失败: {str(e)}")
            self.test_results["failed_tests"] += 1
            self.test_results["coverage"]["session_creation"] = False
            raise

    def test_03_price_simulation_and_grid_buy(self):
        """测试3: 价格模拟与网格买入触发"""
        logger.info("\n" + "="*80)
        logger.info("测试3: 价格模拟与网格买入触发")
        logger.info("="*80)

        try:
            # 创建网格会话
            user_config = {
                "center_price": INITIAL_PRICE,
                "price_interval": PRICE_INTERVAL,
                "position_ratio": 0.25,
                "callback_ratio": CALLBACK_RATIO,
                "max_investment": MAX_INVESTMENT,
                "max_deviation": 0.15,
                "target_profit": 0.10,
                "stop_loss": -0.50,  # 使用更宽松的止损条件，避免买入后立即触发
                "duration_days": 7
            }

            session = self.grid_manager.start_grid_session(TEST_STOCK, user_config)
            self.test_session = session

            logger.info(f"网格会话已创建: session_id={session.id}")
            levels = session.get_grid_levels()
            logger.info(f"初始网格档位: lower={levels['lower']:.2f}, center={levels['center']:.2f}, upper={levels['upper']:.2f}")

            # 模拟价格序列（触发买入）
            buy_signal_detected = False
            buy_signal_executed = False

            for step in PRICE_SEQUENCE[:7]:  # 只执行到触发买入信号
                time.sleep(0.1)  # 模拟时间流逝

                price = step["price"]
                desc = step["desc"]

                # 更新价格
                self.position_manager.update_current_price(TEST_STOCK, price)

                logger.info(f"\n[时刻 {step['time']}] 价格={price:.2f}, {desc}")

                # 检查网格信号
                signal = self.grid_manager.check_grid_signals(TEST_STOCK, price)

                if signal:
                    logger.info(f"  → 检测到信号: {signal['signal_type']}")
                    buy_signal_detected = True

                    # 执行交易
                    if signal['signal_type'] == 'BUY':
                        success = self.grid_manager.execute_grid_trade(signal)
                        if success:
                            buy_signal_executed = True
                            logger.info(f"  → 买入信号执行成功!")

                            # 验证交易记录
                            self.assertEqual(len(self.trading_executor.trades), 1, "应该有1笔买入交易")
                            trade = self.trading_executor.trades[0]
                            self.assertEqual(trade["strategy"], "grid", "策略应该是grid")
                            logger.info(f"  → 交易记录: {trade}")
                            break

            # 验证结果
            self.assertTrue(buy_signal_detected, "应该检测到买入信号")
            self.assertTrue(buy_signal_executed, "买入信号应该执行成功")

            logger.info("\n[OK] 网格买入测试通过")
            self.test_results["passed_tests"] += 1
            self.test_results["coverage"]["grid_buy"] = True
        except Exception as e:
            logger.error(f"\n[FAIL] 网格买入测试失败: {str(e)}")
            self.test_results["failed_tests"] += 1
            self.test_results["coverage"]["grid_buy"] = False
            raise

    def test_04_price_simulation_and_grid_sell(self):
        """测试4: 价格模拟与网格卖出触发"""
        logger.info("\n" + "="*80)
        logger.info("测试4: 价格模拟与网格卖出触发")
        logger.info("="*80)

        try:
            # 创建网格会话
            user_config = {
                "center_price": INITIAL_PRICE,
                "price_interval": PRICE_INTERVAL,
                "position_ratio": 0.25,
                "callback_ratio": CALLBACK_RATIO,
                "max_investment": MAX_INVESTMENT,
                "max_deviation": 0.15,
                "target_profit": 0.10,
                "stop_loss": -0.50,  # 使用更宽松的止损条件，避免买入后立即触发
                "duration_days": 7
            }

            session = self.grid_manager.start_grid_session(TEST_STOCK, user_config)
            self.test_session = session

            logger.info(f"网格会话已创建: session_id={session.id}")

            # 清空之前的交易记录
            self.trading_executor.trades = []

            # 先执行买入，建立网格基础
            logger.info("\n第一阶段: 触发买入信号")
            for step in PRICE_SEQUENCE[:7]:  # 执行到买入完成
                time.sleep(0.05)
                price = step["price"]
                self.position_manager.update_current_price(TEST_STOCK, price)
                logger.info(f"[时刻 {step['time']}] 价格={price:.2f}, {step['desc']}")

                signal = self.grid_manager.check_grid_signals(TEST_STOCK, price)
                if signal and signal['signal_type'] == 'BUY':
                    self.grid_manager.execute_grid_trade(signal)
                    logger.info(f"  → 买入执行成功，网格重建")
                    break

            # 模拟价格序列（触发卖出）
            logger.info("\n第二阶段: 触发卖出信号")
            sell_signal_detected = False
            sell_signal_executed = False

            for step in PRICE_SEQUENCE[7:]:  # 从触发卖出的阶段开始
                time.sleep(0.1)

                price = step["price"]
                desc = step["desc"]

                # 更新价格
                self.position_manager.update_current_price(TEST_STOCK, price)

                logger.info(f"\n[时刻 {step['time']}] 价格={price:.2f}, {desc}")

                # 检查网格信号
                signal = self.grid_manager.check_grid_signals(TEST_STOCK, price)

                if signal:
                    logger.info(f"  → 检测到信号: {signal['signal_type']}")
                    sell_signal_detected = True

                    # 执行交易
                    if signal['signal_type'] == 'SELL':
                        success = self.grid_manager.execute_grid_trade(signal)
                        if success:
                            sell_signal_executed = True
                            logger.info(f"  → 卖出信号执行成功!")

                            # 验证交易记录
                            self.assertGreater(len(self.trading_executor.trades), 1, "应该有买入和卖出交易")
                            trade = self.trading_executor.trades[-1]
                            self.assertEqual(trade["strategy"], "grid", "策略应该是grid")
                            logger.info(f"  → 交易记录: {trade}")
                            break

            # 验证结果
            self.assertTrue(sell_signal_detected, "应该检测到卖出信号")
            self.assertTrue(sell_signal_executed, "卖出信号应该执行成功")

            logger.info("\n[OK] 网格卖出测试通过")
            self.test_results["passed_tests"] += 1
            self.test_results["coverage"]["grid_sell"] = True
        except Exception as e:
            logger.error(f"\n[FAIL] 网格卖出测试失败: {str(e)}")
            self.test_results["failed_tests"] += 1
            self.test_results["coverage"]["grid_sell"] = False
            raise

    def test_05_full_cycle_simulation(self):
        """测试5: 完整周期模拟（买入+卖出）"""
        logger.info("\n" + "="*80)
        logger.info("测试5: 完整周期模拟（买入+卖出）")
        logger.info("="*80)

        try:
            # 创建网格会话
            user_config = {
                "center_price": INITIAL_PRICE,
                "price_interval": PRICE_INTERVAL,
                "position_ratio": 0.25,
                "callback_ratio": CALLBACK_RATIO,
                "max_investment": MAX_INVESTMENT,
                "max_deviation": 0.15,
                "target_profit": 0.10,
                "stop_loss": -0.50,  # 使用更宽松的止损条件，避免买入后立即触发
                "duration_days": 7
            }

            session = self.grid_manager.start_grid_session(TEST_STOCK, user_config)
            self.test_session = session

            logger.info(f"网格会话已创建: session_id={session.id}")

            # 清空交易记录
            self.trading_executor.trades = []

            # 执行完整价格序列
            buy_count = 0
            sell_count = 0

            for step in PRICE_SEQUENCE:
                time.sleep(0.1)

                price = step["price"]
                desc = step["desc"]

                # 更新价格
                self.position_manager.update_current_price(TEST_STOCK, price)

                logger.info(f"\n[时刻 {step['time']}] 价格={price:.2f}, {desc}")

                # 检查网格信号
                signal = self.grid_manager.check_grid_signals(TEST_STOCK, price)

                if signal:
                    signal_type = signal['signal_type']
                    logger.info(f"  → 检测到信号: {signal_type}")

                    # 执行交易
                    success = self.grid_manager.execute_grid_trade(signal)
                    if success:
                        if signal_type == 'BUY':
                            buy_count += 1
                            logger.info(f"  → 买入执行成功! (第{buy_count}次)")
                        elif signal_type == 'SELL':
                            sell_count += 1
                            logger.info(f"  → 卖出执行成功! (第{sell_count}次)")

            # 验证结果
            self.assertGreater(buy_count, 0, "应该至少有1次买入")
            self.assertGreater(sell_count, 0, "应该至少有1次卖出")

            # 获取会话统计（在会话停止前获取）
            stats = self.grid_manager.get_session_stats(session.id)
            if stats:
                logger.info(f"\n会话统计:")
                logger.info(f"  - 总交易次数: {stats.get('trade_count', 0)}")
                logger.info(f"  - 买入次数: {stats.get('buy_count', 0)}")
                logger.info(f"  - 卖出次数: {stats.get('sell_count', 0)}")
                logger.info(f"  - 盈亏比例: {stats.get('profit_ratio', 0)*100:.2f}%")
            else:
                # 会话已停止，使用本地计数
                logger.info(f"\n会话统计:")
                logger.info(f"  - 买入次数: {buy_count}")
                logger.info(f"  - 卖出次数: {sell_count}")

            logger.info("\n[OK] 完整周期模拟测试通过")
            self.test_results["passed_tests"] += 1
            self.test_results["coverage"]["full_cycle"] = True
        except Exception as e:
            logger.error(f"\n[FAIL] 完整周期模拟测试失败: {str(e)}")
            self.test_results["failed_tests"] += 1
            self.test_results["coverage"]["full_cycle"] = False
            raise

    @classmethod
    def _generate_test_report(cls):
        """生成测试报告"""
        logger.info("\n" + "="*80)
        logger.info("测试报告")
        logger.info("="*80)

        logger.info(f"\n总测试数: {cls.test_results['total_tests']}")
        logger.info(f"通过: {cls.test_results['passed_tests']}")
        logger.info(f"失败: {cls.test_results['failed_tests']}")

        if cls.test_results['total_tests'] > 0:
            pass_rate = (cls.test_results['passed_tests'] / cls.test_results['total_tests']) * 100
            logger.info(f"通过率: {pass_rate:.1f}%")

        logger.info(f"\n功能覆盖率:")
        coverage = cls.test_results['coverage']
        total_features = len(coverage)
        covered_features = sum(1 for v in coverage.values() if v)

        for feature, covered in coverage.items():
            status = "[OK]" if covered else "[FAIL]"
            logger.info(f"  {status} {feature}")

        if total_features > 0:
            coverage_rate = (covered_features / total_features) * 100
            logger.info(f"\n总覆盖率: {coverage_rate:.1f}%")

        logger.info("\n" + "="*80)


if __name__ == "__main__":
    # 运行测试
    unittest.main(verbosity=2)
