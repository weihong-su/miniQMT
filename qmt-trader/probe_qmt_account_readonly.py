import json
import os
import threading
from datetime import datetime

IPC_ROOT = os.environ.get("QMT_IPC_ROOT", r"C:\QuantIPC")
_done = False
_log_path = None


def emit(message):
    line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), message)
    print(line)
    with open(_log_path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def find_reader(context):
    reader = globals().get("get_trade_detail_data")
    if callable(reader):
        return reader
    builtins_obj = __builtins__
    reader = (builtins_obj.get("get_trade_detail_data") if isinstance(builtins_obj, dict)
              else getattr(builtins_obj, "get_trade_detail_data", None))
    if callable(reader):
        return reader
    return getattr(context, "get_trade_detail_data", None)


def accounts():
    result = []
    for name in sorted(os.listdir(IPC_ROOT)):
        path = os.path.join(IPC_ROOT, name, "config.json")
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8-sig") as handle:
            config = json.load(handle)
        result.append((str(config.get("account_id") or name),
                       str(config.get("account_type") or "STOCK")))
    return result


def inspect_call(reader, args, kind):
    try:
        rows = reader(*args)
        if rows is None:
            emit("CALL %r -> None" % (args,))
            return
        emit("CALL %r -> type=%s count=%s" % (args, type(rows).__name__, len(rows)))
        names = (("m_dTotalAsset", "m_dBalance", "m_dAvailable", "m_dFrozen", "m_dMarketValue")
                 if kind == "ACCOUNT" else
                 ("m_strAccountID", "m_strInstrumentID", "m_nVolume", "m_nCanUseVolume",
                  "m_dOpenPrice", "m_dLastPrice", "m_dMarketValue"))
        for index, row in enumerate(rows):
            fields = {name: getattr(row, name) for name in names if hasattr(row, name)}
            emit("ROW %s %s" % (index, json.dumps(fields, ensure_ascii=True, default=str)))
    except Exception as error:
        emit("CALL %r -> ERROR %s: %s" % (args, type(error).__name__, error))


def run_probe(context):
    global _done, _log_path
    if _done:
        return
    _done = True
    directory = os.path.join(IPC_ROOT, "qmt_log")
    os.makedirs(directory, exist_ok=True)
    _log_path = os.path.join(directory, "account_probe_%s_%s.log" % (
        datetime.now().strftime("%Y%m%d_%H%M%S_%f"), os.getpid()))
    emit("ACCOUNT READ PROBE thread=%s source=%s" % (
        threading.current_thread().name, globals().get("__file__", "QMT editor")))
    reader = find_reader(context)
    emit("NATIVE_READER_CALLABLE=%s" % callable(reader))
    if not callable(reader):
        emit("STOP: native reader unavailable in this strategy context")
        return
    for account_id, account_type in accounts():
        emit("ACCOUNT=%s TYPE=%s" % (account_id, account_type))
        for kind in ("ACCOUNT", "POSITION"):
            for args in ((account_id, account_type.upper(), kind),
                         (account_id, account_type.lower(), kind.lower()),
                         (account_id, kind)):
                inspect_call(reader, args, kind)
    emit("PROBE_FINISHED log=%s" % _log_path)


def init(ContextInfo):
    run_probe(ContextInfo)


def handlebar(ContextInfo):
    run_probe(ContextInfo)
