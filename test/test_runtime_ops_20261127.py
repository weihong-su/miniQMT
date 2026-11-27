"""跨日账本、指数历史、买入敞口及 IPC 瞬态读取回归；仅使用虚拟账户。"""
import json
import sqlite3
import threading
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pandas as pd
import config
from trading_executor import TradingExecutor
from data_manager import DataManager
from test.test_ipc_full_chain import _IpcSnapshotBase


class TestCrossDayLedger(unittest.TestCase):
    def setUp(self):
        self.e = TradingExecutor.__new__(TradingExecutor)
        self.e.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(self.e.conn.close)
        self.e.order_cache = {}
        self.e._trade_record_lock = threading.RLock()
        self.e.data_manager = SimpleNamespace(get_stock_name=Mock(return_value="测试"))
        self.e.conn.execute("""CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY, stock_code TEXT, stock_name TEXT, trade_time TEXT,
            trade_type TEXT, price REAL, volume INTEGER, amount REAL, trade_id TEXT,
            commission REAL, strategy TEXT, order_id TEXT, row_status TEXT,
            account TEXT, deal_time_str TEXT)""")
        import settlement_db
        p = patch.dict(settlement_db._SCHEMA_CACHE, {config.DB_PATH: False})
        p.start()
        self.addCleanup(p.stop)

    def fill(self, day="2026-11-26", code="603466", side=24, volume=700, ident="F1"):
        return dict(order_id="ORDER1", stock_code=code, order_type=side,
                    traded_price=16.54, traded_volume=volume, traded_id=ident,
                    traded_time=day+" 14:43:25")

    def seed(self, day="2026-11-26", strategy="grid"):
        self.e.conn.execute("""INSERT INTO trade_records
            (stock_code,trade_time,trade_type,price,volume,amount,trade_id,strategy,
             order_id,row_status,account,deal_time_str)
            VALUES ('603466',?,'SELL',16.54,700,11578,'ORDER1',?,'ORDER1','active',?,?)""",
            (day+" 14:43:26",strategy,config.ACCOUNT_CONFIG.get("account_id"),
             day+" 14:43:25"))
        self.e.conn.commit()

    def test_yesterday_aggregated_grid_is_not_external_again(self):
        self.seed()
        self.e.query_stock_trades = Mock(return_value=[self.fill()])
        with patch.object(config,"ENABLE_SIMULATION_MODE",False):
            self.assertEqual(self.e.reconcile_missing_trades(),0)
        self.assertEqual(self.e.conn.execute("SELECT COUNT(*) FROM trade_records").fetchone()[0],1)

    def test_same_order_id_on_different_days_is_preserved(self):
        self.seed()
        self.e.query_stock_trades = Mock(return_value=[self.fill("2026-11-27",ident="F2")])
        with patch.object(config,"ENABLE_SIMULATION_MODE",False):
            self.assertEqual(self.e.reconcile_missing_trades(),1)

    def test_historical_missing_fill_can_be_backfilled(self):
        self.e.query_stock_trades = Mock(return_value=[self.fill()])
        with patch.object(config,"ENABLE_SIMULATION_MODE",False):
            self.assertEqual(self.e.reconcile_missing_trades(),1)
        row=self.e.conn.execute("SELECT trade_time,strategy FROM trade_records").fetchone()
        self.assertEqual(row,("2026-11-26 14:43:25","external"))

    def test_same_order_other_stock_is_not_swallowed(self):
        self.seed("2026-11-27","external")
        self.e.query_stock_trades = Mock(return_value=[self.fill("2026-11-27",code="001306")])
        with patch.object(config,"ENABLE_SIMULATION_MODE",False):
            self.assertEqual(self.e.reconcile_missing_trades(),1)


