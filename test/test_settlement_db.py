# -*- coding: utf-8 -*-
"""
settlement_db 单元测试：持仓快照 / 每日净值 / 运行事件 / 交易日历。

全部跑在临时测试库上，不碰生产库。
"""
import os
import sqlite3
import time
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, time as dtime

import pandas as pd

import config
import db_migrate
import settlement_db as sdb


class FakePositionManager:
    """最小可用的 position_manager 替身。"""

    def __init__(self, positions=None, account_info=None):
        self._positions = positions if positions is not None else pd.DataFrame()
        self._account_info = account_info
        self.raise_on_positions = False
        self.raise_on_account = False

    def get_all_positions_with_all_fields(self):
        if self.raise_on_positions:
            raise RuntimeError("模拟取数失败")
        return self._positions

    def get_account_info(self):
        if self.raise_on_account:
            raise RuntimeError("模拟取数失败")
        return self._account_info


def make_positions(rows):
    cols = ['stock_code', 'stock_name', 'volume', 'available', 'cost_price',
            'base_cost_price', 'current_price', 'market_value', 'profit_ratio']
    return pd.DataFrame(rows, columns=cols)


class SettlementDBTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix='_test.db', prefix='settlement_')
        os.close(fd)
        db_migrate.migrate_settlement_schema(self.db, do_backup=False)
        # 交易日历依赖 stock_daily_data
        conn = sqlite3.connect(self.db)
        conn.execute('''CREATE TABLE IF NOT EXISTS stock_daily_data (
            stock_code TEXT NOT NULL, date TEXT NOT NULL, open REAL, high REAL,
            low REAL, close REAL, volume REAL, amount REAL,
            PRIMARY KEY (stock_code, date))''')
        conn.commit()
        conn.close()

    def tearDown(self):
        for suffix in ('', '-wal', '-shm'):
            path = self.db + suffix
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    def add_kline_days(self, days):
        conn = sqlite3.connect(self.db)
        conn.executemany(
            "INSERT OR REPLACE INTO stock_daily_data(stock_code, date, close) "
            "VALUES ('000001', ?, 10.0)", [(d,) for d in days])
        conn.commit()
        conn.close()

    def query(self, sql, params=()):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()


class TestRunEvents(SettlementDBTestBase):
    def test_log_event_writes_row(self):
        ok = sdb.log_event('deal_received', 'INFO', {'a': 1},
                           code='000001', order_id=123, db_path=self.db)
        self.assertTrue(ok)
        rows = self.query("SELECT * FROM run_events")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['event_type'], 'deal_received')
        self.assertEqual(rows[0]['code'], '000001')
        self.assertEqual(rows[0]['order_id'], '123')
        self.assertIn('"a": 1', rows[0]['detail'])

    def test_log_event_never_raises(self):
        """它常在异常处理路径里被调用，自己炸会掩盖原始错误。"""
        ok = sdb.log_event('x', db_path='/nonexistent_dir/nope.db')
        self.assertFalse(ok)

    def test_detail_accepts_plain_string(self):
        sdb.log_event('startup', detail='plain text', db_path=self.db)
        self.assertEqual(self.query("SELECT detail FROM run_events")[0]['detail'],
                         'plain text')


class TestTradingCalendar(SettlementDBTestBase):
    def test_day_with_kline_is_trading_day(self):
        self.add_kline_days(['2026-09-10', '2026-09-11'])
        trading, confident = sdb.is_trading_day('2026-09-10', self.db)
        self.assertTrue(trading)
        self.assertTrue(confident)

    def test_holiday_inside_coverage_is_not_trading_day(self):
        """覆盖范围内但无 K 线 —— 确信是休市日（这正是节假日的样子）。"""
        self.add_kline_days(['2026-09-30', '2026-10-09'])
        trading, confident = sdb.is_trading_day('2026-10-01', self.db)
        self.assertFalse(trading)
        self.assertTrue(confident)

    def test_outside_coverage_falls_back_to_weekday(self):
        self.add_kline_days(['2026-09-10'])
        # 2027-01-04 是周一，超出覆盖范围 → 降级判断且 confident=False
        trading, confident = sdb.is_trading_day('2027-01-04', self.db)
        self.assertTrue(trading)
        self.assertFalse(confident)
        # 2027-01-09 是周六
        trading, confident = sdb.is_trading_day('2027-01-09', self.db)
        self.assertFalse(trading)
        self.assertFalse(confident)

    def test_empty_kline_table_falls_back(self):
        trading, confident = sdb.is_trading_day('2026-09-10', self.db)
        self.assertFalse(confident)


