"""
QMT_trade_executor.py tests -- compact rewrite aligned with current module.
"""
import os, sys, json, time, types, shutil, tempfile, unittest, threading
from contextlib import contextmanager
from unittest.mock import patch
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "qmt-trader"))

_MODULE_TMP = tempfile.mkdtemp(prefix="ipc_exec_module_")
os.environ["QMT_IPC_ROOT"] = _MODULE_TMP

# 测试显式调用 main/handle_account，导入时不能另启守护线程争用同一状态和锁。
_original_top_level_worker = os.environ.get('QMT_IPC_TOP_LEVEL_WORKER')
os.environ['QMT_IPC_TOP_LEVEL_WORKER'] = '0'
try:
    import QMT_trade_executor as ex
finally:
    if _original_top_level_worker is None:
        os.environ.pop('QMT_IPC_TOP_LEVEL_WORKER', None)
    else:
        os.environ['QMT_IPC_TOP_LEVEL_WORKER'] = _original_top_level_worker
ex.DIR_LOG = os.path.join(_MODULE_TMP, "qmt_log")
os.makedirs(ex.DIR_LOG, exist_ok=True)

def tearDownModule():
    shutil.rmtree(_MODULE_TMP, ignore_errors=True)

class FakeOrder:
    def __init__(self, oid, status, price=10.48, vol=1000):
        self.order_id = oid; self.order_status = status
        self.traded_price = price; self.traded_vol = vol

class FakeTrader:
    def __init__(self, fill_status=56, seq=8888):
        self.fill_status = fill_status; self.seq = seq
        self.cancel_called = []; self.orders_placed = []
    def connect(self): pass
    def order_stock(self, aid, sc, act, vol, pt, price, *ex):
        self.orders_placed.append((aid, sc, act, vol, price)); return self.seq
    def query_stock_orders(self, aid, cancelable_only=False):
        return [FakeOrder(self.seq, self.fill_status)]
    def cancel_order(self, aid, seq): self.cancel_called.append(seq)

class SequenceTrader(FakeTrader):
    def __init__(self, states, seq=8888):
        super().__init__(fill_status=states[-1][0], seq=seq)
        self.states = list(states)
        self.query_count = 0
    def query_stock_orders(self, aid, cancelable_only=False):
        idx = min(self.query_count, len(self.states) - 1)
        self.query_count += 1
        status, price, vol = self.states[idx]
        return [FakeOrder(self.seq, status, price=price, vol=vol)]

class _ExecBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ipc_exec_")
        self.acc_dir = os.path.join(self.tmp, "TEST_ACC_1")
        self.acc = {"account_id":"TEST_ACC_1","qmt_path":"C:/QMT/userdata_mini",
                    "dir":self.acc_dir,"dirs":ex._account_dirs(self.acc_dir)}
        ex._SNAPSHOT_STATE.clear()
        ex._LAST_SNAPSHOT_ATTEMPT.clear()
        connect_patch = patch.object(ex, "_try_connect_xttrader", return_value=None)
        heartbeat_patch = patch.object(ex, "_ensure_heartbeat_thread", return_value=False)
        for item in (connect_patch, heartbeat_patch):
            item.start()
            self.addCleanup(item.stop)
    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
    def _write_pending(self, oid, action="buy", stock="000001.SZ",
                       volume=1000, price=10.5, price_type="limit", timeout_sec=2):
        order = dict(version="1.0", order_id=oid, timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     action=action, stock_code=stock, price_type=price_type, price=price,
                     volume=volume, strategy="test", timeout_sec=timeout_sec, remark="")
        path = os.path.join(self.acc["dirs"]["pending"], "ord_%s.json" % oid)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(order, f, ensure_ascii=False)
        return path