class TestIndexRouting(unittest.TestCase):
    def test_ipc_index_respects_common_history_policy(self):
        from autobuy.filter import download_market_index_history
        dm=object.__new__(DataManager)
        dm.xt=SimpleNamespace()
        dm.download_history_xtdata=Mock(side_effect=AssertionError("不可调用原生接口"))
        dm.download_history_data=Mock(return_value=pd.DataFrame({"close":range(60)}))
        with patch.object(config,"ENABLE_QMT_IPC_FALLBACK",True):
            self.assertEqual(len(download_market_index_history(dm,"399001.SZ")),60)
        dm.download_history_xtdata.assert_not_called()

    def test_index_and_stock_exchange_are_distinct(self):
        dm=object.__new__(DataManager)
        dm._ts_cooldown_until=0
        dm._ts_consecutive_failures=0
        dm._record_market_health=Mock()
        pro=Mock()
        frame=pd.DataFrame(dict(trade_date=["20261126"],open=[1],high=[2],
                                low=[1],close=[2],vol=[100],amount=[200]))
        pro.index_daily.return_value=frame
        pro.daily.return_value=frame
        dm._get_tushare_pro=Mock(return_value=pro)
        dm._download_history_tushare("000001.SH",end_date="20261126")
        pro.index_daily.assert_called_once()
        pro.daily.assert_not_called()
        dm._download_history_tushare("000001.SZ",end_date="20261126")
        pro.daily.assert_called_once()


class TestSnapshotRetry(_IpcSnapshotBase):
    def test_temporary_read_denial_recovers(self):
        from qmt_ipc_data import read_snapshot
        self.snapshot()
        original=open
        count=[0]
        def transient(*args,**kwargs):
            count[0]+=1
            if count[0]<3:
                raise PermissionError(13,"临时共享冲突")
            return original(*args,**kwargs)
        with patch("builtins.open",side_effect=transient):
            value=read_snapshot(self.trader.ipc_root,self.acc,"account.json",90)
        self.assertEqual(value["account_id"],self.acc)
        self.assertEqual(count[0],3)

    def test_wrong_account_is_never_retried_into_valid_data(self):
        from qmt_ipc_data import read_snapshot, IpcSnapshotUnavailable
        self.snapshot()
        with patch("qmt_ipc_data.json.load",return_value={"account_id":"WRONG"}) as read:
            with self.assertRaises(IpcSnapshotUnavailable):
                read_snapshot(self.trader.ipc_root,self.acc,"account.json",90)
        self.assertEqual(read.call_count,1)


