"""
网格会话巡检测试 - sweep_stale_sessions()

背景（2026-09-21 实盘日志审查发现）:
    数据库里有 6 个 status='active' 的网格会话，但账户只剩 2 只持仓。
    其中 session_id=23 (301218.SZ) 的 end_time=2026-09-21 11:18:52，
    过点后全天没有任何到期日志，一直挂到次日 09:25 盘前同步才被清掉。

根因（两条路径同时失效）:
    1. 持仓监控线程按 positions_df 遍历，已清仓股票的会话拿不到
       check_grid_signals() 调用 —— 而"持仓清空"本身就是退出条件，
       清仓后不再被轮询，形成自锁；
    2. check_grid_signals() 的 `if not session.enabled: return None`
       位于退出检测之前，暂停会话连到期都判不了。

sweep_stale_sessions() 独立于持仓列表运行，补上这两个缺口。

设计约束（必须守住，否则破坏既有语义）:
    暂停会话只参与"到期"检测，不参与"清仓"退出 ——
    ENABLE_PAUSE_GRID_AFTER_TAKE_PROFIT_FULL 的意图是清仓止盈后保留现场
    待人工复核后原样恢复，清仓退出会直接销毁会话，与该意图冲突。
    有效期到了则是硬约束，不冲突。

测试环境:
- Python虚拟环境: C:\\Users\\PC\\Anaconda3\\envs\\python39
- 使用内存数据库 + Mock 持仓管理器，无需真实 QMT
"""

import unittest
import sys
import os
import threading
from datetime import datetime, timedelta
from dataclasses import asdict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import config
from grid_trading_manager import GridTradingManager, GridSession
from grid_database import DatabaseManager


class MockPositionManager:
    """只提供 sweep 需要的两个方法：get_position / _increment_data_version"""

    def __init__(self):
        self.positions = {}
        self.signal_lock = threading.RLock()
        self.latest_signals = {}
        self.get_position_calls = []
        self.raise_on_get_position = False

    def set_position(self, stock_code, volume, cost_price=10.0):
        self.positions[stock_code] = {
            'stock_code': stock_code,
            'volume': volume,
            'can_use_volume': volume,
            'cost_price': cost_price,
            'current_price': cost_price,
            'market_value': cost_price * volume,
        }

    def clear_position(self, stock_code):
        self.positions.pop(stock_code, None)

    def get_position(self, stock_code):
        self.get_position_calls.append(stock_code)
        if self.raise_on_get_position:
            raise RuntimeError("模拟持仓查询失败")
        code = stock_code.split('.')[0] if '.' in stock_code else stock_code
        for key, pos in self.positions.items():
            if (key.split('.')[0] if '.' in key else key) == code:
                return pos
        return None

    def _increment_data_version(self):
        pass


class GridSweepTestBase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.original_simulation = config.ENABLE_SIMULATION_MODE
        cls.original_grid_enabled = config.ENABLE_GRID_TRADING
        cls.original_sweep_interval = getattr(
            config, 'GRID_SESSION_SWEEP_INTERVAL', 60)
        config.ENABLE_SIMULATION_MODE = False
        config.ENABLE_GRID_TRADING = True

    @classmethod
    def tearDownClass(cls):
        config.ENABLE_SIMULATION_MODE = cls.original_simulation
        config.ENABLE_GRID_TRADING = cls.original_grid_enabled
        config.GRID_SESSION_SWEEP_INTERVAL = cls.original_sweep_interval

    def setUp(self):
        config.GRID_SESSION_SWEEP_INTERVAL = 60
        self.db_manager = DatabaseManager(':memory:')
        self.db_manager.init_grid_tables()
        self.position_manager = MockPositionManager()
        self.grid_manager = GridTradingManager(
            db_manager=self.db_manager,
            position_manager=self.position_manager,
            trading_executor=None
        )

    def tearDown(self):
        if hasattr(self, 'db_manager'):
            self.db_manager.close()

    def _make_session(self, stock_code='000001.SZ', enabled=True,
                      end_time=None, volume=1000, status='active'):
        """建一个会话并挂进内存；volume=0 表示该股已清仓"""
        if volume > 0:
            self.position_manager.set_position(stock_code, volume)
        session = GridSession(
            stock_code=stock_code,
            status=status,
            enabled=enabled,
            center_price=10.00,
            current_center_price=10.00,
            price_interval=0.05,
            position_ratio=0.25,
            callback_ratio=0.005,
            max_investment=10000,
            max_deviation=0.15,
            target_profit=0.10,
            stop_loss=-0.10,
            start_time=datetime.now(),
            end_time=end_time if end_time is not None
            else datetime.now() + timedelta(days=7)
        )
        session.id = self.db_manager.create_grid_session(asdict(session))
        key = self.grid_manager._normalize_code(stock_code)
        self.grid_manager.sessions[key] = session
        return session

    def _live_session_ids(self):
        with self.grid_manager.lock:
            return {s.id for s in self.grid_manager.sessions.values()}


