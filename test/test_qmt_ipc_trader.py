"""
QmtIpcTrader 大QMT文件IPC交易客户端测试

覆盖:
- 连接与心跳检测
- 下单（写 pending + 轮询 done 回执）
- 撤单（写 cancel 文件）
- 持仓/资产快照读取（DataFrame 列契约）
- 委托/成交查询（done 目录聚合）
- 成交回报轮询线程触发 callback
- .xt_trader / .acc / .order_id_map 属性兼容
- 接口契约与 easy_qmt_trader 对齐
"""
import unittest
import os
import sys
import json
import time
import shutil
import tempfile
import threading
from unittest.mock import patch, MagicMock

# 项目根目录 + qmt-trader 目录入 path
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "qmt-trader"))

import config
from qmt_ipc_trader import QmtIpcTrader, _FakeXtTrader, _FakeAccount, _next_order_id, _IPC_STATUS_TO_QMT


class _IpcTestBase(unittest.TestCase):
    """构造临时 IPC 目录的基类。"""

    def setUp(self):
        self.base_root = tempfile.mkdtemp(prefix="ipc_test_")
        # 打 patch：让 config.QMT_IPC_ROOT 指向临时目录
        self._patchers = [
            patch.object(config, "QMT_IPC_ROOT", self.base_root, create=True),
            patch.object(config, "QMT_IPC_ORDER_TIMEOUT", 2, create=True),
            patch.object(config, "QMT_IPC_HEARTBEAT_MAX_AGE", 10, create=True),
            patch.object(config, "QMT_IPC_DEAL_POLL_INTERVAL", 0.1, create=True),
            patch.object(config, "QMT_IPC_DONE_LOOKBACK_SECONDS", 86400, create=True),
            patch.object(config, "USE_SYNC_ORDER_API", True),
        ]
        for p in self._patchers:
            p.start()
        self.trader = QmtIpcTrader(account="TEST_ACC_1", account_type="STOCK")
        # 多账号隔离：trader 工作目录是 base_root/{account_id}/
        self.ipc_root = self.trader.ipc_root
        for d in ["orders/pending", "orders/processing", "orders/done", "cancel", "status"]:
            os.makedirs(os.path.join(self.ipc_root, *d.split("/")), exist_ok=True)
        self._write_account({"available": 1000000.0, "positions": []})

    def tearDown(self):
        try:
            self.trader.stop()
        except Exception:
            pass
        for p in self._patchers:
            p.stop()
        shutil.rmtree(self.base_root, ignore_errors=True)

    # ── 模拟 QMT 端行为 ──

    def _write_heartbeat(self):
        path = os.path.join(self.ipc_root, "status", "heartbeat.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"ts": datetime_now()}, f)

    def _write_account(self, snapshot):
        snapshot = dict(snapshot)
        snapshot.setdefault("account_id", self.trader.account)
        snapshot.setdefault("timestamp", datetime_now())
        path = os.path.join(self.ipc_root, "status", "account.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False)

    def _write_done(self, order_id, status="filled", action="buy", stock="000001.SZ",
                    filled_price=10.5, filled_volume=1000, total_volume=1000):
        path = os.path.join(self.ipc_root, "orders", "done", f"ord_{order_id}.json")
        rec = {
            "version": "1.0",
            "order_id": order_id,
            "status": status,
            "action": action,
            "stock_code": stock,
            "filled_price": filled_price,
            "filled_volume": filled_volume,
            "total_volume": total_volume,
            "strategy": "test",
            "remark": "",
            "error": None,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
        return rec


def datetime_now():
    from datetime import datetime
    return datetime.now().isoformat()


# ── 连接与心跳 ──

class TestConnect(_IpcTestBase):

    def test_connect_when_heartbeat_fresh(self):
        self._write_heartbeat()
        result = self.trader.connect()
        self.assertIsNotNone(result)
        self.assertEqual(result, (self.trader, self.trader))

    def test_connect_when_heartbeat_missing(self):
        # 无心跳文件
        result = self.trader.connect()
        self.assertIsNone(result)

    def test_connect_when_heartbeat_stale(self):
        self._write_heartbeat()
        # 把心跳文件改成很旧
        path = os.path.join(self.ipc_root, "status", "heartbeat.json")
        old = time.time() - 100
        os.utime(path, (old, old))
        result = self.trader.connect()
        self.assertIsNone(result)

    def test_ping_xttrader(self):
        self._write_heartbeat()
        self.assertTrue(self.trader.ping_xttrader())

    def test_ping_xttrader_no_heartbeat(self):
        self.assertFalse(self.trader.ping_xttrader())

    def test_reconnect_xttrader(self):
        self._write_heartbeat()
        self.assertTrue(self.trader.reconnect_xttrader())

    def test_connect_writes_ipc_config(self):
        """connect() 自动把账号写入 config.json，供大QMT端 executor 读取。"""
        self._write_heartbeat()
        t = QmtIpcTrader(account="TEST_ACC_1", path="C:/QMT/userdata_mini")
        t.connect()
        t.stop()
        cfg_path = os.path.join(self.ipc_root, "config.json")
        self.assertTrue(os.path.exists(cfg_path))
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertEqual(cfg["account_id"], "TEST_ACC_1")
        self.assertEqual(cfg["qmt_path"], "C:/QMT/userdata_mini")

    def test_ipc_config_does_not_overwrite_user_qmt_path(self):
        """已填的大QMT路径不被策略端覆盖（用户手填优先）。"""
        cfg_path = os.path.join(self.ipc_root, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"qmt_path": "D:/大QMT/userdata_mini"}, f)
        self._write_heartbeat()
        t = QmtIpcTrader(account="TEST_ACC_1", path="C:/QMT/userdata_mini")
        t.connect()
        t.stop()
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        # 账号被更新，但 qmt_path 保留用户填的值
        self.assertEqual(cfg["account_id"], "TEST_ACC_1")
        self.assertEqual(cfg["qmt_path"], "D:/大QMT/userdata_mini")


# ── 属性兼容 ──

class TestAttributeCompat(_IpcTestBase):

    def test_has_xt_trader_attribute(self):
        self.assertIsInstance(self.trader.xt_trader, _FakeXtTrader)
        self.assertIsNotNone(self.trader.xt_trader)
        # position_manager.py:3800 检查 xt_trader is None or == ''
        self.assertNotEqual(self.trader.xt_trader, '')

    def test_has_acc_attribute(self):
        self.assertIsInstance(self.trader.acc, _FakeAccount)
        self.assertEqual(self.trader.acc.account_id, "TEST_ACC_1")

    def test_has_order_id_map(self):
        self.assertEqual(self.trader.order_id_map, {})

    def test_xt_trader_query_stock_order(self):
        self._write_done(12345, status="filled")
        order = self.trader.xt_trader.query_stock_order(self.trader.acc, 12345)
        self.assertIsNotNone(order)
        self.assertEqual(order.order_status, 56)  # filled → 56

    def test_xt_trader_query_stock_order_not_found(self):
        order = self.trader.xt_trader.query_stock_order(self.trader.acc, 99999)
        self.assertIsNone(order)

    def test_xt_trader_cancel_order_stock(self):
        # 无终态回执 → 可撤单
        result = self.trader.xt_trader.cancel_order_stock(self.trader.acc, 12345)
        self.assertEqual(result, 0)
        # cancel 文件已写入
        cancel_path = os.path.join(self.ipc_root, "cancel", "cancel_12345.json")
        self.assertTrue(os.path.exists(cancel_path))

    def test_xt_trader_query_stock_orders_list(self):
        self._write_done(111, status="pending")
        self._write_done(222, status="filled")
        orders = self.trader.xt_trader.query_stock_orders(self.trader.acc, cancelable_only=False)
        self.assertEqual(len(orders), 2)

    def test_xt_trader_query_stock_orders_cancelable_only(self):
        self._write_done(111, status="pending")   # 50 → active
        self._write_done(222, status="filled")    # 56 → 非active
        orders = self.trader.xt_trader.query_stock_orders(self.trader.acc, cancelable_only=True)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].order_id, 111)


