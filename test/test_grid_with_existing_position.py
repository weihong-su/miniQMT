"""
网格交易已有持仓场景测试

测试目标：
1. 关闭模拟交易模式（ENABLE_SIMULATION_MODE = False）
2. 开启全局监控总开关（ENABLE_MONITORING = True）
3. 模拟已有1000股持仓
4. 触发一次网格交易：先卖出再买入
5. 触发一次网格交易：先买入再卖出
6. 100%功能覆盖测试

测试环境：
- Python虚拟环境: python39
- xtquant库: xtquant目录
- 闭市时间测试
"""

import unittest
import sys
import os
import time
from datetime import datetime
from unittest.mock import MagicMock, patch

# 添加项目根目录到路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# 导入配置和模块
import config
from logger import get_logger
from grid_trading_manager import GridTradingManager
from grid_database import DatabaseManager
from position_manager import PositionManager
from trading_executor import TradingExecutor

logger = get_logger(__name__)

# ==================== 测试配置 ====================
TEST_STOCK = "000001.SZ"
INITIAL_PRICE = 10.00
INITIAL_POSITION = 1000  # 初始持仓1000股
PRICE_INTERVAL = 0.05  # 5%
CALLBACK_RATIO = 0.005  # 0.5%
MAX_INVESTMENT = 10000

# 价格模拟序列
# 场景1: 先卖出再买入（价格先上涨后下跌）
# 场景2: 先买入再卖出（价格先下跌后上涨）
PRICE_SEQUENCE_SELL_FIRST = [
    # 阶段1: 初始价格
    {"time": 0, "price": 10.00, "desc": "初始价格，已有1000股持仓"},

    # 阶段2: 上涨穿越上档位 (10.00 * 1.05 = 10.50)
    {"time": 1, "price": 10.20, "desc": "上涨中"},
    {"time": 2, "price": 10.40, "desc": "上涨中"},
    {"time": 3, "price": 10.55, "desc": "穿越上档位 10.50"},
    {"time": 4, "price": 10.60, "desc": "继续上涨（峰值）"},

    # 阶段3: 回落触发卖出信号 (10.60 * 0.995 = 10.547)
    {"time": 5, "price": 10.58, "desc": "开始回落"},
    {"time": 6, "price": 10.55, "desc": "回调0.5%，触发SELL信号"},
    # 卖出后网格重建，新中心价=10.55，下档位=10.55*0.95=10.0225

    # 阶段4: 下跌穿越新下档位 (10.0225)
    {"time": 7, "price": 10.40, "desc": "继续下跌"},
    {"time": 8, "price": 10.20, "desc": "继续下跌"},
    {"time": 9, "price": 10.00, "desc": "穿越下档位 10.0225"},
    {"time": 10, "price": 9.90, "desc": "继续下跌（谷值）"},

    # 阶段5: 回升触发买入信号 (9.90 * 1.005 = 9.9495)
    {"time": 11, "price": 9.92, "desc": "开始回升"},
    {"time": 12, "price": 9.95, "desc": "回调0.5%，触发BUY信号"},

    # 阶段6: 稳定
    {"time": 13, "price": 10.00, "desc": "价格稳定"},
]

