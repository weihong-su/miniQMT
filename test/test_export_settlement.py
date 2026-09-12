# -*- coding: utf-8 -*-
"""
交割单导出测试：合并规则、14 列结构、幂等、缺快照报错。

全部在临时库上跑，不碰生产库。
"""
import csv
import os
import shutil
import sqlite3
import tempfile
import unittest

import db_migrate
import settlement_db as sdb
from scripts import export_settlement as ex


def make_row(code='000620', ts='2026-08-25 14:30:59', side='BUY', strategy='external',
             volume=100, amount=1000.0, trade_id='T1', commission=1.0,
             is_simulation=False, account='A'):
    return {
        'account': account, 'code': code, 'stock_name': '测试股',
        'trade_time': ts, 'trade_type': side, 'strategy': strategy,
        'strategy_label': sdb.strategy_label_for(strategy),
        'is_simulation': is_simulation, 'volume': volume, 'amount': amount,
        'commission': commission, 'trade_id': trade_id,
    }


class TestMergeDeals(unittest.TestCase):
    def test_merges_within_window(self):
        rows = [
            make_row(ts='2026-08-25 14:30:00', volume=100, amount=1000.0, trade_id='A'),
            make_row(ts='2026-08-25 14:30:05', volume=200, amount=2000.0, trade_id='B'),
            make_row(ts='2026-08-25 14:30:09', volume=300, amount=3000.0, trade_id='C'),
        ]
        merged = ex.merge_deals(rows)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['volume'], 600)
        self.assertEqual(merged[0]['amount'], 6000.0)
        self.assertEqual(merged[0]['fills'], 3)
        self.assertEqual(merged[0]['trade_ids'], ['A', 'B', 'C'])

    def test_does_not_merge_beyond_window(self):
        rows = [
            make_row(ts='2026-08-25 14:30:00', trade_id='A'),
            make_row(ts='2026-08-25 14:30:11', trade_id='B'),   # 超过 10 秒
        ]
        self.assertEqual(len(ex.merge_deals(rows)), 2)

    def test_never_merges_across_day_boundary(self):
        """隔夜的两笔同向成交即使间隔够近，也是两笔独立交易。"""
        rows = [
            make_row(ts='2026-08-25 23:59:58', trade_id='A'),
            make_row(ts='2026-08-26 00:00:02', trade_id='B'),   # 间隔 4 秒但跨日
        ]
        self.assertEqual(len(ex.merge_deals(rows)), 2)

    def test_different_stock_not_merged(self):
        rows = [make_row(code='000620', trade_id='A'),
                make_row(code='000621', trade_id='B')]
        self.assertEqual(len(ex.merge_deals(rows)), 2)

    def test_different_side_not_merged(self):
        rows = [make_row(side='BUY', trade_id='A'),
                make_row(side='SELL', trade_id='B')]
        self.assertEqual(len(ex.merge_deals(rows)), 2)

    def test_different_strategy_not_merged(self):
        rows = [make_row(strategy='grid', trade_id='A'),
                make_row(strategy='stop_loss', trade_id='B')]
        self.assertEqual(len(ex.merge_deals(rows)), 2)

    def test_output_sorted_by_time(self):
        rows = [make_row(ts='2026-08-25 14:31:00', trade_id='B'),
                make_row(ts='2026-08-25 14:30:00', trade_id='A')]
        merged = ex.merge_deals(rows)
        self.assertEqual([r['trade_time'] for r in merged],
                         ['2026-08-25 14:30:00', '2026-08-25 14:31:00'])

    def test_merge_is_deterministic(self):
        """同样的输入必须产出完全一致的输出（导出幂等的前提）。"""
        rows = [make_row(ts=f'2026-08-25 14:30:0{i}', trade_id=f'T{i}')
                for i in range(5)]
        first = ex.merge_deals(list(rows))
        second = ex.merge_deals(list(reversed(rows)))
        self.assertEqual([r['volume'] for r in first], [r['volume'] for r in second])
        self.assertEqual([r['trade_ids'] for r in first], [r['trade_ids'] for r in second])

    def test_commission_summed_on_merge(self):
        rows = [make_row(ts='2026-08-25 14:30:00', commission=1.5, trade_id='A'),
                make_row(ts='2026-08-25 14:30:03', commission=2.5, trade_id='B')]
        self.assertAlmostEqual(ex.merge_deals(rows)[0]['commission'], 4.0)

    def test_max_merge_span_reported(self):
        rows = [make_row(ts='2026-08-25 14:30:00', trade_id='A'),
                make_row(ts='2026-08-25 14:30:08', trade_id='B')]
        self.assertAlmostEqual(ex.max_merge_span(ex.merge_deals(rows)), 8.0)

    def test_empty_input(self):
        self.assertEqual(ex.merge_deals([]), [])