# ── 下单 ──

class TestOrder(_IpcTestBase):

    def test_order_id_is_integer(self):
        """order_id 必须是纯整数（position_manager 会 int() 转换）。"""
        oid = _next_order_id()
        self.assertIsInstance(oid, int)

    def test_buy_writes_pending_and_waits_filled(self):
        self._write_heartbeat()

        # 模拟 QMT 端：延迟后写 done 回执
        def qmt_side():
            time.sleep(0.3)
            # 找到 pending 里的 order_id
            pending_dir = os.path.join(self.ipc_root, "orders", "pending")
            for f in os.listdir(pending_dir):
                if f.startswith("ord_"):
                    oid = int(f.replace("ord_", "").replace(".json", ""))
                    self._write_done(oid, status="filled")
        threading.Thread(target=qmt_side, daemon=True).start()

        order_id = self.trader.buy("000001.SZ", amount=1000, price=10.5)
        self.assertIsNotNone(order_id)
        self.assertIsInstance(order_id, int)

    def test_buy_timeout_returns_order_id_and_writes_cancel(self):
        """QMT 端不回执 → 返回已知 order_id 并写追撤，阻止上层重复下单。"""
        self._write_heartbeat()
        order_id = self.trader.buy("000001.SZ", amount=1000, price=10.5)
        self.assertIsInstance(order_id, int)
        cancel_path = os.path.join(self.ipc_root, "cancel", f"cancel_{order_id}.json")
        self.assertTrue(os.path.exists(cancel_path))
        self.assertIn(order_id, self.trader._timeout_orders)

    def test_buy_rejected_returns_none(self):
        self._write_heartbeat()

        def qmt_side():
            time.sleep(0.2)
            pending_dir = os.path.join(self.ipc_root, "orders", "pending")
            for f in os.listdir(pending_dir):
                if f.startswith("ord_"):
                    oid = int(f.replace("ord_", "").replace(".json", ""))
                    self._write_done(oid, status="rejected")
        threading.Thread(target=qmt_side, daemon=True).start()

        order_id = self.trader.buy("000001.SZ", amount=1000, price=10.5)
        self.assertIsNone(order_id)

    def test_buy_invalid_volume_returns_none(self):
        order_id = self.trader.buy("000001.SZ", amount=0, price=10.5)
        self.assertIsNone(order_id)

    def test_pending_file_written_atomically(self):
        """验证 pending 文件是 ord_{id}.json，无残留 .tmp。"""
        self._write_heartbeat()

        captured = {}
        def qmt_side():
            time.sleep(0.2)
            pending_dir = os.path.join(self.ipc_root, "orders", "pending")
            files = os.listdir(pending_dir)
            captured['files'] = list(files)
            for f in files:
                if f.startswith("ord_"):
                    oid = int(f.replace("ord_", "").replace(".json", ""))
                    self._write_done(oid, status="filled")
        threading.Thread(target=qmt_side, daemon=True).start()

        self.trader.buy("000001.SZ", amount=1000, price=10.5)
        # 无 .tmp 残留
        self.assertTrue(all(not f.endswith(".tmp") for f in captured.get('files', [])))

    def test_order_stock_buy_direction(self):
        self._write_heartbeat()

        def qmt_side():
            time.sleep(0.2)
            pending_dir = os.path.join(self.ipc_root, "orders", "pending")
            for f in os.listdir(pending_dir):
                if f.startswith("ord_"):
                    fpath = os.path.join(pending_dir, f)
                    with open(fpath, encoding="utf-8") as fh:
                        order = json.load(fh)
                    # 验证 action=buy
                    self.assertEqual(order["action"], "buy")
                    oid = order["order_id"]
                    self._write_done(oid, status="filled")
        threading.Thread(target=qmt_side, daemon=True).start()

        self.trader.order_stock("000001.SZ", order_type=23, order_volume=1000, price=10.5)


