# -*- coding: utf-8 -*-
"""
收盘快照陈旧数据专项测试（P1 回归）
==================================

复现并守护 2026-09-14 / 09-15 的交割单快照失真：

    position_snapshot 合计市值   vs   account_equity_daily.market_value
    09-14 open  (09:25 交易时段)    114602.00  ==  114602.00   ✅
    09-16 close (15:06 进程在跑)    111928.00  ==  111928.00   ✅
    09-14 close (17:55 重启补录)    180414.00  !=  194558.00   ❌ 差 14144.00
    09-15 close (22:30 重启补录)    178166.00  !=  192266.00   ❌ 差 14100.00

两个差额精确等于 301085 缺失的 200 股（200×70.72 / 200×70.50）——那正是
09-14 13:29 网格买入的份额。同一次 take_snapshot 里，净值快照走 QMT 实时
接口所以知道这 200 股，持仓快照读内存表所以不知道。

两条根因：
  Q1 持仓快照取自内存表，而持仓监控线程在非交易时段直接 sleep 跳过同步，
     盘后补录时内存表停留在上个交易时段、甚至上个进程留在 SQLite 里的值。
     09-15 的 close 快照就是 09-14 close 的逐字段拷贝（当天进程根本没运行）。
  Q2 `last_run_date` 只活在进程内存里，重启即归 None，于是一天内每重启一次
     就重跑一次 take_snapshot，而写入是 INSERT OR REPLACE —— 后跑的覆盖先跑的。
     09-14 15:07 写过一份，17:55 重启后又写一份把它覆盖掉。

内存表当时为何是陈旧的：09-14 13:29:55~14:59:45 期间 `database is locked`
故障持续 90 分钟、内存→SQLite 同步连续失败 457 次，13:29 网格买入的
volume 1000→1200 / cost 73.58→73.04 从未落到 SQLite；随后每次重启都从
SQLite 载入这份旧值，而非交易时段又不会回源实盘去纠正它。
注意 QMT 侧数据始终是对的 —— 同一次写入的净值快照 market_value=194558.00
正是按 1200 股算的，所以失真只发生在"内存表 → 快照"这一侧，取数前回源
实盘即可根治。

测试分组：
    A - Q1：快照取数前必须强制回源实盘
    B - 数据来源如实标记（刷不到实盘不得冒充实时）
    C - Q2：重复补录不得覆盖已有快照
    D - has_snapshot 语义
    E - refresh_positions_from_broker 语义
    F - 端到端：持仓快照合计市值必须与同次净值快照一致
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, date, time as dtime
from unittest.mock import patch

import pandas as pd

import config
import db_migrate
import settlement_db as sdb


POSITION_COLS = ['stock_code', 'stock_name', 'volume', 'available', 'cost_price',
                 'base_cost_price', 'current_price', 'market_value', 'profit_ratio']


def _positions(rows):
    return pd.DataFrame(rows, columns=POSITION_COLS)


class RefreshablePositionManager:
    """带"实盘 vs 内存"两套数据的 position_manager 替身。

    `refresh_positions_from_broker()` 被调用后，内存快照才会跟上实盘；
    不调用就一直返回陈旧值——正是生产中盘后补录的处境。
    """

    def __init__(self, stale_positions, broker_positions, account_info,
                 refresh_ok=True):
        self._stale = stale_positions
        self._broker = broker_positions
        self._account_info = account_info
        self._refresh_ok = refresh_ok
        self.refreshed = False
        self.refresh_calls = []

    def refresh_positions_from_broker(self, reason="", timeout=None):
        self.refresh_calls.append(reason)
        if not self._refresh_ok:
            return False
        self.refreshed = True
        return True

    def get_all_positions_with_all_fields(self):
        return self._broker if self.refreshed else self._stale

    def get_account_info(self):
        return self._account_info


class LegacyPositionManager:
    """没有 refresh_positions_from_broker 的旧版替身（向后兼容验证）。"""

    def __init__(self, positions, account_info):
        self._positions = positions
        self._account_info = account_info

    def get_all_positions_with_all_fields(self):
        return self._positions

    def get_account_info(self):
        return self._account_info


class SnapshotFreshnessBase(unittest.TestCase):
    """复刻 09-14 的真实数据：内存表少记 301085 的 200 股。"""

    STALE = [
        ('001288', '运机集团', 1000, 0, 26.53, 26.53, 29.09, 29090.0, 9.6),
        ('002859', '洁美科技', 1200, 0, 66.20, 66.20, 67.17, 80604.0, 1.5),
        ('301085', '亚康股份', 1000, 0, 73.58, 73.58, 70.72, 70720.0, -3.9),
    ]
    BROKER = [
        ('001288', '运机集团', 1000, 1000, 26.53, 26.53, 29.09, 29090.0, 9.6),
        ('002859', '洁美科技', 1200, 1200, 66.20, 66.20, 67.17, 80604.0, 1.5),
        ('301085', '亚康股份', 1200, 1200, 73.04, 73.04, 70.72, 84864.0, -3.2),
    ]
    # 净值快照走 QMT 实时接口，认的是 1200 股那一版
    ACCOUNT_INFO = {'total_asset': 514063.63, 'market_value': 194558.0,
                    'available': 319505.63, 'frozen_cash': 0.0}

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix='_test.db', prefix='snapfresh_')
        os.close(fd)
        db_migrate.migrate_settlement_schema(self.db, do_backup=False)
        self.pm = RefreshablePositionManager(
            _positions(self.STALE), _positions(self.BROKER), dict(self.ACCOUNT_INFO))
        # 默认按实盘模式跑，不受运行环境的 config 取值影响
        patcher = patch.object(config, 'ENABLE_SIMULATION_MODE', False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        for suffix in ('', '-wal', '-shm'):
            path = self.db + suffix
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    def query(self, sql, params=()):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    def snapshot_rows(self, snapshot_type=sdb.SNAPSHOT_CLOSE):
        return self.query(
            "SELECT * FROM position_snapshot WHERE snapshot_type=? ORDER BY code",
            (snapshot_type,))


class TestForcesBrokerRefresh(SnapshotFreshnessBase):
    """A 组：快照取数前必须强制回源实盘（修复前失败）"""

    def test_A1_refresh_is_called_before_fetch(self):
        sdb.write_position_snapshot(self.pm, sdb.SNAPSHOT_CLOSE,
                                    snapshot_date='2026-09-14', db_path=self.db)
        self.assertEqual(len(self.pm.refresh_calls), 1,
                         "写持仓快照前必须强制回源一次实盘持仓")

    def test_A2_snapshot_uses_broker_volume_not_stale_memory(self):
        """核心复现：内存表 301085=1000，实盘 1200，快照必须记 1200"""
        sdb.write_position_snapshot(self.pm, sdb.SNAPSHOT_CLOSE,
                                    snapshot_date='2026-09-14', db_path=self.db)
        row = [r for r in self.snapshot_rows() if r['code'] == '301085'][0]
        self.assertEqual(row['volume'], 1200,
                         "快照写了陈旧的内存持仓（09-14 实盘少记 200 股即此故障）")
        self.assertEqual(row['available'], 1200,
                         "available=0 是 SQLite 陈旧快照的指纹，实盘刷新后不应为 0")

    def test_A3_cost_price_also_refreshed(self):
        """成本价同样必须是刷新后的值（买入摊薄后 73.58 -> 73.04）"""
        sdb.write_position_snapshot(self.pm, sdb.SNAPSHOT_CLOSE,
                                    snapshot_date='2026-09-14', db_path=self.db)
        row = [r for r in self.snapshot_rows() if r['code'] == '301085'][0]
        self.assertAlmostEqual(row['cost_price'], 73.04, places=2)

    def test_A4_open_snapshot_also_refreshes(self):
        """open 快照走同一个写入函数，同样受保护"""
        sdb.write_position_snapshot(self.pm, sdb.SNAPSHOT_OPEN,
                                    snapshot_date='2026-09-14', db_path=self.db)
        self.assertEqual(len(self.pm.refresh_calls), 1)
        row = [r for r in self.snapshot_rows(sdb.SNAPSHOT_OPEN)
               if r['code'] == '301085'][0]
        self.assertEqual(row['volume'], 1200)


class TestSourceHonesty(SnapshotFreshnessBase):
    """B 组：数据来源如实标记，绝不把陈旧数据冒充实时"""

    def test_B1_refreshed_marked_memory_db(self):
        with patch.object(config, 'ENABLE_SIMULATION_MODE', False):
            sdb.write_position_snapshot(self.pm, sdb.SNAPSHOT_CLOSE,
                                        snapshot_date='2026-09-14', db_path=self.db)
        self.assertEqual({r['source'] for r in self.snapshot_rows()}, {'memory_db'})

    def test_B2_refresh_failure_marked_stale(self):
        """刷不到实盘（QMT 断连/超时）必须标 stale，不得冒充实时"""
        pm = RefreshablePositionManager(
            _positions(self.STALE), _positions(self.BROKER),
            dict(self.ACCOUNT_INFO), refresh_ok=False)
        with patch.object(config, 'ENABLE_SIMULATION_MODE', False):
            sdb.write_position_snapshot(pm, sdb.SNAPSHOT_CLOSE,
                                        snapshot_date='2026-09-14', db_path=self.db)
        self.assertEqual({r['source'] for r in self.snapshot_rows()},
                         {'memory_db_stale'},
                         "刷新失败时必须如实标记来源，否则审计无法识别失真快照")

    def test_B3_refresh_exception_does_not_break_snapshot(self):
        """刷新抛异常不得让快照写失败——宁可标 stale 也要留下数据"""
        pm = RefreshablePositionManager(
            _positions(self.STALE), _positions(self.BROKER), dict(self.ACCOUNT_INFO))

        def _boom(reason="", timeout=None):
            raise RuntimeError("QMT 崩了")
        pm.refresh_positions_from_broker = _boom

        with patch.object(config, 'ENABLE_SIMULATION_MODE', False):
            rows = sdb.write_position_snapshot(pm, sdb.SNAPSHOT_CLOSE,
                                               snapshot_date='2026-09-14',
                                               db_path=self.db)
        self.assertEqual(rows, 3)
        self.assertEqual({r['source'] for r in self.snapshot_rows()},
                         {'memory_db_stale'})

    def test_B4_legacy_position_manager_still_works(self):
        """没有该方法的旧版 position_manager 应降级而不是报错"""
        pm = LegacyPositionManager(_positions(self.STALE), dict(self.ACCOUNT_INFO))
        with patch.object(config, 'ENABLE_SIMULATION_MODE', False):
            rows = sdb.write_position_snapshot(pm, sdb.SNAPSHOT_CLOSE,
                                               snapshot_date='2026-09-14',
                                               db_path=self.db)
        self.assertEqual(rows, 3)
        self.assertEqual({r['source'] for r in self.snapshot_rows()},
                         {'memory_db_stale'})

    def test_B5_simulation_marked_simulation_not_stale(self):
        """模拟模式下内存表就是权威数据源，标 simulation 而不是 stale。

        与 write_equity_snapshot 的 source 取值口径保持一致。
        """
        with patch.object(config, 'ENABLE_SIMULATION_MODE', True):
            sdb.write_position_snapshot(self.pm, sdb.SNAPSHOT_CLOSE,
                                        snapshot_date='2026-09-14', db_path=self.db)
        self.assertEqual({r['source'] for r in self.snapshot_rows()}, {'simulation'})
        self.assertEqual(self.pm.refresh_calls, [],
                         "模拟模式不该去刷实盘")


class TestNoDuplicateBackfill(SnapshotFreshnessBase):
    """C 组：重启造成的重复补录不得覆盖已有快照（09-14 15:07 被 17:55 覆盖）"""

    def _run_scheduler_once(self, now, stop_event):
        """跑一轮 schedule_close_snapshot 循环体。"""
        class _OneShot:
            def __init__(self):
                self.calls = 0

            def is_set(self):
                self.calls += 1
                return self.calls > 1

            def wait(self, _):
                return None

        with patch.object(sdb, 'datetime', wraps=datetime) as mock_dt, \
             patch.object(sdb, 'is_trading_day', return_value=(True, True)), \
             patch.object(sdb, '_connect', side_effect=lambda p=None: sqlite3.connect(self.db)), \
             patch.object(sdb, 'check_snapshot_health', return_value=[]):
            mock_dt.now.return_value = now
            mock_dt.strptime = datetime.strptime
            sdb.schedule_close_snapshot(self.pm, stop_event=_OneShot())

    def test_C1_second_run_does_not_overwrite(self):
        """15:07 写了一份，17:55 重启后再跑不得覆盖"""
        good_now = datetime(2026, 9, 14, 15, 7, 0)
        self._run_scheduler_once(good_now, None)
        first = self.snapshot_rows()
        self.assertEqual(len(first), 3, "第一次应正常写入")
        first_recorded = {r['code']: r['recorded_at'] for r in first}

        # 模拟重启：内存持仓退化成清算时段的失真数据，last_run_date 归 None
        self.pm.refreshed = False
        self.pm._broker = _positions([
            ('001288', '运机集团', 1000, 0, 26.53, 26.53, 29.09, 29090.0, 9.6),
        ])
        self._run_scheduler_once(datetime(2026, 9, 14, 17, 55, 0), None)

        second = self.snapshot_rows()
        self.assertEqual(len(second), 3, "已有快照不得被重复补录覆盖成 1 行")
        self.assertEqual({r['code']: r['recorded_at'] for r in second}, first_recorded,
                         "recorded_at 变了说明快照被覆盖重写")

    def test_C2_first_run_still_writes(self):
        """判重不得误伤真正的首次补录"""
        self._run_scheduler_once(datetime(2026, 9, 14, 16, 30, 0), None)
        self.assertEqual(len(self.snapshot_rows()), 3)

    def test_C3_next_day_not_blocked(self):
        """当天已有快照不得挡住第二天"""
        self._run_scheduler_once(datetime(2026, 9, 14, 15, 7, 0), None)
        self._run_scheduler_once(datetime(2026, 9, 15, 15, 7, 0), None)
        dates = {r['snapshot_date'] for r in self.snapshot_rows()}
        self.assertEqual(dates, {'2026-09-14', '2026-09-15'})


class TestHasSnapshot(SnapshotFreshnessBase):
    """D 组：has_snapshot 判重语义"""

    def test_D1_false_when_empty(self):
        self.assertFalse(sdb.has_snapshot('2026-09-14', sdb.SNAPSHOT_CLOSE, self.db))

    def test_D2_true_after_position_snapshot(self):
        sdb.write_position_snapshot(self.pm, sdb.SNAPSHOT_CLOSE,
                                    snapshot_date='2026-09-14', db_path=self.db)
        self.assertTrue(sdb.has_snapshot('2026-09-14', sdb.SNAPSHOT_CLOSE, self.db))

    def test_D3_true_on_equity_only(self):
        """空仓日没有持仓行，只有净值行——也必须算跑过，否则会重复补录"""
        sdb.write_equity_snapshot(self.pm, sdb.SNAPSHOT_CLOSE,
                                  snapshot_date='2026-09-14', db_path=self.db)
        self.assertEqual(self.snapshot_rows(), [])
        self.assertTrue(sdb.has_snapshot('2026-09-14', sdb.SNAPSHOT_CLOSE, self.db))

    def test_D4_type_and_date_are_scoped(self):
        sdb.write_position_snapshot(self.pm, sdb.SNAPSHOT_OPEN,
                                    snapshot_date='2026-09-14', db_path=self.db)
        self.assertTrue(sdb.has_snapshot('2026-09-14', sdb.SNAPSHOT_OPEN, self.db))
        self.assertFalse(sdb.has_snapshot('2026-09-14', sdb.SNAPSHOT_CLOSE, self.db))
        self.assertFalse(sdb.has_snapshot('2026-09-15', sdb.SNAPSHOT_OPEN, self.db))

    def test_D5_query_failure_returns_false(self):
        """查不了就按没有处理：宁可重跑也不要漏掉当天快照"""
        self.assertFalse(sdb.has_snapshot('2026-09-14', sdb.SNAPSHOT_CLOSE,
                                          '/nonexistent_dir/nope.db'))


class TestRefreshPositionsFromBroker(unittest.TestCase):
    """E 组：position_manager.refresh_positions_from_broker 语义"""

    def _make_pm(self, simulation=False, qmt_trader=object()):
        from position_manager import PositionManager

        class FakePM:
            pass

        pm = FakePM()
        pm.qmt_trader = qmt_trader
        pm.positions_cache = 'stale'
        pm.last_position_update_time = 12345.0
        pm.get_all_positions_calls = []
        pm.get_all_positions = lambda: pm.get_all_positions_calls.append(1)
        pm._invalidate_positions_cache = \
            PositionManager._invalidate_positions_cache.__get__(pm, FakePM)
        pm.refresh_positions_from_broker = \
            PositionManager.refresh_positions_from_broker.__get__(pm, FakePM)
        return pm

    def test_E1_invalidates_cache_and_reloads(self):
        pm = self._make_pm()
        with patch.object(config, 'ENABLE_SIMULATION_MODE', False):
            self.assertTrue(pm.refresh_positions_from_broker("单测"))
        self.assertEqual(len(pm.get_all_positions_calls), 1, "必须真正回源一次")
        self.assertEqual(pm.last_position_update_time, 0,
                         "必须复位 TTL，否则 get_all_positions 会走缓存短路")

    def test_E2_simulation_mode_returns_false(self):
        """模拟模式没有实盘可刷，如实返回 False 让调用方标 stale"""
        pm = self._make_pm()
        with patch.object(config, 'ENABLE_SIMULATION_MODE', True):
            self.assertFalse(pm.refresh_positions_from_broker("单测"))
        self.assertEqual(pm.get_all_positions_calls, [])

    def test_E3_no_qmt_trader_returns_false(self):
        pm = self._make_pm(qmt_trader=None)
        with patch.object(config, 'ENABLE_SIMULATION_MODE', False):
            self.assertFalse(pm.refresh_positions_from_broker("单测"))

    def test_E4_exception_returns_false_not_raise(self):
        pm = self._make_pm()

        def _boom():
            raise RuntimeError("QMT 崩了")
        pm.get_all_positions = _boom

        with patch.object(config, 'ENABLE_SIMULATION_MODE', False):
            self.assertFalse(pm.refresh_positions_from_broker("单测"))


class TestSnapshotConsistency(SnapshotFreshnessBase):
    """F 组：端到端——持仓快照合计市值必须与同次净值快照一致。

    这正是定位本 bug 的判据：两份快照由同一次 take_snapshot 写出，一份走内存
    表、一份走 QMT 实时接口，对不上就说明持仓快照失真。
    """

    def _mv_gap(self, snapshot_date):
        pos_total = sum(r['market_value'] or 0 for r in self.snapshot_rows())
        equity = self.query(
            "SELECT market_value FROM account_equity_daily "
            "WHERE date=? AND snapshot_type=?",
            (snapshot_date, sdb.SNAPSHOT_CLOSE))
        self.assertEqual(len(equity), 1, "净值快照应有且仅有一行")
        return round(pos_total - equity[0]['market_value'], 2)

    def test_F1_position_and_equity_market_value_match(self):
        with patch.object(config, 'ENABLE_SIMULATION_MODE', False), \
             patch.object(sdb, '_connect', side_effect=lambda p=None: sqlite3.connect(self.db)):
            sdb.take_snapshot(self.pm, sdb.SNAPSHOT_CLOSE, snapshot_date='2026-09-14',
                              db_path=self.db)
        self.assertEqual(
            self._mv_gap('2026-09-14'), 0.0,
            "持仓快照与净值快照市值不一致 —— 09-14 差 14144.00、09-15 差 14100.00 即此故障")

    def test_F2_gap_reproduced_without_refresh(self):
        """反向验证：不刷新实盘就会重现 14144.00 的缺口。

        保证 F1 不是假阳性——如果桩数据本来就对得上，F1 即使没修复也会通过。
        """
        pm = RefreshablePositionManager(
            _positions(self.STALE), _positions(self.BROKER),
            dict(self.ACCOUNT_INFO), refresh_ok=False)
        with patch.object(config, 'ENABLE_SIMULATION_MODE', False), \
             patch.object(sdb, '_connect', side_effect=lambda p=None: sqlite3.connect(self.db)):
            sdb.take_snapshot(pm, sdb.SNAPSHOT_CLOSE, snapshot_date='2026-09-14',
                              db_path=self.db)
        self.assertEqual(
            self._mv_gap('2026-09-14'), -14144.0,
            "未刷新时应精确重现 09-14 的 14144.00 缺口（200 股 × 70.72）")


if __name__ == '__main__':
    unittest.main(verbosity=2)
