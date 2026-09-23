# -*- coding: utf-8 -*-
"""手续费估算测试。

背景：QMT 的 XtTrade 结构体没有手续费字段，成交回报路径拿到的恒为 0，
实盘流水此前一律落 commission=0 / commission_source='unknown'，
交割单因此把每笔费用算成 0、盈亏被系统性高估。

本文件锁定两件事：
1. 估算算法本身（费率构成、最低佣金、买卖差异、边界）
2. **费率取值与真实扣费一致** —— 见 TestRealWorldFeeRegression，
   用 2026-09 五个交易日的实盘资金流做端到端校验。
"""
import os
import sqlite3
import tempfile
import unittest

import config
import db_migrate
import settlement_db as sdb


class TestEstimateTradeCost(unittest.TestCase):
    """估算算法的结构性约束。"""

    def setUp(self):
        self._saved = {
            k: getattr(config, k) for k in (
                'SETTLEMENT_COMMISSION_RATE', 'SETTLEMENT_STAMP_DUTY_RATE',
                'SETTLEMENT_TRANSFER_FEE_RATE', 'SETTLEMENT_COMMISSION_MIN_FEE')
        }

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(config, k, v)

    def test_buy_has_no_stamp_duty(self):
        """印花税单边收取，买入不缴。"""
        value, _ = sdb.estimate_trade_cost(10000, 'BUY')
        expected = 10000 * (config.SETTLEMENT_COMMISSION_RATE
                            + config.SETTLEMENT_TRANSFER_FEE_RATE)
        self.assertAlmostEqual(value, expected, places=4)

    def test_sell_includes_stamp_duty(self):
        value, _ = sdb.estimate_trade_cost(10000, 'SELL')
        expected = 10000 * (config.SETTLEMENT_COMMISSION_RATE
                            + config.SETTLEMENT_TRANSFER_FEE_RATE
                            + config.SETTLEMENT_STAMP_DUTY_RATE)
        self.assertAlmostEqual(value, expected, places=4)

    def test_sell_costs_more_than_buy(self):
        self.assertGreater(sdb.estimate_trade_cost(10000, 'SELL')[0],
                           sdb.estimate_trade_cost(10000, 'BUY')[0])

    def test_zero_and_negative_amount(self):
        """无成交金额的行（撤单占位等）不收费，更不能触发最低佣金。"""
        config.SETTLEMENT_COMMISSION_MIN_FEE = 5.0
        for amount in (0, None, -100):
            value, label = sdb.estimate_trade_cost(amount, 'BUY')
            self.assertEqual(value, 0.0, f'amount={amount} 应零费用')
            self.assertIsNone(label, f'amount={amount} 不应有费率标签')

    def test_case_insensitive_side(self):
        self.assertEqual(sdb.estimate_trade_cost(10000, 'sell')[0],
                         sdb.estimate_trade_cost(10000, 'SELL')[0])

    def test_rate_label_differs_by_side(self):
        self.assertNotEqual(sdb.estimate_trade_cost(10000, 'BUY')[1],
                            sdb.estimate_trade_cost(10000, 'SELL')[1])

    def test_label_reconstructs_value(self):
        """费率标签必须能还原出费用 —— 回填脚本靠它自证估算来源。"""
        for side in ('BUY', 'SELL'):
            value, label = sdb.estimate_trade_cost(8897, side)
            self.assertAlmostEqual(8897 * float(label), value, places=4)


