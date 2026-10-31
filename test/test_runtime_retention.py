"""运行产物保留策略的回归测试，所有写入均限制在临时目录。"""
import contextlib
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import config
from test import run_integration_regression_tests as runner


class TestRegressionBackupRetention(unittest.TestCase):
    def test_prepare_prunes_old_backups_after_valid_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            source = data / "trading.db"
            with contextlib.closing(sqlite3.connect(str(source))) as conn:
                conn.execute("CREATE TABLE sample(value)")
                conn.execute("INSERT INTO sample VALUES (42)")
                conn.commit()
            for day in range(1, 16):
                target = data / ("trading.db.backup_202601%02d_010000" % day)
                with contextlib.closing(sqlite3.connect(str(source))) as src:
                    with contextlib.closing(sqlite3.connect(str(target))) as dst:
                        src.backup(dst)
                os.utime(str(target), (1, 1))
            protected = data / "trading.db.bak_manual"
            protected.write_bytes("专项修复备份".encode("utf-8"))
            with patch.object(runner, "PROJECT_ROOT", tmp), \
                 contextlib.redirect_stdout(io.StringIO()):
                result = runner.prepare_test_environment(clean_db=False)
            backups = list(data.glob("trading.db.backup_*"))
            self.assertLessEqual(len(backups), 10)
            self.assertEqual(protected.read_bytes(), "专项修复备份".encode("utf-8"))
            self.assertTrue(result["backed_up_files"])
            with contextlib.closing(sqlite3.connect(result["backed_up_files"][0])) as conn:
                self.assertEqual(conn.execute("SELECT value FROM sample").fetchone()[0], 42)



    def test_prepare_includes_account_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            account = Path(tmp) / "data_TEST"
            account.mkdir()
            source = account / "trading.db"
            with contextlib.closing(sqlite3.connect(str(source))) as conn:
                conn.execute("CREATE TABLE sample(value)")
                conn.execute("INSERT INTO sample VALUES (7)")
                conn.commit()
            with patch.object(runner, "PROJECT_ROOT", tmp), contextlib.redirect_stdout(io.StringIO()):
                result = runner.prepare_test_environment(clean_db=False)
            self.assertEqual(len(result["backed_up_files"]), 1)
            self.assertEqual(Path(result["backed_up_files"][0]).parent, account)


    def test_backup_failure_stops_test_database_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            source = data / "trading.db"
            with contextlib.closing(sqlite3.connect(str(source))) as conn:
                conn.execute("CREATE TABLE sample(value)")
            protected = data / "trading_test.db"
            protected.write_bytes(b"test-db-to-keep")
            with patch.object(runner, "PROJECT_ROOT", tmp), \
                 patch("runtime_retention.create_regression_backup", side_effect=OSError("备份失败")), \
                 contextlib.redirect_stdout(io.StringIO()):
                result = runner.prepare_test_environment(clean_db=True)
            self.assertTrue(result["backup_failed"])
            self.assertEqual(result["cleaned_files"], [])
            self.assertEqual(protected.read_bytes(), b"test-db-to-keep")


    def test_import_preserves_existing_standard_streams(self):
        import subprocess
        import sys
        code = ("import sys; original=(sys.stdout,sys.stderr); "
                "import test.run_integration_regression_tests; "
                "raise SystemExit(0 if sys.stdout is original[0] and sys.stderr is original[1] else 3)")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))


