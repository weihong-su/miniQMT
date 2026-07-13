"""Big QMT position and asset adapters."""

from ..code_utils import normalize_stock_code
from ..models import AssetSnapshot, PositionSnapshot


def _attr(obj, names, default=None):
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return default


def _full_code(instrument_id, exchange_id):
    code = str(instrument_id or "").strip().upper()
    market = str(exchange_id or "").strip().upper()
    if "." in code:
        return normalize_stock_code(code)
    if market in ("SH", "SZ"):
        return normalize_stock_code("%s.%s" % (code, market))
    return normalize_stock_code(code)


class BigQmtPositionProvider:
    def __init__(self, get_trade_detail_data_func, account_type="STOCK"):
        self.get_trade_detail_data = get_trade_detail_data_func
        self.account_type = account_type

    def _require_query_func(self):
        if self.get_trade_detail_data is None:
            raise RuntimeError("get_trade_detail_data is not available in Big QMT runtime")
        return self.get_trade_detail_data

    def get_positions(self, account_id):
        query = self._require_query_func()
        # QMT's get_trade_detail_data can raise on POSITION queries in some
        # states (e.g. context not bound). Degrade to empty like get_asset does.
        try:
            rows = query(account_id, self.account_type, "POSITION") or []
        except Exception:
            return {}
        positions = {}
        for row in rows:
            code = _full_code(
                _attr(row, ("m_strInstrumentID", "instrument_id", "stock_code")),
                _attr(row, ("m_strExchangeID", "exchange_id", "market")),
            )
            positions[code] = PositionSnapshot(
                stock_code=code,
                volume=int(_attr(row, ("m_nVolume", "volume"), 0) or 0),
                available=int(_attr(row, ("m_nCanUseVolume", "available", "can_use_volume"), 0) or 0),
                cost=float(_attr(row, ("m_dOpenPrice", "m_dCostPrice", "cost"), 0.0) or 0.0),
                stock_name=str(_attr(row, ("m_strInstrumentName", "stock_name"), "") or ""),
            )
        return positions

    def get_asset(self, account_id):
        query = self._require_query_func()
        rows = []
        for detail_type in ("ACCOUNT", "ASSET"):
            try:
                rows = query(account_id, self.account_type, detail_type) or []
                if rows:
                    break
            except Exception:
                rows = []
        if not rows:
            return AssetSnapshot(account_id=account_id, cash=None, total_asset=None)

        row = rows[0]
        cash = _attr(row, ("m_dAvailable", "m_dAvailableCash", "available_cash", "cash"))
        total_asset = _attr(row, ("m_dBalance", "m_dAsset", "total_asset", "asset"))
        return AssetSnapshot(
            account_id=account_id,
            cash=float(cash) if cash is not None else None,
            total_asset=float(total_asset) if total_asset is not None else None,
        )
