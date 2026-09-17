"""
XtQuantManager 完整功能测试

使用真实 QMT 环境验证所有接口。
测试分组：
  A. 服务器启动与账号注册
  B. XtQuantClient 兼容接口（对比 easy_qmt_trader）
  C. XtDataAdapter 行情接口（对比 xtquant.xtdata）
  D. 工厂函数路由（ENABLE_XTQUANT_MANAGER 开关）

运行方式：
  python test/test_functional_xtquant_manager.py

注意：只测试只读操作（查询），不下单。
"""
import sys
import os
import time
import json
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── 颜色输出 ──────────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

def ok(msg):   print(f"  {GREEN}[PASS]{RESET} {msg}")
def fail(msg): print(f"  {RED}[FAIL]{RESET} {msg}")
def warn(msg): print(f"  {YELLOW}[WARN]{RESET} {msg}")
def info(msg): print(f"  {CYAN}[INFO]{RESET} {msg}")
def section(msg): print(f"\n{BOLD}{CYAN}{'='*60}{RESET}\n{BOLD}{msg}{RESET}")


# ── 读取账号配置 ───────────────────────────────────────────────────────────────
with open("account_config.json") as f:
    _ACFG = json.load(f)

ACCOUNT_ID   = _ACFG["account_id"]
ACCOUNT_TYPE = _ACFG.get("account_type", "STOCK")
QMT_PATH     = _ACFG["qmt_path"]

SERVER_URL = "http://127.0.0.1:8888"

results = {"pass": 0, "fail": 0, "warn": 0}