PRICE_SEQUENCE_BUY_FIRST = [
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


# ==================== Mock类 ====================
class MockXtQuantTrader:
    """模拟XtQuantTrader"""
    def __init__(self):
        self.connected = True
        self.positions = {}
        self.account_info = {
            'cash': 100000.0,
            'total_asset': 100000.0
        }

    def connect(self):
        self.connected = True
        return True

    def is_connected(self):
        return self.connected

    def query_stock_positions(self, account):
        """返回持仓列表"""
        return list(self.positions.values())

    def query_stock_asset(self, account):
        """返回账户资产"""
        return self.account_info

    def order_stock(self, account, stock_code, order_type, volume, price, strategy_name="", order_remark=""):
        """模拟下单"""
        trade_id = f"ORDER_{int(time.time()*1000)}"
        logger.info(f"[MOCK] 下单: {stock_code}, type={order_type}, volume={volume}, price={price}, id={trade_id}")

        # 更新持仓
        if order_type == 23:  # 买入
            if stock_code in self.positions:
                pos = self.positions[stock_code]
                total_cost = pos['cost_price'] * pos['volume'] + price * volume
                total_volume = pos['volume'] + volume
                pos['cost_price'] = total_cost / total_volume
                pos['volume'] = total_volume
                pos['can_use_volume'] = total_volume
            else:
                self.positions[stock_code] = {
                    'stock_code': stock_code,
                    'volume': volume,
                    'can_use_volume': volume,
                    'cost_price': price,
                    'market_value': price * volume
                }
        elif order_type == 24:  # 卖出
            if stock_code in self.positions:
                pos = self.positions[stock_code]
                pos['volume'] -= volume
                pos['can_use_volume'] -= volume
                if pos['volume'] <= 0:
                    del self.positions[stock_code]

        return {'order_id': trade_id, 'volume': volume, 'price': price}


class MockTradingExecutor:
    """模拟交易执行器"""
    def __init__(self, qmt_trader):
        self.qmt_trader = qmt_trader
        self.trades = []

    def execute_buy(self, stock_code, amount, strategy="grid"):
        """模拟买入"""
        price = 10.0  # 简化处理，使用固定价格
        volume = int(amount / price / 100) * 100

        if volume == 0:
            logger.warning(f"[MOCK] 买入数量不足100股, 跳过")
            return None

        trade_id = self.qmt_trader.order_stock(
            None, stock_code, 23, volume, price, strategy_name=strategy
        )

        self.trades.append({
            'stock_code': stock_code,
            'trade_type': 'BUY',
            'volume': volume,
            'price': price,
            'amount': volume * price,
            'strategy': strategy,
            'trade_id': trade_id,
            'timestamp': datetime.now().isoformat()
        })

        return {'order_id': trade_id, 'volume': volume, 'price': price}

    def execute_sell(self, stock_code, volume, strategy="grid"):
        """模拟卖出"""
        price = 10.0  # 简化处理，使用固定价格

        trade_id = self.qmt_trader.order_stock(
            None, stock_code, 24, volume, price, strategy_name=strategy
        )

        self.trades.append({
            'stock_code': stock_code,
            'trade_type': 'SELL',
            'volume': volume,
            'price': price,
            'amount': volume * price,
            'strategy': strategy,
            'trade_id': trade_id,
            'timestamp': datetime.now().isoformat()
        })

        return {'order_id': trade_id, 'volume': volume, 'price': price}


class MockPositionManager:
    """模拟持仓管理器"""
    def __init__(self, qmt_trader):
        self.qmt_trader = qmt_trader
        self.current_prices = {}

    def update_current_price(self, stock_code, price):
        """更新当前价格"""
        self.current_prices[stock_code] = price

    def get_position(self, stock_code):
        """获取持仓（返回完整的持仓数据结构）"""
        positions = self.qmt_trader.query_stock_positions(None)
        for pos in positions:
            if pos['stock_code'] == stock_code:
                # 确保返回完整的持仓数据结构
                return {
                    'stock_code': pos['stock_code'],
                    'volume': pos['volume'],
                    'can_use_volume': pos.get('can_use_volume', pos['volume']),
                    'cost_price': pos['cost_price'],
                    'market_value': pos.get('market_value', pos['cost_price'] * pos['volume']),
                    'profit_triggered': pos.get('profit_triggered', False),
                    'highest_price': pos.get('highest_price', pos['cost_price'])
                }
        return None

    def _increment_data_version(self):
        """Mock方法：数据版本更新（空实现）"""
        pass


# ==================== 测试类 ====================
class TestGridWithExistingPosition(unittest.TestCase):
    """网格交易已有持仓场景测试"""

    @classmethod
    def setUpClass(cls):
        """测试类初始化"""
        # 临时修改配置以满足测试要求
        config.ENABLE_SIMULATION_MODE = False
        config.ENABLE_MONITORING = True
        config.ENABLE_GRID_TRADING = True
        config.GRID_REQUIRE_PROFIT_TRIGGERED = False  # 关闭止盈触发要求，允许测试

        logger.info("="*80)
        logger.info("网格交易已有持仓场景测试开始")
        logger.info("="*80)
        logger.info(f"测试配置: ENABLE_SIMULATION_MODE={config.ENABLE_SIMULATION_MODE}")
        logger.info(f"测试配置: ENABLE_MONITORING={config.ENABLE_MONITORING}")
        logger.info(f"测试配置: ENABLE_GRID_TRADING={config.ENABLE_GRID_TRADING}")
        logger.info(f"测试配置: GRID_REQUIRE_PROFIT_TRIGGERED={config.GRID_REQUIRE_PROFIT_TRIGGERED}")

        # 初始化测试结果统计
        cls.test_results = {
            "total_tests": 0,
            "passed_tests": 0,
            "failed_tests": 0,
            "coverage": {
                "configuration": False,
                "existing_position_setup": False,
                "sell_first_cycle": False,
                "buy_first_cycle": False,
                "full_coverage": False
            }
        }

    def setUp(self):
        """每个测试前的初始化"""
        # 创建Mock对象
        self.qmt_trader = MockXtQuantTrader()
        self.trading_executor = MockTradingExecutor(self.qmt_trader)
        self.position_manager = MockPositionManager(self.qmt_trader)

        # 初始化数据库和网格管理器
        self.db = DatabaseManager()
        # CRITICAL FIX: 初始化网格交易表,否则会报"no such table: grid_trading_sessions"错误
        self.db.init_grid_tables()
        self.grid_manager = GridTradingManager(
            db_manager=self.db,
            position_manager=self.position_manager,
            trading_executor=self.trading_executor
        )

        self.test_session = None

    def tearDown(self):
        """每个测试后的清理"""
        if self.test_session:
            try:
                self.grid_manager.stop_grid_session(self.test_session.id, reason="test_completed")
            except:
                pass

    @classmethod
    def tearDownClass(cls):
        """测试类清理"""
        logger.info("="*80)
        logger.info("网格交易已有持仓场景测试结束")
        logger.info("="*80)

        # 打印测试报告
        logger.info("\n" + "="*80)
        logger.info("测试报告")
        logger.info("="*80)
        logger.info(f"\n总测试数: {cls.test_results['total_tests']}")
        logger.info(f"通过: {cls.test_results['passed_tests']}")
        logger.info(f"失败: {cls.test_results['failed_tests']}")

        if cls.test_results['total_tests'] > 0:
            logger.info(f"通过率: {cls.test_results['passed_tests']/cls.test_results['total_tests']*100:.1f}%")
        else:
            logger.info("通过率: N/A (没有运行测试)")

        logger.info(f"\n功能覆盖率:")
        for feature, covered in cls.test_results['coverage'].items():
            status = "[OK]" if covered else "[FAIL]"
            logger.info(f"  {status} {feature}")

        covered_count = sum(1 for v in cls.test_results['coverage'].values() if v)
        total_features = len(cls.test_results['coverage'])
        logger.info(f"\n总覆盖率: {covered_count/total_features*100:.1f}%")
        logger.info("\n" + "="*80)

    def test_01_configuration_check(self):
        """测试1: 配置检查"""
        logger.info("\n" + "="*80)
        logger.info("测试1: 配置检查")
        logger.info("="*80)

        self.test_results["total_tests"] += 1

        try:
            # 验证配置
            self.assertFalse(config.ENABLE_SIMULATION_MODE, "应该关闭模拟交易模式")
            self.assertTrue(config.ENABLE_MONITORING, "应该开启全局监控")
            self.assertTrue(config.ENABLE_GRID_TRADING, "应该开启网格交易")

            logger.info("[OK] 配置检查通过")
            self.test_results["passed_tests"] += 1
            self.test_results["coverage"]["configuration"] = True
        except Exception as e:
            logger.error(f"[FAIL] 配置检查失败: {str(e)}")
            self.test_results["failed_tests"] += 1
            raise

    def test_02_setup_existing_position(self):
        """测试2: 设置初始持仓"""
        logger.info("\n" + "="*80)
        logger.info("测试2: 设置初始持仓")
        logger.info("="*80)

        self.test_results["total_tests"] += 1

        try:
            # 设置初始持仓1000股
            self.qmt_trader.positions[TEST_STOCK] = {
                'stock_code': TEST_STOCK,
                'volume': INITIAL_POSITION,
                'can_use_volume': INITIAL_POSITION,
                'cost_price': INITIAL_PRICE,
                'market_value': INITIAL_PRICE * INITIAL_POSITION
            }

            # 验证持仓
            position = self.position_manager.get_position(TEST_STOCK)
            self.assertIsNotNone(position, "应该有持仓")
            self.assertEqual(position['volume'], INITIAL_POSITION, f"持仓应该是{INITIAL_POSITION}股")

            logger.info(f"[OK] 初始持仓设置成功: {INITIAL_POSITION}股 @ {INITIAL_PRICE}元")
            logger.info(f"  持仓市值: {INITIAL_PRICE * INITIAL_POSITION:.2f}元")

            self.test_results["passed_tests"] += 1
            self.test_results["coverage"]["existing_position_setup"] = True
        except Exception as e:
            logger.error(f"[FAIL] 初始持仓设置失败: {str(e)}")
            self.test_results["failed_tests"] += 1
            raise

    def test_03_sell_first_then_buy(self):
        """测试3: 先卖出再买入场景"""
        logger.info("\n" + "="*80)
        logger.info("测试3: 先卖出再买入场景")
        logger.info("="*80)

        self.test_results["total_tests"] += 1

        try:
            # 设置初始持仓
            self.qmt_trader.positions[TEST_STOCK] = {
                'stock_code': TEST_STOCK,
                'volume': INITIAL_POSITION,
                'can_use_volume': INITIAL_POSITION,
                'cost_price': INITIAL_PRICE,
                'market_value': INITIAL_PRICE * INITIAL_POSITION
            }

            # 创建网格会话
            user_config = {
                "center_price": INITIAL_PRICE,
                "price_interval": PRICE_INTERVAL,
                "position_ratio": 0.25,
                "callback_ratio": CALLBACK_RATIO,
                "max_investment": MAX_INVESTMENT,
                "max_deviation": 0.15,
                "target_profit": 0.20,  # 提高目标盈利，避免过早退出
                "stop_loss": -0.50,
                "duration_days": 7
            }

            session = self.grid_manager.start_grid_session(TEST_STOCK, user_config)
            self.test_session = session

            logger.info(f"网格会话已创建: session_id={session.id}")
            logger.info(f"初始持仓: {INITIAL_POSITION}股")

            # 清空交易记录
            self.trading_executor.trades = []

            # 模拟价格序列
            sell_executed = False
            buy_executed = False

            for step in PRICE_SEQUENCE_SELL_FIRST:
                time.sleep(0.05)

                price = step["price"]
                desc = step["desc"]
                self.position_manager.update_current_price(TEST_STOCK, price)
                logger.info(f"\n[时刻 {step['time']}] 价格={price:.2f}, {desc}")

                # 检查网格信号
                signal = self.grid_manager.check_grid_signals(TEST_STOCK, price)

                if signal:
                    signal_type = signal['signal_type']
                    logger.info(f"  → 检测到{signal_type}信号")

                    # 执行交易
                    if signal_type == 'SELL' and not sell_executed:
                        success = self.grid_manager.execute_grid_trade(signal)
                        if success:
                            sell_executed = True
                            logger.info(f"  → 卖出执行成功!")
                    elif signal_type == 'BUY' and sell_executed and not buy_executed:
                        success = self.grid_manager.execute_grid_trade(signal)
                        if success:
                            buy_executed = True
                            logger.info(f"  → 买入执行成功!")
                            break

            # 验证结果
            self.assertTrue(sell_executed, "应该执行卖出")
            self.assertTrue(buy_executed, "应该执行买入")

            logger.info("\n[OK] 先卖出再买入场景测试通过")
            self.test_results["passed_tests"] += 1
            self.test_results["coverage"]["sell_first_cycle"] = True
        except Exception as e:
            logger.error(f"\n[FAIL] 先卖出再买入场景测试失败: {str(e)}")
            self.test_results["failed_tests"] += 1
            raise

    def test_04_buy_first_then_sell(self):
        """测试4: 先买入再卖出场景"""
        logger.info("\n" + "="*80)
        logger.info("测试4: 先买入再卖出场景")
        logger.info("="*80)

        self.test_results["total_tests"] += 1

        try:
            # 设置初始持仓（小量持仓，允许继续买入）
            self.qmt_trader.positions[TEST_STOCK] = {
                'stock_code': TEST_STOCK,
                'volume': 100,  # 小量持仓
                'can_use_volume': 100,
                'cost_price': INITIAL_PRICE,
                'market_value': INITIAL_PRICE * 100
            }

            # 创建网格会话
            user_config = {
                "center_price": INITIAL_PRICE,
                "price_interval": PRICE_INTERVAL,
                "position_ratio": 0.25,
                "callback_ratio": CALLBACK_RATIO,
                "max_investment": MAX_INVESTMENT,
                "max_deviation": 0.15,
                "target_profit": 0.20,
                "stop_loss": -0.50,
                "duration_days": 7
            }

            session = self.grid_manager.start_grid_session(TEST_STOCK, user_config)
            self.test_session = session

            logger.info(f"网格会话已创建: session_id={session.id}")
            logger.info(f"初始持仓: 100股（允许继续买入）")

            # 清空交易记录
            self.trading_executor.trades = []

            # 模拟价格序列
            buy_executed = False
            sell_executed = False

            for step in PRICE_SEQUENCE_BUY_FIRST:
                time.sleep(0.05)

                price = step["price"]
                desc = step["desc"]
                self.position_manager.update_current_price(TEST_STOCK, price)
                logger.info(f"\n[时刻 {step['time']}] 价格={price:.2f}, {desc}")

                # 检查网格信号
                signal = self.grid_manager.check_grid_signals(TEST_STOCK, price)

                if signal:
                    signal_type = signal['signal_type']
                    logger.info(f"  → 检测到{signal_type}信号")

                    # 执行交易
                    if signal_type == 'BUY' and not buy_executed:
                        success = self.grid_manager.execute_grid_trade(signal)
                        if success:
                            buy_executed = True
                            logger.info(f"  → 买入执行成功!")
                    elif signal_type == 'SELL' and buy_executed and not sell_executed:
                        success = self.grid_manager.execute_grid_trade(signal)
                        if success:
                            sell_executed = True
                            logger.info(f"  → 卖出执行成功!")
                            break

            # 验证结果
            self.assertTrue(buy_executed, "应该执行买入")
            self.assertTrue(sell_executed, "应该执行卖出")

            logger.info("\n[OK] 先买入再卖出场景测试通过")
            self.test_results["passed_tests"] += 1
            self.test_results["coverage"]["buy_first_cycle"] = True
        except Exception as e:
            logger.error(f"\n[FAIL] 先买入再卖出场景测试失败: {str(e)}")
            self.test_results["failed_tests"] += 1
            raise

    def test_05_full_coverage(self):
        """测试5: 完整功能覆盖"""
        logger.info("\n" + "="*80)
        logger.info("测试5: 完整功能覆盖")
        logger.info("="*80)

        self.test_results["total_tests"] += 1

        try:
            # 验证所有关键功能都已覆盖
            required_features = [
                "configuration",
                "existing_position_setup",
                "sell_first_cycle",
                "buy_first_cycle"
            ]

            all_covered = all(
                self.test_results["coverage"][feature]
                for feature in required_features
            )

            self.assertTrue(all_covered, "应该覆盖所有关键功能")

            logger.info("[OK] 完整功能覆盖测试通过")
            self.test_results["passed_tests"] += 1
            self.test_results["coverage"]["full_coverage"] = True
        except Exception as e:
            logger.error(f"[FAIL] 完整功能覆盖测试失败: {str(e)}")
            self.test_results["failed_tests"] += 1
            raise


if __name__ == '__main__':
    # 运行测试
    unittest.main(verbosity=2)
