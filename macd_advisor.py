"""MACD 操盘建议(悬浮窗)。

依据日线 MACD 的 DEA 方向与 DIF 相对 0 轴位置，给出"底仓 / 网格"参考建议。
纯只读展示，不触发任何交易动作。

决策矩阵(方向看 DEA，0轴位置看 DIF)::

    DEA 方向   DIF 位置    趋势判定             底仓      网格
    向上       0 轴以上    上升趋势(强)         重仓      启动
    向上       0 轴以下    上升趋势(弱/修复)    半仓以下  启动
    向下       0 轴以上    下降趋势(弱)/顶部反转 半仓以下  启动
    向下       0 轴以下    下降趋势(强)         清仓      停用

DIF 与 DEA 的金叉/死叉仅作补充说明，不改变四选一结果。
"""
from __future__ import annotations

import threading
import time

import config
from logger import get_logger

logger = get_logger()

SHENZHEN_INDEX_CODE = config.SHENZHEN_INDEX_CODE

# 建议结果缓存: {code: {"data": dict, "ts": float}}
_advice_cache: dict = {}
_advice_lock = threading.Lock()


def _is_index_code(code: str) -> bool:
    """判断是否为大盘指数代码(深证成指/中小板/上证)。"""
    c = (code or "").upper()
    return c.startswith("399") or c in ("000001.SH", "999999.SH")


# 决策矩阵四类别(与 _cat_index 返回下标一一对应)
CATEGORIES = [
    {"trend": "上升趋势(强)", "base_position": "重仓", "grid": "启动"},
    {"trend": "上升趋势(弱/修复)", "base_position": "半仓以下", "grid": "启动"},
    {"trend": "下降趋势(弱)/顶部反转", "base_position": "半仓以下", "grid": "启动"},
    {"trend": "下降趋势(强)", "base_position": "清仓", "grid": "停用"},
]


def _cat_index(dea_prev, dea_last, dif_last):
    """DEA 方向 × DIF 相对0轴 → 类别下标 0..3(纯逻辑)。"""
    dea_up = dea_last >= dea_prev              # DEA 方向：向上
    try:
        dif_above_zero = dif_last is not None and float(dif_last) > 0   # DIF 位置：0 轴以上
    except (TypeError, ValueError):
        dif_above_zero = False
    if dea_up and dif_above_zero:
        return 0
    if dea_up and not dif_above_zero:
        return 1
    if not dea_up and dif_above_zero:
        return 2
    return 3


def classify(dea_prev, dea_last, dif_last):
    """依据 DEA 前后值与 DIF 给出建议(纯函数)。

    参数:
        dea_prev: 前一根 DEA(macd_signal)
        dea_last: 最新一根 DEA
        dif_last: 最新一根 DIF(macd)，仅用于金叉/死叉补充说明

    返回:
        dict: {trend, base_position, grid, cross} 或 None(数据无效)
    """
    if dea_prev is None or dea_last is None:
        return None
    try:
        dea_prev = float(dea_prev)
        dea_last = float(dea_last)
    except (TypeError, ValueError):
        return None

    cat = CATEGORIES[_cat_index(dea_prev, dea_last, dif_last)]

    # 金叉/死叉补充说明(DIF 相对 DEA)
    cross = ""
    if dif_last is not None:
        try:
            cross = "DIF在DEA上方(多头)" if float(dif_last) >= dea_last else "DIF在DEA下方(空头)"
        except (TypeError, ValueError):
            cross = ""

    return {"trend": cat["trend"], "base_position": cat["base_position"], "grid": cat["grid"], "cross": cross}


SERIES_BARS = 60  # 迷你全景图展示的日线根数
MA34_PERIOD = 34  # K线叠加的慢均线周期
MA8_PERIOD = 8    # K线叠加的快均线周期


def _build_series(hist_df, ind_df):
    """合并 OHLC 与 MACD，逐日打类别标签，返回最近 SERIES_BARS 根日线序列。

    参数:
        hist_df: get_history_data_from_db 结果(含 open/high/low/close)
        ind_df:  get_indicators_history 结果(含 macd/macd_signal/macd_hist)，按日期升序
    返回:
        list[dict]: [{d,o,h,l,c,dif,dea,hist,ma8,ma34,cat}, ...]
    """
    if hist_df is None or getattr(hist_df, "empty", True):
        return []
    hist_df = hist_df.sort_values("date")
    ohlc = {}
    for _, r in hist_df.iterrows():
        ohlc[str(r["date"])] = (r["open"], r["high"], r["low"], r["close"])

    # MA8 / MA34 均线(基于完整历史收盘价滚动，覆盖可视窗口全部日期)
    ma8_map, ma34_map = {}, {}
    try:
        closes = hist_df["close"].astype(float)
        dates = hist_df["date"].astype(str)
        ma8 = closes.rolling(MA8_PERIOD).mean()
        ma34 = closes.rolling(MA34_PERIOD).mean()
        for date, v8, v34 in zip(dates, ma8, ma34):
            ma8_map[date] = None if v8 != v8 else round(float(v8), 3)     # v!=v 判 NaN
            ma34_map[date] = None if v34 != v34 else round(float(v34), 3)
    except Exception:
        ma8_map, ma34_map = {}, {}

    rows = []
    prev_dea = None
    for _, r in ind_df.iterrows():
        dea = r.get("macd_signal")
        dif = r.get("macd")
        if dea is None:
            continue
        try:
            dea = float(dea)
        except (TypeError, ValueError):
            continue
        date = str(r["date"])
        bar = ohlc.get(date)
        if bar is None:
            continue
        cat = _cat_index(prev_dea if prev_dea is not None else dea, dea, dif)
        prev_dea = dea
        try:
            o, h, l, c = (float(x) for x in bar)
        except (TypeError, ValueError):
            continue
        rows.append({
            "d": date,
            "o": round(o, 3), "h": round(h, 3), "l": round(l, 3), "c": round(c, 3),
            "dif": round(float(dif), 4) if dif is not None else None,
            "dea": round(dea, 4),
            "hist": round(float(r.get("macd_hist")), 4) if r.get("macd_hist") is not None else None,
            "ma8": ma8_map.get(date),
            "ma34": ma34_map.get(date),
            "cat": cat,
        })
    return rows[-SERIES_BARS:]