class TestCodeNorm(unittest.TestCase):
    def test_strips_suffixes_and_keeps_leading_zeros(self):
        for raw, expected in (('000620.SZ', '000620'), ('sh600519', '600519'),
                              ('600519.SH', '600519'), ('301085', '301085')):
            self.assertEqual(ex.norm_code(raw), expected)

    def test_never_returns_int(self):
        self.assertIsInstance(ex.norm_code('000620'), str)


class ExportFixture(unittest.TestCase):
    """带扩展 schema + 快照的临时库。"""

    ACCOUNT = '25105132'

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='export_')
        self.acct_dir = os.path.join(self.tmp, 'data_' + self.ACCOUNT)
        os.makedirs(self.acct_dir)
        self.db = os.path.join(self.acct_dir, 'trading.db')
        self.out = os.path.join(self.tmp, 'out')
        os.makedirs(self.out)

        conn = sqlite3.connect(self.db)
        conn.execute('''CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT, stock_code TEXT, stock_name TEXT,
            trade_time TIMESTAMP, trade_type TEXT, price REAL, volume INTEGER,
            amount REAL, trade_id TEXT, commission REAL, strategy TEXT)''')
        conn.execute('''CREATE TABLE positions (
            stock_code TEXT PRIMARY KEY, stock_name TEXT, volume REAL,
            available REAL, cost_price REAL)''')
        conn.commit()
        conn.close()
        db_migrate.migrate_settlement_schema(self.db, do_backup=False)
        db_migrate.apply_trade_records_extension(
            self.db, do_backup=False, account_override=self.ACCOUNT)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_trade(self, **kw):
        conn = sqlite3.connect(self.db)
        rec = {
            'stock_code': '000620', 'stock_name': '盈新发展',
            'trade_time': '2026-08-25 14:30:59', 'trade_type': 'BUY',
            'price': 5.0, 'volume': 100, 'amount': 500.0, 'trade_id': 'T1',
            'commission': 0.15, 'strategy': 'external',
            'account': self.ACCOUNT, 'deal_time_str': '2026-08-25 14:30:59',
            'time_source': 'exchange',
        }
        rec.update(kw)
        conn.close()
        sdb.record_trade(rec, db_path=self.db)

    def add_snapshot(self, date, code, volume, snap_type='close'):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT OR REPLACE INTO position_snapshot(account, snapshot_date, "
            "snapshot_type, code, stock_name, volume, source, recorded_at) "
            "VALUES (?,?,?,?,?,?,'memory_db','2026-08-25 09:25:00')",
            (self.ACCOUNT, date, snap_type, code, '测试股', volume))
        conn.commit()
        conn.close()

    def read_csv(self, name):
        path = os.path.join(self.out, name)
        with open(path, encoding='utf-8') as handle:
            return list(csv.reader(handle))

    def run_export(self, start='2026-08-25', end='2026-08-31'):
        return ex.export_account(self.ACCOUNT, self.db, start, end, self.out)


