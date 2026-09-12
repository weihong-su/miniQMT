# -*- coding: utf-8 -*-
"""
trade_records 历史回填测试。

核心约束：**绝不把本地时间伪装成交易所成交时间**；
不确定的值写 UNKNOWN，不用"合理值"填充。
"""
import os
import shutil
import sqlite3
import tempfile
import unittest

import config
import db_migrate
import settlement_db as sdb
from scripts import backfill_trade_records as bf


class TestCommissionEstimate(unittest.TestCase):
    def test_buy_has_no_stamp_duty(self):
        expected = 10000 * (config.SETTLEMENT_COMMISSION_RATE
                            + config.SETTLEMENT_TRANSFER_FEE_RATE)
        self.assertAlmostEqual(bf.estimate_commission(10000, 'BUY'), expected, places=4)

    def test_sell_includes_stamp_duty(self):
        expected = 10000 * (config.SETTLEMENT_COMMISSION_RATE
                            + config.SETTLEMENT_TRANSFER_FEE_RATE
                            + config.SETTLEMENT_STAMP_DUTY_RATE)
        self.assertAlmostEqual(bf.estimate_commission(10000, 'SELL'), expected, places=4)

    def test_sell_costs_more_than_buy(self):
        self.assertGreater(bf.estimate_commission(10000, 'SELL'),
                           bf.estimate_commission(10000, 'BUY'))

    def test_zero_amount(self):
        self.assertEqual(bf.estimate_commission(0, 'BUY'), 0.0)
        self.assertEqual(bf.estimate_commission(None, 'SELL'), 0.0)

    def test_rate_labels(self):
        self.assertNotEqual(bf.commission_rate_label('BUY'),
                            bf.commission_rate_label('SELL'))


class TestClassifyCommission(unittest.TestCase):
    def test_zero_becomes_estimate(self):
        value, source, rate, _ = bf.classify_commission(10000, 'BUY', 0.0)
        self.assertAlmostEqual(value, bf.estimate_commission(10000, 'BUY'), places=4)
        self.assertEqual(source, 'estimated')
        self.assertIsNotNone(rate)

    def test_none_becomes_estimate(self):
        value, source, _, _ = bf.classify_commission(10000, 'BUY', None)
        self.assertGreater(value, 0)
        self.assertEqual(source, 'estimated')

    def test_legacy_rate_is_recognised_and_recomputed(self):
        """旧代码写死 amount×0.0003 —— 卖出还漏了印花税，必须重算。"""
        value, source, _, _ = bf.classify_commission(10000, 'SELL', 3.0)
        self.assertAlmostEqual(value, bf.estimate_commission(10000, 'SELL'), places=4)
        self.assertGreater(value, 3.0, "卖出应补上印花税，比旧的 0.0003 估算更高")
        self.assertEqual(source, 'estimated')

    def test_current_estimate_is_idempotent(self):
        """重复运行看到的是自己写过的值，必须仍判为 estimated 而不是 unknown。"""
        first, _, _, _ = bf.classify_commission(10000, 'BUY', 0.0)
        second, source, _, _ = bf.classify_commission(10000, 'BUY', first)
        self.assertAlmostEqual(first, second, places=6)
        self.assertEqual(source, 'estimated')

    def test_unknown_value_left_untouched(self):
        """来源说不清的金额一律不动，标 unknown —— 不猜。"""
        value, source, rate, _ = bf.classify_commission(10000, 'BUY', 7.77)
        self.assertEqual(value, 7.77)
        self.assertEqual(source, 'unknown')
        self.assertIsNone(rate)


class TestClassifyTradeIdSource(unittest.TestCase):
    def test_shapes(self):
        cases = [('ORDER_123', 'placeholder'),
                 ('SIM_20260914_X', 'sim_trade_id'),
                 ('940572675', 'order_id'),
                 ('74500104000054860480', 'traded_id')]
        for tid, expected in cases:
            with self.subTest(tid=tid):
                self.assertEqual(bf.classify_trade_id_source(tid), expected)

    def test_empty(self):
        self.assertIsNone(bf.classify_trade_id_source(None))


