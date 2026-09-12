# -*- coding: utf-8 -*-
"""
网格成交 time_source 透传测试。

重点：网格的 trade_time 来自 datetime.now()，不是交易所成交时间。
   - 实盘成交回报路径 → time_source='exchange'（若能取到 traded_time）
   - 对账补记路径     → time_source='reconcile_backfill'
两者都不许拿 now() 冒充成交时间。
"""
import unittest

import settlement_db as sdb
from grid_trading_manager import GridTradingManager


class _Stub:
    """只提供 _build_deal_meta_from_trade 依赖的取值器。"""

    def _get_attr_or_key(self, record, names, default=None):
        for name in names:
            if isinstance(record, dict) and name in record:
                return record[name]
            if hasattr(record, name):
                return getattr(record, name)
        return default


class _FakeTrade:
    def __init__(self, traded_time):
        self.traded_time = traded_time


class TestGridDealMeta(unittest.TestCase):
    def _meta(self, trade, order_id, time_source=None):
        return GridTradingManager._build_deal_meta_from_trade(
            _Stub(), trade, order_id, time_source=time_source)

    def test_real_deal_time_marked_exchange(self):
        meta = self._meta(_FakeTrade(1789353015), '1209008129')
        self.assertEqual(meta['time_source'], 'exchange')
        self.assertEqual(meta['deal_time'], 1789353015)
        self.assertEqual(meta['deal_time_str'], '2026-09-14 10:30:15')
        self.assertEqual(meta['order_id'], '1209008129')

    def test_missing_deal_time_marked_local_fallback(self):
        """取不到成交时间就如实标注，绝不用 now() 冒充。"""
        meta = self._meta(_FakeTrade(None), '1209008129')
        self.assertEqual(meta['time_source'], 'local_fallback')
        self.assertIsNone(meta['deal_time'])
        self.assertIsNone(meta['deal_time_str'])

    def test_unparseable_deal_time_marked_local_fallback(self):
        meta = self._meta(_FakeTrade('garbage'), '1209008129')
        self.assertEqual(meta['time_source'], 'local_fallback')
        self.assertIsNone(meta['deal_time'])

    def test_reconcile_path_override_wins(self):
        """对账补记的 synthetic_trade 无 traded_time，必须显式标 reconcile_backfill。"""
        synthetic = {'order_id': '1209008129', 'traded_volume': 500,
                     'traded_price': 122.2, 'trade_id': '1209008129_ORDER_FILLED_RECON'}
        meta = self._meta(synthetic, '1209008129',
                          time_source=sdb.TIME_SOURCE_RECONCILE)
        self.assertEqual(meta['time_source'], 'reconcile_backfill')
        self.assertIsNone(meta['deal_time'],
                          "补记时刻不是成交时刻，必须留空")

    def test_dict_trade_supported(self):
        meta = self._meta({'traded_time': 1789353015, 'order_id': '9'}, '9')
        self.assertEqual(meta['time_source'], 'exchange')

    def test_empty_trade_does_not_raise(self):
        meta = self._meta(None, '9')
        self.assertEqual(meta['time_source'], 'local_fallback')

    def test_time_source_constants_match_db_check(self):
        """store 到库里的值必须与索引/查询使用的常量一致。"""
        self.assertEqual(sdb.TIME_SOURCE_EXCHANGE, 'exchange')
        self.assertEqual(sdb.TIME_SOURCE_LOCAL, 'local_fallback')
        self.assertEqual(sdb.TIME_SOURCE_RECONCILE, 'reconcile_backfill')
        self.assertEqual(sdb.TIME_SOURCE_BROKER, 'broker')


if __name__ == '__main__':
    unittest.main(verbosity=2)