class TestSingleStockExposure(unittest.TestCase):
    def setUp(self):
        self.e=TradingExecutor.__new__(TradingExecutor)
        self.e.order_cache={}
        self.e.trade_lock=threading.Lock()
        self.e.position_manager=SimpleNamespace(qmt_trader=Mock())
        self.trader=self.e.position_manager.qmt_trader
        self.trader.position.return_value=pd.DataFrame([{
            "证券代码":"603466","股票余额":3700,"可用余额":700,"市值":55500,"市价":15}])
        self.trader.query_stock_orders.return_value=pd.DataFrame()
        self.trader.select_slippage.side_effect=lambda **kw:kw["price"]
        self.e._buy_risk_lock=threading.RLock()

    def check(self,volume=1500):
        return self.e._reserve_auto_buy("603466.SH",15,volume,"grid")

    def test_t1_holdings_count_toward_cap(self):
        self.assertIsNone(self.check())

    def test_under_cap_reserves_full_quantity(self):
        key=self.check(500)
        self.assertIsNotNone(key)

    def test_concurrent_reservations_cannot_overbuy(self):
        accepted=[]
        barrier=threading.Barrier(2)
        def submit():
            barrier.wait()
            accepted.append(self.check(700))
        threads=[threading.Thread(target=submit) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(2)
        self.assertEqual(sum(key is not None for key in accepted),1)

    def test_unavailable_positions_fail_closed(self):
        self.trader.position.side_effect=RuntimeError("快照不可用")
        self.assertIsNone(self.check(100))

    def order(self,oid="B1",side=23,volume=700,filled=0,status=50):
        return {"订单编号":oid,"证券代码":"603466","委托类型":side,
                "委托数量":volume,"成交数量":filled,"委托状态":status,"委托价格":15}

    def test_partial_fill_and_remaining_are_both_reserved(self):
        key=self.check(700)
        self.e._update_buy_reservation(key,"B1")
        self.trader.query_stock_orders.return_value=[self.order(filled=300,status=55)]
        self.assertIsNone(self.check(300))

    def test_filled_order_waits_for_position_quantity(self):
        key=self.check(700)
        self.e._update_buy_reservation(key,"B1")
        self.trader.query_stock_orders.return_value=[self.order(filled=700,status=56)]
        self.assertIsNone(self.check(300))
        self.trader.position.return_value.iloc[0,self.trader.position.return_value.columns.get_loc("股票余额")]=4400
        self.trader.position.return_value.iloc[0,self.trader.position.return_value.columns.get_loc("市值")]=66000
        self.assertIsNotNone(self.check(200))

    def test_cancel_releases_only_unfilled_quantity(self):
        key=self.check(700)
        self.e._update_buy_reservation(key,"B1")
        self.trader.query_stock_orders.return_value=[self.order(filled=300,status=53)]
        self.assertIsNotNone(self.check(500))

    def test_pending_sell_does_not_release_holdings(self):
        self.trader.query_stock_orders.return_value=[self.order("S1",side=24,volume=1500)]
        self.assertIsNone(self.check(1500))

    def test_unknown_submission_reservation_does_not_expire(self):
        key=self.check(700)
        self.e.order_cache[key]["order_time"]=datetime(2000,1,1)
        self.assertIsNone(self.check(700))

    def test_broker_pending_order_is_included_after_restart(self):
        self.trader.query_stock_orders.return_value=[self.order()]
        self.assertIsNone(self.check(700))

    def test_risk_io_runs_outside_reservation_lock(self):
        def positions():
            self.assertFalse(self.e._buy_risk_lock._is_owned())
            return []
        self.trader.position.side_effect=positions
        self.assertIsNotNone(self.check(100))

    def test_missing_snapshot_is_not_empty_account(self):
        self.trader.position.return_value=None
        self.assertIsNone(self.check(100))

    def test_slippage_is_included_in_reserve(self):
        self.trader.select_slippage.side_effect=None
        self.trader.select_slippage.return_value=16
        self.assertIsNone(self.check(700))


class TestBuyEntryIntegration(unittest.TestCase):
    def setUp(self):
        from test.test_order_rejection import TestOrderRejection
        self.h=TestOrderRejection()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)
        self.e=self.h._make_executor()
        self.e._save_trade_record=Mock(return_value=True)
        self.e.position_manager.get_position.return_value=None
        self.e._ensure_trade_push_ready_before_submit=Mock(return_value=True)
        self.e.position_manager._get_real_order_id.return_value=123456789
        self.e.position_manager.qmt_trader.buy.return_value=123456789
        self.time_patch=patch.object(config,"is_trade_time",return_value=True)
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)

    def test_limit_blocks_even_when_cash_check_raises(self):
        trader=self.e.position_manager.qmt_trader
        trader.position.return_value=[{"证券代码":"603466","股票余额":5200,"市值":78000}]
        trader.check_stock_is_av_buy.side_effect=RuntimeError("检查失败")
        self.assertIsNone(self.e.buy_stock("603466.SH",volume=1500,price=15,strategy="grid"))
        trader.buy.assert_not_called()

    def test_unknown_submit_exception_is_not_retried(self):
        trader=self.e.position_manager.qmt_trader
        trader.buy.side_effect=RuntimeError("发出后失联")
        self.assertIsNone(self.e.buy_stock("603466.SH",volume=100,price=15,strategy="grid"))
        self.assertEqual(trader.buy.call_count,1)
        self.assertEqual(sum(bool(row.get("risk_reservation")) for row in self.e.order_cache.values()),1)

    def test_definite_rejection_releases_reservation(self):
        self.e.position_manager._get_real_order_id.return_value=-1
        self.assertIsNone(self.e.buy_stock("603466.SH",volume=100,price=15,strategy="grid"))
        self.assertFalse(any(row.get("risk_reservation") for row in self.e.order_cache.values()))

    def test_manual_entry_keeps_existing_scope(self):
        self.e.position_manager.qmt_trader.position.side_effect=AssertionError("手动入口不增加自动风控")
        self.assertEqual(self.e.buy_stock("603466.SH",volume=100,price=15,strategy="M_real"),123456789)


