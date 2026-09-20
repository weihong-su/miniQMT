"""
miniqmt_autobuy 自有存储 (data/autobuy.db)。

两张表:
  buy_history  — 每次买入尝试记录，用于防重(dedup_window_days)与资金/成交复盘
  decision_log — 每轮每只的条件检查明细，复盘"为什么买/没买"

代码格式约定: buy_history.stock_code 按写入方给定的格式原样存储(通常为
'600000.SH')，防重查询 recently_bought_codes() 统一用 normalize_code() 归一
成 6 位数字后返回，与 app._dedup_filter 的比较口径保持一致。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta

from .pool import normalize_code

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(MODULE_DIR)
DEFAULT_DB_PATH = os.path.join(PROJECT_ROOT, "data", "autobuy.db")


class AutoBuyStore:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS buy_history (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    stock_code   TEXT NOT NULL,
                    buy_time     TEXT NOT NULL,
                    run_trigger  TEXT,
                    success      INTEGER NOT NULL DEFAULT 0,
                    http_status  INTEGER,
                    order_result TEXT,
                    amount       REAL
                );
                CREATE INDEX IF NOT EXISTS idx_buy_history_code_time
                    ON buy_history (stock_code, buy_time);

                CREATE TABLE IF NOT EXISTS decision_log (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_time    TEXT NOT NULL,
                    stock_code  TEXT NOT NULL,
                    passed      INTEGER NOT NULL,
                    reason_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_decision_log_time
                    ON decision_log (run_time);
                """
            )
            # 幂等补列: 老库无 is_simulation 列，补 0(实盘)以兼容存量行
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(buy_history)")}
            if "is_simulation" not in cols:
                self.conn.execute(
                    "ALTER TABLE buy_history ADD COLUMN is_simulation INTEGER NOT NULL DEFAULT 0"
                )
            self.conn.commit()

    # ---- 写入 ----
    def record_decision(self, run_time: str, stock_code: str, passed: bool, reason: dict) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO decision_log (run_time, stock_code, passed, reason_json) VALUES (?, ?, ?, ?)",
                (run_time, stock_code, 1 if passed else 0, json.dumps(reason, ensure_ascii=False)),
            )
            self.conn.commit()

    def record_buy(self, stock_code: str, run_trigger: str, success: bool,
                   http_status=None, order_result=None, amount=None,
                   is_simulation: bool = False) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO buy_history "
                "(stock_code, buy_time, run_trigger, success, http_status, order_result, amount, is_simulation) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stock_code,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    run_trigger,
                    1 if success else 0,
                    http_status,
                    json.dumps(order_result, ensure_ascii=False) if order_result is not None else None,
                    amount,
                    1 if is_simulation else 0,
                ),
            )
            self.conn.commit()

    # ---- 防重查询 ----
    def recently_bought_codes(self, window_days: int) -> set:
        """返回防重窗口内已成功买入的股票代码集合(规范化为 6 位数字)。

        window_days: -1=永久(全部历史), 0=仅当天, N=最近 N 天(含今天)

        仅统计实盘买入(is_simulation=0): 模拟运行不产生真实持仓，若计入防重会
        让试跑污染实盘风控、挡住当天真实买入。
        返回值统一经 normalize_code 归一，与 app._dedup_filter 的比较口径一致
        (历史 bug: 库里存 '600000.SH' 而比较用 '600000'，防重恒不命中)。
        """
        with self._lock:
            if window_days < 0:
                rows = self.conn.execute(
                    "SELECT DISTINCT stock_code FROM buy_history "
                    "WHERE success = 1 AND is_simulation = 0"
                ).fetchall()
            else:
                # window_days=0 → 今天 00:00 起; N → (今天-N) 00:00 起
                start = (datetime.now() - timedelta(days=window_days)).strftime("%Y-%m-%d 00:00:00")
                rows = self.conn.execute(
                    "SELECT DISTINCT stock_code FROM buy_history "
                    "WHERE success = 1 AND is_simulation = 0 AND buy_time >= ?",
                    (start,),
                ).fetchall()
            return {normalize_code(r["stock_code"]) for r in rows}

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
            except Exception:
                pass
