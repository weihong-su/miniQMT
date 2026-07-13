# coding: utf-8
"""Client-side private config example for MiniQMT-compatible replacement.

Copy this file to:

    src/bigqmt_signal_trader_client_config.py

Do not commit the real file. It may contain account ids and Redis credentials.
"""

BIGQMT_ACCOUNT_ID = "YOUR_ACCOUNT_ID"
BIGQMT_RPC_TIMEOUT_SECONDS = 6.0

BIGQMT_REDIS_CONFIG = {
    "host": "YOUR_REDIS_HOST",
    "port": 6379,
    "db": 5,
    "username": "",
    "password": "",
}

# Default direct mode calls get_full_tick through RPC. Set enabled=True only when
# you want client-side get_full_tick to read demand-driven Redis snapshots.
BIGQMT_FULL_TICK_CACHE_CONFIG = {
    "enabled": False,
    "demand_ttl_seconds": 10,
    "cache_ttl_seconds": 10,
    "wait_seconds": 3.5,
    "poll_interval_seconds": 0.2,
}

# Client-side LOCAL market-data cache.
#   download_history_data2(codes, period, start_time, ..., callback) pulls bars
#   over RPC once and persists them under `dir`; get_local_data(...) then reads
#   them locally with NO RPC to Big QMT (for offline / repeated local analysis).
#   - dir: cache folder (default ~/.bigqmt_cache), one pickle per (period, code).
#   - fallback_rpc: if True, get_local_data auto-fetches+caches a cache miss;
#     if False (default), a cache-missed code is simply omitted (download first).
BIGQMT_LOCAL_CACHE_CONFIG = {
    "enabled": True,
    "dir": None,            # None -> ~/.bigqmt_cache
    "fallback_rpc": False,
    # Storage format: "auto" (parquet if pyarrow installed, else pickle),
    # "parquet" (columnar/compressed/cross-language — recommended), or "pkl".
    # One file per (period, dividend_type, code); switching format auto-migrates.
    "format": "auto",
}
