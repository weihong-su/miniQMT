# coding: utf-8
"""Big QMT signal trader dry-run entry.

Put this file into QMT's python strategy directory and load it from QMT.
Current default uses empty signal source and DryRunOrderGateway, so it will not
submit real orders.
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


# Fill this before real account testing. Leave empty for dry-run loading tests.
ACCOUNT_ID = ""


if ACCOUNT_ID:
    set_account_id(ACCOUNT_ID)

configure(mode="dryrun", account_id=ACCOUNT_ID or "dryrun")
