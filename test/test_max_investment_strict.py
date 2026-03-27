"""
严格 max_investment 专项测试
=================================
验证三类漏洞修复后，实盘/模拟模式下 current_investment 绝对不超过 max_investment。

V1：executor.buy_stock 改为传 volume+price，不再让 executor 用市价重算股数
V2：DB 加载时修正 current_investment > max_investment
V3：买入前 / 买入后双重硬上限校验
"""
import unittest
import os
import sys
import time
from datetime import datetime
from unittest.mock import Mock, patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from test.test_base import TestBase
from grid_database import DatabaseManager
from grid_trading_manager import GridTradingManager, GridSession, PriceTracker
from trading_executor import TradingExecutor
from position_manager import PositionManager


# ──────────────────────────────────────────────────────────────────────────────
# 测试基类（复用 test_grid_no_repeated_trades.py 中验证过的框架）
# ──────────────────────────────────────────────────────────────────────────────

class MaxInvTestBase(TestBase):
    """max_investment 专项测试基类"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        # 禁用止盈触发要求（本测试专注于max_investment验证）
        config.GRID_REQUIRE_PROFIT_TRIGGERED = False

        self.position_manager = PositionManager()
        self._patch_get_position()

        self.test_db_path = f"data/test_maxinv_{int(time.time()*1000)}.db"
        self.db = DatabaseManager(db_path=self.test_db_path)
        self.db.init_grid_tables()
        self.mock_executor = Mock(spec=TradingExecutor)

        self.grid_manager = GridTradingManager(
            db_manager=self.db,
            position_manager=self.position_manager,
            trading_executor=self.mock_executor,
        )

    def _patch_get_position(self, volume=10000, price=10.0):
        def _get(stock_code):
            return {
                'stock_code': stock_code, 'volume': volume,
                'current_price': price, 'cost_price': 9.5,
                'profit_triggered': False, 'highest_price': 10.5,
                'market_value': volume * price,
            }
        self.position_manager.get_position = _get

    def tearDown(self):
        if hasattr(self, 'grid_manager'):
            for key in list(self.grid_manager.sessions.keys()):
                try:
                    s = self.grid_manager.sessions[key]
                    self.grid_manager.stop_grid_session(s.id, "test_cleanup")
                except Exception:
                    pass
        if hasattr(self, 'position_manager'):
            try:
                self.position_manager.stop_sync_thread()
            except Exception:
                pass
        if hasattr(self, 'db') and self.db:
            self.db.close()
        if hasattr(self, 'test_db_path') and os.path.exists(self.test_db_path):
            try:
                os.remove(self.test_db_path)
            except Exception:
                pass
        super().tearDown()

    def _start_session(self, code='700001.SH', max_investment=10000.0,
                       center=10.0, price_interval=0.05):
        cfg = {
            'center_price': center,
            'price_interval': price_interval,
            'max_investment': max_investment,
            'callback_ratio': 0.005,
            'duration_days': 7,
        }
        session = self.grid_manager.start_grid_session(code, cfg)
        self.assertIsNotNone(session, f"启动会话失败: 返回None")
        self.assertIsInstance(session, GridSession, f"返回对象不是GridSession: {type(session)}")
        return session

    def _make_buy_signal(self, code, session_id, trigger_price, valley_price=None):
        if valley_price is None:
            valley_price = trigger_price * 0.995
        return {
            'stock_code': code, 'signal_type': 'BUY',
            'grid_level': trigger_price * 0.95,
            'trigger_price': trigger_price, 'session_id': session_id,
            'timestamp': datetime.now().isoformat(),
            'valley_price': valley_price, 'callback_ratio': 0.005, 'strategy': 'grid',
        }

    def _force_tracker_waiting_buy(self, session_id, trigger_price):
        with self.grid_manager.lock:
            t = self.grid_manager.trackers[session_id]
            t.waiting_callback = True
            t.direction = 'falling'
            t.valley_price = trigger_price * 0.995
            t.last_price = trigger_price
            t.crossed_level = trigger_price * 1.05


# ──────────────────────────────────────────────────────────────────────────────
# V1：executor.buy_stock 调用方式测试
# ──────────────────────────────────────────────────────────────────────────────

class TestExecutorCalledWithVolumePrice(MaxInvTestBase):
    """V1：验证实盘模式下 executor.buy_stock 必须以 volume+price 调用"""

    def test_V1_1_live_mode_passes_volume_and_price_not_amount(self):
        """V1-1：实盘买入时 executor 收到 volume=N, price=trigger_price，不传 amount"""
        code = '700001.SH'
        session = self._start_session(code, max_investment=20000)
        sid = session.id
        trigger = 9.45

        self._force_tracker_waiting_buy(sid, trigger)
        signal = self._make_buy_signal(code, sid, trigger)

        call_kwargs_record = []

        def mock_buy_stock(**kwargs):
            call_kwargs_record.append(kwargs.copy())
            return {'order_id': 'MOCK_ORDER_001'}

        with patch.object(config, 'ENABLE_SIMULATION_MODE', False), \
             patch.object(self.grid_manager.executor, 'buy_stock',
                          side_effect=mock_buy_stock):
            result = self.grid_manager.execute_grid_trade(signal)

        self.assertTrue(result, "V1-1: 实盘买入应成功")
        self.assertEqual(len(call_kwargs_record), 1, "V1-1: buy_stock 应被调用1次")

        kw = call_kwargs_record[0]
        # 必须有 volume 和 price
        self.assertIn('volume', kw, "V1-1: buy_stock 必须传入 volume")
        self.assertIn('price', kw, "V1-1: buy_stock 必须传入 price")
        # price == trigger_price（不能是 None 让 executor 自己取市价）
        self.assertAlmostEqual(kw['price'], trigger, places=4,
                               msg="V1-1: price 必须等于 trigger_price")
        # volume 为正整数 100 倍数
        vol = kw['volume']
        self.assertGreater(vol, 0, "V1-1: volume > 0")
        self.assertEqual(vol % 100, 0, "V1-1: volume 为100股整数倍")
        # amount 不应出现（防止 executor 用 amount 重新取市价算量）
        self.assertIsNone(kw.get('amount'),
                          f"V1-1: amount 参数不应传给 executor，实际={kw.get('amount')}")

    def test_V1_2_live_mode_volume_matches_gridmanager_calculation(self):
        """V1-2：executor 收到的 volume 等于 grid_manager 内部计算的 volume"""
        code = '700002.SH'
        max_inv = 10000.0
        session = self._start_session(code, max_investment=max_inv)
        sid = session.id
        trigger = 9.45

        self._force_tracker_waiting_buy(sid, trigger)
        signal = self._make_buy_signal(code, sid, trigger)

        # 预期 volume
        remaining = max_inv - session.current_investment
        buy_amount = min(remaining, max_inv * session.position_ratio)
        expected_volume = (int(buy_amount / trigger) // 100) * 100

        received = {'volume': None}

        def mock_buy(**kwargs):
            received['volume'] = kwargs.get('volume')
            return {'order_id': 'MOCK_002'}

        with patch.object(config, 'ENABLE_SIMULATION_MODE', False), \
             patch.object(self.grid_manager.executor, 'buy_stock',
                          side_effect=mock_buy):
            self.grid_manager.execute_grid_trade(signal)

        self.assertEqual(received['volume'], expected_volume,
                         f"V1-2: executor volume={received['volume']} 应等于 {expected_volume}")

    def test_V1_3_tiny_remaining_executor_never_called(self):
        """V1-3：剩余50元不足100股，executor 不应被调用（新 volume+price 方案的关键保障）

        旧方案（传 amount）：executor 内部 volume=0 时强制100股，可能超买
        新方案（传 volume）：volume<100 在 grid_manager 内拦截，executor 完全不参与
        """
        code = '700003.SH'
        max_inv = 10000.0
        session = self._start_session(code, max_investment=max_inv)
        sid = session.id

        # 剩余仅 50 元（不够100股 @ 9.45=945元）
        with self.grid_manager.lock:
            session.current_investment = max_inv - 50.0

        trigger = 9.45
        self._force_tracker_waiting_buy(sid, trigger)
        signal = self._make_buy_signal(code, sid, trigger)

        executor_called = [False]
        def mock_buy(**kwargs):
            executor_called[0] = True
            return {'order_id': 'SHOULD_NOT_REACH'}

        with patch.object(config, 'ENABLE_SIMULATION_MODE', False), \
             patch.object(self.grid_manager.executor, 'buy_stock',
                          side_effect=mock_buy):
            result = self.grid_manager.execute_grid_trade(signal)

        self.assertFalse(result, "V1-3: 剩余50元不足100股，应拒绝")
        self.assertFalse(executor_called[0], "V1-3: executor 不应被调用")
        with self.grid_manager.lock:
            self.assertAlmostEqual(session.current_investment, max_inv - 50.0, places=4,
                                   msg="V1-3: current_investment 不应改变")

    def test_V1_4_amount_overshoot_prevented(self):
        """V1-4：旧方案的超买场景——new 方案已防范

        旧方案（amount）场景重现（通过 mock 验证它确实不会发生）：
          remaining=50, trigger=9.45 → buy_amount=50 → volume=0
          旧 executor: volume==0 → 强制100股 @ 市价 → 实际花 ~945元，超限 895元
          新方案：volume<100 在 grid_manager 层拦截，executor 从未被调用
        """
        code = '700004.SH'
        max_inv = 1000.0
        session = self._start_session(code, max_investment=max_inv)
        sid = session.id

        # 仅剩30元
        with self.grid_manager.lock:
            session.current_investment = max_inv - 30.0

        trigger = 9.45
        self._force_tracker_waiting_buy(sid, trigger)
        signal = self._make_buy_signal(code, sid, trigger)

        # 旧方案 mock：executor 内部强制100股
        old_style_executor_calls = [0]
        def old_style_mock(**kwargs):
            # 模拟旧方案逻辑：amount=30 → volume=0 → 强制100
            amount = kwargs.get('amount', 0)
            forced_vol = 100 if (amount > 0 and int(amount / 9.45 / 100) * 100 == 0) else 0
            old_style_executor_calls[0] += 1
            return {'order_id': 'OLD_STYLE'}

        # 新方案（不传 amount）：executor 不会被调用
        with patch.object(config, 'ENABLE_SIMULATION_MODE', False), \
             patch.object(self.grid_manager.executor, 'buy_stock',
                          side_effect=old_style_mock):
            result = self.grid_manager.execute_grid_trade(signal)

        self.assertFalse(result, "V1-4: 新方案 remaining=30 < 100股，必须拒绝")
        self.assertEqual(old_style_executor_calls[0], 0,
                         "V1-4: 新方案下 executor 完全未被调用（不存在旧方案的 volume 膨胀风险）")
        with self.grid_manager.lock:
            inv = session.current_investment
        self.assertAlmostEqual(inv, max_inv - 30.0, places=4,
                               msg="V1-4: current_investment 不变")


# ──────────────────────────────────────────────────────────────────────────────
# V3：硬上限校验
# ──────────────────────────────────────────────────────────────────────────────

class TestHardCapValidation(MaxInvTestBase):
    """V3：买入前后双重硬上限校验"""

    def test_V3_1_precheck_blocks_when_remaining_insufficient(self):
        """V3-1：前置 HARD CAP：remaining < actual_amount 时直接拦截不下单"""
        code = '700011.SH'
        max_inv = 5000.0
        session = self._start_session(code, max_investment=max_inv)
        sid = session.id

        # remaining = 10 元，100股 @ 9.45 = 945元 >> 10
        with self.grid_manager.lock:
            session.current_investment = max_inv - 10.0
            t = self.grid_manager.trackers[sid]
            t.waiting_callback = True
            t.direction = 'falling'
            t.valley_price = 9.40
            t.last_price = 9.45
            t.crossed_level = 9.45 * 1.05

        signal = self._make_buy_signal(code, sid, 9.45)

        executor_called = [False]
        def mock_buy(**kwargs):
            executor_called[0] = True
            return {'order_id': 'BLOCKED?'}

        with patch.object(config, 'ENABLE_SIMULATION_MODE', False), \
             patch.object(self.grid_manager.executor, 'buy_stock',
                          side_effect=mock_buy):
            result = self.grid_manager.execute_grid_trade(signal)

        self.assertFalse(result, "V3-1: remaining=10 < 945，HARD CAP 拦截")
        self.assertFalse(executor_called[0], "V3-1: 拦截后不调用 executor")
        with self.grid_manager.lock:
            self.assertAlmostEqual(session.current_investment, max_inv - 10.0, places=4,
                                   msg="V3-1: current_investment 不变")

    def test_V3_2_successive_buys_never_exceed_max(self):
        """V3-2：连续买入（模拟）后，current_investment 严格 <= max_investment"""
        code = '700012.SH'
        max_inv = 8000.0
        session = self._start_session(code, max_investment=max_inv)
        sid = session.id

        for i in range(20):
            trigger = 9.45 - i * 0.3
            if trigger <= 0:
                break

            with self.grid_manager.lock:
                if sid not in self.grid_manager.trackers:
                    break
                if session.current_investment >= max_inv:
                    break
                t = self.grid_manager.trackers[sid]
                t.waiting_callback = True
                t.direction = 'falling'
                t.valley_price = trigger * 0.995
                t.last_price = trigger
                t.crossed_level = trigger * 1.05

            signal = self._make_buy_signal(code, sid, trigger)
            with patch.object(config, 'ENABLE_SIMULATION_MODE', True):
                self.grid_manager.execute_grid_trade(signal)

            with self.grid_manager.lock:
                inv = session.current_investment
            self.assertLessEqual(
                inv, max_inv + 0.01,
                f"V3-2: 第{i+1}次 current_investment={inv:.4f} > max={max_inv}"
            )

    def test_V3_3_exactly_at_max_blocked(self):
        """V3-3：current_investment == max_investment 时任何买入都被拒绝"""
        code = '700013.SH'
        max_inv = 5000.0
        session = self._start_session(code, max_investment=max_inv)
        sid = session.id

        with self.grid_manager.lock:
            session.current_investment = max_inv

        for attempt in range(5):
            with self.grid_manager.lock:
                if sid not in self.grid_manager.trackers:
                    break
                t = self.grid_manager.trackers[sid]
                t.waiting_callback = True
                t.direction = 'falling'
                t.valley_price = 9.40
                t.last_price = 9.45
                t.crossed_level = 9.99

            signal = self._make_buy_signal(code, sid, 9.45)
            with patch.object(config, 'ENABLE_SIMULATION_MODE', True):
                result = self.grid_manager.execute_grid_trade(signal)
            self.assertFalse(result, f"V3-3: 第{attempt+1}次 current_investment==max 必须拒绝")

        with self.grid_manager.lock:
            inv = session.current_investment
        self.assertAlmostEqual(inv, max_inv, places=4,
                               msg="V3-3: current_investment 不应增加")

    def test_V3_4_float_precision_no_overshoot(self):
        """V3-4：非整元价格产生浮点小数时，current_investment 仍不超限"""
        code = '700014.SH'
        max_inv = 7777.0
        session = self._start_session(code, max_investment=max_inv)
        sid = session.id

        for i in range(12):
            trigger = 9.47 - i * 0.07  # 非整数价格
            if trigger <= 0:
                break
            with self.grid_manager.lock:
                if sid not in self.grid_manager.trackers:
                    break
                if session.current_investment >= max_inv:
                    break
                t = self.grid_manager.trackers[sid]
                t.waiting_callback = True
                t.direction = 'falling'
                t.valley_price = trigger * 0.995
                t.last_price = trigger
                t.crossed_level = trigger * 1.05

            signal = self._make_buy_signal(code, sid, trigger)
            with patch.object(config, 'ENABLE_SIMULATION_MODE', True):
                self.grid_manager.execute_grid_trade(signal)

            with self.grid_manager.lock:
                inv = session.current_investment
            self.assertLessEqual(
                inv, max_inv + 0.01,
                f"V3-4: 第{i+1}次(price={trigger:.2f}) current_investment={inv:.6f} > {max_inv}"
            )

    def test_V3_5_investment_accurately_tracked_after_live_buy(self):
        """V3-5：实盘买入成功后，current_investment 精确增加 volume * trigger_price"""
        code = '700015.SH'
        max_inv = 20000.0
        session = self._start_session(code, max_investment=max_inv)
        sid = session.id
        trigger = 9.47

        before_inv = session.current_investment
        self._force_tracker_waiting_buy(sid, trigger)
        signal = self._make_buy_signal(code, sid, trigger)

        # 预计算
        remaining = max_inv - before_inv
        buy_amount = min(remaining, max_inv * session.position_ratio)
        expected_volume = (int(buy_amount / trigger) // 100) * 100
        expected_delta = expected_volume * trigger

        def mock_buy(**kwargs):
            return {'order_id': 'LIVE_001'}

        with patch.object(config, 'ENABLE_SIMULATION_MODE', False), \
             patch.object(self.grid_manager.executor, 'buy_stock',
                          side_effect=mock_buy):
            result = self.grid_manager.execute_grid_trade(signal)

        self.assertTrue(result, "V3-5: 实盘买入应成功")
        with self.grid_manager.lock:
            after_inv = session.current_investment
        delta = after_inv - before_inv
        self.assertAlmostEqual(delta, expected_delta, places=4,
                               msg=f"V3-5: 投入增量={delta:.4f} 应等于 volume*price={expected_delta:.4f}")
        self.assertLessEqual(after_inv, max_inv + 0.01,
                             f"V3-5: 买入后不超限 {after_inv:.4f} <= {max_inv}")


# ──────────────────────────────────────────────────────────────────────────────
# V2：DB 加载时 current_investment > max_investment 修正
# ──────────────────────────────────────────────────────────────────────────────

class TestDBLoadConsistency(MaxInvTestBase):
    """V2：DB 加载时 current_investment 超限修正"""

    def _inject_db_investment(self, session_id, value):
        """直接写 DB 注入 current_investment 值（模拟 DB 写入不一致场景）"""
        import sqlite3
        conn = sqlite3.connect(self.test_db_path)
        try:
            conn.execute("UPDATE grid_trading_sessions SET current_investment=? WHERE id=?",
                         (value, session_id))
            conn.commit()
        finally:
            conn.close()

    def _reload_grid_manager(self):
        """丢弃当前 grid_manager 的内存状态，创建新实例模拟进程重启（DB 状态保留）"""
        # 不调用 stop_grid_session，只丢弃内存对象（模拟进程崩溃重启场景）
        # 停止 position_manager 同步线程（避免线程泄漏）
        try:
            self.position_manager.stop_sync_thread()
        except Exception:
            pass

        # 关闭旧 DB 连接
        try:
            self.db.close()
        except Exception:
            pass

        # 创建新 position_manager（模拟重启）
        self.position_manager = PositionManager()
        self._patch_get_position()

        new_db = DatabaseManager(db_path=self.test_db_path)
        new_db.init_grid_tables()
        new_gm = GridTradingManager(
            db_manager=new_db,
            position_manager=self.position_manager,
            trading_executor=self.mock_executor,
        )
        return new_gm, new_db

    def test_V2_1_db_overshoot_capped_on_load(self):
        """V2-1：DB 中 current_investment > max_investment，加载后被修正至 max"""
        code = '700021.SH'
        max_inv = 5000.0
        session = self._start_session(code, max_investment=max_inv)
        sid = session.id

        # 注入超限值
        injected = max_inv + 2000.0
        self._inject_db_investment(sid, injected)

        # 模拟重启
        new_gm, new_db = self._reload_grid_manager()

        try:
            restored = None
            for s in new_gm.sessions.values():
                if s.stock_code == code:
                    restored = s
                    break

            if restored is None:
                self.skipTest("V2-1: 会话未被恢复")

            self.assertLessEqual(
                restored.current_investment, max_inv + 0.01,
                f"V2-1: 加载后 current_investment={restored.current_investment:.2f} "
                f"应被修正至 max_investment={max_inv}"
            )
        finally:
            for s in list(new_gm.sessions.values()):
                try:
                    new_gm.stop_grid_session(s.id, 'test_cleanup')
                except Exception:
                    pass
            new_db.close()

    def test_V2_2_normal_db_value_unchanged(self):
        """V2-2：DB 中 current_investment <= max_investment，加载后值不变"""
        code = '700022.SH'
        max_inv = 5000.0
        session = self._start_session(code, max_investment=max_inv)
        sid = session.id

        normal_value = max_inv * 0.6
        self._inject_db_investment(sid, normal_value)

        new_gm, new_db = self._reload_grid_manager()

        try:
            restored = None
            for s in new_gm.sessions.values():
                if s.stock_code == code:
                    restored = s
                    break

            if restored is None:
                self.skipTest("V2-2: 会话未被恢复")

            self.assertAlmostEqual(
                restored.current_investment, normal_value, places=2,
                msg=f"V2-2: 正常值 {normal_value} 不应被修改"
            )
        finally:
            for s in list(new_gm.sessions.values()):
                try:
                    new_gm.stop_grid_session(s.id, 'test_cleanup')
                except Exception:
                    pass
            new_db.close()

    def test_V2_3_after_cap_no_buy_allowed(self):
        """V2-3：DB 超限修正 → 修正后 current_investment==max → 任何买入都被拒绝"""
        code = '700023.SH'
        max_inv = 5000.0
        session = self._start_session(code, max_investment=max_inv)
        sid = session.id

        # 注入超限（整个 max 都"已用"，实际多出500）
        self._inject_db_investment(sid, max_inv + 500.0)

        new_gm, new_db = self._reload_grid_manager()

        try:
            restored = None
            for s in new_gm.sessions.values():
                if s.stock_code == code:
                    restored = s
                    break

            if restored is None:
                self.skipTest("V2-3: 会话未被恢复")

            new_sid = restored.id
            # 修正后应 == max_investment
            self.assertAlmostEqual(restored.current_investment, max_inv, delta=0.01,
                                   msg="V2-3: 修正后应等于 max_investment")

            # 尝试买入，必须被拒
            if new_sid in new_gm.trackers:
                with new_gm.lock:
                    t = new_gm.trackers[new_sid]
                    t.waiting_callback = True
                    t.direction = 'falling'
                    t.valley_price = 9.40
                    t.last_price = 9.45
                    t.crossed_level = 9.99

            buy_signal = {
                'stock_code': code, 'signal_type': 'BUY',
                'grid_level': 9.50, 'trigger_price': 9.45, 'session_id': new_sid,
                'timestamp': datetime.now().isoformat(),
                'valley_price': 9.40, 'callback_ratio': 0.005, 'strategy': 'grid',
            }
            with patch.object(config, 'ENABLE_SIMULATION_MODE', True):
                blocked = new_gm.execute_grid_trade(buy_signal)

            self.assertFalse(blocked, "V2-3: 修正后 current_investment==max，买入必须被拒")
        finally:
            for s in list(new_gm.sessions.values()):
                try:
                    new_gm.stop_grid_session(s.id, 'test_cleanup')
                except Exception:
                    pass
            new_db.close()

    def test_V2_4_db_equal_max_not_capped(self):
        """V2-4：DB 中 current_investment == max_investment（边界），不应修改也不应超买"""
        code = '700024.SH'
        max_inv = 5000.0
        session = self._start_session(code, max_investment=max_inv)
        sid = session.id

        # 恰好等于 max（合法，如最后一次买入后精确用完）
        self._inject_db_investment(sid, max_inv)

        new_gm, new_db = self._reload_grid_manager()

        try:
            restored = None
            for s in new_gm.sessions.values():
                if s.stock_code == code:
                    restored = s
                    break

            if restored is None:
                self.skipTest("V2-4: 会话未被恢复")

            self.assertAlmostEqual(
                restored.current_investment, max_inv, places=4,
                msg="V2-4: current_investment==max_investment 不应被修改"
            )
        finally:
            for s in list(new_gm.sessions.values()):
                try:
                    new_gm.stop_grid_session(s.id, 'test_cleanup')
                except Exception:
                    pass
            new_db.close()


# ──────────────────────────────────────────────────────────────────────────────
# 综合端到端：模拟多种极端场景，确保任何情况下都不超限
# ──────────────────────────────────────────────────────────────────────────────

class TestMaxInvestmentEndToEnd(MaxInvTestBase):
    """端到端：极端场景综合验证"""

    def test_E2E_1_boundary_remaining_less_than_one_lot(self):
        """E2E-1：剩余额度 < 100股成本时，买入被拒且 current_investment 不增"""
        for remaining_yuan, trigger_price in [
            (50, 9.45),   # 50/9.45 ~ 5 股
            (80, 9.45),   # 80/9.45 ~ 8 股
            (949, 9.50),  # 949/9.50 ~ 99.9 股
        ]:
            with self.subTest(remaining=remaining_yuan, price=trigger_price):
                code = f'700031{remaining_yuan}.SH'
                # code 限制长度，简化
                code = '700031.SH'
                max_inv = 10000.0
                session = self._start_session(code, max_investment=max_inv)
                sid = session.id

                with self.grid_manager.lock:
                    session.current_investment = max_inv - remaining_yuan
                    if sid in self.grid_manager.trackers:
                        t = self.grid_manager.trackers[sid]
                        t.waiting_callback = True
                        t.direction = 'falling'
                        t.valley_price = trigger_price * 0.995
                        t.last_price = trigger_price
                        t.crossed_level = trigger_price * 1.05

                signal = self._make_buy_signal(code, sid, trigger_price)
                before_inv = max_inv - remaining_yuan

                executor_called = [False]
                def mock_buy(**kwargs):
                    executor_called[0] = True
                    return {'order_id': 'OVER?'}

                with patch.object(config, 'ENABLE_SIMULATION_MODE', False), \
                     patch.object(self.grid_manager.executor, 'buy_stock',
                                  side_effect=mock_buy):
                    result = self.grid_manager.execute_grid_trade(signal)

                with self.grid_manager.lock:
                    after_inv = session.current_investment

                # 计算期望：如果理论上能买100股则应成功，否则失败
                expected_vol = (int(min(remaining_yuan, max_inv * session.position_ratio) / trigger_price) // 100) * 100
                if expected_vol >= 100:
                    # 可以买，但不超限
                    self.assertLessEqual(after_inv, max_inv + 0.01,
                                         f"remaining={remaining_yuan} 买入后不超限")
                else:
                    # 不够买，被拒
                    self.assertFalse(result, f"remaining={remaining_yuan} < 100股，应拒绝")
                    self.assertFalse(executor_called[0], "不应调用 executor")
                    self.assertAlmostEqual(after_inv, before_inv, places=4,
                                           msg="investment 不应变化")

                # 无论如何不超限
                self.assertLessEqual(after_inv, max_inv + 0.01,
                                     f"E2E-1: remaining={remaining_yuan} 后 current_investment={after_inv:.4f} 不超 {max_inv}")

                # 停止会话供下次 subTest 重用
                self.grid_manager.stop_grid_session(sid, 'subtest_cleanup')

    def test_E2E_2_max_zero_no_buy(self):
        """E2E-2：max_investment=0 时，任何买入都被立即拒绝"""
        code = '700032.SH'
        session = self._start_session(code, max_investment=0.0)
        sid = session.id

        self._force_tracker_waiting_buy(sid, 9.45)
        signal = self._make_buy_signal(code, sid, 9.45)

        with patch.object(config, 'ENABLE_SIMULATION_MODE', True):
            result = self.grid_manager.execute_grid_trade(signal)

        self.assertFalse(result, "E2E-2: max_investment=0 时买入必须被拒绝")
        with self.grid_manager.lock:
            self.assertAlmostEqual(session.current_investment, 0.0, places=4,
                                   msg="E2E-2: current_investment 不应改变")


if __name__ == '__main__':
    unittest.main(verbosity=2)