# ---- atomic write ----
class TestAtomicWrite(_ExecBase):
    def test_roundtrip_no_tmp(self):
        p = os.path.join(self.tmp, "x.json")
        ex._atomic_write_json(p, {"a":1,"b":"hello"})
        self.assertEqual(json.load(open(p, encoding="utf-8")), {"a":1,"b":"hello"})
        self.assertEqual([f for f in os.listdir(self.tmp) if f.endswith(".tmp")], [])
    def test_load_json_accepts_utf8_bom(self):
        p = os.path.join(self.tmp, "bom.json")
        with open(p, "w", encoding="utf-8-sig") as f:
            json.dump({"v": 1}, f)
        self.assertEqual(ex._load_json(p), {"v": 1})
    def test_overwrite(self):
        p = os.path.join(self.tmp, "x.json")
        ex._atomic_write_json(p, {"v":1}); ex._atomic_write_json(p, {"v":2})
        self.assertEqual(json.load(open(p, encoding="utf-8"))["v"], 2)

    def test_concurrent_writes_do_not_share_tmp_name(self):
        p = os.path.join(self.tmp, "x.json")
        errors = []
        def write(i):
            try:
                ex._atomic_write_json(p, {"v": i})
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=write, args=(i,)) for i in range(20)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(errors, [])
        self.assertEqual([f for f in os.listdir(self.tmp) if f.endswith(".tmp")], [])

# ---- write_done ----
class TestWriteDone(_ExecBase):
    def test_done_and_cleanup(self):
        pr = os.path.join(self.acc["dirs"]["processing"], "ord_100.json")
        with open(pr, "w", encoding="utf-8") as f:
            json.dump({"order_id":100,"volume":1000,"action":"buy","stock_code":"000001.SZ"}, f)
        ex.write_done(self.acc["dirs"], pr,
                      {"order_id":100,"volume":1000,"action":"buy","stock_code":"000001.SZ"},
                      "filled", "ok", filled_price=10.5, filled_vol=1000)
        self.assertTrue(os.path.exists(os.path.join(self.acc["dirs"]["done"],"ord_100.json")))
        self.assertFalse(os.path.exists(pr))
    def test_error_field(self):
        pr = os.path.join(self.acc["dirs"]["processing"], "ord_101.json")
        ex.write_done(self.acc["dirs"], pr,
                      {"order_id":101,"volume":1000,"action":"buy","stock_code":"000001.SZ"},
                      "rejected", "no cash")
        rec = json.load(open(os.path.join(self.acc["dirs"]["done"],"ord_101.json"), encoding="utf-8"))
        self.assertEqual(rec["status"], "rejected"); self.assertEqual(rec["error"], "no cash")

# ---- discover_accounts ----
class TestDiscoverAccounts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ipc_disc_")
        self._r, self._f = ex.IPC_ROOT, ex.ACCOUNT_FILTER
        ex.IPC_ROOT = self.tmp; ex.ACCOUNT_FILTER = ""
    def tearDown(self):
        ex.IPC_ROOT = self._r; ex.ACCOUNT_FILTER = self._f
        shutil.rmtree(self.tmp, ignore_errors=True)
    def _mk(self, aid, qp):
        d = os.path.join(self.tmp, aid); os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"account_id": aid, "qmt_path": qp}, f)
    def test_multi(self):
        self._mk("A1","C:/"); self._mk("A2","C:/")
        self.assertEqual(sorted(a["account_id"] for a in ex.discover_accounts()), ["A1","A2"])
    def test_skip_no_config(self):
        os.makedirs(os.path.join(self.tmp, "no_cfg"), exist_ok=True)
        self._mk("A1","C:/")
        self.assertEqual([a["account_id"] for a in ex.discover_accounts()], ["A1"])
    def test_filter(self):
        self._mk("A1","C:/"); self._mk("A2","C:/"); ex.ACCOUNT_FILTER = "A2"
        self.assertEqual([a["account_id"] for a in ex.discover_accounts()], ["A2"])
    def test_legacy_flat(self):
        os.makedirs(os.path.join(self.tmp, "orders"), exist_ok=True)
        accs = ex.discover_accounts()
        self.assertEqual(len(accs), 1); self.assertEqual(accs[0]["dir"], self.tmp)

