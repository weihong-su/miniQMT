"""运行优化回归：模拟账户与内存执行器，禁止真实下单。"""
import json
import os
import threading
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import config
from test.test_grid_mece_regression import GridMeceRegressionBase
from test.test_ipc_full_chain import _IpcSnapshotBase


class TestGridRuntimeOptimization(GridMeceRegressionBase):
    def setUp(self):
        super().setUp()
        config.ENABLE_SIMULATION_MODE = True
        self.addCleanup(patch.stopall)

    def _live_mock(self, volume=2900):
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        self.executor.buy_stock.return_value = {"order_id": "MOCK_BUY"}
        self.executor.sell_stock.return_value = {"order_id": "MOCK_SELL"}
        self.position_manager.get_position.return_value.update(volume=volume, available=volume)
        return self.make_session(max_investment=100000)

    def test_actual_buy_entry_honors_fixed_shares(self):
        session = self._live_mock()
        session.trade_mode, session.fixed_volume = "shares", 1500
        self.assertTrue(self.manager.execute_grid_trade(self.buy_signal(session)))
        self.assertEqual(self.executor.buy_stock.call_args.kwargs["volume"], 1500)

    def test_actual_sell_entry_honors_fixed_shares(self):
        session = self._live_mock()
        session.trade_mode, session.fixed_volume = "shares", 1500
        self.assertTrue(self.manager.execute_grid_trade(self.sell_signal(session, 10)))
        self.assertEqual(self.executor.sell_stock.call_args.kwargs["volume"], 1500)

    def test_fixed_sell_clips_to_available_whole_lots(self):
        session = self._live_mock()
        session.trade_mode, session.fixed_volume = "shares", 1500
        self.position_manager.get_position.return_value["available"] = 250
        self.assertTrue(self.manager.execute_grid_trade(self.sell_signal(session, 10)))
        self.assertEqual(self.executor.sell_stock.call_args.kwargs["volume"], 200)

    def test_fixed_buy_rejects_budget_smaller_than_configured_quantity(self):
        session = self._live_mock()
        session.trade_mode, session.fixed_volume = "shares", 1500
        session.max_investment = 10000
        self.assertFalse(self.manager.execute_grid_trade(self.buy_signal(session)))
        self.executor.buy_stock.assert_not_called()

    def test_fixed_buy_rejects_invalid_quantity(self):
        session = self._live_mock()
        session.trade_mode, session.fixed_volume = "shares", 50
        self.assertFalse(self.manager.execute_grid_trade(self.buy_signal(session)))
        self.executor.buy_stock.assert_not_called()

    def test_amount_mode_keeps_position_ratio_quantity(self):
        session = self._live_mock()
        self.assertTrue(self.manager.execute_grid_trade(self.buy_signal(session)))
        self.assertEqual(self.executor.buy_stock.call_args.kwargs["volume"], 700)

    def test_budget_shortfall_does_not_generate_repeated_buy_signals(self):
        session = self.make_session(max_investment=40000, current_investment=38019)
        session.center_price = session.current_center_price = 51.93
        with patch.object(self.manager, "_check_exit_conditions", return_value=None):
            for price in (46.0, 46.3, 45.7, 46.1, 45.8, 46.2):
                self.assertIsNone(self.manager.check_grid_signals("000001", price))
        state = self.manager.get_buy_budget_status("000001")
        self.assertTrue(state["blocked"])
        self.assertEqual(state["remaining"], 1981)
        self.assertGreater(state["required_amount"], state["remaining"])
        self.executor.buy_stock.assert_not_called()

    def test_budget_release_reenables_buy_without_changing_switch(self):
        session = self.make_session(max_investment=40000, current_investment=38019)
        session.center_price = session.current_center_price = 51.93
        with patch.object(self.manager, "_check_exit_conditions", return_value=None):
            self.manager.check_grid_signals("000001", 46)
            session.current_investment = 30000
            self.manager.check_grid_signals("000001", 45.8)
            signal = self.manager.check_grid_signals("000001", 46.1)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["signal_type"], "BUY")
        self.assertFalse(self.manager.get_buy_budget_status("000001")["blocked"])
        self.assertTrue(session.enabled)

    def test_budget_shortfall_does_not_disable_sell(self):
        session = self.make_session(max_investment=40000, current_investment=38019)
        session.center_price = session.current_center_price = 51.93
        with patch.object(self.manager, "_check_exit_conditions", return_value=None):
            self.manager.check_grid_signals("000001", 55)
            signal = self.manager.check_grid_signals("000001", 54.5)
        self.assertEqual(signal["signal_type"], "SELL")

    def test_quote_detail_lookup_happens_outside_the_grid_lock(self):
        self.make_session(max_investment=40000)
        def inspect_lock(*args, **kwargs):
            self.assertFalse(self.manager.lock._is_owned())
            return 10.0
        self.manager._calculate_buy_reserved_price = Mock(side_effect=inspect_lock)
        with patch.object(self.manager,"_check_exit_conditions",return_value=None):
            self.manager.check_grid_signals("000001",10)
        self.manager._calculate_buy_reserved_price.assert_called_once()

    def test_pending_budget_is_included_in_signal_gate(self):
        session = self.make_session(max_investment=40000, current_investment=35000)
        session.center_price = session.current_center_price = 51.93
        self.manager.submitting_grid_orders["OTHER"] = dict(
            session_id=session.id, side="BUY", volume=100, reserved_price=46)
        with patch.object(self.manager, "_check_exit_conditions", return_value=None):
            self.manager.check_grid_signals("000001", 46)
        self.assertTrue(self.manager.get_buy_budget_status("000001")["blocked"])


