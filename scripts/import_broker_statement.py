# -*- coding: utf-8 -*-
"""
券商对账单导入与回填。

用法：
    python scripts/import_broker_statement.py --dir "<对账单目录>" --dry-run
    python scripts/import_broker_statement.py --dir "<对账单目录>"

为什么必须做：QMT 的 xttrader 没有历史成交查询接口（query_stock_trades 只返回
当日），库里既成的 time_source='local_fallback' 记录只有靠对账单才能升级为
'broker' 并拿到真实手续费。

幂等：同一文件重复导入会先按 (account, import_file) 清掉旧行再写入，
匹配与回填本身也是覆盖式的，因此可以反复跑。
"""
import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import broker_import as bi   # noqa: E402
import db_migrate            # noqa: E402

FILE_PATTERN = re.compile(r'^(?P<acct>\d+)_(?P<seq>\d+)_(?P<kind>[A-Za-z]+)\.csv$')
KIND_ALIASES = {
    'deals': 'deals', 'orders': 'orders', 'positions': 'positions',
    'account': 'account', 'stkdelivery': 'delivery', 'stkfundflow': 'fund_flow',
    'taskprogress': 'task_progress',
}


def discover_statement_files(directory):
    """扫描对账单目录，返回 {account: {kind: path}}。"""
    found = {}
    for name in sorted(os.listdir(directory)):
        m = FILE_PATTERN.match(name)
        if not m:
            continue
        kind = KIND_ALIASES.get(m.group('kind').lower())
        if not kind:
            continue
        found.setdefault(m.group('acct'), {})[kind] = os.path.join(directory, name)
    return found


def store_broker_deals(conn, account, deals, source_file):
    """写入 broker_deals，按 (account, import_file) 先清后插保证幂等。"""
    conn.execute("DELETE FROM broker_deals WHERE account=? AND import_file=?",
                 (account, source_file))
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.executemany(
        "INSERT INTO broker_deals(account, deal_date, deal_time, deal_time_str, "
        "code, stock_name, side, price, volume, amount, commission, stamp_duty, "
        "transfer_fee, broker_traded_id, broker_order_id, broker_order_ref, "
        "strategy_name, matched_trade_id, match_status, match_method, "
        "import_file, imported_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(d['account'] or account, d['deal_date'], d['deal_time'], d['deal_time_str'],
          d['code'], d['stock_name'], d['side'], d['price'], d['volume'], d['amount'],
          d['commission'], d['stamp_duty'], d['transfer_fee'], d['broker_traded_id'],
          d['broker_order_id'], d['broker_order_ref'], d['strategy_name'],
          None, 'pending', None, source_file, now) for d in deals])


def store_broker_orders(conn, account, orders, source_file):
    conn.execute("DELETE FROM broker_orders WHERE account=? AND import_file=?",
                 (account, source_file))
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.executemany(
        "INSERT INTO broker_orders(account, order_date, order_time, code, side, "
        "order_ref, broker_order_id, status, price, volume, traded_volume, "
        "cancel_volume, reject_reason, strategy_name, import_file, imported_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(o['account'] or account, o['order_date'], o['order_time'], o['code'],
          o['side'], o['order_ref'], o['broker_order_id'], o['status'], o['price'],
          o['volume'], o['traded_volume'], o['cancel_volume'], o['reject_reason'],
          o['strategy_name'], source_file, now) for o in orders])


def apply_backfill(conn, match, updates, dry_run=False):
    """把回填结果写回 trade_records，并标注 broker_deals 的匹配情况。"""
    local_id = match['local']['id']
    if updates and not dry_run:
        sets = ', '.join(f"{k}=?" for k in updates)
        conn.execute(f"UPDATE trade_records SET {sets} WHERE id=?",
                     list(updates.values()) + [local_id])
    if not dry_run:
        conn.execute(
            "UPDATE broker_deals SET matched_trade_id=?, match_status=?, match_method=? "
            "WHERE account=? AND broker_traded_id=? AND code=? AND deal_date=?",
            (local_id, 'matched', match['method'], match['broker']['account'],
             match['broker']['broker_traded_id'], match['broker']['code'],
             match['broker']['deal_date']))


