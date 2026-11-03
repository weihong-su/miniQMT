"""停机后迁移网格订单身份，默认仅预览；保留旧表和一致性备份。"""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime_retention import process_running
from db_migrate import apply_grid_order_scope

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, help='目标 SQLite 数据库')
    parser.add_argument('--apply', action='store_true', help='正式迁移，默认在副本中预览')
    parser.add_argument('--repair-errors', action='store_true', help='双账本核对后归档并清理失效错误')
    args = parser.parse_args()
    path = Path(args.db).resolve()
    if not path.is_file():
        raise RuntimeError('目标数据库不存在')
    if args.apply:
        pid_file = path.parent / 'pid.txt'
        if pid_file.is_file():
            pid = int(pid_file.read_text(encoding='utf-8').strip())
            if process_running(pid):
                raise RuntimeError('目标账户仍有记录的运行进程，请先优雅停机')
    result = apply_grid_order_scope(str(path), dry_run=not args.apply,
                                    repair_errors=args.repair_errors)
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
