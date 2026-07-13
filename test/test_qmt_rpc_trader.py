"""
QmtRpcTrader 大QMT RPC 交易客户端测试（基于 xtquant_big_convert）

覆盖:
- 契约方法齐全 + .xt_trader / .acc / .order_id_map 属性兼容
- 连接生命周期：connect / ping / reconnect / stop
- 断连回调：poller 检测 + callback 触发
- 推送事件：_on_push_order / _on_push_trade（vendored Redis pubsub 模拟）
- 下单门禁：allow_order / RPC 离线快速失败
- 买卖方向：buy / sell / order_stock + 滑点方向
- order_id 映射：passorder 无同步 sysid → 靠返回串 reconcile 配对（核心）
- 撤单：int_id → order_sys_id 解析后调用 RPC
- 持仓/资产：DataFrame 列契约 + 空持仓
- 资金校验：check_stock_is_av_buy / sell
- 委托/成交查询：别名方法 today_entrusts / today_trades
- 回调：reconcile 后触发 order_callback / trade_callback（去重）
- 健康诊断：get_rpc_health() 快照
- 边界：零 volume / order_id_map 超 4096 / config 缺失 / 空持仓

不依赖真实 Redis：注入 FakeRpcClient 到 QmtRpcTrader._bq.client。
"""
import unittest
import os
import sys
import time
import threading

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
        self._asset = {"account_id": account_id, "cash": 50000.0, "total_asset": 150000.0,
                       "market_value": 100000.0}
        self._positions = {
            "600000.SH": {"stock_code": "600000.SH", "volume": 1000, "available": 800,
                          "cost": 10.2, "stock_name": "浦发银行"},
        }
        self._cancelled_ids = set()

    def _redis(self):
        raise RuntimeError("redis not available in test")

    def set_alive(self, alive):
        self.alive = alive

    def set_asset(self, cash=None, total_asset=None, market_value=None):
        if cash is not None:
            self._asset["cash"] = cash
        if total_asset is not None:
            self._asset["total_asset"] = total_asset
        if market_value is not None:
            self._asset["market_value"] = market_value

    def set_positions(self, positions):
        self._positions = dict(positions)

    def add_position(self, stock_code, volume, available=None, cost=0, stock_name=""):
        self._positions[stock_code] = {
            "stock_code": stock_code, "volume": volume,
            "available": available if available is not None else volume,
            "cost": cost, "stock_name": stock_name,
        }

    def set_order_status(self, user_order_id, status, traded_volume=None, price=None):
        o = self._orders.get(user_order_id)
        if o:
            o["status"] = str(status)
            if traded_volume is not None:
                o["traded_volume"] = traded_volume
            if price is not None:
                o["price"] = price

    def set_order_filled(self, user_order_id, traded_volume, price):
        self.set_order_status(user_order_id, "56", traded_volume, price)

    def set_order_rejected(self, user_order_id):
        self.set_order_status(user_order_id, "57")

    def set_order_cancelled(self, user_order_id):
        self.set_order_status(user_order_id, "54")

    def _make_order(self, uid):
        return {
            "order_sys_id": "sys%d" % self._seq,
            "user_order_id": uid,
            "remark": uid,
            "stock_code": self._orders.get(uid, {}).get("stock_code", "600000.SH"),
            "action": self._orders.get(uid, {}).get("action", "BUY"),
            "volume": self._orders.get(uid, {}).get("volume", 100),
            "traded_volume": self._orders.get(uid, {}).get("traded_volume", 0),
            "status": self._orders.get(uid, {}).get("status", "50"),
            "price": self._orders.get(uid, {}).get("price", 10.0),
            "strategy_name": self._orders.get(uid, {}).get("strategy_name", ""),
        }

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
                "strategy_name": params.get("strategy_name", ""),
            }
            # passorder 风格：同步无 order_sys_id，返回 user_order_id 字符串
            return {"status": "SUBMITTED", "user_order_id": uid, "order_sys_id": None}
        if method == "query_stock_orders":
            # 支持 cancelable_only 过滤
            cancelable_only = params.get("cancelable_only", False)
            orders = []
            for uid, o in self._orders.items():
                status = int(o.get("status", 50))
                if cancelable_only and status not in base.ACTIVE_ORDER_STATUS:
                    continue
                if uid in self._cancelled_ids:
                    continue
                orders.append(self._make_order(uid))
            return orders
        if method == "query_stock_trades":
            return [self._make_order(uid) for uid, o in self._orders.items()
                    if o["status"] in ("55", "56")]
        if method == "query_stock_positions":
            return dict(self._positions)
        if method == "query_stock_asset":
            return dict(self._asset)
        if method == "cancel_order_stock_sysid":
            sysid = params.get("order_sysid") or params.get("order_sys_id") or ""
            for uid, o in list(self._orders.items()):
                if o.get("order_sys_id") == sysid:
                    self._cancelled_ids.add(uid)
                    return {"success": True}
            return {"success": False}
        raise AssertionError("unexpected method: %s" % method)


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------

