# -*- coding: utf-8 -*-
"""
券商对账单导入测试：解析 / 匹配 / 回填决策。

用真实对账单的**格式与取值**构造样例（含 5 位 HHMMSS、GBK 编码、
账号串分节等容易踩的细节），不依赖外部文件。
"""
import io
import os
import shutil
import sqlite3
import tempfile
import unittest

import broker_import as bi


# 真实对账单的表头（逐字复制）
DEALS_HEADER = ('账号,市场,证券代码,操作,成交价格,成交数量,成交金额,手续费,成交日期,'
                '成交时间,委托编号,成交编号,订单编号,策略名称,备注,委托类型')
ORDERS_HEADER = ('账号,市场,证券代码,操作,报价类型,委托价格,委托数量,委托日期,委托时间,'
                 '委托编号,委托状态,成交数量,成交均价,成交金额,撤单数量,订单编号,'
                 '策略名称,备注,废单原因,委托类型')
FUNDFLOW_HEADER = ('成功,返回信息,账号信息,发生日期,发生时间,流水序号,业务类型,业务名称,'
                   '发生数量,剩余数量,币种,交易市场类别,资金账号,证券代码,定位串,证券名称,'
                   '成交价格,成交金额,手续费,印花税,过户费,其他费用,发生数量/金额,'
                   '剩余数量/金额,委托价格,委托数量,股东账号,成交序列号,委托号,买卖方向')
DELIVERY_HEADER = ('成功,错误,日期,成交时间,市场类型,市场,股票账号,证券代码,证券名称,'
                   '买卖,操作,成交数量,成交价格,成交金额,资金余额,股份余额,成交序号,'
                   '手续费,印花税,其它杂费,委托号,资金账号,业务类型,业务名称,记录序列号')

ACCOUNT_CELL = '2____10064____001____49____25105132____'


def write_csv(path, header, rows, encoding='gbk'):
    with open(path, 'w', encoding=encoding, newline='') as handle:
        handle.write(header + '\n')
        for row in rows:
            handle.write(row + '\n')


class TestReadCsv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='broker_')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reads_gbk(self):
        path = os.path.join(self.tmp, 'a.csv')
        write_csv(path, '名称,数量', ['贵州茅台,100'], encoding='gbk')
        rows, enc = bi.read_csv_rows(path)
        self.assertEqual(rows[0]['名称'], '贵州茅台')
        self.assertEqual(rows[0]['数量'], '100')

    def test_reads_utf8(self):
        path = os.path.join(self.tmp, 'b.csv')
        write_csv(path, '名称,数量', ['贵州茅台,100'], encoding='utf-8')
        rows, _ = bi.read_csv_rows(path)
        self.assertEqual(rows[0]['名称'], '贵州茅台')

    def test_header_only_file_gives_zero_rows(self):
        """stkDelivery / stkFundFlow 就是这种：只有表头没有数据。"""
        path = os.path.join(self.tmp, 'c.csv')
        write_csv(path, DELIVERY_HEADER, [])
        rows, _ = bi.read_csv_rows(path)
        self.assertEqual(rows, [])

    def test_blank_lines_dropped(self):
        path = os.path.join(self.tmp, 'd.csv')
        with open(path, 'w', encoding='gbk', newline='') as handle:
            handle.write('a,b\n1,2\n,\n\n3,4\n')
        rows, _ = bi.read_csv_rows(path)
        self.assertEqual(len(rows), 2)