class TestExportOutput(ExportFixture):
    def test_events_has_14_contract_columns_then_diagnostics(self):
        """前 14 列是固定契约，诊断列追加在后面（按列名读取，不影响分析）。"""
        self.add_trade()
        self.run_export()
        files = [f for f in os.listdir(self.out) if f.startswith('trading_events_')]
        self.assertEqual(len(files), 1)
        rows = self.read_csv(files[0])
        contract = ['account', 'code', 'stock_name', 'trade_time', 'trade_type',
                    'strategy', 'is_simulation', 'volume', 'amount', 'commission',
                    'fills', 'trade_ids', 'price', 'strategy_label']
        self.assertEqual(rows[0][:14], contract, "前 14 列必须逐字一致且顺序不变")
        self.assertEqual(rows[0][14:], ['time_source', 'order_id', 'row_status'])

    def test_account_is_masked(self):
        self.add_trade()
        self.run_export()
        files = [f for f in os.listdir(self.out) if f.startswith('trading_events_')]
        rows = self.read_csv(files[0])
        self.assertEqual(rows[1][0], '账户A(***5132)')

    def test_report_has_account_mapping_table(self):
        self.add_trade()
        self.run_export()
        report = open(os.path.join(self.out, 'export_report.txt'),
                      encoding='utf-8').read()
        self.assertIn('账号映射表', report)
        self.assertIn('账户A(***5132)', report)
        self.assertIn('25105132', report)

    def test_report_shows_actual_range(self):
        self.add_trade()   # 唯一成交在 2026-08-25
        self.run_export(start='2026-08-01', end='2026-08-31')
        report = open(os.path.join(self.out, 'export_report.txt'),
                      encoding='utf-8').read()
        self.assertIn('实际首末成交', report)
        self.assertIn('2026-08-25', report)

    def test_report_has_trade_id_uniqueness_check(self):
        self.add_trade()
        self.run_export()
        report = open(os.path.join(self.out, 'export_report.txt'),
                      encoding='utf-8').read()
        self.assertIn('trade_id 唯一性检查', report)
        self.assertIn('跨标的重复组', report)
        self.assertIn('当前唯一键下冲突组', report)

    def test_report_has_commission_source_distribution(self):
        self.add_trade()
        self.run_export()
        report = open(os.path.join(self.out, 'export_report.txt'),
                      encoding='utf-8').read()
        self.assertIn('commission_source 分布', report)
        self.assertNotIn('见数据库统计', report, "必须实打实打印分布")

    def test_changelog_written(self):
        self.add_trade()
        self.run_export()
        self.assertIn('changelog_since_last_export.txt', os.listdir(self.out))

    def test_changelog_detects_modification_on_second_run(self):
        self.add_trade()
        self.run_export()
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE trade_records SET commission=99.99, "
                     "commission_source='broker' WHERE trade_id='T1'")
        conn.commit()
        conn.close()
        self.run_export()
        log = open(os.path.join(self.out, 'changelog_since_last_export.txt'),
                   encoding='utf-8').read()
        self.assertIn('修改 1 行', log)
        self.assertIn('commission', log)
        self.assertIn('→', log)

    def test_filename_uses_actual_range(self):
        """文件名必须反映**实际**首末成交，不是请求区间。"""
        self.add_snapshot('2026-08-22', '000620', 0)
        self.add_trade(trade_time='2026-08-25 14:30:59', trade_id='A')
        self.run_export(start='2026-02-01', end='2026-08-31')
        files = [f for f in os.listdir(self.out) if f.startswith('trading_events_')]
        self.assertEqual(files[0], 'trading_events_20260825_20260825.csv')

    def test_all_five_outputs_written(self):
        self.add_trade()
        self.run_export()
        names = set(os.listdir(self.out))
        self.assertIn('positions_begin.csv', names)
        self.assertIn('positions_end.csv', names)
        self.assertIn('account_daily.csv', names)
        self.assertIn('cash_flows.csv', names)
        self.assertIn('export_report.txt', names)

    def test_no_stock_is_silently_dropped(self):
        """规格明令禁止导出时剔除股票 —— 与上一轮「只导完整的」相反。"""
        self.add_trade(stock_code='000620', trade_id='A')
        self.add_trade(stock_code='301085', trade_id='B')
        merged = ex.merge_deals([
            make_row(code='000620', trade_id='A'),
            make_row(code='301085', trade_id='B'),
        ])
        self.assertEqual(len(merged), 2)

    def test_unbalanced_stock_listed_in_report(self):
        self.add_snapshot('2026-08-22', '000620', 0)
        self.add_trade(stock_code='000620', volume=100, trade_id='A')
        # positions 表里放一个对不上的数
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO positions(stock_code, stock_name, volume) "
                     "VALUES ('000620','盈新发展', 999)")
        conn.commit()
        conn.close()
        self.run_export()
        report = open(os.path.join(self.out, 'export_report.txt'), encoding='utf-8').read()
        self.assertIn('不平股票数', report)
        self.assertIn('000620', report)
        self.assertIn('差额', report)

    def test_leading_zero_code_preserved(self):
        self.add_snapshot('2026-08-22', '000620', 0)
        self.add_trade(stock_code='000620', trade_id='A')
        self.run_export()
        files = [f for f in os.listdir(self.out) if f.startswith('trading_events_')]
        rows = self.read_csv(files[0])
        self.assertEqual(rows[1][1], '000620')
        self.assertIsInstance(rows[1][1], str)

    def test_report_contains_required_sections(self):
        self.add_trade()
        self.run_export()
        report = open(os.path.join(self.out, 'export_report.txt'), encoding='utf-8').read()
        for section in ('总量', '合并参数', '时间自检', 'time_source 分布', '代码自检',
                        '数值自检', '枚举自检', '逐只股数闭合'):
            self.assertIn(section, report, f"报告缺少「{section}」章节")

    def test_report_states_real_source_not_fabricated(self):
        """来源必须如实标注 —— 上次标 runtime_log(not_db) 是诚实做法，继续保持。"""
        self.add_trade()
        self.run_export()
        report = open(os.path.join(self.out, 'export_report.txt'), encoding='utf-8').read()
        self.assertIn('只读', report)
        self.assertIn('不依赖 logs', report)


