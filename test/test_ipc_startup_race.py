"""启动回报时序回归：虚拟交易器与临时 IPC，禁止真实委托。"""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pandas as pd
import config
import position_manager
import trading_executor
from qmt_ipc_trader import QmtIpcTrader


class TestManagerStartupState(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(":memory:",check_same_thread=False)
        self.conn.execute("CREATE TABLE positions(stock_code TEXT)")
        self.addCleanup(self.conn.close)
        self.dm=SimpleNamespace(conn=self.conn)
        self.ready_fields=("memory_conn","memory_conn_lock","pending_orders_lock","pending_orders",
            "callback_refresh_lock","version_lock","signal_lock","sync_lock","grid_manager",
            "_reconnect_lock","_memory_sync_lock","_trade_reconcile_done")

    def manager(self,trade=False,order=False,disconnect=False,simulation=False,connected=True):
        checks,events=[],[]
        fields=self.ready_fields
        class ImmediateTrader:
            def connect(inner):
                events.append("connect")
                return (inner,inner) if connected else None
            def check(inner,cb,name):
                events.append(name)
                checks.append(all(hasattr(cb.__self__,field) for field in fields))
            def register_trade_callback(inner,cb):
                inner.check(cb,"trade")
                if trade:
                    cb(SimpleNamespace(order_id=101,stock_code="603466.SH",
                        traded_id="BOOT_FILL",traded_volume=100,traded_price=10,order_type=24))
            def register_order_callback(inner,cb):
                inner.check(cb,"order")
                if order:
                    cb(SimpleNamespace(order_id=101,stock_code="603466.SH",
                        order_status=56,traded_volume=100))
            def register_disconnect_callback(inner,cb):
                inner.check(cb,"disconnect")
                if disconnect: cb()
        class InlineThread:
            def __init__(inner,target,**kwargs):
                inner.target=target
            def start(inner):
                inner.target()
        def sync(pm):
            events.append("sync")
            checks.append(all(hasattr(pm,field) for field in fields))
        trader=ImmediateTrader()
        with patch.object(config,"ENABLE_SIMULATION_MODE",simulation), \
             patch.object(config,"ENABLE_GRID_TRADING",False), \
             patch.object(config,"ENABLE_XTQUANT_MANAGER",False), \
             patch.object(position_manager,"get_data_manager",return_value=self.dm), \
             patch.object(position_manager,"_create_qmt_trader",return_value=trader) as factory, \
             patch.object(position_manager.PositionManager,"start_sync_thread",sync), \
             patch.object(position_manager.PositionManager,"get_all_positions",return_value=pd.DataFrame()), \
             patch.object(position_manager.PositionManager,"_record_external_trade_after_callback",return_value=True) as record, \
             patch.object(position_manager,"run_with_timeout",side_effect=lambda fn,timeout:fn()), \
             patch("position_manager.threading.Thread",InlineThread), \
             patch.object(position_manager,"logger") as log:
            pm=position_manager.PositionManager()
        self.addCleanup(pm.memory_conn.close)
        return pm,checks,events,record,log,factory

    def test_registered_callbacks_see_all_initialized_state(self):
        _,checks,_,_,_,_=self.manager()
        self.assertTrue(all(checks),checks)

    def test_immediate_trade_is_handled_instead_of_lost_during_init(self):
        _,_,_,record,log,_=self.manager(trade=True)
        log.error.assert_not_called()
        record.assert_called_once()

    def test_immediate_terminal_order_can_refresh_without_resetting_version(self):
        pm,_,_,_,log,_=self.manager(order=True)
        log.warning.assert_not_called()
        self.assertEqual(pm.data_version,1)

    def test_sync_worker_starts_after_state_and_callback_setup(self):
        _,checks,events,_,_,_=self.manager()
        self.assertEqual(events[-1],"sync")
        self.assertTrue(all(checks))

    def test_disconnect_during_registration_is_not_overwritten(self):
        pm,checks,_,_,_,_=self.manager(disconnect=True)
        self.assertTrue(all(checks))
        self.assertFalse(pm.qmt_connected)

    def test_simulation_skips_real_factory_but_has_ready_sync_state(self):
        pm,checks,_,_,_,factory=self.manager(simulation=True)
        factory.assert_not_called()
        self.assertIsNone(pm.qmt_trader)
        self.assertTrue(all(checks))


    def test_early_background_trade_reuses_published_manager_and_executor(self):
        entered=threading.Event()
        workers,records,constructed=[],[],[]
        original=position_manager.PositionManager
        class StartupTrader:
            def connect(inner):
                return inner,inner
            def register_trade_callback(inner,cb):
                worker=threading.Thread(target=cb,args=(SimpleNamespace(
                    order_id=101,stock_code="603466.SH",traded_id="BOOT_FILL",
                    traded_volume=100,traded_price=10,order_type=24),))
                workers.append(worker)
                worker.start()
            def register_order_callback(inner,cb):
                pass
            def register_disconnect_callback(inner,cb):
                pass
        def record(pm,*args,**kwargs):
            entered.set()
            records.append(trading_executor.get_trading_executor().position_manager)
            return True
        def sync(pm):
            self.assertTrue(entered.wait(2), "构造返回前必须已收到后台成交")
        def construct():
            constructed.append(True)
            return original()
        with patch.object(config,"ENABLE_SIMULATION_MODE",False), \
             patch.object(config,"ENABLE_GRID_TRADING",False), \
             patch.object(position_manager,"_instance",None), \
             patch.object(trading_executor,"_instance",None), \
             patch.object(position_manager,"PositionManager",side_effect=construct), \
             patch.object(position_manager,"get_data_manager",return_value=self.dm), \
             patch.object(trading_executor,"get_data_manager",return_value=self.dm), \
             patch.object(position_manager,"_create_qmt_trader",return_value=StartupTrader()), \
             patch.object(original,"start_sync_thread",sync), \
             patch.object(original,"_record_external_trade_after_callback",record), \
             patch.object(trading_executor.TradingExecutor,"_init_xttrader"):
            pm=position_manager.get_position_manager()
            self.addCleanup(pm.memory_conn.close)
            for worker in workers:
                worker.join(2)
                self.assertFalse(worker.is_alive(), "启动回调不能与单例初始化互相等待")
            self.assertEqual(constructed,[True])
            self.assertEqual(records,[pm])
            self.assertIs(trading_executor.get_trading_executor().position_manager,pm)

    def test_failed_connection_does_not_register_callbacks(self):
        pm,_,events,_,_,_=self.manager(connected=False)
        self.assertFalse(pm.qmt_connected)
        self.assertEqual(events,["connect","sync"])


class TestIpcLateCallbacks(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        p=patch.object(config,"QMT_IPC_ROOT",str(self.root))
        p.start()
        self.addCleanup(p.stop)
        self.trader=QmtIpcTrader(account="STARTUP_SIM")
        self.trader._ensure_dirs()
        self.rec=dict(order_id=101,status="filled",action="sell",stock_code="603466.SH",
            total_volume=100,filled_volume=100,filled_price=10,filled_amount=1000,
            orig_timestamp="2026-11-27 10:00:00",
            deals=[dict(trade_id="F1",time="2026-11-27 10:00:01",volume=100,price=10,amount=1000)])

    def poll(self):
        path=Path(self.trader._dir("orders","done","ord_101.json"))
        path.write_text(json.dumps(self.rec),encoding="utf-8")
        self.trader._poller_stop=False
        def finish(_):
            self.trader._poller_stop=True
        with patch.object(self.trader,"_is_qmt_alive",return_value=True), \
             patch("qmt_ipc_trader.time.sleep",side_effect=finish):
            self.trader._deal_poll_loop()

    def test_native_deal_is_not_consumed_before_callback_registration(self):
        self.poll()
        received=[]
        self.trader.register_trade_callback(received.append)
        self.poll()
        self.poll()
        self.assertEqual([row.traded_id for row in received],["F1"])

    def test_legacy_cumulative_deal_is_not_consumed_before_registration(self):
        self.rec.pop("deals")
        self.poll()
        received=[]
        self.trader.register_trade_callback(received.append)
        self.poll()
        self.poll()
        self.assertEqual([row.traded_volume for row in received],[100])

    def test_terminal_order_waits_for_registered_consumer(self):
        self.poll()
        received=[]
        self.trader.register_order_callback(received.append)
        self.poll()
        self.poll()
        self.assertEqual([row.order_status for row in received],[56])

    def test_order_callback_failure_can_retry_without_marking_success(self):
        attempts=[]
        def callback(order):
            attempts.append(order.order_id)
            if len(attempts)==1:
                raise RuntimeError("临时投递失败")
        self.trader.register_order_callback(callback)
        self.poll()
        self.poll()
        self.poll()
        self.assertEqual(attempts,[101,101])

    def test_absent_trade_consumer_does_not_advance_deal_cache(self):
        self.poll()
        self.assertEqual(self.trader._seen_deals,{})
        self.assertEqual(self.trader._seen_native_deals,set())


class TestStartupSingletons(unittest.TestCase):
    def singleton(self,module,constructor,getter):
        entered,release=threading.Event(),threading.Event()
        created,received=[],[]
        class Instance:
            def __init__(inner):
                created.append(inner)
                entered.set()
                release.wait(2)
        def request():
            received.append(getattr(module,getter)())
        with patch.object(module,"_instance",None),patch.object(module,constructor,Instance):
            first=threading.Thread(target=request)
            second=threading.Thread(target=request)
            first.start()
            self.assertTrue(entered.wait(1))
            second.start()
            second.join(0.05)
            release.set()
            first.join(2)
            second.join(2)
        self.assertEqual(len(created),1)
        self.assertEqual(len(received),2)
        self.assertIs(received[0],received[1])

    def test_position_manager_is_published_once_under_concurrent_startup(self):
        self.singleton(position_manager,"PositionManager","get_position_manager")

    def test_trading_executor_is_published_once_under_concurrent_startup(self):
        self.singleton(trading_executor,"TradingExecutor","get_trading_executor")
