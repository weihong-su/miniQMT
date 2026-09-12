# -*- coding: utf-8 -*-
"""
完整数据链路端到端测试（Mock 数据）。

链路：成交回报 → 统一落库 → 持仓快照 → 券商对账单导入回填 → 导出交割单。

验证的核心承诺：**任意区间导出一次即可拿到完整交割单 + 期初/期末持仓，
且逐只股票股数闭合**。

⚠️ 本测试用 Mock 数据，**不能作为实盘验收证据** —— 它能证明解析/落库/回填/
   导出/合并各环节正确，证明不了 QMT 真的提供了 traded_time。
"""
import csv
import hashlib
import os
import shutil
import sqlite3
import tempfile
import unittest

import broker_import as bi
import config
import db_migrate
import settlement_db as sdb
from scripts import backfill_trade_records as bf
import scripts.export_settlement as ex
import scripts.import_broker_statement as imp


ACCOUNT = '25105132'
START, END = '2026-09-11', '2026-09-11'
OPEN_DATE = '2026-09-10'


class FakeXtTrade:
    """模拟 xtquant.xttype.XtTrade —— 关键是带 traded_time（Unix 秒）。"""

    def __init__(self, code, traded_time, price, volume, trade_id, order_id,
                 side='BUY', amount=None):
        self.stock_code = code
        self.traded_time = traded_time
        self.traded_price = price
        self.traded_volume = volume
        self.traded_amount = amount if amount is not None else price * volume
        self.traded_id = trade_id
        self.order_id = order_id
        self.order_type = 23 if side == 'BUY' else 24


def epoch_of(text):
    from datetime import datetime
    return int(datetime.strptime(text, '%Y-%m-%d %H:%M:%S').timestamp())


class PipelineFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pipeline_')
        self.acct_dir = os.path.join(self.tmp, 'data_' + ACCOUNT)
        os.makedirs(self.acct_dir)
        self.db = os.path.join(self.acct_dir, 'trading.db')
        self.out = os.path.join(self.tmp, 'out')
        self.stmt_dir = os.path.join(self.tmp, 'stmt')
        os.makedirs(self.out)
        os.makedirs(self.stmt_dir)

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
            self.db, do_backup=False, account_override=ACCOUNT)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- 链路各环节 ----------

    def record_deal(self, trade, strategy, commission=0.0):
        """模拟成交回报落库：走真实的时间解析 + 统一写入口。"""
        deal_time, deal_time_str = sdb.parse_deal_time(trade.traded_time)
        return sdb.record_trade({
            'account': ACCOUNT,
            'stock_code': trade.stock_code,
            'stock_name': '',
            'trade_time': deal_time_str,
            'trade_type': 'BUY' if trade.order_type == 23 else 'SELL',
            'price': trade.traded_price,
            'volume': trade.traded_volume,
            'amount': trade.traded_amount,
            'trade_id': str(trade.traded_id),
            'commission': commission,
            'strategy': strategy,
            'deal_time': deal_time,
            'deal_time_str': deal_time_str,
            'time_source': (sdb.TIME_SOURCE_EXCHANGE if deal_time_str
                            else sdb.TIME_SOURCE_LOCAL),
            'order_id': trade.order_id,
        }, db_path=self.db)

    def seed_open_snapshot(self, holdings):
        conn = sqlite3.connect(self.db)
        for code, name, volume in holdings:
            conn.execute(
                "INSERT OR REPLACE INTO position_snapshot(account, snapshot_date, "
                "snapshot_type, code, stock_name, volume, source, recorded_at) "
                "VALUES (?,?,?,?,?,?, 'memory_db', ?)",
                (ACCOUNT, OPEN_DATE, 'close', code, name, volume,
                 OPEN_DATE + ' 15:05:00'))
        conn.commit()
        conn.close()

    def seed_end_positions(self, holdings):
        conn = sqlite3.connect(self.db)
        for code, name, volume in holdings:
            conn.execute("INSERT OR REPLACE INTO positions(stock_code, stock_name, "
                         "volume) VALUES (?,?,?)", (code, name, volume))
        conn.commit()
        conn.close()

    def write_statement(self, deals):
        """写一份 GBK 编码的对账单 deals.csv（格式对齐 QMT 导出）。"""
        header = ('账号,市场,证券代码,操作,成交价格,成交数量,成交金额,手续费,成交日期,'
                  '成交时间,委托编号,成交编号,订单编号,策略名称,备注,委托类型')
        lines = [header]
        for d in deals:
            lines.append(','.join([
                '2____10064____001____49____%s____' % ACCOUNT, d['market'],
                d['code'], d['op'], '%.3f' % d['price'], str(d['volume']),
                '%.2f' % (d['price'] * d['volume']), '%.2f' % d.get('fee', 0.0),
                d['date'], d['time'], d['order_ref'], d['traded_id'],
                d['order_id'], d.get('strategy', ''), '', '48']))
        path = os.path.join(self.stmt_dir, '%s_2_deals.csv' % ACCOUNT)
        with open(path, 'w', encoding='gbk', newline='') as handle:
            handle.write('\n'.join(lines) + '\n')
        return path

    def run_export(self):
        return ex.export_account(ACCOUNT, self.db, START, END, self.out)

    def events_rows(self):
        name = [f for f in os.listdir(self.out) if f.startswith('trading_events_')][0]
        with open(os.path.join(self.out, name), encoding='utf-8') as handle:
            return list(csv.reader(handle))

    def report(self):
        return open(os.path.join(self.out, 'export_report.txt'), encoding='utf-8').read()

    def query(self, sql, params=()):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()


