"""运行日志优化回归：冻结网格、超时精度、快照质量与后台任务互斥。"""
import csv
import json
from contextlib import closing
from datetime import datetime, timedelta
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault('MINIQMT_DISABLE_DOTENV', '1')
os.environ.setdefault('MINIQMT_LOG_FILE', 'test/logs/runtime_optimization.log')

import pandas as pd
import config
import db_migrate
import logger as log_module
import settlement_db
from grid_trading_manager import GridSession, GridTradingManager, PriceTracker
from position_manager import PositionManager
from scripts import export_settlement


class TestGridRestrictedSignals(unittest.TestCase):
    def setUp(self):
        self.pm = MagicMock()
        self.position = {'stock_code': '000001', 'volume': 1000, 'available': 0}
        self.pm.get_position.side_effect = lambda code: dict(self.position)
        self.pm.get_position_restriction.return_value = {
            'restriction_reason': 't1', 'restriction_detail': '当日买入，T+1 暂不可卖'}
        self.pm.signal_lock = threading.Lock()
        self.pm.latest_signals = {}
        with patch.object(GridTradingManager, '_load_active_sessions', return_value=0), \
                patch.object(GridTradingManager, '_load_open_grid_orders', return_value=0):
            self.manager = GridTradingManager(MagicMock(), self.pm, MagicMock())
        self.manager.reconcile_pending_grid_orders_if_due = MagicMock()
        self.manager._check_exit_conditions = MagicMock(return_value=None)
        self.session = GridSession(id=1, stock_code='000001.SZ', center_price=10,
                                   current_center_price=10, callback_ratio=0.005,
                                   end_time=datetime.now() + timedelta(days=1))
        self.manager.sessions['000001'] = self.session
        self.manager.trackers[1] = PriceTracker(session_id=1, last_price=10)

    def prices(self, *values):
        return [self.manager.check_grid_signals('000001', price) for price in values]

    def test_locked_position_does_not_emit_sell_or_repeat_notifications(self):
        with patch('grid_trading_manager.logger.info') as info:
            self.assertEqual(self.prices(10.6, 10.7, 10.54, 10.8, 10.5), [None] * 5)
        notices = [call for call in info.call_args_list if '卖出受限' in str(call)]
        self.assertEqual(len(notices), 1)
        self.assertTrue(self.session.enabled)

    def test_locked_sell_does_not_disable_buy_signals(self):
        self.prices(10.6, 10.54)
        signals = self.prices(9.4, 9.3, 9.36)
        self.assertEqual(signals[-1]['signal_type'], 'BUY')

    def test_unlock_rechecks_price_without_replaying_frozen_peak(self):
        self.prices(10.6, 10.8, 10.5)
        self.position['available'] = 1000
        self.assertIsNone(self.prices(10.51)[0])
        self.assertIsNone(self.prices(10.7)[0])
        self.assertEqual(self.prices(10.64)[0]['signal_type'], 'SELL')

    def test_partial_available_position_can_sell(self):
        self.position['available'] = 200
        self.assertEqual(self.prices(10.6, 10.7, 10.54)[-1]['signal_type'], 'SELL')

    def test_unknown_available_does_not_generate_sell(self):
        self.position['available'] = None
        self.assertEqual(self.prices(10.6, 10.7, 10.54), [None] * 3)

    def test_restriction_reason_lookup_failure_still_blocks_sell(self):
        self.pm.get_position_restriction.side_effect = RuntimeError('原因取数失败')
        self.assertEqual(self.prices(10.6, 10.7, 10.54), [None] * 3)

    def test_freeze_after_crossing_discards_previous_sell_tracker(self):
        self.position['available'] = 1000
        self.prices(10.6, 10.8)
        self.position['available'] = 0
        self.assertIsNone(self.prices(10.5)[0])
        self.assertFalse(self.manager.trackers[1].waiting_callback)

    def test_execution_guard_rejects_zero_available_after_signal_generation(self):
        self.position['available'] = 1000
        signal = self.prices(10.6, 10.7, 10.54)[-1]
        self.position['available'] = 0
        self.manager._get_latest_price_for_signal = MagicMock(return_value=10.54)
        with patch.object(config, 'GRID_ENABLE_PRICE_LIMIT_GUARD', False):
            self.assertFalse(self.manager.execute_grid_trade(signal))
        self.manager.executor.sell_stock.assert_not_called()