def check(cond, msg, critical=False):
    if cond:
        ok(msg)
        results["pass"] += 1
        return True
    else:
        fail(msg)
        results["fail"] += 1
        if critical:
            raise RuntimeError(f"关键检查失败，中止: {msg}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# 准备：启动实际 XtQuantManager 服务器
# ═══════════════════════════════════════════════════════════════════════════════

_server_instance = None
_server_thread   = None

def start_real_server():
    """在后台线程启动真实 XtQuantManager 服务器"""
    global _server_instance
    from xtquant_manager.server_runner import XtQuantServer, XtQuantServerConfig
    cfg = XtQuantServerConfig(host="127.0.0.1", port=8888, api_token="")
    _server_instance = XtQuantServer(cfg)
    _server_instance.start(blocking=False)


def register_account_http():
    """通过 HTTP API 注册真实账号"""
    import httpx
    payload = {
        "account_id":           ACCOUNT_ID,
        "account_type":         ACCOUNT_TYPE,
        "qmt_path":             QMT_PATH,
        "call_timeout":         5.0,
        "reconnect_interval":   30.0,
        "max_reconnect_attempts": 3,
    }
    resp = httpx.post(
        f"{SERVER_URL}/api/v1/accounts",
        json=payload,
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


# ═══════════════════════════════════════════════════════════════════════════════
# A. 服务器启动与账号注册
# ═══════════════════════════════════════════════════════════════════════════════

def test_A_server_startup():
    section("A. 服务器启动与账号注册")

    # A1. 启动服务器
    info("启动 XtQuantManager 服务器 (127.0.0.1:8888)...")
    try:
        start_real_server()
        time.sleep(1.5)   # 等待 uvicorn 就绪
        ok("服务器线程已启动")
        results["pass"] += 1
    except Exception as e:
        fail(f"服务器启动失败: {e}")
        results["fail"] += 1
        raise

    # A2. 健康检查
    import httpx
    try:
        resp = httpx.get(f"{SERVER_URL}/api/v1/health", timeout=5)
        data = resp.json()
        check(resp.status_code == 200, f"GET /health → 200 OK")
        check(data.get("success") is True, "/health 响应 success=true")
    except Exception as e:
        fail(f"健康检查失败: {e}")
        results["fail"] += 1
        raise

    # A3. 注册账号
    info(f"注册账号 {ACCOUNT_ID} ...")
    try:
        reg = register_account_http()
        check(reg.get("success") is True, f"POST /accounts → success=true")
        connected = reg.get("data", {}).get("connected", False)
        check(connected is True, f"账号 {ACCOUNT_ID} 已连接到 QMT", critical=True)
    except Exception as e:
        fail(f"账号注册失败: {e}")
        results["fail"] += 1
        raise

    # A4. 查询账号状态
    try:
        resp = httpx.get(f"{SERVER_URL}/api/v1/accounts/{ACCOUNT_ID}/status", timeout=5)
        data = resp.json()
        check(resp.status_code == 200, f"GET /accounts/{ACCOUNT_ID}/status → 200")
        check(data.get("success") is True, "账号状态查询成功")
        account_data = data.get("data", {})
        info(f"账号状态: healthy={account_data.get('healthy')}, "
             f"connected={account_data.get('connected')}")
    except Exception as e:
        warn(f"账号状态查询失败: {e}")
        results["warn"] += 1


# ═══════════════════════════════════════════════════════════════════════════════
# B. XtQuantClient 兼容接口对比测试
# ═══════════════════════════════════════════════════════════════════════════════

def test_B_client_compat():
    section("B. XtQuantClient vs easy_qmt_trader 兼容性测试")

    import pandas as pd
    from xtquant_manager.client import XtQuantClient, ClientConfig
    from easy_qmt_trader import easy_qmt_trader

    # 创建两个客户端
    client = XtQuantClient(config=ClientConfig(
        base_url=SERVER_URL,
        account_id=ACCOUNT_ID,
        api_token="",
    ))
    original = easy_qmt_trader(
        path=QMT_PATH,
        account=ACCOUNT_ID,
        account_type=ACCOUNT_TYPE,
    )
    original.connect()

    # B1. connect()
    result = client.connect()
    check(result is not None, "XtQuantClient.connect() 返回非 None")
    check(result == (client, client), "connect() 返回 (self, self)")

    # B2. position()
    info("测试 position() …")
    pos_client   = client.position()
    pos_original = original.position()

    check(isinstance(pos_client, pd.DataFrame),   "position() 返回 DataFrame")
    check(pos_client.shape[0] == pos_original.shape[0],
          f"持仓行数一致: {pos_client.shape[0]} == {pos_original.shape[0]}")

    # 列兼容性验证：原始列应是客户端列的子集（客户端额外返回更多字段）
    client_cols   = set(pos_client.columns.tolist())
    original_cols = set(pos_original.columns.tolist())
    missing_in_client = original_cols - client_cols
    check(len(missing_in_client) == 0,
          f"easy_qmt_trader 的所有列均在 XtQuantClient 中: missing={missing_in_client}")
    extra_in_client = client_cols - original_cols
    info(f"XtQuantClient 额外列（超出 easy_qmt_trader）: {sorted(extra_in_client)}")

    # 核心持仓数据验证（股票代码）
    if pos_client.shape[0] > 0 and pos_original.shape[0] > 0:
        # 找到证券代码列
        code_col = None
        for c in pos_client.columns:
            if "证券" in c or "代码" in c:
                code_col = c
                break
        if code_col:
            client_codes   = set(pos_client[code_col].tolist())
            original_codes = set(pos_original[code_col].tolist())
            check(client_codes == original_codes,
                  f"持仓股票代码一致: {client_codes}")
        info(f"持仓数据预览:\n{pos_client.to_string(index=False)}")

    # B3. balance()
    info("测试 balance() …")
    bal_client   = client.balance()
    bal_original = original.balance()

    check(isinstance(bal_client, pd.DataFrame),   "balance() 返回 DataFrame")
    check(bal_client.shape[0] > 0,                "balance() 有数据行")

    client_bal_cols   = set(bal_client.columns.tolist())
    original_bal_cols = set(bal_original.columns.tolist())
    check(client_bal_cols == original_bal_cols,
          f"资产 DataFrame 列名一致")
    if client_bal_cols != original_bal_cols:
        warn(f"  客户端列: {sorted(client_bal_cols)}")
        warn(f"  原始列:   {sorted(original_bal_cols)}")

    if bal_client.shape[0] > 0:
        info(f"资产数据:\n{bal_client.to_string(index=False)}")

    # B4. query_stock_asset()
    info("测试 query_stock_asset() …")
    asset = client.query_stock_asset()
    check(isinstance(asset, dict), "query_stock_asset() 返回 dict")
    info(f"资产详情: {asset}")

    # B5. register_trade_callback (no-op, 不抛异常)
    try:
        client.register_trade_callback(lambda: None)
        check(True, "register_trade_callback() 不抛异常")
    except Exception as e:
        check(False, f"register_trade_callback() 抛出异常: {e}")

    # B6. subscribe_callback (no-op, 不抛异常)
    try:
        client.subscribe_callback()
        check(True, "subscribe_callback() 不抛异常")
    except Exception as e:
        check(False, f"subscribe_callback() 抛出异常: {e}")

    # B7. query_stock_orders() — 委托查询
    info("测试 query_stock_orders() …")
    orders = client.query_stock_orders()
    check(isinstance(orders, pd.DataFrame), "query_stock_orders() 返回 DataFrame")
    info(f"委托数量: {len(orders)}")

    # B8. query_stock_trades() — 成交查询
    info("测试 query_stock_trades() …")
    trades = client.query_stock_trades()
    check(isinstance(trades, pd.DataFrame), "query_stock_trades() 返回 DataFrame")
    info(f"成交数量: {len(trades)}")

    client.close()


# ═══════════════════════════════════════════════════════════════════════════════
# C. XtDataAdapter 行情接口对比测试
# ═══════════════════════════════════════════════════════════════════════════════

def test_C_data_adapter():
    section("C. XtDataAdapter vs xtquant.xtdata 行情接口测试")

    import xtquant.xtdata as xt_orig
    from xtquant_manager.client import XtQuantClient, ClientConfig, XtDataAdapter

    client  = XtQuantClient(config=ClientConfig(
        base_url=SERVER_URL,
        account_id=ACCOUNT_ID,
    ))
    adapter = XtDataAdapter(client)

    test_codes = ["000001.SZ", "600036.SH"]

    # C1. connect()
    result = adapter.connect()
    check(result is True, "XtDataAdapter.connect() 返回 True")

    # C2. get_full_tick()
    info(f"测试 get_full_tick({test_codes}) …")
    tick_adapter = adapter.get_full_tick(test_codes)
    xt_orig.connect()
    tick_orig    = xt_orig.get_full_tick(test_codes)

    check(isinstance(tick_adapter, dict),       "get_full_tick() 返回 dict")
    check(len(tick_adapter) > 0,                "get_full_tick() 有数据")
    check(set(tick_adapter.keys()) == set(tick_orig.keys()),
          f"get_full_tick() 股票代码集合一致: {set(tick_adapter.keys())}")

    # 检查每个股票的关键字段
    for code in test_codes:
        if code in tick_adapter and code in tick_orig:
            adapter_price = tick_adapter[code].get("lastPrice")
            orig_price    = tick_orig[code].get("lastPrice")
            check(adapter_price == orig_price,
                  f"{code} 最新价一致: {adapter_price}")

    # C3. get_market_data_ex()
    info("测试 get_market_data_ex() …")
    from datetime import datetime, timedelta
    end_date   = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")

    try:
        hist_adapter = adapter.get_market_data_ex(
            ["open", "high", "low", "close", "volume"],
            ["000001.SZ"],
            "1d",
            start_date,
            end_date,
        )
        hist_orig = xt_orig.get_market_data_ex(
            ["open", "high", "low", "close", "volume"],
            ["000001.SZ"],
            period="1d",
            start_time=start_date,
            end_time=end_date,
        )

        check(isinstance(hist_adapter, dict),       "get_market_data_ex() 返回 dict")
        check("000001.SZ" in hist_adapter,          "000001.SZ 在结果中")

        if "000001.SZ" in hist_adapter and "000001.SZ" in hist_orig:
            adapter_len = len(hist_adapter["000001.SZ"].get("close", {}))
            orig_len    = len(hist_orig["000001.SZ"].get("close", {}))
            check(adapter_len == orig_len,
                  f"000001.SZ 历史数据条数一致: {adapter_len} == {orig_len}")
            info(f"历史数据条数: {adapter_len}")
    except Exception as e:
        warn(f"get_market_data_ex() 测试失败: {e}")
        results["warn"] += 1

    # C4. download_history_data() — 只验证不抛异常
    info("测试 download_history_data() …")
    try:
        adapter.download_history_data("000001.SZ", "1d")
        check(True, "download_history_data() 不抛异常")
    except Exception as e:
        check(False, f"download_history_data() 抛出异常: {e}")

    client.close()


# ═══════════════════════════════════════════════════════════════════════════════
# D. 工厂函数路由测试
# ═══════════════════════════════════════════════════════════════════════════════

def test_D_factory_routing():
    section("D. 工厂函数路由测试 (ENABLE_XTQUANT_MANAGER 开关)")

    import config
    from xtquant_manager.client import XtQuantClient, XtDataAdapter

    # D1. True 模式 → 工厂返回 XtQuantClient
    original = config.ENABLE_XTQUANT_MANAGER
    try:
        config.ENABLE_XTQUANT_MANAGER = True
        from position_manager import _create_qmt_trader
        trader = _create_qmt_trader()
        check(isinstance(trader, XtQuantClient),
              f"True 模式 _create_qmt_trader() 返回 XtQuantClient: {type(trader).__name__}")

        # 验证 client 能连接
        conn = trader.connect()
        check(conn is not None,
              f"True 模式 XtQuantClient.connect() 成功: {conn is not None}")
        trader.close()
    finally:
        config.ENABLE_XTQUANT_MANAGER = original

    # D2. False 模式 → 工厂返回 easy_qmt_trader
    try:
        config.ENABLE_XTQUANT_MANAGER = False
        from easy_qmt_trader import easy_qmt_trader as EQT
        from position_manager import _create_qmt_trader as f2
        # 重新导入以清除缓存
        import importlib
        import position_manager as pm_mod
        importlib.reload(pm_mod)
        trader2 = pm_mod._create_qmt_trader()
        check(isinstance(trader2, EQT),
              f"False 模式 _create_qmt_trader() 返回 easy_qmt_trader: {type(trader2).__name__}")
    finally:
        config.ENABLE_XTQUANT_MANAGER = original

    # D3. True 模式 → _create_xtdata() 返回 XtDataAdapter
    try:
        config.ENABLE_XTQUANT_MANAGER = True
        from data_manager import _create_xtdata
        xtdata = _create_xtdata()
        check(isinstance(xtdata, XtDataAdapter),
              f"True 模式 _create_xtdata() 返回 XtDataAdapter: {type(xtdata).__name__}")
        check(xtdata.connect() is True,
              "True 模式 XtDataAdapter.connect() 成功")
    finally:
        config.ENABLE_XTQUANT_MANAGER = original

    info(f"config.ENABLE_XTQUANT_MANAGER 已恢复为: {config.ENABLE_XTQUANT_MANAGER}")


# ═══════════════════════════════════════════════════════════════════════════════
# E. HTTP API 可观测性接口
# ═══════════════════════════════════════════════════════════════════════════════

def test_E_observability():
    section("E. 可观测性接口 (metrics / health)")

    import httpx

    # E1. GET /metrics
    try:
        resp = httpx.get(f"{SERVER_URL}/api/v1/metrics", timeout=5)
        data = resp.json()
        check(resp.status_code == 200,          "GET /metrics → 200")
        check(data.get("success") is True,      "/metrics success=true")
        info(f"/metrics 数据: {json.dumps(data.get('data', {}), ensure_ascii=False, indent=2)[:400]}")
    except Exception as e:
        warn(f"/metrics 测试失败: {e}")
        results["warn"] += 1

    # E2. GET /metrics/{account_id}
    try:
        resp = httpx.get(f"{SERVER_URL}/api/v1/metrics/{ACCOUNT_ID}", timeout=5)
        data = resp.json()
        check(resp.status_code == 200,          f"GET /metrics/{ACCOUNT_ID} → 200")
        check(data.get("success") is True,      "单账号 metrics success=true")
    except Exception as e:
        warn(f"单账号 /metrics 测试失败: {e}")
        results["warn"] += 1

    # E3. GET /accounts
    try:
        resp = httpx.get(f"{SERVER_URL}/api/v1/accounts", timeout=5)
        data = resp.json()
        check(resp.status_code == 200,          "GET /accounts → 200")
        account_list = data.get("data", {}).get("accounts", [])
        check(ACCOUNT_ID in account_list,
              f"账号 {ACCOUNT_ID} 在账号列表中")
        info(f"已注册账号: {account_list}")
    except Exception as e:
        warn(f"GET /accounts 测试失败: {e}")
        results["warn"] += 1


# ═══════════════════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    print(f"\n{BOLD}{'═'*60}{RESET}")
    print(f"{BOLD}  XtQuantManager 完整功能测试{RESET}")
    print(f"  账号: {ACCOUNT_ID}  类型: {ACCOUNT_TYPE}")
    print(f"  QMT路径: {QMT_PATH}")
    print(f"  服务地址: {SERVER_URL}")
    print(f"{BOLD}{'═'*60}{RESET}")

    start = time.time()
    errors = []

    for fn, label in [
        (test_A_server_startup, "A"),
        (test_B_client_compat,  "B"),
        (test_C_data_adapter,   "C"),
        (test_D_factory_routing,"D"),
        (test_E_observability,  "E"),
    ]:
        try:
            fn()
        except Exception as e:
            errors.append(f"测试组 {label} 中断: {e}")
            fail(f"测试组 {label} 中断: {e}")
            break

    elapsed = time.time() - start
    total   = results["pass"] + results["fail"]

    print(f"\n{BOLD}{'═'*60}{RESET}")
    print(f"{BOLD}  测试结果汇总{RESET}")
    print(f"{'─'*60}")
    print(f"  通过: {GREEN}{results['pass']}{RESET}")
    print(f"  失败: {RED}{results['fail']}{RESET}")
    print(f"  警告: {YELLOW}{results['warn']}{RESET}")
    print(f"  总计: {total}")
    print(f"  耗时: {elapsed:.1f}s")
    print(f"{'─'*60}")

    if results["fail"] == 0:
        print(f"  {GREEN}{BOLD}[SUCCESS] All checks passed{RESET}")
    else:
        print(f"  {RED}{BOLD}[FAILED] {results['fail']} check(s) failed{RESET}")

    print(f"{BOLD}{'═'*60}{RESET}\n")

    # 停止服务器
    if _server_instance:
        try:
            _server_instance.stop()
        except Exception:
            pass

    return results["fail"] == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