class TestRuntimeRetentionSafety(unittest.TestCase):
    def make_database(self, root):
        source = Path(root) / "trading.db"
        with contextlib.closing(sqlite3.connect(str(source))) as conn:
            conn.execute("CREATE TABLE sample(value)")
            conn.execute("INSERT INTO sample VALUES (42)")
            conn.commit()
        return source

    def test_backup_includes_committed_wal(self):
        from runtime_retention import create_regression_backup
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "trading.db"
            with contextlib.closing(sqlite3.connect(str(source))) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA wal_autocheckpoint=0")
                conn.execute("CREATE TABLE sample(value)")
                conn.execute("INSERT INTO sample VALUES (99)")
                conn.commit()
                self.assertTrue(Path(str(source) + "-wal").exists())
                backup = create_regression_backup(source)
                with contextlib.closing(sqlite3.connect(backup)) as dst:
                    self.assertEqual(dst.execute("SELECT value FROM sample").fetchone(), (99,))

    def test_failed_backup_does_not_prune_existing_files(self):
        from runtime_retention import create_regression_backup
        with tempfile.TemporaryDirectory() as tmp:
            source = self.make_database(tmp)
            for day in range(1, 13):
                (Path(tmp) / ("trading.db.backup_202601%02d_000000" % day)).write_bytes(source.read_bytes())
            before = sorted(p.name for p in Path(tmp).glob("*.backup_*"))
            with patch("runtime_retention.check_database", side_effect=ValueError("校验失败")):
                with self.assertRaises(ValueError):
                    create_regression_backup(source)
            self.assertEqual(sorted(p.name for p in Path(tmp).glob("*.backup_*")), before)

    def test_concurrent_backup_and_interrupted_temporary_file(self):
        from concurrent.futures import ThreadPoolExecutor
        from runtime_retention import create_regression_backup
        with tempfile.TemporaryDirectory() as tmp:
            source = self.make_database(tmp)
            interrupted = Path(tmp) / "trading.db.backup_20260101_000000_000001.tmp"
            interrupted.write_bytes(b"partial")
            with patch.object(config, "REGRESSION_BACKUP_KEEP", 2):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    paths = list(pool.map(lambda _: create_regression_backup(source), range(4)))
                self.assertEqual(len(set(paths)), 4)
                self.assertEqual(len(list(Path(tmp).glob("*.backup_*"))), 2)
                self.assertFalse(interrupted.exists())

    def test_previews_keep_running_and_unknown_tasks(self):
        from datetime import datetime
        from runtime_retention import cleanup_runtime_artifacts
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "logs"
            logs.mkdir()
            for index in range(7):
                task = logs / ("regression_2026010%d" % (index + 1))
                (task / "workspace").mkdir(parents=True)
                (task / "workspace" / "source.py").write_text("可重建源码", encoding="utf-8")
                (task / "verified_integration_report.json").write_text("{}", encoding="utf-8")
                flag = task / "full_regression.exitcode"
                flag.write_text("0")
                os.utime(str(flag), (datetime(2026, 1, index + 1).timestamp(),) * 2)
            running = logs / "regression_20260101"
            (running / "full_regression.pid").write_text(str(os.getpid()))
            unknown = logs / "regression_20260108"
            (unknown / "workspace").mkdir(parents=True)
            now = datetime(2026, 1, 10)
            preview = cleanup_runtime_artifacts(root, now=now)
            self.assertEqual([Path(x["path"]).parent.name for x in preview], ["regression_20260102"])
            self.assertTrue((logs / "regression_20260102" / "workspace").exists())
            actual = cleanup_runtime_artifacts(root, apply=True, now=now)
            self.assertEqual([(x["path"], x["bytes"]) for x in actual],
                             [(x["path"], x["bytes"]) for x in preview])
            self.assertTrue(all(x.get("applied") for x in actual))
            self.assertTrue((running / "workspace").exists())
            self.assertTrue((unknown / "workspace").exists())
            self.assertTrue((logs / "regression_20260102" / "verified_integration_report.json").exists())
            import zipfile
            with zipfile.ZipFile(str(logs / "regression_20260102" / "retained_workspace.zip")) as saved:
                self.assertEqual(saved.read("files/source.py").decode("utf-8"), "可重建源码")
            self.assertEqual(cleanup_runtime_artifacts(root, apply=True, now=now), [])

    def test_path_escape_and_nested_junction_are_rejected(self):
        import subprocess
        from runtime_retention import checked_path, tree_size
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            outside = Path(tmp) / "outside"
            root.mkdir()
            outside.mkdir()
            sentinel = outside / "keep.txt"
            sentinel.write_text("保留", encoding="utf-8")
            with self.assertRaises(ValueError):
                checked_path(root / ".." / "outside" / "keep.txt", root)
            link = root / "link"
            if os.name == "nt":
                result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                                        capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            else:
                link.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                tree_size(root)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "保留")


    def test_process_lock_and_baseline_exit_do_not_allow_cleanup(self):
        import subprocess
        import sys
        from datetime import datetime
        from runtime_retention import backup_lock, cleanup_runtime_artifacts
        with tempfile.TemporaryDirectory() as tmp:
            source = self.make_database(tmp)
            with backup_lock(Path(tmp)):
                code = "import sys,config; config.ENABLE_SIMULATION_MODE=True; from runtime_retention import create_regression_backup; create_regression_backup(sys.argv[1])"
                result = subprocess.run([sys.executable, "-c", code, str(source)], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(list(Path(tmp).glob("*.backup_*")), [])
            task = Path(tmp) / "logs" / "regression_20240101"
            (task / "workspace").mkdir(parents=True)
            (task / "baseline.exitcode").write_text("0")
            self.assertEqual(cleanup_runtime_artifacts(tmp, apply=True, now=datetime(2026, 10, 31)), [])
            self.assertTrue((task / "workspace").exists())

    def test_original_source_archive_is_retained(self):
        from datetime import datetime
        from runtime_retention import cleanup_runtime_artifacts
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "logs" / "regression_20260901"
            task.mkdir(parents=True)
            final = task / "full_regression.exitcode"
            final.write_text("0")
            os.utime(str(final), (datetime(2026, 9, 1).timestamp(),) * 2)
            archive = task / "source.zip"
            archive.write_bytes(b"unique-original-source")
            (task / "tested_sources.json").write_text("{}")
            self.assertEqual(cleanup_runtime_artifacts(tmp, apply=True, now=datetime(2026, 10, 31)), [])
            self.assertEqual(archive.read_bytes(), b"unique-original-source")
            cleanup_runtime_artifacts(tmp, apply=True, now=datetime(2027, 6, 1))
            self.assertEqual(archive.read_bytes(), b"unique-original-source")
            self.assertTrue((task / "full_regression.exitcode").exists())

    def test_retained_source_archive_survives_partial_cleanup_retry(self):
        import zipfile
        from runtime_retention import preserve_task_sources
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "logs" / "regression_20240101"
            workspace = task / "workspace"
            workspace.mkdir(parents=True)
            (workspace / "first.py").write_text("first = 1")
            (workspace / "second.py").write_text("second = 2")
            preserve_task_sources(workspace, Path(tmp))
            (workspace / "first.py").unlink()
            preserve_task_sources(workspace, Path(tmp))
            with zipfile.ZipFile(str(task / "retained_workspace.zip")) as saved:
                self.assertEqual(saved.read("files/first.py"), b"first = 1")
            (workspace / "second.py").write_text("second = 3")
            with self.assertRaises(ValueError):
                preserve_task_sources(workspace, Path(tmp))


    def test_wal_backup_validation_creates_no_sidecars(self):
        from runtime_retention import create_regression_backup, stale_backups
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "trading.db"
            with contextlib.closing(sqlite3.connect(str(source))) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("CREATE TABLE sample(value)")
                conn.commit()
                create_regression_backup(source)
                self.assertEqual(stale_backups(source), [])
                names = [p.name for p in Path(tmp).glob("trading.db.backup_*")]
                self.assertEqual(len(names), 1, names)
                self.assertFalse(any(p.endswith(("-wal", "-shm", ".tmp")) for p in names))


    def test_standalone_validation_rejects_nonempty_wal(self):
        from runtime_retention import check_database, create_regression_backup
        with tempfile.TemporaryDirectory() as tmp:
            source = self.make_database(tmp)
            backup = create_regression_backup(source)
            Path(backup + "-wal").write_bytes(b"unverified-wal")
            with self.assertRaises(ValueError):
                check_database(backup)
            self.assertTrue(Path(backup).exists())


class TestDatabaseRetentionSafety(unittest.TestCase):
    def open_event_database(self, tmp):
        conn = sqlite3.connect(str(Path(tmp) / "trading.db"))
        conn.executescript("""
            CREATE TABLE run_events (
                id INTEGER PRIMARY KEY, account TEXT, event_time TEXT,
                event_type TEXT, detail TEXT
            );
            CREATE TABLE account_equity_daily (account TEXT, validation_status TEXT);
        """)
        return conn

    def test_archive_boundaries_pending_assets_and_retry(self):
        import json
        from datetime import datetime
        from maintenance import cleanup_trading_db
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trading.db"
            with contextlib.closing(self.open_event_database(tmp)) as conn:
                conn.executemany("INSERT INTO run_events VALUES (?,?,?,?,?)", [
                    (1, "A", "2025-01-01 12:00:00", "snapshot", '{"信息":"旧事件"}'),
                    (2, "A", "2026-10-30 12:00:00", "snapshot", "新事件"),
                    (3, "A", "2025-10-31 00:00:00", "snapshot", "截止边界"),
                    (4, "A", "2025-01-01 12:00:00", "asset_identity_mismatch", "待复核"),
                    (5, "B", "2025-01-01 12:00:00", "asset_identity_recheck", "已复核"),
                ])
                conn.executemany("INSERT INTO account_equity_daily VALUES (?,?)",
                                 [("A", "pending_review"), ("B", "valid")])
                conn.commit()
            now = datetime(2026, 10, 31)
            with patch.object(config, "DB_MAINTENANCE_ENABLE_VACUUM", False), \
                 patch.object(config, "DB_RETENTION_BATCH_SIZE", 1):
                self.assertEqual(cleanup_trading_db(str(path), now)["run_events"], 2)
                with contextlib.closing(sqlite3.connect(str(path))) as conn:
                    self.assertEqual(conn.execute("SELECT id FROM run_events ORDER BY id").fetchall(), [(2,), (3,), (4,)])
                    conn.execute("INSERT INTO run_events VALUES (1,'A','2025-01-01 12:00:00','snapshot',?)", ('{"信息":"旧事件"}',))
                    conn.commit()
                self.assertEqual(cleanup_trading_db(str(path), now)["run_events"], 1)
            archive = Path(tmp) / "archive" / "run_events" / "trading.db.run_events_2025-01.sqlite"
            with contextlib.closing(sqlite3.connect(str(archive))) as conn:
                rows = conn.execute("SELECT id,payload FROM run_events ORDER BY id").fetchall()
                self.assertEqual([row[0] for row in rows], [1, 5])
                self.assertEqual(json.loads(rows[0][1])["detail"], '{"信息":"旧事件"}')

    def test_archive_failure_or_content_conflict_keeps_online_events(self):
        from datetime import datetime
        from maintenance import _archive_run_events
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trading.db"
            with contextlib.closing(self.open_event_database(tmp)) as conn:
                conn.execute("INSERT INTO run_events VALUES (1,'A','2025-01-01','snapshot','旧事件')")
                conn.commit()
                with patch("maintenance.sqlite3.connect", side_effect=OSError("归档磁盘不可写")):
                    with self.assertRaises(OSError):
                        _archive_run_events(conn, str(path), datetime(2026, 10, 31))
                self.assertEqual(conn.execute("SELECT count(*) FROM run_events").fetchone()[0], 1)
                archive = Path(tmp) / "archive" / "run_events" / "trading.db.run_events_2025-01.sqlite"
                with contextlib.closing(sqlite3.connect(str(archive))) as dst:
                    dst.execute("CREATE TABLE run_events(id INTEGER PRIMARY KEY,payload TEXT NOT NULL)")
                    dst.execute("INSERT INTO run_events VALUES (1,'不同内容')")
                    dst.commit()
                with self.assertRaises(ValueError):
                    _archive_run_events(conn, str(path), datetime(2026, 10, 31))
                self.assertEqual(conn.execute("SELECT count(*) FROM run_events").fetchone()[0], 1)

    def test_old_simulations_and_attempts_preserve_permanent_dedup(self):
        from datetime import datetime
        from autobuy.store import AutoBuyStore
        from maintenance import cleanup_autobuy_db, cleanup_trading_db
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "trading.db")
            with contextlib.closing(sqlite3.connect(path)) as conn:
                conn.execute("CREATE TABLE trade_records_sim(id INTEGER PRIMARY KEY,trade_time TEXT)")
                conn.executemany("INSERT INTO trade_records_sim VALUES (?,?)",
                                 [(1, "2024-01-01"), (2, "2026-08-02"), (3, "2026-10-30")])
                conn.commit()
            with patch.object(config, "DB_MAINTENANCE_ENABLE_VACUUM", False):
                self.assertEqual(cleanup_trading_db(path, datetime(2026, 10, 31))["trade_records_sim"], 1)
            store = AutoBuyStore(str(Path(tmp) / "autobuy.db"))
            try:
                store.conn.executemany(
                    "INSERT INTO buy_history(stock_code,buy_time,success,is_simulation) VALUES (?,?,?,?)",
                    [("600000.SH", "2024-01-01", 1, 0), ("600001.SH", "2024-01-01", 0, 0),
                     ("600002.SH", "2024-01-01", 1, 1), ("600003.SH", "2026-10-30", 0, 0)],
                )
                store.conn.commit()
                before = store.recently_bought_codes(-1)
                with patch.object(config, "DB_MAINTENANCE_ENABLE_VACUUM", False):
                    result = cleanup_autobuy_db(store.db_path, datetime(2026, 10, 31))
                self.assertEqual(result["buy_history"], 2)
                self.assertEqual(store.recently_bought_codes(-1), before)
                self.assertEqual(before, {"600000"})
                self.assertEqual(store.conn.execute("SELECT count(*) FROM buy_history").fetchone()[0], 2)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