class TestFullChainCloses(PipelineFixture):
    """核心承诺：成交落库 → 快照 → 导出，逐只股数闭合。"""

    def _build_book(self):
        # 000620：期初 1000，买 500，卖 1500 → 期末 0
        # 603757：期初 0，   买 2000，卖 500  → 期末 1500
        self.seed_open_snapshot([('000620', '盈新发展', 1000),
                                 ('603757', '大元泵业', 0)])
        self.record_deal(FakeXtTrade('000620', epoch_of('2026-09-11 09:31:00'),
                                     5.0, 500, 'T1', 'O1', 'BUY'), 'grid')
        self.record_deal(FakeXtTrade('000620', epoch_of('2026-09-11 10:00:00'),
                                     5.5, 1500, 'T2', 'O2', 'SELL'), 'stop_loss')
        self.record_deal(FakeXtTrade('603757', epoch_of('2026-09-11 09:32:00'),
                                     70.0, 2000, 'T3', 'O3', 'BUY'), 'grid')
        self.record_deal(FakeXtTrade('603757', epoch_of('2026-09-11 14:00:00'),
                                     72.0, 500, 'T4', 'O4', 'SELL'), 'auto_full')
        self.seed_end_positions([('000620', '盈新发展', 0),
                                 ('603757', '大元泵业', 1500)])

    def test_positions_close_for_every_stock(self):
        self._build_book()
        _, code = self.run_export()
        self.assertEqual(code, 0, "有期初快照时应正常退出")
        self.assertIn('不平股票数        : 0', self.report())

    def test_report_lists_no_unbalanced_stock(self):
        self._build_book()
        self.run_export()
        report = self.report()
        self.assertIn('逐只股数闭合', report)
        self.assertIn('不平股票数        : 0', report)
        self.assertNotIn('差额', report, "闭合时不应出现差额行")

    def test_all_deals_reach_the_statement(self):
        self._build_book()
        self.run_export()
        rows = self.events_rows()
        self.assertEqual(len(rows) - 1, 4, "4 笔成交应各占一行")
        codes = {r[1] for r in rows[1:]}
        self.assertEqual(codes, {'000620', '603757'})

    def test_trade_time_is_exchange_time(self):
        """落库的必须是交易所成交时间，不是本地入库时刻。"""
        self._build_book()
        rows = self.query("SELECT DISTINCT time_source FROM trade_records")
        self.assertEqual([r['time_source'] for r in rows], ['exchange'])
        self.run_export()
        self.assertIn('time_source 分布', self.report())
        self.assertIn('exchange', self.report())

    def test_deal_time_matches_what_was_sent(self):
        self._build_book()
        rows = self.query("SELECT deal_time_str FROM trade_records "
                          "WHERE trade_id='T1'")
        self.assertEqual(rows[0]['deal_time_str'], '2026-09-11 09:31:00')

    def test_duplicate_delivery_does_not_duplicate_rows(self):
        """重复投递（底层会把同一笔推两次）由唯一索引原子挡住。"""
        self._build_book()
        before = self.query("SELECT COUNT(*) c FROM trade_records")[0]['c']
        self.record_deal(FakeXtTrade('000620', epoch_of('2026-09-11 09:31:00'),
                                     5.0, 500, 'T1', 'O1', 'BUY'), 'grid')
        after = self.query("SELECT COUNT(*) c FROM trade_records")[0]['c']
        self.assertEqual(before, after)

    def test_simulation_does_not_pollute_real_table(self):
        self._build_book()
        sdb.record_trade({
            'account': ACCOUNT, 'stock_code': '000620', 'trade_name': '',
            'trade_time': '2026-09-11 09:31:00', 'trade_type': 'BUY',
            'price': 5.0, 'volume': 100, 'amount': 500.0,
            'trade_id': 'SIM_1', 'strategy': 'simu', 'is_simulation': True,
        }, db_path=self.db)
        real = self.query("SELECT COUNT(*) c FROM trade_records")[0]['c']
        sim = self.query("SELECT COUNT(*) c FROM trade_records_sim")[0]['c']
        self.assertEqual(sim, 1)
        self.assertEqual(real, 4, "模拟单不得进入实盘表")

    def test_export_is_idempotent(self):
        self._build_book()
        path1, _ = self.run_export()
        h1 = hashlib.sha256(open(path1, 'rb').read()).hexdigest()
        path2, _ = self.run_export()
        h2 = hashlib.sha256(open(path2, 'rb').read()).hexdigest()
        self.assertEqual(h1, h2)