class TestIpcReadOnce(_IpcSnapshotBase):
    def test_readiness_reads_each_snapshot_once(self):
        from qmt_ipc_data import QmtIpcDataAdapter
        self.snapshot()
        now=datetime.now().isoformat()
        for name,value in (("heartbeat.json",dict(account_id=self.acc,ts=now)),
            ("quotes.json",dict(account_id=self.acc,timestamp=now,quotes={
                "000001.SZ":dict(lastPrice=10,time=int(time.time()*1000))}))):
            with open(self.trader._dir("status",name),"w",encoding="utf-8") as handle:
                json.dump(value,handle)
        original=open
        calls=[]
        def track(path,*args,**kwargs):
            calls.append(str(path))
            return original(path,*args,**kwargs)
        with patch("builtins.open",side_effect=track):
            result=QmtIpcDataAdapter(self.acc).get_readiness(["000001"])
        self.assertTrue(result["account"]["available"])
        self.assertEqual(sum(path.endswith("account.json") for path in calls),1)
        self.assertEqual(sum(path.endswith("quotes.json") for path in calls),1)


class TestDuplicateRepair(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute("""CREATE TABLE trade_records(
            id INTEGER PRIMARY KEY,account TEXT,order_id TEXT,trade_type TEXT,
            fill_ids TEXT,stock_code TEXT,price REAL,volume INTEGER,amount REAL,
            strategy TEXT,trade_id TEXT,is_simulation INTEGER,deal_time_str TEXT,row_status TEXT)""")
        self.conn.execute("""CREATE TABLE run_events(account,event_time,level,event_type,
            code,order_id,trade_id,detail)""")
        for ident,strategy,trade_id in ((1,"grid","O1"),(2,"external","F1")):
            self.conn.execute("INSERT INTO trade_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ident,"VIRTUAL","O1","SELL","F1","603466.SH",16.54,700,11578,
                 strategy,trade_id,0,"2026-11-26 14:43:25","active"))
        self.conn.commit()

    def repair(self,apply=False):
        from scripts.repair_duplicate_trade import repair_duplicate
        return repair_duplicate(self.conn,1,2,apply)

    def test_preview_has_no_mutation(self):
        self.assertFalse(self.repair()["applied"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0],0)

    def test_apply_is_audited_and_idempotent(self):
        self.assertTrue(self.repair(True)["applied"])
        self.assertFalse(self.repair(True)["applied"])
        self.assertEqual(self.conn.execute("SELECT row_status FROM trade_records WHERE id=1").fetchone()[0],"active")
        self.assertEqual(self.conn.execute("SELECT row_status FROM trade_records WHERE id=2").fetchone()[0],"superseded")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0],1)

    def test_different_amount_is_not_repaired(self):
        self.conn.execute("UPDATE trade_records SET amount=12000 WHERE id=2")
        self.conn.commit()
        with self.assertRaises(ValueError):
            self.repair(True)
        self.assertEqual(self.conn.execute("SELECT row_status FROM trade_records WHERE id=2").fetchone()[0],"active")

    def test_different_account_is_not_repaired(self):
        self.conn.execute("UPDATE trade_records SET account='ANOTHER' WHERE id=2")
        self.conn.commit()
        with self.assertRaises(ValueError): self.repair(True)

    def test_audit_failure_rolls_back_status(self):
        self.conn.execute("DROP TABLE run_events")
        self.conn.commit()
        with self.assertRaises(sqlite3.Error): self.repair(True)
        self.assertEqual(self.conn.execute("SELECT row_status FROM trade_records WHERE id=2").fetchone()[0],"active")