class TestPositionSnapshot(SettlementDBTestBase):
    def test_writes_all_positions(self):
        pm = FakePositionManager(make_positions([
            ['000620', '盈新发展', 1000, 1000, 5.0, 5.0, 5.5, 5500, 0.10],
            ['301085.SZ', '亚康股份', 1200, 0, 73.54, 73.54, 70.0, 84000, -0.048],
        ]))
        n = sdb.write_position_snapshot(pm, sdb.SNAPSHOT_OPEN,
                                        snapshot_date='2026-09-14', db_path=self.db)
        self.assertEqual(n, 2)
        rows = self.query("SELECT * FROM position_snapshot ORDER BY code")
        self.assertEqual([r['code'] for r in rows], ['000620', '301085'])
        self.assertEqual(rows[0]['snapshot_type'], 'open')
        self.assertAlmostEqual(rows[1]['cost_price'], 73.54)

    def test_code_normalized_strips_suffix(self):
        """库里混存 '002319' 与 '301399.SZ' 两种格式，快照必须统一。"""
        pm = FakePositionManager(make_positions([
            ['600519.SH', '贵州茅台', 100, 100, 1000.0, 1000.0, 1100.0, 110000, 0.1],
        ]))
        sdb.write_position_snapshot(pm, sdb.SNAPSHOT_OPEN,
                                    snapshot_date='2026-09-14', db_path=self.db)
        self.assertEqual(self.query("SELECT code FROM position_snapshot")[0]['code'],
                         '600519')

    def test_rerun_is_idempotent(self):
        pm = FakePositionManager(make_positions([
            ['000620', '盈新发展', 1000, 1000, 5.0, 5.0, 5.5, 5500, 0.10],
        ]))
        sdb.write_position_snapshot(pm, sdb.SNAPSHOT_OPEN,
                                    snapshot_date='2026-09-14', db_path=self.db)
        pm._positions = make_positions([
            ['000620', '盈新发展', 2000, 2000, 5.0, 5.0, 6.0, 12000, 0.20],
        ])
        sdb.write_position_snapshot(pm, sdb.SNAPSHOT_OPEN,
                                    snapshot_date='2026-09-14', db_path=self.db)
        rows = self.query("SELECT * FROM position_snapshot")
        self.assertEqual(len(rows), 1, "同日同类型重跑应覆盖而非新增")
        self.assertEqual(rows[0]['volume'], 2000)

    def test_open_and_close_coexist(self):
        pm = FakePositionManager(make_positions([
            ['000620', '盈新发展', 1000, 1000, 5.0, 5.0, 5.5, 5500, 0.10],
        ]))
        sdb.write_position_snapshot(pm, sdb.SNAPSHOT_OPEN,
                                    snapshot_date='2026-09-14', db_path=self.db)
        sdb.write_position_snapshot(pm, sdb.SNAPSHOT_CLOSE,
                                    snapshot_date='2026-09-14', db_path=self.db)
        self.assertEqual(len(self.query("SELECT * FROM position_snapshot")), 2)

    def test_empty_positions_writes_zero_rows_not_failure(self):
        """空持仓是合法状态（全部卖出），必须与取数失败区分开。"""
        pm = FakePositionManager(pd.DataFrame())
        n = sdb.write_position_snapshot(pm, sdb.SNAPSHOT_OPEN,
                                        snapshot_date='2026-09-14', db_path=self.db)
        self.assertEqual(n, 0)
        self.assertNotEqual(n, -1)

    def test_fetch_failure_returns_minus_one_and_logs_event(self):
        pm = FakePositionManager(make_positions([]))
        pm.raise_on_positions = True
        n = sdb.write_position_snapshot(pm, sdb.SNAPSHOT_OPEN,
                                        snapshot_date='2026-09-14', db_path=self.db)
        self.assertEqual(n, -1)
        events = self.query("SELECT * FROM run_events WHERE event_type=?",
                            ('snapshot_write_failed',))
        self.assertEqual(len(events), 1)


class TestEquitySnapshot(SettlementDBTestBase):
    def _info(self, total, mv, cash, frozen):
        return {'account_id': '25105132', 'account_type': 'STOCK',
                'total_asset': total, 'market_value': mv,
                'available': cash, 'frozen_cash': frozen,
                'timestamp': '2026-09-14 15:05:00'}

    def test_writes_equity_row(self):
        pm = FakePositionManager(account_info=self._info(100000, 60000, 40000, 0))
        ok = sdb.write_equity_snapshot(pm, sdb.SNAPSHOT_CLOSE,
                                       snapshot_date='2026-09-14', db_path=self.db)
        self.assertTrue(ok)
        row = self.query("SELECT * FROM account_equity_daily")[0]
        self.assertEqual(row['total_asset'], 100000)
        self.assertEqual(row['cash'], 40000)
        self.assertIsNone(row['deposit'], "出入金拿不到，必须留 NULL 而不是填 0")

    def test_identity_check_passes_silently(self):
        pm = FakePositionManager(account_info=self._info(100000, 60000, 40000, 0))
        sdb.write_equity_snapshot(pm, sdb.SNAPSHOT_OPEN,
                                  snapshot_date='2026-09-14', db_path=self.db)
        self.assertEqual(
            self.query("SELECT * FROM run_events WHERE event_type=?",
                       ('asset_identity_mismatch',)), [])

    def test_identity_mismatch_logs_event(self):
        """差额说明账户里有非股票资产项，归因时必须排除。"""
        pm = FakePositionManager(account_info=self._info(150000, 60000, 40000, 0))
        sdb.write_equity_snapshot(pm, sdb.SNAPSHOT_OPEN,
                                  snapshot_date='2026-09-14', db_path=self.db)
        events = self.query("SELECT * FROM run_events WHERE event_type=?",
                            ('asset_identity_mismatch',))
        self.assertEqual(len(events), 1)
        self.assertIn('50000', events[0]['detail'])

    def test_frozen_cash_counted_in_identity(self):
        pm = FakePositionManager(account_info=self._info(100000, 60000, 35000, 5000))
        sdb.write_equity_snapshot(pm, sdb.SNAPSHOT_OPEN,
                                  snapshot_date='2026-09-14', db_path=self.db)
        self.assertEqual(
            self.query("SELECT * FROM run_events WHERE event_type=?",
                       ('asset_identity_mismatch',)), [])

    def test_missing_frozen_cash_does_not_crash(self):
        """模拟分支没有 frozen_cash 这个 key。"""
        info = {'account_id': 'SIM', 'total_asset': 100000,
                'market_value': 60000, 'available': 40000, 'profit_loss': 0.0}
        pm = FakePositionManager(account_info=info)
        self.assertTrue(sdb.write_equity_snapshot(
            pm, sdb.SNAPSHOT_OPEN, snapshot_date='2026-09-14', db_path=self.db))

    def test_none_account_info_returns_false(self):
        pm = FakePositionManager(account_info=None)
        ok = sdb.write_equity_snapshot(pm, sdb.SNAPSHOT_CLOSE,
                                       snapshot_date='2026-09-14', db_path=self.db)
        self.assertFalse(ok)
        self.assertEqual(len(self.query(
            "SELECT * FROM run_events WHERE event_type=?", ('asset_write_failed',))), 1)

    def test_daily_pnl_derived_from_open(self):
        pm = FakePositionManager(account_info=self._info(100000, 60000, 40000, 0))
        sdb.write_equity_snapshot(pm, sdb.SNAPSHOT_OPEN,
                                  snapshot_date='2026-09-14', db_path=self.db)
        pm._account_info = self._info(101000, 61000, 40000, 0)
        sdb.write_equity_snapshot(pm, sdb.SNAPSHOT_CLOSE,
                                  snapshot_date='2026-09-14', db_path=self.db)
        close = self.query("SELECT * FROM account_equity_daily WHERE snapshot_type='close'")[0]
        self.assertAlmostEqual(close['daily_pnl'], 1000.0)
        self.assertIsNone(close['unexplained_delta'])

    def test_large_jump_flagged_as_unexplained(self):
        """人为转入大额资金：必须标出来（允许误报，不允许漏报）。"""
        pm = FakePositionManager(account_info=self._info(100000, 60000, 40000, 0))
        sdb.write_equity_snapshot(pm, sdb.SNAPSHOT_OPEN,
                                  snapshot_date='2026-09-14', db_path=self.db)
        pm._account_info = self._info(110000, 60000, 50000, 0)
        sdb.write_equity_snapshot(pm, sdb.SNAPSHOT_CLOSE,
                                  snapshot_date='2026-09-14', db_path=self.db)
        close = self.query("SELECT * FROM account_equity_daily WHERE snapshot_type='close'")[0]
        self.assertAlmostEqual(close['unexplained_delta'], 10000.0)
        self.assertEqual(len(self.query(
            "SELECT * FROM run_events WHERE event_type=?", ('asset_jump',))), 1)

    def test_open_snapshot_has_no_pnl(self):
        pm = FakePositionManager(account_info=self._info(100000, 60000, 40000, 0))
        sdb.write_equity_snapshot(pm, sdb.SNAPSHOT_OPEN,
                                  snapshot_date='2026-09-14', db_path=self.db)
        row = self.query("SELECT * FROM account_equity_daily")[0]
        self.assertIsNone(row['daily_pnl'])

    def test_all_zero_reading_is_rejected(self):
        """QMT 未连接时 balance() 返回整行 0 —— 那是无效读数，不是净值。

        回归：恒等式校验拦不住它（0 == 0+0+0 恒成立），曾以 source='qmt_api'
        落库，让净值曲线凭空出现归零点。
        """
        pm = FakePositionManager(account_info=self._info(0.0, 0.0, 0.0, 0.0))
        ok = sdb.write_equity_snapshot(pm, sdb.SNAPSHOT_CLOSE,
                                       snapshot_date='2026-09-12', db_path=self.db)
        self.assertFalse(ok)
        self.assertEqual(self.query("SELECT * FROM account_equity_daily"), [],
                         "全零读数不得落库")
        events = self.query("SELECT * FROM run_events WHERE event_type=?",
                            ('asset_write_failed',))
        self.assertEqual(len(events), 1)
        self.assertIn('invalid_asset_reading', events[0]['detail'])

    def test_all_none_reading_is_rejected(self):
        pm = FakePositionManager(account_info={'account_id': 'X'})
        self.assertFalse(sdb.write_equity_snapshot(
            pm, sdb.SNAPSHOT_CLOSE, snapshot_date='2026-09-12', db_path=self.db))

    def test_empty_position_but_real_cash_is_accepted(self):
        """空仓但有可用资金是正常状态，不能被全零判定误杀。"""
        pm = FakePositionManager(account_info=self._info(50000.0, 0.0, 50000.0, 0.0))
        self.assertTrue(sdb.write_equity_snapshot(
            pm, sdb.SNAPSHOT_CLOSE, snapshot_date='2026-09-12', db_path=self.db))
        self.assertEqual(len(self.query("SELECT * FROM account_equity_daily")), 1)