class TestMissingBeginSnapshot(ExportFixture):
    def test_errors_out_without_begin_snapshot(self):
        """没有期初快照必须报错退出，不许用 0 填充。"""
        self.add_trade()
        _, code = self.run_export()
        self.assertEqual(code, 2, "缺期初快照应返回非零退出码")

    def test_begin_csv_writes_blocker_not_silent_empty(self):
        """无期初快照时必须显式写 BLOCKER 行，不能静默输出只有表头的空文件。"""
        self.add_trade()
        self.run_export()
        rows = self.read_csv('positions_begin.csv')
        self.assertEqual(rows[0], ['account', 'code', 'stock_name', 'volume',
                                   'cost_price', 'source'])
        self.assertEqual(len(rows), 2, "应有一行 BLOCKER")
        self.assertIn('BLOCKER', rows[1][0])
        self.assertIn('no snapshot before', rows[1][0])

    def test_begin_csv_has_cost_price_column(self):
        self.add_snapshot('2026-08-22', '000620', 300)
        self.add_trade()
        self.run_export()
        rows = self.read_csv('positions_begin.csv')
        self.assertEqual(rows[0], ['account', 'code', 'stock_name', 'volume',
                                   'cost_price', 'source'])

    def test_succeeds_when_snapshot_present(self):
        self.add_snapshot('2026-08-22', '000620', 0)
        self.add_trade()
        _, code = self.run_export()
        self.assertEqual(code, 0)

    def test_uses_last_snapshot_before_start(self):
        self.add_snapshot('2026-08-20', '000620', 500)
        self.add_snapshot('2026-08-22', '000620', 300)   # 更近
        self.add_snapshot('2026-08-26', '000620', 100)   # 在区间内，不该用
        self.add_trade()
        self.run_export()
        rows = self.read_csv('positions_begin.csv')
        self.assertEqual(rows[1][3], '300')


class TestIdempotence(ExportFixture):
    def test_two_runs_produce_identical_sha256(self):
        self.add_snapshot('2026-08-22', '000620', 0)
        self.add_trade(trade_id='A')
        self.add_trade(trade_id='B', trade_time='2026-08-26 10:00:00')
        path1, _ = self.run_export()
        digest1 = ex.sha256_of(path1)
        path2, _ = self.run_export()
        digest2 = ex.sha256_of(path2)
        self.assertEqual(digest1, digest2, "同区间连跑两次 sha256 必须一致")