class TestLedgerExtraBoundaries(unittest.TestCase):
    def setUp(self):
        self.h=TestCrossDayLedger()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.e=self.h.e

    def test_account_scope_does_not_hide_missing_fill(self):
        self.h.seed("2026-11-27","external")
        self.e.conn.execute("UPDATE trade_records SET account='OTHER_ACCOUNT'")
        self.e.conn.commit()
        self.e.query_stock_trades=Mock(return_value=[self.h.fill("2026-11-27")])
        with patch.object(config,"ENABLE_SIMULATION_MODE",False):
            self.assertEqual(self.e.reconcile_missing_trades(),1)

    def test_other_account_query_result_is_ignored(self):
        fill=dict(self.h.fill(),account_id="OTHER_ACCOUNT")
        self.e.query_stock_trades=Mock(return_value=[fill])
        with patch.object(config,"ENABLE_SIMULATION_MODE",False):
            self.assertEqual(self.e.reconcile_missing_trades(),0)

    def test_stale_order_cache_does_not_change_historical_side_or_strategy(self):
        self.e.order_cache["ORDER1"]=dict(stock_code="603466",trade_type="BUY",
            strategy="grid",order_time=datetime(2026,11,27,9,30))
        self.e.query_stock_trades=Mock(return_value=[self.h.fill()])
        with patch.object(config,"ENABLE_SIMULATION_MODE",False):
            self.assertEqual(self.e.reconcile_missing_trades(),1)
        self.assertEqual(self.e.conn.execute("SELECT trade_type,strategy FROM trade_records").fetchone(),("SELL","external"))


    def test_historical_fill_does_not_delete_today_reused_order_placeholder(self):
        today=datetime.now().strftime("%Y-%m-%d")
        yesterday=(datetime.now()-timedelta(days=1)).strftime("%Y-%m-%d")
        self.h.seed(today,"manual")
        self.e.conn.execute("UPDATE trade_records SET trade_id='ORDER_ORDER1'")
        self.e.conn.commit()
        self.e.query_stock_trades=Mock(return_value=[self.h.fill(yesterday)])
        with patch.object(config,"ENABLE_SIMULATION_MODE",False):
            self.assertEqual(self.e.reconcile_missing_trades(),1)
        self.assertEqual(self.e.conn.execute(
            "SELECT COUNT(*) FROM trade_records WHERE trade_id='ORDER_ORDER1'").fetchone()[0],1)

    def test_superseded_rows_are_hidden_in_web_trade_query(self):
        self.h.seed()
        self.e.conn.execute("UPDATE trade_records SET row_status='superseded'")
        self.e.conn.commit()
        for start,end in ((None,None),("2026-11-26",None),(None,"2026-11-27"),
                          ("2026-11-26","2026-11-27")):
            self.assertTrue(self.e.get_trades(start,end).empty)

    def test_legacy_record_does_not_commit_callers_transaction(self):
        import settlement_db
        record=dict(stock_code="603466",trade_type="BUY",volume=100,price=15,amount=1500,
                    trade_id="F100",strategy="external")
        self.e.conn.execute("BEGIN")
        self.assertEqual(settlement_db.record_trade(record,conn=self.e.conn),"legacy_inserted")
        self.assertTrue(self.e.conn.in_transaction)
        self.e.conn.rollback()
        self.assertEqual(self.e.conn.execute("SELECT COUNT(*) FROM trade_records").fetchone()[0],0)


class TestMacdHistoryState(unittest.TestCase):
    def _advice(self,count,refresh=True):
        from macd_advisor import _compute_advice
        dm=object.__new__(DataManager)
        dates=pd.bdate_range(end="2026-11-26",periods=max(count,1)).strftime("%Y-%m-%d")[:count]
        hist=pd.DataFrame(dict(date=dates,open=[10]*count,high=[11]*count,
            low=[9]*count,close=[10]*count))
        dm.get_history_data_from_db=Mock(return_value=hist)
        dm.download_history_data=Mock(return_value=hist if refresh else None)
        dm.save_history_data=Mock()
        ind=Mock()
        ind.get_indicators_history.return_value=pd.DataFrame(dict(
            date=["2026-11-25","2026-11-26"],macd=[1,2],macd_signal=[0.5,0.6],macd_hist=[1,2]))
        # 先导入指标模块，再模拟数据入口，避免模块缓存模拟函数引用。
        with patch("indicator_calculator.get_indicator_calculator",return_value=ind), \
             patch("data_manager.get_data_manager",return_value=dm), \
             patch.object(dm,"_get_completed_history_end_date",return_value="20261126"):
            return _compute_advice("399001.SZ")

    def test_first_history_missing_is_explicitly_unavailable(self):
        self.assertEqual(self._advice(0)["history_status"],"unavailable")

    def test_insufficient_history_does_not_reuse_old_advice(self):
        result=self._advice(10)
        self.assertEqual(result["status"],"error")
        self.assertEqual(result["history_status"],"insufficient")

    def test_valid_cache_has_original_data_date(self):
        result=self._advice(60,False)
        self.assertEqual(result["status"],"success")
        self.assertEqual(result["history_status"],"cache")
        self.assertEqual(result["history_asof"],"2026-11-26")


