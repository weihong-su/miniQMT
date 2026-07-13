"""
QmtRpcTrader 大QMT RPC 交易客户端测试（基于 xtquant_big_convert）

覆盖:
- 契约方法齐全 + .xt_trader / .acc / .order_id_map 属性兼容
- 下单：allow_order 门禁 / RPC 离线快速失败 / 返回纯整数 order_id
- order_id 映射：passorder 无同步 sysid → 靠返回串 reconcile 配对（核心）
- 撤单：int_id → order_sys_id 解析后调用 RPC
- 持仓/资产：DataFrame 列契约（必需 5 列）
- 委托/成交查询
- 回调：reconcile 后触发 order_callback / trade_callback（去重）
- ping / 断连回调

不依赖真实 Redis：注入 FakeRpcClient 到 QmtRpcTrader._bq.client。
"""
import unittest
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "qmt-trader"))

import config
from qmt_rpc_trader import QmtRpcTrader
import _qmt_trader_base as base


class FakeRpcClient:
    """模拟 vendored BigQmtRpcClient.call：复现 passorder 无同步 sysid 的真实行为。

    order_stock → 返回 user_order_id 字符串（order_sys_id=None，passorder 风格）。
    query_stock_orders → 返回已提交订单（remark == user_order_id），供 reconcile 配对。
    """

    def __init__(self, account_id="TESTACC", alive=True):
        self.account_id = account_id
        self.alive = alive
        self.calls = []
        self._seq = 0
        self._orders = {}   # user_order_id -> order dict
        self._asset = {"account_id": account_id, "cash": 50000.0, "total_asset": 150000.0}
        self._positions = {
            "600000.SH": {"stock_code": "600000.SH", "volume": 1000, "available": 800,
                          "cost": 10.2, "stock_name": "浦发银行"},
        }

    def _redis(self):
        raise RuntimeError("redis not available in test")

    def set_order_filled(self, user_order_id, traded_volume, price):
        o = self._orders.get(user_order_id)
        if o:
            o["status"] = "56"
            o["traded_volume"] = traded_volume
            o["price"] = price

    def call(self, method, params=None, account_id=None, timeout_seconds=None):
        params = params or {}
        self.calls.append((method, params))
        if method == "ping":
            if not self.alive:
                raise RuntimeError("rpc down")
            return {"pong": True}
        if method == "order_stock":
            self._seq += 1
            uid = "bq:%d" % self._seq
            self._orders[uid] = {
                "order_sys_id": "sys%d" % self._seq,
                "user_order_id": uid,
                "remark": uid,
                "stock_code": params.get("stock_code"),
                "action": "BUY" if params.get("order_type") == base.STOCK_BUY else "SELL",
                "volume": params.get("order_volume"),
                "traded_volume": 0,
                "status": "50",
                "price": params.get("price"),
            }
            # passorder 风格：同步无 order_sys_id，返回 user_order_id 字符串
            return {"status": "SUBMITTED", "user_order_id": uid, "order_sys_id": None}
        if method == "query_stock_orders":
            return list(self._orders.values())
        if method == "query_stock_trades":
            return [o for o in self._orders.values() if o["status"] in ("55", "56")]
        if method == "query_stock_positions":
            return dict(self._positions)
        if method == "query_stock_asset":
            return dict(self._asset)
        if method == "cancel_order_stock_sysid":
            return {"success": True}
        raise AssertionError("unexpected method: %s" % method)


