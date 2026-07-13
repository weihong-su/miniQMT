# coding: utf-8
"""Live API smoke test + latency bench (read-only, safe for live account).

Covers every read method grouped by category. Reports per-call status and
latency, plus a category summary. Does NOT call any order/cancel method.
"""
import sys
import time

sys.path.insert(0, r"D:\gjzqqmt\xtquant_big_convert\src")
sys.path.insert(0, r"D:\国金证券QMT交易端_lemo\python")

import bigqmt_signal_trader.xtquant_compat as compat

compat.configure()
client = compat.get_default_client()
ACCOUNT = client.account_id
print("account:", ACCOUNT, "| transport:", client.transport_name)
print("=" * 78)

# (category, method, params)
GROUPS = [
    ("系统", [
        ("ping", {}),
    ]),
    ("行情快照", [
        ("get_full_tick", {"codes": ["000001.SZ"]}),
        ("get_ticks", {"codes": ["000001.SZ", "600000.SH"]}),
    ]),
    ("合约/品种", [
        ("get_instrument", {"code": "000001.SZ"}),
        ("get_instrument_type", {"code": "000001.SZ"}),
        ("get_stock_name", {"stock": "000001.SZ"}),
        ("get_last_close", {"stock": "000001.SZ"}),
        ("get_float_caps", {"stockcode": "000001.SZ"}),
        ("get_total_share", {"stockcode": "000001.SZ"}),
        ("get_contract_multiplier", {"stockcode": "000001.SZ"}),
    ]),
    ("K线/历史", [
        ("get_market_data_ex", {"field_list": ["close"], "stock_list": ["000001.SZ"], "period": "1d", "count": 5}),
        ("get_market_data", {"field_list": ["close"], "stock_list": ["000001.SZ"], "period": "1d", "count": 5}),
        ("get_local_data", {"field_list": ["close"], "stock_list": ["000001.SZ"], "period": "1d", "count": 5}),
        ("get_divid_factors", {"stock_code": "000001.SZ", "end_time": "20250101"}),
    ]),
    ("板块", [
        ("get_sector_list", {}),
        ("get_stock_list_in_sector", {"sector_name": "沪深A股"}),
        ("get_sector_info", {"sector_name": "沪深A股"}),
    ]),
    ("交易日历/时间", [
        ("get_trading_dates", {"market": "SH", "count": 5}),
        ("get_holidays", {}),
        ("get_markets", {}),
        ("get_market_last_trade_date", {"market": "SH"}),
        ("get_trading_calendar", {"market": "SH", "start_time": "20250601", "end_time": "20250615"}),
        ("get_date_location", {"date": "20250701"}),
        ("datetime_to_timetag", {"datetime_str": "20250701150000", "format": "%Y%m%d%H%M%S"}),
        ("timetag_to_datetime", {"timetag": 1751353200000, "format": "%Y%m%d %H:%M:%S"}),
    ]),
    ("财务/因子", [
        ("get_financial_data", {"stock_list": ["000001.SZ"], "table_list": ["CAPITAL"], "start_time": "20240101", "end_time": "20241231"}),
    ]),
    ("ETF/期权/期货", [
        ("get_etf_info", {}),
        ("get_main_contract", {"code_market": "IF"}),
        ("get_his_contract_list", {"market": "IF"}),
    ]),
    ("期权定价", [
        ("bsm_price", {"opt_type": "C", "target_price": 3.0, "strike_price": 2.8, "risk_free": 0.03, "sigma": 0.3, "days": 30}),
        ("bsm_iv", {"opt_type": "C", "target_price": 3.0, "strike_price": 2.8, "option_price": 0.25, "risk_free": 0.03, "days": 30}),
    ]),
    ("龙虎榜/资金流", [
        ("get_longhubang", {"stock_list": ["000001.SZ"], "start_time": "20250101", "end_time": "20250630"}),
        ("get_turnover_rate", {"stock_code": ["000001.SZ"], "start_time": "20250601", "end_time": "20250630"}),
        ("get_industry", {"industry_name": "银行"}),
        ("get_north_finance_change", {"period": "1d"}),
    ]),
    ("账户查询", [
        ("get_asset", {}),
        ("get_positions", {}),
        ("query_stock_position", {"stock_code": "000001.SZ"}),
        ("query_orders", {}),
        ("query_trades", {}),
    ]),
    ("官方交易函数", [
        ("get_ipo_data", {}),
        ("get_new_purchase_limit", {}),
        ("get_hkt_exchange_rate", {}),
        ("get_value_by_order_id", {"order_id": "1"}),
        ("get_last_order_id", {}),
    ]),
    ("融资融券(普通账户应空)", [
        ("get_assure_contract", {}),
        ("get_unclosed_compacts", {}),
        ("get_debt_contract", {}),
        ("get_enable_short_contract", {}),
    ]),
]

results = []  # (category, method, status, ms, summary)


def summarize(d):
    if d is None:
        return "None"
    if isinstance(d, dict):
        if not d:
            return "{}"
        if "__bigqmt_type__" in d:
            return "[%s cols=%d rec=%d]" % (d.get("__bigqmt_type__"), len(d.get("columns") or []), len(d.get("records") or []))
        k = list(d.keys())[:2]
        return "{%s...}(%d)" % (k, len(d))
    if isinstance(d, list):
        return "[len=%d]" % len(d)
    return repr(d)[:40]


for category, methods in GROUPS:
    print("\n--- %s ---" % category)
    for method, params in methods:
        t0 = time.time()
        try:
            data = client.call(method, params)
            ms = (time.time() - t0) * 1000
            status = "OK"
            results.append((category, method, status, ms, summarize(data)))
        except Exception as e:
            ms = (time.time() - t0) * 1000
            status = "FAIL"
            results.append((category, method, status, ms, str(e)[:40]))
        r = results[-1]
        print("  [%-4s %6.1fms] %-28s %s" % (r[2], r[3], r[1], r[4]))

# Summary
print("\n" + "=" * 78)
print("=== 汇总 ===")
ok = [r for r in results if r[2] == "OK"]
fail = [r for r in results if r[2] == "FAIL"]
print("通过 %d / 失败 %d / 总计 %d" % (len(ok), len(fail), len(results)))

print("\n=== 按类别 ===")
cats = {}
for r in results:
    cats.setdefault(r[0], []).append(r)
for cat, items in cats.items():
    o = sum(1 for i in items if i[2] == "OK")
    avg = sum(i[3] for i in items) / len(items)
    print("  %-22s %d/%d  avg=%.1fms" % (cat, o, len(items), avg))

print("\n=== 延迟分布 (OK) ===")
lat = sorted(i[3] for i in ok)
if lat:
    p50 = lat[len(lat) // 2]
    p90 = lat[int(len(lat) * 0.9)]
    print("  n=%d  min=%.1fms  p50=%.1fms  p90=%.1fms  max=%.1fms" % (len(lat), lat[0], p50, p90, lat[-1]))

if fail:
    print("\n=== 失败明细 ===")
    for r in fail:
        print("  %-28s %s" % (r[1], r[4]))