class TestAccountFreshness(unittest.TestCase):
    def setUp(self):
        import web_server
        self.web = web_server
        self.original = dict(web_server._account_info_cache)
        self.addCleanup(lambda: web_server._account_info_cache.update(self.original))

    def _refresh(self, value):
        future = Mock()
        future.result.return_value = value
        executor = SimpleNamespace(_shutdown=False, submit=Mock(return_value=future))
        with patch.object(self.web, "api_executor", executor), \
             patch.object(self.web, "get_position_manager_instance", return_value=Mock()):
            self.web._refresh_account_info_worker()

    def test_failed_refresh_preserves_last_amount_and_source_time(self):
        old = dict(account_id="TEST", total_asset=10000, available=9000,
                   timestamp="2026-11-26 09:25:00")
        self.web._account_info_cache.update(data=old, ts=time.time())
        self._refresh(None)
        result = self.web._account_info_cache["data"]
        self.assertEqual(result["total_asset"], 10000)
        self.assertEqual(result["timestamp"], old["timestamp"])
        self.assertEqual(result["data_status"], "stale")

    def test_initial_missing_data_has_no_fabricated_source_time(self):
        self.web._account_info_cache.update(data=None, ts=0)
        self._refresh(None)
        result = self.web._account_info_cache["data"]
        self.assertIsNone(result["timestamp"])
        self.assertEqual(result["data_status"], "unavailable")

    def test_success_clears_stale_status(self):
        self.web._account_info_cache.update(data=dict(data_status="stale"), ts=0)
        self._refresh(dict(total_asset=10000, timestamp=datetime.now().isoformat()))
        self.assertEqual(self.web._account_info_cache["data"]["data_status"], "fresh")

    def test_status_api_exposes_missing_data_and_no_source_time(self):
        self.web._account_info_cache.update(data=None,ts=time.time())
        with patch.object(self.web,"_schedule_account_info_refresh"), \
             patch.object(self.web,"get_position_manager_instance",return_value=SimpleNamespace()):
            response = self.web.app.test_client().get("/api/status")
        self.assertEqual(response.status_code,200)
        account = response.get_json()["account"]
        self.assertEqual(account["dataStatus"],"unavailable")
        self.assertIsNone(account["timestamp"])

    def test_cached_source_age_can_expire_without_refresh_success(self):
        old = dict(account_id="TEST", total_asset=10000,
                   timestamp=(datetime.now()-timedelta(seconds=200)).isoformat())
        self.web._account_info_cache.update(data=old, ts=time.time())
        with patch.object(config, "ENABLE_QMT_IPC_FALLBACK", True), \
             patch.object(self.web, "_schedule_account_info_refresh"), \
             patch.object(self.web, "get_position_manager_instance", return_value=Mock()):
            result = self.web.get_account_info_cached()
        self.assertEqual(result["data_status"], "stale")
        self.assertGreater(result["data_age_seconds"], 190)


