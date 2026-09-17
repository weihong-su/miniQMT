"""
持仓缓存失效专项测试（P0 假空窗口回归）
=====================================

复现并守护 2026-09-16 实盘日志暴露的 P0 缺陷：

    10:39:01  [POSITION_REFRESH] 301085 持仓快刷，当前缓存 2 条
    10:39:02  301085 首次突破止盈阈值 5.50%          ← _mark_profit_breakout: positions_cache = None
    10:39:02  [GRID] 301085.SZ 首次检测到持仓为空     ← 实际持有 800 股
    10:39:02  [GRID] 002859.SZ 首次检测到持仓为空     ← 实际持有 1200 股
    （随后监控循环走 positions_df.empty -> sleep(60)，静默停摆 168 秒）

根因：`positions_cache = None` 未同步复位 `last_position_update_time`。
get_all_positions() 只有在距上次刷新 >= position_update_interval(10秒) 时才重建缓存，
否则直接走 `positions_cache is None -> return pd.DataFrame()`，于是**所有**股票
在最长 10 秒内都表现为"无持仓"。

测试分组：
    A - 四个缓存失效点：失效后立即读取必须拿到真实持仓（修复前全部失败）
    B - _invalidate_positions_cache 自身语义
    C - 端到端复现 09-16 时序（两只股票同时假空 + 监控循环空转）
    D - 设计约束守卫：源码中不得再出现裸的 positions_cache = None
    E - 不得破坏 BUG-1 原意：失效后必须读到刚写入的新状态
"""

import unittest
import sqlite3
import os
import sys
import threading
import time
import re
from datetime import datetime
from unittest.mock import MagicMock, patch
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import mootdx.quotes  # noqa: F401
except ModuleNotFoundError:
    mock_mootdx = MagicMock()
    mock_mootdx_quotes = MagicMock()
    mock_mootdx_quotes.Quotes.factory.return_value = MagicMock()
    sys.modules.setdefault('mootdx', mock_mootdx)
    sys.modules.setdefault('mootdx.quotes', mock_mootdx_quotes)

import config
from position_manager import PositionManager

POSITION_MANAGER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'position_manager.py')

_CREATE_POSITIONS_SQL = """
    CREATE TABLE IF NOT EXISTS positions (
        stock_code TEXT PRIMARY KEY,
        stock_name TEXT,
        volume REAL,
        available REAL,
        cost_price REAL,
        base_cost_price REAL,
        current_price REAL,
        market_value REAL,
        profit_ratio REAL,
        last_update TIMESTAMP,
        open_date TIMESTAMP,
        profit_triggered BOOLEAN DEFAULT FALSE,
        highest_price REAL,
        stop_loss_price REAL,
        profit_breakout_triggered BOOLEAN DEFAULT FALSE,
        breakout_highest_price REAL
    )
"""


