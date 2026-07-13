# -*- coding: utf-8 -*-
"""大QMT RPC 只读联调自检（L0 redis库 / L1 Redis直连 / L2 RPC链路）。
全程只读，绝不下单。配置从 .env 经 config 加载。"""
import sys, os
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "qmt-trader"))
sys.path.insert(0, _ROOT)

# 明确走生产路径：不禁用 .env（自检需要读 .env 里的 RPC 配置）
os.environ.pop("MINIQMT_DISABLE_DOTENV", None)

import config

ACCOUNT = sys.argv[1] if len(sys.argv) > 1 else str(config.get_account_config().get("account_id", ""))

def line(): print("-" * 60)

print("=" * 60)
print("大QMT RPC 只读联调自检")
print("=" * 60)
print("账号:", ACCOUNT)
print("ENABLE_QMT_RPC_FALLBACK:", config.ENABLE_QMT_RPC_FALLBACK)
print("transport:", config.QMT_RPC_TRANSPORT)
rc = config.QMT_RPC_REDIS
print("redis: host=%s port=%s db=%s password=%s" % (
    rc.get("host"), rc.get("port"), rc.get("db"),
    "<%d字符>" % len(rc.get("password") or "") if rc.get("password") else "<空>"))
print("QMT_RPC_ALLOW_ORDER:", config.QMT_RPC_ALLOW_ORDER, "(false=只读安全)")

# ---- L0: redis 库 ----
line(); print("[L0] redis 库可用性")
try:
    import redis
    print("  OK: redis", getattr(redis, "__version__", "?"))
except Exception as e:
    print("  FAIL: redis 库未安装 ->", e)
    print("  修复: pip install redis")
    sys.exit(1)

# ---- L1: Redis 直连 ----
line(); print("[L1] Redis 直连 ping")
try:
    kw = dict(host=rc["host"], port=rc["port"], db=rc["db"], socket_timeout=3)
    if rc.get("password"): kw["password"] = rc["password"]
    r = redis.Redis(**kw)
    print("  PING ->", r.ping())
except Exception as e:
    print("  FAIL: Redis 连不上 ->", e)
    print("  检查: Memurai 服务是否运行 / 密码 / 端口")
    sys.exit(1)

# ---- L2: RPC 链路（只读）----
line(); print("[L2] QmtRpcTrader 只读链路")
from qmt_rpc_trader import QmtRpcTrader
t = QmtRpcTrader(account=ACCOUNT)

print("  ping_xttrader ->", t.ping_xttrader(), "(True=大QMT策略在线)")
print("  health ->", t.get_rpc_health())

if not t.ping_xttrader():
    print("\n  [!] RPC ping 失败。排查:")
    print("      - 大QMT策略是否在跑该账号(%s)？面板应有 [bigqmt_rpc] started channel" % ACCOUNT)
    print("      - 两端 db 是否都为 %s？两端账号是否一致？" % rc.get("db"))
    print("      - 若大QMT端跑的是另一个账号，用: python %s <account_id>" % os.path.basename(__file__))
    sys.exit(2)

line(); print("  查询资产 query_stock_asset()")
try:
    asset = t.query_stock_asset()
    print("   ", asset)
except Exception as e:
    print("   FAIL:", e)

line(); print("  查询持仓 position()")
try:
    df = t.position()
    n = len(df) if hasattr(df, "__len__") else 0
    print("    持仓数:", n)
    if n:
        cols = ["证券代码", "股票余额", "可用余额", "成本价", "市值"]
        try:
            print(df[cols].to_string(index=False))
        except Exception:
            print(df)
except Exception as e:
    print("   FAIL:", e)

line(); print("  查询委托 query_stock_orders()")
try:
    odf = t.query_stock_orders()
    print("    委托数:", len(odf) if hasattr(odf, "__len__") else 0)
except Exception as e:
    print("   FAIL:", e)

line(); print("  查询成交 query_stock_trades()")
try:
    tdf = t.query_stock_trades()
    print("    成交数:", len(tdf) if hasattr(tdf, "__len__") else 0)
except Exception as e:
    print("   FAIL:", e)

line()
print("自检完成（只读，未下单）。")
try:
    t.stop()
except Exception:
    pass