class QmtRpcTraderTest(unittest.TestCase):
    def _make(self, alive=True, allow_order=True):
        t = QmtRpcTrader(account="TESTACC", account_type="STOCK")
        t._bq.client = FakeRpcClient(account_id="TESTACC", alive=alive)
        t._allow_order = allow_order
        return t

    # ---- 契约 / 属性 ----
    def test_contract_attributes_present(self):
        t = self._make()
        self.assertTrue(hasattr(t, "xt_trader"))
        self.assertTrue(hasattr(t, "acc"))
        self.assertIsInstance(t.order_id_map, dict)
        # xt_trader 三个直接访问方法
        for m in ("query_stock_orders", "query_stock_order", "cancel_order_stock"):
            self.assertTrue(hasattr(t.xt_trader, m))

    def test_ping_reflects_alive(self):
        self.assertTrue(self._make(alive=True).ping_xttrader())
        self.assertFalse(self._make(alive=False).ping_xttrader())

    # ---- 下单门禁 ----
    def test_order_blocked_when_allow_order_false(self):
        t = self._make(allow_order=False)
        self.assertIsNone(t.buy("600000.SH", amount=100, price=10.0))

    def test_order_blocked_when_rpc_offline(self):
        t = self._make(alive=False, allow_order=True)
        self.assertIsNone(t.buy("600000.SH", amount=100, price=10.0))

    # ---- 下单返回纯整数 order_id + 映射 ----
    def test_buy_returns_int_order_id_and_maps(self):
        t = self._make()
        oid = t.buy("600000", order_type=base.STOCK_BUY, amount=100, price=10.0)
        self.assertIsInstance(oid, int)
        self.assertIn(oid, t.order_id_map)
        self.assertIn(oid, t._id_map)
        # 返回串索引已建立
        self.assertEqual(len(t._return_index), 1)
        # 下单参数带交易所后缀
        order_calls = [c for c in t._bq.client.calls if c[0] == "order_stock"]
        self.assertEqual(order_calls[0][1]["stock_code"], "600000.SH")

    # ---- 核心：reconcile 配对（passorder 无同步 sysid）----
    def test_reconcile_maps_returned_uid_to_int_id_and_learns_sysid(self):
        t = self._make()
        oid = t.buy("600000.SH", amount=100, price=10.0)
        orders = t._read_all_orders()
        self.assertEqual(len(orders), 1)
        # order_id 被重映射为纯整数
        self.assertEqual(orders[0].order_id, oid)
        # sysid 已回填
        self.assertEqual(t._id_map[oid]["order_sys_id"], "sys1")

    # ---- 回调：order + trade 去重触发 ----
    def test_callbacks_fire_on_fill(self):
        t = self._make()
        fired_orders, fired_trades = [], []
        t.register_order_callback(lambda o: fired_orders.append(o))
        t.register_trade_callback(lambda tr: fired_trades.append(tr))
        oid = t.buy("600000.SH", amount=100, price=10.0)
        # 首轮：已报(50) → 仅 order_callback
        for o in t._read_all_orders():
            t._maybe_fire_from_order(o)
        self.assertEqual(len(fired_orders), 1)
        self.assertEqual(len(fired_trades), 0)
        # 成交 → 再轮：trade_callback 触发一次
        t._bq.client.set_order_filled("bq:1", 100, 10.0)
        for o in t._read_all_orders():
            t._maybe_fire_from_order(o)
        self.assertEqual(len(fired_trades), 1)
        self.assertEqual(fired_trades[0].order_id, oid)
        self.assertEqual(fired_trades[0].traded_volume, 100)
        # 幂等：重复轮询不重复触发成交
        for o in t._read_all_orders():
            t._maybe_fire_from_order(o)
        self.assertEqual(len(fired_trades), 1)

    # ---- 撤单 ----
    def test_cancel_resolves_sysid(self):
        t = self._make()
        oid = t.buy("600000.SH", amount=100, price=10.0)
        # reconcile 学到 sysid
        for o in t._read_all_orders():
            t._reconcile(o)
        self.assertEqual(t.cancel_order_stock(oid), 0)
        self.assertTrue(any(c[0] == "cancel_order_stock_sysid" for c in t._bq.client.calls))

    def test_cancel_unknown_returns_minus_one(self):
        t = self._make()
        self.assertEqual(t.cancel_order_stock(999999999), -1)

    # ---- 持仓 DataFrame 列契约 ----
    def test_position_dataframe_required_columns(self):
        t = self._make()
        df = t.position()
        for col in ("证券代码", "股票余额", "可用余额", "成本价", "市值"):
            self.assertIn(col, df.columns)
        row = df.iloc[0]
        self.assertEqual(row["证券代码"], "600000")
        self.assertEqual(row["股票余额"], 1000)
        self.assertEqual(row["可用余额"], 800)
        self.assertAlmostEqual(row["成本价"], 10.2)
        self.assertAlmostEqual(row["市值"], 10.2 * 1000)

    # ---- 资产 ----
    def test_balance_and_asset(self):
        t = self._make()
        df = t.balance()
        self.assertEqual(df.iloc[0]["可用金额"], 50000.0)
        self.assertEqual(df.iloc[0]["总资产"], 150000.0)
        asset = t.query_stock_asset()
        self.assertEqual(asset["可用金额"], 50000.0)
        self.assertEqual(asset["总资产"], 150000.0)

    # ---- 委托/成交查询 ----
    def test_query_orders_and_trades(self):
        t = self._make()
        t.buy("600000.SH", amount=100, price=10.0)
        odf = t.query_stock_orders()
        self.assertEqual(len(odf), 1)
        # 未成交 → trades 为空
        self.assertEqual(len(t.query_stock_trades()), 0)
        t._bq.client.set_order_filled("bq:1", 100, 10.0)
        tdf = t.query_stock_trades()
        self.assertEqual(len(tdf), 1)

    def test_active_orders_by_stock(self):
        t = self._make()
        t.buy("600000.SH", amount=100, price=10.0)
        active = t.get_active_orders_by_stock("600000.SH")
        self.assertEqual(len(active), 1)
        info = t.get_active_order_info_by_stock("600000.SH")
        self.assertEqual(info[0]["stock_code"][:6], "600000")


if __name__ == "__main__":
    unittest.main()