class TestBlankAssetDetection(unittest.TestCase):
    def test_detects_all_zero(self):
        self.assertTrue(sdb._is_blank_asset(0.0, 0.0, 0.0, 0.0))

    def test_detects_all_none(self):
        self.assertTrue(sdb._is_blank_asset(None, None, None, None))

    def test_mixed_zero_and_none_is_blank(self):
        self.assertTrue(sdb._is_blank_asset(0.0, None, 0.0, None))

    def test_any_nonzero_is_valid(self):
        self.assertFalse(sdb._is_blank_asset(50000.0, 0.0, 50000.0, 0.0))
        self.assertFalse(sdb._is_blank_asset(0.0, 0.0, 0.01, 0.0))


class TestCloseSnapshotGate(unittest.TestCase):
    """收盘快照的触发门控。

    回归：周六实测写出了一份 close 快照 —— 降级判断（K 线未入库时按
    工作日放行）没有排除周末。
    """

    TARGET = dtime(15, 5, 0)

    def _at(self, text):
        return datetime.strptime(text, '%Y-%m-%d %H:%M:%S')

    def test_confident_trading_day_runs(self):
        # 2026-09-11 周五
        self.assertTrue(sdb.should_run_close_snapshot(
            self._at('2026-09-11 15:06:00'), self.TARGET, None, True, True))

    def test_saturday_does_not_run_even_when_unconfident(self):
        # 2026-09-12 周六，当天无 K 线 → (trading=False, confident=False)
        self.assertFalse(sdb.should_run_close_snapshot(
            self._at('2026-09-12 18:49:00'), self.TARGET, None, False, False),
            "周末不得写收盘快照")

    def test_sunday_does_not_run(self):
        self.assertFalse(sdb.should_run_close_snapshot(
            self._at('2026-09-13 16:00:00'), self.TARGET, None, False, False))

    def test_weekday_unconfident_still_runs(self):
        """交易日当天 K 线未入库时仍要跑 —— 宁可多写一次也不漏。"""
        self.assertTrue(sdb.should_run_close_snapshot(
            self._at('2026-09-14 15:06:00'), self.TARGET, None, False, False))

    def test_confirmed_holiday_does_not_run(self):
        """确信是休市日（长假）→ 不跑，避免长假期间每天写空快照。"""
        self.assertFalse(sdb.should_run_close_snapshot(
            self._at('2026-10-01 15:06:00'), self.TARGET, None, False, True))

    def test_before_target_time_does_not_run(self):
        self.assertFalse(sdb.should_run_close_snapshot(
            self._at('2026-09-14 14:59:00'), self.TARGET, None, True, True))

    def test_already_ran_today_does_not_rerun(self):
        now = self._at('2026-09-14 16:00:00')
        self.assertFalse(sdb.should_run_close_snapshot(
            now, self.TARGET, now.date(), True, True))

    def test_late_start_still_backfills_same_day(self):
        """进程 22:00 才启动也应补录当天收盘快照。"""
        self.assertTrue(sdb.should_run_close_snapshot(
            self._at('2026-09-14 22:00:00'), self.TARGET, None, True, True))