class BackfillFixture(unittest.TestCase):
    ACCOUNT = '25105132'

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='backfill_')
        self.acct_dir = os.path.join(self.tmp, 'data_' + self.ACCOUNT)
        os.makedirs(self.acct_dir)
        self.db = os.path.join(self.acct_dir, 'trading.db')

        conn = sqlite3.connect(self.db)
        conn.execute('''CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT, stock_code TEXT, stock_name TEXT,
            trade_time TIMESTAMP, trade_type TEXT, price REAL, volume INTEGER,
            amount REAL, trade_id TEXT, commission REAL, strategy TEXT)''')
        conn.executemany(
            "INSERT INTO trade_records(stock_code, stock_name, trade_time, "
            "trade_type, price, volume, amount, trade_id, commission, strategy) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", self.rows())
        conn.commit()
        conn.close()
        db_migrate.migrate_settlement_schema(self.db, do_backup=False)
        db_migrate.apply_trade_records_extension(
            self.db, do_backup=False, account_override=self.ACCOUNT)

    def rows(self):
        return [
            # 旧版估算的手续费（amount*0.0003）
            ('603466', '风语筑', '2026-07-09 09:32:13', 'BUY', 8.64, 1000, 8640.0,
             '72250000000003598833', 2.592, 'grid'),
            # 手续费为 0
            ('000620', '盈新发展', '2026-08-25 14:30:59', 'SELL', 5.0, 1000, 5000.0,
             '76190105000062410345', 0.0, 'stop_loss'),
            # 来源不明的非零手续费
            ('300454', '深信服', '2026-08-04 13:32:34', 'BUY', 122.2, 500, 61100.0,
             '74500104000054860480', 7.77, 'external'),
            # 映射不到的 strategy
            ('000001', '平安银行', '2026-08-04 13:32:34', 'BUY', 10.0, 100, 1000.0,
             '74500104000054860481', 0.0, '某个没见过的策略'),
        ]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def query(self, sql, params=()):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()