# ── 撤单 ──

class TestCancel(_IpcTestBase):

    def test_cancel_writes_file(self):
        result = self.trader.cancel_order_stock(12345)
        self.assertEqual(result, 0)
        cancel_path = os.path.join(self.ipc_root, "cancel", "cancel_12345.json")
        self.assertTrue(os.path.exists(cancel_path))

    def test_cancel_already_filled_returns_minus_one(self):
        self._write_done(12345, status="filled")
        result = self.trader.cancel_order_stock(12345)
        self.assertEqual(result, -1)


# ── 持仓/资产 ──

class TestPositionAndBalance(_IpcTestBase):

    def _sample_snapshot(self):
        return {
            "timestamp": datetime_now(),
            "total_asset": 1000000.0,
            "available": 800000.0,
            "market_value": 200000.0,
            "frozen": 5000.0,
            "positions": [
                {"stock": "000001.SZ", "volume": 5000, "available": 5000,
                 "cost": 10.2, "market_price": 10.48, "market_value": 52400.0},
                {"stock": "600519.SH", "volume": 200, "available": 100,
                 "cost": 1500.0, "market_price": 1520.0, "market_value": 304000.0},
            ],
        }

    def test_position_returns_dataframe_with_required_columns(self):
        self._write_account(self._sample_snapshot())
        df = self.trader.position()
        self.assertFalse(df.empty)
        # _sync_real_positions_to_memory 依赖的 5 个必需列
        for col in ['证券代码', '股票余额', '可用余额', '成本价', '市值']:
            self.assertIn(col, df.columns)

    def test_position_stock_code_stripped(self):
        self._write_account(self._sample_snapshot())
        df = self.trader.position()
        codes = df['证券代码'].tolist()
        self.assertIn('000001', codes)  # 去掉 .SZ 后缀
        self.assertIn('600519', codes)

    def test_position_available_field(self):
        self._write_account(self._sample_snapshot())
        df = self.trader.position()
        row = df[df['证券代码'] == '600519'].iloc[0]
        self.assertEqual(row['可用余额'], 100)  # available < volume

    def test_position_no_snapshot_is_unavailable(self):
        os.remove(self.trader._dir("status", "account.json"))
        with self.assertRaises(RuntimeError):
            self.trader.position()

    def test_balance_returns_dataframe(self):
        self._write_account(self._sample_snapshot())
        df = self.trader.balance()
        self.assertFalse(df.empty)
        for col in ['可用金额', '冻结金额', '持仓市值', '总资产']:
            self.assertIn(col, df.columns)
        self.assertEqual(df['总资产'].iloc[0], 1000000.0)
        self.assertEqual(df['可用金额'].iloc[0], 800000.0)

    def test_balance_no_snapshot_is_unavailable(self):
        os.remove(self.trader._dir("status", "account.json"))
        with self.assertRaises(RuntimeError):
            self.trader.balance()

    def test_query_stock_asset_dict(self):
        self._write_account(self._sample_snapshot())
        asset = self.trader.query_stock_asset()
        self.assertEqual(asset['可用金额'], 800000.0)
        self.assertEqual(asset['总资产'], 1000000.0)