class TestBrokerImportChain(PipelineFixture):
    """对账单导入 → 回填真实成交时间 → 导出反映回填结果。"""

    def _seed_with_wrong_local_time(self):
        """先按 local_fallback 落一笔，时间故意比真实成交晚 2 秒。"""
        self.seed_open_snapshot([('000620', '盈新发展', 1000)])
        sdb.record_trade({
            'account': ACCOUNT, 'stock_code': '000620', 'stock_name': '盈新发展',
            'trade_time': '2026-09-11 13:00:02',   # 本地入库时刻（错）
            'trade_type': 'SELL', 'price': 5.5, 'volume': 1500, 'amount': 8250.0,
            'trade_id': '940572733',
            'commission': 6.68, 'strategy': 'stop_loss',
            'time_source': sdb.TIME_SOURCE_LOCAL,
        }, db_path=self.db)
        self.seed_end_positions([('000620', '盈新发展', -500)])  # 故意不平，稍后验证

    def test_import_backfills_real_deal_time(self):
        self._seed_with_wrong_local_time()
        self.write_statement([{
            'market': 'SZ', 'code': '000620', 'op': '限价卖出', 'price': 5.5,
            'volume': 1500, 'date': '20260911', 'time': '130000',   # 真实成交时间
            'order_ref': '8129', 'traded_id': '83640102000038433071',
            'order_id': '940572733', 'strategy': 'stop_loss', 'fee': 0.0,
        }])

        files = imp.discover_statement_files(self.stmt_dir)
        self.assertIn(ACCOUNT, files)
        stats = imp.process_account(ACCOUNT, files[ACCOUNT], self.db)
        self.assertEqual(stats['matched'], 1)
        self.assertEqual(stats['unmatched'], 0)

        row = self.query("SELECT deal_time_str, time_source, trade_id, "
                         "trade_id_source FROM trade_records")[0]
        self.assertEqual(row['deal_time_str'], '2026-09-11 13:00:00',
                         "应以对账单的真实成交时间为准")
        self.assertEqual(row['time_source'], 'broker')
        self.assertEqual(row['trade_id'], '83640102000038433071',
                         "order_id 应被改写为真实成交编号")
        self.assertEqual(row['trade_id_source'], 'traded_id')

    def test_zero_broker_fee_keeps_estimate(self):
        """对账单手续费为 0 时不覆盖本地估算 —— 否则会更差。"""
        self._seed_with_wrong_local_time()
        self.write_statement([{
            'market': 'SZ', 'code': '000620', 'op': '限价卖出', 'price': 5.5,
            'volume': 1500, 'date': '20260911', 'time': '130000',
            'order_ref': '8129', 'traded_id': '83640102000038433071',
            'order_id': '940572733', 'strategy': 'stop_loss', 'fee': 0.0,
        }])
        files = imp.discover_statement_files(self.stmt_dir)
        imp.process_account(ACCOUNT, files[ACCOUNT], self.db)
        row = self.query("SELECT commission FROM trade_records")[0]
        self.assertAlmostEqual(row['commission'], 6.68)

    def test_import_writes_broker_deals_audit_rows(self):
        """本地 trade_id 是 order_id 形态时走二级匹配（订单号+代码）。"""
        self._seed_with_wrong_local_time()
        self.write_statement([{
            'market': 'SZ', 'code': '000620', 'op': '限价卖出', 'price': 5.5,
            'volume': 1500, 'date': '20260911', 'time': '130000',
            'order_ref': '8129', 'traded_id': '83640102000038433071',
            'order_id': '940572733', 'strategy': 'stop_loss', 'fee': 0.0,
        }])
        files = imp.discover_statement_files(self.stmt_dir)
        imp.process_account(ACCOUNT, files[ACCOUNT], self.db)
        rows = self.query("SELECT * FROM broker_deals")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['match_status'], 'matched')
        self.assertEqual(rows[0]['match_method'], bi.MATCH_BY_ORDER_ID)

    def test_matches_by_traded_id_when_local_already_has_it(self):
        """本地 trade_id 已是真实成交编号时走一级匹配（成交编号）。"""
        self.seed_open_snapshot([('000620', '盈新发展', 1000)])
        sdb.record_trade({
            'account': ACCOUNT, 'stock_code': '000620', 'stock_name': '盈新发展',
            'trade_time': '2026-09-11 13:00:00', 'trade_type': 'SELL',
            'price': 5.5, 'volume': 1500, 'amount': 8250.0,
            'trade_id': '83640102000038433071', 'commission': 6.68,
            'strategy': 'stop_loss', 'time_source': sdb.TIME_SOURCE_LOCAL,
        }, db_path=self.db)
        self.seed_end_positions([('000620', '盈新发展', -500)])
        self.write_statement([{
            'market': 'SZ', 'code': '000620', 'op': '限价卖出', 'price': 5.5,
            'volume': 1500, 'date': '20260911', 'time': '130000',
            'order_ref': '8129', 'traded_id': '83640102000038433071',
            'order_id': '940572733', 'strategy': 'stop_loss', 'fee': 0.0,
        }])
        files = imp.discover_statement_files(self.stmt_dir)
        imp.process_account(ACCOUNT, files[ACCOUNT], self.db)
        rows = self.query("SELECT match_method FROM broker_deals")
        self.assertEqual(rows[0]['match_method'], bi.MATCH_BY_TRADED_ID)

    def test_import_is_idempotent(self):
        self._seed_with_wrong_local_time()
        self.write_statement([{
            'market': 'SZ', 'code': '000620', 'op': '限价卖出', 'price': 5.5,
            'volume': 1500, 'date': '20260911', 'time': '130000',
            'order_ref': '8129', 'traded_id': '83640102000038433071',
            'order_id': '940572733', 'strategy': 'stop_loss', 'fee': 0.0,
        }])
        files = imp.discover_statement_files(self.stmt_dir)
        imp.process_account(ACCOUNT, files[ACCOUNT], self.db)
        first = self.query("SELECT * FROM trade_records ORDER BY id")
        imp.process_account(ACCOUNT, files[ACCOUNT], self.db)
        second = self.query("SELECT * FROM trade_records ORDER BY id")
        self.assertEqual(first, second)
        self.assertEqual(len(self.query("SELECT * FROM broker_deals")), 1,
                         "重复导入不应产生重复 broker_deals 行")

    def test_unmatched_deal_recorded_not_silently_dropped(self):
        self.seed_open_snapshot([('000620', '盈新发展', 1000)])
        self.seed_end_positions([('000620', '盈新发展', 1000)])
        self.write_statement([{
            'market': 'SZ', 'code': '600000', 'op': '限价买入', 'price': 10.0,
            'volume': 999, 'date': '20260911', 'time': '100000',
            'order_ref': '1', 'traded_id': 'NO_SUCH_DEAL', 'order_id': 'NO_SUCH_ORDER',
            'strategy': '', 'fee': 0.0,
        }])
        files = imp.discover_statement_files(self.stmt_dir)
        stats = imp.process_account(ACCOUNT, files[ACCOUNT], self.db)
        self.assertEqual(stats['matched'], 0)
        self.assertEqual(stats['unmatched'], 1)
        statuses = [r['match_status'] for r in
                    self.query("SELECT match_status FROM broker_deals")]
        self.assertEqual(statuses, ['no_local_record'])