class TestBackfillRun(BackfillFixture):
    def test_time_source_is_local_fallback_never_exchange(self):
        """历史 trade_time 是本地入库时刻 —— 绝不能伪装成交易所成交时间。"""
        bf.run_backfill(self.db, self.ACCOUNT)
        row = self.query("SELECT DISTINCT time_source FROM trade_records")
        self.assertEqual([r['time_source'] for r in row], ['local_fallback'])

    def test_deal_time_stays_null(self):
        bf.run_backfill(self.db, self.ACCOUNT)
        self.assertEqual(self.query(
            "SELECT COUNT(*) c FROM trade_records WHERE deal_time IS NOT NULL")[0]['c'], 0)

    def test_account_backfilled(self):
        bf.run_backfill(self.db, self.ACCOUNT)
        rows = self.query("SELECT DISTINCT account FROM trade_records")
        self.assertEqual([r['account'] for r in rows], [self.ACCOUNT])

    def test_strategy_label_mapped_or_unknown(self):
        bf.run_backfill(self.db, self.ACCOUNT)
        labels = {r['strategy']: r['strategy_label'] for r in self.query(
            "SELECT strategy, strategy_label FROM trade_records")}
        self.assertEqual(labels['grid'], '网格')
        self.assertEqual(labels['stop_loss'], '固定止损')
        self.assertEqual(labels['external'], '外部-计划外')
        # 映射不到的必须写 UNKNOWN，不留空也不猜
        self.assertEqual(labels['某个没见过的策略'], 'UNKNOWN')
        self.assertEqual(self.query(
            "SELECT COUNT(*) c FROM trade_records WHERE strategy_label IS NULL")[0]['c'], 0)

    def test_no_commission_left_zero(self):
        bf.run_backfill(self.db, self.ACCOUNT)
        self.assertEqual(self.query(
            "SELECT COUNT(*) c FROM trade_records WHERE COALESCE(commission,0)=0")[0]['c'], 0)

    def test_unknown_commission_untouched(self):
        bf.run_backfill(self.db, self.ACCOUNT)
        row = self.query("SELECT commission, commission_source FROM trade_records "
                         "WHERE trade_id='74500104000054860480'")[0]
        self.assertEqual(row['commission'], 7.77)
        self.assertEqual(row['commission_source'], 'unknown')

    def test_legacy_estimate_recomputed_for_sell(self):
        """卖出的旧 0.0003 估算漏了印花税，应被重算为更高的现行费率值。"""
        bf.run_backfill(self.db, self.ACCOUNT)
        row = self.query("SELECT commission, commission_source, commission_rate "
                         "FROM trade_records WHERE trade_id='76190105000062410345'")[0]
        self.assertGreater(row['commission'], 0)
        self.assertEqual(row['commission_source'], 'estimated')
        self.assertEqual(row['commission_rate'],
                         bf.commission_rate_label('SELL'))

    def test_fills_and_fill_ids_populated(self):
        bf.run_backfill(self.db, self.ACCOUNT)
        rows = self.query("SELECT fills, fill_ids, trade_id FROM trade_records")
        for row in rows:
            self.assertEqual(row['fills'], 1)
            self.assertEqual(row['fill_ids'], row['trade_id'])

    def test_trade_id_source_classified(self):
        bf.run_backfill(self.db, self.ACCOUNT)
        self.assertEqual(self.query(
            "SELECT COUNT(*) c FROM trade_records WHERE trade_id_source IS NULL")[0]['c'], 0)

    def test_row_count_unchanged(self):
        before = self.query("SELECT COUNT(*) c FROM trade_records")[0]['c']
        bf.run_backfill(self.db, self.ACCOUNT)
        after = self.query("SELECT COUNT(*) c FROM trade_records")[0]['c']
        self.assertEqual(before, after, "回填不应增删行")

    def test_is_idempotent(self):
        bf.run_backfill(self.db, self.ACCOUNT)
        first = self.query("SELECT * FROM trade_records ORDER BY id")
        bf.run_backfill(self.db, self.ACCOUNT)
        second = self.query("SELECT * FROM trade_records ORDER BY id")
        self.assertEqual(first, second, "重复回填必须产生完全相同的结果")

    def test_dry_run_writes_nothing(self):
        before = self.query("SELECT * FROM trade_records ORDER BY id")
        result = bf.run_backfill(self.db, self.ACCOUNT, dry_run=True)
        after = self.query("SELECT * FROM trade_records ORDER BY id")
        self.assertEqual(before, after, "dry-run 不应改数据")
        self.assertIn('plan', result)
        self.assertGreater(result['plan']['strategy_label'], 0)

    def test_does_not_touch_broker_sourced_rows(self):
        """对账单导入的成果不能被回填覆盖。"""
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE trade_records SET commission_source='broker', "
                     "commission=99.99 WHERE trade_id='76190105000062410345'")
        conn.commit()
        conn.close()
        bf.run_backfill(self.db, self.ACCOUNT)
        row = self.query("SELECT commission, commission_source FROM trade_records "
                         "WHERE trade_id='76190105000062410345'")[0]
        self.assertEqual(row['commission'], 99.99)
        self.assertEqual(row['commission_source'], 'broker')

    def test_reports_missing_columns_instead_of_guessing(self):
        fd, plain = tempfile.mkstemp(suffix='_test.db')
        os.close(fd)
        try:
            conn = sqlite3.connect(plain)
            conn.execute("CREATE TABLE trade_records (id INTEGER PRIMARY KEY, "
                         "stock_code TEXT, trade_time TEXT, trade_type TEXT, "
                         "price REAL, volume REAL, amount REAL, trade_id TEXT, "
                         "commission REAL, strategy TEXT)")
            conn.commit()
            conn.close()
            result = bf.run_backfill(plain, self.ACCOUNT)
            self.assertIn('error', result)
            self.assertIn('缺少扩展列', result['error'])
        finally:
            for sfx in ('', '-wal', '-shm'):
                if os.path.exists(plain + sfx):
                    os.remove(plain + sfx)