# ── 委托/成交查询 ──

class TestOrderTradeQuery(_IpcTestBase):

    def test_query_stock_orders_aggregates_done(self):
        self._write_done(111, status="filled")
        self._write_done(222, status="pending")
        df = self.trader.query_stock_orders()
        self.assertEqual(len(df), 2)

    def test_query_stock_trades_only_filled(self):
        self._write_done(111, status="filled")   # 56
        self._write_done(222, status="pending")  # 50 非成交
        self._write_done(333, status="partial")  # 55 部成
        df = self.trader.query_stock_trades()
        self.assertEqual(len(df), 2)  # filled + partial

    def test_get_active_orders_by_stock(self):
        self._write_done(111, status="pending", stock="000001.SZ")   # active
        self._write_done(222, status="filled", stock="000001.SZ")    # 非active
        active = self.trader.get_active_orders_by_stock("000001.SZ")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].order_id, 111)


# ── 成交回报轮询线程 ──

class TestDealPoller(_IpcTestBase):

    def test_trade_callback_fired_on_new_done(self):
        self._write_heartbeat()
        received = []
        self.trader.register_trade_callback(lambda trade: received.append(trade))
        self.trader.connect()  # 启动轮询线程

        # QMT 端写入成交回执
        self._write_done(555, status="filled", stock="000001.SZ")

        # 等待轮询线程触发
        deadline = time.time() + 3
        while time.time() < deadline and not received:
            time.sleep(0.1)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].order_id, 555)
        self.assertEqual(str(received[0].stock_code), "000001.SZ")

    def test_trade_callback_fired_once_per_order(self):
        self._write_heartbeat()
        received = []
        self.trader.register_trade_callback(lambda trade: received.append(trade))
        self.trader.connect()

        self._write_done(555, status="filled")

        time.sleep(0.5)  # 让轮询线程多扫几轮
        # 同一 order_id 只触发一次
        self.assertEqual(len(received), 1)

    def test_trade_callback_fires_incremental_volume(self):
        self._write_heartbeat()
        received = []
        self.trader.register_trade_callback(lambda trade: received.append(trade))
        self.trader.connect()

        self._write_done(556, status="partial", filled_volume=100, total_volume=300)
        deadline = time.time() + 3
        while time.time() < deadline and len(received) < 1:
            time.sleep(0.1)

        self._write_done(556, status="filled", filled_volume=300, total_volume=300)
        deadline = time.time() + 3
        while time.time() < deadline and len(received) < 2:
            time.sleep(0.1)

        self.assertEqual([t.traded_volume for t in received], [100, 200])
        self.assertNotEqual(received[0].traded_id, received[1].traded_id)

    def test_order_callback_fired(self):
        self._write_heartbeat()
        received = []
        self.trader.register_order_callback(lambda order: received.append(order))
        self.trader.connect()

        self._write_done(777, status="filled")

        deadline = time.time() + 3
        while time.time() < deadline and not received:
            time.sleep(0.1)
        self.assertGreaterEqual(len(received), 1)

    def test_disconnect_callback_on_heartbeat_loss(self):
        self._write_heartbeat()
        received = []
        self.trader.register_disconnect_callback(lambda: received.append(True))
        self.trader.connect()

        # 让轮询确认在线
        time.sleep(0.3)
        # 删除心跳文件 → 模拟断连
        os.remove(os.path.join(self.ipc_root, "status", "heartbeat.json"))

        deadline = time.time() + 3
        while time.time() < deadline and not received:
            time.sleep(0.1)
        self.assertTrue(received)


