"""无人值守维护任务：日志轮转与 SQLite 历史数据清理。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import time
from contextlib import closing
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


def _delete_expired_batches(conn, table, column, cutoff, extra_where=""):
    """新增历史清理使用短事务，保留截止时间相等的记录。"""
    if not _table_exists(conn, table):
        return 0
    if config.DB_RETENTION_BATCH_SIZE < 1:
        raise ValueError("DB_RETENTION_BATCH_SIZE 必须大于零")
    total = 0
    condition = "datetime(%s) < datetime(?)" % column
    if extra_where:
        condition += " AND (%s)" % extra_where
    while True:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT rowid FROM %s WHERE %s LIMIT ?" % (table, condition),
                (cutoff.strftime("%Y-%m-%d %H:%M:%S"), config.DB_RETENTION_BATCH_SIZE),
            ).fetchall()
            conn.executemany("DELETE FROM %s WHERE rowid=?" % table, rows)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        total += len(rows)
        if len(rows) < config.DB_RETENTION_BATCH_SIZE:
            return total


def _archive_run_events(conn, db_path, now):
    """事件归档先持久化并逐条核对，再删除在线数据，重试保持幂等。"""
    from runtime_retention import checked_path

    if not _table_exists(conn, "run_events"):
        return 0
    if min(config.DB_RETENTION_BATCH_SIZE, config.RUN_EVENT_RETENTION_DAYS,
           config.RUN_EVENT_ARCHIVE_RETENTION_DAYS) < 1:
        raise ValueError("运行事件保留参数必须大于零")
    parent = Path(db_path).absolute().parent
    directory = checked_path(parent / "archive" / "run_events", parent)
    cutoff = (now - timedelta(days=config.RUN_EVENT_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    protect = "COALESCE(event_type,'') LIKE 'asset_%'"
    if _table_exists(conn, "account_equity_daily"):
        columns = {row[1] for row in conn.execute("PRAGMA table_info(account_equity_daily)")}
        if {"account", "validation_status"} <= columns:
            protect += """ AND EXISTS (
                SELECT 1 FROM account_equity_daily e
                WHERE COALESCE(e.account,'')=COALESCE(run_events.account,'')
                  AND COALESCE(e.validation_status,'unverified') IN ('pending_review','unverified')
            )"""
    condition = "datetime(event_time) < datetime(?) AND NOT (%s)" % protect
    total = 0
    while True:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                "SELECT * FROM run_events WHERE %s ORDER BY id LIMIT ?" % condition,
                (cutoff, config.DB_RETENTION_BATCH_SIZE),
            )
            columns = [item[0] for item in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
            if not rows:
                conn.commit()
                break
            directory.mkdir(parents=True, exist_ok=True)
            by_month = {}
            for row in rows:
                month = datetime.fromisoformat(row["event_time"]).strftime("%Y-%m")
                by_month.setdefault(month, []).append(row)
            for month, events in by_month.items():
                archive_path = checked_path(directory / ("%s.run_events_%s.sqlite" % (Path(db_path).name, month)), parent)
                with closing(sqlite3.connect(str(archive_path), timeout=config.RUNTIME_DB_TIMEOUT_SECONDS)) as archive:
                    archive.execute("PRAGMA synchronous=FULL")
                    archive.execute("CREATE TABLE IF NOT EXISTS run_events (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
                    for event in events:
                        payload = json.dumps(event, ensure_ascii=False, sort_keys=True)
                        archive.execute("INSERT OR IGNORE INTO run_events VALUES (?,?)", (event["id"], payload))
                        actual = archive.execute("SELECT payload FROM run_events WHERE id=?", (event["id"],)).fetchone()
                        if actual != (payload,):
                            raise ValueError("事件归档内容不一致，保留在线记录: %s" % event["id"])
                    archive.commit()
            conn.executemany("DELETE FROM run_events WHERE id=?", [(row["id"],) for row in rows])
            conn.commit()
            total += len(rows)
        except Exception:
            conn.rollback()
            raise
    if directory.exists():
        archive_cutoff = now - timedelta(days=config.RUN_EVENT_ARCHIVE_RETENTION_DAYS)
        for path in directory.glob(Path(db_path).name + ".run_events_????-??.sqlite"):
            month = datetime.strptime(path.stem[-7:], "%Y-%m")
            next_month = month.replace(year=month.year + 1, month=1) if month.month == 12 else month.replace(month=month.month + 1)
            if next_month <= archive_cutoff:
                checked_path(path, parent).unlink()
    return total


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
        "run_events": 0,
        "trade_records_sim": 0,
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
        summary["run_events"] = _archive_run_events(conn, db_path, now)
        summary["trade_records_sim"] = _delete_expired_batches(
            conn, "trade_records_sim", "trade_time",
            now - timedelta(days=config.SIM_TRADE_RETENTION_DAYS),
        )
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
    summary = {"db_path": db_path, "decision_log": 0, "buy_history": 0, "vacuum": False}
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
        if _table_exists(conn, "buy_history"):
            columns = {row[1] for row in conn.execute("PRAGMA table_info(buy_history)")}
            condition = "success = 0"
            if "is_simulation" in columns:
                condition += " OR is_simulation = 1"
            summary["buy_history"] = _delete_expired_batches(
                conn, "buy_history", "buy_time",
                now - timedelta(days=config.AUTOBUY_ATTEMPT_RETENTION_DAYS), condition,
            )
        if (
            summary["decision_log"] + summary["buy_history"] >= getattr(config, "DB_MAINTENANCE_VACUUM_MIN_DELETED_ROWS", 1000)
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
                from runtime_retention import cleanup_runtime_artifacts
                artifacts = cleanup_runtime_artifacts(Path(__file__).resolve().parent, apply=True, now=now)
                failures = [item for item in artifacts if item.get("error")]
                if failures:
                    raise RuntimeError("部分运行产物清理失败: %s" % failures)
                logger.info("运行产物维护完成: 清理 %s 项, 目标大小 %s 字节",
                            len(artifacts), sum(item["bytes"] for item in artifacts))
                last_run_date = now.date()
            except Exception as exc:
                logger.error(f"数据库维护任务失败: {exc}", exc_info=True)

        if stop_event is not None:
            stop_event.wait(interval)
        else:
            time.sleep(interval)