class TestIpcReadiness(_IpcSnapshotBase):
    def _files(self, quote_age=0):
        self.snapshot()
        now = datetime.now()
        self._write("heartbeat.json", dict(account_id=self.acc, ts=now.isoformat()))
        self._write("quotes.json", dict(account_id=self.acc, timestamp=now.isoformat(),
                    quotes={"000001.SZ": dict(lastPrice=10, time=int((time.time()-quote_age)*1000))}))

    def _write(self, name, value):
        with open(self.trader._dir("status", name), "w", encoding="utf-8") as handle:
            json.dump(value, handle)

    def test_readiness_checks_all_sources_without_subscribing(self):
        from qmt_ipc_data import QmtIpcDataAdapter
        self._files()
        adapter = QmtIpcDataAdapter(account=self.acc)
        with patch.object(config, "is_continuous_trade_time", return_value=True), \
             patch.object(adapter, "subscribe_quote", side_effect=AssertionError("诊断不可修改订阅")):
            state = adapter.get_readiness(["000001"])
        self.assertTrue(state["ready"])
        self.assertTrue(state["trading_ready"])
        self.assertIn("age_seconds", state["heartbeat"])

    def test_fresh_heartbeat_cannot_hide_stale_account(self):
        from qmt_ipc_data import QmtIpcDataAdapter
        self._files()
        self.snapshot(timestamp="2026-07-13 09:25:00")
        state = QmtIpcDataAdapter(account=self.acc).get_readiness(["000001"])
        self.assertFalse(state["ready"])
        self.assertFalse(state["account"]["available"])

    def test_fresh_export_cannot_hide_expired_stock(self):
        from qmt_ipc_data import QmtIpcDataAdapter
        self._files(quote_age=25)
        with patch.object(config, "is_continuous_trade_time", return_value=True):
            state = QmtIpcDataAdapter(account=self.acc).get_readiness(["000001"])
        self.assertFalse(state["trading_ready"])
        self.assertEqual(state["quotes"]["stocks"]["000001.SZ"]["reason"], "quote_expired")

    def test_reference_quote_is_explicitly_nontradable(self):
        from qmt_ipc_data import QmtIpcDataAdapter
        self._files(quote_age=120)
        with patch.object(config, "is_continuous_trade_time", return_value=False):
            state = QmtIpcDataAdapter(account=self.acc).get_readiness(["000001"])
        self.assertTrue(state["ready"])
        self.assertFalse(state["trading_ready"])

    def test_invalid_producer_shape_cannot_break_readiness(self):
        from qmt_ipc_data import QmtIpcDataAdapter
        self._files()
        self._write("producer.json", [])
        state = QmtIpcDataAdapter(account=self.acc).get_readiness(["000001"])
        self.assertIsNone(state["producer"])

    def test_nonfinite_source_time_has_valid_json_diagnostics(self):
        from qmt_ipc_data import QmtIpcDataAdapter
        self._files()
        self._write("heartbeat.json", dict(account_id=self.acc, ts=float("nan")))
        self._write("quotes.json", dict(account_id=self.acc, timestamp=datetime.now().isoformat(),
                    quotes={"000001.SZ":dict(lastPrice=10,time=float("nan"))}))
        with patch.object(config,"is_continuous_trade_time",return_value=True):
            state = QmtIpcDataAdapter(account=self.acc).get_readiness(["000001"])
        self.assertFalse(state["trading_ready"])
        json.dumps(state,allow_nan=False)

    def test_stale_account_diagnostics_retain_original_source_time(self):
        from qmt_ipc_data import QmtIpcDataAdapter
        self._files()
        self.snapshot(timestamp="2026-07-13 09:25:00")
        state = QmtIpcDataAdapter(account=self.acc).get_readiness(["000001"])
        self.assertEqual(state["account"]["asof"],"2026-07-13 09:25:00")
        self.assertGreater(state["account"]["age_seconds"],90)

    def test_repeated_connection_failures_are_summarized(self):
        from logger import reset_log_throttle
        reset_log_throttle()
        self.addCleanup(reset_log_throttle)
        clock = [10000.0]
        with patch("qmt_ipc_trader.time.time",side_effect=lambda:clock[0]), \
             patch.object(self.trader,"_write_ipc_config"), patch("qmt_ipc_trader.logger") as log:
            for _ in range(20):
                self.assertIsNone(self.trader.connect())
                clock[0] += 30
        self.assertEqual(log.log.call_count,2)
        log.error.assert_not_called()

    def test_small_future_heartbeat_is_accepted_by_both_adapters(self):
        from qmt_ipc_data import QmtIpcDataAdapter
        self._files()
        adapter = QmtIpcDataAdapter(account=self.acc)
        now = time.time()
        for offset in (0.001, config.QMT_IPC_TIMESTAMP_FUTURE_TOLERANCE_SECONDS):
            with self.subTest(offset=offset), \
                 patch("qmt_ipc_data.time.time",return_value=now), \
                 patch("qmt_ipc_data.os.path.getmtime",return_value=now+offset):
                self.assertTrue(adapter.connect())
                self.assertTrue(self.trader._is_qmt_alive())

    def test_future_heartbeat_beyond_tolerance_is_rejected_by_both(self):
        from qmt_ipc_data import QmtIpcDataAdapter
        self._files()
        now = time.time()
        with patch("qmt_ipc_data.time.time",return_value=now), \
             patch("qmt_ipc_data.os.path.getmtime",
                   return_value=now+config.QMT_IPC_TIMESTAMP_FUTURE_TOLERANCE_SECONDS+1):
            self.assertFalse(QmtIpcDataAdapter(account=self.acc).connect())
            self.assertFalse(self.trader._is_qmt_alive())

    def test_reconnect_does_not_rewrite_unchanged_configuration(self):
        self.trader._write_ipc_config()
        path = self.trader._dir("config.json")
        before = os.stat(path).st_mtime_ns
        self.trader._write_ipc_config()
        self.assertEqual(os.stat(path).st_mtime_ns, before)


