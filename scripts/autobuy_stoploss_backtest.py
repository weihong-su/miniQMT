"""autobuy 选股 + 生产止盈止损逻辑 的联合回测 (一次性分析脚本)。

输入 : scripts/autobuy_backtest.py 产出的命中清单 CSV (code/date)
过程 : 按信号日收盘价建仓，之后逐日用【生产的止盈止损状态机】判定卖出
输出 : 每笔交易的开平仓明细 + 组合层面统计

⚠️ 复用生产算法，不重写:
  - 动态止盈价 = position_manager.PositionManager.calculate_stop_loss_price()
    (未绑定调用，只依赖 config，不碰 QMT/DB)
  - 状态机四分支严格照搬 check_trading_signals():
      ① 固定止损      current <= cost*(1+STOP_LOSS_RATIO)
      ② 首次止盈-突破  profit >= INITIAL_TAKE_PROFIT_RATIO → 只标记，不卖
      ③ 首次止盈-回撤  自突破后最高价回撤 >= PULLBACK_RATIO → 卖 60%
         (且现价 >= _get_initial_take_profit_min_valid_price，否则清除突破状态)
      ④ 动态全仓止盈   profit_triggered 后，current <= calculate_stop_loss_price()

⚠️ 回测口径 (与实盘的差异):
  1. 日内触发 — 实盘监控线程每 3 秒看实时价；回测用当日 OHLC 的 high/low 近似。
     日内先后顺序不可知，按【止损优先】假设(最保守)。
  2. 买入价 — 信号日收盘价，次日起开始监控(信号日当天不触发卖出)。
  3. 卖出价 — 触发价本身(不含滑点/冲击成本)。
  4. 无手续费/印花税 — 实盘单边约 0.05%~0.15%，会系统性高估收益。
  5. 不复权 — 与网格模块一致用不复权价；区间内若除权会产生虚假跳空。
  6. 未平仓 — 数据末尾仍持有的按最后收盘价市价估值(标记 open)。

用法:
    python scripts/autobuy_stoploss_backtest.py
    python scripts/autobuy_stoploss_backtest.py --hits export/autobuy_backtest_v2.csv
    python scripts/autobuy_stoploss_backtest.py --max-hold-days 60
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

CHAN_DB = r"C:\github-repo\stockquant\chan.db"


def _load_production_config(db_path: str) -> dict:
    """读取生产库持久化配置并应用到 config，使回测参数与实盘一致。"""
    applied = {}
    if not os.path.exists(db_path):
        return applied
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT config_key, config_value, config_type FROM system_config").fetchall()
    except sqlite3.Error:
        return applied
    finally:
        conn.close()

    for key, value, ctype in rows:
        if not hasattr(config, key):
            continue
        try:
            if ctype == "float":
                parsed = float(value)
            elif ctype == "int":
                parsed = int(value)
            elif ctype == "bool":
                parsed = str(value).lower() == "true"
            else:
                parsed = value
        except (TypeError, ValueError):
            continue
        setattr(config, key, parsed)
        applied[key] = parsed
    return applied


class StopProfitSimulator:
    """生产止盈止损状态机的逐日重放器。

    完全复用 PositionManager.calculate_stop_loss_price 计算动态止盈价；
    其余分支按 check_trading_signals 的判定顺序与阈值实现。
    """

    def __init__(self, calc_stop_loss_price):
        self._calc = calc_stop_loss_price

    def _min_valid_take_profit_price(self, cost_price):
        """照搬 _get_initial_take_profit_min_valid_price。"""
        tp = getattr(config, "INITIAL_TAKE_PROFIT_RATIO", 0.0)
        pb = getattr(config, "INITIAL_TAKE_PROFIT_PULLBACK_RATIO", 0.0)
        return cost_price * (1 + tp) * (1 - pb) * (1 - pb)

    def run(self, cost_price, bars, max_hold_days=None):
        """逐日重放。bars: [{date, open, high, low, close}, ...] 升序，不含建仓日。

        返回 (trades, state)：trades 为卖出事件列表，state 含结束时的持仓比例。
        """
        stop_ratio = getattr(config, "STOP_LOSS_RATIO", -0.07)
        tp_ratio = getattr(config, "INITIAL_TAKE_PROFIT_RATIO", 0.06)
        tp_pct = getattr(config, "INITIAL_TAKE_PROFIT_RATIO_PERCENTAGE", 0.6)
        pullback = getattr(config, "INITIAL_TAKE_PROFIT_PULLBACK_RATIO", 0.005)

        stop_loss_price = cost_price * (1 + stop_ratio)
        remaining = 1.0                 # 剩余仓位比例
        profit_triggered = False        # 是否已完成首次止盈(卖出60%)
        breakout = False                # 是否已突破止盈阈值
        breakout_high = 0.0
        highest = cost_price            # 持仓期最高价(动态止盈基准)
        events = []

        for i, bar in enumerate(bars):
            if remaining <= 0:
                break
            if max_hold_days is not None and i >= max_hold_days:
                events.append({
                    "date": bars[i - 1]["date"] if i else bar["date"],
                    "type": "max_hold_exit", "price": bars[i - 1]["close"] if i else bar["close"],
                    "ratio": remaining,
                })
                remaining = 0.0
                break

            hi, lo, close = float(bar["high"]), float(bar["low"]), float(bar["close"])

            # --- ① 固定止损 (最高优先级，日内最低价触发) ---
            # 照搬生产: 亏损须至少达到预期止损的 50% 才认可该信号
            if lo <= stop_loss_price:
                loss_ratio = (cost_price - stop_loss_price) / cost_price
                if loss_ratio >= abs(stop_ratio) * 0.5:
                    events.append({
                        "date": bar["date"],
                        "type": "stop_loss_1" if profit_triggered else "stop_loss_0",
                        "price": stop_loss_price, "ratio": remaining,
                    })
                    remaining = 0.0
                    break

            # --- ④ 动态全仓止盈 (已完成首次止盈) ---
            if profit_triggered:
                if hi > highest:
                    highest = hi
                dyn_price = self._calc(cost_price, highest, True)
                if dyn_price > 0 and lo <= dyn_price:
                    events.append({
                        "date": bar["date"], "type": "take_profit_full",
                        "price": dyn_price, "ratio": remaining,
                    })
                    remaining = 0.0
                    break
                continue

            # --- ②③ 首次止盈: 突破 → 回撤 ---
            if not breakout:
                if hi >= cost_price * (1 + tp_ratio):
                    breakout = True
                    breakout_high = hi       # 突破当日以最高价为突破后最高价
                    highest = max(highest, hi)
                # 突破当日不卖，继续监控(照搬生产: return None 继续)
                continue

            # 已突破: 更新突破后最高价，再判回撤
            if hi > breakout_high:
                breakout_high = hi
            highest = max(highest, hi)

            pullback_ratio = (breakout_high - lo) / breakout_high if breakout_high > 0 else 0
            if pullback_ratio >= pullback:
                trigger_price = breakout_high * (1 - pullback)
                floor = self._min_valid_take_profit_price(cost_price)
                if floor > 0 and trigger_price < floor:
                    # 照搬生产: 信号失效，清除突破状态，等待重新突破
                    breakout = False
                    breakout_high = 0.0
                    continue
                events.append({
                    "date": bar["date"], "type": "take_profit_half",
                    "price": trigger_price, "ratio": remaining * tp_pct,
                })
                remaining -= remaining * tp_pct
                profit_triggered = True
                highest = max(highest, hi)

        return events, {"remaining": remaining, "profit_triggered": profit_triggered}


def main() -> int:
    ap = argparse.ArgumentParser(description="autobuy 选股 + 生产止盈止损 联合回测")
    ap.add_argument("--hits", default="export/autobuy_backtest_v2.csv", help="选股命中清单CSV")
    ap.add_argument("--prod-db", default="data_25105132/trading.db", help="生产库(读持久化参数)")
    ap.add_argument("--max-hold-days", type=int, help="最长持有交易日数(默认不限制)")
    ap.add_argument("--out", default="export/autobuy_stoploss_trades.csv")
    args = ap.parse_args()

    import pandas as pd

    applied = _load_production_config(args.prod_db)
    print("=" * 76)
    print("autobuy 选股 + 生产止盈止损逻辑 联合回测")
    print("=" * 76)
    print("参数 (已从生产库同步):" if applied else "参数 (config.py 默认值):")
    print(f"  固定止损             STOP_LOSS_RATIO            = {config.STOP_LOSS_RATIO:+.3%}")
    print(f"  首次止盈阈值         INITIAL_TAKE_PROFIT_RATIO  = {config.INITIAL_TAKE_PROFIT_RATIO:+.3%}")
    print(f"  首次止盈卖出比例     ..._RATIO_PERCENTAGE       = {config.INITIAL_TAKE_PROFIT_RATIO_PERCENTAGE:.0%}")
    print(f"  首次止盈回撤触发     ..._PULLBACK_RATIO         = {config.INITIAL_TAKE_PROFIT_PULLBACK_RATIO:.3%}")
    print(f"  动态止盈档位         DYNAMIC_TAKE_PROFIT        = {config.DYNAMIC_TAKE_PROFIT}")
    print("=" * 76)

    hits = pd.read_csv(args.hits)
    print(f"[1/3] 选股清单: {len(hits)} 条 ({hits['code'].nunique()} 只)\n")

    # 复用生产的动态止盈价计算(未绑定调用，不需要实例)
    from position_manager import PositionManager
    calc = lambda c, h, t: PositionManager.calculate_stop_loss_price(None, c, h, t)  # noqa: E731
    sim = StopProfitSimulator(calc)

    print("[2/3] 取历史K线并重放止盈止损...")
    from data_manager import get_data_manager
    dm = get_data_manager()

    rows = []
    hist_cache = {}
    for i, rec in enumerate(hits.itertuples(index=False), 1):
        code, signal_date = rec.code, rec.date
        if i % 10 == 0 or i == len(hits):
            print(f"  进度 {i}/{len(hits)}")

        if code not in hist_cache:
            try:
                h = dm.download_history_data(code, period="day")
            except Exception as e:
                print(f"  [WARN] {code} 取数异常: {e}")
                hist_cache[code] = None
                continue
            if h is None or getattr(h, "empty", True):
                hist_cache[code] = None
            else:
                need = {"date", "open", "high", "low", "close"}
                if not need.issubset(h.columns):
                    hist_cache[code] = None
                else:
                    hist_cache[code] = h.sort_values("date").reset_index(drop=True)
        hist = hist_cache[code]
        if hist is None:
            continue

        idx = hist.index[hist["date"] == signal_date]
        if len(idx) == 0:
            continue
        entry_i = int(idx[0])
        cost = float(hist.iloc[entry_i]["close"])
        if cost <= 0:
            continue

        bars = hist.iloc[entry_i + 1:].to_dict("records")
        if not bars:
            continue

        events, state = sim.run(cost, bars, args.max_hold_days)

        # 汇总为单笔交易: 按卖出比例加权算实现收益
        realized = sum(e["ratio"] * (e["price"] / cost - 1) for e in events)
        remaining = state["remaining"]
        last_close = float(bars[-1]["close"])
        unrealized = remaining * (last_close / cost - 1)
        exit_types = "+".join(e["type"] for e in events) or "none"
        exit_date = events[-1]["date"] if events else None
        hold_days = (
            next((j for j, b in enumerate(bars, 1) if b["date"] == exit_date), len(bars))
            if exit_date else len(bars)
        )

        rows.append({
            "code": code, "name": getattr(rec, "name", ""),
            "entry_date": signal_date, "entry_price": round(cost, 3),
            "exit_types": exit_types, "exit_date": exit_date,
            "hold_days": hold_days,
            "realized_pct": round(realized, 4),
            "unrealized_pct": round(unrealized, 4),
            "total_pct": round(realized + unrealized, 4),
            "closed": remaining <= 1e-9,
            "last_close": round(last_close, 3),
        })

    if not rows:
        print("\n无可回测样本。")
        return 1

    df = pd.DataFrame(rows).sort_values(["entry_date", "code"]).reset_index(drop=True)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    df.to_csv(args.out, index=False, encoding="utf-8-sig")

    print(f"\n[3/3] 结果 (已导出 {args.out})\n")
    pd.set_option("display.width", 250)
    pd.set_option("display.max_rows", 200)
    show = ["code", "name", "entry_date", "entry_price", "exit_types",
            "exit_date", "hold_days", "total_pct"]
    print(df[show].to_string(index=False))

    tot = df["total_pct"]
    wins = df[tot > 0]
    print("\n" + "=" * 76)
    print("组合统计")
    print("=" * 76)
    print(f"  样本数           : {len(df)}  (已平仓 {int(df['closed'].sum())} / 未平仓 {int((~df['closed']).sum())})")
    print(f"  平均单笔收益     : {tot.mean():+.2%}")
    print(f"  中位数           : {tot.median():+.2%}")
    print(f"  胜率             : {len(wins)}/{len(df)} = {len(wins) / len(df):.1%}")
    print(f"  最好 / 最差      : {tot.max():+.2%} / {tot.min():+.2%}")
    print(f"  平均持有交易日   : {df['hold_days'].mean():.1f}")
    pos, neg = tot[tot > 0].sum(), -tot[tot < 0].sum()
    print(f"  盈亏比(总盈/总亏): {pos / neg:.2f}" if neg > 0 else "  盈亏比: N/A (无亏损)")
    print("\n  按退出方式分布:")
    g = df.groupby("exit_types")["total_pct"].agg(["count", "mean"])
    for name, r in g.sort_values("count", ascending=False).iterrows():
        print(f"    {name:<34} {int(r['count']):>3} 笔  平均 {r['mean']:+.2%}")
    print("\n  [注意] 未计手续费/印花税(单边约0.05%~0.15%)，实际收益低于上表")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