def process_account(account, files, db_path, dry_run=False):
    """导入一个账号的对账单并回填。返回统计 dict。"""
    stats = {'account': account, 'deals': 0, 'orders': 0, 'matched': 0,
             'unmatched': 0, 'updated': 0, 'by_method': {}, 'notes': [],
             'fund_flows': 0, 'delivery': 0}

    conn = sqlite3.connect(db_path, timeout=60.0)
    conn.row_factory = sqlite3.Row
    try:
        db_migrate.migrate_settlement_schema(db_path, do_backup=False, dry_run=dry_run)

        deals = []
        if 'deals' in files:
            rows, enc = bi.read_csv_rows(files['deals'])
            deals = bi.parse_deals(rows, os.path.basename(files['deals']), account)
            stats['deals'] = len(deals)
            stats['notes'].append(f"deals.csv 编码={enc}, {len(rows)} 行")
            if not dry_run:
                store_broker_deals(conn, account, deals, os.path.basename(files['deals']))

        # 交割单若也有数据，合并进 broker_deals（它是另一条独立来源）
        if 'delivery' in files:
            rows, _ = bi.read_csv_rows(files['delivery'])
            extra = bi.parse_delivery(rows, os.path.basename(files['delivery']), account)
            stats['delivery'] = len(extra)
            if extra:
                deals.extend(extra)
                if not dry_run:
                    store_broker_deals(conn, account, extra,
                                       os.path.basename(files['delivery']))

        if 'orders' in files:
            rows, _ = bi.read_csv_rows(files['orders'])
            orders = bi.parse_orders(rows, os.path.basename(files['orders']), account)
            stats['orders'] = len(orders)
            if not dry_run:
                store_broker_orders(conn, account, orders,
                                    os.path.basename(files['orders']))

        if 'fund_flow' in files:
            rows, _ = bi.read_csv_rows(files['fund_flow'])
            flows = bi.parse_fund_flows(rows, os.path.basename(files['fund_flow']), account)
            stats['fund_flows'] = len(flows)
            stats['notes'].append(
                f"资金流水 {len(flows)} 条"
                + ("（文件只有表头，无数据）" if not flows else ""))

        if not deals:
            stats['notes'].append('无成交数据可回填')
            if not dry_run:
                conn.commit()
            return stats

        local = bi.load_local_trades(conn, account)
        matches, unmatched = bi.match_deals(deals, local)
        stats['matched'] = len(matches)
        stats['unmatched'] = len(unmatched)
        for m in matches:
            stats['by_method'][m['method']] = stats['by_method'].get(m['method'], 0) + 1

        for match in matches:
            updates, notes = bi.plan_backfill(match)
            if updates:
                stats['updated'] += 1
            apply_backfill(conn, match, updates, dry_run=dry_run)

        if not dry_run:
            for deal in unmatched:
                conn.execute(
                    "UPDATE broker_deals SET match_status='no_local_record' "
                    "WHERE account=? AND broker_traded_id=? AND code=? AND deal_date=?",
                    (account, deal['broker_traded_id'], deal['code'], deal['deal_date']))
            conn.commit()
        else:
            conn.rollback()
    finally:
        conn.close()
    return stats


def main():
    parser = argparse.ArgumentParser(description='券商对账单导入与回填')
    parser.add_argument('--dir', required=True, help='对账单目录')
    parser.add_argument('--accounts', default='all', help="'all' 或具体账号")
    parser.add_argument('--dry-run', action='store_true', help='只报告，不写库')
    parser.add_argument('--no-backfill', action='store_true',
                        help='只导入 broker_deals，不改 trade_records')
    parser.add_argument('--db', help='直接指定库路径（排障/演练用，需配合 --accounts）')
    args = parser.parse_args()

    if not os.path.isdir(args.dir):
        print(f"目录不存在: {args.dir}")
        return 1

    print("=" * 70)
    print("券商对账单导入")
    print("=" * 70)
    if args.dry_run:
        print(">>> DRY RUN —— 不会写入任何数据 <<<")
    print(f"对账单目录: {args.dir}\n")

    statements = discover_statement_files(args.dir)
    if not statements:
        print("目录内没有识别到对账单文件（期望形如 25105132_2_deals.csv）")
        return 1

    db_map = {acc: path for acc, path, _ in db_migrate.discover_account_dbs()}
    if args.db:
        db_map = {args.accounts: args.db} if args.accounts != 'all' else {}

    total = {'deals': 0, 'matched': 0, 'unmatched': 0, 'updated': 0}
    for account, files in sorted(statements.items()):
        if args.accounts != 'all' and account != args.accounts:
            continue
        print(f"─── 账号 {account} ───")
        for kind, path in sorted(files.items()):
            print(f"    {kind:<14} {os.path.basename(path)}")

        db_path = db_map.get(account)
        if not db_path:
            print(f"    [SKIP] 找不到该账号的库（account_config.json 与 data_*/ 都没有）")
            continue

        try:
            stats = process_account(account, files, db_path, dry_run=args.dry_run)
        except Exception as exc:
            print(f"    [FAIL] {exc}")
            raise

        print(f"    成交 {stats['deals']} 笔 / 委托 {stats['orders']} 笔")
        print(f"    匹配成功 {stats['matched']}，未匹配 {stats['unmatched']}")
        if stats['by_method']:
            print(f"    匹配方式: {stats['by_method']}")
        print(f"    将回填 {stats['updated']} 行")
        for note in stats['notes']:
            print(f"    注: {note}")
        print()

        for key in total:
            total[key] += stats.get(key, 0)

    print("=" * 70)
    print(f"合计: 成交 {total['deals']} 笔，匹配 {total['matched']}，"
          f"未匹配 {total['unmatched']}，回填 {total['updated']} 行")
    if args.dry_run:
        print("DRY RUN 结束，未写入。")
    print("=" * 70)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