# ---- leftovers ----
class TestRecoverLeftovers(_ExecBase):
    def test_stale(self):
        pr = os.path.join(self.acc["dirs"]["processing"], "ord_200.json")
        with open(pr, "w", encoding="utf-8") as f:
            json.dump({"order_id":200,"volume":1000}, f)
        os.utime(pr, (time.time()-(ex.PROCESSING_STALE_SEC+10),)*2)
        ex.recover_leftovers(self.acc)
        self.assertFalse(os.path.exists(pr))
        d = os.path.join(self.acc["dirs"]["done"], "ord_200.json")
        self.assertTrue(os.path.exists(d))
        self.assertEqual(json.load(open(d, encoding="utf-8"))["status"], "error")
    def test_fresh_untouched(self):
        pr = os.path.join(self.acc["dirs"]["processing"], "ord_201.json")
        with open(pr, "w", encoding="utf-8") as f:
            json.dump({"order_id":201,"volume":1000}, f)
        ex.recover_leftovers(self.acc)
        self.assertTrue(os.path.exists(pr))
    def test_no_resubmit(self):
        pr = os.path.join(self.acc["dirs"]["processing"], "ord_202.json")
        with open(pr, "w", encoding="utf-8") as f:
            json.dump({"order_id":202,"volume":1000}, f)
        os.utime(pr, (time.time()-(ex.PROCESSING_STALE_SEC+10),)*2)
        ex.recover_leftovers(self.acc)
        self.assertEqual([f for f in os.listdir(self.acc["dirs"]["pending"]) if f.endswith(".json")], [])

# ---- archive ----
class TestArchiveOldDone(_ExecBase):
    def test_moved(self):
        od = os.path.join(self.acc["dirs"]["done"], "ord_1.json")
        nd = os.path.join(self.acc["dirs"]["done"], "ord_2.json")
        for p in (od, nd):
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"order_id": os.path.basename(p)}, f)
        os.utime(od, (time.time()-(ex.DONE_RETENTION_SEC+100),)*2)
        ex.archive_old(self.acc)
        self.assertFalse(os.path.exists(od)); self.assertTrue(os.path.exists(nd))
        self.assertTrue(os.path.exists(os.path.join(self.acc["dir"],"orders","done_archive","ord_1.json")))