class TestAtomicSettlementGuard(unittest.TestCase):
    def setUp(self):
        import db_migrate
        import settlement_db
        self.h=TestCrossDayLedger()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.conn=self.h.e.conn
        self.conn.row_factory=sqlite3.Row
        for name,definition in db_migrate.TRADE_RECORDS_NEW_COLUMNS:
            try: self.conn.execute("ALTER TABLE trade_records ADD COLUMN "+name+" "+definition)
            except sqlite3.OperationalError: pass
        self.conn.commit()
        self.p=patch.dict(settlement_db._SCHEMA_CACHE,{config.DB_PATH:True})
        self.p.start()
        self.addCleanup(self.p.stop)
        self.h.seed()
        self.conn.execute("UPDATE trade_records SET fill_ids='F1'")
        self.conn.commit()

    def record(self,day="2026-11-26",**extra):
        import settlement_db
        value=dict(stock_code="603466",trade_time=day+" 14:43:25",trade_type="SELL",
            price=16.54,volume=700,amount=11578,trade_id="F1",fill_ids="F1",
            order_id="ORDER1",strategy="external",deal_time_str=day+" 14:43:25",
            is_simulation=False)
        value.update(extra)
        return settlement_db.record_trade(value,conn=self.conn)

    def test_grid_aggregate_blocks_delayed_fill_inside_write_transaction(self):
        self.assertEqual(self.record(),"duplicate")
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM trade_records").fetchone()[0],1)

    def test_same_fill_number_on_another_day_is_not_discarded(self):
        self.assertEqual(self.record("2026-11-27"),"inserted")

    def test_different_account_is_not_covered_by_grid_row(self):
        self.assertEqual(self.record(account="OTHER_VIRTUAL"),"inserted")


class TestFurtherRiskAndHistory(unittest.TestCase):
    def test_market_order_uses_upper_limit_for_reserve(self):
        h=TestSingleStockExposure()
        h.setUp()
        h.e._quote_api=Mock(return_value=SimpleNamespace(
            get_instrument_detail=Mock(return_value={"UpStopPrice":20})))
        self.assertIsNone(h.e._reserve_auto_buy("603466.SH",15,500,"grid",5))

    def test_initial_empty_history_records_failure(self):
        dm=object.__new__(DataManager)
        dm._ts_cooldown_until=0
        dm._ts_consecutive_failures=0
        dm._get_tushare_pro=Mock(return_value=SimpleNamespace(index_daily=Mock(return_value=pd.DataFrame())))
        dm._record_market_health=Mock()
        self.assertIsNone(dm._download_history_tushare("399001.SZ"))
        self.assertFalse(dm._record_market_health.call_args.args[3])
        self.assertEqual(dm._record_market_health.call_args.kwargs["reason"],"empty_history")

    def test_transient_read_retry_does_not_accept_expired_source(self):
        h=TestSnapshotRetry()
        h.setUp()
        self.addCleanup(h.tearDown)
        self.addCleanup(h.doCleanups)
        h.snapshot(timestamp="2020-01-01 09:25:00")
        from qmt_ipc_data import read_snapshot, IpcSnapshotUnavailable
        original=open
        count=[0]
        def transient(*args,**kwargs):
            count[0]+=1
            if count[0]==1: raise PermissionError(13,"共享冲突")
            return original(*args,**kwargs)
        with patch("builtins.open",side_effect=transient):
            with self.assertRaises(IpcSnapshotUnavailable):
                read_snapshot(h.trader.ipc_root,h.acc,"account.json",90)
        self.assertEqual(count[0],2)

    def test_continuous_read_denial_is_bounded(self):
        from qmt_ipc_data import read_snapshot, IpcSnapshotUnavailable
        with patch("builtins.open",side_effect=PermissionError(13,"共享冲突")) as read, \
             patch("qmt_ipc_data.time.sleep") as wait:
            with self.assertRaises(IpcSnapshotUnavailable):
                read_snapshot("unused","VIRTUAL","account.json",90)
        self.assertEqual(read.call_count,config.QMT_IPC_READ_RETRIES+1)
        self.assertEqual(wait.call_count,config.QMT_IPC_READ_RETRIES)


