"""委托身份、终态保护和资产估值的隔离回归测试。"""
import os
os.environ.setdefault('MINIQMT_DISABLE_DOTENV', '1')
os.environ.setdefault('ENABLE_SIMULATION_MODE', 'true')
os.environ.setdefault('MINIQMT_LOG_FILE', 'test/logs/priority_optimization.log')

import sqlite3
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from test.test_grid_mece_regression import GridMeceRegressionBase, FakeOrderInfo, FakeTrade
import config


class TestScopedGridOrders(GridMeceRegressionBase):
    def order(self, stock='000001.SZ', day=None, **updates):
        session = self.make_session(stock_code=stock)
        data = dict(order_id='REUSED', session_id=session.id, stock_code=stock,
                    side='BUY', requested_volume=100, expected_price=10,
                    submitted_at=(day or datetime.now().strftime('%Y-%m-%d')) + 'T09:30:00')
        data.update(updates)
        self.db.create_grid_order(data)
        return data

    def test_order_number_reused_across_days_and_stocks_is_isolated(self):
        self.order(day='2026-10-30')
        self.order(day='2026-11-02')
        self.order(stock='600000.SH', day='2026-11-02')
        for day, stock in [('2026-10-30', '000001.SZ'), ('2026-11-02', '000001.SZ'),
                           ('2026-11-02', '600000.SH')]:
            row = self.db.get_grid_order('REUSED', stock_code=stock, trading_date=day)
            self.assertEqual(row['trading_date'], day)
            self.assertEqual(row['stock_code'], stock)
        with self.assertRaises(ValueError):
            self.db.get_grid_order('REUSED')

    def test_repeated_registration_does_not_reset_confirmed_order(self):
        data = self.order(status='filled', filled_volume=100, filled_amount=1000)
        self.db.create_grid_order(dict(data, status='submitted', filled_volume=0, filled_amount=0))
        row = self.db.get_grid_order('REUSED')
        self.assertEqual(row['status'], 'filled')
        self.assertEqual(row['filled_volume'], 100)

    def test_late_terminal_callbacks_do_not_downgrade_filled_order(self):
        self.order(status='filled', filled_volume=100, filled_amount=1000)
        for status in (53, 54, 57):
            with self.subTest(status=status):
                self.manager.handle_order_callback(FakeOrderInfo('REUSED', status=status))
                row = self.db.get_grid_order('REUSED')
                self.assertEqual(row['status'], 'filled')
                self.assertIsNone(row['last_error'])

    def test_real_fill_clears_current_error(self):
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self.make_session(max_investment=1000, position_ratio=1)
        self.executor.buy_stock.return_value = {'order_id': 'FILL_ERROR'}
        self.assertTrue(self.manager.execute_grid_trade(self.buy_signal(session)))
        self.db.update_grid_order('FILL_ERROR', {'last_error': 'timeout cancel failed'})
        self.assertTrue(self.manager.handle_deal_callback(
            FakeTrade('FILL_ERROR', volume=100, price=10, trade_id='FILL_ERROR_DEAL')))
        row = self.db.get_grid_order('FILL_ERROR')
        self.assertEqual(row['status'], 'filled')
        self.assertIsNone(row['last_error'])

    def test_failed_fill_transaction_does_not_publish_filled(self):
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self.make_session(max_investment=1000, position_ratio=1)
        self.executor.buy_stock.return_value = {'order_id': 'DB_FAIL'}
        self.assertTrue(self.manager.execute_grid_trade(self.buy_signal(session)))
        with patch.object(self.db, 'record_grid_trade_and_update_session',
                          side_effect=sqlite3.OperationalError('测试事务失败')):
            self.assertFalse(self.manager.handle_deal_callback(
                FakeTrade('DB_FAIL', volume=100, price=10, trade_id='DB_FAIL_DEAL')))
        self.assertNotEqual(self.db.get_grid_order('DB_FAIL')['status'], 'filled')
        self.assertEqual(self.manager.pending_grid_orders['DB_FAIL']['filled_volume'], 0)
        self.assertTrue(self.manager.handle_deal_callback(
            FakeTrade('DB_FAIL', volume=100, price=10, trade_id='DB_FAIL_DEAL')))
        self.assertEqual(self.db.get_grid_order('DB_FAIL')['status'], 'filled')

    def test_callback_for_another_stock_does_not_change_order(self):
        self.order()
        callback = FakeOrderInfo('REUSED', status=57)
        callback.stock_code = '600000.SH'
        self.manager.handle_order_callback(callback)
        self.assertEqual(self.db.get_grid_order('REUSED')['status'], 'submitted')

    def test_old_callback_does_not_change_current_order(self):
        self.order()
        callback = FakeOrderInfo('REUSED', status=57)
        callback.stock_code = '000001.SZ'
        callback.order_time = (datetime.now() - timedelta(days=1)).timestamp()
        self.manager.handle_order_callback(callback)
        self.assertEqual(self.db.get_grid_order('REUSED')['status'], 'submitted')

    def test_repeated_pending_registration_keeps_partial_fill(self):
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self.make_session(max_investment=2000, position_ratio=1)
        self.executor.buy_stock.return_value = {'order_id': 'REPEAT_PART'}
        signal = self.buy_signal(session)
        self.assertTrue(self.manager.execute_grid_trade(signal))
        self.assertTrue(self.manager.handle_deal_callback(
            FakeTrade('REPEAT_PART', volume=100, price=10, trade_id='PART_ONE')))
        self.manager._register_pending_grid_order('REPEAT_PART', session, signal, 'BUY', 200, 10)
        self.assertEqual(self.manager.pending_grid_orders['REPEAT_PART']['filled_volume'], 100)

    def test_reload_quarantines_previous_day_pending(self):
        yesterday = (datetime.now() - timedelta(days=1)).date().isoformat()
        data = self.order(day=yesterday)
        self.manager.pending_grid_orders['REUSED'] = dict(
            data, created_at=data['submitted_at'], filled_volume=0)
        self.assertEqual(self.manager._load_open_grid_orders(), 0)
        self.assertNotIn('REUSED', self.manager.pending_grid_orders)
        self.assertEqual(self.db.get_grid_order('REUSED')['status'], 'orphaned')

    def test_deal_deduplication_includes_stock_and_day(self):
        session = self.make_session()
        self.record_trade(session, 'BUY', 100, 10, 'OLD_DEAL')
        self.assertTrue(self.db.grid_trade_exists('OLD_DEAL', '000001.SZ',
                                                datetime.now().strftime('%Y-%m-%d')))
        self.assertFalse(self.db.grid_trade_exists('OLD_DEAL', '600000.SH',
                                                 datetime.now().strftime('%Y-%m-%d')))
        self.assertFalse(self.db.grid_trade_exists('OLD_DEAL', '000001.SZ', '2000-01-01'))