class TestOrderTimeoutPrecision(unittest.TestCase):
    def test_default_check_interval_is_shorter_than_stop_loss_timeout(self):
        self.assertEqual(config.PENDING_ORDER_CHECK_INTERVAL_SECONDS, 3.0)

    def test_expired_order_is_processed_on_next_check(self):
        pm = PositionManager.__new__(PositionManager)
        pm.pending_orders_lock = threading.Lock()
        pm.order_check_interval = 3
        pm.last_order_check_time = 100
        now = datetime.now()
        pm.pending_orders = {'000001': {'stock_code': '000001', 'order_id': 1,
                            'submit_time': now - timedelta(seconds=31),
                            'signal_type': 'stop_loss', 'signal_info': {}}}
        with patch.object(config, 'ENABLE_SIMULATION_MODE', False), \
                patch.object(config, 'ENABLE_PENDING_ORDER_AUTO_CANCEL', True), \
                patch.object(config, 'is_continuous_trade_time', return_value=True), \
                patch.object(config, 'get_continuous_trading_seconds', return_value=31), \
                patch('position_manager.time.time', return_value=103), \
                patch.object(pm, '_handle_timeout_order') as handle:
            pm.check_pending_orders_timeout()
        handle.assert_called_once()
        self.assertGreaterEqual(pm.last_order_timeout_delay_seconds, 1)


