"""MACD 操盘建议(悬浮窗)。

依据日线 MACD 的 DEA 方向与 DEA 相对 0 轴位置，给出"底仓 / 网格"参考建议。
纯只读展示，不触发任何交易动作。

决策矩阵(以最新一根日线 DEA 为准)::

    DEA 方向   DEA 位置    趋势判定             底仓      网格
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

    dea_up = dea_last >= dea_prev          # DEA 方向：向上
    dea_above_zero = dea_last > 0          # DEA 位置：0 轴以上

    if dea_up and dea_above_zero:
        trend, base, grid = "上升趋势(强)", "重仓", "启动"
    elif dea_up and not dea_above_zero:
        trend, base, grid = "上升趋势(弱/修复)", "半仓以下", "启动"
    elif not dea_up and dea_above_zero:
        trend, base, grid = "下降趋势(弱)/顶部反转", "半仓以下", "启动"
    else:
        trend, base, grid = "下降趋势(强)", "清仓", "停用"

    # 金叉/死叉补充说明(DIF 相对 DEA)
    cross = ""
    if dif_last is not None:
        try:
            cross = "DIF在DEA上方(多头)" if float(dif_last) >= dea_last else "DIF在DEA下方(空头)"
        except (TypeError, ValueError):
            cross = ""

    return {"trend": trend, "base_position": base, "grid": grid, "cross": cross}


def _compute_advice(code: str) -> dict:
    """拉取历史数据、计算 MACD 并给出建议(不含缓存)。"""
    from data_manager import get_data_manager
    from indicator_calculator import get_indicator_calculator

    data_manager = get_data_manager()
    indicator_calculator = get_indicator_calculator()

    # 1) 确保历史日线已入库
    try:
        if _is_index_code(code):
            from autobuy.filter import download_market_index_history
            df = download_market_index_history(data_manager, code)
            if df is not None and not getattr(df, "empty", True):
                data_manager.save_history_data(code, df)
        else:
            data_manager.update_stock_data(code)
    except Exception as e:
        logger.warning(f"[MACD建议] {code} 历史数据准备失败: {e}")

    # 2) 计算并落库指标
    try:
        indicator_calculator.calculate_all_indicators(code)
    except Exception as e:
        logger.warning(f"[MACD建议] {code} 指标计算失败: {e}")

    # 3) 取最近的 DEA/DIF
    ind_df = indicator_calculator.get_indicators_history(code, days=5)
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