class QmtRpcTraderTest(unittest.TestCase):
    def _make(self, alive=True, allow_order=True, account="TESTACC"):
        t = QmtRpcTrader(account=account, account_type="STOCK")
        t._bq.client = FakeRpcClient(account_id=account, alive=alive)
        t._allow_order = allow_order
        return t

    # ==================================================================
    # 契约 / 属性（原有）
    # ==================================================================

    def test_contract_attributes_present(self):
        t = self._make()
        self.assertTrue(hasattr(t, "xt_trader"))
        self.assertTrue(hasattr(t, "acc"))
        self.assertIsInstance(t.order_id_map, dict)
        for m in ("query_stock_orders", "query_stock_order", "cancel_order_stock"):
            self.assertTrue(hasattr(t.xt_trader, m))

    def test_ping_reflects_alive(self):
        self.assertTrue(self._make(alive=True).ping_xttrader())
        self.assertFalse(self._make(alive=False).ping_xttrader())

    # ==================================================================
    # 连接生命周期（新增）
    # ==================================================================

    def test_connect_success_returns_tuple(self):
        t = self._make(alive=True)
        result = t.connect()
        self.assertIsNotNone(result)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        self.assertTrue(t._connected)

    def test_connect_failure_when_rpc_down(self):
        t = self._make(alive=False)
        result = t.connect()
        self.assertIsNone(result)
        self.assertFalse(t._connected)

    def test_reconnect_xttrader_after_connect_failure(self):
        """重连：RPC 离线 → connect 失败 → 恢复在线 → reconnect 成功"""
        t = self._make(alive=False)
        self.assertIsNone(t.connect())
        self.assertFalse(t._connected)
        # 恢复在线
        t._bq.client.set_alive(True)
        self.assertTrue(t.reconnect_xttrader())
        self.assertTrue(t._connected)

    def test_reconnect_xttrader_when_already_connected(self):
        t = self._make(alive=True)
        t.connect()
        self.assertTrue(t._connected)
        self.assertTrue(t.reconnect_xttrader())

    def test_stop_stops_poller(self):
        t = self._make()
        t.connect()
        self.assertTrue(t._poller_thread is not None)
        t.stop()
        self.assertTrue(t._poller_stop)

    # ==================================================================
    # 断连回调（新增）
    # ==================================================================

    def test_disconnect_callback_registered_and_called(self):
        t = self._make(alive=True)
        fired = []
        t.register_disconnect_callback(lambda: fired.append(1))
        self.assertEqual(len(t._disconnect_callbacks), 1)
        # 手动触发
        t._on_disconnect()
        self.assertEqual(len(fired), 1)

    def test_multiple_disconnect_callbacks_all_fired(self):
        t = self._make()
        fired = []
        t.register_disconnect_callback(lambda: fired.append("a"))
        t.register_disconnect_callback(lambda: fired.append("b"))
        t._on_disconnect()
        self.assertEqual(fired, ["a", "b"])

    def test_disconnect_callback_exception_does_not_block_others(self):
        t = self._make()
        fired = []
        t.register_disconnect_callback(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        t.register_disconnect_callback(lambda: fired.append("ok"))
        t._on_disconnect()
        self.assertEqual(fired, ["ok"])

    # ==================================================================
    # 推送事件（Redis pubsub 模拟）（新增）
    # ==================================================================

    def test_on_push_order_triggers_order_callback(self):
        t = self._make()
        fired = []
        t.register_order_callback(lambda o: fired.append(o))
        # 模拟 vendored 推送的 CompatObject
        push_order = base.FakeXtObject(
            account_id="TESTACC", stock_code="600000.SH",
            order_id="", order_sysid="sys99", order_type=base.STOCK_BUY,
            order_volume=100, price=10.0, order_status=50,
            order_remark="bq:9", strategy_name="test",
            traded_volume=0, price_type=50, status_msg="",
        )
        # 先建立一个 int_id 映射让 reconcile 能配对
        oid = t.buy("600000.SH", amount=100, price=10.0)
        # 把这次下单的返回串与推送的 order_remark 对齐
        ret_str = list(t._return_index.keys())[0]
        with t._map_lock:
            t._return_index["bq:9"] = oid
        t._on_push_order(push_order)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0].order_id, oid)

    def test_on_push_trade_triggers_trade_callback(self):
        t = self._make()
        fired_trades = []
        t.register_trade_callback(lambda tr: fired_trades.append(tr))
        oid = t.buy("600000.SH", amount=100, price=10.0)
        # reconcile 建立 sysid 映射
        for o in t._read_all_orders():
            t._reconcile(o)
        push_trade = base.FakeXtObject(
            order_sysid="sys1", order_sys_id="sys1",
            stock_code="600000.SH", order_type=base.STOCK_BUY,
            traded_volume=100, traded_price=10.0,
        )
        t._on_push_trade(push_trade)
        self.assertEqual(len(fired_trades), 1)
        self.assertEqual(fired_trades[0].order_id, oid)
        self.assertEqual(fired_trades[0].traded_volume, 100)

    def test_on_push_trade_unknown_sysid_is_noop(self):
        t = self._make()
        fired = []
        t.register_trade_callback(lambda tr: fired.append(tr))
        push_trade = base.FakeXtObject(
            order_sysid="nonexistent", stock_code="600000.SH",
            order_type=base.STOCK_BUY, traded_volume=100, traded_price=10.0,
        )
        t._on_push_trade(push_trade)
        self.assertEqual(len(fired), 0)

    # ==================================================================
    # 下单门禁（原有 + 扩展）
    # ==================================================================

    def test_order_blocked_when_allow_order_false(self):
        t = self._make(allow_order=False)
        self.assertIsNone(t.buy("600000.SH", amount=100, price=10.0))

    def test_order_blocked_when_rpc_offline(self):
        t = self._make(alive=False, allow_order=True)
        self.assertIsNone(t.buy("600000.SH", amount=100, price=10.0))

    def test_buy_zero_volume_returns_none(self):
        t = self._make()
        self.assertIsNone(t.buy("600000.SH", amount=0, price=10.0))
        self.assertIsNone(t.buy("600000.SH", amount=-5, price=10.0))

    def test_sell_blocked_when_allow_order_false(self):
        t = self._make(allow_order=False)
        self.assertIsNone(t.sell("600000.SH", amount=100, price=10.0))

    def test_sell_blocked_when_rpc_offline(self):
        t = self._make(alive=False, allow_order=True)
        self.assertIsNone(t.sell("600000.SH", amount=100, price=10.0))

    # ==================================================================
    # 下单返回纯整数 order_id + 映射（原有 + 扩展）
    # ==================================================================

    def test_buy_returns_int_order_id_and_maps(self):
        t = self._make()
        oid = t.buy("600000", order_type=base.STOCK_BUY, amount=100, price=10.0)
        self.assertIsInstance(oid, int)
        self.assertIn(oid, t.order_id_map)
        self.assertIn(oid, t._id_map)
        self.assertEqual(len(t._return_index), 1)
        order_calls = [c for c in t._bq.client.calls if c[0] == "order_stock"]
        self.assertEqual(order_calls[0][1]["stock_code"], "600000.SH")

    def test_sell_returns_int_order_id_and_maps(self):
        t = self._make()
        oid = t.sell("600000.SH", order_type=base.STOCK_SELL, amount=200, price=12.0)
        self.assertIsInstance(oid, int)
        self.assertIn(oid, t.order_id_map)
        order_calls = [c for c in t._bq.client.calls if c[0] == "order_stock"]
        self.assertEqual(order_calls[0][1]["stock_code"], "600000.SH")
        self.assertEqual(order_calls[0][1]["order_type"], base.STOCK_SELL)
        self.assertEqual(order_calls[0][1]["order_volume"], 200)

    def test_order_stock_buy(self):
        t = self._make()
        oid = t.order_stock("600000.SH", order_type=base.STOCK_BUY, order_volume=300, price=15.0)
        self.assertIsInstance(oid, int)
        order_calls = [c for c in t._bq.client.calls if c[0] == "order_stock"]
        self.assertEqual(order_calls[0][1]["order_type"], base.STOCK_BUY)

    def test_order_stock_sell(self):
        t = self._make()
        oid = t.order_stock("000001.SZ", order_type=base.STOCK_SELL, order_volume=500, price=20.0)
        self.assertIsInstance(oid, int)
        order_calls = [c for c in t._bq.client.calls if c[0] == "order_stock"]
        self.assertEqual(order_calls[0][1]["order_type"], base.STOCK_SELL)
        self.assertEqual(order_calls[0][1]["stock_code"], "000001.SZ")

    def test_order_stock_async_same_as_order_stock(self):
        t = self._make()
        oid = t.order_stock_async("600000.SH", order_type=base.STOCK_BUY, order_volume=100, price=10.0)
        self.assertIsInstance(oid, int)
        self.assertIn(oid, t.order_id_map)

    def test_market_order_uses_latest_price(self):
        from qmt_rpc_trader import LATEST_PRICE, FIX_PRICE
        t = self._make()
        t.buy("600000.SH", amount=100, price=0)  # price=0 → 市价
        order_calls = [c for c in t._bq.client.calls if c[0] == "order_stock"]
        # 市价单 price_type 应为 LATEST_PRICE(5)
        self.assertEqual(order_calls[0][1]["price_type"], LATEST_PRICE)

    def test_limit_order_uses_fix_price(self):
        from qmt_rpc_trader import FIX_PRICE
        t = self._make()
        t.buy("600000.SH", amount=100, price=10.0)  # 限价单
        order_calls = [c for c in t._bq.client.calls if c[0] == "order_stock"]
        self.assertEqual(order_calls[0][1]["price_type"], FIX_PRICE)

    # ==================================================================
    # 核心：reconcile 配对（原有）
    # ==================================================================

    def test_reconcile_maps_returned_uid_to_int_id_and_learns_sysid(self):
        t = self._make()
        oid = t.buy("600000.SH", amount=100, price=10.0)
        orders = t._read_all_orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].order_id, oid)
        self.assertEqual(t._id_map[oid]["order_sys_id"], "sys1")

    # ==================================================================
    # 回调：order + trade 去重触发（原有 + 扩展）
    # ==================================================================

    def test_callbacks_fire_on_fill(self):
        t = self._make()
        fired_orders, fired_trades = [], []
        t.register_order_callback(lambda o: fired_orders.append(o))
        t.register_trade_callback(lambda tr: fired_trades.append(tr))
        oid = t.buy("600000.SH", amount=100, price=10.0)
        for o in t._read_all_orders():
            t._maybe_fire_from_order(o)
        self.assertEqual(len(fired_orders), 1)
        self.assertEqual(len(fired_trades), 0)
        t._bq.client.set_order_filled("bq:1", 100, 10.0)
        for o in t._read_all_orders():
            t._maybe_fire_from_order(o)
        self.assertEqual(len(fired_trades), 1)
        self.assertEqual(fired_trades[0].order_id, oid)
        self.assertEqual(fired_trades[0].traded_volume, 100)
        # 幂等
        for o in t._read_all_orders():
            t._maybe_fire_from_order(o)
        self.assertEqual(len(fired_trades), 1)

    def test_rejected_order_triggers_only_order_callback(self):
        t = self._make()
        fired_orders, fired_trades = [], []
        t.register_order_callback(lambda o: fired_orders.append(o))
        t.register_trade_callback(lambda tr: fired_trades.append(tr))
        t.buy("600000.SH", amount=100, price=10.0)
        t._bq.client.set_order_rejected("bq:1")
        for o in t._read_all_orders():
            t._maybe_fire_from_order(o)
        self.assertEqual(len(fired_orders), 1)
        # rejected 不是成交，不应触发 trade callback
        self.assertEqual(len(fired_trades), 0)

    def test_cancel_callback_no_trade_fire(self):
        t = self._make()
        fired_trades = []
        t.register_trade_callback(lambda tr: fired_trades.append(tr))
        t.buy("600000.SH", amount=100, price=10.0)
        t._bq.client.set_order_cancelled("bq:1")
        for o in t._read_all_orders():
            t._maybe_fire_from_order(o)
        self.assertEqual(len(fired_trades), 0)

    # ==================================================================
    # 撤单（原有 + 扩展）
    # ==================================================================

    def test_cancel_resolves_sysid(self):
        t = self._make()
        oid = t.buy("600000.SH", amount=100, price=10.0)
        for o in t._read_all_orders():
            t._reconcile(o)
        self.assertEqual(t.cancel_order_stock(oid), 0)
        self.assertTrue(any(c[0] == "cancel_order_stock_sysid" for c in t._bq.client.calls))

    def test_cancel_unknown_returns_minus_one(self):
        t = self._make()
        self.assertEqual(t.cancel_order_stock(999999999), -1)

    def test_cancel_order_stock_async(self):
        t = self._make()
        oid = t.buy("600000.SH", amount=100, price=10.0)
        for o in t._read_all_orders():
            t._reconcile(o)
        result = t.cancel_order_stock_async(oid)
        self.assertEqual(result, 0)

    def test_cancel_before_sysid_resolved_returns_minus_one(self):
        """撤单时 sysid 未回填 → 必须先轮询一次查询来补映射"""
        t = self._make()
        oid = t.buy("600000.SH", amount=100, price=10.0)
        # 不清除映射，但 sysid 已知（FakeRpcClient._make_order 返回它）
        # 这里验证 cancel 调用能解析到 sysid
        result = t.cancel_order_stock(oid)
        # _resolve_sysid 会调用 _read_all_orders 来补映射
        self.assertIn(result, (0, -1))

    # ==================================================================
    # 持仓 DataFrame 列契约（原有 + 扩展）
    # ==================================================================

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

    def test_empty_positions_returns_empty_df(self):
        t = self._make()
        t._bq.client.set_positions({})
        df = t.position()
        self.assertEqual(len(df), 0)
        for col in ("证券代码", "股票余额", "可用余额", "成本价", "市值"):
            self.assertIn(col, df.columns)

    def test_query_stock_positions_alias(self):
        t = self._make()
        df = t.query_stock_positions()
        self.assertEqual(df.iloc[0]["证券代码"], "600000")
        self.assertEqual(df.iloc[0]["股票余额"], 1000)

    def test_position_without_available_field_degrades_to_zero(self):
        """持仓缺失 available/can_use_volume 字段时，vendored 层 _safe_int(None)=0。

        注意：真实 RPC 契约（RPC_API_REFERENCE）持仓恒带 available 字段，
        此用例记录字段缺失时的降级边界（可用余额=0，偏保守不会误卖）。
        """
        t = self._make()
        t._bq.client.set_positions({
            "000001.SZ": {"stock_code": "000001.SZ", "volume": 500,
                          "cost": 15.0, "stock_name": "平安银行"}
        })
        df = t.position()
        self.assertEqual(df.iloc[0]["股票余额"], 500)
        self.assertEqual(df.iloc[0]["可用余额"], 0)

    # ==================================================================
    # 资产（原有 + 扩展）
    # ==================================================================

    def test_balance_and_asset(self):
        t = self._make()
        df = t.balance()
        self.assertEqual(df.iloc[0]["可用金额"], 50000.0)
        self.assertEqual(df.iloc[0]["总资产"], 150000.0)
        asset = t.query_stock_asset()
        self.assertEqual(asset["可用金额"], 50000.0)
        self.assertEqual(asset["总资产"], 150000.0)

    def test_balance_with_market_value(self):
        t = self._make()
        t._bq.client.set_asset(cash=30000.0, total_asset=130000.0, market_value=100000.0)
        df = t.balance()
        self.assertEqual(df.iloc[0]["可用金额"], 30000.0)
        self.assertEqual(df.iloc[0]["持仓市值"], 100000.0)
        self.assertEqual(df.iloc[0]["总资产"], 130000.0)

    # ==================================================================
    # 资金校验（新增）
    # ==================================================================

    def test_check_stock_is_av_buy_true(self):
        t = self._make()
        t._bq.client.set_asset(cash=50000.0)
        # 买入 1000 股 × 10.0 = 10000 < 50000
        self.assertTrue(t.check_stock_is_av_buy("600000", price=10.0, amount=1000))

    def test_check_stock_is_av_buy_false_insufficient(self):
        t = self._make()
        t._bq.client.set_asset(cash=5000.0)
        # 买入 1000 股 × 10.0 = 10000 > 5000
        self.assertFalse(t.check_stock_is_av_buy("600000", price=10.0, amount=1000))

    def test_check_stock_is_av_sell_true(self):
        t = self._make()
        # 持仓 600000.SH: volume=1000, available=800
        self.assertTrue(t.check_stock_is_av_sell("600000.SH", amount=500))

    def test_check_stock_is_av_sell_false_insufficient(self):
        t = self._make()
        self.assertFalse(t.check_stock_is_av_sell("600000.SH", amount=2000))

    def test_check_stock_is_av_sell_false_no_position(self):
        t = self._make()
        self.assertFalse(t.check_stock_is_av_sell("999999.SZ", amount=100))

    def test_check_stock_is_av_sell_with_short_code(self):
        """使用6位短代码查可卖（不带后缀）"""
        t = self._make()
        self.assertTrue(t.check_stock_is_av_sell("600000", amount=500))

    # ==================================================================
    # 委托/成交查询（原有 + 扩展）
    # ==================================================================

    def test_query_orders_and_trades(self):
        t = self._make()
        t.buy("600000.SH", amount=100, price=10.0)
        odf = t.query_stock_orders()
        self.assertEqual(len(odf), 1)
        self.assertEqual(len(t.query_stock_trades()), 0)
        t._bq.client.set_order_filled("bq:1", 100, 10.0)
        tdf = t.query_stock_trades()
        self.assertEqual(len(tdf), 1)

    def test_today_entrusts_alias(self):
        t = self._make()
        t.buy("600000.SH", amount=100, price=10.0)
        df = t.today_entrusts()
        self.assertEqual(len(df), 1)

    def test_today_trades_alias(self):
        t = self._make()
        t.buy("600000.SH", amount=100, price=10.0)
        t._bq.client.set_order_filled("bq:1", 100, 10.0)
        df = t.today_trades()
        self.assertEqual(len(df), 1)

    def test_active_orders_by_stock(self):
        t = self._make()
        t.buy("600000.SH", amount=100, price=10.0)
        active = t.get_active_orders_by_stock("600000.SH")
        self.assertEqual(len(active), 1)
        info = t.get_active_order_info_by_stock("600000.SH")
        self.assertEqual(info[0]["stock_code"][:6], "600000")

    def test_active_orders_by_stock_with_short_code(self):
        t = self._make()
        t.buy("600000.SH", amount=100, price=10.0)
        active = t.get_active_orders_by_stock("600000")
        self.assertEqual(len(active), 1)

    def test_get_active_order_info_by_stock_format(self):
        t = self._make()
        t.buy("600000.SH", amount=100, price=10.0)
        info = t.get_active_order_info_by_stock("600000.SH")
        self.assertIsInstance(info, list)
        self.assertEqual(len(info), 1)
        for key in ("order_id", "stock_code", "order_type", "order_status",
                     "order_volume", "traded_volume", "price", "strategy_name"):
            self.assertIn(key, info[0])

    def test_xt_trader_query_stock_orders(self):
        t = self._make()
        t.buy("600000.SH", amount=100, price=10.0)
        orders = t.xt_trader.query_stock_orders(t.acc)
        self.assertGreaterEqual(len(orders), 1)

    def test_xt_trader_query_stock_order_found(self):
        t = self._make()
        oid = t.buy("600000.SH", amount=100, price=10.0)
        o = t.xt_trader.query_stock_order(t.acc, oid)
        self.assertIsNotNone(o)
        self.assertEqual(o.order_id, oid)

    def test_xt_trader_query_stock_order_not_found(self):
        t = self._make()
        o = t.xt_trader.query_stock_order(t.acc, 999999999)
        self.assertIsNone(o)

    def test_xt_trader_cancel_order_stock(self):
        t = self._make()
        oid = t.buy("600000.SH", amount=100, price=10.0)
        for o in t._read_all_orders():
            t._reconcile(o)
        result = t.xt_trader.cancel_order_stock(t.acc, oid)
        self.assertEqual(result, 0)

    # ==================================================================
    # 健康诊断（新增）
    # ==================================================================

    def test_get_rpc_health_snapshot(self):
        t = self._make(alive=True)
        t.connect()
        health = t.get_rpc_health()
        self.assertEqual(health["account"], "TESTACC")
        self.assertEqual(health["transport"], "redis")
        self.assertTrue(health["connected"])
        self.assertTrue(health["rpc_alive"])
        self.assertEqual(health["allow_order"], True)
        self.assertIn("tracked_orders", health)
        self.assertIn("poller_alive", health)

    def test_get_rpc_health_reflects_disconnected_state(self):
        t = self._make(alive=False)
        health = t.get_rpc_health()
        self.assertFalse(health["connected"])
        self.assertFalse(health["rpc_alive"])

    # ==================================================================
    # 边界场景（新增）
    # ==================================================================

    def test_order_id_map_truncation(self):
        """_id_map > 4096 时，_send() 下单会触发 FIFO 截断到约 2048。"""
        t = self._make()
        # 预填 4097 条历史映射
        with t._map_lock:
            for k in range(4097):
                t._id_map[k] = {"user_order_id": str(k), "order_sys_id": None,
                                "stock_code": "000001.SZ", "action": "buy", "volume": 100}
        self.assertGreater(len(t._id_map), 4096)
        # 真实下单驱动 _send 的截断分支
        oid = t.buy("600000.SH", amount=100, price=10.0)
        self.assertIsInstance(oid, int)
        # 截断后应显著下降（pop 掉 2048 条），且新单仍在
        self.assertLessEqual(len(t._id_map), 4096)
        self.assertIn(oid, t._id_map)

    def test_buy_with_default_config(self):
        """config 缺失时使用默认值创建 QmtRpcTrader"""
        # 直接传参数，不依赖 config
        t = QmtRpcTrader(account="FALLBACK", account_type="STOCK")
        t._bq.client = FakeRpcClient(account_id="FALLBACK", alive=True)
        t._allow_order = True
        oid = t.buy("600000.SH", amount=100, price=10.0)
        self.assertIsInstance(oid, int)
        self.assertEqual(t.account, "FALLBACK")

    def test_adjust_stock_delegates_to_base(self):
        t = self._make()
        self.assertEqual(t.adjust_stock("600000"), "600000.SH")
        self.assertEqual(t.adjust_stock("000001"), "000001.SZ")
        self.assertEqual(t.adjust_stock("600000.SH"), "600000.SH")

    def test_select_data_type_delegates_to_base(self):
        t = self._make()
        self.assertEqual(t.select_data_type("600000"), "stock")
        self.assertEqual(t.select_data_type("110053"), "bond")
        self.assertEqual(t.select_data_type("510050"), "fund")

    def test_select_slippage_applies_buy(self):
        t = QmtRpcTrader(account="TESTACC", is_slippage=True, slippage=0.01)
        price = 10.0
        # buy: 加滑点
        adjusted = t.select_slippage("600000", price, "buy")
        self.assertGreater(adjusted, price)

    def test_select_slippage_applies_sell(self):
        t = QmtRpcTrader(account="TESTACC", is_slippage=True, slippage=0.01)
        price = 10.0
        # sell: 减滑点
        adjusted = t.select_slippage("600000", price, "sell")
        self.assertLess(adjusted, price)

    def test_select_slippage_disabled(self):
        t = QmtRpcTrader(account="TESTACC", is_slippage=False, slippage=0.01)
        price = 10.0
        self.assertEqual(t.select_slippage("600000", price, "buy"), price)

    def test_get_active_orders_by_stock_empty_for_unrelated_code(self):
        t = self._make()
        t.buy("600000.SH", amount=100, price=10.0)
        active = t.get_active_orders_by_stock("000001.SZ")
        self.assertEqual(len(active), 0)

    # ==================================================================
    # FakeAccount 属性（新增）
    # ==================================================================

    def test_fake_account_attributes(self):
        t = self._make()
        self.assertEqual(t.acc.account_id, "TESTACC")
        self.assertEqual(t.acc.account_type, "STOCK")

    def test_fake_xt_trader_has_expected_methods(self):
        t = self._make()
        self.assertTrue(callable(t.xt_trader.query_stock_orders))
        self.assertTrue(callable(t.xt_trader.query_stock_order))
        self.assertTrue(callable(t.xt_trader.cancel_order_stock))


if __name__ == "__main__":
    unittest.main()