class TestUnmigratedDatabase(unittest.TestCase):
    """未迁移的库也必须能导出（只是字段少），不能直接崩。

    回归：曾经在 `account` 列不存在时仍往 params 里塞了参数，
    导致 "Incorrect number of bindings supplied"。这个 bug 只在
    未迁移的库上才暴露 —— 所有测试都跑在已迁移库上就会漏掉。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='export_raw_')
        self.db = os.path.join(self.tmp, 'trading.db')
        self.out = os.path.join(self.tmp, 'out')
        os.makedirs(self.out)
        conn = sqlite3.connect(self.db)
        # 改造前的原始 schema：没有 account / row_status / deal_time_str
        conn.execute('''CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT, stock_code TEXT, stock_name TEXT,
            trade_time TIMESTAMP, trade_type TEXT, price REAL, volume INTEGER,
            amount REAL, trade_id TEXT, commission REAL, strategy TEXT)''')
        conn.execute('''CREATE TABLE positions (
            stock_code TEXT PRIMARY KEY, stock_name TEXT, volume REAL)''')
        conn.execute(
            "INSERT INTO trade_records(stock_code, stock_name, trade_time, "
            "trade_type, price, volume, amount, trade_id, commission, strategy) "
            "VALUES ('000620','盈新发展','2026-08-25 14:30:59','BUY',5.0,100,"
            "500.0,'T1',0.15,'external')")
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_export_does_not_crash_without_account_column(self):
        path, code = ex.export_account('25105132', self.db,
                                       '2026-08-01', '2026-08-31', self.out)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(code, 2, "无期初快照仍应返回 2")

    def test_rows_still_exported(self):
        ex.export_account('25105132', self.db, '2026-08-01', '2026-08-31', self.out)
        name = [f for f in os.listdir(self.out) if f.startswith('trading_events_')][0]
        with open(os.path.join(self.out, name), encoding='utf-8') as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(len(rows) - 1, 1)
        self.assertEqual(rows[1][1], '000620')
        # 未迁移库没有 strategy_label 列，应由映射补出
        self.assertEqual(rows[1][13], '外部-计划外')

    def test_report_notes_missing_time_source(self):
        ex.export_account('25105132', self.db, '2026-08-01', '2026-08-31', self.out)
        report = open(os.path.join(self.out, 'export_report.txt'),
                      encoding='utf-8').read()
        self.assertIn('尚未扩展', report)


class TestTimeSourceMessage(ExportFixture):
    """「列不存在」与「区间内无成交」含义完全不同，措辞不能混为一谈。

    回归：data/trading.db 已迁移（28 列），但区间内无成交，
    报告却写"trade_records 尚未扩展" —— 会被读成"迁移没跑"。
    """

    def test_no_rows_in_range_says_no_trades(self):
        # 已迁移，但区间内没有任何成交
        self.run_export(start='2026-01-01', end='2026-01-31')
        report = open(os.path.join(self.out, 'export_report.txt'),
                      encoding='utf-8').read()
        self.assertIn('区间内无成交记录', report)
        self.assertNotIn('尚未扩展', report)

    def test_unmigrated_says_needs_migration(self):
        """未迁移的库必须明确提示先跑迁移，而不是含糊其辞。"""
        plain = os.path.join(self.tmp, 'raw', 'data_25105132', 'trading.db')
        os.makedirs(os.path.dirname(plain))
        conn = sqlite3.connect(plain)
        conn.execute('''CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT, stock_code TEXT, stock_name TEXT,
            trade_time TIMESTAMP, trade_type TEXT, price REAL, volume INTEGER,
            amount REAL, trade_id TEXT, commission REAL, strategy TEXT)''')
        conn.commit()
        conn.close()
        conn = sqlite3.connect(plain)
        conn.execute("CREATE TABLE positions (stock_code TEXT PRIMARY KEY, "
                     "stock_name TEXT, volume REAL)")
        conn.commit()
        conn.close()
        out = os.path.join(self.tmp, 'out_raw')
        os.makedirs(out, exist_ok=True)
        ex.export_account('25105132', plain, '2026-08-01', '2026-08-31', out)
        report = open(os.path.join(out, 'export_report.txt'), encoding='utf-8').read()
        self.assertIn('尚未扩展', report)
        self.assertIn('请先执行数据库迁移', report)

    def test_missing_positions_table_does_not_crash(self):
        """positions 缺失应降级为"期末无持仓"，而不是中断整次导出。"""
        plain = os.path.join(self.tmp, 'nopos', 'data_25105132', 'trading.db')
        os.makedirs(os.path.dirname(plain))
        conn = sqlite3.connect(plain)
        conn.execute('''CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT, stock_code TEXT, stock_name TEXT,
            trade_time TIMESTAMP, trade_type TEXT, price REAL, volume INTEGER,
            amount REAL, trade_id TEXT, commission REAL, strategy TEXT)''')
        conn.execute("INSERT INTO trade_records(stock_code, stock_name, trade_time, "
                     "trade_type, price, volume, amount, trade_id, strategy) "
                     "VALUES ('000620','盈新发展','2026-08-25 14:30:59','BUY',5.0,100,"
                     "500.0,'T1','grid')")
        conn.commit()
        conn.close()
        out = os.path.join(self.tmp, 'out_nopos')
        os.makedirs(out, exist_ok=True)
        path, code = ex.export_account('25105132', plain, '2026-08-01',
                                       '2026-08-31', out)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(code, 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