class TestMootdxIndexRoute(unittest.TestCase):
    def test_index_api_receives_exchange_and_index_symbol(self):
        import Methods
        client=Mock()
        with patch.object(Methods.Quotes,"factory",return_value=client):
            Methods.getIndexData("000001.SH",offset=120)
            self.assertEqual(client.index.call_args.kwargs["symbol"],"000001")
            self.assertEqual(client.index.call_args.kwargs["market"],1)
            Methods.getIndexData("399001.SZ",offset=120)
            self.assertEqual(client.index.call_args.kwargs["symbol"],"399001")
            self.assertEqual(client.index.call_args.kwargs["market"],0)
        client.bars.assert_not_called()

    def test_common_ipc_history_uses_index_fallback(self):
        import Methods
        dm=object.__new__(DataManager)
        dm.xt=SimpleNamespace()
        dm._should_log_history_no_data_warning=Mock(return_value=False)
        frame=pd.DataFrame(dict(datetime=["2026-11-26"],open=[10],high=[11],
                                low=[9],close=[10],volume=[100],amount=[1000]))
        with patch.object(config,"ENABLE_XTQUANT_MANAGER",False), \
             patch.object(config,"ENABLE_TUSHARE_DATA_SOURCE",False), \
             patch.object(Methods,"getIndexData",return_value=frame) as index, \
             patch.object(Methods,"getStockData",side_effect=AssertionError("指数不能走股票接口")):
            result=dm.download_history_data("399001.SZ",end_date="20261126")
        self.assertIsNotNone(result)
        self.assertEqual(index.call_args.kwargs["code"],"399001.SZ")


class TestRestartFillEvidence(unittest.TestCase):
    def test_filled_order_without_quantity_basis_blocks_new_buy(self):
        h=TestSingleStockExposure()
        h.setUp()
        h.trader.query_stock_orders.return_value=[h.order(filled=700,status=56)]
        self.assertIsNone(h.check(300))

    def test_open_quantity_and_fills_restore_restart_floor(self):
        h=TestSingleStockExposure()
        h.setUp()
        conn=sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE position_snapshot(account,code,snapshot_date,snapshot_type,source,volume)")
        import settlement_db
        conn.execute("INSERT INTO position_snapshot VALUES(?,?,?,?,?,?)",
            (settlement_db.get_account_id(),"603466",datetime.now().strftime("%Y-%m-%d"),
             "open","memory_db",3700))
        conn.commit()
        h.e.conn=conn
        order=h.order(filled=700,status=56)
        order["报单时间"]=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        h.trader.query_stock_orders.return_value=[order]
        self.assertIsNone(h.check(300))


class TestLateOrderMapping(unittest.TestCase):
    def test_late_seq_mapping_moves_risk_reservation_to_real_order(self):
        h=TestSingleStockExposure()
        h.setUp()
        e=h.e
        key=h.check(700)
        e._update_buy_reservation(key,100)
        e._mark_unknown_order_submission("603466.SH","BUY",100,15,700,"grid")
        h.trader.order_id_map={100:200}
        self.assertFalse(e._has_recent_unknown_order_submission("603466.SH","BUY"))
        self.assertEqual(e.order_cache[key]["risk_order_id"],200)
        h.trader.query_stock_orders.return_value=[h.order("200")]
        self.assertIsNotNone(h.check(200))

    def test_unknown_buy_cannot_be_retried_by_disabling_cooldown(self):
        h=TestSingleStockExposure()
        h.setUp()
        h.trader.order_id_map={}
        h.e._mark_unknown_order_submission("603466.SH","BUY",100,15,700,"grid")
        with patch.object(config,"ASYNC_ORDER_UNKNOWN_COOLDOWN_SECONDS",0):
            self.assertTrue(h.e._has_recent_unknown_order_submission("603466.SH","BUY"))