# ── 辅助方法 ──

class TestHelpers(_IpcTestBase):

    def test_adjust_stock(self):
        self.assertEqual(self.trader.adjust_stock("000001"), "000001.SZ")
        self.assertEqual(self.trader.adjust_stock("600519"), "600519.SH")
        self.assertEqual(self.trader.adjust_stock("000001.SZ"), "000001.SZ")
        self.assertEqual(self.trader.adjust_stock("920118"), "920118.BJ")
        self.assertEqual(self.trader.adjust_stock("bj.920118"), "920118.BJ")
        self.assertEqual(self.trader.adjust_stock("830799.BJ"), "830799.BJ")
        self.assertEqual(self.trader.adjust_stock("000920"), "000920.SZ")
        self.assertEqual(self.trader.adjust_stock("830799"), "830799")

    def test_select_data_type(self):
        self.assertEqual(self.trader.select_data_type("600519"), "stock")
        self.assertEqual(self.trader.select_data_type("510300"), "fund")
        self.assertEqual(self.trader.select_data_type("113001"), "bond")

    def test_check_stock_is_av_buy(self):
        self._write_account({"available": 100000.0, "positions": []})
        self.assertTrue(self.trader.check_stock_is_av_buy("000001", price=10, amount=1000))   # 10000 < 100000
        self.assertFalse(self.trader.check_stock_is_av_buy("000001", price=200, amount=1000))  # 200000 > 100000

    def test_check_stock_is_av_sell(self):
        self._write_account({"positions": [
            {"stock": "000001.SZ", "volume": 1000, "available": 1000}
        ]})
        self.assertTrue(self.trader.check_stock_is_av_sell("000001", amount=500))
        self.assertFalse(self.trader.check_stock_is_av_sell("000001", amount=2000))
        self.assertFalse(self.trader.check_stock_is_av_sell("999999", amount=100))