class TestAccountValuation(unittest.TestCase):
    def setUp(self):
        from asset_valuation import build_account_valuation
        self.build = build_account_valuation
        self.now = datetime(2026, 11, 2, 14, 9, 44)
        self.raw = dict(total_asset=493746.83, available=431612.83, frozen_cash=0,
                        market_value=69043, timestamp=self.now.isoformat())
        self.positions = [dict(stock_code='603466', volume=4700,
                               position_asof=self.now.isoformat())]
        self.quotes = {'603466': dict(lastPrice=13.22, time=self.now.timestamp() * 1000)}

    def value(self, **kwargs):
        return self.build(self.raw, self.positions, self.quotes, now=self.now,
                          is_trade_time=True, **kwargs)

    def test_reported_mismatch_is_explained_without_changing_raw_values(self):
        original = dict(self.raw)
        result = self.value()
        self.assertEqual(result['status'], 'explained')
        self.assertEqual(result['quote_market_value'], 62134)
        self.assertAlmostEqual(result['broker_identity_diff'], -6909, places=2)
        self.assertAlmostEqual(result['quote_identity_diff'], 0, places=2)
        self.assertEqual(self.raw, original)

    def test_missing_quote_does_not_produce_fake_zero_value(self):
        self.quotes.clear()
        result = self.value()
        self.assertEqual(result['status'], 'unavailable')
        self.assertIsNone(result['quote_market_value'])

    def test_old_quote_is_unavailable_during_trading(self):
        self.quotes['603466']['time'] -= 3600 * 1000
        self.assertEqual(self.value()['status'], 'unavailable')

    def test_quote_without_source_time_is_unavailable(self):
        self.quotes['603466'].pop('time')
        self.assertEqual(self.value()['status'], 'unavailable')

    def test_nonfinite_price_is_rejected(self):
        self.quotes['603466']['lastPrice'] = float('nan')
        self.assertEqual(self.value()['status'], 'unavailable')

    def test_zero_asset_is_invalid(self):
        self.raw.update(total_asset=0, available=0, market_value=0)
        self.assertEqual(self.value()['status'], 'unavailable')

    def test_multiple_positions_and_frozen_cash_are_included(self):
        self.positions.append(dict(stock_code='600000.SH', volume=100,
                                   position_asof=self.now.isoformat()))
        self.quotes['600000'] = dict(lastPrice=10, time=self.now.timestamp() * 1000)
        self.raw.update(frozen_cash=100, total_asset=494846.83)
        self.assertEqual(self.value()['quote_market_value'], 63134)
        self.assertAlmostEqual(self.value()['quote_identity_diff'], 0, places=2)

    def test_stale_position_cannot_explain_current_assets(self):
        self.positions[0]['position_asof'] = (self.now - timedelta(hours=1)).isoformat()
        self.assertEqual(self.value()['status'], 'unavailable')

    def test_unexplained_difference_is_kept(self):
        self.raw['total_asset'] += 100
        self.assertEqual(self.value()['status'], 'unexplained')

    def test_nontrading_quote_is_marked_static(self):
        result = self.build(self.raw, self.positions, self.quotes, now=self.now,
                            is_trade_time=False)
        self.assertEqual(result['status'], 'static')
        self.assertEqual(result['quote_market_value'], 62134)


