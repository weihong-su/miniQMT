# -*- coding: utf-8 -*-
"""Focused check: is the empty order-query caused by strategy_name mismatch?

Places ONE buy order with an explicit strategy_name, then queries the order
list with several strategy_name values to see which (if any) returns it.
ASCII-only output. Safe: 1 lot, price far below market, cancelled at the end.

usage: python test/live_qmt_rpc_strategy_check.py [account] [stock] [price] [volume]
"""
import sys, os, time
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "qmt-trader"))
sys.path.insert(0, _ROOT)
os.environ.pop("MINIQMT_DISABLE_DOTENV", None)

import config
from qmt_rpc_trader import QmtRpcTrader
import _qmt_trader_base as base

ACCOUNT = sys.argv[1] if len(sys.argv) > 1 else str(config.get_account_config().get("account_id", ""))
STOCK   = sys.argv[2] if len(sys.argv) > 2 else "300105"
PRICE   = float(sys.argv[3]) if len(sys.argv) > 3 else 4.00
VOL     = int(sys.argv[4]) if len(sys.argv) > 4 else 100
ORDER_STRATEGY = "bigqmt_signal_trader"

t = QmtRpcTrader(account=ACCOUNT)
bq = t._bq
print("account:", ACCOUNT, "ping:", t.ping_xttrader(), "allow_order:", t._allow_order, flush=True)
if not (t.ping_xttrader() and t._allow_order):
    print("[!] not ready (ping/allow_order)"); sys.exit(2)

def raw_orders(strategy):
    try:
        r = bq.client.call("query_orders", {"account_id": ACCOUNT, "strategy_name": strategy})
        return r or []
    except Exception as e:
        return "ERR:%s" % str(e)[:80]

print("--- BEFORE: query_orders by strategy ---", flush=True)
for s in (ORDER_STRATEGY, "", "qmt"):
    r = raw_orders(s)
    print("  strategy=%-22r -> %s" % (s, ("len=%d" % len(r)) if isinstance(r, list) else r), flush=True)

print("--- placing 1 order: BUY %s x%d @ %.2f strategy=%r ---" % (STOCK, VOL, PRICE, ORDER_STRATEGY), flush=True)
oid = t.order_stock(STOCK, order_type=base.STOCK_BUY, order_volume=VOL,
                    price=PRICE, strategy_name=ORDER_STRATEGY, order_remark="strat_check")
print("  order_id:", oid, "type:", type(oid).__name__, flush=True)
print("  _id_map:", t._id_map.get(oid), flush=True)
if oid is None:
    print("[!] order returned None"); sys.exit(3)

time.sleep(2.0)

print("--- AFTER: query_orders by strategy ---", flush=True)
for s in (ORDER_STRATEGY, "", "qmt", "strat_check"):
    r = raw_orders(s)
    if isinstance(r, list):
        print("  strategy=%-22r -> len=%d" % (s, len(r)), flush=True)
        for o in r[:3]:
            print("       order:", {k: o.get(k) for k in
                  ("stock_code","order_sys_id","user_order_id","remark","status","volume")}, flush=True)
    else:
        print("  strategy=%-22r -> %s" % (s, r), flush=True)

print("--- client _read_all_orders() ---", flush=True)
orders = t._read_all_orders()
print("  count:", len(orders), flush=True)
for o in orders[:3]:
    print("    ", {k: getattr(o, k, None) for k in
          ("stock_code","order_id","order_sysid","order_status","order_remark")}, flush=True)
print("  our order present:", any(o.order_id == oid for o in orders), flush=True)
print("  learned sysid:", (t._id_map.get(oid) or {}).get("order_sys_id"), flush=True)

print("--- cancel cleanup ---", flush=True)
print("  cancel ret:", t.cancel_order_stock(oid), flush=True)
try:
    t.stop()
except Exception:
    pass
