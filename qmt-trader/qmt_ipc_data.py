"""IPC 行情读取及账户快照有效性校验，不连接本地 xtquant 服务。"""
import json
import math
import os
import threading
import time
from datetime import datetime
import config


_subscriptions_lock = threading.Lock()


class IpcSnapshotUnavailable(RuntimeError):
    """文件缺失、过期或账号不符，不能解释为账户空仓。"""


def source_timestamp(value):
    if isinstance(value, (int, float)):
        return float(value) / 1000 if value > 100000000000 else float(value)
    text = str(value or "").strip()
    if text.isdigit() and len(text) in (10, 13):
        return source_timestamp(int(text))
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        for pattern in ("%Y%m%d %H:%M:%S", "%Y%m%d%H%M%S"):
            try:
                return datetime.strptime(text, pattern).timestamp()
            except ValueError:
                pass
    raise ValueError("来源时间无效")


def read_snapshot(root, account, name, max_age):
    try:
        with open(os.path.join(root, "status", name), encoding="utf-8-sig") as handle:
            value = json.load(handle)
        if not isinstance(value, dict) or str(value.get("account_id")) != str(account):
            raise ValueError("快照账号不符")
        age = time.time() - source_timestamp(value.get("timestamp"))
        if not math.isfinite(age) or not -config.QMT_IPC_TIMESTAMP_FUTURE_TOLERANCE_SECONDS <= age <= max_age:
            raise ValueError("快照来源时间已过期或超前")
        if name == "account.json":
            if not isinstance(value.get("positions"), list):
                raise ValueError("持仓列表格式无效")
            for field in ("available", "frozen", "market_value", "total_asset"):
                number = float(value.get(field, 0))
                if not math.isfinite(number) or number < 0:
                    raise ValueError("账户金额字段无效")
        elif not isinstance(value.get("quotes"), dict):
            raise ValueError("行情列表格式无效")
        return value
    except (OSError, ValueError, TypeError) as exc:
        raise IpcSnapshotUnavailable("%s 不可用: %s" % (name, exc)) from exc


class QmtIpcDataAdapter:
    """通过大 QMT 导出的报价文件提供行情接口。"""
    def __init__(self, account=None):
        self.account = account or config.get_account_config().get("account_id", "")
        self.root = os.path.join(config.QMT_IPC_ROOT, str(self.account))
        self._lock = _subscriptions_lock

    def connect(self):
        path = os.path.join(self.root, "status", "heartbeat.json")
        try:
            return 0 <= time.time() - os.path.getmtime(path) < config.QMT_IPC_HEARTBEAT_MAX_AGE
        except OSError:
            return False

    reconnect = connect

    def subscribe_quote(self, stock_code, **kwargs):
        code = self._code(stock_code)
        with self._lock:
            directory = os.path.join(self.root, "status")
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, "subscriptions.json")
            try:
                with open(path, encoding="utf-8-sig") as handle:
                    stocks = set(json.load(handle).get("stocks", []))
            except (OSError, ValueError, TypeError):
                stocks = set()
            if code not in stocks:
                stocks.add(code)
                temporary = path + ".%s.%s.tmp" % (os.getpid(), threading.get_ident())
                with open(temporary, "w", encoding="utf-8") as handle:
                    json.dump({"account_id": self.account, "stocks": sorted(stocks)}, handle)
                os.replace(temporary, path)
        return 1

    @staticmethod
    def _code(value):
        from Methods import add_xt_suffix
        return add_xt_suffix(str(value).upper())

    def _snapshot(self):
        return read_snapshot(self.root, self.account, "quotes.json",
                             config.QMT_IPC_QUOTE_MAX_AGE_SECONDS)

    def get_full_tick(self, stock_list):
        for code in stock_list:
            self.subscribe_quote(code)
        try:
            snapshot = self._snapshot()
        except IpcSnapshotUnavailable:
            return {}
        result = {}
        for code in stock_list:
            canonical = self._code(code)
            quote = snapshot.get("quotes", {}).get(canonical)
            if not isinstance(quote, dict):
                continue
            try:
                price = float(quote.get("lastPrice") or 0)
                stamp = source_timestamp(quote.get("time") or quote.get("timetag"))
                age = time.time() - stamp
                if not math.isfinite(price) or price <= 0:
                    continue
                if config.is_continuous_trade_time() and not (
                        -config.QMT_IPC_TIMESTAMP_FUTURE_TOLERANCE_SECONDS <= age <= config.QMT_IPC_QUOTE_MAX_AGE_SECONDS):
                    continue
            except (TypeError, ValueError, OverflowError):
                continue
            result[canonical] = dict(quote, _tradable=(
                -config.QMT_IPC_TIMESTAMP_FUTURE_TOLERANCE_SECONDS <= age <= config.QMT_IPC_QUOTE_MAX_AGE_SECONDS))
        return result

    def get_instrument_detail(self, stock_code):
        try:
            return dict(self._snapshot().get("instruments", {}).get(self._code(stock_code), {}))
        except IpcSnapshotUnavailable:
            return {}

    def get_stock_name(self, stock_code):
        return self.get_instrument_detail(stock_code).get("InstrumentName", "")
