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


def _diagnostic_time(value):
    """无效数值用文字留痕，避免诊断输出含非标准 JSON 数值。"""
    return str(value) if isinstance(value, float) and not math.isfinite(value) else value


def heartbeat_is_fresh(path, max_age):
    """两端统一允许配置内的时钟偏差，超出容差仍视为不可用。"""
    try:
        age = time.time() - os.path.getmtime(path)
        return (math.isfinite(age) and
                -config.QMT_IPC_TIMESTAMP_FUTURE_TOLERANCE_SECONDS <= age <= max_age)
    except OSError:
        return False


def _read_snapshot_json(path):
    """只重试瞬态共享/访问冲突；格式错误和账号错误不重试。"""
    for attempt in range(config.QMT_IPC_READ_RETRIES + 1):
        try:
            with open(path, encoding="utf-8-sig") as handle:
                return json.load(handle)
        except OSError as exc:
            transient = isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in (32, 33)
            if not transient or attempt >= config.QMT_IPC_READ_RETRIES:
                raise
            time.sleep(config.QMT_IPC_READ_RETRY_DELAY_SECONDS)


def read_snapshot(root, account, name, max_age, value=None):
    try:
        if value is None:
            value = _read_snapshot_json(os.path.join(root, "status", name))
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
        self._quote_diagnostics = threading.local()

    def connect(self):
        path = os.path.join(self.root, "status", "heartbeat.json")
        return heartbeat_is_fresh(path, config.QMT_IPC_HEARTBEAT_MAX_AGE)

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

    def get_quote_status(self, stock_code):
        """返回当前线程最近一次报价校验结果，避免并发查询覆盖拒绝原因。"""
        code = self._code(stock_code)
        return dict(getattr(self._quote_diagnostics, "values", {}).get(code, {}))

    def get_full_tick(self, stock_list):
        for code in stock_list:
            self.subscribe_quote(code)
        return self._read_ticks(stock_list)

    def _read_ticks(self, stock_list, snapshot=None):
        """只读校验供行情与就绪诊断共用，诊断不会修改订阅。"""
        statuses = {}
        self._quote_diagnostics.values = statuses
        try:
            if snapshot is None:
                snapshot = self._snapshot()
        except IpcSnapshotUnavailable as exc:
            for code in stock_list:
                statuses[self._code(code)] = dict(reason="snapshot_unavailable",
                    message="IPC 行情快照不可用", detail=str(exc))
            return {}
        result = {}
        for code in stock_list:
            canonical = self._code(code)
            quote = snapshot.get("quotes", {}).get(canonical)
            status = dict(exported_at=snapshot.get("timestamp"),
                          max_age_seconds=config.QMT_IPC_QUOTE_MAX_AGE_SECONDS)
            statuses[canonical] = status
            if not isinstance(quote, dict):
                status.update(reason="quote_missing", message="快照中缺少该股票报价")
                continue
            try:
                price = float(quote.get("lastPrice") or 0)
                if not math.isfinite(price) or price <= 0:
                    status.update(reason="invalid_price", message="报价价格无效")
                    continue
                source_time = quote.get("time") or quote.get("timetag")
                status["quote_asof"] = _diagnostic_time(source_time)
                stamp = source_timestamp(source_time)
                age = time.time() - stamp
                if not math.isfinite(age):
                    raise ValueError("报价来源时间不是有限数值")
                status["quote_age_seconds"] = round(age, 6)
            except (TypeError, ValueError, OverflowError):
                status.update(reason="invalid_quote", message="报价价格或来源时间格式无效")
                continue
            tradable = (-config.QMT_IPC_TIMESTAMP_FUTURE_TOLERANCE_SECONDS <= age <=
                        config.QMT_IPC_QUOTE_MAX_AGE_SECONDS)
            if config.is_continuous_trade_time() and not tradable:
                status.update(reason="quote_expired" if age > 0 else "quote_future",
                              message="报价来源时间过期" if age > 0 else "报价来源时间超前")
                continue
            status.update(reason="success" if tradable else "reference_quote",
                          message="报价有效" if tradable else "仅可作为参考行情")
            result[canonical] = dict(quote, _tradable=tradable)
        return result

    def get_readiness(self, stock_list=None):
        """分别核对心跳、账户和行情；不修改文件或交易开关。"""
        stocks = [self._code(code) for code in
                  (getattr(config, "STOCK_POOL", []) if stock_list is None else stock_list)]
        result = {}
        snapshots = {}
        sources = (("heartbeat", "heartbeat.json", "ts", config.QMT_IPC_HEARTBEAT_MAX_AGE),
                   ("account", "account.json", "timestamp", config.QMT_IPC_SNAPSHOT_MAX_AGE_SECONDS),
                   ("quotes", "quotes.json", "timestamp", config.QMT_IPC_QUOTE_MAX_AGE_SECONDS))
        for key, name, timestamp_key, limit in sources:
            value, age = {}, None
            try:
                value = _read_snapshot_json(os.path.join(self.root, "status", name))
                if not isinstance(value, dict) or str(value.get("account_id")) != str(self.account):
                    raise ValueError("快照账号不符")
                age = time.time() - source_timestamp(value.get(timestamp_key))
                if key != "heartbeat":
                    read_snapshot(self.root, self.account, name, limit, value=value)
                    snapshots[key] = value
                valid = math.isfinite(age) and -config.QMT_IPC_TIMESTAMP_FUTURE_TOLERANCE_SECONDS <= age <= limit
                result[key] = dict(available=valid, asof=_diagnostic_time(value.get(timestamp_key)),
                                   age_seconds=round(age, 6) if math.isfinite(age) else None, max_age_seconds=limit,
                                   error=None if valid else "来源时间已过期或超前")
            except (OSError, ValueError, TypeError, IpcSnapshotUnavailable) as exc:
                result[key] = dict(available=False,
                                   asof=_diagnostic_time(value.get(timestamp_key)) if isinstance(value, dict) else None,
                                   age_seconds=round(age, 6) if age is not None and math.isfinite(age) else None,
                                   max_age_seconds=limit, error=str(exc))

        ticks = self._read_ticks(stocks, snapshot=snapshots.get("quotes", {}))
        result["quotes"]["stocks"] = {code: self.get_quote_status(code) for code in stocks}
        valid_stocks = all(code in ticks for code in stocks)
        tradable = bool(stocks) and all(ticks.get(code, {}).get("_tradable") is True for code in stocks)
        sources_ok = all(result[key]["available"] for key in ("heartbeat", "account", "quotes"))
        result["account"]["data_status"] = ("fresh" if result["account"]["available"] else
            "stale" if result["account"].get("asof") else "unavailable")
        result["quotes"]["data_status"] = ("realtime" if tradable and config.is_continuous_trade_time()
            else "reference" if sources_ok and not config.is_continuous_trade_time() else "unavailable")
        result["ready"] = sources_ok and (valid_stocks or not config.is_continuous_trade_time())
        result["trading_ready"] = sources_ok and tradable and config.is_continuous_trade_time()
        try:
            with open(os.path.join(self.root, "status", "producer.json"), encoding="utf-8-sig") as handle:
                producer = json.load(handle)
            result["producer"] = producer if isinstance(producer, dict) and str(producer.get("account_id")) == str(self.account) else None
        except (OSError, ValueError, TypeError):
            result["producer"] = None
        return result

    def get_instrument_detail(self, stock_code):
        try:
            return dict(self._snapshot().get("instruments", {}).get(self._code(stock_code), {}))
        except IpcSnapshotUnavailable:
            return {}

    def get_stock_name(self, stock_code):
        return self.get_instrument_detail(stock_code).get("InstrumentName", "")
