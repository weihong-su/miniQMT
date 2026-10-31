"""预览或执行运行产物清理；不会初始化行情、交易或 Web 服务。"""
import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import config
from runtime_retention import checked_path, cleanup_runtime_artifacts, create_regression_backup


def database_paths(root):
    """列出默认库和账号库，不把备份文件当成运行数据库。"""
    return [directory / "trading.db"
            for directory in [root / "data"] + sorted(root.glob("data_*"))
            if directory.is_dir() and (directory / "trading.db").is_file()]


def fingerprint(path):
    """对短只读事务内的业务表计算摘要，供清理前后核对。"""
    result = {}
    checked_path(path, path.parent)
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                                timeout=config.RUNTIME_DB_TIMEOUT_SECONDS)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        for name, in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            quoted = '"' + name.replace('"', '""') + '"'
            digest = hashlib.sha256()
            count = 0
            for row in conn.execute("SELECT * FROM " + quoted + " ORDER BY rowid"):
                digest.update((json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))
                count += 1
            result[name] = {"rows": count, "sha256": digest.hexdigest()}
        result["quick_check"] = conn.execute("PRAGMA quick_check").fetchall()
        conn.rollback()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT, help="项目根目录")
    parser.add_argument("--apply", action="store_true", help="执行清理；默认只预览")
    parser.add_argument("--snapshots", action="store_true", help="先为实际源库创建一致性备份")
    parser.add_argument("--maintain-databases", action="store_true", help="执行数据库保留维护")
    parser.add_argument("--report", type=Path, help="保存完整 JSON 清单和结果")
    args = parser.parse_args()
    if (args.snapshots or args.maintain_databases) and not args.apply:
        parser.error("备份和数据库维护必须与 --apply 一起使用")
    root = args.root.absolute()
    checked_path(root, root)
    output = {"timestamp": datetime.now().isoformat(), "apply": args.apply, "root": str(root)}
    paths = database_paths(root)
    autobuy = root / "data" / "autobuy.db"
    observed = paths + ([autobuy] if autobuy.exists() else [])
    try:
        output["before"] = {str(path.relative_to(root)): fingerprint(path) for path in observed}
        if args.snapshots:
            output["snapshots"] = {str(path.relative_to(root)): create_regression_backup(path, prune=False)
                                   for path in observed}
        output["artifacts"] = cleanup_runtime_artifacts(root, apply=args.apply)
        if any(item.get("error") for item in output["artifacts"]):
            output["error"] = "部分产物未清理，详情见各项错误"
        if args.maintain_databases:
            # 指定维护日志，避免独立工具污染账号业务日志。
            os.environ.setdefault("MINIQMT_LOG_FILE", str(root / "logs" / "runtime_retention_tool.log"))
            from maintenance import cleanup_autobuy_db, cleanup_trading_db
            output["databases"] = {str(path.relative_to(root)): cleanup_trading_db(str(path)) for path in paths}
            if autobuy.exists():
                output["databases"][str(autobuy.relative_to(root))] = cleanup_autobuy_db(str(autobuy))
        output["after"] = {str(path.relative_to(root)): fingerprint(path) for path in observed}
    except Exception as exc:
        output["error"] = str(exc)
    if args.report:
        target = checked_path(args.report.absolute(), root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    items = output.get("artifacts", [])
    print(json.dumps({
        "apply": args.apply, "items": len(items), "bytes": sum(item["bytes"] for item in items),
        "report": str(args.report) if args.report else None, "error": output.get("error"),
    }, ensure_ascii=False))
    if not args.report:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    return 1 if "error" in output else 0


if __name__ == "__main__":
    raise SystemExit(main())