def _insert_position(conn, stock_code, volume, cost_price=10.0, current_price=None):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    current_price = current_price if current_price is not None else cost_price
    conn.execute("""
        INSERT OR REPLACE INTO positions
            (stock_code, stock_name, volume, available, cost_price, base_cost_price,
             current_price, market_value, profit_ratio, last_update, open_date,
             profit_triggered, highest_price, stop_loss_price,
             profit_breakout_triggered, breakout_highest_price)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        stock_code, stock_code, volume, volume, cost_price, cost_price,
        current_price, volume * current_price, 0.0, now, now,
        False, current_price, cost_price * 0.93, False, current_price,
    ))
    conn.commit()


class _CacheTestBase(unittest.TestCase):
    """构造只带缓存相关依赖的最小 PositionManager 桩。

    不启动真实 PositionManager（需要 QMT 环境），而是把待测的真实方法绑定到
    裸对象上，这样测的就是生产代码本身。
    """

    STOCKS = (("301085", 800, 72.20), ("002859", 1200, 66.19))

    def setUp(self):
        self.memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.memory_conn.row_factory = sqlite3.Row
        self.memory_conn.execute(_CREATE_POSITIONS_SQL)
        self.memory_conn.commit()
        for code, volume, cost in self.STOCKS:
            _insert_position(self.memory_conn, code, volume, cost)

        self.pm = self._make_pm()

    def tearDown(self):
        self.memory_conn.close()

    def _make_pm(self):
        class FakePM:
            pass

        pm = FakePM()
        pm.memory_conn = self.memory_conn
        pm.memory_conn_lock = threading.Lock()
        pm.positions_cache = None
        pm.last_position_update_time = 0
        pm.position_update_interval = config.QMT_POSITION_QUERY_INTERVAL
        pm.empty_real_position_count = 0
        pm.data_version = 0

        # QMT 返回值不为空即可：缓存内容始终来自内存表，
        # _sync_real_positions_to_memory 被打桩掉（不是本用例关注点）。
        self.qmt_position_calls = []

        def _fake_position():
            self.qmt_position_calls.append(time.time())
            return pd.DataFrame([{
                '证券代码': code, '证券名称': code, '股票余额': volume,
                '可用余额': volume, '成本价': cost,
            } for code, volume, cost in self.STOCKS])

        pm.qmt_trader = MagicMock()
        pm.qmt_trader.position = _fake_position
        pm._sync_real_positions_to_memory = MagicMock()
        pm._handle_empty_real_positions = MagicMock()
        pm._increment_data_version = MagicMock()

        for name in ('_invalidate_positions_cache', 'get_all_positions', 'get_position',
                     '_mark_profit_breakout', '_reset_profit_breakout',
                     'mark_profit_triggered'):
            method = getattr(PositionManager, name, None)
            if method is not None:
                setattr(pm, name, method.__get__(pm, FakePM))

        if not hasattr(pm, '_invalidate_positions_cache'):
            # 修复前的代码没有这个方法。装上旧写法的等价实现，好让 A/B/C 组
            # 以"读到假空持仓"的形式失败，而不是以 AttributeError 掩盖真因。
            def _legacy_invalidate(reason=""):
                pm.positions_cache = None
            pm._invalidate_positions_cache = _legacy_invalidate
        return pm

    def _warm_cache(self):
        """把缓存刷成"刚刚更新过"的状态——即 TTL 保护区内。"""
        with patch.object(config, 'ENABLE_SIMULATION_MODE', False):
            df = self.pm.get_all_positions()
        self.assertFalse(df.empty, "前置条件：预热后缓存应有持仓")
        self.assertGreater(self.pm.last_position_update_time, 0)
        return df

    def _read_positions(self):
        with patch.object(config, 'ENABLE_SIMULATION_MODE', False):
            return self.pm.get_all_positions()


class TestInvalidationPointsReturnRealPositions(_CacheTestBase):
    """A 组：四个缓存失效点失效后，立即读取必须仍拿到真实持仓。

    修复前这四个用例全部失败（返回空 DataFrame）。
    """

    def _assert_not_blind(self, action_desc):
        df = self._read_positions()
        self.assertFalse(
            df.empty,
            f"{action_desc} 后立即读取持仓不得为空——这正是 P0 假空窗口（实盘停摆 168 秒）")
        self.assertEqual(len(df), len(self.STOCKS),
                         f"{action_desc} 后应读到全部 {len(self.STOCKS)} 只持仓")

    def test_A1_mark_profit_breakout(self):
        """_mark_profit_breakout（09-16 实盘触发点）"""
        self._warm_cache()
        self.assertTrue(self.pm._mark_profit_breakout("301085", 76.32))
        self._assert_not_blind("_mark_profit_breakout")

    def test_A2_mark_profit_triggered(self):
        """mark_profit_triggered（首次止盈成交回报后调用）"""
        self._warm_cache()
        self.assertTrue(self.pm.mark_profit_triggered("301085"))
        self._assert_not_blind("mark_profit_triggered")

    def test_A3_reset_profit_breakout(self):
        """_reset_profit_breakout（跨日清除突破状态）"""
        self._warm_cache()
        self.pm._mark_profit_breakout("301085", 76.32)
        self.assertTrue(self.pm._reset_profit_breakout("301085", reason="跨日"))
        self._assert_not_blind("_reset_profit_breakout")

    def test_A4_initialize_all_positions_data(self):
        """initialize_all_positions_data（Web API 可触发）"""
        self._warm_cache()
        self.pm._invalidate_positions_cache("初始化全部持仓数据")
        self._assert_not_blind("initialize_all_positions_data 的缓存清理")

    def test_A5_get_position_not_none_after_invalidation(self):
        """网格 _check_exit_conditions 的读取口径：get_position 不得返回 None"""
        self._warm_cache()
        self.pm._mark_profit_breakout("301085", 76.32)

        with patch.object(config, 'ENABLE_SIMULATION_MODE', False):
            for code, volume, _ in self.STOCKS:
                position = self.pm.get_position(code)
                self.assertIsNotNone(
                    position, f"{code} 失效后 get_position 返回 None —— 网格会误判持仓为空")
                self.assertEqual(position['volume'], volume,
                                 f"{code} 持仓数量应为 {volume}")


class TestInvalidateHelperSemantics(_CacheTestBase):
    """B 组：_invalidate_positions_cache 自身语义"""

    def test_B1_resets_both_cache_and_ttl(self):
        """必须同时置空缓存并复位 TTL —— 两者缺一即重现 P0"""
        self._warm_cache()
        self.assertGreater(self.pm.last_position_update_time, 0)

        self.pm._invalidate_positions_cache("单测")

        self.assertIsNone(self.pm.positions_cache, "缓存应被置空")
        self.assertEqual(self.pm.last_position_update_time, 0,
                         "TTL 必须复位为 0，否则下次读取会在 TTL 保护区内返回空表")

    def test_B2_forces_real_reload(self):
        """失效后下一次读取必须真正回源，而不是走 TTL 短路"""
        self._warm_cache()
        calls_before = len(self.qmt_position_calls)

        self.pm._invalidate_positions_cache("单测")
        self._read_positions()

        self.assertEqual(len(self.qmt_position_calls), calls_before + 1,
                         "失效后应触发一次真实回源查询")

    def test_B3_ttl_still_effective_without_invalidation(self):
        """不调用失效时 TTL 仍生效（修复不得让缓存失去节流作用）"""
        self._warm_cache()
        calls_before = len(self.qmt_position_calls)

        for _ in range(5):
            df = self._read_positions()
            self.assertFalse(df.empty)

        self.assertEqual(len(self.qmt_position_calls), calls_before,
                         "TTL 未到期时不应重复查询 QMT —— 节流行为必须保留")


class TestSep16ScenarioRegression(_CacheTestBase):
    """C 组：端到端复现 2026-09-16 10:39:02 的实盘时序"""

    def test_C1_two_stocks_not_blind_simultaneously(self):
        """301085 触发首次突破后，它和 002859 都必须仍可见（缓存是全局的）"""
        self._warm_cache()  # 对应 10:39:01 [POSITION_REFRESH] 缓存 2 条

        # 10:39:02.717 301085 首次突破止盈阈值 -> _mark_profit_breakout
        self.pm._mark_profit_breakout("301085", 76.32)

        # 10:39:02.725 / .733 网格依次检查两只股票的持仓
        with patch.object(config, 'ENABLE_SIMULATION_MODE', False):
            blind = [code for code, _, _ in self.STOCKS
                     if self.pm.get_position(code) is None]

        self.assertEqual(blind, [],
                         f"以下股票被误判为持仓为空: {blind}（09-16 实盘为 301085/002859 两只同时假空）")

    def test_C2_monitor_loop_does_not_go_empty(self):
        """模拟监控循环连续多轮：任何一轮都不得读到空持仓而进入 sleep(60) 分支"""
        self._warm_cache()

        empty_rounds = []
        for round_no in range(1, 4):
            positions_df = self._read_positions()
            if positions_df.empty:
                empty_rounds.append(round_no)
                continue
            # 每轮循环内标记一次突破，等同实盘中 _detect_and_enqueue_dynamic_signal 的行为
            self.pm._mark_profit_breakout("301085", 76.32 + round_no)

        self.assertEqual(empty_rounds, [],
                         f"第 {empty_rounds} 轮监控读到空持仓 —— 将触发 sleep(60) 静默停摆")

    def test_C3_blind_window_reproduced_by_legacy_behavior(self):
        """反向验证：还原旧写法（只置 None 不复位 TTL）必须重现假空。

        这个用例保证 A/C 组不是假阳性——如果读取路径本身永远不会返回空，
        那么 A/C 组即使没有修复也会通过。
        """
        self._warm_cache()

        # 旧代码的等价写法
        self.pm.positions_cache = None

        df = self._read_positions()
        self.assertTrue(
            df.empty,
            "旧写法未能重现假空——说明测试前提已失效，A/C 组的保护力需重新评估")


class TestNoBareCacheAssignment(unittest.TestCase):
    """D 组：设计约束守卫，防止未来再引入裸的 positions_cache = None"""

    def test_D1_no_bare_assignment_outside_allowed_scopes(self):
        with open(POSITION_MANAGER_PATH, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        offenders = []
        for idx, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith('#'):
                continue
            if re.match(r'^self\.positions_cache\s*=\s*None$', stripped):
                offenders.append((idx, stripped))

        # 允许两处：__init__ 的初始化、_invalidate_positions_cache 的实现本体
        allowed_context = ('def __init__', 'def _invalidate_positions_cache')
        real_offenders = []
        for line_no, text in offenders:
            enclosing = ''
            for back in range(line_no - 1, max(0, line_no - 60), -1):
                if lines[back - 1].lstrip().startswith('def '):
                    enclosing = lines[back - 1].strip()
                    break
            if not any(enclosing.startswith(a) for a in allowed_context):
                real_offenders.append(f"第{line_no}行 ({enclosing})")

        self.assertEqual(
            real_offenders, [],
            "发现裸的 positions_cache = None："
            + "; ".join(real_offenders)
            + " —— 请改用 _invalidate_positions_cache()，否则会重现 P0 假空窗口")


class TestBug1IntentPreserved(_CacheTestBase):
    """E 组：修复不得破坏 BUG-1 的原始意图（失效后要读到刚写入的新状态）"""

    def test_E1_breakout_flag_visible_immediately(self):
        """标记突破后，下一次读取必须看到 profit_breakout_triggered=True"""
        self._warm_cache()
        self.pm._mark_profit_breakout("301085", 76.32)

        df = self._read_positions()
        row = df[df['stock_code'] == "301085"].iloc[0]
        self.assertTrue(bool(row['profit_breakout_triggered']),
                        "失效后应立即读到新的突破标记（BUG-1 原意）")
        self.assertAlmostEqual(float(row['breakout_highest_price']), 76.32, places=2)

    def test_E2_profit_triggered_flag_visible_immediately(self):
        """标记首次止盈后，下一次读取必须看到 profit_triggered=True"""
        self._warm_cache()
        self.pm.mark_profit_triggered("301085")

        df = self._read_positions()
        row = df[df['stock_code'] == "301085"].iloc[0]
        self.assertTrue(bool(row['profit_triggered']),
                        "失效后应立即读到新的止盈标记（BUG-1 原意）")


if __name__ == '__main__':
    unittest.main(verbosity=2)