class TestParsers(unittest.TestCase):
    def test_extract_account(self):
        self.assertEqual(bi.extract_account(ACCOUNT_CELL), '25105132')
        self.assertEqual(bi.extract_account(''), None)
        self.assertEqual(bi.extract_account(None, 'fallback'), 'fallback')

    def test_norm_code_keeps_leading_zeros(self):
        self.assertEqual(bi.norm_code('000799'), '000799')
        self.assertEqual(bi.norm_code('000799.SZ'), '000799')

    def test_parse_deal_time_five_digit_hhmmss(self):
        """对账单的 成交时间 是 93055 这种 5 位数，必须补零成 09:30:55。"""
        epoch, text = bi.parse_deal_time('20260911', '93055')
        self.assertEqual(text, '2026-09-11 09:30:55')
        self.assertIsNotNone(epoch)

    def test_parse_deal_time_six_digit(self):
        _, text = bi.parse_deal_time('20260911', '101930')
        self.assertEqual(text, '2026-09-11 10:19:30')

    def test_parse_deal_time_invalid(self):
        for day, tm in (('', ''), ('2026', '93055'), ('20260911', 'abc'),
                        (None, None), ('18989999', '0')):
            with self.subTest(day=day, tm=tm):
                self.assertEqual(bi.parse_deal_time(day, tm), (None, None))

    def test_parse_side(self):
        self.assertEqual(bi.parse_side('限价买入'), 'BUY')
        self.assertEqual(bi.parse_side('限价卖出'), 'SELL')
        self.assertEqual(bi.parse_side('买入'), 'BUY')
        self.assertIsNone(bi.parse_side(''))

    def test_parse_deals(self):
        row = {
            '账号': ACCOUNT_CELL, '市场': 'SH', '证券代码': '603757',
            '操作': '限价卖出', '成交价格': '63.520', '成交数量': '100',
            '成交金额': '6352.00', '手续费': '0.00', '成交日期': '20260911',
            '成交时间': '93055', '委托编号': '8122',
            '成交编号': '81240000000001947839', '订单编号': '940572673',
            '策略名称': 'auto_full',
        }
        deals = bi.parse_deals([row], 'deals.csv')
        self.assertEqual(len(deals), 1)
        d = deals[0]
        self.assertEqual(d['account'], '25105132')
        self.assertEqual(d['code'], '603757')
        self.assertEqual(d['side'], 'SELL')
        self.assertEqual(d['price'], 63.52)
        self.assertEqual(d['deal_time_str'], '2026-09-11 09:30:55')
        self.assertEqual(d['deal_date'], '20260911')
        self.assertEqual(d['deal_time'], '09:30:55')
        self.assertEqual(d['broker_traded_id'], '81240000000001947839')
        self.assertEqual(d['broker_order_id'], '940572673')

    def test_parse_deals_skips_rows_without_code(self):
        deals = bi.parse_deals([{'账号': ACCOUNT_CELL, '证券代码': ''}])
        self.assertEqual(deals, [])

    def test_parse_orders(self):
        row = {
            '账号': ACCOUNT_CELL, '证券代码': '603757', '操作': '限价卖出',
            '委托日期': '20260911', '委托时间': '93011', '委托编号': '8122',
            '委托状态': '已成', '委托价格': '63.520', '委托数量': '100',
            '成交数量': '100', '撤单数量': '0', '订单编号': '940572673',
            '废单原因': '', '策略名称': 'auto_full',
        }
        orders = bi.parse_orders([row])
        self.assertEqual(orders[0]['status'], '已成')
        self.assertEqual(orders[0]['traded_volume'], 100)
        self.assertEqual(orders[0]['order_time'], '093011')

    def test_parse_fund_flows(self):
        row = {
            '资金账号': ACCOUNT_CELL, '发生日期': '20260911', '发生时间': '100000',
            '流水序号': '1', '业务类型': 'BANK_TRANSFER', '业务名称': '银证转入',
            '发生数量/金额': '10000.00',
        }
        flows = bi.parse_fund_flows([row])
        self.assertEqual(flows[0]['flow_type'], 'BANK_TRANSFER')
        self.assertEqual(flows[0]['amount'], 10000.0)
        self.assertEqual(flows[0]['flow_time'], '2026-09-11 10:00:00')

    def test_parse_delivery_sums_fees(self):
        row = {
            '资金账号': ACCOUNT_CELL, '证券代码': '000620', '买卖': '买入',
            '日期': '20260911', '成交时间': '93055', '成交数量': '100',
            '成交价格': '5.00', '成交金额': '500.00', '手续费': '0.15',
            '印花税': '0.00', '其它杂费': '0.01', '成交序号': 'T1', '委托号': 'P1',
        }
        rows = bi.parse_delivery([row])
        self.assertEqual(rows[0]['commission'], 0.15)
        self.assertEqual(rows[0]['stamp_duty'], 0.0)
        self.assertEqual(rows[0]['transfer_fee'], 0.01)