class TestSnapshotHealth(SettlementDBTestBase):
    def test_no_missing_when_snapshots_complete(self):
        pm = FakePositionManager(make_positions([
            ['000620', '盈新发展', 1000, 1000, 5.0, 5.0, 5.5, 5500, 0.10]]))
        today = datetime.now().date()
        days = []
        d = today - timedelta(days=1)
        while len(days) < 3:
            if d.weekday() < 5:
                days.append(d.strftime('%Y-%m-%d'))
            d -= timedelta(days=1)
        self.add_kline_days(days)
        for day in days:
            for stype in (sdb.SNAPSHOT_OPEN, sdb.SNAPSHOT_CLOSE):
                sdb.write_position_snapshot(pm, stype, snapshot_date=day, db_path=self.db)
        self.assertEqual(sdb.check_snapshot_health(lookback_days=7, db_path=self.db), [])

    def test_holidays_do_not_count_as_missing(self):
        """长假不该产生连续缺失误报 —— 这是没有节假日日历时的最大误报源。"""
        self.add_kline_days([])
        missing = sdb.check_snapshot_health(lookback_days=7, db_path=self.db)
        self.assertEqual(missing, [], "无 K 线覆盖时应跳过判断而不是全部报缺失")

    def test_missing_day_detected(self):
        today = datetime.now().date()
        days = []
        d = today - timedelta(days=1)
        while len(days) < 2:
            if d.weekday() < 5:
                days.append(d.strftime('%Y-%m-%d'))
            d -= timedelta(days=1)
        self.add_kline_days(days)
        missing = sdb.check_snapshot_health(lookback_days=7, db_path=self.db)
        self.assertEqual(len(missing), 2)
        events = self.query("SELECT * FROM run_events WHERE event_type=?",
                            ('snapshot_missing_streak',))
        self.assertEqual(len(events), 1, "连续 2 个交易日缺失应告警一次")


class TestMigrationGuards(unittest.TestCase):
    def test_production_db_rejected_for_destructive_ops(self):
        with self.assertRaises(RuntimeError):
            db_migrate.assert_test_db('data_25105132/trading.db', 'DROP TABLE')

    def test_memory_db_allowed(self):
        db_migrate.assert_test_db(':memory:', 'DROP TABLE')

    def test_ensure_column_is_idempotent(self):
        conn = sqlite3.connect(':memory:')
        conn.execute("CREATE TABLE t (a INTEGER)")
        self.assertTrue(db_migrate.ensure_column(conn, 't', 'b', 'TEXT'))
        self.assertFalse(db_migrate.ensure_column(conn, 't', 'b', 'TEXT'))
        cols = [r[1] for r in conn.execute("PRAGMA table_info(t)")]
        self.assertEqual(cols, ['a', 'b'])
        conn.close()


class TestDeriveAccountId(unittest.TestCase):
    def test_derives_from_account_directory(self):
        self.assertEqual(db_migrate.derive_account_id('/x/data_25105132/trading.db'),
                         '25105132')
        self.assertEqual(db_migrate.derive_account_id('C:/a/data_25106531/trading.db'),
                         '25106531')

    def test_default_db_has_no_account(self):
        self.assertIsNone(db_migrate.derive_account_id('data/trading.db'))
        self.assertIsNone(db_migrate.derive_account_id('data/trading_test.db'))


