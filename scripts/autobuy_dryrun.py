"""autobuy 模拟运行试跑脚本 (手动使用)。

在不改 autobuy/miniqmt_autobuy.cfg 的前提下跑一轮模拟买入:
  - 强制 simulation_mode=True (绝不发送真实下单请求)
  - 从 config.WEB_API_TOKEN (.env 的 QMT_API_TOKEN) 自动取 Token，无需手抄密钥
  - 可用 --port / --base-url 覆盖 web_server 地址 (默认读 cfg)
  - 可用 --date 覆盖候选池基准日 (候选池数据陈旧时试跑用)

用法:
    python scripts/autobuy_dryrun.py --port 50000
    python scripts/autobuy_dryrun.py --port 50000 --date 2026-04-20
    python scripts/autobuy_dryrun.py --port 50000 --check-only
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402  (需先插入项目根路径)
from autobuy.config import DEFAULT_CFG_PATH, load_config  # noqa: E402


def _preflight(cfg) -> bool:
    """检查 web 连通性与鉴权，失败时给出可操作的提示。"""
    import requests

    url = f"{cfg.base_url}/api/positions"
    headers = {"X-API-Token": cfg.api_token} if cfg.api_token else {}
    print(f"[1/2] 检查 web_server: {url}")
    try:
        resp = requests.get(url, params={"version": -1}, headers=headers, timeout=cfg.timeout)
    except requests.RequestException as e:
        print(f"  [FAIL] 连接失败: {e}")
        print(f"    -> 确认 web_server 已运行，且端口与 {cfg.base_url} 一致")
        return False

    if resp.status_code == 401:
        print("  [FAIL] HTTP 401: Token 校验失败")
        print("    -> 确认 .env 的 QMT_API_TOKEN 与主程序一致")
        return False
    if resp.status_code != 200:
        print(f"  [FAIL] HTTP {resp.status_code}")
        return False
    print("  [OK] 连通且鉴权通过")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="autobuy 模拟运行试跑")
    parser.add_argument("--config", default=DEFAULT_CFG_PATH)
    parser.add_argument("--port", type=int, help="覆盖 web_server 端口")
    parser.add_argument("--base-url", help="覆盖 web_server 完整地址")
    parser.add_argument("--date", help="覆盖候选池基准日 YYYY-MM-DD (候选池陈旧时试跑用)")
    parser.add_argument("--check-only", action="store_true", help="只做连通性预检，不跑筛选")
    args = parser.parse_args()

    cfg = load_config(args.config)
    cfg.simulation_mode = True  # 硬编码: 本脚本永不发真实买单

    if args.base_url:
        cfg.base_url = args.base_url.rstrip("/")
    elif args.port:
        cfg.base_url = f"http://127.0.0.1:{args.port}"

    # Token 从主程序同一来源取，避免手抄
    if not cfg.api_token and config.WEB_API_TOKEN:
        cfg.api_token = config.WEB_API_TOKEN
        print(f"已从 .env(QMT_API_TOKEN) 自动取用 Token (长度 {len(cfg.api_token)})")

    print("=" * 60)
    print("autobuy 模拟运行试跑 (不会发送任何真实买入请求)")
    print(f"  web_server : {cfg.base_url}")
    print(f"  候选池     : {cfg.db_path}")
    print(f"  基准日     : {args.date or '今天'}  (取前 {cfg.latest_n_dates} 个交易日)")
    print(f"  每轮上限   : {cfg.max_buys_per_run} 只")
    print("=" * 60)

    if not _preflight(cfg):
        return 1
    if args.check_only:
        print("\n--check-only 指定，预检通过，未执行筛选。")
        return 0

    print("[2/2] 执行一轮模拟筛选...\n")

    if args.date:
        # 覆盖候选池基准日: 仅影响本次试跑的取数范围
        from autobuy import app as app_mod
        from autobuy.pool import read_candidates as _real_read

        app_mod.read_candidates = lambda c: _real_read(c, reference_date=args.date)

    from autobuy.app import AutoBuyApp

    app = AutoBuyApp(cfg)
    try:
        app.run_once("manual-dryrun")
    finally:
        app.store.close()

    print("\n" + "=" * 60)
    print("试跑完成。复盘:")
    print("  决策明细: data/autobuy.db -> decision_log 表")
    print("  模拟买入: data/autobuy.db -> buy_history 表 (is_simulation=1)")
    print("  运行日志: logs/miniqmt_autobuy.log")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
