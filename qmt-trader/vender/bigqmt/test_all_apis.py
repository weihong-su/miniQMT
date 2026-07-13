# coding: utf-8
"""Systematically test all RPC APIs and MiniQMT alias mapping.

For each method: call it with sensible params, report ok/error and whether
data is non-empty. Also test that MiniQMT aliases resolve to the same handler.

Config is read from bigqmt_signal_trader_local_config (gitignored) or env vars;
no credentials are hard-coded here. Run from a dir where that config module
resolves, e.g.:

    PYTHONPATH="src;D:\\国金证券QMT交易端\\python" python test_all_apis.py

or set BIGQMT_ACCOUNT_ID / BIGQMT_REDIS_HOST / BIGQMT_REDIS_PORT /
BIGQMT_REDIS_DB / BIGQMT_REDIS_PASSWORD env vars.
"""
import os
import time

import redis

from bigqmt_signal_trader.redis_rpc import call_redis_rpc


def _load_account_and_redis():
    cfg = {}
    try:
        import bigqmt_signal_trader_local_config as _c  # noqa
        cfg = getattr(_c, "BIGQMT_REDIS_CONFIG", {}) or {}
        account = getattr(_c, "BIGQMT_ACCOUNT_ID", None) or cfg.get("account_id")
    except Exception:
        account = None
    account = account or os.environ.get("BIGQMT_ACCOUNT_ID", "")
    redis_cfg = dict(
        host=cfg.get("host") or os.environ.get("BIGQMT_REDIS_HOST", "127.0.0.1"),
        port=int(cfg.get("port") or os.environ.get("BIGQMT_REDIS_PORT", 6379)),
        db=int(cfg.get("db") or os.environ.get("BIGQMT_REDIS_DB", 5)),
        password=cfg.get("password", os.environ.get("BIGQMT_REDIS_PASSWORD", "")),
        socket_timeout=15,
    )
    if not redis_cfg["password"]:
        redis_cfg.pop("password")
    return str(account), redis_cfg


ACCOUNT, REDIS = _load_account_and_redis()
# account_id placeholder filled in main() once ACCOUNT is confirmed.
_ACCT_PARAM = {"account_id": None}

# (method, params, label) — params chosen to be valid during/after market hours
TESTS = [
    # --- 行情快照 ---
    ("get_full_tick", {"codes": ["000001.SZ"]}, "tick"),
    ("get_ticks", {"codes": ["000001.SZ"]}, "ticks-alias"),
    # --- 合约/品种 ---
    ("get_instrument", {"code": "000001.SZ"}, "instrument"),
    ("get_instrument_detail", {"code": "000001.SZ"}, "instrument-alias"),
    ("get_instrumentdetail", {"code": "000001.SZ"}, "instrument-alias2"),
    ("get_instrument_type", {"code": "000001.SZ", "variety_list": ["stock", "fund"]}, "inst-type"),
    # --- K线/历史 ---
    ("get_market_data_ex", {"field_list": ["close"], "stock_list": ["000001.SZ"], "period": "1d", "count": 3}, "md-ex"),
    ("get_market_data", {"field_list": ["close"], "stock_list": ["000001.SZ"], "period": "1d", "count": 3}, "md"),
    ("get_local_data", {"field_list": ["close"], "stock_list": ["000001.SZ"], "period": "1d", "count": 3}, "local-data"),
    # --- 板块 ---
    ("get_sector_list", {}, "sector-list"),
    ("get_stock_list_in_sector", {"sector_name": "沪深A股"}, "sector-stocks"),
    # --- 交易日历 ---
    ("get_trading_dates", {"market": "SH", "count": 3}, "trade-dates"),
    ("get_holidays", {}, "holidays"),
    ("get_markets", {}, "markets"),
    ("get_market_last_trade_date", {"market": "SH"}, "last-trade-date"),
    # --- 账户 ---
    ("get_asset", {}, "asset"),
    ("get_positions", {}, "positions"),
    ("query_stock_asset", dict(_ACCT_PARAM), "asset-alias"),
    ("query_stock_positions", dict(_ACCT_PARAM), "positions-alias"),
    ("query_stock_position", dict(stock_code="000001.SZ", **_ACCT_PARAM), "position-single"),
]


def data_summary(data):
    """One-line summary of returned data for readability."""
    if data is None:
        return "None"
    if isinstance(data, dict):
        if not data:
            return "{}"
        if "__bigqmt_type__" in data:
            return "[%s cols=%s records=%d]" % (
                data.get("__bigqmt_type__"),
                data.get("columns"),
                len(data.get("records") or []),
            )
        keys = list(data.keys())[:3]
        return "{%s%s: ...}(%d keys)" % (keys, "" if len(keys) < 3 else ", ...", len(data))
    if isinstance(data, list):
        return "[list len=%d]" % len(data)
    return repr(data)[:60]


def main():
    if not ACCOUNT:
        raise SystemExit("ACCOUNT is empty: set BIGQMT_ACCOUNT_ID or configure bigqmt_signal_trader_local_config")
    # Fill the account_id into the account-query test params now that we know it.
    for i, (method, params, label) in enumerate(TESTS):
        if "account_id" in params and params["account_id"] is None:
            params["account_id"] = ACCOUNT

    r = redis.Redis(**REDIS)
    # warmup
    call_redis_rpc(r, ACCOUNT, "ping", {}, timeout_seconds=8)

    print("=" * 90)
    print("全量 API 测试 (account=%s)" % ACCOUNT)
    print("=" * 90)
    print("%-22s %-8s %-8s %s" % ("method", "ok", "ms", "data summary"))
    print("-" * 90)

    results = {"ok": [], "ok_empty": [], "fail": [], "timeout": []}
    for method, params, label in TESTS:
        t0 = time.time()
        try:
            resp = call_redis_rpc(r, ACCOUNT, method, params, timeout_seconds=12)
            dt = (time.time() - t0) * 1000
            ok = resp.get("ok")
            data = resp.get("data")
            error = resp.get("error", "")
            empty = data is None or data == {} or data == [] or data == ""
            if ok and not empty:
                results["ok"].append(method)
                status = "OK"
            elif ok and empty:
                results["ok_empty"].append(method)
                status = "EMPTY"
            else:
                results["fail"].append((method, error))
                status = "FAIL"
            summary = data_summary(data) if ok else error[:50]
            print("%-22s %-8s %6.0f   %s" % (method, status, dt, summary))
        except Exception as e:
            dt = (time.time() - t0) * 1000
            is_timeout = "timeout" in str(e).lower()
            bucket = "timeout" if is_timeout else "fail"
            results[bucket].append((method, str(e)[:60]))
            print("%-22s %-8s %6.0f   %s" % (method, "TIMEOUT" if is_timeout else "ERROR", dt, str(e)[:50]))

    print("-" * 90)
    print("\n=== 汇总 ===")
    print("有数据 (OK):     %d 个" % len(results["ok"]))
    print("成功但空 (EMPTY): %d 个 %s" % (len(results["ok_empty"]), results["ok_empty"]))
    print("失败 (FAIL):     %d 个 %s" % (len(results["fail"]), [m for m, _ in results["fail"]]))
    print("超时 (TIMEOUT):  %d 个 %s" % (len(results["timeout"]), [m for m, _ in results["timeout"]]))


if __name__ == "__main__":
    main()