class TestEquityQuality(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'quality_test.db')
        db_migrate.migrate_settlement_schema(self.path, do_backup=False)
        self.pm = MagicMock()
        self.good = {'total_asset': 10000, 'available': 6000,
                     'market_value': 4000, 'frozen_cash': 0}
        self.bad = dict(self.good, total_asset=10200)

    def write(self):
        # 本类验证的是告警开启时的复查/标记行为，显式打开开关（config.py 默认已关闭）
        with patch.object(config, 'ENABLE_ASSET_IDENTITY_ALERT', True, create=True), \
                patch.object(config, 'SETTLEMENT_ASSET_RECHECK_DELAY_SECONDS', 0, create=True):
            self.assertTrue(settlement_db.write_equity_snapshot(
                self.pm, 'open', snapshot_date='2026-10-19', db_path=self.path))
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            return dict(conn.execute('SELECT * FROM account_equity_daily').fetchone())

    def test_valid_reading_is_not_requeried(self):
        self.pm.get_account_info.return_value = self.good
        row = self.write()
        self.assertEqual(row['validation_status'], 'valid')
        self.assertEqual(row['identity_diff'], 0)
        self.assertTrue(row['observed_at'])
        self.pm.get_account_info.assert_called_once()

    def test_transient_mismatch_is_rechecked_and_original_reading_retained(self):
        self.pm.get_account_info.side_effect = [self.bad, self.good]
        row = self.write()
        self.assertEqual(row['total_asset'], 10000)
        self.assertEqual(row['validation_status'], 'valid')
        with closing(sqlite3.connect(self.path)) as conn, conn:
            detail = conn.execute("SELECT detail FROM run_events WHERE event_type='asset_identity_recheck'").fetchone()[0]
        self.assertIn('10200', detail)

    def test_persistent_mismatch_keeps_raw_total_and_marks_review(self):
        self.pm.get_account_info.return_value = self.bad
        row = self.write()
        self.assertEqual(row['total_asset'], 10200)
        self.assertEqual(row['identity_diff'], 200)
        self.assertEqual(row['validation_status'], 'pending_review')
        self.assertLessEqual(self.pm.get_account_info.call_count, 2)

    def test_recheck_exception_keeps_last_valid_reading(self):
        self.pm.get_account_info.side_effect = [self.bad, RuntimeError('测试取数失败')]
        row = self.write()
        self.assertEqual(row['total_asset'], 10200)
        self.assertEqual(row['validation_status'], 'pending_review')

    def test_failed_recheck_keeps_last_valid_raw_reading(self):
        self.pm.get_account_info.side_effect = [self.bad, None]
        row = self.write()
        self.assertEqual(row['total_asset'], 10200)
        self.assertEqual(row['validation_status'], 'pending_review')

    def test_quality_is_exported(self):
        self.pm.get_account_info.return_value = self.bad
        self.write()
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            rows = export_settlement.load_equity(conn, settlement_db.get_account_id(),
                                                  '2026-10-19', '2026-10-19')
        out = Path(self.temp.name) / 'daily.csv'
        export_settlement.write_account_daily('测试账号', rows, str(out))
        with out.open(encoding='utf-8', newline='') as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row['validation_status'], 'pending_review')

    def test_legacy_equity_table_migrates_idempotently(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('DROP TABLE account_equity_daily')
            conn.execute('CREATE TABLE account_equity_daily (account TEXT, date TEXT, snapshot_type TEXT)')
        for _ in range(2):
            db_migrate.migrate_settlement_schema(self.path, do_backup=False)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            columns = {row[1] for row in conn.execute('PRAGMA table_info(account_equity_daily)')}
        self.assertTrue({'identity_diff', 'validation_status', 'observed_at'} <= columns)


class TestSyncSerialization(unittest.TestCase):
    def test_timer_and_periodic_sync_do_not_overlap_database_connections(self):
        pm = PositionManager.__new__(PositionManager)
        pm.memory_conn_lock = threading.RLock()
        pm.memory_conn = object()
        pm._sync_retry_count = 0
        entered = threading.Event()
        release = threading.Event()
        connection = MagicMock()
        connection.cursor.return_value.fetchall.return_value = []
        def open_connection(*args):
            entered.set()
            release.wait(3)
            return connection
        workers = [threading.Thread(target=pm._sync_memory_to_db),
                   threading.Thread(target=pm._retry_sync)]
        with patch.object(config, 'ENABLE_SIMULATION_MODE', False), \
                patch.object(config, 'is_trade_time', return_value=True), \
                patch('position_manager.sqlite3.connect', side_effect=open_connection) as connect, \
                patch('position_manager.pd.read_sql_query', return_value=pd.DataFrame(columns=['stock_code'])):
            try:
                workers[0].start()
                self.assertTrue(entered.wait(2))
                workers[1].start()
                self.assertFalse(threading.Event().wait(0.1))
                self.assertEqual(connect.call_count, 1)
            finally:
                release.set()
                for worker in workers:
                    worker.join(4)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(connect.call_count, 2)
        status = pm.get_sync_status()
        self.assertIsNotNone(status['last_success_time'])
        self.assertGreaterEqual(status['duration_seconds'], 0)

    def test_failed_sync_does_not_report_new_success_and_releases_lock(self):
        pm = PositionManager.__new__(PositionManager)
        pm._sync_last_success_time = 123
        with patch.object(config, 'ENABLE_SIMULATION_MODE', False), \
                patch.object(config, 'is_trade_time', return_value=True), \
                patch('position_manager.sqlite3.connect', side_effect=sqlite3.OperationalError('测试写锁')), \
                patch.object(pm, '_schedule_sync_retry') as retry:
            pm._sync_memory_to_db()
        self.assertEqual(pm.get_sync_status()['last_success_time'], 123)
        self.assertIsInstance(pm._sync_last_error, sqlite3.OperationalError)
        retry.assert_called_once()
        self.assertTrue(pm._memory_sync_lock.acquire(blocking=False))
        pm._memory_sync_lock.release()


class TestLogCleanupSchedule(unittest.TestCase):
    def test_missed_minute_still_cleans_once_per_day(self):
        moments = [datetime(2026, 10, 19, 0, 3), datetime(2026, 10, 19, 0, 4),
                   datetime(2026, 10, 20, 0, 2)]
        with patch.object(config, 'ENABLE_LOG_CLEANUP', True), \
                patch.object(config, 'LOG_CLEANUP_TIME', '00:00:00'), \
                patch('logger.datetime') as clock, \
                patch('logger.clean_old_logs') as clean, \
                patch('logger.time.sleep', side_effect=[None, None, StopIteration]):
            clock.now.side_effect = moments
            clock.strptime = datetime.strptime
            with self.assertRaises(StopIteration):
                log_module.schedule_log_cleanup()
        self.assertEqual(clean.call_count, 2)

    def test_cleanup_failure_can_retry_next_check(self):
        with patch.object(config, 'ENABLE_LOG_CLEANUP', True), \
                patch.object(config, 'LOG_CLEANUP_TIME', '00:00:00'), \
                patch('logger.clean_old_logs', side_effect=[OSError('测试失败'), None]) as clean, \
                patch('logger.time.sleep', side_effect=[None, StopIteration]):
            with self.assertRaises(StopIteration):
                log_module.schedule_log_cleanup()
        self.assertEqual(clean.call_count, 2)

    def test_before_cleanup_time_does_not_run(self):
        with patch.object(config, 'ENABLE_LOG_CLEANUP', True), \
                patch.object(config, 'LOG_CLEANUP_TIME', '23:59:59'), \
                patch('logger.datetime') as clock, \
                patch('logger.clean_old_logs') as clean, \
                patch('logger.time.sleep', side_effect=StopIteration):
            clock.now.return_value = datetime(2026, 10, 19, 10)
            clock.strptime = datetime.strptime
            with self.assertRaises(StopIteration):
                log_module.schedule_log_cleanup()
        clean.assert_not_called()


class TestRuntimeVersion(unittest.TestCase):
    def test_lifecycle_contains_release_and_source_fingerprint(self):
        import main
        identity = main._get_runtime_identity()
        expected = json.loads(Path('release_version.json').read_text(encoding='utf-8'))['releaseVersion']
        self.assertEqual(identity['release_version'], expected)
        self.assertRegex(identity['code_id'], r'^[0-9a-f]{12}$')
        formatted = main._format_runtime_identity(identity)
        self.assertIn(f'version={expected}', formatted)
        self.assertIn('code_id=', formatted)


if __name__ == '__main__':
    unittest.main()