class TestValuationSnapshot(unittest.TestCase):
    def setUp(self):
        import threading
        from unittest.mock import Mock
        from position_manager import PositionManager
        self.pm = PositionManager.__new__(PositionManager)
        self.pm.memory_conn_lock = threading.RLock()
        self.pm.memory_conn = sqlite3.connect(':memory:')
        self.addCleanup(self.pm.memory_conn.close)
        self.pm.memory_conn.execute('CREATE TABLE positions(stock_code TEXT,volume REAL)')
        self.pm.memory_conn.execute("INSERT INTO positions VALUES('603466',4700)")
        now = datetime.now()
        self.pm._position_restrictions = {
            '603466': {'position_asof': now.isoformat(), 'quantities': (4700, 3300)}}
        self.pm.data_manager = Mock()
        self.quotes = {'603466': {'lastPrice': 13.22, 'time': now.timestamp() * 1000}}
        self.pm.data_manager.get_cached_quotes.return_value = self.quotes
        self.raw = dict(total_asset=493746.83, available=431612.83, frozen_cash=0,
                        market_value=69043, timestamp=now.isoformat())

    def test_valuation_reads_cache_without_querying_market(self):
        with patch.object(config, 'is_trade_time', return_value=True):
            value = self.pm.get_account_valuation(self.raw)
        self.assertEqual(value['status'], 'explained')
        self.assertEqual(value['quote_market_value'], 62134)
        self.pm.data_manager.get_latest_data.assert_not_called()

    def test_quantity_change_during_valuation_discards_result(self):
        def change_position():
            self.pm.memory_conn.execute("UPDATE positions SET volume=4800")
            return self.quotes
        self.pm.data_manager.get_cached_quotes.side_effect = change_position
        self.assertEqual(self.pm.get_account_valuation(self.raw)['status'], 'unavailable')

    def test_metadata_for_different_quantity_cannot_prove_freshness(self):
        self.pm._position_restrictions['603466']['quantities'] = (4600, 3200)
        with patch.object(config, 'is_trade_time', return_value=True):
            self.assertEqual(self.pm.get_account_valuation(self.raw)['status'], 'unavailable')


class TestQuoteSnapshotCache(unittest.TestCase):
    """行情快照缓存必须由 DataManager.__init__ 创建。

    资产估值只读缓存、不再查行情；缓存缺失时 get_cached_quotes() 恒为空，
    asset_valuation 对有持仓账户永远返回 unavailable。此前测试把
    get_cached_quotes 整体 Mock 掉，正好绕过了这条真实构造路径。
    """

    def setUp(self):
        from data_manager import DataManager
        self.dm = DataManager.__new__(DataManager)
        with patch.object(DataManager, '_connect_db',
                          side_effect=lambda: sqlite3.connect(':memory:')), \
                patch.object(DataManager, '_create_tables'), \
                patch.object(DataManager, '_init_xtquant'):
            DataManager.__init__(self.dm)
        self.addCleanup(self.dm.conn.close)

    def test_init_creates_empty_cache(self):
        self.assertEqual(self.dm.get_cached_quotes(), {})

    def test_realtime_quote_is_cached(self):
        self.dm._decorate_quote({'lastPrice': 13.22, 'time': 1793943070000},
                                'xtdata', 'realtime', '603466.SH')
        cached = self.dm.get_cached_quotes()
        self.assertEqual(cached['603466']['lastPrice'], 13.22)

    def test_non_realtime_quote_is_not_cached(self):
        self.dm._decorate_quote({'lastPrice': 13.22}, 'mootdx', 'history', '603466.SH')
        self.assertEqual(self.dm.get_cached_quotes(), {})


