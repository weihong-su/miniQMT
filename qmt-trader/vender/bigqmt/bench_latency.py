# coding: utf-8
"""BigQMT Redis RPC latency benchmark.

Measures end-to-end latency for ping (no QMT API call) and get_full_tick
(real ContextInfo call) to separate transport cost from API cost.
"""
import os
import statistics
import time

import redis
from bigqmt_signal_trader.redis_rpc import call_redis_rpc


def _load_redis_config():
    """Pull connection details from the local client config or env vars.

    Never hardcode secrets in the repo.
    """
    try:
        from bigqmt_signal_trader.xtquant_compat import load_client_config

        cfg = load_client_config()
        rc = dict(cfg.get("redis_config") or {})
        rc.setdefault("host", os.environ.get("BIGQMT_REDIS_HOST", "127.0.0.1"))
        rc.setdefault("port", int(os.environ.get("BIGQMT_REDIS_PORT", "6379")))
        rc.setdefault("db", int(os.environ.get("BIGQMT_REDIS_DB", "5")))
        return {
            "host": rc.get("host"),
            "port": int(rc.get("port")),
            "db": int(rc.get("db")),
            "username": rc.get("username") or None,
            "password": rc.get("password") or None,
            "socket_timeout": 8,
        }
    except Exception:
        return {
            "host": os.environ.get("BIGQMT_REDIS_HOST", "127.0.0.1"),
            "port": int(os.environ.get("BIGQMT_REDIS_PORT", "6379")),
            "db": int(os.environ.get("BIGQMT_REDIS_DB", "5")),
            "socket_timeout": 8,
        }


ACCOUNT = os.environ.get("BIGQMT_ACCOUNT_ID", "")
REDIS = _load_redis_config()


def bench(r, method, params, n=20, timeout=6):
    lats = []
    errors = 0
    for i in range(n):
        t0 = time.time()
        try:
            resp = call_redis_rpc(r, ACCOUNT, method, params, timeout_seconds=timeout)
            dt = (time.time() - t0) * 1000
            if resp.get("ok"):
                lats.append(dt)
            else:
                errors += 1
                if errors <= 2:
                    print("  %s #%d error: %s" % (method, i, resp.get("error", "")[:120]))
        except Exception as e:
            errors += 1
            if errors <= 2:
                print("  %s #%d exc: %s" % (method, i, e))
    if not lats:
        print("%-18s: ALL FAILED (%d errors)" % (method, errors))
        return
    lats.sort()
    p50 = statistics.median(lats)
    p95 = lats[int(len(lats) * 0.95)] if len(lats) >= 20 else lats[-1]
    print(
        "%-18s: n=%d ok=%d fail=%d  min=%.0f  p50=%.0f  p95=%.0f  max=%.0f  avg=%.0f ms"
        % (
            method,
            len(lats),
            len(lats),
            errors,
            min(lats),
            p50,
            p95,
            max(lats),
            statistics.mean(lats),
        )
    )


def main():
    r = redis.Redis(**REDIS)
    # warmup
    try:
        call_redis_rpc(r, ACCOUNT, "ping", {}, timeout_seconds=6)
        print("warmup ping ok\n")
    except Exception as e:
        print("warmup FAILED: %s\n" % e)
        return

    print("=== latency benchmark (20 calls each) ===")
    bench(r, "ping", {}, n=20)
    bench(r, "get_full_tick", {"codes": ["000001.SZ"]}, n=20)
    bench(r, "get_full_tick", {"codes": ["000001.SZ", "600000.SH", "000333.SZ"]}, n=20)
    bench(r, "get_instrument", {"code": "000001.SZ"}, n=20)


if __name__ == "__main__":
    main()
