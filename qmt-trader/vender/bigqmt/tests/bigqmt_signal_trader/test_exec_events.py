import json
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.exec_events import (
    normalize_order_event,
    normalize_trade_event,
    order_channel,
    publish_order_event,
    publish_trade_event,
    trade_channel,
)
from bigqmt_signal_trader.xtquant_compat import BigQmtXtTrader, XtQuantTraderCallback


class FakeDeal:
    m_strAccountID = "acct"
    m_strInstrumentID = "600000.SH"
    m_dPrice = 10.5
    m_nVolume = 100
    m_strTradeID = "T1"
    m_strOrderSysID = "O1"
    m_strTradeTime = "2026-07-02 10:00:00"
    m_nDirection = 48
    m_dTradeAmount = 1050.0
    m_dComssion = 0.5


class FakeOrder:
    m_strAccountID = "acct"
    m_strInstrumentID = "000001.SZ"
    m_nOrderStatus = 50
    m_nVolumeTotal = 200
    m_nVolumeTraded = 50
    m_dLimitPrice = 9.9
    m_strOrderSysID = "O2"
    m_nDirection = 49
    m_strOptName = "s1"


class FakeRedis:
    def __init__(self):
        self.xadds = []
        self.pubs = []

    def xadd(self, key, fields, maxlen=None, approximate=None):
        self.xadds.append((key, fields))
        return b"1-0"

    def publish(self, key, value):
        self.pubs.append((key, value))
        return 1


class RecordingCallback(XtQuantTraderCallback):
    def __init__(self):
        self.orders = []
        self.trades = []

    def on_stock_order(self, order):
        self.orders.append(order)

    def on_stock_trade(self, trade):
        self.trades.append(trade)


class ExecEventsServerTest(unittest.TestCase):
    def test_normalize_trade_event_maps_thinktrader_fields(self):
        ev = normalize_trade_event(FakeDeal(), "acct")

        self.assertEqual(ev["event_type"], "trade")
        self.assertEqual(ev["stock_code"], "600000.SH")
        self.assertEqual(ev["trade_id"], "T1")
        self.assertEqual(ev["order_sys_id"], "O1")
        self.assertEqual(ev["volume"], 100)
        self.assertEqual(ev["price"], 10.5)
        self.assertEqual(ev["action"], "BUY")  # m_nDirection 48 -> buy
        self.assertEqual(ev["traded_at"], "2026-07-02 10:00:00")
        self.assertEqual(ev["commission"], 0.5)

    def test_normalize_order_event_maps_thinktrader_fields(self):
        ev = normalize_order_event(FakeOrder(), "acct")

        self.assertEqual(ev["event_type"], "order")
        self.assertEqual(ev["stock_code"], "000001.SZ")
        self.assertEqual(ev["order_sys_id"], "O2")
        self.assertEqual(ev["order_volume"], 200)
        self.assertEqual(ev["traded_volume"], 50)
        self.assertEqual(ev["status"], 50)
        self.assertEqual(ev["action"], "SELL")  # m_nDirection 49 -> sell
        self.assertEqual(ev["strategy_name"], "s1")

    def test_publish_writes_stream_and_channel(self):
        r = FakeRedis()
        publish_trade_event(r, "acct", {"event_type": "trade", "trade_id": "T1"})

        self.assertEqual(r.pubs[0][0], trade_channel("acct"))
        self.assertEqual(r.xadds[0][0], trade_channel("acct"))
        self.assertIn("T1", r.pubs[0][1])

        publish_order_event(r, "acct", {"event_type": "order"})
        self.assertEqual(r.pubs[1][0], order_channel("acct"))

    def test_action_comes_from_direction_not_offset_flag(self):
        class Deal:
            m_strInstrumentID = "600000.SH"
            m_nDirection = 49   # EEntrustBS sell
            m_nOffsetFlag = 48  # offset 48 = 开仓 (open) — must NOT be read as buy
            m_nVolume = 10
            m_dPrice = 1.0
            m_strTradeID = "X"

        ev = normalize_trade_event(Deal(), "acct")

        self.assertEqual(ev["action"], "SELL")   # derived from m_nDirection
        self.assertEqual(ev["offset_flag"], 48)  # raw offset preserved, not conflated

    def test_pledge_direction_has_no_buy_sell_action(self):
        class Deal:
            m_strInstrumentID = "600000.SH"
            m_nDirection = 81   # 质押入库
            m_nVolume = 10
            m_dPrice = 1.0

        ev = normalize_trade_event(Deal(), "acct")

        self.assertEqual(ev["action"], "")   # pledge is neither buy nor sell
        self.assertEqual(ev["direction"], 81)  # raw direction preserved


class ExecEventsClientDispatchTest(unittest.TestCase):
    def _trader(self):
        trader = BigQmtXtTrader(account_id="acct")
        cb = RecordingCallback()
        trader.register_callback(cb)
        return trader, cb

    def test_dispatch_trade_invokes_on_stock_trade(self):
        trader, cb = self._trader()
        event = {
            "event_type": "trade",
            "account_id": "acct",
            "stock_code": "600000.SH",
            "order_sys_id": "sys-1",
            "trade_id": "t-1",
            "volume": 100,
            "price": 10.5,
            "action": "BUY",
            "traded_at": "2026-07-02 10:00:00",
        }
        trader._dispatch_event(json.dumps(event).encode("utf-8"))

        self.assertEqual(len(cb.trades), 1)
        trade = cb.trades[0]
        self.assertEqual(trade.stock_code, "600000.SH")
        self.assertEqual(trade.trade_id, "t-1")
        self.assertEqual(trade.traded_volume, 100)
        self.assertEqual(trade.traded_price, 10.5)
        self.assertEqual(trade.order_type, 23)  # BUY -> STOCK_BUY

    def test_dispatch_order_invokes_on_stock_order(self):
        trader, cb = self._trader()
        event = {
            "event_type": "order",
            "account_id": "acct",
            "stock_code": "000001.SZ",
            "order_sys_id": "sys-2",
            "order_volume": 200,
            "traded_volume": 50,
            "price": 9.9,
            "status": 50,
            "action": "SELL",
        }
        trader._dispatch_event(json.dumps(event).encode("utf-8"))

        self.assertEqual(len(cb.orders), 1)
        order = cb.orders[0]
        self.assertEqual(order.stock_code, "000001.SZ")
        self.assertEqual(order.order_volume, 200)
        self.assertEqual(order.traded_volume, 50)
        self.assertEqual(order.order_status, 50)
        self.assertEqual(order.order_type, 24)  # SELL -> STOCK_SELL

    def test_dispatch_without_callback_is_noop(self):
        trader = BigQmtXtTrader(account_id="acct")
        # No callback registered; must not raise.
        trader._dispatch_event(json.dumps({"event_type": "trade"}).encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