class TestIpcRecovery(unittest.TestCase):
    def test_failed_reconnect_backoff_is_bounded_and_success_resets_it(self):
        from position_manager import PositionManager
        pm = PositionManager.__new__(PositionManager)
        pm._reconnect_lock = threading.Lock()
        pm._reconnect_in_progress = False
        pm.qmt_connected = False
        pm.qmt_trader = Mock()
        pm.qmt_trader.reconnect_xttrader.return_value = False
        with patch.object(config, "ENABLE_SIMULATION_MODE", False), \
             patch.object(config, "ENABLE_XTQUANT_MANAGER", False), \
             patch.object(config, "ENABLE_QMT_IPC_FALLBACK", True):
            for expected in (60, 120, 240, 300, 300):
                pm._start_qmt_connect_worker()
                pm._reconnect_thread.join(2)
                self.assertFalse(pm._reconnect_thread.is_alive())
                self.assertEqual(pm._qmt_reconnect_interval(), expected)
            pm.qmt_trader.reconnect_xttrader.return_value = True
            pm._start_qmt_connect_worker()
            pm._reconnect_thread.join(2)
            self.assertTrue(pm.qmt_connected)
            self.assertEqual(pm._qmt_reconnect_interval(), config.XTQUANT_RECONNECT_INTERVAL)

    def test_nontrading_recovery_probe_ignores_full_reconnect_backoff(self):
        from test.test_ipc_full_chain import TestIpcRuntimeDiagnostics
        harness = TestIpcRuntimeDiagnostics()
        try:
            pm = harness._manager(connected=False)
            pm._probe_qmt_recovered.return_value = True
            pm._ipc_reconnect_interval = 300
            harness._run(pm, 1, trade=False)
            self.assertTrue(pm.qmt_connected)
            pm._attempt_qmt_reconnect.assert_not_called()
        finally:
            harness.doCleanups()

    def test_initial_disconnected_probe_uses_configured_interval(self):
        from test.test_ipc_full_chain import TestIpcRuntimeDiagnostics
        harness = TestIpcRuntimeDiagnostics()
        try:
            pm = harness._manager(connected=False)
            pm._last_reconnect_time = 0
            sleeps = []
            def finish(seconds):
                sleeps.append(seconds)
                pm.stop_flag = True
            with patch.object(config,"ENABLE_SIMULATION_MODE",False), \
                 patch.object(config,"ENABLE_XTQUANT_MANAGER",False), \
                 patch.object(config,"ENABLE_QMT_IPC_FALLBACK",True), \
                 patch.object(config,"is_trade_time",return_value=False), \
                 patch("position_manager.time.time",return_value=10000), \
                 patch("position_manager.time.sleep",side_effect=finish):
                pm._position_monitor_loop()
            self.assertEqual(sleeps,[config.QMT_IPC_HEALTH_PROBE_INTERVAL_SECONDS])
        finally:
            harness.doCleanups()

    def test_heartbeat_only_recovery_requires_valid_account(self):
        from position_manager import PositionManager
        pm = PositionManager.__new__(PositionManager)
        pm._reconnect_lock = threading.Lock()
        pm._reconnect_in_progress = False
        pm.qmt_trader = Mock()
        pm.qmt_trader.ping_xttrader.return_value = True
        pm.qmt_trader._read_account_snapshot.side_effect = RuntimeError("账户快照过期")
        with patch.object(config, "ENABLE_SIMULATION_MODE", False), \
             patch.object(config, "ENABLE_XTQUANT_MANAGER", False), \
             patch.object(config, "ENABLE_QMT_IPC_FALLBACK", True):
            self.assertFalse(pm._probe_qmt_recovered())
        pm.qmt_trader.register_trade_callback.assert_not_called()


