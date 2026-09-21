"""
交易日志术语回归测试

背景（2026-09-21 实盘日志审查发现）:
    同一笔网格卖单在三条日志里出现了三个互不相同的「委托价」:
        [网格] 已登记待成交委托 ... 委托价=70.86   ← 其实是触发价
        卖出 002859.SZ 下单成功 ... 委托价=70.77   ← 滑点前的报价基准
        卖出请求提交(异步) ... 委托价=70.76        ← 真正报出去的委托价
    后两者的差异源于 easy_qmt_trader.sell() 里 select_slippage() 在打日志前
    就覆盖了 price。排查成交偏差时，三个"委托价"互相矛盾会直接误导。

CLAUDE.md 日志术语表要求：价格必须标语义
    委托价= / 成交价= / 触发价= / 档位价= / 均价= 各有所指，不得混用。
"""

import unittest
import sys
import os
import threading
from datetime import datetime, timedelta
from dataclasses import asdict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import config
from grid_trading_manager import GridTradingManager, GridSession
from grid_database import DatabaseManager


class _StubPositionManager:
    def __init__(self):
        self.signal_lock = threading.RLock()
        self.latest_signals = {}

    def get_position(self, stock_code):
        return {'stock_code': stock_code, 'volume': 1000, 'can_use_volume': 1000,
                'cost_price': 10.0, 'current_price': 10.0, 'market_value': 10000.0}

    def _increment_data_version(self):
        pass


class TestPendingOrderLogTerminology(unittest.TestCase):
    """网格待成交委托登记日志的价格语义"""

    TRIGGER_PRICE = 70.86

    def setUp(self):
        self.db_manager = DatabaseManager(':memory:')
        self.db_manager.init_grid_tables()
        self.grid_manager = GridTradingManager(
            db_manager=self.db_manager,
            position_manager=_StubPositionManager(),
            trading_executor=None
        )
        self.session = GridSession(
            stock_code='002859.SZ',
            center_price=69.12,
            current_center_price=69.12,
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
        self.session.id = self.db_manager.create_grid_session(asdict(self.session))

    def tearDown(self):
        self.db_manager.close()

    def _register(self):
        with self.assertLogs('miniQMT.gtm', level='INFO') as captured:
            self.grid_manager._register_pending_grid_order(
                order_id='1477443585',
                session=self.session,
                signal={'signal_type': 'SELL', 'trigger_price': self.TRIGGER_PRICE},
                side='SELL',
                volume=100,
                expected_price=self.TRIGGER_PRICE,
            )
        return [line for line in captured.output if '已登记待成交委托' in line]

    def test_logs_expected_price_as_trigger_price(self):
        """expected_price 是信号的触发价，不是报给交易所的委托价。"""
        lines = self._register()
        self.assertEqual(len(lines), 1)
        self.assertIn(f'触发价={self.TRIGGER_PRICE:.2f}', lines[0])

    def test_does_not_label_trigger_price_as_order_price(self):
        """回归：这条日志曾把触发价标成「委托价」，与真实报价差 0.10 元。"""
        lines = self._register()
        self.assertNotIn('委托价=', lines[0],
                         '登记日志里没有真实委托价，不得使用该字段名')

    def test_keeps_order_id_terminology(self):
        """委托号是全链路主键，术语表要求固定叫法，一并守住。"""
        lines = self._register()
        self.assertIn('委托号=1477443585', lines[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
