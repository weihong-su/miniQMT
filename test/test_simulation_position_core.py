#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L1 — 模拟交易核心链路测试

直接验证 position_manager.simulate_buy_position / simulate_sell_position：
    - 加权平均成本计算
    - 买入/卖出手续费精度（费率见 config.SETTLEMENT_*_RATE）
    - SIMULATION_BALANCE 资金增减
    - 双层存储隔离（内存 positions / SQLite trade_records）
    - 边界与异常（超卖、零量、负量、available 不足、未持仓）

隔离策略：Mock data_manager 中触碰 xtdata 的方法，保留真实 conn 供 trade_records 读写。
全程只写 data/trading_test.db 与 :memory:，不触碰生产库与 5000/5001/8888 实例。
"""

import os
import re
import sys
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# 与开发者本地 .env 隔离（conftest 只在 pytest 下生效，unittest 直跑时需自行设置）
os.environ.setdefault("MINIQMT_DISABLE_DOTENV", "1")

import config
import settlement_db as sdb
from test.test_base import TestBase
from position_manager import PositionManager


def buy_cost(amount):
    """模拟买入的资金扣减额（含手续费）。

    口径与生产代码同源（settlement_db.estimate_trade_cost）——
    此处曾硬编码 0.0003 / 0.0013，费率校准后测试反而成了错误值的守门人。
    """
    return amount + sdb.estimate_trade_cost(amount, 'BUY')[0]


def sell_revenue(amount):
    """模拟卖出的资金到账额（已扣手续费与印花税）。"""
    return amount - sdb.estimate_trade_cost(amount, 'SELL')[0]


class SimulationCoreTestBase(TestBase):
    """L1 公共夹具：真实 PositionManager + Mock 掉外部数据源"""

    def setUp(self):
        super().setUp()

        # SIMULATION_BALANCE 是模块级全局变量，会被买卖原地增减。
        # TestBase 只在 setUpClass 设一次，用例之间不会重置 → 逐用例存档恢复。
        self._orig_sim_balance = config.SIMULATION_BALANCE
        config.SIMULATION_BALANCE = 100000.0
        self.initial_balance = config.SIMULATION_BALANCE

        self.pm = PositionManager()
        self.pm.stop_sync_thread()   # 停后台同步线程，避免测试期竞态写入

        # Mock data_manager：只替换触碰 xtdata/baostock 的方法，
        # 保留真实 conn —— _save_simulated_trade_record 写的是 self.conn。
        real_conn = self.pm.data_manager.conn
        mock_dm = MagicMock()
        mock_dm.conn = real_conn
        mock_dm.get_stock_name.return_value = '测试股票'
        mock_dm.get_latest_data.return_value = {'lastPrice': 10.0, 'lastClose': 9.8}
        mock_dm.ensure_subscribed = MagicMock()
        self.pm.data_manager = mock_dm
        self.mock_dm = mock_dm

        self._clear_memory_positions()
        self._clear_trade_records()

    def tearDown(self):
        try:
            self.pm.stop_sync_thread()
        except Exception:
            pass
        config.SIMULATION_BALANCE = self._orig_sim_balance
        super().tearDown()

    # ---------- 工具方法 ----------

    def _clear_memory_positions(self):
        with self.pm.memory_conn_lock:
            self.pm.memory_conn.execute("DELETE FROM positions")
            self.pm.memory_conn.commit()

    def _clear_trade_records(self):
        cur = self.pm.conn.cursor()
        cur.execute("DELETE FROM trade_records")
        self.pm.conn.commit()

    def _trade_record_count(self):
        cur = self.pm.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM trade_records")
        return cur.fetchone()[0]

    def _memory_position(self, stock_code):
        """从内存库直接读取一行持仓（绕过 get_position 的任何加工）

        注意：_sync_db_to_memory 用 pandas to_sql(if_exists="replace") 重建内存表，
        列类型由启动时 SQLite 里的存量数据推断而来，可能是 TEXT 而非 schema 声明的
        REAL。因此数值字段一律经 _num() 归一，测试断言只关心数值本身。
        """
        with self.pm.memory_conn_lock:
            cur = self.pm.memory_conn.cursor()
            cur.execute("SELECT * FROM positions WHERE stock_code=?", (stock_code,))
            row = cur.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    @staticmethod
    def _num(value):
        """把内存库取出的值归一为 float（应对 TEXT 列类型）"""
        if value is None:
            return None
        return float(value)

    def _sqlite_position_count(self, stock_code):
        cur = self.pm.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM positions WHERE stock_code=?", (stock_code,))
        return cur.fetchone()[0]

    def assert_no_side_effects(self, balance_before, records_before, msg=""):
        """异常用例三重断言之二三：资金未变 + 未落交易记录"""
        self.assertAlmostEqual(
            config.SIMULATION_BALANCE, balance_before, places=6,
            msg=f"{msg}: SIMULATION_BALANCE 不应变化")
        self.assertEqual(
            self._trade_record_count(), records_before,
            msg=f"{msg}: 不应写入交易记录")


class TestSimulationBuy(SimulationCoreTestBase):
    """L1-01 ~ L1-08, L1-17, L1-18: 模拟买入"""

    def test_L1_01_new_position_fields(self):
        """L1-01 新建仓字段落地正确"""
        ok = self.pm.simulate_buy_position('000001.SZ', 1000, 10.0)
        self.assertTrue(ok)

        pos = self._memory_position('000001.SZ')
        self.assertIsNotNone(pos, "内存库应有持仓记录")
        self.assertEqual(self._num(pos['volume']), 1000)
        self.assertEqual(self._num(pos['available']), 1000)
        self.assertAlmostEqual(self._num(pos['cost_price']), 10.0, places=2)
        self.assertAlmostEqual(self._num(pos['base_cost_price']), 10.0, places=2)
        self.assertAlmostEqual(self._num(pos['highest_price']), 10.0, places=2)
        self.assertFalse(bool(pos['profit_triggered']))
        self.assertIsNotNone(pos['open_date'], "新建仓应写入 open_date")

    def test_L1_02_buy_commission_precision(self):
        """L1-02 买入手续费精度：cost = 成交额 + 按配置费率估算的手续费"""
        before = config.SIMULATION_BALANCE
        self.pm.simulate_buy_position('000001.SZ', 1000, 10.0)

        expected_cost = buy_cost(10.0 * 1000)
        self.assertAlmostEqual(
            before - config.SIMULATION_BALANCE, expected_cost, places=2,
            msg="扣减金额必须与 settlement_db.estimate_trade_cost 同源")

    def test_L1_03_weighted_average_cost(self):
        """L1-03 加仓走加权平均成本"""
        self.pm.simulate_buy_position('000001.SZ', 1000, 10.0)
        self.pm.simulate_buy_position('000001.SZ', 500, 12.0)

        pos = self._memory_position('000001.SZ')
        self.assertEqual(self._num(pos['volume']), 1500)
        self.assertEqual(self._num(pos['available']), 1500)
        # (1000*10.0 + 500*12.0) / 1500 = 10.6666...，写库时 round(...,2) → 10.67
        expected = round((1000 * 10.0 + 500 * 12.0) / 1500, 2)
        self.assertAlmostEqual(self._num(pos['cost_price']), expected, places=2)

    def test_L1_04_addon_keeps_open_date_and_base_cost(self):
        """L1-04 加仓保留原 open_date 与 base_cost_price"""
        self.pm.simulate_buy_position('000001.SZ', 1000, 10.0)
        first = self._memory_position('000001.SZ')

        self.pm.simulate_buy_position('000001.SZ', 500, 12.0)
        after = self._memory_position('000001.SZ')

        self.assertEqual(after['open_date'], first['open_date'],
                         "加仓不应刷新 open_date")
        self.assertAlmostEqual(self._num(after['base_cost_price']), 10.0, places=2,
                               msg="base_cost_price 应保持初始建仓成本，不随加仓变化")

    def test_L1_04b_update_position_keeps_existing_base_cost(self):
        """L1-04b update_position 后续更新不得覆盖已有 base_cost_price"""
        self.pm.update_position('000001.SZ', 1000, 10.0, 10.0, stock_name='测试股票')
        self.pm.update_position('000001.SZ', 1500, 10.67, 11.0, stock_name='测试股票')

        pos = self._memory_position('000001.SZ')
        self.assertAlmostEqual(self._num(pos['cost_price']), 10.67, places=2)
        self.assertAlmostEqual(self._num(pos['base_cost_price']), 10.0, places=2,
                               msg="已有持仓的 base_cost_price 应保持首次建仓成本")

    def test_L1_05_highest_price_takes_max(self):
        """L1-05 加仓价低于历史高点时 highest_price 不倒退"""
        self.pm.simulate_buy_position('000001.SZ', 1000, 10.0)
        self.pm.simulate_buy_position('000001.SZ', 500, 9.0)

        pos = self._memory_position('000001.SZ')
        self.assertAlmostEqual(self._num(pos['highest_price']), 10.0, places=2,
                               msg="highest_price 应取 max，不应被低价加仓拉低")

    def test_L1_06_stop_loss_price_recalculated(self):
        """L1-06 买入后重算 stop_loss_price"""
        self.pm.simulate_buy_position('000001.SZ', 1000, 10.0)

        pos = self._memory_position('000001.SZ')
        self.assertIsNotNone(pos['stop_loss_price'], "应写入止损价")
        self.assertLess(self._num(pos['stop_loss_price']), self._num(pos['cost_price']),
                        "止损价应低于成本价")

    def test_L1_07_success_side_effects(self):
        """L1-07 买入成功的副作用编排 + trade_id 格式"""
        with patch.object(self.pm, '_increment_data_version',
                          wraps=self.pm._increment_data_version) as mock_ver:
            ok = self.pm.simulate_buy_position('000001.SZ', 1000, 10.0)

        self.assertTrue(ok)
        # 盘中新增持仓需确保已订阅 xtdata 实时推送
        self.mock_dm.ensure_subscribed.assert_called_once_with('000001.SZ')
        self.assertGreaterEqual(mock_ver.call_count, 1,
                                "必须触发数据版本更新，否则前端不刷新")

        cur = self.pm.conn.cursor()
        cur.execute("SELECT trade_id, trade_type, strategy FROM trade_records "
                    "WHERE stock_code='000001.SZ'")
        trade_id, trade_type, strategy = cur.fetchone()
        self.assertRegex(trade_id, r'^SIM_\d{14}_000001\.SZ_BUY$')
        self.assertEqual(trade_type, 'BUY')
        self.assertEqual(strategy, 'simu')

    def test_L1_08_trade_record_failure_short_circuits(self):
        """L1-08 交易记录落库失败时整体短路，不动资金、不订阅"""
        before_balance = config.SIMULATION_BALANCE
        before_records = self._trade_record_count()

        with patch.object(self.pm, '_save_simulated_trade_record', return_value=False):
            ok = self.pm.simulate_buy_position('000001.SZ', 1000, 10.0)

        self.assertFalse(ok, "落库失败必须返回 False")
        self.assertIsNone(self._memory_position('000001.SZ'), "不应建仓")
        self.mock_dm.ensure_subscribed.assert_not_called()
        self.assert_no_side_effects(before_balance, before_records, "落库失败")

    def test_L1_17_consecutive_buys_accumulate_cost(self):
        """L1-17 连续买入不同股票，资金精确累计扣减"""
        before = config.SIMULATION_BALANCE
        self.pm.simulate_buy_position('000001.SZ', 1000, 10.0)
        self.pm.simulate_buy_position('600036.SH', 500, 20.0)

        expected = buy_cost(10.0 * 1000) + buy_cost(20.0 * 500)
        self.assertAlmostEqual(before - config.SIMULATION_BALANCE, expected, places=2)

    def test_L1_18_stock_name_source(self):
        """L1-18 新建仓查 get_stock_name；加仓沿用已有名称不重复查询"""
        self.mock_dm.get_stock_name.return_value = '平安银行'
        self.pm.simulate_buy_position('000001.SZ', 1000, 10.0)

        pos = self._memory_position('000001.SZ')
        self.assertEqual(pos['stock_name'], '平安银行')

        # 加仓：simulate_buy_position 从已有 position 取 stock_name，
        # 不再为"建仓命名"而调用；此处校验加仓后名称保持稳定。
        self.mock_dm.get_stock_name.return_value = '不应被采用'
        self.pm.simulate_buy_position('000001.SZ', 500, 11.0)

        pos2 = self._memory_position('000001.SZ')
        self.assertEqual(pos2['stock_name'], '平安银行',
                         "加仓应沿用原股票名称")


class TestSimulationSell(SimulationCoreTestBase):
    """L1-09 ~ L1-15: 模拟卖出"""

    def _seed(self, stock_code='000001.SZ', volume=1000, price=10.0):
        ok = self.pm.simulate_buy_position(stock_code, volume, price)
        self.assertTrue(ok, "前置建仓应成功")

    def test_L1_09_full_sell_revenue_and_clear(self):
        """L1-09 全部卖出：按配置费率扣手续费 + 持仓清零"""
        self._seed()
        before = config.SIMULATION_BALANCE

        ok = self.pm.simulate_sell_position('000001.SZ', 1000, 11.0, sell_type='full')
        self.assertTrue(ok)

        expected_revenue = sell_revenue(11.0 * 1000)
        self.assertAlmostEqual(config.SIMULATION_BALANCE - before,
                               expected_revenue, places=2,
                               msg="到账金额必须与 settlement_db.estimate_trade_cost 同源")
        self.assertIsNone(self._memory_position('000001.SZ'),
                          "全仓卖出后持仓记录应被删除")

    def test_L1_10_partial_sell_amortizes_profit_into_cost(self):
        """L1-10 首次部分卖出：获利分摊到剩余持仓，成本价被摊薄

        这是有意设计（position_manager.py:3257-3272）：
        profit_triggered=False 时的部分卖出视为"首次止盈"，
        把卖出获利摊入剩余持仓以降低成本价，并置 profit_triggered=True。
        注意源码此分支的日志写 "成本价: xx (保持不变)" 属措辞误导，实际会变。
        """
        self._seed()   # 1000 @ 10.0
        ok = self.pm.simulate_sell_position('000001.SZ', 400, 11.0, sell_type='partial')
        self.assertTrue(ok)

        pos = self._memory_position('000001.SZ')
        self.assertEqual(self._num(pos['volume']), 600)
        self.assertEqual(self._num(pos['available']), 600)

        # revenue      = 11.0*400 扣手续费后的到账额
        # sell_profit  = revenue - 400*10.0
        # final_cost   = (600*10.0 - sell_profit)/600
        revenue = sell_revenue(11.0 * 400)
        expected_cost = round((600 * 10.0 - (revenue - 400 * 10.0)) / 600, 2)
        self.assertAlmostEqual(self._num(pos['cost_price']), expected_cost, places=2,
                               msg="首次部分卖出应把获利摊入剩余持仓")
        self.assertTrue(bool(pos['profit_triggered']),
                        "首次部分卖出后应置 profit_triggered=True")

    def test_L1_10b_second_partial_sell_keeps_cost_price(self):
        """L1-10b 已触发首次止盈后，再次部分卖出不再摊薄成本价"""
        self._seed()
        # 第一次部分卖出 → profit_triggered=True
        self.pm.simulate_sell_position('000001.SZ', 400, 11.0, sell_type='partial')
        cost_after_first = self._num(self._memory_position('000001.SZ')['cost_price'])

        # 第二次部分卖出 → 走 else 分支，保持原成本价
        ok = self.pm.simulate_sell_position('000001.SZ', 200, 12.0, sell_type='partial')
        self.assertTrue(ok)

        pos = self._memory_position('000001.SZ')
        self.assertEqual(self._num(pos['volume']), 400)
        self.assertAlmostEqual(self._num(pos['cost_price']), cost_after_first, places=2,
                               msg="profit_triggered=True 后成本价应保持不变")

    def test_L1_11_partial_with_full_volume_goes_clear_branch(self):
        """L1-11 sell_type='partial' 但卖光时应走清仓分支"""
        self._seed()
        ok = self.pm.simulate_sell_position('000001.SZ', 1000, 11.0, sell_type='partial')
        self.assertTrue(ok)
        self.assertIsNone(self._memory_position('000001.SZ'),
                          "sell_volume >= current_volume 应触发清仓")

    def test_L1_12_oversell_rejected(self):
        """L1-12 超卖拒绝（三重断言）"""
        self._seed(volume=600)
        before_balance = config.SIMULATION_BALANCE
        before_records = self._trade_record_count()

        ok = self.pm.simulate_sell_position('000001.SZ', 700, 11.0)

        self.assertFalse(ok)
        self.assert_no_side_effects(before_balance, before_records, "超卖")
        self.assertEqual(self._num(self._memory_position('000001.SZ')['volume']), 600,
                         "持仓不应变化")

    def test_L1_13_zero_and_negative_volume_rejected(self):
        """L1-13 零/负数量卖出拒绝（三重断言）"""
        self._seed()
        for bad_volume in (0, -100):
            with self.subTest(sell_volume=bad_volume):
                before_balance = config.SIMULATION_BALANCE
                before_records = self._trade_record_count()

                ok = self.pm.simulate_sell_position('000001.SZ', bad_volume, 11.0)

                self.assertFalse(ok)
                self.assert_no_side_effects(before_balance, before_records,
                                            f"sell_volume={bad_volume}")

    def test_L1_14_insufficient_available_rejected(self):
        """L1-14 可用数量不足时拒绝（部分冻结场景）"""
        self._seed(volume=1000)
        # 模拟 500 股被挂单冻结
        with self.pm.memory_conn_lock:
            self.pm.memory_conn.execute(
                "UPDATE positions SET available=500 WHERE stock_code='000001.SZ'")
            self.pm.memory_conn.commit()

        before_balance = config.SIMULATION_BALANCE
        before_records = self._trade_record_count()

        ok = self.pm.simulate_sell_position('000001.SZ', 800, 11.0)

        self.assertFalse(ok, "卖出量超过 available 应被拒绝")
        self.assert_no_side_effects(before_balance, before_records, "available 不足")

    def test_L1_15_sell_without_position_rejected(self):
        """L1-15 未持仓卖出直接拒绝"""
        before_balance = config.SIMULATION_BALANCE
        before_records = self._trade_record_count()

        ok = self.pm.simulate_sell_position('999999.SZ', 100, 11.0)

        self.assertFalse(ok)
        self.assert_no_side_effects(before_balance, before_records, "未持仓卖出")


class TestSimulationDualLayerIsolation(SimulationCoreTestBase):
    """L1-16: 双层存储隔离"""

    def test_L1_16_buy_writes_memory_not_sqlite_positions(self):
        """L1-16 模拟买入只写内存 positions，交易流水落 SQLite"""
        ok = self.pm.simulate_buy_position('000001.SZ', 1000, 10.0)
        self.assertTrue(ok)

        # 内存库有持仓
        self.assertIsNotNone(self._memory_position('000001.SZ'))

        # SQLite positions 表不应有该模拟持仓（_simulate_update_position 只写内存）
        self.assertEqual(self._sqlite_position_count('000001.SZ'), 0,
                         "模拟持仓不应落到 SQLite positions 表")

        # 但交易流水必须落 SQLite
        cur = self.pm.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM trade_records WHERE stock_code='000001.SZ'")
        self.assertEqual(cur.fetchone()[0], 1,
                         "交易记录必须持久化到 SQLite trade_records")


if __name__ == '__main__':
    unittest.main(verbosity=2)