# ── 多账号隔离 ──

class TestMultiAccountIsolation(unittest.TestCase):
    """验证多账号使用独立子目录，互不干扰。"""

    def setUp(self):
        self.base_root = tempfile.mkdtemp(prefix="ipc_multi_")
        self._patchers = [
            patch.object(config, "QMT_IPC_ROOT", self.base_root, create=True),
            patch.object(config, "QMT_IPC_ORDER_TIMEOUT", 2, create=True),
            patch.object(config, "QMT_IPC_HEARTBEAT_MAX_AGE", 10, create=True),
            patch.object(config, "QMT_IPC_DEAL_POLL_INTERVAL", 0.1, create=True),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        shutil.rmtree(self.base_root, ignore_errors=True)

    def test_two_accounts_use_separate_dirs(self):
        t1 = QmtIpcTrader(account="TEST_ACC_1")
        t2 = QmtIpcTrader(account="TEST_ACC_2")
        self.assertNotEqual(t1.ipc_root, t2.ipc_root)
        self.assertTrue(t1.ipc_root.endswith("TEST_ACC_1"))
        self.assertTrue(t2.ipc_root.endswith("TEST_ACC_2"))
        # 共享同一 base_root
        self.assertEqual(t1.ipc_base_root, t2.ipc_base_root)

    def test_config_json_written_per_account(self):
        for acc, path in [("TEST_ACC_1", "C:/QMT1/userdata_mini"),
                          ("TEST_ACC_2", "C:/QMT2/userdata_mini")]:
            t = QmtIpcTrader(account=acc, path=path)
            os.makedirs(t._dir("status"), exist_ok=True)
            with open(t._dir("status", "heartbeat.json"), "w") as f:
                json.dump({"ts": time.time()}, f)
            t.connect()
            t.stop()
        cfg1_path = os.path.join(self.base_root, "TEST_ACC_1", "config.json")
        cfg2_path = os.path.join(self.base_root, "TEST_ACC_2", "config.json")
        self.assertTrue(os.path.exists(cfg1_path))
        self.assertTrue(os.path.exists(cfg2_path))
        cfg1 = json.load(open(cfg1_path, encoding="utf-8"))
        cfg2 = json.load(open(cfg2_path, encoding="utf-8"))
        self.assertEqual(cfg1["account_id"], "TEST_ACC_1")
        self.assertEqual(cfg2["account_id"], "TEST_ACC_2")
        self.assertEqual(cfg1["qmt_path"], "C:/QMT1/userdata_mini")
        self.assertEqual(cfg2["qmt_path"], "C:/QMT2/userdata_mini")

    def test_order_written_to_own_account_dir(self):
        t1 = QmtIpcTrader(account="TEST_ACC_1")
        t2 = QmtIpcTrader(account="TEST_ACC_2")
        t1._ensure_dirs()
        t2._ensure_dirs()
        with open(t1._dir("status", "account.json"), "w", encoding="utf-8") as handle:
            json.dump({"account_id": "TEST_ACC_1", "timestamp": datetime_now(),
                       "available": 100000.0, "positions": []}, handle)
        # 心跳（下单前快速失败门禁要求 QMT 在线）
        with open(t1._dir("status", "heartbeat.json"), "w") as f:
            json.dump({"ts": time.time()}, f)

        # 后台模拟 QMT 端只在 t1 目录消费并回执
        def qmt_side():
            time.sleep(0.2)
            pending = t1._dir("orders", "pending")
            for f in os.listdir(pending):
                if f.startswith("ord_"):
                    oid = int(f.replace("ord_", "").replace(".json", ""))
                    with open(t1._dir("orders", "done", f"ord_{oid}.json"), "w", encoding="utf-8") as fh:
                        json.dump({"order_id": oid, "status": "filled", "action": "buy",
                                   "stock_code": "000001.SZ", "filled_price": 10.5,
                                   "filled_volume": 1000, "total_volume": 1000}, fh)
        threading.Thread(target=qmt_side, daemon=True).start()

        oid = t1.buy("000001.SZ", amount=1000, price=10.5)
        self.assertIsNotNone(oid)
        # t2 目录完全没有订单/回执
        self.assertEqual(os.listdir(t2._dir("orders", "done")), [])
        self.assertEqual([f for f in os.listdir(t2._dir("orders", "pending")) if f.endswith(".json")], [])

    def test_account_snapshot_isolation(self):
        t1 = QmtIpcTrader(account="TEST_ACC_1")
        t2 = QmtIpcTrader(account="TEST_ACC_2")
        t1._ensure_dirs()
        t2._ensure_dirs()
        # 只给 t1 写账户快照
        with open(t1._dir("status", "account.json"), "w", encoding="utf-8") as f:
            json.dump({"account_id": "TEST_ACC_1", "timestamp": datetime_now(), "total_asset": 1000000, "available": 800000, "positions": [
                {"stock": "000001.SZ", "volume": 5000, "available": 5000,
                 "cost": 10.2, "market_price": 10.5, "market_value": 52500}
            ]}, f)
        # t1 有有效快照，t2 未取得快照不能冒充空仓。
        self.assertFalse(t1.position().empty)
        with self.assertRaises(RuntimeError):
            t2.position()


# ── 接口契约对齐 easy_qmt_trader ──

class TestInterfaceContract(unittest.TestCase):

    def test_has_all_easy_qmt_trader_methods(self):
        """QmtIpcTrader 必须实现 easy_qmt_trader 被外部调用的全部方法。"""
        required_methods = [
            'connect', 'ping_xttrader', 'reconnect_xttrader',
            'position', 'query_stock_positions', 'balance', 'query_stock_asset',
            'buy', 'sell', 'order_stock', 'order_stock_async',
            'cancel_order_stock', 'cancel_order_stock_async',
            'query_stock_orders', 'today_entrusts', 'query_stock_trades', 'today_trades',
            'get_active_orders_by_stock', 'get_active_order_info_by_stock',
            'adjust_stock', 'select_data_type', 'select_slippage',
            'check_stock_is_av_buy', 'check_stock_is_av_sell',
            'register_trade_callback', 'register_order_callback', 'register_disconnect_callback',
        ]
        for m in required_methods:
            self.assertTrue(hasattr(QmtIpcTrader, m), f"QmtIpcTrader 缺少方法: {m}")

    def test_has_compat_attributes(self):
        """必须提供 xt_trader / acc / order_id_map 属性。"""
        t = QmtIpcTrader(account="test")
        self.assertTrue(hasattr(t, 'xt_trader'))
        self.assertTrue(hasattr(t, 'acc'))
        self.assertTrue(hasattr(t, 'order_id_map'))

    def test_status_mapping_complete(self):
        """IPC status → QMT 状态码映射覆盖所有终态。"""
        for status in ['filled', 'partial', 'partial_cancelled', 'rejected', 'cancelled', 'pending']:
            self.assertIn(status, _IPC_STATUS_TO_QMT)


# ── order_id 自映射（兼容 position_manager 异步模式）──

class TestOrderIdMapping(_IpcTestBase):
    """验证下单后 order_id 自映射写入 order_id_map。

    position_manager._get_real_order_id() 在 USE_SYNC_ORDER_API=False(config 默认)时
    从 order_id_map 取真实 order_id；若不自映射，IPC 返回的真实 order_id 查不到 → 下单被判失败。
    """

    def _order_and_capture(self, status="filled"):
        self._write_heartbeat()

        def qmt_side():
            time.sleep(0.2)
            pending_dir = os.path.join(self.ipc_root, "orders", "pending")
            for f in os.listdir(pending_dir):
                if f.startswith("ord_"):
                    oid = int(f.replace("ord_", "").replace(".json", ""))
                    self._write_done(oid, status=status)
        threading.Thread(target=qmt_side, daemon=True).start()
        return self.trader.buy("000001.SZ", amount=1000, price=10.5)

    def test_buy_registers_self_mapping(self):
        oid = self._order_and_capture()
        self.assertIsNotNone(oid)
        # 自映射：order_id_map[oid] == oid
        self.assertIn(oid, self.trader.order_id_map)
        self.assertEqual(self.trader.order_id_map[oid], oid)

    def test_rejected_order_still_registered(self):
        """即便被拒(返回 None)，order_id 也已在提交时登记（幂等，无害）。"""
        before = len(self.trader.order_id_map)
        self._order_and_capture(status="rejected")
        self.assertGreater(len(self.trader.order_id_map), before)

    def test_order_id_map_capacity_bounded(self):
        """order_id_map 超过上限时自动裁剪，防止长时间运行内存膨胀。"""
        for i in range(1, 4101):
            self.trader._register_order_id(i)
        self.assertLessEqual(len(self.trader.order_id_map), 4096)


# ── 心跳快速失败门禁 ──

class TestHeartbeatFastFail(_IpcTestBase):
    """大QMT离线时下单立即返回 None，不阻塞 order_timeout 秒。"""

    def test_buy_fast_fail_when_no_heartbeat(self):
        # order_timeout 被 patch 为 2 秒；快速失败应远小于该值
        start = time.time()
        oid = self.trader.buy("000001.SZ", amount=1000, price=10.5)
        elapsed = time.time() - start
        self.assertIsNone(oid)
        self.assertLess(elapsed, 1.0, "无心跳时下单应快速失败，不应阻塞等待回执")

    def test_sell_fast_fail_when_no_heartbeat(self):
        start = time.time()
        oid = self.trader.sell("000001.SZ", amount=1000, price=10.5)
        elapsed = time.time() - start
        self.assertIsNone(oid)
        self.assertLess(elapsed, 1.0)

    def test_fast_fail_when_heartbeat_stale(self):
        self._write_heartbeat()
        path = os.path.join(self.ipc_root, "status", "heartbeat.json")
        old = time.time() - 100  # 超过 heartbeat_max_age
        os.utime(path, (old, old))
        start = time.time()
        oid = self.trader.buy("000001.SZ", amount=1000, price=10.5)
        self.assertIsNone(oid)
        self.assertLess(time.time() - start, 1.0)

    def test_no_pending_file_written_on_fast_fail(self):
        """快速失败时不应残留 pending 下单文件。"""
        self.trader.buy("000001.SZ", amount=1000, price=10.5)
        pending_dir = os.path.join(self.ipc_root, "orders", "pending")
        remaining = [f for f in os.listdir(pending_dir) if f.endswith(".json")]
        self.assertEqual(remaining, [])


# ── IPC 健康诊断 ──

class TestIpcHealth(_IpcTestBase):

    def test_health_keys(self):
        h = self.trader.get_ipc_health()
        for k in ['account', 'ipc_root', 'connected', 'qmt_alive',
                  'heartbeat_age', 'heartbeat_max_age', 'pending_count',
                  'processing_count', 'done_count', 'timeout_unknown_count', 'poller_alive']:
            self.assertIn(k, h)

    def test_health_reflects_alive_and_counts(self):
        self._write_heartbeat()
        self._write_done(111, status="filled")
        self._write_done(222, status="pending")
        h = self.trader.get_ipc_health()
        self.assertTrue(h['qmt_alive'])
        self.assertIsNotNone(h['heartbeat_age'])
        self.assertEqual(h['done_count'], 2)
        self.assertEqual(h['account'], "TEST_ACC_1")

    def test_health_when_offline(self):
        h = self.trader.get_ipc_health()
        self.assertFalse(h['qmt_alive'])
        self.assertIsNone(h['heartbeat_age'])


if __name__ == '__main__':
    unittest.main()