class TestPremarketSnapshotRetry(unittest.TestCase):
    def _run(self, times, snapshots, fresh=True):
        import premarket_sync
        import settlement_db
        pm = SimpleNamespace(qmt_trader=SimpleNamespace(position_snapshot_valid=fresh))
        with patch.object(config, "ENABLE_SIMULATION_MODE", False), \
             patch.object(config, "ENABLE_QMT_IPC_FALLBACK", True), \
             patch.object(premarket_sync, "datetime") as clock, \
             patch.object(premarket_sync.time, "sleep") as sleep, \
             patch.object(settlement_db, "take_snapshot", side_effect=snapshots) as take:
            clock.now.side_effect = times
            result = premarket_sync._take_open_snapshot_with_retry(pm)
        return result, take.call_count, sleep.call_count

    def test_retry_recovers_only_inside_compensation_window(self):
        result, calls, sleeps = self._run(
            [datetime(2026,11,26,9,25), datetime(2026,11,26,9,25),
             datetime(2026,11,26,9,25,15), datetime(2026,11,26,9,25,15)],
            [(3,False), (3,True)])
        self.assertEqual(result, (3,True,True))
        self.assertEqual((calls,sleeps),(2,1))

    def test_after_window_does_not_relabel_afternoon_as_open(self):
        result, calls, sleeps = self._run(
            [datetime(2026,11,26,14,25)],[(3,False)])
        self.assertEqual(result,(-1,False,False))
        self.assertEqual((calls,sleeps),(0,0))

    def test_deadline_does_not_allow_an_extra_snapshot(self):
        result, calls, sleeps = self._run(
            [datetime(2026,11,26,9,29,59), datetime(2026,11,26,9,29,59),
             datetime(2026,11,26,9,30)],[(3,False)])
        self.assertFalse(result[2])
        self.assertEqual((calls,sleeps),(1,1))

    def test_stale_positions_do_not_count_as_a_complete_baseline(self):
        result, _, _ = self._run(
            [datetime(2026,11,26,9,29,59), datetime(2026,11,26,9,29,59),
             datetime(2026,11,26,9,30)],[(3,True)],fresh=False)
        self.assertEqual(result,(3,True,False))

    def test_preflight_is_scheduled_before_sync_and_cancelled_on_stop(self):
        import premarket_sync
        scheduler = premarket_sync.PreMarketSyncScheduler.__new__(premarket_sync.PreMarketSyncScheduler)
        scheduler.running, scheduler.readiness_timer = True, None
        scheduler.calculate_next_sync_time = Mock(return_value=datetime(2026,11,26,9,25))
        scheduler.save_persisted_schedule = Mock()
        with patch.object(config, "ENABLE_QMT_IPC_FALLBACK", True), \
             patch.object(premarket_sync, "datetime") as clock, \
             patch.object(premarket_sync.threading, "Timer") as timer:
            clock.now.return_value = datetime(2026,11,26,9)
            scheduler.schedule_next_sync()
            self.assertEqual([call.args[0] for call in timer.call_args_list], [1200,1500])
            scheduler.stop()
            self.assertEqual(timer.return_value.cancel.call_count,2)