def _compute_advice(code: str) -> dict:
    """拉取历史数据、计算 MACD 并给出建议(不含缓存)。"""
    from data_manager import get_data_manager
    from indicator_calculator import get_indicator_calculator

    data_manager = get_data_manager()
    indicator_calculator = get_indicator_calculator()

    # 1) 确保历史日线已入库
    history_status = "fresh"
    try:
        if _is_index_code(code):
            from autobuy.filter import download_market_index_history
            df = download_market_index_history(data_manager, code)
            if df is not None and not getattr(df, "empty", True):
                history_status = df.attrs.get("data_status", "fresh")
                data_manager.save_history_data(code, df)
            else:
                history_status = "unavailable"
        else:
            data_manager.update_stock_data(code)
    except Exception as e:
        history_status = "unavailable"
        logger.warning(f"[MACD建议] {code} 历史数据准备失败: {e}")

    hist_df = data_manager.get_history_data_from_db(code)
    if hist_df is None or hist_df.empty or 'close' not in hist_df.columns:
        return dict(status="error", code=code, history_status="unavailable", message="无有效历史日线")
    hist_df = data_manager._filter_history_date_range(
        hist_df, end_date=data_manager._get_completed_history_end_date())
    hist_df = hist_df[hist_df["close"].notna() & (hist_df["close"] > 0)]
    if len(hist_df) < config.MACD_ADVICE_MIN_HISTORY_BARS:
        return dict(status="error", code=code, history_status="insufficient",
                    message="历史日线不足，暂不生成建议")
    if history_status == "unavailable":
        history_status = "cache"

    # 2) 计算并落库指标
    try:
        indicator_calculator.calculate_all_indicators(code)
    except Exception as e:
        logger.warning(f"[MACD建议] {code} 指标计算失败: {e}")

    # 3) 取最近的 DEA/DIF
    ind_df = indicator_calculator.get_indicators_history(code, days=SERIES_BARS + 10)
    if ind_df is None or ind_df.empty or "macd_signal" not in ind_df.columns:
        return {"status": "error", "code": code, "message": "无指标数据"}

    dea_series = ind_df["macd_signal"].dropna()
    dif_series = ind_df["macd"].dropna() if "macd" in ind_df.columns else None
    if len(dea_series) < 2:
        return {"status": "error", "code": code, "message": "指标数据不足"}

    dea_last = dea_series.iloc[-1]
    dea_prev = dea_series.iloc[-2]
    dif_last = dif_series.iloc[-1] if dif_series is not None and len(dif_series) else None

    result = classify(dea_prev, dea_last, dif_last)
    if result is None:
        return {"status": "error", "code": code, "message": "指标数值无效"}

    # 4) 构建迷你全景图序列(OHLC + MACD + 逐日类别)
    series = _build_series(hist_df, ind_df)

    updated = str(ind_df["date"].iloc[-1]) if "date" in ind_df.columns else ""
    return {
        "status": "success",
        "code": code,
        "trend": result["trend"],
        "base_position": result["base_position"],
        "grid": result["grid"],
        "cross": result["cross"],
        "dif": round(float(dif_last), 4) if dif_last is not None else None,
        "dea": round(float(dea_last), 4),
        "dea_prev": round(float(dea_prev), 4),
        "updated": updated,
        "history_status": history_status,
        "history_asof": str(hist_df["date"].max()),
        "series": series,
    }


def get_advice(code: str) -> dict:
    """获取某代码的操盘建议(带缓存)。"""
    code = (code or SHENZHEN_INDEX_CODE).strip()
    ttl = getattr(config, "MACD_ADVICE_CACHE_TTL", 300)
    now = time.time()

    with _advice_lock:
        cached = _advice_cache.get(code)
        if cached and (now - cached["ts"]) < ttl:
            return cached["data"]

    try:
        data = _compute_advice(code)
    except Exception as e:
        logger.error(f"[MACD建议] {code} 计算异常: {e}")
        data = {"status": "error", "code": code, "message": str(e)}

    with _advice_lock:
        _advice_cache[code] = {"data": data, "ts": now}
    return data