# ---- process_one_order ----
class TestProcessOrderFlow(_ExecBase):
    def test_buy_filled(self):
        path = self._write_pending(300, action="buy")
        fake = FakeTrader(fill_status=56, seq=8888)
        _orig_connect = ex._try_connect_xttrader
        ex._try_connect_xttrader = lambda qp: fake
        try:
            ex.process_one_order(self.acc, path)
        finally:
            ex._try_connect_xttrader = _orig_connect
        rec = json.load(open(os.path.join(self.acc["dirs"]["done"],"ord_300.json"), encoding="utf-8"))
        self.assertEqual(rec["status"], "filled")
        placed_account = fake.orders_placed[0][0]
        self.assertEqual(getattr(placed_account, "account_id", placed_account), "TEST_ACC_1")

    def test_timeout(self):
        path = self._write_pending(301, action="buy", timeout_sec=1)
        fake = FakeTrader(fill_status=48, seq=9001)
        _o = ex._try_connect_xttrader; ex._try_connect_xttrader = lambda qp: fake
        try: ex.process_one_order(self.acc, path)
        finally: ex._try_connect_xttrader = _o
        rec = json.load(open(os.path.join(self.acc["dirs"]["done"],"ord_301.json"), encoding="utf-8"))
        self.assertEqual(rec["status"], "cancelled_timeout")
        self.assertIn(9001, fake.cancel_called)

    def test_partial_waits_until_full_fill(self):
        path = self._write_pending(305, action="buy", timeout_sec=2)
        fake = SequenceTrader([(55, 10.1, 200), (56, 10.2, 1000)], seq=9500)
        _o = ex._try_connect_xttrader; ex._try_connect_xttrader = lambda qp: fake
        try: ex.process_one_order(self.acc, path)
        finally: ex._try_connect_xttrader = _o
        with open(os.path.join(self.acc["dirs"]["done"], "ord_305.json"), encoding="utf-8") as f:
            rec = json.load(f)
        self.assertEqual(rec["status"], "filled")
        self.assertEqual(rec["filled_volume"], 1000)
        self.assertEqual(fake.cancel_called, [])

    def test_timeout_preserves_partial_volume(self):
        path = self._write_pending(306, action="buy", timeout_sec=1)
        fake = FakeTrader(fill_status=55, seq=9600)
        fake.query_stock_orders = lambda aid, cancelable_only=False: [FakeOrder(9600, 55, price=10.2, vol=200)]
        _o = ex._try_connect_xttrader; ex._try_connect_xttrader = lambda qp: fake
        try: ex.process_one_order(self.acc, path)
        finally: ex._try_connect_xttrader = _o
        with open(os.path.join(self.acc["dirs"]["done"], "ord_306.json"), encoding="utf-8") as f:
            rec = json.load(f)
        self.assertEqual(rec["status"], "partial")
        self.assertEqual(rec["filled_volume"], 200)
        self.assertIn(9600, fake.cancel_called)

    def test_rejected_order_status_writes_rejected(self):
        path = self._write_pending(304, action="sell", timeout_sec=1)
        fake = FakeTrader(fill_status=57, seq=9400)
        _o = ex._try_connect_xttrader; ex._try_connect_xttrader = lambda qp: fake
        try: ex.process_one_order(self.acc, path)
        finally: ex._try_connect_xttrader = _o
        rec = json.load(open(os.path.join(self.acc["dirs"]["done"],"ord_304.json"), encoding="utf-8"))
        self.assertEqual(rec["status"], "rejected")
        self.assertEqual(fake.cancel_called, [])

    def test_midflight_cancel(self):
        path = self._write_pending(302, action="buy", timeout_sec=3)
        cp = os.path.join(self.acc["dirs"]["cancel"], "cancel_302.json")
        def delayed():
            time.sleep(0.7)
            with open(cp, "w", encoding="utf-8") as f:
                json.dump({"order_id": 302}, f)
        threading.Thread(target=delayed, daemon=True).start()
        fake = FakeTrader(fill_status=48, seq=9100)
        _o = ex._try_connect_xttrader; ex._try_connect_xttrader = lambda qp: fake
        try: ex.process_one_order(self.acc, path)
        finally: ex._try_connect_xttrader = _o
        self.assertIn(9100, fake.cancel_called)
        self.assertFalse(os.path.exists(cp))

    def test_cancel_before_submit(self):
        path = self._write_pending(303, action="buy")
        cp = os.path.join(self.acc["dirs"]["cancel"], "cancel_303.json")
        with open(cp, "w", encoding="utf-8") as f:
            json.dump({"order_id": 303}, f)
        fake = FakeTrader(fill_status=56, seq=9200)
        _o = ex._try_connect_xttrader; ex._try_connect_xttrader = lambda qp: fake
        try: ex.process_one_order(self.acc, path)
        finally: ex._try_connect_xttrader = _o
        rec = json.load(open(os.path.join(self.acc["dirs"]["done"],"ord_303.json"), encoding="utf-8"))
        self.assertEqual(rec["status"], "cancelled")
        self.assertEqual(fake.orders_placed, [])

    def test_cancel_before_submit_accepts_utf8_bom_order(self):
        oid = "bom_pre_cancel"
        path = os.path.join(self.acc["dirs"]["pending"], "ord_%s.json" % oid)
        order = dict(version="1.0", order_id=oid, timestamp="2026-07-12 10:00:00.000",
                     action="sell", stock_code="000001.SZ", price_type="limit",
                     price=999.99, volume=100, strategy="test", timeout_sec=1, remark="")
        with open(path, "w", encoding="utf-8-sig") as f:
            json.dump(order, f)
        cp = os.path.join(self.acc["dirs"]["cancel"], "cancel_%s.json" % oid)
        with open(cp, "w", encoding="utf-8") as f:
            json.dump({"order_id": oid}, f)
        fake = FakeTrader(fill_status=56, seq=9300)
        _o = ex._try_connect_xttrader; ex._try_connect_xttrader = lambda qp: fake
        try: ex.process_one_order(self.acc, path)
        finally: ex._try_connect_xttrader = _o
        rec = json.load(open(os.path.join(self.acc["dirs"]["done"], "ord_%s.json" % oid), encoding="utf-8"))
        self.assertEqual(rec["status"], "cancelled")
        self.assertEqual(fake.orders_placed, [])
        self.assertFalse(os.path.exists(cp))

    def test_invalid_order_json_writes_error_done(self):
        path = os.path.join(self.acc["dirs"]["pending"], "ord_bad_json.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{bad json")
        ex.process_one_order(self.acc, path)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.exists(os.path.join(self.acc["dirs"]["processing"], "ord_bad_json.json")))
        rec = json.load(open(os.path.join(self.acc["dirs"]["done"], "ord_bad_json.json"), encoding="utf-8"))
        self.assertEqual(rec["status"], "error")
        self.assertEqual(rec["error"], "invalid order json")

