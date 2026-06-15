"""
miniqmt_autobuy 买入条件检查。

复用 data_manager 取行情/历史K线，自算指标(不改 config.MA_PERIODS):
  - 换手率 = 当日成交量(股) / 流通股本
  - 量比   = 当日成交量 / 前5个完整交易日均量
  - 涨幅   = (现价 - 前收) / 前收            (可选)
  - MA8 方向 = ma8[-1] > ma8[-2]
  - 价格相对 MA8 = 现价 / ma8 <= 阈值
  - 涨停/停牌防护 (借 xt.get_instrument_detail，复用 grid 思路)

check() 返回 (passed, reason_dict)，reason 含每项条件实际值与通过判定，供复盘。
"""
from __future__ import annotations

from .config import AutoBuyConfig, get_autobuy_logger

logger = get_autobuy_logger("autobuy.filter")

# 涨跌停价比较容差
_PRICE_EPS = 0.001
MARKET_INDEX_CODES = ("999999", "399001", "399005")
_MARKET_INDEX_XT_CODES = {
    "999999": "999999.SH",
    "399001": "399001.SZ",
    "399005": "399005.SZ",
}
_MARKET_INDEX_ALIASES = {
    # 999999 是通达信口径的上证指数，Baostock 日线接口使用 sh.000001。
    "999999": ("999999.SH", "sh.000001"),
    "399001": ("399001.SZ", "sz.399001"),
    "399005": ("399005.SZ", "sz.399005"),
}


def _first_positive(detail: dict, keys) -> float | None:
    for k in keys:
        v = detail.get(k)
        if v is not None:
            try:
                fv = float(v)
                if fv > 0:
                    return fv
            except (TypeError, ValueError):
                continue
    return None


class BuyConditionFilter:
    def __init__(self, cfg: AutoBuyConfig, data_manager):
        self.cfg = cfg
        self.dm = data_manager

    def _instrument_detail(self, code: str) -> dict:
        """取标的明细(流通股本/涨跌停价)。失败返回 {}。"""
        xt = getattr(self.dm, "xt", None)
        if xt is None or not hasattr(xt, "get_instrument_detail"):
            return {}
        try:
            full_code = code
            adjust = getattr(self.dm, "_adjust_stock", None)
            if callable(adjust):
                full_code = adjust(code)
            detail = xt.get_instrument_detail(full_code)
            return detail if isinstance(detail, dict) else {}
        except Exception as e:
            logger.debug(f"{code} get_instrument_detail 失败: {e}")
            return {}

    def check(self, code: str) -> tuple:
        """对单只股票做全部已启用的买入条件检查。"""
        cfg = self.cfg
        reason: dict = {"code": code, "failed": []}

        # --- 取实时行情 ---
        quote = self.dm.get_latest_data(code)
        if not quote or quote.get("lastPrice", 0) <= 0:
            reason["failed"].append("无有效实时行情")
            return False, reason
        price = float(quote.get("lastPrice", 0))
        last_close = float(quote.get("lastClose", 0) or 0)
        today_volume = float(quote.get("volume", 0) or 0)
        reason["price"] = price

        detail = self._instrument_detail(code)

        # --- 涨停/停牌防护 ---
        if cfg.skip_limit_up:
            up_limit = _first_positive(detail, ("UpStopPrice", "upStopPrice", "HighLimit", "涨停价"))
            if up_limit is not None and price >= up_limit - _PRICE_EPS:
                reason["limit_up"] = {"price": price, "up_limit": up_limit}
                reason["failed"].append("已涨停")
                return False, reason

        # --- 取历史K线 (算 MA8 / 前5日均量) ---
        df = self.dm.download_history_data(code, period="day")
        if df is None or getattr(df, "empty", True) or "close" not in df.columns:
            reason["failed"].append("无有效历史K线")
            return False, reason

        # --- 换手率 ---
        if cfg.enable_turnover_rate:
            float_vol = _first_positive(detail, ("FloatVolume", "FloatVol", "流通股本", "流通股", "FloatA"))
            if float_vol and today_volume > 0:
                turnover = (today_volume * cfg.volume_unit_multiplier) / float_vol
                reason["turnover_rate"] = round(turnover, 4)
                if turnover < cfg.min_turnover_rate:
                    reason["failed"].append(f"换手率{turnover:.2%}<{cfg.min_turnover_rate:.2%}")
            else:
                reason["turnover_rate"] = None
                reason["failed"].append("换手率无法计算(缺流通股本或成交量)")

        # --- 量比 (当日量 / 前5个完整交易日均量；盘中为累计量比，未按时间折算) ---
        if cfg.enable_volume_ratio:
            if "volume" in df.columns and len(df) >= 6 and today_volume > 0:
                avg5 = float(df["volume"].iloc[-6:-1].mean())
                if avg5 > 0:
                    vol_ratio = today_volume / avg5
                    reason["volume_ratio"] = round(vol_ratio, 3)
                    if vol_ratio < cfg.min_volume_ratio:
                        reason["failed"].append(f"量比{vol_ratio:.2f}<{cfg.min_volume_ratio}")
                else:
                    reason["volume_ratio"] = None
                    reason["failed"].append("量比无法计算(前5日均量为0)")
            else:
                reason["volume_ratio"] = None
                reason["failed"].append("量比无法计算(历史数据不足)")

        # --- 涨幅 (可选) ---
        if cfg.enable_pct_change:
            if last_close > 0:
                pct = (price - last_close) / last_close
                reason["pct_change"] = round(pct, 4)
                if pct < cfg.min_pct_change:
                    reason["failed"].append(f"涨幅{pct:.2%}<{cfg.min_pct_change:.2%}")
            else:
                reason["pct_change"] = None
                reason["failed"].append("涨幅无法计算(无前收价)")

        # --- MA8 方向 / 价格相对 MA8 ---
        if cfg.enable_ma8_uptrend or cfg.enable_price_below_ma8_ratio:
            ma = df["close"].rolling(8).mean()
            if len(ma) >= 9 and ma.iloc[-1] == ma.iloc[-1] and ma.iloc[-2] == ma.iloc[-2]:  # 非NaN
                ma8_now = float(ma.iloc[-1])
                ma8_prev = float(ma.iloc[-2])
                reason["ma8"] = round(ma8_now, 3)
                if cfg.enable_ma8_uptrend:
                    reason["ma8_uptrend"] = ma8_now > ma8_prev
                    if not (ma8_now > ma8_prev):
                        reason["failed"].append("MA8方向向下")
                if cfg.enable_price_below_ma8_ratio and ma8_now > 0:
                    ratio = price / ma8_now
                    reason["price_to_ma8"] = round(ratio, 4)
                    if ratio > cfg.max_price_to_ma8_ratio:
                        reason["failed"].append(
                            f"现价/MA8={ratio:.3f}>{cfg.max_price_to_ma8_ratio}"
                        )
            else:
                reason["ma8"] = None
                reason["failed"].append("MA8无法计算(历史数据不足)")

        passed = len(reason["failed"]) == 0
        return passed, reason