def local(tid, code='603757', side='SELL', price=63.52, volume=100,
          time='2026-09-11 09:30:55', amount=None, commission=0.0, row_id=1):
    return {
        'id': row_id, 'code': code, 'side': side, 'price': price, 'volume': volume,
        'trade_time': time, 'effective_time': time,
        'amount': amount if amount is not None else price * volume,
        'trade_id': tid, 'commission': commission, 'strategy': 'grid',
    }


def broker(traded_id, order_id, code='603757', side='SELL', price=63.52,
           volume=100, time='2026-09-11 09:30:55', commission=0.0,
           stamp=None, transfer=None):
    return {
        'account': '25105132', 'code': code, 'side': side, 'price': price,
        'volume': volume, 'amount': price * volume, 'commission': commission,
        'stamp_duty': stamp, 'transfer_fee': transfer,
        'broker_traded_id': traded_id, 'broker_order_id': order_id,
        'deal_date': '20260911', 'deal_time': time[11:], 'deal_time_str': time,
        'deal_time_epoch': None, 'stock_name': '', 'broker_order_ref': None,
        'strategy_name': '',
    }


class TestMatching(unittest.TestCase):
    def test_match_by_traded_id(self):
        matches, unmatched = bi.match_deals(
            [broker('T100', 'O1')], [local('T100')])
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['method'], bi.MATCH_BY_TRADED_ID)
        self.assertEqual(unmatched, [])

    def test_match_by_order_id_needs_code_disambiguation(self):
        """order_id 跨股复用：只有带上代码才能选对那一行。

        这是真实数据里的情形 —— 同一个 order_id 940572674 出现在
        002083/301218/603757/001288 四只股票上。
        """
        trades = [
            local('O1', code='002083', row_id=1),
            local('O1', code='603757', row_id=2),
            local('O1', code='001288', row_id=3),
        ]
        matches, _ = bi.match_deals(
            [broker('T999', 'O1', code='001288')], trades)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['local']['id'], 3)
        self.assertEqual(matches[0]['method'], bi.MATCH_BY_ORDER_ID)

    def test_ambiguous_order_id_without_code_is_not_guessed(self):
        """同一 order_id + 同代码有多行时不能瞎选，应落到最近时间兜底或未匹配。"""
        trades = [local('O1', code='603757', row_id=1, time='2026-09-11 09:30:55'),
                  local('O1', code='603757', row_id=2, time='2026-09-11 09:30:55')]
        matches, unmatched = bi.match_deals([broker('T999', 'O1')], trades)
        # 两行 key 完全相同 → 候选 2 个，放弃按 order_id 匹配；
        # 时间兜底也会拿到 2 个候选，最终同样无法唯一确定
        self.assertTrue(len(matches) + len(unmatched) == 1)
        if matches:
            self.assertNotEqual(matches[0]['method'], bi.MATCH_BY_ORDER_ID)

    def test_match_by_nearest_when_no_ids(self):
        trades = [local(None, row_id=7, time='2026-09-11 09:30:50')]
        matches, _ = bi.match_deals([broker(None, None, time='2026-09-11 09:30:55')],
                                    trades)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['method'], bi.MATCH_BY_NEAREST)

    def test_nearest_respects_tolerance(self):
        trades = [local(None, row_id=7, time='2026-09-11 09:00:00')]
        _, unmatched = bi.match_deals(
            [broker(None, None, time='2026-09-11 09:30:55')], trades,
            time_tolerance_sec=60)
        self.assertEqual(len(unmatched), 1)

    def test_nearest_requires_same_volume_and_price(self):
        trades = [local(None, row_id=7, volume=999)]
        _, unmatched = bi.match_deals([broker(None, None)], trades)
        self.assertEqual(len(unmatched), 1)

    def test_unmatched_broker_deal_reported(self):
        """本地根本没有这笔成交（代码/数量都对不上）时必须报未匹配，不许硬凑。"""
        matches, unmatched = bi.match_deals(
            [broker('NOT_IN_DB', 'X', code='600000', volume=999)], [local('OTHER')])
        self.assertEqual(matches, [])
        self.assertEqual(len(unmatched), 1)

    def test_fallback_still_matches_when_ids_differ(self):
        """编号对不上但代码/方向/价量/时间全同时，时间兜底应能匹配上。"""
        matches, unmatched = bi.match_deals(
            [broker('NOT_IN_DB', 'X')], [local('OTHER')])
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['method'], bi.MATCH_BY_NEAREST)
        self.assertEqual(unmatched, [])

    def test_each_local_row_consumed_at_most_once(self):
        """两笔对账单成交不能都匹配到同一行本地记录。"""
        deals = [broker('T1', 'O1'), broker('T2', 'O2')]
        trades = [local('T1', row_id=1)]
        matches, unmatched = bi.match_deals(deals, trades)
        self.assertEqual(len(matches), 1)
        self.assertEqual(len(unmatched), 1)


