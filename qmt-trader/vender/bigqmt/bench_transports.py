# coding: utf-8
"""Compare end-to-end RPC latency across transports.

Runs the same ping workload through:
  * Redis (real server, the production path) via call_redis_rpc
  * ZMQ (local tcp loopback, the low-latency path) via ZmqTransport

Prints a side-by-side min/p50/p90/p99/max comparison. The ZMQ leg spins up a
local in-process server so no QMT process is needed for the comparison.
"""
import argparse
import os
import socket
import statistics
import time
import uuid

import redis

from bigqmt_signal_trader.redis_rpc import call_redis_rpc
from bigqmt_signal_trader.transports.zmq_transport import ZmqTransport


def _load_redis_config():
    """Pull connection details from the local client config or env vars."""
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


REDIS = _load_redis_config()
ACCOUNT = os.environ.get("BIGQMT_ACCOUNT_ID", "")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _stats(name, lats):
    lats = sorted(lats)
    n = len(lats)
    print(
        "%-8s n=%d  min=%.2f  p50=%.2f  p90=%.2f  p99=%.2f  max=%.2f  avg=%.2f ms"
        % (
            name,
            n,
            min(lats),
            statistics.median(lats),
            lats[int(n * 0.9)],
            lats[int(n * 0.99)] if n > 1 else lats[-1],
            max(lats),
            statistics.mean(lats),
        )
    )


def bench_redis(n):
    r = redis.Redis(**REDIS)
    # warmup + connectivity
    try:
        call_redis_rpc(r, ACCOUNT, "ping", {}, timeout_seconds=6)
    except Exception as e:
        print("Redis server not reachable, skipping redis leg: %s" % e)
        return
    lats = []
    for _ in range(n):
        t0 = time.time()
        call_redis_rpc(r, ACCOUNT, "ping", {}, timeout_seconds=6)
        lats.append((time.time() - t0) * 1000)
    _stats("redis", lats)


def bench_zmq(n):
    port = _free_port()
    addr = "tcp://127.0.0.1:%d" % port

    def on_req(req):
        return {
            "schema_version": 1,
            "request_id": req["request_id"],
            "account_id": "zmq",
            "method": req["method"],
            "ok": True,
            "data": {"pong": True},
            "error": "",
            "handled_at": "now",
        }

    server = ZmqTransport(bind_address=addr, account_id="zmq", recv_timeout_seconds=0.3)
    server.start_receiving(on_req, background_threads=True)
    time.sleep(0.3)
    client = ZmqTransport(connect_address=addr, account_id="zmq")
    time.sleep(0.2)
    lats = []
    for _ in range(n):
        req = {
            "schema_version": 1,
            "request_id": uuid.uuid4().hex,
            "account_id": "zmq",
            "method": "ping",
            "params": {},
            "reply_channel": "",
            "reply_list": "",
            "reply_key": "",
            "ttl_seconds": 5,
        }
        t0 = time.time()
        client.send_request(req, timeout_seconds=3.0)
        lats.append((time.time() - t0) * 1000)
    _stats("zmq", lats)
    server.stop()
    client.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--count", type=int, default=100, help="requests per transport")
    ap.add_argument("--skip-redis", action="store_true", help="skip the redis leg")
    args = ap.parse_args()
    print("=== transport latency comparison (n=%d each) ===" % args.count)
    if not args.skip_redis:
        bench_redis(args.count)
    bench_zmq(args.count)


if __name__ == "__main__":
    main()