class TestIpcAssetWarning(unittest.TestCase):
    def test_ipc_valuation_mismatch_only_warns(self):
        from test.test_asset_consistency_monitor import TestAssetConsistencyMonitor
        harness = TestAssetConsistencyMonitor()
        try:
            harness.setUp()
            harness._set_evidence(price=11)
            with patch.object(config,"ENABLE_QMT_IPC_FALLBACK",True), \
                 patch("position_manager.log_throttled") as warning:
                harness.pm._monitor_total_asset_consistency()
            self.assertTrue(harness.pm.qmt_connected)
            self.assertTrue(harness.pm._asset_monitor_abnormal)
            self.assertTrue(warning.called)
        finally:
            harness.doCleanups()


class TestProducerDiagnostics(unittest.TestCase):
    def setUp(self):
        from test.test_qmt_ipc_executor import _ExecBase, ex
        self.harness = _ExecBase()
        self.harness.setUp()
        self.addCleanup(self.harness.tearDown)
        self.addCleanup(self.harness.doCleanups)
        self.ex = ex
        ex._atomic_write_json(os.path.join(self.harness.acc_dir,"config.json"),
                              dict(account_id=self.harness.acc["account_id"], stocks=["000001.SZ"]))
        for name in ("_EXPORT_STATE","_LAST_QUOTES"):
            item = patch.object(ex,name,{})
            item.start()
            self.addCleanup(item.stop)

    def test_failed_account_export_has_explicit_reason(self):
        with patch.object(self.ex,"_write_snapshot",return_value=False):
            self.assertFalse(self.ex.write_snapshot(self.harness.acc))
        self.ex._write_heartbeat(self.harness.acc)
        path = os.path.join(self.harness.acc["dirs"]["status"],"producer.json")
        value = json.load(open(path,encoding="utf-8"))
        self.assertIn("version",value)
        self.assertIn("last_error",value["exports"]["account"])
        self.assertNotIn("last_success_at",value["exports"]["account"])

    def test_heartbeat_does_not_fabricate_a_successful_export_time(self):
        self.ex._record_export(self.harness.acc,"account",True)
        before = dict(self.ex._EXPORT_STATE[self.harness.acc["account_id"]]["account"])
        self.ex._write_heartbeat(self.harness.acc)
        after = self.ex._EXPORT_STATE[self.harness.acc["account_id"]]["account"]
        self.assertEqual(before,after)

    def test_quote_export_failure_keeps_the_actual_error(self):
        context = SimpleNamespace(get_full_tick=Mock(side_effect=RuntimeError("feed failed")))
        with patch.object(self.ex,"_CONTEXT_INFO",{"obj":context}):
            self.assertFalse(self.ex.write_quotes(self.harness.acc))
        value = self.ex._EXPORT_STATE[self.harness.acc["account_id"]]["quotes"]
        self.assertIn("feed failed",value["last_error"])