class TestSweepExpiry(GridSweepTestBase):
    """到期检测 —— 不论 enabled 与否都该停"""

    def test_paused_expired_session_is_stopped(self):
        """核心回归：301218 场景。

        暂停 + 已过有效期，旧实现全天不处理，要等次日盘前同步。
        """
        session = self._make_session(
            stock_code='301218.SZ', enabled=False, volume=0,
            end_time=datetime.now() - timedelta(minutes=30))

        result = self.grid_manager.sweep_stale_sessions(force=True)

        self.assertEqual(result['expired'], 1)
        self.assertNotIn(session.id, self._live_session_ids(),
                         "已到期的暂停会话必须被停止")

    def test_enabled_expired_session_is_stopped(self):
        session = self._make_session(
            enabled=True, volume=1000,
            end_time=datetime.now() - timedelta(seconds=1))

        result = self.grid_manager.sweep_stale_sessions(force=True)

        self.assertEqual(result['expired'], 1)
        self.assertNotIn(session.id, self._live_session_ids())

    def test_expired_session_without_position_is_stopped(self):
        """已清仓 + 已到期：两条失效路径叠加的情形"""
        session = self._make_session(
            enabled=True, volume=0,
            end_time=datetime.now() - timedelta(hours=2))

        result = self.grid_manager.sweep_stale_sessions(force=True)

        self.assertEqual(result['expired'], 1)
        self.assertNotIn(session.id, self._live_session_ids())

    def test_unexpired_paused_session_survives(self):
        """暂停但未到期 —— 保留现场，不得误停"""
        session = self._make_session(
            enabled=False, volume=1000,
            end_time=datetime.now() + timedelta(days=3))

        result = self.grid_manager.sweep_stale_sessions(force=True)

        self.assertEqual(result['expired'], 0)
        self.assertEqual(result['cleared'], 0)
        self.assertIn(session.id, self._live_session_ids())

    def test_session_without_end_time_is_not_expired(self):
        session = self._make_session(enabled=True, volume=1000)
        with self.grid_manager.lock:
            self.grid_manager.sessions[
                self.grid_manager._normalize_code(session.stock_code)
            ].end_time = None

        result = self.grid_manager.sweep_stale_sessions(force=True)

        self.assertEqual(result['expired'], 0)
        self.assertIn(session.id, self._live_session_ids())


class TestSweepPositionCleared(GridSweepTestBase):
    """清仓检测 —— 只对 enabled=True 生效"""

    def test_cleared_position_stops_after_two_confirmations(self):
        """核心回归：清仓自锁。

        清仓后该股不再出现在持仓列表，check_grid_signals 永远轮不到它，
        position_cleared 退出条件因此永远触发不了。
        """
        session = self._make_session(enabled=True, volume=0)

        first = self.grid_manager.sweep_stale_sessions(force=True)
        self.assertEqual(first['cleared'], 0, "首轮只登记确认，不停止")
        self.assertIn(session.id, self._live_session_ids())

        second = self.grid_manager.sweep_stale_sessions(force=True)
        self.assertEqual(second['cleared'], 1)
        self.assertNotIn(session.id, self._live_session_ids())

    def test_paused_cleared_session_is_never_stopped(self):
        """设计约束：暂停会话不做清仓退出，留给人工复核。"""
        session = self._make_session(enabled=False, volume=0)

        for _ in range(5):
            result = self.grid_manager.sweep_stale_sessions(force=True)
            self.assertEqual(result['cleared'], 0)

        self.assertIn(session.id, self._live_session_ids(),
                      "暂停会话即便已清仓也要保留现场")

    def test_paused_cleared_session_skips_position_query(self):
        """暂停会话不该为清仓判定去查持仓 —— 省掉无谓的 QMT 调用"""
        self._make_session(enabled=False, volume=0)

        self.grid_manager.sweep_stale_sessions(force=True)

        self.assertEqual(self.position_manager.get_position_calls, [])

    def test_session_with_position_survives(self):
        session = self._make_session(enabled=True, volume=1000)

        for _ in range(3):
            result = self.grid_manager.sweep_stale_sessions(force=True)
            self.assertEqual(result['cleared'], 0)

        self.assertIn(session.id, self._live_session_ids())

    def test_position_recovering_resets_confirmation(self):
        """先查到空仓、下一轮又有持仓（查询抖动）→ 确认计数必须清零"""
        session = self._make_session(enabled=True, volume=0)

        self.grid_manager.sweep_stale_sessions(force=True)  # 第1次确认
        self.position_manager.set_position(session.stock_code, 1000)
        self.grid_manager.sweep_stale_sessions(force=True)  # 有持仓 → 复位
        self.position_manager.clear_position(session.stock_code)
        result = self.grid_manager.sweep_stale_sessions(force=True)

        self.assertEqual(result['cleared'], 0,
                         "计数已复位，这应当是新的第1次确认")
        self.assertIn(session.id, self._live_session_ids())

    def test_position_query_failure_skips_session(self):
        """持仓查询异常时跳过该会话，不得当成清仓"""
        session = self._make_session(enabled=True, volume=0)
        self.position_manager.raise_on_get_position = True

        for _ in range(3):
            result = self.grid_manager.sweep_stale_sessions(force=True)
            self.assertEqual(result['cleared'], 0)

        self.assertIn(session.id, self._live_session_ids())