class TestPlanBackfill(unittest.TestCase):
    def _match(self, b, l):
        return {'broker': b, 'local': l, 'method': bi.MATCH_BY_TRADED_ID}

    def test_backfills_deal_time_and_marks_broker(self):
        updates, _ = bi.plan_backfill(self._match(
            broker('T1', 'O1', time='2026-09-11 13:00:00'),
            local('T1', time='2026-09-11 13:00:02')))
        self.assertEqual(updates['deal_time_str'], '2026-09-11 13:00:00')
        self.assertEqual(updates['time_source'], 'broker')

    def test_zero_broker_commission_does_not_overwrite(self):
        """对账单里 0.00 通常表示"该字段未导出"，用它覆盖估算值会更差。"""
        updates, notes = bi.plan_backfill(self._match(
            broker('T1', 'O1', commission=0.0), local('T1', commission=5.0)))
        self.assertNotIn('commission', updates)
        self.assertNotIn('commission_source', updates)
        self.assertTrue(any('手续费' in n for n in notes))

    def test_positive_broker_commission_overwrites(self):
        updates, _ = bi.plan_backfill(self._match(
            broker('T1', 'O1', commission=1.23), local('T1', commission=5.0)))
        self.assertEqual(updates['commission'], 1.23)
        self.assertEqual(updates['commission_source'], 'broker')

    def test_stamp_duty_added_to_commission(self):
        updates, _ = bi.plan_backfill(self._match(
            broker('T1', 'O1', commission=1.0, stamp=2.0, transfer=0.5),
            local('T1')))
        self.assertEqual(updates['commission'], 3.5)

    def test_rewrites_order_id_trade_id_to_real_traded_id(self):
        updates, _ = bi.plan_backfill(self._match(
            broker('81240000000001947839', '940572673'),
            local('940572673')))
        self.assertEqual(updates['trade_id'], '81240000000001947839')
        self.assertEqual(updates['trade_id_source'], 'traded_id')

    def test_keeps_already_correct_trade_id(self):
        updates, _ = bi.plan_backfill(self._match(
            broker('81240000000001947839', '940572673'),
            local('81240000000001947839')))
        self.assertNotIn('trade_id', updates)

    def test_does_not_rewrite_non_order_id_shape(self):
        """trade_id 不是 order_id 形态时不要动，避免破坏已有语义。"""
        updates, notes = bi.plan_backfill(self._match(
            broker('NEW_ID', 'O1'), local('SOME_SIM_ID')))
        self.assertNotIn('trade_id', updates)


if __name__ == '__main__':
    unittest.main(verbosity=2)