class TestBackfillThenExport(PipelineFixture):
    """历史回填 → 导入对账单 → 导出，三者串起来不互相破坏。"""

    def test_full_sequence(self):
        # 1) 历史遗留：本地时间落库、无来源标注
        self.seed_open_snapshot([('000620', '盈新发展', 1000)])
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO trade_records(stock_code, stock_name, trade_time, "
            "trade_type, price, volume, amount, trade_id, commission, strategy) "
            "VALUES ('000620','盈新发展','2026-09-11 13:00:02','SELL',5.5,1500,"
            "8250.0,'940572733',2.475,'stop_loss')")
        conn.commit()
        conn.close()
        self.seed_end_positions([('000620', '盈新发展', -500)])

        # 2) 历史回填：标 local_fallback，绝不伪装 exchange
        bf.run_backfill(self.db, ACCOUNT)
        row = self.query("SELECT time_source, commission_source, strategy_label "
                         "FROM trade_records")[0]
        self.assertEqual(row['time_source'], 'local_fallback')
        self.assertEqual(row['commission_source'], 'estimated')
        self.assertEqual(row['strategy_label'], '固定止损')

        # 3) 导入对账单：升级为 broker + 真实成交时间
        self.write_statement([{
            'market': 'SZ', 'code': '000620', 'op': '限价卖出', 'price': 5.5,
            'volume': 1500, 'date': '20260911', 'time': '130000',
            'order_ref': '8129', 'traded_id': '83640102000038433071',
            'order_id': '940572733', 'strategy': 'stop_loss', 'fee': 0.0,
        }])
        files = imp.discover_statement_files(self.stmt_dir)
        imp.process_account(ACCOUNT, files[ACCOUNT], self.db)
        row = self.query("SELECT time_source, deal_time_str, commission_source "
                         "FROM trade_records")[0]
        self.assertEqual(row['time_source'], 'broker')
        self.assertEqual(row['deal_time_str'], '2026-09-11 13:00:00')
        self.assertEqual(row['commission_source'], 'estimated',
                         "对账单手续费为 0，保留估算来源")

        # 4) 回填再跑一次，不应覆盖 broker 成果
        bf.run_backfill(self.db, ACCOUNT)
        row = self.query("SELECT time_source, commission_source FROM trade_records")[0]
        self.assertEqual(row['time_source'], 'broker')
        self.assertEqual(row['commission_source'], 'estimated')

        # 5) 导出：真实成交时间生效
        self.run_export()
        rows = self.events_rows()
        self.assertEqual(rows[1][3], '2026-09-11 13:00:00')
        self.assertEqual(rows[1][11], '83640102000038433071')


if __name__ == '__main__':
    unittest.main(verbosity=2)
