"""autobuy 买入条件历史回测 (一次性分析脚本)。

对 chan.db 候选池的全部历史入池记录，逐 (股票, 入池日) 重放 autobuy 的买入条件，
输出"历史上哪天该买哪只"的清单。

⚠️ 口径声明 (与实时 filter.check() 的差异，务必知悉):
  1. 换手率 — 用【当前】流通股本(FloatVolume)。xtdata 不提供历史股本序列，
     期间若有送转/增发，历史换手率会失真。标记 turnover_approx=True。
  2. 涨停判定 — 用"当日涨幅 >= 9.8%(主板) / 19.8%(创业板科创板)"近似，
     因 UpStopPrice 同样只有当前值。
  2b. ST 判定 — 用【当前】证券名称。若某股在回测区间内刚被 ST 或刚摘帽，
     判定会与当时实际不符(xtdata 不提供历史名称)。
  3. 成交量口径 — 回测的"当日"指标(涨幅/换手率/盘中累计量比)用历史日线完整
     交易日数据，实时链路用盘中累计量，因此回测值偏高。
     但"近N日收盘量比"两侧完全一致(都只用已收盘交易日)，无此偏差。
  4. MA8/MA20/量比 — 均严格使用【入池日及之前】的数据，无未来函数。
     现价取入池日【收盘价】(实时链路取盘中现价)。

用法:
    python scripts/autobuy_backtest.py                      # 全量回测
    python scripts/autobuy_backtest.py --tables stg_chan,zs_pool
    python scripts/autobuy_backtest.py --limit 50           # 只跑前50只(试运行)
    python scripts/autobuy_backtest.py --out export/bt.csv
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autobuy.config import DEFAULT_CFG_PATH, load_config  # noqa: E402
from autobuy.filter import _recent_volume_ratios, is_st_name  # noqa: E402
from autobuy.pool import to_xt_code  # noqa: E402

CHAN_DB = r"C:\github-repo\stockquant\chan.db"
ALL_TABLES = ("stg_chan", "zs_pool", "chan_pool")

# 涨停幅度近似阈值 (留 0.2% 余量，规避四舍五入)
_LIMIT_MAIN = 0.098      # 主板/中小板 10%
_LIMIT_GROWTH = 0.198    # 创业板(300/301)/科创板(688) 20%
_LIMIT_BJ = 0.298        # 北交所 30%


def _limit_threshold(code: str) -> float:
    num = code.split(".")[0]
    if num.startswith(("300", "301", "688", "689")):
        return _LIMIT_GROWTH
    if num.startswith(("43", "83", "87", "88", "92")):
        return _LIMIT_BJ
    return _LIMIT_MAIN


def read_pool_records(db_path: str, tables) -> dict:
    """返回 {xt_code: [入池日, ...]}，跨表去重。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rec = defaultdict(set)
    try:
        for tbl in tables:
            try:
                rows = conn.execute(f"SELECT code, date FROM {tbl}").fetchall()
            except sqlite3.Error as e:
                print(f"  [WARN] 读表失败，跳过 {tbl}: {e}")
                continue
            for code, date in rows:
                if code and date:
                    rec[to_xt_code(str(code).strip())].add(str(date).strip())
            print(f"  {tbl}: {len(rows)} 行")
    finally:
        conn.close()
    return {k: sorted(v) for k, v in sorted(rec.items())}