# ---- handle_account heartbeat ----
class TestHandleAccountHeartbeat(_ExecBase):
    def test_heartbeat(self):
        ex.handle_account(self.acc)
        hb = os.path.join(self.acc["dirs"]["status"], "heartbeat.json")
        self.assertTrue(os.path.exists(hb))
        self.assertEqual(json.load(open(hb, encoding="utf-8"))["account_id"], "TEST_ACC_1")
        self.assertEqual([f for f in os.listdir(self.acc["dirs"]["status"]) if f.endswith(".tmp")], [])

    def test_slow_snapshot_does_not_block_heartbeat(self):
        started = threading.Event()
        _orig_write_snapshot = ex.write_snapshot
        _orig_archive_old = ex.archive_old
        def slow_snapshot(acc):
            started.set()
            time.sleep(1.2)
            return False
        ex.write_snapshot = slow_snapshot
        ex.archive_old = lambda acc: None
        try:
            t0 = time.time()
            ex.handle_account(self.acc)
            elapsed = time.time() - t0
        finally:
            ex.write_snapshot = _orig_write_snapshot
            ex.archive_old = _orig_archive_old
        hb = os.path.join(self.acc["dirs"]["status"], "heartbeat.json")
        self.assertTrue(os.path.exists(hb))
        self.assertTrue(started.wait(0.5))
        self.assertLess(elapsed, 0.5)

    def test_main_starts_all_heartbeat_threads_before_handling_accounts(self):
        acc2_dir = os.path.join(self.tmp, "TEST_ACC_2")
        acc2 = {"account_id":"TEST_ACC_2","qmt_path":"C:/QMT/userdata_mini",
                "dir":acc2_dir,"dirs":ex._account_dirs(acc2_dir)}
        calls = []
        old_discover = ex.discover_accounts
        old_ensure = ex._ensure_heartbeat_thread
        old_handle = ex.handle_account
        old_interval = ex.MAIN_INTERVAL_SEC
        old_last_run = ex._MAIN_STATE.get("last_run", 0)
        ex.discover_accounts = lambda: [self.acc, acc2]
        ex._ensure_heartbeat_thread = lambda acc: calls.append(("hb", acc["account_id"]))
        def handle(acc):
            calls.append(("handle", acc["account_id"]))
            self.assertEqual(
                calls[:2],
                [("hb", "TEST_ACC_1"), ("hb", "TEST_ACC_2")]
            )
        ex.handle_account = handle
        ex.MAIN_INTERVAL_SEC = 0
        ex._MAIN_STATE["last_run"] = 0
        try:
            ex.main()
        finally:
            ex.discover_accounts = old_discover
            ex._ensure_heartbeat_thread = old_ensure
            ex.handle_account = old_handle
            ex.MAIN_INTERVAL_SEC = old_interval
            ex._MAIN_STATE["last_run"] = old_last_run
        self.assertIn(("handle", "TEST_ACC_1"), calls)
        self.assertIn(("handle", "TEST_ACC_2"), calls)