class TestMinimumCommission(unittest.TestCase):
    """最低佣金开关。默认 0（本账户实测未生效），但机制必须可用。"""

    def setUp(self):
        self._saved_min = config.SETTLEMENT_COMMISSION_MIN_FEE

    def tearDown(self):
        config.SETTLEMENT_COMMISSION_MIN_FEE = self._saved_min

    def test_default_is_disabled(self):
        """默认不设下限 —— 09-21 实盘 7103 元成交佣金仅 0.71 元，未被抬到 5 元。"""
        self.assertEqual(config.SETTLEMENT_COMMISSION_MIN_FEE, 0.0)

    def test_min_fee_applies_to_commission_only(self):
        """最低佣金只托底佣金部分，印花税照常按比例收。"""
        config.SETTLEMENT_COMMISSION_MIN_FEE = 5.0
        value, _ = sdb.estimate_trade_cost(1000, 'SELL')
        # 佣金 1000*0.0001=0.1 → 托底为 5；印花税 1000*0.0005=0.5
        expected = 5.0 + 1000 * (config.SETTLEMENT_STAMP_DUTY_RATE
                                 + config.SETTLEMENT_TRANSFER_FEE_RATE)
        self.assertAlmostEqual(value, expected, places=4)

    def test_min_fee_not_applied_when_commission_exceeds(self):
        """佣金已超过下限时不受影响。"""
        config.SETTLEMENT_COMMISSION_MIN_FEE = 5.0
        big = 5.0 / config.SETTLEMENT_COMMISSION_RATE * 2  # 佣金 = 10 元
        value, label = sdb.estimate_trade_cost(big, 'BUY')
        self.assertAlmostEqual(value, big * config.SETTLEMENT_COMMISSION_RATE,
                               places=4)
        self.assertFalse(label.startswith('minfee'))

    def test_min_fee_label_is_explicit(self):
        """下限生效时单一费率无法表达计费，标签须显式标出。"""
        config.SETTLEMENT_COMMISSION_MIN_FEE = 5.0
        _, label = sdb.estimate_trade_cost(1000, 'SELL')
        self.assertTrue(label.startswith('minfee'), label)


class TestRealWorldFeeRegression(unittest.TestCase):
    """用实盘资金流校验费率取值 —— 本文件最重要的测试。

    数据来源：账号 25105132 的 account_equity_daily 与 trade_records。
    实扣费用 = 当日净成交额 - 当日现金变动（open→close 快照）。

    2026-09-14 被刻意排除：那天的 close 快照记录于 17:55 而非 15:05，
    跨过清算时点，15.00 元整的差额是清算调整而非手续费。

    费率若被改动而未重新校准，这里会立刻失败。
    """

    # (日期, [(方向, 成交额), ...], 实扣费用)
    SAMPLES = [
        ('2026-09-16', [('SELL', 27900.0), ('SELL', 14606.0), ('SELL', 15286.0),
                        ('SELL', 7592.0), ('SELL', 22776.0)], 52.90),
        ('2026-09-17', [('SELL', 20727.0), ('SELL', 34535.0),
                        ('BUY', 6678.0)], 33.82),
        ('2026-09-21', [('SELL', 7103.0), ('BUY', 6874.0)], 4.95),
        ('2026-09-23', [('SELL', 8897.0)], 5.34),
    ]

    # 逐笔四舍五入到分，累积误差随笔数增长；单日 24 笔时实测偏差 0.04 元
    TOLERANCE = 0.05

    def test_matches_actual_broker_charges(self):
        for date, deals, actual in self.SAMPLES:
            estimated = sum(round(sdb.estimate_trade_cost(amt, side)[0], 2)
                            for side, amt in deals)
            self.assertAlmostEqual(
                estimated, actual, delta=self.TOLERANCE,
                msg=(f'{date}: 估算 {estimated:.2f} 与实扣 {actual:.2f} 不符。'
                     f'若券商费率变更，请用新的资金流样本重新校准 '
                     f'config.SETTLEMENT_COMMISSION_RATE 并更新本用例。'))

    def test_min_fee_would_break_reality(self):
        """反向证明：启用最低 5 元会与实际扣费严重偏离。

        经纪人口头告知"最低 5 元"，但 09-21 两笔小额成交的实扣费用
        （4.95 元）低于单笔下限，该规则在本账户并未生效。
        """
        saved = config.SETTLEMENT_COMMISSION_MIN_FEE
        try:
            config.SETTLEMENT_COMMISSION_MIN_FEE = 5.0
            deals = next(d for date, d, _ in self.SAMPLES if date == '2026-09-21')
            estimated = sum(round(sdb.estimate_trade_cost(amt, side)[0], 2)
                            for side, amt in deals)
            self.assertGreater(estimated, 4.95 + 1.0,
                               '最低5元若生效，估算应显著高于实扣 4.95 元')
        finally:
            config.SETTLEMENT_COMMISSION_MIN_FEE = saved