class TestTradeRecordsExtension(unittest.TestCase):
    """迁移扩展：补列 → 回填 account → 归档占位流水 → 标记重复 → 建索引。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='migrate_ext_')
        # 用 data_<id>/ 目录结构，derive_account_id 才推得出账号
        self.acct_dir = os.path.join(self.tmpdir, 'data_25105132')
        os.makedirs(self.acct_dir)
        self.db = os.path.join(self.acct_dir, 'trading.db')

        conn = sqlite3.connect(self.db)
        conn.execute('''CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT, stock_code TEXT, stock_name TEXT,
            trade_time TIMESTAMP, trade_type TEXT, price REAL, volume INTEGER,
            amount REAL, trade_id TEXT, commission REAL, strategy TEXT)''')
        conn.executemany(
            "INSERT INTO trade_records(stock_code, stock_name, trade_time, trade_type,"
            " price, volume, amount, trade_id, commission, strategy)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)", self._rows())
        conn.commit()
        conn.close()

    def _rows(self):
        return [
            # 真实成交，唯一
            ('000620', '盈新发展', '2026-08-25 14:30:59', 'BUY', 5.0, 1000, 5000.0,
             '74500104000054860480', 0.0, 'external'),
            # 真实成交，被写了两遍（同 id 同股同时间）
            ('300454', '深信服', '2026-08-04 13:32:34', 'BUY', 122.2, 500, 61100.0,
             '74500104000054860481', 0.0, 'external'),
            ('300454', '深信服', '2026-08-04 13:32:34', 'BUY', 122.2, 500, 61100.0,
             '74500104000054860481', 0.0, 'external'),
            # 网格：写的是 order_id，同一个 id 跨股票复用（不是重复成交）
            ('301085', '亚康股份', '2026-09-10 09:42:24', 'BUY', 73.5, 100, 7350.0,
             '940572675', 0.0, 'grid'),
            ('603757', '大元泵业', '2026-09-11 10:20:22', 'SELL', 20.0, 100, 2000.0,
             '940572675', 0.0, 'grid'),
            # 占位流水
            ('000620', '盈新发展', '2026-08-25 14:30:55', 'BUY', 5.0, 1000, 5000.0,
             'ORDER_1209008129', 0.0, 'default'),
        ]

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _query(self, sql, params=()):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    def test_account_backfilled_from_path(self):
        db_migrate.apply_trade_records_extension(self.db, do_backup=False)
        rows = self._query("SELECT DISTINCT account FROM trade_records")
        self.assertEqual([r['account'] for r in rows], ['25105132'])

    def test_placeholder_rows_archived_then_deleted(self):
        report = db_migrate.apply_trade_records_extension(self.db, do_backup=False)
        self.assertEqual(report['archived_placeholder'], 1)
        self.assertEqual(self._query(
            "SELECT * FROM trade_records WHERE trade_id LIKE 'ORDER_%'"), [])
        archived = self._query("SELECT * FROM trade_records_placeholder_archive")
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0]['trade_id'], 'ORDER_1209008129')
        self.assertIn('placeholder_purge', archived[0]['archive_reason'])

    def test_duplicate_deal_marked_not_deleted(self):
        report = db_migrate.apply_trade_records_extension(self.db, do_backup=False)
        self.assertEqual(len(report['marked_duplicates']), 1)
        marked = report['marked_duplicates'][0]
        self.assertEqual(marked['superseded'], [marked['keep_id'] + 1])
        rows = self._query(
            "SELECT id, row_status, duplicate_of FROM trade_records "
            "WHERE trade_id='74500104000054860481' ORDER BY id")
        self.assertEqual(rows[0]['row_status'], 'active')
        self.assertEqual(rows[1]['row_status'], 'superseded')
        self.assertEqual(rows[1]['duplicate_of'], rows[0]['id'])
        # 原始事实不丢
        self.assertEqual(len(rows), 2)

    def test_grid_order_id_cross_stock_not_treated_as_duplicate(self):
        """网格写的是 order_id，跨股票复用是 ID 复用，不是重复成交。"""
        report = db_migrate.apply_trade_records_extension(self.db, do_backup=False)
        for m in report['marked_duplicates']:
            self.assertNotEqual(m['trade_id'], '940572675')
        rows = self._query(
            "SELECT row_status FROM trade_records WHERE trade_id='940572675'")
        self.assertEqual([r['row_status'] for r in rows], ['active', 'active'])

    def test_unique_index_blocks_duplicate_deal(self):
        """价量完全相同的同一笔 deal 必须被唯一索引挡下。

        注意键含 volume/price —— 插入时若漏掉这两列会得到 NULL，
        而 SQLite 唯一索引里 NULL 互不相等，索引就形同虚设。
        """
        db_migrate.apply_trade_records_extension(self.db, do_backup=False)
        conn = sqlite3.connect(self.db)
        cols = ("account, stock_code, trade_time, trade_type, trade_id, "
                "volume, price, amount, row_status")
        vals = ('25105132', '300454', '2026-08-04 13:32:34', 'BUY',
                '74500104000054860499', 500, 122.2, 61100.0, 'active')
        conn.execute("INSERT INTO trade_records(%s) VALUES (?,?,?,?,?,?,?,?,?)" % cols,
                     vals)
        conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO trade_records(%s) VALUES (?,?,?,?,?,?,?,?,?)"
                         % cols, vals)
        conn.close()

    def test_unique_index_allows_same_id_different_volume(self):
        """同键但量不同 = 分笔成交，唯一索引不得拦。"""
        db_migrate.apply_trade_records_extension(self.db, do_backup=False)
        conn = sqlite3.connect(self.db)
        cols = ("account, stock_code, trade_time, trade_type, trade_id, "
                "volume, price, amount, row_status")
        conn.execute("INSERT INTO trade_records(%s) VALUES (?,?,?,?,?,?,?,?,?)" % cols,
                     ('25105132', '300454', '2026-08-04 13:32:34', 'BUY',
                      '74500104000054860499', 500, 122.2, 61100.0, 'active'))
        conn.execute("INSERT INTO trade_records(%s) VALUES (?,?,?,?,?,?,?,?,?)" % cols,
                     ('25105132', '300454', '2026-08-04 13:32:34', 'BUY',
                      '74500104000054860499', 300, 122.2, 36660.0, 'active'))
        conn.commit()
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM trade_records WHERE trade_id=?",
                         ('74500104000054860499',)).fetchone()[0], 2,
            "同 id 不同量的两笔分笔成交都必须保留")
        conn.close()

    def test_index_survives_null_account(self):
        """SQLite 唯一索引里 NULL 互不相等 —— 不用 COALESCE 索引就是摆设。

        这是真实踩到的坑：历史行 account 全为 NULL，按 (account, trade_id, ...)
        建的唯一索引一条重复都拦不住。用独立表隔离验证，避免与 fixture 里的
        重复行相互干扰。
        """
        conn = sqlite3.connect(':memory:')
        conn.execute("CREATE TABLE t (stock_code TEXT, trade_time TEXT, "
                     "trade_type TEXT, trade_id TEXT, account TEXT)")
        conn.execute("CREATE UNIQUE INDEX ix_t ON t"
                     "(COALESCE(account,''), trade_id, stock_code, trade_time)")
        conn.execute("INSERT INTO t(account,stock_code,trade_time,trade_type,trade_id)"
                     " VALUES (NULL,'X','2026-01-01 09:30:00','BUY','T1')")
        conn.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO t(account,stock_code,trade_time,trade_type,trade_id)"
                         " VALUES (NULL,'X','2026-01-01 09:30:00','BUY','T1')")

        # 对照：不加 COALESCE 时两条 NULL 行并存，索引形同虚设
        conn.execute("CREATE TABLE t2 (stock_code TEXT, trade_time TEXT, "
                     "trade_type TEXT, trade_id TEXT, account TEXT)")
        conn.execute("CREATE UNIQUE INDEX ix_t2 ON t2"
                     "(account, trade_id, stock_code, trade_time)")
        conn.execute("INSERT INTO t2(account,stock_code,trade_time,trade_type,trade_id)"
                     " VALUES (NULL,'X','2026-01-01 09:30:00','BUY','T1')")
        conn.execute("INSERT INTO t2(account,stock_code,trade_time,trade_type,trade_id)"
                     " VALUES (NULL,'X','2026-01-01 09:30:00','BUY','T1')")
        conn.commit()
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM t2").fetchone()[0], 2,
            "不带 COALESCE 时 NULL 行不冲突，这条对照说明为什么必须包 COALESCE")
        conn.close()

    def test_migration_is_idempotent(self):
        first = db_migrate.apply_trade_records_extension(self.db, do_backup=False)
        second = db_migrate.apply_trade_records_extension(self.db, do_backup=False)
        self.assertEqual(second['added_columns'], [])
        self.assertEqual(second['account_backfilled'], 0)
        self.assertEqual(second['archived_placeholder'], 0)
        self.assertEqual(second['marked_duplicates'], [])
        self.assertEqual(first['rows_after'], second['rows_after'])

    def test_dry_run_does_not_touch_db(self):
        """ALTER/CREATE INDEX 会隐式提交，dry-run 必须靠副本而非 rollback。"""
        before_cols = [r['name'] for r in self._query("PRAGMA table_info(trade_records)")]
        before_rows = self._query("SELECT COUNT(*) c FROM trade_records")[0]['c']

        report = db_migrate.apply_trade_records_extension(
            self.db, do_backup=False, dry_run=True)

        after_cols = [r['name'] for r in self._query("PRAGMA table_info(trade_records)")]
        after_rows = self._query("SELECT COUNT(*) c FROM trade_records")[0]['c']
        self.assertEqual(before_cols, after_cols, "dry-run 不应改 schema")
        self.assertEqual(before_rows, after_rows, "dry-run 不应改数据")
        self.assertEqual(self._query(
            "SELECT name FROM sqlite_master WHERE name='trade_records_placeholder_archive'"),
            [], "dry-run 不应建归档表")
        # 但报告要反映真实会发生什么
        self.assertTrue(report['dry_run'])
        self.assertEqual(report['archived_placeholder'], 1)
        self.assertEqual(len(report['marked_duplicates']), 1)

    def test_archive_table_created_on_real_run(self):
        db_migrate.apply_trade_records_extension(self.db, do_backup=False)
        self.assertTrue(db_migrate.table_exists(
            sqlite3.connect(self.db), 'trade_records_placeholder_archive'))

    def test_schema_migration_dry_run_does_not_create_tables(self):
        """踩过的坑：--dry-run 里先调了建表函数，照样把表建到了生产库。

        CREATE TABLE 与 ALTER 一样是隐式提交的 DDL，dry_run 必须走副本。
        """
        conn = sqlite3.connect(self.db)
        before = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()

        created = db_migrate.migrate_settlement_schema(
            self.db, do_backup=False, dry_run=True)

        conn = sqlite3.connect(self.db)
        after = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertEqual(before, after, "dry-run 不应在真库建表")
        self.assertNotIn('run_events', after)
        # 但报告要说明将会建哪些
        self.assertIn('run_events', created)
        self.assertIn('position_snapshot', created)


class TestParseDealTime(unittest.TestCase):
    """XtTrade.traded_time 的三种编码 —— 解析不出来必须返回 None，不许猜。"""

    def test_epoch_seconds(self):
        epoch, text = sdb.parse_deal_time(1789353015)
        self.assertEqual(epoch, 1789353015)
        self.assertEqual(text, '2026-09-14 10:30:15')

    def test_epoch_milliseconds(self):
        epoch, text = sdb.parse_deal_time(1789353015000)
        self.assertEqual(epoch, 1789353015)
        self.assertEqual(text, '2026-09-14 10:30:15')

    def test_yyyymmddhhmmss(self):
        epoch, text = sdb.parse_deal_time(20260914103015)
        self.assertEqual(text, '2026-09-14 10:30:15')

    def test_datetime_string(self):
        epoch, text = sdb.parse_deal_time('2026-09-14 10:30:15')
        self.assertEqual(text, '2026-09-14 10:30:15')

    def test_hhmmss_uses_today(self):
        now = datetime(2026, 9, 14, 15, 0, 0)
        epoch, text = sdb.parse_deal_time(103015, now=now)
        self.assertEqual(text, '2026-09-14 10:30:15')

    def test_unparseable_returns_none(self):
        for raw in (None, '', 0, 123, 'abc', -5):
            with self.subTest(raw=raw):
                self.assertEqual(sdb.parse_deal_time(raw), (None, None),
                                 "解析不出来必须是 None，让调用方标 local_fallback")


class TestStrategyLabel(unittest.TestCase):
    def test_known_mappings(self):
        cases = {
            'grid': '网格', 'auto_partial': '首次部分止盈',
            'auto_full': '动态全仓止盈', 'stop_loss': '固定止损',
            'reorder_stop_loss': '固定止损',          # 止损重挂仍属止损
            'reorder_take_profit_half': '部分止盈重挂',
            'M_real': '手工买入', 'manual_real': '手工买入',
            'external': '外部-计划外',                 # 来源不明，不冒充手工买入
            'default': '外部-计划外',
            'M_simu': '模拟买入', 'simu_full': '模拟买入',
        }
        for code, label in cases.items():
            with self.subTest(code=code):
                self.assertEqual(sdb.strategy_label_for(code), label)

    def test_unknown_is_not_guessed(self):
        self.assertEqual(sdb.strategy_label_for('nonsense'), 'UNKNOWN')
        self.assertEqual(sdb.strategy_label_for(None), 'UNKNOWN')

    def test_all_labels_within_enum(self):
        allowed = {'网格', '首次部分止盈', '动态全仓止盈', '固定止损',
                   '部分止盈重挂', '手工买入', '外部-计划外', '模拟买入'}
        for code in sdb.STRATEGY_LABELS:
            self.assertIn(sdb.strategy_label_for(code), allowed)


class TestSimulationDetection(unittest.TestCase):
    def test_sim_prefix(self):
        self.assertTrue(sdb.is_simulation_trade('SIM_20260914_000001.SZ_BUY', 'default'))

    def test_simulation_strategies(self):
        for strat in ('simu', 'simu_partial', 'simu_full', 'M_simu', 'manual_simu'):
            self.assertTrue(sdb.is_simulation_trade('12345', strat))

    def test_real_trades_not_flagged(self):
        for tid, strat in (('74500104000054860480', 'external'),
                           ('ORDER_123', 'default'),
                           ('940572675', 'grid')):
            self.assertFalse(sdb.is_simulation_trade(tid, strat))


class TestRecordTrade(SettlementDBTestBase):
    """统一写入口：INSERT OR IGNORE + 唯一索引，原子幂等。"""

    def setUp(self):
        super().setUp()
        # 建出完整的扩展 schema（含唯一索引），模拟迁移后的生产库
        conn = sqlite3.connect(self.db)
        conn.execute('''CREATE TABLE IF NOT EXISTS trade_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT, stock_code TEXT, stock_name TEXT,
            trade_time TIMESTAMP, trade_type TEXT, price REAL, volume INTEGER,
            amount REAL, trade_id TEXT, commission REAL, strategy TEXT)''')
        conn.commit()
        conn.close()
        db_migrate.apply_trade_records_extension(self.db, do_backup=False,
                                                 account_override='25105132')

    def _record(self, **over):
        rec = {
            'stock_code': '300454', 'stock_name': '深信服',
            'trade_time': '2026-08-04 13:32:34', 'trade_type': 'BUY',
            'price': 122.2, 'volume': 500, 'amount': 61100.0,
            'trade_id': '74500104000054860481', 'commission': 0.0,
            'strategy': 'external', 'order_id': 1209008129,
            'deal_time': 1789353015, 'deal_time_str': '2026-09-14 10:30:15',
            'time_source': sdb.TIME_SOURCE_EXCHANGE,
        }
        rec.update(over)
        return rec

    def test_inserts_all_new_columns(self):
        self.assertEqual(sdb.record_trade(self._record(), db_path=self.db), 'inserted')
        row = self.query("SELECT * FROM trade_records")[0]
        self.assertEqual(row['account'], '25105132')
        self.assertEqual(row['deal_time'], 1789353015)
        self.assertEqual(row['deal_time_str'], '2026-09-14 10:30:15')
        self.assertEqual(row['time_source'], 'exchange')
        self.assertEqual(row['strategy_label'], '外部-计划外')
        self.assertEqual(row['order_id'], '1209008129')
        self.assertEqual(row['fills'], 1)
        self.assertEqual(row['is_simulation'], 0)
        self.assertIsNotNone(row['recorded_at'])

    def test_duplicate_is_ignored_atomically(self):
        self.assertEqual(sdb.record_trade(self._record(), db_path=self.db), 'inserted')
        self.assertEqual(sdb.record_trade(self._record(), db_path=self.db), 'duplicate')
        self.assertEqual(self.query("SELECT COUNT(*) c FROM trade_records")[0]['c'], 1,
                         "同一笔 deal 绝不能写两行")

    def test_same_deal_different_stock_both_insert(self):
        """网格 trade_id 是 order_id，跨股票复用不是重复。"""
        rec = self._record(trade_id='940572675', trade_time='2026-09-10 09:42:24')
        self.assertEqual(sdb.record_trade(rec, db_path=self.db), 'inserted')
        rec2 = dict(rec, stock_code='603757', trade_time='2026-09-11 10:20:22')
        self.assertEqual(sdb.record_trade(rec2, db_path=self.db), 'inserted')
        self.assertEqual(self.query("SELECT COUNT(*) c FROM trade_records")[0]['c'], 2)

    def test_time_source_local_fallback_when_unparsed(self):
        rec = self._record(deal_time=None, deal_time_str=None, time_source=None)
        self.assertEqual(sdb.record_trade(rec, db_path=self.db), 'inserted')
        row = self.query("SELECT time_source, deal_time FROM trade_records")[0]
        self.assertEqual(row['time_source'], 'local_fallback')
        self.assertIsNone(row['deal_time'], "拿不到成交时间就留空，不许用 now() 冒充")

    def test_commission_source_inferred(self):
        self.assertEqual(sdb.record_trade(self._record(commission=0.0), db_path=self.db),
                         'inserted')
        self.assertEqual(
            self.query("SELECT commission_source FROM trade_records")[0]['commission_source'],
            'unknown')
        rec2 = self._record(trade_id='X2', commission=12.34, commission_source='broker')
        sdb.record_trade(rec2, db_path=self.db)
        row = self.query("SELECT commission_source FROM trade_records WHERE trade_id='X2'")[0]
        self.assertEqual(row['commission_source'], 'broker')

    def test_simulation_flagged(self):
        db_migrate.migrate_settlement_schema(self.db, do_backup=False)
        rec = self._record(trade_id='SIM_20260914_000001_BUY', strategy='simu',
                           is_simulation=True)
        sdb.record_trade(rec, db_path=self.db)
        row = self.query("SELECT is_simulation, strategy_label FROM trade_records_sim")[0]
        self.assertEqual(row['is_simulation'], 1)
        self.assertEqual(row['strategy_label'], '模拟买入')

    def test_trade_id_source_classified(self):
        cases = [('ORDER_123', 'placeholder'),
                 ('940572675', 'order_id'),
                 ('74500104000054860480', 'traded_id')]
        for i, (tid, expected) in enumerate(cases):
            rec = self._record(trade_id=tid, trade_time='2026-08-0%d 13:32:34' % (i + 1))
            sdb.record_trade(rec, db_path=self.db)
            row = self.query(
                "SELECT trade_id_source FROM trade_records WHERE trade_id=?", (tid,))[0]
            self.assertEqual(row['trade_id_source'], expected)

    def test_falls_back_to_legacy_insert_without_extension(self):
        """代码先上、迁移未跑的部署窗口里，成交记录绝不能丢。

        trade_records 不可重建 —— 宁可少几个归因字段，也要把流水写进去。
        """
        fd, plain = tempfile.mkstemp(suffix='_test.db')
        os.close(fd)
        try:
            conn = sqlite3.connect(plain)
            conn.execute('''CREATE TABLE trade_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT, stock_code TEXT, stock_name TEXT,
                trade_time TIMESTAMP, trade_type TEXT, price REAL, volume INTEGER,
                amount REAL, trade_id TEXT, commission REAL, strategy TEXT)''')
            conn.commit()
            conn.close()
            # 探测结果按路径缓存，测试用独立路径天然隔离
            self.assertEqual(sdb.record_trade(self._record(), db_path=plain),
                             'legacy_inserted')
            conn = sqlite3.connect(plain)
            rows = conn.execute(
                "SELECT trade_id, stock_code, volume FROM trade_records").fetchall()
            conn.close()
            self.assertEqual(len(rows), 1, "降级路径也必须真的写进去")
            self.assertEqual(rows[0][0], '74500104000054860481')
        finally:
            for sfx in ('', '-wal', '-shm'):
                if os.path.exists(plain + sfx):
                    os.remove(plain + sfx)

    def test_missing_stock_code_fails(self):
        self.assertEqual(sdb.record_trade({'trade_id': 'X'}, db_path=self.db), 'failed')

    def test_retry_helper(self):
        self.assertEqual(
            sdb.record_trade_with_retry(self._record(), db_path=self.db), 'inserted')


    def test_simulation_trade_goes_to_separate_table(self):
        """模拟单绝不能落进实盘表 —— 归因时漏过滤 is_simulation 就会算错盈亏。"""
        db_migrate.migrate_settlement_schema(self.db, do_backup=False)
        sim = self._record(trade_id='SIM_20260914_000001_BUY', strategy='simu',
                           is_simulation=True)
        real = self._record(trade_id='74500104000054860499', strategy='external')
        self.assertEqual(sdb.record_trade(sim, db_path=self.db), 'inserted')
        self.assertEqual(sdb.record_trade(real, db_path=self.db), 'inserted')

        real_rows = self.query("SELECT trade_id FROM trade_records")
        sim_rows = self.query("SELECT trade_id, is_simulation FROM trade_records_sim")
        self.assertEqual([r['trade_id'] for r in real_rows],
                         ['74500104000054860499'], "实盘表只应有实盘")
        self.assertEqual([r['trade_id'] for r in sim_rows],
                         ['SIM_20260914_000001_BUY'], "模拟表只应有模拟")
        self.assertEqual(sim_rows[0]['is_simulation'], 1)

    def test_inferred_simulation_routed_without_explicit_flag(self):
        """没有显式 is_simulation 时，靠 SIM_ 前缀/strategy 推断也要正确分表。"""
        db_migrate.migrate_settlement_schema(self.db, do_backup=False)
        sdb.record_trade(self._record(trade_id='SIM_1', strategy='simu'), db_path=self.db)
        sdb.record_trade(self._record(trade_id='ORDER_9', strategy='default',
                                      trade_time='2026-08-09 10:00:00'), db_path=self.db)
        self.assertEqual(self.query("SELECT COUNT(*) c FROM trade_records_sim")[0]['c'], 1)
        self.assertEqual(self.query("SELECT COUNT(*) c FROM trade_records")[0]['c'], 1)


class TestDealKeyNoSilentLoss(SettlementDBTestBase):
    """deal 唯一键：重复投递要被拒，**不同成交绝不能丢**。

    回归背景：短 id（网格写入的 str(order_id)）不是全局唯一成交编号，
    实测 15 组重复 100% 跨标的。键设计不当就会把不同成交判成重复而静默丢弃。
    """

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db)
        conn.execute('''CREATE TABLE IF NOT EXISTS trade_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT, stock_code TEXT, stock_name TEXT,
            trade_time TIMESTAMP, trade_type TEXT, price REAL, volume INTEGER,
            amount REAL, trade_id TEXT, commission REAL, strategy TEXT)''')
        conn.commit()
        conn.close()
        db_migrate.migrate_settlement_schema(self.db, do_backup=False)
        db_migrate.apply_trade_records_extension(self.db, do_backup=False,
                                                 account_override='25105132')
        self.base = {
            'account': '25105132', 'stock_code': '600509', 'stock_name': '天富能源',
            'trade_time': '2026-09-11 13:00:02', 'trade_type': 'SELL',
            'price': 9.77, 'volume': 100, 'amount': 977.0,
            'trade_id': '672137217', 'order_id': '672137217', 'strategy': 'grid',
            'deal_time': None, 'deal_time_str': None,
            'time_source': sdb.TIME_SOURCE_LOCAL,
        }

    def _count(self, where='1=1', params=()):
        return self.query("SELECT COUNT(*) c FROM trade_records WHERE " + where,
                          params)[0]['c']

    def test_exact_duplicate_rejected(self):
        self.assertEqual(sdb.record_trade(dict(self.base), db_path=self.db), 'inserted')
        self.assertEqual(sdb.record_trade(dict(self.base), db_path=self.db), 'duplicate')
        self.assertEqual(self._count(), 1)

    def test_different_volume_kept(self):
        """同 id 同股同向同秒但量不同 —— 是分笔成交，必须都保留。"""
        sdb.record_trade(dict(self.base), db_path=self.db)
        self.assertEqual(
            sdb.record_trade(dict(self.base, volume=200, amount=1954.0),
                             db_path=self.db), 'inserted')
        self.assertEqual(self._count(), 2)

    def test_different_price_kept(self):
        sdb.record_trade(dict(self.base), db_path=self.db)
        self.assertEqual(
            sdb.record_trade(dict(self.base, price=9.99, amount=999.0),
                             db_path=self.db), 'inserted')
        self.assertEqual(self._count(), 2)

    def test_different_trade_type_kept(self):
        sdb.record_trade(dict(self.base), db_path=self.db)
        self.assertEqual(
            sdb.record_trade(dict(self.base, trade_type='BUY'), db_path=self.db),
            'inserted')
        self.assertEqual(self._count(), 2)

    def test_different_stock_kept(self):
        """order_id 跨标的复用 —— 不同股票必须都能写入。"""
        sdb.record_trade(dict(self.base), db_path=self.db)
        self.assertEqual(
            sdb.record_trade(dict(self.base, stock_code='001288'), db_path=self.db),
            'inserted')
        self.assertEqual(self._count(), 2)

    def test_different_order_id_kept(self):
        sdb.record_trade(dict(self.base), db_path=self.db)
        self.assertEqual(
            sdb.record_trade(dict(self.base, order_id='999999999'), db_path=self.db),
            'inserted')
        self.assertEqual(self._count(), 2)

    def test_none_commission_is_estimated_without_losing_row(self):
        sdb.record_trade(dict(self.base, commission=0.0), db_path=self.db)
        self.assertEqual(self._count(), 1)
        row = self.query("SELECT commission, commission_source FROM trade_records")[0]
        self.assertEqual(row['commission_source'], 'unknown')

    def test_repeat_delivery_across_seconds_still_rejected(self):
        """同一条 deal 隔几秒再投递（回调重复）也必须被判重。"""
        sdb.record_trade(dict(self.base), db_path=self.db)
        time.sleep(1.1)
        self.assertEqual(sdb.record_trade(dict(self.base), db_path=self.db), 'duplicate')
        self.assertEqual(self._count(), 1)

    def test_force_insert_on_true_collision_is_logged(self):
        """键冲突且内容不同时（理论边界）必须留痕，而不是静默跳过。"""
        import settlement_db
        conn = sqlite3.connect(self.db)
        # 直接构造一条与待写入行同键但内容不同的行（绕过唯一键校验不可能，
        # 故这里验证的是检测函数本身的判定）
        sdb.record_trade(dict(self.base), db_path=self.db)
        row = self.query("SELECT id, volume, price, amount, trade_type FROM trade_records")[0]
        self.assertFalse(settlement_db._is_same_deal(row, dict(self.base, volume=999)))
        self.assertTrue(settlement_db._is_same_deal(row, dict(self.base)))
        conn.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