class TestLogRotation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ipc_log_")
        self.old_dir = ex.DIR_LOG
        self.old_max = ex.LOG_MAX_BYTES
        self.old_count = ex.LOG_BACKUP_COUNT
        ex.DIR_LOG = self.tmp
        ex.LOG_MAX_BYTES = 64
        ex.LOG_BACKUP_COUNT = 2

    def tearDown(self):
        ex.DIR_LOG = self.old_dir
        ex.LOG_MAX_BYTES = self.old_max
        ex.LOG_BACKUP_COUNT = self.old_count
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _log_path(self):
        return os.path.join(self.tmp, "log_%s.txt" % time.strftime("%Y%m%d"))

    def test_log_rotates_when_size_limit_reached(self):
        path = self._log_path()
        with open(path, "w", encoding="utf-8") as f:
            f.write("x" * 80)
        ex.log("after rotate")
        self.assertTrue(os.path.exists(path))
        self.assertTrue(os.path.exists(path + ".1"))
        self.assertLess(os.path.getsize(path), os.path.getsize(path + ".1"))
        with open(path, "r", encoding="utf-8") as f:
            self.assertIn("after rotate", f.read())

    def test_log_keeps_limited_backups(self):
        path = self._log_path()
        with open(path, "w", encoding="utf-8") as f:
            f.write("current" * 20)
        with open(path + ".1", "w", encoding="utf-8") as f:
            f.write("old1")
        with open(path + ".2", "w", encoding="utf-8") as f:
            f.write("old2")
        ex.log("new current")
        with open(path + ".1", "r", encoding="utf-8") as f:
            self.assertIn("current", f.read())
        with open(path + ".2", "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "old1")

class TestWorkerDaemonPolicy(unittest.TestCase):
    def test_local_top_level_worker_is_daemon(self):
        old = os.environ.pop("QMT_IPC_WORKER_DAEMON", None)
        try:
            self.assertTrue(ex._worker_daemon_for("top-level"))
        finally:
            if old is not None:
                os.environ["QMT_IPC_WORKER_DAEMON"] = old

    def test_qmt_callback_worker_is_not_daemon(self):
        old = os.environ.pop("QMT_IPC_WORKER_DAEMON", None)
        try:
            self.assertFalse(ex._worker_daemon_for("init"))
            self.assertFalse(ex._worker_daemon_for("handlebar"))
        finally:
            if old is not None:
                os.environ["QMT_IPC_WORKER_DAEMON"] = old

    def test_env_override(self):
        old = os.environ.get("QMT_IPC_WORKER_DAEMON")
        try:
            os.environ["QMT_IPC_WORKER_DAEMON"] = "0"
            self.assertFalse(ex._worker_daemon_for("top-level"))
            os.environ["QMT_IPC_WORKER_DAEMON"] = "1"
            self.assertTrue(ex._worker_daemon_for("init"))
        finally:
            if old is None:
                os.environ.pop("QMT_IPC_WORKER_DAEMON", None)
            else:
                os.environ["QMT_IPC_WORKER_DAEMON"] = old

    def test_top_level_worker_stays_daemon_even_with_qmt_symbols(self):
        old = os.environ.pop("QMT_IPC_WORKER_DAEMON", None)
        old_symbol = getattr(ex, "passorder", None)
        ex.passorder = lambda *args: None
        try:
            self.assertTrue(ex._worker_daemon_for("top-level"))
        finally:
            if old_symbol is None:
                delattr(ex, "passorder")
            else:
                ex.passorder = old_symbol
            if old is not None:
                os.environ["QMT_IPC_WORKER_DAEMON"] = old

    def test_foreground_loop_only_for_qmt_callbacks_by_default(self):
        old = os.environ.pop("QMT_IPC_FOREGROUND_LOOP", None)
        old_symbol = getattr(ex, "passorder", None)
        try:
            if old_symbol is not None:
                delattr(ex, "passorder")
            self.assertFalse(ex._foreground_loop_enabled("init"))
            ex.passorder = lambda *args: None
            self.assertTrue(ex._foreground_loop_enabled("init"))
            self.assertFalse(ex._foreground_loop_enabled("top-level"))
        finally:
            if old_symbol is None:
                if hasattr(ex, "passorder"):
                    delattr(ex, "passorder")
            else:
                ex.passorder = old_symbol
            if old is not None:
                os.environ["QMT_IPC_FOREGROUND_LOOP"] = old

    def test_top_level_worker_disabled_in_qmt_runtime(self):
        old = os.environ.pop("QMT_IPC_TOP_LEVEL_WORKER", None)
        old_symbol = getattr(ex, "passorder", None)
        try:
            if old_symbol is not None:
                delattr(ex, "passorder")
            self.assertTrue(ex._top_level_worker_enabled())
            ex.passorder = lambda *args: None
            self.assertFalse(ex._top_level_worker_enabled())
        finally:
            if old_symbol is None:
                if hasattr(ex, "passorder"):
                    delattr(ex, "passorder")
            else:
                ex.passorder = old_symbol
            if old is not None:
                os.environ["QMT_IPC_TOP_LEVEL_WORKER"] = old

    def test_top_level_worker_env_override(self):
        old = os.environ.get("QMT_IPC_TOP_LEVEL_WORKER")
        old_symbol = getattr(ex, "passorder", None)
        ex.passorder = lambda *args: None
        try:
            os.environ["QMT_IPC_TOP_LEVEL_WORKER"] = "1"
            self.assertTrue(ex._top_level_worker_enabled())
            os.environ["QMT_IPC_TOP_LEVEL_WORKER"] = "0"
            self.assertFalse(ex._top_level_worker_enabled())
        finally:
            if old_symbol is None:
                delattr(ex, "passorder")
            else:
                ex.passorder = old_symbol
            if old is None:
                os.environ.pop("QMT_IPC_TOP_LEVEL_WORKER", None)
            else:
                os.environ["QMT_IPC_TOP_LEVEL_WORKER"] = old


class TestNativeAccountSnapshot(_ExecBase):
    def _reader(self, account_id, account_type, kind=None):
        if kind is None:
            raise TypeError("three arguments required")
        self.assertIs(threading.current_thread(), threading.main_thread())
        if kind.upper() == "ACCOUNT":
            return [types.SimpleNamespace(m_dBalance=468119.14, m_dAvailable=243333.14,
                                          m_dFrozen=0.0)]
        return [
            types.SimpleNamespace(m_strAccountID=account_id, m_strInstrumentID=code,
                                  m_nVolume=volume, m_nCanUseVolume=available,
                                  m_dOpenPrice=cost, m_dLastPrice=price,
                                  m_dMarketValue=volume * price)
            for code, volume, available, cost, price in [
                ("603466", 3800, 3800, 8.094, 14.30),
                ("001306", 1400, 1400, 57.109, 48.48),
                ("300792", 5500, 5500, 23.29, 18.66),
            ]
        ]

    def _snapshot_path(self):
        return os.path.join(self.acc["dirs"]["status"], "account.json")

    def _previous_snapshot(self):
        value = {"timestamp": "2026-07-13 14:20:26", "positions": [{"stock": "OLD"}]}
        ex._atomic_write_json(self._snapshot_path(), value)
        return value

    def test_native_reader_is_preferred_over_unavailable_xttrader(self):
        with patch.object(ex, "_get_vba_reader", return_value=self._reader), \
             patch.object(ex, "_snapshot_from_xttrader", return_value=None) as xt_read:
            self.assertTrue(ex.write_snapshot(self.acc))
        xt_read.assert_not_called()
        snapshot = ex._load_json(self._snapshot_path())
        self.assertEqual(snapshot["source"], "vba")
        self.assertEqual(snapshot["total_asset"], 468119.14)
        self.assertEqual(snapshot["available"], 243333.14)
        self.assertEqual([p["volume"] for p in snapshot["positions"]], [3800, 1400, 5500])
        self.assertEqual([p["available"] for p in snapshot["positions"]], [3800, 1400, 5500])

    def test_native_snapshot_task_runs_on_main_thread(self):
        with patch.object(ex, "_get_vba_reader", return_value=self._reader), \
             patch.object(ex, "_snapshot_from_xttrader", return_value=None), \
             patch.object(ex, "archive_old"):
            self.assertTrue(ex._start_snapshot_task(self.acc))
            thread = ex._SNAPSHOT_STATE.get(self.acc["account_id"], {}).get("thread")
            if thread is not None:
                thread.join(timeout=1)
        snapshot = ex._load_json(self._snapshot_path())
        self.assertIsNotNone(snapshot)
        self.assertEqual(len(snapshot["positions"]), 3)

    def test_native_reader_is_not_called_from_background_thread(self):
        results = []
        with patch.object(ex, "_get_vba_reader", return_value=self._reader), \
             patch.object(ex, "_snapshot_from_xttrader", return_value=None), \
             patch.object(ex, "_atomic_write_json") as write:
            thread = threading.Thread(target=lambda: results.append(ex.write_snapshot(self.acc)))
            thread.start()
            thread.join(timeout=1)
        self.assertEqual(results, [False])
        write.assert_not_called()

    def test_missing_account_preserves_previous_snapshot(self):
        previous = self._previous_snapshot()
        with patch.object(ex, "_get_vba_reader", return_value=lambda *args: []), \
             patch.object(ex, "_snapshot_from_xttrader", return_value=None) as xt_read:
            self.assertFalse(ex.write_snapshot(self.acc))
        xt_read.assert_not_called()
        self.assertEqual(ex._load_json(self._snapshot_path()), previous)

    def test_position_query_failure_preserves_previous_snapshot(self):
        previous = self._previous_snapshot()
        def reader(*args):
            if args[-1].upper() == "POSITION":
                raise RuntimeError("position query unavailable")
            return self._reader(*args)
        with patch.object(ex, "_get_vba_reader", return_value=reader), \
             patch.object(ex, "_snapshot_from_xttrader", return_value=None):
            self.assertFalse(ex.write_snapshot(self.acc))
        self.assertEqual(ex._load_json(self._snapshot_path()), previous)

    def test_partial_position_parse_failure_does_not_publish_partial_portfolio(self):
        previous = self._previous_snapshot()
        def reader(*args):
            rows = self._reader(*args)
            if args[-1].upper() == "POSITION":
                rows[1].m_nCanUseVolume = "invalid"
            return rows
        with patch.object(ex, "_get_vba_reader", return_value=reader), \
             patch.object(ex, "_snapshot_from_xttrader", return_value=None):
            self.assertFalse(ex.write_snapshot(self.acc))
        self.assertEqual(ex._load_json(self._snapshot_path()), previous)

    def test_missing_available_volume_does_not_default_to_total_volume(self):
        previous = self._previous_snapshot()
        def reader(*args):
            rows = self._reader(*args)
            if args[-1].upper() == "POSITION":
                del rows[0].m_nCanUseVolume
            return rows
        with patch.object(ex, "_get_vba_reader", return_value=reader), \
             patch.object(ex, "_snapshot_from_xttrader", return_value=None):
            self.assertFalse(ex.write_snapshot(self.acc))
        self.assertEqual(ex._load_json(self._snapshot_path()), previous)

    def test_other_account_positions_are_rejected(self):
        previous = self._previous_snapshot()
        def reader(*args):
            rows = self._reader(*args)
            if args[-1].upper() == "POSITION":
                rows[0].m_strAccountID = "OTHER_ACCOUNT"
            return rows
        with patch.object(ex, "_get_vba_reader", return_value=reader), \
             patch.object(ex, "_snapshot_from_xttrader", return_value=None):
            self.assertFalse(ex.write_snapshot(self.acc))
        self.assertEqual(ex._load_json(self._snapshot_path()), previous)

    def test_valid_empty_positions_can_publish_an_empty_portfolio(self):
        def reader(*args):
            return [] if args[-1].upper() == "POSITION" else self._reader(*args)
        with patch.object(ex, "_get_vba_reader", return_value=reader), \
             patch.object(ex, "_snapshot_from_xttrader", return_value=None):
            self.assertTrue(ex.write_snapshot(self.acc))
        self.assertEqual(ex._load_json(self._snapshot_path())["positions"], [])

if __name__ == "__main__":
    unittest.main()
