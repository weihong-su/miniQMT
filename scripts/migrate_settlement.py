# -*- coding: utf-8 -*-
"""
交割单改造 —— 停机后的一键迁移。

⚠️ 执行前必须确认 miniQMT 已停止：
       tasklist | findstr python
   本脚本会做 WAL checkpoint 并修改 trade_records（删占位流水、标记重复行），
   运行中的进程会与你抢写锁。

用法：
    python scripts/migrate_settlement.py --dry-run        # 先看会改什么
    python scripts/migrate_settlement.py                  # 对配置声明的账号执行
    python scripts/migrate_settlement.py --accounts all   # 含磁盘上未声明的账号库
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config          # noqa: E402
import db_migrate      # noqa: E402


def print_report(report, label):
    print()
    print(f"───── {label} ─────")
    print(f"  库路径           : {report['db_path']}")
    if report.get('backup'):
        print(f"  迁移前备份       : {report['backup']}")
    print(f"  新增列           : {report['added_columns'] or '（无需新增）'}")
    print(f"  account 回填     : {report['account_backfilled']} 行")
    print(f"  占位流水归档     : {report['archived_placeholder']} 行")
    print(f"  占位流水删除     : {report['deleted_placeholder']} 行")
    print(f"  标记为重复行     : {len(report['marked_duplicates'])} 组")
    for m in report['marked_duplicates']:
        print(f"      保留 id={m['keep_id']:<6} 标记 superseded={m['superseded']} "
              f"tid={m['trade_id']} code={m['stock_code']}")
    print(f"  trade_records    : {report['rows_before']} 行 → {report['rows_after']} 行")
    print(f"  已建索引         : {report['indexes']}")


def main():
    parser = argparse.ArgumentParser(description='交割单改造迁移（需先停止 miniQMT）')
    parser.add_argument('--accounts', help="'all' = 包含磁盘扫描到的账号库")
    parser.add_argument('--db', help='只迁移指定库')
    parser.add_argument('--dry-run', action='store_true', help='只报告，不写库')
    parser.add_argument('--no-backup', action='store_true', help='跳过迁移前备份')
    parser.add_argument('--skip-extension', action='store_true',
                        help='只建新表，不扩展 trade_records')
    args = parser.parse_args()

    print("=" * 68)
    print("交割单改造迁移")
    print("=" * 68)
    if args.dry_run:
        print(">>> DRY RUN —— 不会写入任何数据 <<<")

    if args.accounts == 'all':
        targets = db_migrate.discover_account_dbs()
    elif args.db:
        targets = [('arg', args.db, 'arg')]
    else:
        targets = [('current', config.DB_PATH, 'default')]

    print(f"\n目标库 {len(targets)} 个:")
    for acc_id, path, src in targets:
        print(f"  {acc_id:<12} {path}  [{src}]")

    failed = []
    for acc_id, path, _src in targets:
        if not os.path.exists(path):
            print(f"\n  [SKIP] {acc_id}: 库不存在 {path}")
            continue
        try:
            # 阶段一：新表（安全、幂等）。dry_run 时走副本，不碰真库。
            new_tables = db_migrate.migrate_settlement_schema(
                path,
                do_backup=not args.no_backup and not args.dry_run,
                dry_run=args.dry_run)
            print(f"\n  [{acc_id}] 新表: {new_tables if new_tables else '（已存在）'}")

            # 阶段二：trade_records 扩展 + 数据清理 + 索引
            if not args.skip_extension:
                report = db_migrate.apply_trade_records_extension(
                    path, do_backup=False, dry_run=args.dry_run)
                print_report(report, acc_id)
        except Exception as exc:
            failed.append((acc_id, str(exc)))
            print(f"\n  [FAIL] {acc_id}: {exc}")

    print()
    print("=" * 68)
    if failed:
        print(f"失败 {len(failed)} 个库:")
        for acc_id, msg in failed:
            print(f"  {acc_id}: {msg}")
        return 1
    print("全部完成。")
    print("下一步：重启 miniQMT，确认日志出现「启动收盘快照线程」。")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