class TestRecordTradeFallback(unittest.TestCase):
    """落库兜底：实盘路径 commission=0 时必须自动估算并标 estimated。"""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db', prefix='test_comm_')
        os.close(fd)
        conn = sqlite3.connect(self.db_path)
        conn.execute('''CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT, stock_code TEXT, stock_name TEXT,
            trade_time TIMESTAMP, trade_type TEXT, price REAL, volume INTEGER,
            amount REAL, trade_id TEXT, commission REAL, strategy TEXT)''')
        conn.commit()
        conn.close()
        db_migrate.migrate_settlement_schema(self.db_path, do_backup=False)
        db_migrate.apply_trade_records_extension(self.db_path, do_backup=False)
        sdb._SCHEMA_CACHE.clear()
        self.conn = sqlite3.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        sdb._SCHEMA_CACHE.clear()
        for suffix in ('', '-wal', '-shm'):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.remove(path)

    def _fetch(self, trade_id, table='trade_records'):
        row = self.conn.execute(
            f"SELECT commission, commission_source, commission_rate, amount "
            f"FROM {table} WHERE trade_id=?", (str(trade_id),)).fetchone()
        return row

    def _record(self, **kw):
        base = {
            'stock_code': '301085.SZ', 'stock_name': '亚康股份',
            'trade_time': '2026-09-23 09:39:57', 'trade_type': 'SELL',
            'price': 88.97, 'volume': 100, 'amount': 8897.0,
            'trade_id': '2014314497', 'deal_time_str': '2026-09-23 09:39:56',
        }
        base.update(kw)
        result = sdb.record_trade(base, conn=self.conn)
        self.conn.commit()
        return result

    def test_zero_commission_is_estimated(self):
        """复现 2026-09-23 实盘那笔：QMT 回报 commission=0.0。"""
        self.assertEqual(self._record(commission=0.0), 'inserted')
        commission, source, rate, amount = self._fetch('2014314497')
        expected, expected_rate = sdb.estimate_trade_cost(8897.0, 'SELL')
        self.assertAlmostEqual(commission, expected, places=4)
        self.assertEqual(source, sdb.COMMISSION_SOURCE_ESTIMATED)
        self.assertEqual(rate, expected_rate)

    def test_none_commission_is_estimated(self):
        self.assertEqual(self._record(commission=None), 'inserted')
        commission, source, _, _ = self._fetch('2014314497')
        self.assertGreater(commission, 0)
        self.assertEqual(source, sdb.COMMISSION_SOURCE_ESTIMATED)

    def test_estimated_value_matches_actual_charge(self):
        """端到端：落库值须贴近当日实扣的 5.34 元。"""
        self._record(commission=0.0)
        commission, _, _, _ = self._fetch('2014314497')
        self.assertAlmostEqual(commission, 5.34, delta=0.05)

    def test_explicit_source_is_not_overridden(self):
        """对账单导入的真实值绝不能被估算覆盖。"""
        self._record(commission=0.0, commission_source=sdb.COMMISSION_SOURCE_BROKER)
        commission, source, _, _ = self._fetch('2014314497')
        self.assertEqual(source, sdb.COMMISSION_SOURCE_BROKER)
        self.assertEqual(commission, 0.0, '显式 broker 来源的 0 值应原样保留')

    def test_real_commission_is_preserved(self):
        """非零手续费原样落库，不被估算值替换。"""
        self._record(commission=7.77)
        commission, source, _, _ = self._fetch('2014314497')
        self.assertAlmostEqual(commission, 7.77, places=4)
        self.assertEqual(source, sdb.COMMISSION_SOURCE_ESTIMATED)

    def test_zero_amount_row_gets_zero_fee(self):
        """撤单占位流水没有成交金额，不该凭空产生手续费。"""
        self._record(trade_id='ORDER_123', amount=0.0, volume=0,
                     commission=0.0, deal_time_str=None)
        commission, source, _, _ = self._fetch('ORDER_123')
        self.assertEqual(commission, 0.0)

    def test_simulated_trade_also_estimated(self):
        """模拟成交落 trade_records_sim，同样要有费用估算。"""
        self._record(trade_id='SIM_001', is_simulation=True, commission=0.0,
                     strategy='simu')
        row = self._fetch('SIM_001', table='trade_records_sim')
        self.assertIsNotNone(row, '模拟成交应落 trade_records_sim')
        self.assertGreater(row[0], 0)
        self.assertEqual(row[1], sdb.COMMISSION_SOURCE_ESTIMATED)


if __name__ == '__main__':
    unittest.main()