def evaluate(cfg, code, hist, pool_date, float_vol, roll_forward=False):
    """在 pool_date 这一天重放买入条件。返回 (passed, detail) 或 None(数据不足)。

    hist: 该股完整日线 DataFrame，按 date 升序，含 close/volume。
    严格只用 <= eval_date 的行，无未来函数。

    roll_forward=False: 入池日非交易日(周末/节假日/停牌)时跳过。
    roll_forward=True : 顺延到入池日之后的第一个交易日再评估，对应"周末选股、
                        下一交易日买入"的实际用法。此时 detail['date'] 为实际
                        评估日，detail['pool_date'] 保留原始入池日。
    """
    upto = hist[hist["date"] <= pool_date]
    eval_date = upto.iloc[-1]["date"] if len(upto) else None

    if eval_date != pool_date:
        if not roll_forward:
            return None
        # 顺延: 取入池日之后第一个有K线的交易日
        after = hist[hist["date"] > pool_date]
        if after.empty:
            return None
        eval_date = after.iloc[0]["date"]
        upto = hist[hist["date"] <= eval_date]

    if len(upto) < 21:           # MA20 需 20 根 + 前一日用于比较
        return None

    row = upto.iloc[-1]
    price = float(row["close"])          # 回测用收盘价代表"现价"
    volume = float(row["volume"])
    prev_close = float(upto.iloc[-2]["close"])
    if price <= 0 or prev_close <= 0:
        return None

    failed = []
    d = {
        "code": code, "date": eval_date, "pool_date": pool_date,
        "close": round(price, 3),
        "pct_change": round(price / prev_close - 1, 4),
    }

    # --- 涨停近似 ---
    if cfg.skip_limit_up:
        if d["pct_change"] >= _limit_threshold(code):
            d["limit_up_approx"] = True
            failed.append("已涨停(近似)")
            return False, {**d, "failed": failed}

    # --- 换手率 (用当前流通股本，近似) ---
    if cfg.enable_turnover_rate:
        if float_vol and volume > 0:
            turnover = (volume * cfg.volume_unit_multiplier) / float_vol
            d["turnover_rate"] = round(turnover, 4)
            if turnover < cfg.min_turnover_rate:
                failed.append(f"换手率{turnover:.2%}<{cfg.min_turnover_rate:.2%}")
        else:
            d["turnover_rate"] = None
            failed.append("换手率无法计算")

    # --- 盘中累计量比 (回测用当日收盘全量，口径比实盘宽松；默认关闭) ---
    if cfg.enable_volume_ratio:
        avg5 = float(upto["volume"].iloc[-6:-1].mean())
        if avg5 > 0:
            vr = volume / avg5
            d["volume_ratio"] = round(vr, 3)
            if vr < cfg.min_volume_ratio:
                failed.append(f"量比{vr:.2f}<{cfg.min_volume_ratio}")
        else:
            d["volume_ratio"] = None
            failed.append("量比无法计算")

    # --- 近 N 日收盘量比 (与实时链路复用同一实现，口径完全一致) ---
    if cfg.enable_recent_volume_ratio:
        ratios = _recent_volume_ratios(
            upto, cfg.recent_volume_ratio_days, cfg.volume_ratio_baseline_days
        )
        if ratios is None:
            d["recent_volume_ratios"] = None
            failed.append("近N日量比无法计算")
        else:
            d["recent_volume_ratios"] = "/".join(f"{r:.2f}" for r in ratios)
            if any(r < cfg.min_recent_volume_ratio for r in ratios):
                failed.append(
                    f"近{cfg.recent_volume_ratio_days}日量比未全部"
                    f">={cfg.min_recent_volume_ratio}"
                )

    # --- 涨幅 (可选) ---
    if cfg.enable_pct_change and d["pct_change"] < cfg.min_pct_change:
        failed.append(f"涨幅{d['pct_change']:.2%}<{cfg.min_pct_change:.2%}")

    # --- MA8 ---
    if cfg.enable_ma8_uptrend or cfg.enable_price_below_ma8_ratio:
        ma8 = upto["close"].rolling(8).mean()
        ma8_now, ma8_prev = float(ma8.iloc[-1]), float(ma8.iloc[-2])
        d["ma8"] = round(ma8_now, 3)
        if cfg.enable_ma8_uptrend and not (ma8_now > ma8_prev):
            failed.append("MA8方向向下")
        if cfg.enable_price_below_ma8_ratio and ma8_now > 0:
            ratio = price / ma8_now
            d["price_to_ma8"] = round(ratio, 4)
            if ratio > cfg.max_price_to_ma8_ratio:
                failed.append(f"现价/MA8={ratio:.3f}>{cfg.max_price_to_ma8_ratio}")

    # --- MA20 区间 ---
    if cfg.enable_ma20_range:
        ma20 = upto["close"].rolling(20).mean()
        ma20_now = float(ma20.iloc[-1])
        if ma20_now > 0:
            dev = price / ma20_now - 1
            d["ma20"] = round(ma20_now, 3)
            d["ma20_deviation"] = round(dev, 4)
            lo, hi = cfg.min_price_to_ma20_deviation, cfg.max_price_to_ma20_deviation
            if not (lo - 1e-9 <= dev <= hi + 1e-9):
                failed.append(f"偏离MA20 {dev:+.2%} 不在[{lo:+.1%},{hi:+.1%}]")
        else:
            d["ma20"] = None
            failed.append("MA20无法计算")

    d["failed"] = failed
    return len(failed) == 0, d