class TestSweepThrottle(GridSweepTestBase):
    """节流与开关"""

    def test_second_call_within_interval_is_skipped(self):
        self._make_session(enabled=True, volume=0)

        self.grid_manager.sweep_stale_sessions()
        result = self.grid_manager.sweep_stale_sessions()

        self.assertEqual(result['checked'], 0, "间隔内不应重复巡检")

    def test_force_bypasses_throttle(self):
        self._make_session(enabled=True, volume=0)

        self.grid_manager.sweep_stale_sessions()
        result = self.grid_manager.sweep_stale_sessions(force=True)

        self.assertEqual(result['checked'], 1)

    def test_zero_interval_disables_sweep(self):
        session = self._make_session(
            enabled=False, volume=0,
            end_time=datetime.now() - timedelta(hours=1))
        config.GRID_SESSION_SWEEP_INTERVAL = 0

        result = self.grid_manager.sweep_stale_sessions()

        self.assertEqual(result['checked'], 0)
        self.assertIn(session.id, self._live_session_ids())

    def test_zero_interval_still_honors_force(self):
        """force 是显式调用（如测试/运维触发），不该被 interval=0 挡住"""
        session = self._make_session(
            enabled=False, volume=0,
            end_time=datetime.now() - timedelta(hours=1))
        config.GRID_SESSION_SWEEP_INTERVAL = 0

        result = self.grid_manager.sweep_stale_sessions(force=True)

        self.assertEqual(result['expired'], 1)
        self.assertNotIn(session.id, self._live_session_ids())


class TestSweepScope(GridSweepTestBase):
    """巡检范围"""

    def test_non_active_session_is_ignored(self):
        self._make_session(
            enabled=True, volume=0, status='stopping',
            end_time=datetime.now() - timedelta(hours=1))

        result = self.grid_manager.sweep_stale_sessions(force=True)

        self.assertEqual(result['checked'], 0, "非 active 会话不参与巡检")

    def test_empty_session_map_returns_zero(self):
        result = self.grid_manager.sweep_stale_sessions(force=True)
        self.assertEqual(result, {'checked': 0, 'expired': 0, 'cleared': 0})

    def test_mixed_sessions_handled_independently(self):
        """一次巡检里混合多种状态，互不干扰"""
        expired_paused = self._make_session(
            stock_code='301218.SZ', enabled=False, volume=0,
            end_time=datetime.now() - timedelta(hours=1))
        healthy = self._make_session(
            stock_code='002859.SZ', enabled=True, volume=500)
        paused_cleared = self._make_session(
            stock_code='300879.SZ', enabled=False, volume=0,
            end_time=datetime.now() + timedelta(days=5))

        result = self.grid_manager.sweep_stale_sessions(force=True)

        self.assertEqual(result['checked'], 3)
        self.assertEqual(result['expired'], 1)
        self.assertEqual(result['cleared'], 0)
        live = self._live_session_ids()
        self.assertNotIn(expired_paused.id, live)
        self.assertIn(healthy.id, live)
        self.assertIn(paused_cleared.id, live)


if __name__ == '__main__':
    unittest.main(verbosity=2)