class MarketIndexFilter:
    """大盘指数门禁: 三个指数中至少一个 MA5 向上才允许自动买入。"""

    def __init__(self, data_manager, index_codes=MARKET_INDEX_CODES):
        self.dm = data_manager
        self.index_codes = tuple(index_codes)

    def check(self) -> tuple:
        """返回 (是否通过, 详情)。

        MA5 向上定义为最近一个 MA5 大于前一个 MA5。任一指数满足即通过。
        """
        details = {}
        failed = []
        for raw_code in self.index_codes:
            aliases = _MARKET_INDEX_ALIASES.get(raw_code, (_MARKET_INDEX_XT_CODES.get(raw_code, raw_code),))
            df = None
            used_code = aliases[0]
            errors = []
            for code in aliases:
                used_code = code
                try:
                    df = self.dm.download_history_data(code, period="day")
                except Exception as e:
                    errors.append(f"{code}: {e}")
                    logger.warning(f"指数 {raw_code}({code}) 历史数据异常: {e}")
                    continue
                if df is not None and not getattr(df, "empty", True) and "close" in df.columns and len(df) >= 6:
                    break
                errors.append(f"{code}: 历史数据不足")

            if df is None or getattr(df, "empty", True) or "close" not in df.columns or len(df) < 6:
                details[raw_code] = {"code": used_code, "aliases": aliases, "error": "; ".join(errors)}
                failed.append(f"{raw_code}历史数据不足")
                continue

            close = df["close"].astype(float)
            ma5 = close.rolling(5).mean()
            if len(ma5) < 6 or ma5.iloc[-1] != ma5.iloc[-1] or ma5.iloc[-2] != ma5.iloc[-2]:
                details[raw_code] = {"code": used_code, "aliases": aliases, "error": "MA5无法计算"}
                failed.append(f"{raw_code} MA5无法计算")
                continue

            ma5_now = float(ma5.iloc[-1])
            ma5_prev = float(ma5.iloc[-2])
            ma5_up = ma5_now > ma5_prev
            details[raw_code] = {
                "code": used_code,
                "aliases": aliases,
                "ma5": round(ma5_now, 4),
                "ma5_prev": round(ma5_prev, 4),
                "ma5_up": ma5_up,
            }
            if ma5_up:
                return True, {
                    "passed": True,
                    "passed_index": raw_code,
                    "details": details,
                }
            failed.append(f"{raw_code} MA5未向上")

        return False, {
            "passed": False,
            "failed": failed,
            "details": details,
        }
