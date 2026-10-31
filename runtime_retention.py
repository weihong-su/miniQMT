"""回归备份与已完成测试产物的有限保留，不初始化交易组件。"""
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import threading
import time
import subprocess
import zipfile

import config

_backup_thread_lock = threading.Lock()


def checked_path(path, root):
    """核实路径位于指定根目录内，拒绝符号链接及 Windows 目录联接。"""
    root = Path(root).absolute()
    path = Path(path).absolute()
    path.relative_to(root)
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                raise ValueError("拒绝处理链接路径: %s" % item)
        if item == root:
            break
    path.resolve().relative_to(root.resolve())
    return path


@contextmanager
def backup_lock(directory):
    """串行保护同目录内的备份发布和淘汰，进程退出时由系统释放锁。"""
    with _backup_thread_lock:
        path = checked_path(Path(directory) / ".regression_backup.lock", directory)
        with path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)


def check_database(path):
    """校验独立 SQLite 备份，连接始终显式关闭。"""
    wal = Path(str(path) + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise ValueError("独立备份存在非空 WAL，停止校验和淘汰: %s" % path)
    uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True, timeout=config.RUNTIME_DB_TIMEOUT_SECONDS)) as conn:
        if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("备份完整性校验失败: %s" % path)


def backup_candidates(db_path):
    """仅识别本模块生成的回归备份；排序不依赖继承自源文件的修改时间。"""
    source = Path(db_path).absolute()
    pattern = re.compile(re.escape(source.name) + r"\.backup_\d{8}_\d{6}(?:_\d{6})?$")
    return sorted(
        (checked_path(p, source.parent) for p in source.parent.iterdir()
         if pattern.fullmatch(p.name) and p.is_file()),
        key=lambda p: p.name, reverse=True,
    )


def stale_backups(db_path):
    """保留份数必须为正，且留下的每份备份均须有效。"""
    if config.REGRESSION_BACKUP_KEEP < 1:
        raise ValueError("REGRESSION_BACKUP_KEEP 必须大于零")
    paths = backup_candidates(db_path)
    for path in paths[:config.REGRESSION_BACKUP_KEEP]:
        check_database(path)
    return paths[config.REGRESSION_BACKUP_KEEP:]


def create_regression_backup(db_path, prune=True):
    """从包含 WAL 的已提交视图生成备份，成功校验后才发布和淘汰。"""
    source = Path(db_path).absolute()
    checked_path(source, source.parent)
    with backup_lock(source.parent):
        pattern = re.compile(re.escape(source.name) + r"\.backup_\d{8}_\d{6}_\d{6}\.tmp$")
        for item in source.parent.iterdir():
            if pattern.fullmatch(item.name):
                checked_path(item, source.parent).unlink()
        target = source.with_name(source.name + ".backup_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
        temporary = target.with_name(target.name + ".tmp")
        deadline = time.monotonic() + config.RUNTIME_DB_TIMEOUT_SECONDS

        def progress(status, remaining, total):
            if time.monotonic() > deadline:
                raise TimeoutError("SQLite 备份超过等待上限")

        try:
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True,
                                        timeout=config.RUNTIME_DB_TIMEOUT_SECONDS)) as src:
                with closing(sqlite3.connect(str(temporary))) as dst:
                    src.backup(dst, progress=progress)
            check_database(temporary)
            temporary.replace(target)
        finally:
            if temporary.exists():
                temporary.unlink()
        if prune:
            for item in stale_backups(source):
                checked_path(item, source.parent).unlink()
        return str(target)


def process_running(pid):
    """无法查询进程状态时按仍在运行保护。"""
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87
        try:
            code = ctypes.c_ulong()
            if not kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
                return True
            return code.value == 259
        finally:
            kernel.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def completed_task(path):
    """仅接管有退出码证据且无存活 PID 的测试任务。"""
    if not path.name.startswith(config.RUNTIME_ARTIFACT_PREFIXES):
        return None
    checked_path(path, path.parent)
    for pid_file in path.glob("*.pid"):
        value = pid_file.read_text(encoding="utf-8").strip()
        if not value.isdigit() or process_running(int(value)):
            return None
    final = path / "full_regression.exitcode"
    if not final.is_file() or not re.fullmatch(r"-?\d+", final.read_text().strip()):
        return None
    return datetime.fromtimestamp(final.stat().st_mtime)


def tree_size(path):
    """计算普通目录大小；删除前同样扫描，拒绝深层联接。"""
    root = path.parent
    checked_path(path, root)
    if path.is_file():
        return path.stat().st_size
    total = 0
    for current, directories, files in os.walk(str(path), followlinks=False):
        for name in directories + files:
            item = checked_path(Path(current) / name, root)
            if item.is_file():
                total += item.stat().st_size
    return total