def main() -> int:
    ap = argparse.ArgumentParser(description="autobuy 买入条件历史回测")
    ap.add_argument("--db", default=CHAN_DB)
    ap.add_argument("--tables", default=",".join(ALL_TABLES))
    ap.add_argument("--config", default=DEFAULT_CFG_PATH)
    ap.add_argument("--limit", type=int, help="只跑前 N 只股票(试运行)")
    ap.add_argument(
        "--roll-forward", action="store_true",
        help="入池日为非交易日时顺延到下一交易日评估(对应'周末选股、下一交易日买入')",
    )
    ap.add_argument("--out", default="export/autobuy_backtest.csv")
    args = ap.parse_args()

    cfg = load_config(args.config)
    tables = [t.strip() for t in args.tables.split(",") if t.strip()]

    print("=" * 70)
    print("autobuy 买入条件历史回测")
    print(f"  候选池: {args.db}")
    print(f"  表    : {tables}")
    print("  口径  : 换手率用当前流通股本(近似) / 涨停用涨幅近似 / 现价取收盘价")
    if args.roll_forward:
        print("  顺延  : 入池日非交易日时顺延到下一交易日评估")
    print("=" * 70)

    print("[1/3] 读取候选池入池记录...")
    pool = read_pool_records(args.db, tables)
    if args.limit:
        pool = dict(list(pool.items())[: args.limit])
    total_rec = sum(len(v) for v in pool.values())
    print(f"  -> {len(pool)} 只股票, {total_rec} 条入池记录\n")

    print("[2/3] 逐只取历史K线并重放条件 (首次取数较慢)...")
    from data_manager import get_data_manager
    dm = get_data_manager()

    hits, evaluated, skipped, failed_fetch, st_skipped = [], 0, 0, 0, 0
    seen_eval = set()
    for i, (code, dates) in enumerate(pool.items(), 1):
        if i % 50 == 0 or i == len(pool):
            print(f"  进度 {i}/{len(pool)} | 已命中 {len(hits)}")

        detail = {}
        try:
            detail = dm.xt.get_instrument_detail(code) or {}
        except Exception:
            pass

        # ST 判定不依赖行情，放在取K线之前，省掉整只股票的取数开销
        if cfg.skip_st and is_st_name(detail.get("InstrumentName")):
            st_skipped += 1
            continue

        try:
            hist = dm.download_history_data(code, period="day")
        except Exception as e:
            failed_fetch += 1
            print(f"  [WARN] {code} 取数异常: {e}")
            continue
        if hist is None or getattr(hist, "empty", True) or "close" not in hist.columns:
            failed_fetch += 1
            continue

        hist = hist.sort_values("date").reset_index(drop=True)
        float_vol = detail.get("FloatVolume") or detail.get("FloatVol")
        try:
            float_vol = float(float_vol) if float_vol else None
        except (TypeError, ValueError):
            float_vol = None

        for dt in dates:
            r = evaluate(cfg, code, hist, dt, float_vol, roll_forward=args.roll_forward)
            if r is None:
                skipped += 1
                continue
            ok, d = r
            # 顺延模式下多个入池日可能落到同一交易日，按 (code, 评估日) 去重
            key = (code, d["date"])
            if key in seen_eval:
                continue
            seen_eval.add(key)
            evaluated += 1
            if ok:
                hits.append(d)

    print(f"\n[3/3] 汇总")
    print(f"  可评估记录 : {evaluated}")
    print(f"  数据不足跳过: {skipped}  (K线不足21根/入池日非交易日)")
    print(f"  ST股跳过    : {st_skipped} 只")
    print(f"  取数失败    : {failed_fetch} 只")
    print(f"  ** 满足全部买入条件: {len(hits)} 条 **\n")

    if hits:
        import pandas as pd
        df = pd.DataFrame(hits).drop(columns=["failed"], errors="ignore")
        df = df.sort_values(["date", "code"]).reset_index(drop=True)
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        df.to_csv(args.out, index=False, encoding="utf-8-sig")
        print(f"清单已导出: {args.out}\n")
        with pd.option_context("display.max_rows", 200, "display.width", 200):
            print(df.to_string(index=False))
        print("\n按入池日分布:")
        print(df.groupby("date").size().to_string())
    else:
        print("无任何记录满足全部买入条件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