class TestSimulationBackfill(BackfillFixture):
    """回归：迁移给 is_simulation 加了 DEFAULT 0，历史模拟单被标成实盘。

    这不只是标志错——行还留在 trade_records 里，任何不做过滤的查询
    都会把模拟成交算进实盘归因。规格要求模拟单**物理隔离**。
    """

    def rows(self):
        return [
            ('000333', '美的集团', '2026-05-15 21:35:43', 'BUY', 82.6, 400,
             33040.0, 'SIM_20260515213543_000333.SZ_BUY', 9.912, 'M_simu'),
            ('600519', '贵州茅台', '2026-05-15 21:35:43', 'BUY', 1332.98, 100,
             133298.0, 'SIM_20260515213543_600519.SH_BUY', 39.99, 'M_simu'),
            ('600509', '天富能源', '2026-07-02 09:30:20', 'SELL', 9.77, 100,
             977.0, '67213721700000000001', 0.0, 'grid'),
            ('745001', '真实成交', '2026-07-02 09:30:20', 'BUY', 10.0, 100,
             1000.0, '74500104000054860499', 0.0, 'external'),
        ]

    def test_simulated_rows_flagged_by_migration_default(self):
        """迁移后模拟单被误标成实盘 —— 这是回填要修的初始状态。"""
        rows = self.query("SELECT trade_id, is_simulation FROM trade_records "
                          "WHERE trade_id LIKE 'SIM_%'")
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row['is_simulation'], 0)

    def test_is_simulation_backfilled(self):
        bf.run_backfill(self.db, self.ACCOUNT)
        sim = self.query("SELECT COUNT(*) c FROM trade_records "
                         "WHERE COALESCE(is_simulation,0)=1")[0]['c']
        self.assertEqual(sim, 0, "迁移后不应再有模拟单留在实盘表")

    def test_simulated_rows_moved_to_separate_table(self):
        bf.run_backfill(self.db, self.ACCOUNT)
        sim_table = self.query("SELECT trade_id, strategy_label, is_simulation "
                               "FROM trade_records_sim ORDER BY id")
        self.assertEqual(len(sim_table), 2, "两笔 M_simu 应迁到独立表")
        for row in sim_table:
            self.assertEqual(row['is_simulation'], 1)
            self.assertEqual(row['strategy_label'], '模拟买入')

    def test_real_rows_stay_in_main_table(self):
        bf.run_backfill(self.db, self.ACCOUNT)
        remaining = {r['trade_id'] for r in
                     self.query("SELECT trade_id FROM trade_records")}
        self.assertIn('74500104000054860499', remaining, "真实成交必须留在实盘表")
        self.assertIn('67213721700000000001', remaining, "网格单不是模拟单")
        self.assertNotIn('SIM_20260515213543_000333.SZ_BUY', remaining)

    def test_relocation_is_idempotent(self):
        bf.run_backfill(self.db, self.ACCOUNT)
        first = self.query("SELECT * FROM trade_records_sim ORDER BY id")
        bf.run_backfill(self.db, self.ACCOUNT)
        second = self.query("SELECT * FROM trade_records_sim ORDER BY id")
        self.assertEqual(first, second, "重复回填不应重复搬迁")
        self.assertEqual(len(second), 2)

    def test_dry_run_does_not_relocate(self):
        bf.run_backfill(self.db, self.ACCOUNT, dry_run=True)
        self.assertEqual(self.query(
            "SELECT COUNT(*) c FROM trade_records_sim")[0]['c'], 0)
        self.assertEqual(self.query(
            "SELECT COUNT(*) c FROM trade_records WHERE trade_id LIKE 'SIM_%'"
        )[0]['c'], 2)

    def test_dry_run_reports_planned_relocation(self):
        result = bf.run_backfill(self.db, self.ACCOUNT, dry_run=True)
        self.assertEqual(result['plan']['is_simulation'], 2)

    def test_row_count_delta_equals_moved_rows(self):
        before = self.query("SELECT COUNT(*) c FROM trade_records")[0]['c']
        result = bf.run_backfill(self.db, self.ACCOUNT)
        after = self.query("SELECT COUNT(*) c FROM trade_records")[0]['c']
        moved = result['stats']['relocated_to_sim']
        self.assertEqual(moved, 2)
        self.assertEqual(before - after, moved, "行数减少应恰好等于迁出的模拟单数")


if __name__ == '__main__':
    unittest.main(verbosity=2)
