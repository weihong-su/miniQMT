# coding: utf-8
"""Big QMT Redis dry-run strategy entry.

This entry reads Redis db5 test signals and writes Redis state, but orders are
DryRunOrderGateway orders only. It does not submit real QMT orders.
"""

from bigqmt_signal_trader_strategy import (  # noqa: E402
    adjust,
    configure,
    deal_callback,
    handlebar,
    init,
    on_order,
    on_trade,
    order_callback,
    set_account_id,
    sync_positions,
)


ACCOUNT_ID = "bigqmt_probe"
REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
REDIS_DB = 5
REDIS_USERNAME = ""
REDIS_PASSWORD = ""

try:
    from bigqmt_signal_trader_local_config import BIGQMT_REDIS_CONFIG
except Exception:
    BIGQMT_REDIS_CONFIG = {}

REDIS_HOST = BIGQMT_REDIS_CONFIG.get("host", REDIS_HOST)
REDIS_PORT = int(BIGQMT_REDIS_CONFIG.get("port", REDIS_PORT))
REDIS_DB = int(BIGQMT_REDIS_CONFIG.get("db", REDIS_DB))
REDIS_USERNAME = BIGQMT_REDIS_CONFIG.get("username", REDIS_USERNAME)
REDIS_PASSWORD = BIGQMT_REDIS_CONFIG.get("password", REDIS_PASSWORD)


if ACCOUNT_ID:
    set_account_id(ACCOUNT_ID)

configure(
    mode="dryrun",
    account_id=ACCOUNT_ID,
    signal_source_type="redis",
    state_store_type="redis",
    position_sync_type="redis",
    redis={
        "host": REDIS_HOST,
        "port": REDIS_PORT,
        "db": REDIS_DB,
        "username": REDIS_USERNAME,
        "password": REDIS_PASSWORD,
        "stream_key_template": "bigqmt:signals:{account_id}",
        "group_name": "bigqmt-signal-trader",
        "consumer_name": "bigqmt-probe",
        "block_ms": 0,
        "claim_key_template": "bigqmt:signal_claim:{account_id}:{signal_id}",
        "status_key_template": "bigqmt:signal_status:{account_id}:{signal_id}",
        "position_key_template": "bigqmt:positions:{account_id}",
        "position_event_stream_template": "bigqmt:position_events:{account_id}",
    },
)