def stale_task_artifacts(log_dir, now=None):
    """只淘汰过期完整副本，根目录的报告证据单独保留。"""
    now = now or datetime.now()
    log_dir = Path(log_dir).absolute()
    completed = []
    for task in log_dir.iterdir():
        if task.is_dir():
            stamp = completed_task(task)
            if stamp:
                completed.append((stamp, task))
    completed.sort(reverse=True)
    result = []
    for index, (stamp, task) in enumerate(completed):
        if index >= config.RUNTIME_ARTIFACT_KEEP or stamp < now - timedelta(days=config.RUNTIME_ARTIFACT_RETENTION_DAYS):
            for child in task.iterdir():
                # 源码副本先保存复现证据；任务根目录中的报告、补丁和指纹保留。
                if child.is_dir() and ("workspace" in child.name or child.name in ("environment_recheck", "stockquant", "pytest_temp", "chrome-profile")):
                    result.append(child)
                elif child.name == "source.zip":
                    # 原始源码归档保留；不加入待删除清单。
                    continue
        if stamp < now - timedelta(days=config.RUNTIME_REPORT_RETENTION_DAYS):
            if (task / "source.zip").exists():
                # 到期报告可清理，唯一原始源码包仍保留。
                result.extend(child for child in task.iterdir() if child.name not in ("source.zip", "full_regression.exitcode"))
            else:
                result.append(task)
    # 删除整个任务时不重复列出其子目录。
    return [p for p in result if not any(parent in result for parent in p.parents)]


def preserve_task_sources(path, project_root):
    """记录可从 Git 还原的版本，并保存所有其余源码，成功后才允许删工作副本。"""
    if not path.is_dir():
        return
    task = path.parent
    archive = checked_path(task / ("retained_%s.zip" % path.name), project_root)
    tree_size(path)
    commit, tracked = None, {}
    if (Path(project_root) / ".git").exists():
        commit = subprocess.check_output(["git", "-C", str(project_root), "rev-parse", "HEAD"]).decode().strip()
        listing = subprocess.check_output(["git", "-C", str(project_root), "ls-tree", "-rz", "HEAD"])
        for entry in listing.split(b"\0"):
            if entry:
                attributes, name = entry.split(b"\t", 1)
                tracked[name.decode("utf-8")] = attributes.split()[-1].decode()
    files = {}
    for current, directories, names in os.walk(str(path)):
        relative = Path(current).relative_to(path)
        directories[:] = [name for name in directories if name not in ("__pycache__", ".pytest_cache", "node_modules")
                          and not (relative == Path(".") and name in ("data", "logs", "dummy"))]
        for name in names:
            item = Path(current) / name
            content = item.read_bytes()
            digest = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
            files[item.relative_to(path).as_posix()] = (digest, content)
    if archive.exists():
        with zipfile.ZipFile(str(archive)) as saved:
            if saved.testzip() is not None:
                raise ValueError("保留的源码归档校验失败: %s" % archive)
            manifest = json.loads(saved.read(".retention_manifest.json"))
            for name, (digest, _) in files.items():
                if manifest["files"].get(name) != digest:
                    raise ValueError("工作副本在归档后发生变化，保留目录: %s" % path)
        return
    temporary = checked_path(archive.with_name(archive.name + ".tmp"), project_root)
    try:
        with zipfile.ZipFile(str(temporary), "w", zipfile.ZIP_DEFLATED) as saved:
            for name, (digest, content) in files.items():
                if not commit or tracked.get(name) != digest:
                    saved.writestr("files/" + name, content)
            saved.writestr(".retention_manifest.json", json.dumps({
                "base_commit": commit, "files": {name: item[0] for name, item in files.items()},
            }, ensure_ascii=False, sort_keys=True))
        with zipfile.ZipFile(str(temporary)) as saved:
            if saved.testzip() is not None:
                raise ValueError("源码归档写入校验失败")
        temporary.replace(archive)
    finally:
        if temporary.exists():
            temporary.unlink()


def cleanup_runtime_artifacts(project_root, apply=False, now=None):
    """预览或执行白名单清理；返回路径、容量与处理结果。"""
    root = Path(project_root).absolute()
    if min(config.REGRESSION_BACKUP_KEEP, config.RUNTIME_ARTIFACT_KEEP,
           config.RUNTIME_ARTIFACT_RETENTION_DAYS, config.RUNTIME_REPORT_RETENTION_DAYS) < 1:
        raise ValueError("运行产物保留参数必须大于零")
    result = []
    data_dirs = [root / "data"] + sorted(root.glob("data_*"))
    for directory in data_dirs:
        if not directory.is_dir():
            continue
        checked_path(directory, root)
        with backup_lock(directory):
            for name in ("trading.db", "positions.db", "trading_test.db", "autobuy.db"):
                for path in stale_backups(directory / name):
                    result.append({"path": str(path), "bytes": path.stat().st_size, "rule": "回归备份超出保留份数"})
                    if apply:
                        try:
                            checked_path(path, root).unlink()
                            result[-1]["applied"] = True
                        except OSError as exc:
                            result[-1]["error"] = str(exc)
    log_dir = root / "logs"
    if log_dir.exists():
        for path in stale_task_artifacts(log_dir, now):
            item = {"path": str(path), "bytes": tree_size(path), "rule": "已完成测试产物到期"}
            result.append(item)
            if apply:
                try:
                    if completed_task(path if path.parent == log_dir else path.parent) is None:
                        raise RuntimeError("任务状态变化，停止清理: %s" % path)
                    checked_path(path, root)
                    if path.is_dir():
                        if path.parent != log_dir and ("workspace" in path.name or path.name in ("environment_recheck", "stockquant")):
                            preserve_task_sources(path, root)
                        shutil.rmtree(str(path))
                    else:
                        path.unlink()
                    item["applied"] = True
                except (OSError, ValueError, RuntimeError) as exc:
                    item["error"] = str(exc)
    return result