class TestGridOrderMigration(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'grid_scope_test.db')
        with sqlite3.connect(self.path) as conn:
            conn.execute("""CREATE TABLE grid_orders(
                order_id TEXT PRIMARY KEY, session_id INTEGER NOT NULL,
                stock_code TEXT NOT NULL, side TEXT NOT NULL, status TEXT,
                requested_volume INTEGER, expected_price REAL, filled_volume INTEGER,
                filled_amount REAL, last_error TEXT, submitted_at TEXT)""")
            conn.execute("INSERT INTO grid_orders VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         ('OLD', 1, '603466.SH', 'BUY', 'filled', 100, 10, 100, 1000,
                          'order terminal status 57', '2026-11-02T09:30:00'))
            for table in ('grid_trades', 'trade_records'):
                conn.execute('CREATE TABLE ' + table + '(trade_id TEXT,stock_code TEXT,'
                             'trade_type TEXT,volume INTEGER,amount REAL,trade_time TEXT)')
                conn.execute('INSERT INTO ' + table + ' VALUES(?,?,?,?,?,?)',
                             ('OLD', '603466.SH', 'BUY', 100, 1000, '2026-11-02T09:30:01'))

    def apply(self, **kwargs):
        from db_migrate import apply_grid_order_scope
        return apply_grid_order_scope(self.path, repair_errors=True, **kwargs)

    def test_legacy_table_is_preserved_and_error_is_archived(self):
        result = self.apply(do_backup=False)
        self.assertEqual(result['copied'], 1)
        self.assertEqual(len(result['repaired']), 1)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT last_error FROM grid_orders').fetchone()[0],
                             'order terminal status 57')
            self.assertIsNone(conn.execute('SELECT last_error FROM grid_orders_scoped').fetchone()[0])
            self.assertIn('order terminal status 57',
                          conn.execute("SELECT detail FROM run_events").fetchone()[0])

    def test_preview_does_not_change_original_database(self):
        from pathlib import Path
        before = Path(self.path).read_bytes()
        result = self.apply(dry_run=True)
        self.assertEqual(len(result['repaired']), 1)
        self.assertEqual(Path(self.path).read_bytes(), before)
        with sqlite3.connect(self.path) as conn:
            self.assertIsNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE name='grid_orders_scoped'").fetchone())

    def test_migration_and_repair_are_idempotent(self):
        self.apply(do_backup=False)
        result = self.apply(do_backup=False)
        self.assertEqual(result['copied'], 0)
        self.assertEqual(result['repaired'], [])
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM run_events').fetchone()[0], 1)

    def test_repair_requires_both_ledgers_to_agree(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute('UPDATE trade_records SET amount=999')
        self.assertEqual(self.apply(do_backup=False)['repaired'], [])
        with sqlite3.connect(self.path) as conn:
            self.assertIsNotNone(conn.execute(
                'SELECT last_error FROM grid_orders_scoped').fetchone()[0])

    def test_invalid_order_date_rolls_back_schema_and_data(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE grid_orders SET submitted_at='unknown'")
        with self.assertRaises(ValueError):
            self.apply(do_backup=False)
        with sqlite3.connect(self.path) as conn:
            self.assertIsNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE name='grid_orders_scoped'").fetchone())
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM grid_orders').fetchone()[0], 1)

    def test_consistent_backup_contains_original_schema_and_data(self):
        result = self.apply()
        with sqlite3.connect(result['backup']) as conn:
            self.assertEqual(conn.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM grid_orders').fetchone()[0], 1)
            self.assertIsNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE name='grid_orders_scoped'").fetchone())

    def test_migration_does_not_commit_outer_transaction(self):
        from db_migrate import migrate_grid_order_scope
        with sqlite3.connect(self.path) as conn:
            conn.execute('BEGIN')
            migrate_grid_order_scope(conn)
            conn.rollback()
            self.assertIsNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE name='grid_orders_scoped'").fetchone())


if __name__ == '__main__':
    unittest.main()
