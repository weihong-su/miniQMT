"""完整 IPC 链路的隔离回归，禁止调用真实大 QMT。"""
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "qmt-trader"))
import config
from qmt_ipc_trader import QmtIpcTrader
from test.test_qmt_ipc_executor import ex, _ExecBase


class _IpcSnapshotBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ipc_valid_")
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.acc = "TEST_IPC"
        self.patches = [
            patch.object(config, "QMT_IPC_ROOT", self.root),
            patch.object(config, "ENABLE_SIMULATION_MODE", True),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.trader = QmtIpcTrader(account=self.acc)
        self.trader._ensure_dirs()

    def snapshot(self, **overrides):
        value = dict(account_id=self.acc, timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     total_asset=10000.0, available=5000.0, frozen=0.0,
                     market_value=5000.0, positions=[
                         dict(stock="000001.SZ", volume=500, available=300, cost=9.8,
                              market_price=10.0, market_value=5000.0)])
        value.update(overrides)
        with open(self.trader._dir("status", "account.json"), "w", encoding="utf-8") as handle:
            json.dump(value, handle)
        return value


class TestIpcSnapshotValidity(_IpcSnapshotBase):
    def test_stale_snapshot_is_not_a_valid_position_or_cash_source(self):
        self.snapshot(timestamp="2026-07-13 14:20:26")
        with self.assertRaises(RuntimeError):
            self.trader.position()
        with self.assertRaises(RuntimeError):
            self.trader.balance()
        self.assertFalse(self.trader.check_stock_is_av_sell("000001", amount=100))
        self.assertFalse(self.trader.check_stock_is_av_buy("000001", price=10, amount=100))

    def test_missing_snapshot_is_unavailable_instead_of_empty_portfolio(self):
        with self.assertRaises(RuntimeError):
            self.trader.position()

    def test_wrong_account_snapshot_is_rejected(self):
        self.snapshot(account_id="ANOTHER_ACCOUNT")
        with self.assertRaises(RuntimeError):
            self.trader.position()

    def test_fresh_empty_snapshot_is_valid(self):
        self.snapshot(positions=[])
        self.assertTrue(self.trader.position().empty)

    def test_source_time_and_price_survive_position_mapping(self):
        value = self.snapshot()
        frame = self.trader.position()
        self.assertEqual(frame.iloc[0]["市价"], 10.0)
        self.assertEqual(frame.iloc[0]["快照时间"], value["timestamp"])

    def test_reconnect_does_not_register_callbacks_twice(self):
        callback = Mock()
        self.trader.register_trade_callback(callback)
        self.trader.register_trade_callback(callback)
        self.assertEqual(len(self.trader._trade_callbacks), 1)


class TestIpcMarketData(_IpcSnapshotBase):
    def _adapter(self):
        from qmt_ipc_trader import QmtIpcDataAdapter
        return QmtIpcDataAdapter(account=self.acc)

    def quotes(self, age=0):
        stamp = datetime.now() - timedelta(seconds=age)
        value = dict(timestamp=stamp.isoformat(), account_id=self.acc, quotes={
            "000001.SZ": dict(lastPrice=10.0, lastClose=9.8, time=int(stamp.timestamp()*1000),
                              askPrice=[10.01,10.02,10.03], bidPrice=[9.99,9.98,9.97])
        }, instruments={"000001.SZ": {"UpStopPrice": 10.78, "DownStopPrice": 8.82}})
        with open(self.trader._dir("status", "quotes.json"), "w", encoding="utf-8") as handle:
            json.dump(value, handle)

    def test_reads_native_quote_without_xtdata_or_mootdx(self):
        import data_manager
        self.quotes()
        manager = data_manager.DataManager.__new__(data_manager.DataManager)
        manager.xt = self._adapter()
        manager.market_health = data_manager.MarketDataHealthTracker()
        with patch.object(config, "ENABLE_QMT_IPC_FALLBACK", True), \
             patch.object(manager, "get_latest_xtdata", side_effect=AssertionError("local xtdata called")), \
             patch("Methods.getStockData", side_effect=AssertionError("Mootdx called")):
            quote = manager.get_latest_data("000001")
        self.assertEqual(quote["lastPrice"], 10.0)
        self.assertEqual(quote["_source"], "QMT_IPC")

    def test_stale_tick_is_rejected_even_when_file_was_just_rewritten(self):
        self.quotes(age=120)
        with patch.object(config, "is_continuous_trade_time", return_value=True):
            self.assertEqual(self._adapter().get_full_tick(["000001.SZ"]), {})

    def test_reads_instrument_limits_from_ipc(self):
        self.quotes()
        self.assertEqual(self._adapter().get_instrument_detail("000001.SZ")["UpStopPrice"], 10.78)

    def test_quote_request_is_registered_for_new_stock(self):
        adapter = self._adapter()
        adapter.subscribe_quote("600000.SH")
        path = self.trader._dir("status", "subscriptions.json")
        with open(path, encoding="utf-8") as handle:
            self.assertIn("600000.SH", json.load(handle)["stocks"])


class _NativeIpcBase(_ExecBase):
    def setUp(self):
        super().setUp()
        self.context = types.SimpleNamespace()
        self.orders = []
        self.deals = []
        self.submissions = []
        self.cancels = []
        self.patches = [
            patch.object(ex, "_CONTEXT_INFO", {"obj": self.context}),
            patch.object(ex, "get_trade_detail_data", self.reader, create=True),
            patch.object(ex, "passorder", self.submit, create=True),
            patch.object(ex, "cancel", self.cancel, create=True),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def reader(self, account, account_type, kind):
        self.assertIs(threading.current_thread(), threading.main_thread())
        if kind == "ORDER":
            return self.orders
        if kind == "DEAL":
            return self.deals
        if kind == "ACCOUNT":
            return [types.SimpleNamespace(m_dBalance=10000.0, m_dAvailable=9000.0)]
        return []

    def submit(self, *args):
        self.submissions.append(args)

    def cancel(self, *args):
        self.cancels.append(args)
        return True

    def broker_order(self, oid, status=50, volume=1000, traded=0, marker=None):
        return types.SimpleNamespace(
            m_strAccountID=self.acc["account_id"], m_strInstrumentID="000001",
            m_strExchangeID="SZ", m_strRemark=marker or "ipc:%s" % oid,
            m_strOrderSysID="BROKER_%s" % oid, m_nVolumeTotalOriginal=volume,
            m_nVolumeTraded=traded, m_nOffsetFlag=48, m_nOrderStatus=status)

    def deal(self, oid, trade_id, volume, price):
        return types.SimpleNamespace(
            m_strAccountID=self.acc["account_id"], m_strInstrumentID="000001",
            m_strExchangeID="SZ", m_strOrderSysID="BROKER_%s" % oid,
            m_strTradeID=trade_id, m_nVolume=volume, m_dPrice=price,
            m_dTradeAmount=volume*price, m_nOffsetFlag=48,
            m_strTradeDate=datetime.now().strftime("%Y%m%d"), m_strTradeTime="14:30:00")

    def receipt(self, oid):
        return ex._load_json(os.path.join(self.acc["dirs"]["done"], "ord_%s.json" % oid))

    def poll(self):
        ex._poll_native_orders(self.acc)


class TestNativeIpcOrders(_NativeIpcBase):
    def test_passorder_uses_native_api_and_returns_without_waiting_for_fill(self):
        path = self._write_pending(700, volume=1000, price=10.5)
        with patch.object(ex, "_try_connect_xttrader", side_effect=AssertionError("xttrader called")):
            ex.process_one_order(self.acc, path)
        self.assertEqual(len(self.submissions), 1)
        args = self.submissions[0]
        self.assertEqual(args[:7], (23, 1101, self.acc["account_id"], "000001.SZ", 11, 10.5, 1000))
        self.assertEqual(args[-2:], ("ipc:700", self.context))
        self.assertTrue(os.path.exists(os.path.join(self.acc["dirs"]["processing"], "ord_700.json")))

    def test_same_processing_request_is_never_resubmitted_after_restart(self):
        path = self._write_pending(701)
        ex.process_one_order(self.acc, path)
        self.poll()
        ex.recover_leftovers(self.acc)
        self.poll()
        self.assertEqual(len(self.submissions), 1)

    def test_broker_report_matches_exact_user_marker(self):
        path = self._write_pending(702)
        ex.process_one_order(self.acc, path)
        self.orders = [self.broker_order(702, marker="ipc:OTHER")]
        self.poll()
        self.assertIsNone(self.receipt(702))
        self.orders = [self.broker_order(702)]
        self.poll()
        self.assertEqual(self.receipt(702)["status"], "pending")
        self.assertEqual(self.receipt(702)["entrust_id"], "BROKER_702")

    def test_two_partial_fills_preserve_real_ids_prices_and_amounts(self):
        path = self._write_pending(703)
        ex.process_one_order(self.acc, path)
        self.orders = [self.broker_order(703, status=55, traded=200)]
        self.deals = [self.deal(703, "FILL_A", 200, 10.0)]
        self.poll()
        self.assertEqual(self.receipt(703)["filled_volume"], 200)
        self.orders = [self.broker_order(703, status=56, traded=1000)]
        self.deals += [self.deal(703, "FILL_B", 800, 10.5)]
        self.poll()
        rec = self.receipt(703)
        self.assertEqual(rec["status"], "filled")
        self.assertEqual(rec["filled_volume"], 1000)
        self.assertAlmostEqual(rec["filled_price"], 10.4)
        self.assertEqual([d["trade_id"] for d in rec["deals"]], ["FILL_A", "FILL_B"])

    def test_cancel_uses_broker_system_id_and_waits_for_terminal_report(self):
        path = self._write_pending(704)
        ex.process_one_order(self.acc, path)
        self.orders = [self.broker_order(704)]
        with open(os.path.join(self.acc["dirs"]["cancel"], "cancel_704.json"), "w") as handle:
            json.dump({"order_id":704}, handle)
        self.poll()
        self.assertEqual(self.cancels[0], ("BROKER_704", self.acc["account_id"], "STOCK", self.context))
        self.assertEqual(self.receipt(704)["status"], "pending")
        self.orders = [self.broker_order(704, status=54)]
        self.poll()
        self.assertEqual(self.receipt(704)["status"], "cancelled")

    def test_partial_cancel_preserves_fills(self):
        path = self._write_pending(705)
        ex.process_one_order(self.acc, path)
        self.orders = [self.broker_order(705, status=53, traded=200)]
        self.deals = [self.deal(705, "PARTIAL", 200, 10.0)]
        self.poll()
        self.assertEqual(self.receipt(705)["status"], "partial_cancelled")
        self.assertEqual(self.receipt(705)["filled_volume"], 200)

    def test_ambiguous_orders_do_not_cancel_or_claim_fill(self):
        path = self._write_pending(706)
        ex.process_one_order(self.acc, path)
        self.orders = [self.broker_order(706), self.broker_order(706)]
        self.orders[1].m_strOrderSysID = "ANOTHER_BROKER_ID"
        self.poll()
        self.assertIsNone(self.receipt(706))
        self.assertEqual(self.cancels, [])



class TestIpcPositionProtection(_IpcSnapshotBase):
    def test_unavailable_snapshot_preserves_positions_without_reconnect_or_grid_exit(self):
        import sqlite3
        from position_manager import PositionManager
        pm = PositionManager.__new__(PositionManager)
        pm.memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(pm.memory_conn.close)
        pm.memory_conn.execute("CREATE TABLE positions (stock_code TEXT, volume INTEGER, available INTEGER)")
        pm.memory_conn.execute("INSERT INTO positions VALUES ('000001',500,300)")
        pm.memory_conn_lock = threading.Lock()
        pm.qmt_trader = self.trader
        pm.qmt_connected = True
        pm.last_position_update_time = 0
        pm.position_update_interval = 10
        pm.positions_cache = None
        pm.empty_real_position_count = 0
        pm._handle_empty_real_positions = Mock()
        pm.grid_manager = Mock()
        pm._attempt_qmt_reconnect = Mock()
        pm.check_pending_orders_timeout = Mock()
        pm.stop_flag = False
        pm.last_update_highest_time = float("inf")
        pm.update_highest_interval = 60
        with patch.object(config, "ENABLE_SIMULATION_MODE", False), \
             patch.object(config, "ENABLE_QMT_IPC_FALLBACK", True), \
             patch.object(config, "is_trade_time", return_value=True), \
             patch("position_manager.run_with_timeout", side_effect=lambda function, timeout: function()), \
             patch("position_manager.time.sleep", side_effect=lambda seconds: setattr(pm, "stop_flag", True)):
            pm._position_monitor_loop()
        self.assertEqual(pm.memory_conn.execute("SELECT volume FROM positions").fetchone()[0], 500)
        self.assertTrue(pm.qmt_connected)
        pm._handle_empty_real_positions.assert_not_called()
        pm._attempt_qmt_reconnect.assert_not_called()
        pm.grid_manager.sweep_stale_sessions.assert_not_called()
        pm.check_pending_orders_timeout.assert_called_once()

    def test_async_submit_is_fast_and_keeps_market_price_type(self):
        self.snapshot()
        with open(self.trader._dir("status", "heartbeat.json"), "w") as handle:
            json.dump({"account_id":self.acc}, handle)
        with patch.object(config, "USE_SYNC_ORDER_API", False):
            started = time.monotonic()
            oid = self.trader.buy("000001.SZ", amount=100, price=0, price_type=5)
            self.assertLess(time.monotonic()-started, 0.5)
        self.assertIsNotNone(oid)
        with open(self.trader._dir("orders", "pending", "ord_%s.json" % oid)) as handle:
            order = json.load(handle)
        self.assertEqual((order["price"], order["qmt_price_type"], order["price_type"]), (0,5,"market"))

    def test_native_fill_callbacks_and_query_preserve_real_deals(self):
        received = []
        self.trader.register_trade_callback(received.append)
        base = dict(order_id=800, status="partial", action="buy", stock_code="000001.SZ",
                    total_volume=1000, filled_volume=200, filled_price=10, entrust_id="BROKER_800",
                    orig_timestamp=datetime.now().isoformat(), deals=[
                        dict(trade_id="REAL_A",volume=200,price=10,amount=2000,time=int(time.time()))])
        self.trader._dispatch_deals(base)
        base.update(status="filled",filled_volume=1000,filled_price=10.4)
        base["deals"].append(dict(trade_id="REAL_B",volume=800,price=10.5,amount=8400,time=int(time.time())))
        self.trader._dispatch_deals(base)
        self.trader._dispatch_deals(base)
        self.assertEqual([r.traded_id for r in received], ["REAL_A","REAL_B"])
        self.assertEqual([r.traded_volume for r in received], [200,800])
        self.assertEqual([r.traded_price for r in received], [10,10.5])
        with open(self.trader._dir("orders","done","ord_800.json"),"w") as handle:
            json.dump(base,handle)
        frame = self.trader.query_stock_trades()
        self.assertEqual(frame["成交编号"].tolist(), ["REAL_A","REAL_B"])
        self.assertEqual(frame["成交数量"].tolist(), [200,800])

    def test_legacy_cumulative_average_is_converted_to_delta_price(self):
        received = []
        self.trader.register_trade_callback(received.append)
        receipt = dict(order_id=801,status="partial",action="buy",stock_code="000001.SZ",
                       filled_volume=100,filled_price=10,filled_amount=1000)
        self.trader._dispatch_deals(receipt)
        receipt.update(status="filled",filled_volume=300,filled_price=3200/300,filled_amount=3200)
        self.trader._dispatch_deals(receipt)
        self.assertEqual([r.traded_volume for r in received], [100,200])
        self.assertEqual([r.traded_price for r in received], [10,11])


class TestNativeQuoteExport(_ExecBase):
    def test_exports_native_ticks_and_limits_on_main_thread(self):
        code = "000001.SZ"
        now = int(time.time()*1000)
        ex._atomic_write_json(os.path.join(self.acc["dirs"]["status"],"account.json"),{
            "positions":[{"stock":"000001","volume":100}]})
        ctx = types.SimpleNamespace(
            get_full_tick=Mock(return_value={code:{"lastPrice":10,"time":now,"askPrice":[10.01]}}),
            get_instrumentdetail=Mock(return_value={"UpStopPrice":11,"DownStopPrice":9}))
        with patch.object(ex, "_CONTEXT_INFO", {"obj":ctx}), patch.object(ex, "_LAST_QUOTES", {}):
            self.assertTrue(ex.write_quotes(self.acc))
        ctx.get_full_tick.assert_called_once_with([code])
        value = ex._load_json(os.path.join(self.acc["dirs"]["status"],"quotes.json"))
        self.assertEqual(value["quotes"][code]["time"],now)
        self.assertEqual(value["instruments"][code]["UpStopPrice"],11)


class TestNativeIpcOrderFailures(_NativeIpcBase):
    def test_cancel_report_waits_for_lagging_deal_details(self):
        path = self._write_pending(710)
        ex.process_one_order(self.acc,path)
        self.orders = [self.broker_order(710,status=53,traded=200)]
        self.poll()
        self.assertIsNone(self.receipt(710))
        self.deals = [self.deal(710,"LATE_FILL",200,10)]
        self.poll()
        self.assertEqual(self.receipt(710)["status"],"partial_cancelled")

    def test_submit_exception_remains_unknown_without_resubmission(self):
        path = self._write_pending(711)
        def unknown(*args):
            self.submissions.append(args)
            raise RuntimeError("response lost after request")
        with patch.object(ex,"passorder",unknown):
            ex.process_one_order(self.acc,path)
        self.orders = [self.broker_order(711)]
        self.poll()
        self.assertEqual(len(self.submissions),1)
        self.assertEqual(self.receipt(711)["status"],"pending")

    def test_unknown_expired_request_is_not_claimed_cancelled(self):
        path = self._write_pending(712,timeout_sec=0)
        ex.process_one_order(self.acc,path)
        self.poll()
        self.assertIsNone(self.receipt(712))
        self.assertEqual(len(self.submissions),1)
        self.assertEqual(self.cancels,[])
        self.assertTrue(os.path.exists(os.path.join(self.acc["dirs"]["processing"],"ord_712.json")))

    def test_native_sell_uses_sell_direction(self):
        path = self._write_pending(713,action="sell")
        ex.process_one_order(self.acc,path)
        self.assertEqual(self.submissions[0][0],24)
        order = self.broker_order(713,status=56,traded=1000)
        order.m_nOffsetFlag = 49
        fill = self.deal(713,"SELL_FILL",1000,10)
        fill.m_nOffsetFlag = 49
        self.orders,self.deals = [order],[fill]
        self.poll()
        self.assertEqual(self.receipt(713)["status"],"filled")


    def test_cancel_is_sent_even_when_deal_details_are_lagging(self):
        path = self._write_pending(714)
        ex.process_one_order(self.acc,path)
        self.orders = [self.broker_order(714,status=55,traded=200)]
        with open(os.path.join(self.acc["dirs"]["cancel"],"cancel_714.json"),"w") as handle:
            json.dump({"order_id":714},handle)
        self.poll()
        self.assertEqual(len(self.cancels),1)
        self.assertIsNone(self.receipt(714))

    def test_journal_error_after_submit_does_not_drop_unknown_order(self):
        path = self._write_pending(715)
        writer = ex._atomic_write_json
        def fail_after_submit(filename,data):
            if data.get("native",{}).get("phase") == "submitted":
                raise OSError("journal publication failed")
            return writer(filename,data)
        with patch.object(ex,"_atomic_write_json",side_effect=fail_after_submit):
            ex.process_one_order(self.acc,path)
        self.assertEqual(len(self.submissions),1)
        self.assertIsNone(self.receipt(715))
        record = ex._load_json(os.path.join(self.acc["dirs"]["processing"],"ord_715.json"))
        self.assertEqual(record["native"]["phase"],"submitting")
        self.orders = [self.broker_order(715)]
        self.poll()
        self.assertEqual(self.receipt(715)["status"],"pending")

    def test_missing_cancel_api_rejects_before_submit(self):
        path = self._write_pending(716)
        with patch.object(ex,"cancel",None):
            ex.process_one_order(self.acc,path)
        self.assertEqual(self.submissions,[])
        self.assertEqual(self.receipt(716)["status"],"error")



class TestIpcAfterHoursWeb(unittest.TestCase):
    def test_web_fields_refresh_ipc_available_after_market_close(self):
        import sqlite3
        import pandas as pd
        from position_manager import PositionManager
        pm = PositionManager.__new__(PositionManager)
        pm.memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(pm.memory_conn.close)
        pm.memory_conn_lock = threading.Lock()
        pm.memory_conn.execute(
            "CREATE TABLE positions (stock_code TEXT, volume INTEGER, available INTEGER, "
            "current_price REAL, market_value REAL, cost_price REAL, profit_ratio REAL)")
        pm.memory_conn.execute("INSERT INTO positions VALUES ('000001',500,0,10,5000,10,0)")
        pm.data_manager = Mock()
        pm.data_manager.get_latest_data.return_value = None
        pm.get_position_restriction = Mock(return_value={})
        def refresh():
            with pm.memory_conn_lock:
                pm.memory_conn.execute("UPDATE positions SET available=300")
                pm.memory_conn.commit()
                return pd.read_sql_query("SELECT * FROM positions", pm.memory_conn)
        pm.get_all_positions = Mock(side_effect=refresh)
        for ipc in (False,True):
            with self.subTest(ipc=ipc), patch.object(config,"ENABLE_SIMULATION_MODE",False), \
                 patch.object(config,"ENABLE_QMT_IPC_FALLBACK",ipc), \
                 patch.object(config,"is_trade_time",return_value=False):
                pm.memory_conn.execute("UPDATE positions SET available=0")
                pm.memory_conn.commit()
                pm.get_all_positions.reset_mock()
                frame = pm.get_all_positions_with_all_fields()
                self.assertEqual(frame.iloc[0]["available"],300 if ipc else 0)
                self.assertEqual(pm.get_all_positions.call_count,1 if ipc else 0)


class TestIpcAccountErrorLogging(_IpcSnapshotBase):
    def test_stale_account_errors_are_throttled_and_recovery_resets_notice(self):
        from position_manager import PositionManager
        from logger import reset_log_throttle
        reset_log_throttle("ipc_account_unavailable")
        self.addCleanup(reset_log_throttle, "ipc_account_unavailable")
        pm = PositionManager.__new__(PositionManager)
        pm.qmt_trader = self.trader
        pm.get_account_valuation = Mock(return_value={})
        self.snapshot(timestamp="2026-07-13 14:20:26")
        with patch.object(config, "ENABLE_SIMULATION_MODE", False), \
             patch.object(config, "ENABLE_QMT_IPC_FALLBACK", True), \
             patch.object(config, "LOG_THROTTLE_INTERVAL", 300), \
             patch("position_manager.logger") as log:
            self.assertIsNone(pm.get_account_info())
            self.assertIsNone(pm.get_account_info())
            self.assertEqual(log.log.call_count, 1)
            self.assertIn("快照来源时间", str(log.log.call_args))
            self.snapshot()
            self.assertEqual(pm.get_account_info()["available"], 5000.0)
            self.snapshot(timestamp="2026-07-13 14:20:26")
            self.assertIsNone(pm.get_account_info())
            self.assertEqual(log.log.call_count, 2)
            pm.get_account_valuation.assert_called_once()


if __name__ == "__main__":
    unittest.main()
