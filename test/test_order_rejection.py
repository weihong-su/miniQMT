"""
QMT 拒单 (order_id=-1) 修复验证测试

测试范围:
- 买入下单 QMT 返回 order_id=-1 时，不保存交易记录，返回 None
- 卖出下单 QMT 返回 order_id=-1 时，不保存交易记录，返回 None
- order_id 正常 (>0) 时，交易记录正常保存（回归验证）
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import unittest
from unittest.mock import Mock, patch, MagicMock, call
import sqlite3

import config


class TestOrderRejection(unittest.TestCase):
    """order_id=-1 拒单场景验证"""

    def setUp(self):
        self.orig_sim = config.ENABLE_SIMULATION_MODE
        self.orig_confirm = getattr(config, 'GRID_CONFIRM_LIVE_ORDER_BY_DEAL', True)
        config.ENABLE_SIMULATION_MODE = False  # 实盘模式才走 order_id 路径
        # 本模块使用虚构价格，需把当日涨跌停放开，避免限价夹取改变断言值
        self._limit_patch = patch(
            'xtquant.xtdata.get_instrument_detail',
            return_value={'UpStopPrice': 1e6, 'DownStopPrice': 0.01}
        )
        self._limit_patch.start()
        self.addCleanup(self._limit_patch.stop)

    def tearDown(self):
        config.ENABLE_SIMULATION_MODE = self.orig_sim
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = self.orig_confirm

    def _make_executor(self):
        """创建已完成初始化的 TradingExecutor，并 mock 全部外部依赖"""
        from trading_executor import TradingExecutor

        with patch('trading_executor.get_data_manager') as mock_dm, \
             patch('trading_executor.get_position_manager') as mock_pm:

            mock_dm.return_value = Mock()
            mock_dm.return_value.conn = sqlite3.connect(':memory:')

            pm = Mock()
            pm.qmt_trader = Mock()
            pm.qmt_trader.position.return_value = []
            pm.qmt_trader.query_stock_orders.return_value = []
            pm.qmt_trader.select_slippage.side_effect = lambda **kw: kw['price']
            pm.qmt_trader.can_buy.return_value = True
            pm.qmt_trader.can_sell.return_value = True
            pm.qmt_trader.check_buy.return_value = True
            mock_pm.return_value = pm

            executor = TradingExecutor.__new__(TradingExecutor)
            executor.data_manager = mock_dm.return_value
            executor.position_manager = mock_pm.return_value
            executor.conn = mock_dm.return_value.conn
            executor.trader = None
            executor.callbacks = {}
            executor.order_cache = {}
            import threading
            executor.trade_lock = threading.Lock()
            executor.sim_order_counter = 0
            executor.simulation_balance = 0
            executor.debug_mode = True

        return executor

    # ------------------------------------------------------------------
    # T-R1: 买入 order_id=-1 → 不保存记录，返回 None
    # ------------------------------------------------------------------
    def test_buy_order_rejected_returns_none(self):
        """T-R1a: buy_stock 当 QMT 返回 order_id=-1 时应返回 None"""
        print("\n=== T-R1a: 买入拒单 (order_id=-1) 返回 None ===")

        executor = self._make_executor()

        # mock qmt_trader.buy 返回 seq=215722
        executor.position_manager.qmt_trader.buy = Mock(return_value=215722)
        # mock _get_real_order_id 返回 -1（QMT 回调中 response.order_id=-1）
        executor.position_manager._get_real_order_id = Mock(return_value=-1)
        # spy _save_trade_record
        executor._save_trade_record = Mock(return_value=True)

        result = executor.buy_stock(
            stock_code='301399.SZ',
            price=24.41,
            volume=2000,
            strategy='default'
        )

        self.assertIsNone(result, "order_id=-1 时 buy_stock 应返回 None")
        executor._save_trade_record.assert_not_called()
        print(f"[OK] buy_stock 返回 None，交易记录未保存")

    def test_buy_order_rejected_no_trade_record(self):
        """T-R1b: buy_stock 当 QMT 返回 order_id=-1 时不应写入 trade_records 表"""
        print("\n=== T-R1b: 买入拒单不写数据库 ===")

        executor = self._make_executor()

        # 准备真实内存 DB 表，验证无记录写入
        cursor = executor.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trade_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stock_code TEXT, stock_name TEXT, trade_time TEXT,
                trade_type TEXT, price REAL, volume INTEGER, amount REAL,
                trade_id TEXT, commission REAL, strategy TEXT
            )
        """)
        executor.conn.commit()

        executor.position_manager.qmt_trader.buy = Mock(return_value=215722)
        executor.position_manager._get_real_order_id = Mock(return_value=-1)

        executor.buy_stock(stock_code='301399.SZ', price=24.41, volume=2000)

        cursor.execute("SELECT COUNT(*) FROM trade_records")
        count = cursor.fetchone()[0]
        self.assertEqual(count, 0, "order_id=-1 时不应写入 trade_records")
        print(f"[OK] trade_records 表行数={count}，确认无误写入")

    # ------------------------------------------------------------------
    # T-R2: 卖出 order_id=-1 → 不保存记录，返回 None
    # ------------------------------------------------------------------
    def test_sell_order_rejected_returns_none(self):
        """T-R2a: sell_stock 当 QMT 返回 order_id=-1 时应返回 None"""
        print("\n=== T-R2a: 卖出拒单 (order_id=-1) 返回 None ===")

        executor = self._make_executor()

        # mock 持仓检查通过
        executor.position_manager.get_position = Mock(return_value={
            'stock_code': '300057.SZ',
            'volume': 3000,
            'available': 3000,
            'cost_price': 6.18
        })
        executor.position_manager.qmt_trader.can_sell = Mock(return_value=True)
        executor.position_manager.qmt_trader.sell = Mock(return_value=215739)
        executor.position_manager._get_real_order_id = Mock(return_value=-1)
        executor._save_trade_record = Mock(return_value=True)

        result = executor.sell_stock(
            stock_code='300057.SZ',
            volume=400,
            price=7.42,
            strategy='grid'
        )

        self.assertIsNone(result, "order_id=-1 时 sell_stock 应返回 None")
        executor._save_trade_record.assert_not_called()
        print(f"[OK] sell_stock 返回 None，交易记录未保存")

    # ------------------------------------------------------------------
    # T-R3: 正常 order_id>0 不受影响（回归验证）
    # ------------------------------------------------------------------
    def test_buy_normal_order_id_saves_record(self):
        """T-R3: order_id>0 时交易记录正常保存（回归验证）"""
        print("\n=== T-R3: 正常 order_id>0 回归验证 ===")

        executor = self._make_executor()

        # 准备 trade_records 表
        cursor = executor.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trade_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stock_code TEXT, stock_name TEXT, trade_time TEXT,
                trade_type TEXT, price REAL, volume INTEGER, amount REAL,
                trade_id TEXT, commission REAL, strategy TEXT
            )
        """)
        executor.conn.commit()

        executor.position_manager.qmt_trader.buy = Mock(return_value=215722)
        executor.position_manager._get_real_order_id = Mock(return_value=2014314497)  # 正常订单号
        # get_stock_name 需要返回字符串，否则 SQLite 绑定失败
        executor.data_manager.get_stock_name = Mock(return_value='英特科技')

        # 强制 is_trade_time() 返回 True，使测试不依赖实际交易时间（CI/非交易时段也能通过）
        with patch('config.is_trade_time', return_value=True):
            result = executor.buy_stock(stock_code='301399.SZ', price=24.41, volume=2000)

        self.assertEqual(result, 2014314497, "正常 order_id 应原样返回")
        cursor.execute("SELECT COUNT(*) FROM trade_records")
        count = cursor.fetchone()[0]
        self.assertEqual(count, 1, "正常 order_id 应写入 1 条交易记录")
        print(f"[OK] order_id={result}, trade_records 行数={count}")

    def test_grid_buy_confirm_mode_defers_trade_record(self):
        """实盘网格买入确认模式下，下单成功不立即写 trade_records。"""
        executor = self._make_executor()
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True

        executor.position_manager.qmt_trader.buy = Mock(return_value=215722)
        executor.position_manager._get_real_order_id = Mock(return_value=2014314497)
        executor._save_trade_record = Mock(return_value=True)

        with patch('config.is_trade_time', return_value=True):
            result = executor.buy_stock(
                stock_code='301399.SZ',
                price=24.41,
                volume=2000,
                strategy=config.GRID_STRATEGY_NAME
            )

        self.assertEqual(result, 2014314497)
        executor._save_trade_record.assert_not_called()
        self.assertEqual(
            executor.order_cache[str(result)]['strategy'],
            config.GRID_STRATEGY_NAME
        )

    def test_grid_sell_confirm_mode_defers_trade_record(self):
        """实盘网格卖出确认模式下，下单成功不立即写 trade_records。"""
        executor = self._make_executor()
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True

        executor.position_manager.get_position = Mock(return_value={
            'stock_code': '300057.SZ',
            'volume': 3000,
            'available': 3000,
            'cost_price': 6.18
        })
        executor.position_manager.qmt_trader.check_stock_is_av_sell = Mock(return_value=True)
        executor.position_manager.qmt_trader.sell = Mock(return_value=215739)
        executor.position_manager._get_real_order_id = Mock(return_value=672137217)
        executor._save_trade_record = Mock(return_value=True)

        with patch('config.is_trade_time', return_value=True):
            result = executor.sell_stock(
                stock_code='300057.SZ',
                volume=400,
                price=7.42,
                strategy=config.GRID_STRATEGY_NAME
            )

        self.assertEqual(result, 672137217)
        executor._save_trade_record.assert_not_called()
        self.assertEqual(
            executor.order_cache[str(result)]['strategy'],
            config.GRID_STRATEGY_NAME
        )

    def test_sell_zero_price_falls_back_to_latest_price(self):
        """传入卖出价为0时，应降级使用有效最新价继续下单。"""
        executor = self._make_executor()

        executor.position_manager.get_position = Mock(return_value={
            'stock_code': '300057.SZ',
            'volume': 3000,
            'available': 3000,
            'cost_price': 6.18
        })
        executor.position_manager.qmt_trader.check_stock_is_av_sell = Mock(return_value=True)
        executor.position_manager.qmt_trader.sell = Mock(return_value=215739)
        executor.position_manager._get_real_order_id = Mock(return_value=672137218)
        executor._save_trade_record = Mock(return_value=True)
        executor.data_manager.get_latest_data = Mock(return_value={
            'lastPrice': 7.41,
            'close': 7.40,
        })

        with patch('config.is_trade_time', return_value=True), \
             patch('xtquant.xtdata.get_full_tick', return_value={
                 '300057.SZ': {'bidPrice': [0, 0, 0]}
             }):
            result = executor.sell_stock(
                stock_code='300057.SZ',
                volume=400,
                price=0,
                strategy='auto_partial'
            )

        self.assertEqual(result, 672137218)
        sell_kwargs = executor.position_manager.qmt_trader.sell.call_args.kwargs
        self.assertEqual(sell_kwargs['price'], 7.41)


def run_tests():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestOrderRejection)
    runner = unittest.TextTestRunner(verbosity=2)
    return runner.run(suite)


if __name__ == '__main__':
    run_tests()
