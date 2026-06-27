"""无人值守维护任务：日志轮转与 SQLite 历史数据清理。"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timedelta

import config
from logger import get_logger

logger = get_logger("maintenance")


def rotate_plain_log(log_path: str, max_bytes: int, backup_count: int) -> bool:
    """轮转由 stdout/stderr 追加写入的普通日志文件。"""
    if max_bytes <= 0 or backup_count <= 0 or not os.path.exists(log_path):
        return False
    if os.path.getsize(log_path) < max_bytes:
        return False

    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    oldest = f"{log_path}.{backup_count}"
    if os.path.exists(oldest):
        os.remove(oldest)

    for index in range(backup_count - 1, 0, -1):
        src = f"{log_path}.{index}"
        dst = f"{log_path}.{index + 1}"
        if os.path.exists(src):
            os.replace(src, dst)

    os.replace(log_path, f"{log_path}.1")
    open(log_path, "a", encoding="utf-8").close()
    logger.info(f"已轮转日志: {log_path}")
    return True


def rotate_xqm_log() -> bool:
    """轮转 XtQuantManager 批处理重定向日志。"""
    return rotate_plain_log(
        getattr(config, "XQM_LOG_FILE", os.path.join("logs", "xqm_manager.log")),
        getattr(config, "XQM_LOG_MAX_SIZE", 10 * 1024 * 1024),
        getattr(config, "XQM_LOG_BACKUP_COUNT", 5),
    )


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _delete_older_than(
    conn: sqlite3.Connection,
    table_name: str,
    date_column: str,
    cutoff: datetime,
    extra_where: str = "",
) -> int:
    if not _table_exists(conn, table_name):
        return 0

    cutoff_text = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    where = f"datetime({date_column}) < datetime(?)"
    if extra_where:
        where = f"({where}) AND ({extra_where})"
    cursor = conn.execute(f"DELETE FROM {table_name} WHERE {where}", (cutoff_text,))
    return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0


def cleanup_trading_db(db_path: str | None = None, now: datetime | None = None) -> dict:
    """清理主交易库中的追加型历史表。"""
    db_path = db_path or config.DB_PATH
    now = now or datetime.now()
    summary = {
        "db_path": db_path,
        "trade_records": 0,
        "grid_sessions": 0,
        "premarket_sync_history": 0,
        "config_history": 0,
        "vacuum": False,
    }

    if not db_path or not os.path.exists(db_path):
        return summary

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        summary["trade_records"] = _delete_older_than(
            conn,
            "trade_records",
            "trade_time",
            now - timedelta(days=config.TRADE_RECORD_RETENTION_DAYS),
        )
        summary["premarket_sync_history"] = _delete_older_than(
            conn,
            "premarket_sync_history",
            "sync_time",
            now - timedelta(days=config.PREMARKET_HISTORY_RETENTION_DAYS),
        )
        summary["config_history"] = _delete_older_than(
            conn,
            "config_history",
            "changed_at",
            now - timedelta(days=config.CONFIG_HISTORY_RETENTION_DAYS),
        )

        if _table_exists(conn, "grid_trading_sessions"):
            cutoff = (now - timedelta(days=config.GRID_SESSION_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
            cursor = conn.execute(
                """
                DELETE FROM grid_trading_sessions
                WHERE status != 'active'
                  AND datetime(COALESCE(stop_time, updated_at, created_at)) < datetime(?)
                """,
                (cutoff,),
            )
            summary["grid_sessions"] = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

        conn.commit()
        deleted = sum(v for k, v in summary.items() if k not in ("db_path", "vacuum") and isinstance(v, int))
        if (
            deleted >= getattr(config, "DB_MAINTENANCE_VACUUM_MIN_DELETED_ROWS", 1000)
            and getattr(config, "DB_MAINTENANCE_ENABLE_VACUUM", True)
        ):
            conn.execute("VACUUM")
            summary["vacuum"] = True
    finally:
        conn.close()

    return summary


def cleanup_autobuy_db(db_path: str | None = None, now: datetime | None = None) -> dict:
    """清理自动买入复盘库中的高频决策明细。"""
    from autobuy.store import DEFAULT_DB_PATH

    db_path = db_path or DEFAULT_DB_PATH
    now = now or datetime.now()
    summary = {"db_path": db_path, "decision_log": 0, "vacuum": False}
    if not os.path.exists(db_path):
        return summary

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        summary["decision_log"] = _delete_older_than(
            conn,
            "decision_log",
            "run_time",
            now - timedelta(days=config.AUTOBUY_DECISION_LOG_RETENTION_DAYS),
        )
        conn.commit()
        if (
            summary["decision_log"] >= getattr(config, "DB_MAINTENANCE_VACUUM_MIN_DELETED_ROWS", 1000)
            and getattr(config, "DB_MAINTENANCE_ENABLE_VACUUM", True)
        ):
            conn.execute("VACUUM")
            summary["vacuum"] = True
    finally:
        conn.close()
    return summary


def _summarize_maintenance_result(result: dict) -> tuple[int, bool]:
    deleted_rows = 0
    vacuum_ran = False
    for summary in result.values():
        if not isinstance(summary, dict):
            continue
        vacuum_ran = vacuum_ran or bool(summary.get("vacuum"))
        for key, value in summary.items():
            if key in ("db_path", "vacuum"):
                continue
            if isinstance(value, int):
                deleted_rows += value
    return deleted_rows, vacuum_ran


def run_database_maintenance(now: datetime | None = None) -> dict:
    """执行一次数据库维护。"""
    now = now or datetime.now()
    result = {
        "trading_db": cleanup_trading_db(now=now),
        "autobuy_db": cleanup_autobuy_db(now=now),
    }
    deleted_rows, vacuum_ran = _summarize_maintenance_result(result)
    vacuum_text = "已执行" if vacuum_ran else "未执行"
    logger.info(f"数据库维护完成: 清理 {deleted_rows} 行, VACUUM={vacuum_text}")
    logger.debug(f"数据库维护明细: {result}")
    return result


def schedule_database_maintenance(stop_event=None) -> None:
    """每天在配置时间附近执行一次数据库维护。"""
    if not getattr(config, "ENABLE_DB_MAINTENANCE", True):
        return

    last_run_date = None
    interval = getattr(config, "DB_MAINTENANCE_CHECK_INTERVAL", 600)
    target_time = datetime.strptime(config.DB_MAINTENANCE_TIME, "%H:%M:%S").time()

    while stop_event is None or not stop_event.is_set():
        now = datetime.now()
        should_run = now.time() >= target_time and last_run_date != now.date()
        if (
            should_run
            and (
                not getattr(config, "DB_MAINTENANCE_REQUIRE_NON_TRADE_TIME", True)
                or not config.is_market_hours()
            )
        ):
            try:
                rotate_xqm_log()
                run_database_maintenance(now)
                last_run_date = now.date()
            except Exception as exc:
                logger.error(f"数据库维护任务失败: {exc}", exc_info=True)

        if stop_event is not None:
            stop_event.wait(interval)
        else:
            time.sleep(interval)
