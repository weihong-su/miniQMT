"""只用已采集快照解释资产估值差额，保留柜台原始数据。"""
from datetime import datetime
import math
import config


def _time(value):
    """兼容行情源毫秒时间、时间标签及持仓 ISO 时间。"""
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value / 1000 if value > 100000000000 else value)
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).replace(tzinfo=None)
        except ValueError:
            try:
                return datetime.strptime(value, '%Y%m%d %H:%M:%S')
            except ValueError:
                return None
    return None


def build_account_valuation(account, positions, quotes, *, now=None, is_trade_time=None):
    """返回行情估值及两种恒等式差额；缺少可靠证据时不猜测。"""
    now = now or datetime.now()
    if is_trade_time is None:
        is_trade_time = config.is_trade_time()
    result = dict(status='unavailable', reason='资产或持仓行情证据不完整',
                  broker_market_value=account.get('market_value'), quote_market_value=None,
                  broker_identity_diff=None, quote_identity_diff=None,
                  asset_asof=account.get('timestamp'), position_asof=None, quote_asof=None)
    try:
        total, cash, frozen, broker = [float(account[key]) for key in
                                      ('total_asset', 'available', 'frozen_cash', 'market_value')]
        if not all(math.isfinite(value) for value in (total, cash, frozen, broker)) or total <= 0:
            return result
        result['broker_identity_diff'] = round(total - cash - frozen - broker, 4)
        asset_time = _time(account.get('timestamp'))
        max_age = config.ASSET_VALUATION_MAX_AGE_SECONDS
        if asset_time is None or (is_trade_time and abs((now - asset_time).total_seconds()) > max_age):
            result['reason'] = '资产快照时间缺失或过期'
            return result
        value, position_times, quote_times = 0.0, [], []
        for position in positions:
            volume = float(position['volume'])
            if not math.isfinite(volume) or volume < 0 or not volume.is_integer():
                return result
            if volume == 0:
                continue
            code = str(position['stock_code']).split('.')[0]
            quote = quotes.get(code) or quotes.get(str(position['stock_code']))
            if not isinstance(quote, dict):
                result['reason'] = '持仓行情覆盖不完整'
                return result
            price = float(quote.get('lastPrice') or 0)
            quote_time = _time(quote.get('time') or quote.get('timetag'))
            position_time = _time(position.get('position_asof'))
            if not math.isfinite(price) or price <= 0 or quote_time is None or position_time is None:
                result['reason'] = '行情价格或来源时间无效'
                return result
            if is_trade_time and any(abs((now - stamp).total_seconds()) > max_age
                                     for stamp in (quote_time, position_time)):
                result['reason'] = '持仓或行情快照已过期'
                return result
            value += volume * price
            position_times.append(position_time)
            quote_times.append(quote_time)
        diff = round(total - cash - frozen - value, 4)
        tolerance = config.SETTLEMENT_ASSET_IDENTITY_TOLERANCE
        status = 'consistent' if abs(result['broker_identity_diff']) <= tolerance else 'explained'
        if abs(diff) > tolerance:
            status = 'unexplained'
        if not is_trade_time:
            status = 'static'
        result.update(
            status=status, reason='静态行情估值' if status == 'static' else
            '行情估值解释柜台差额' if status == 'explained' else
            '行情估值后仍有差额' if status == 'unexplained' else '两种估值一致',
            quote_market_value=round(value, 4), quote_identity_diff=diff,
            position_asof=min(position_times).isoformat() if position_times else account.get('timestamp'),
            quote_asof=min(quote_times).isoformat() if quote_times else account.get('timestamp'))
    except (KeyError, TypeError, ValueError, OverflowError):
        pass
    return result
