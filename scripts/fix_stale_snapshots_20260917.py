# -*- coding: utf-8 -*-
"""
一次性修正 2026-09-14 / 2026-09-15 的失真收盘快照。

背景（详见 test/test_snapshot_freshness.py 的模块 docstring）：
  09-14 13:29:55~14:59:45 `database is locked` 连续 457 次同步失败，13:29 网格
  买入的 volume 1000→1200 / cost 73.58→73.04 从未落到 SQLite；随后 15:02、17:09、
  09-15 22:30 三次重启都从 SQLite 载入这份旧值写快照，而盘后不会回源实盘纠正。

  失真是可量化的：同一次 take_snapshot 写出的 position_snapshot 合计 market_value
  必须等于 account_equity_daily.market_value（后者走 QMT 实时接口）。
    09-14 close: 180414.00 vs 194558.00  差 -14144.00 = 200 股 × 70.72
    09-15 close: 178166.00 vs 192266.00  差 -14100.00 = 200 股 × 70.50

修正值的证据：
  volume=1200      —— 09-14 open 1200 -10:08 卖200-> 1000 -13:29 买200-> 1200；
                      且 09-16 open 快照同样是 1200（期间无成交）
  cost_price=73.04 —— 09-14 13:29:40 日志「301085 成本价变化：73.58 -> 73.04」，
                      与 09-16 open 快照的 73.04 一致
  market_value     —— volume × 该行自带的 current_price（收盘价，本身无误）
  profit_ratio     —— (current_price - cost_price) / cost_price × 100

不修改的字段及原因：
  available        —— 当日买入 200 股是否 T+1 冻结只是推断，没有硬证据；
                      且 scripts/export_settlement.py 完全不读该字段
  base_cost_price  —— 全部快照恒为 74.12，本就没错
  001288 / 002859  —— 两只的 volume/cost 与净值快照自洽，无需修正

用法：
    python scripts/fix_stale_snapshots_20260917.py                 # dry-run（只读）
    python scripts/fix_stale_snapshots_20260917.py --apply         # 实际写入
    python scripts/fix_stale_snapshots_20260917.py --apply --with-equity
                                                   # 一并修正 09-14 close 的 cash=0
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_DB = 'data_25105132/trading.db'
ACCOUNT = '25105132'

# 每条修正都附证据，逐字段写死而不是"聪明地"反推——一次性脚本要的是可审计
POSITION_FIXES = [
    {
        'date': '2026-09-14', 'type': 'close', 'code': '301085',
        'set': {'volume': 1200.0, 'cost_price': 73.04,
                'market_value': 84864.00, 'profit_ratio': -3.18,
                'source': 'manual_fix'},
        'expect_before': {'volume': 1000.0, 'cost_price': 73.58,
                          'market_value': 70720.0},
        'why': '09-14 13:29 网格买入 200 股因 database is locked 未落 SQLite',
    },
    {
        'date': '2026-09-15', 'type': 'close', 'code': '301085',
        'set': {'volume': 1200.0, 'cost_price': 73.04,
                'market_value': 84600.00, 'profit_ratio': -3.48,
                'source': 'manual_fix'},
        'expect_before': {'volume': 1000.0, 'cost_price': 73.58,
                          'market_value': 70500.0},
        'why': '09-15 进程全天未运行，22:30 补录的快照是 09-14 的逐字段拷贝',
    },
]

# 清算时段脏读：QMT 在 16:30 后返回 cash=0.00 而 total_asset 正常，
# _is_blank_asset 的「全零」判据拦不住。cash 由恒等式反推，唯一解。
EQUITY_FIXES = [
    {
        'date': '2026-09-14', 'type': 'close',
        'set': {'cash': 319505.63, 'source': 'manual_fix'},
        'expect_before': {'cash': 0.0, 'total_asset': 514063.63,
                          'market_value': 194558.0},
        'why': 'cash = total_asset - market_value - frozen_cash '
               '= 514063.63 - 194558.00 - 0.00；当时日志已报「差额=319505.63」',
    },
]


def fetch_position(conn, fix):
    row = conn.execute(
        "SELECT volume, available, cost_price, base_cost_price, current_price, "
        "market_value, profit_ratio, source FROM position_snapshot "
        "WHERE account=? AND snapshot_date=? AND snapshot_type=? AND code=?",
        (ACCOUNT, fix['date'], fix['type'], fix['code'])).fetchone()
    return row


def fetch_equity(conn, fix):
    return conn.execute(
        "SELECT total_asset, market_value, cash, frozen_cash, source "
        "FROM account_equity_daily WHERE account=? AND date=? AND snapshot_type=?",
        (ACCOUNT, fix['date'], fix['type'])).fetchone()


def check_consistency(conn, date, stype):
    """持仓快照合计市值 vs 净值快照 market_value —— 这是判定失真的判据。"""
    pos = conn.execute(
        "SELECT COALESCE(SUM(market_value), 0) FROM position_snapshot "
        "WHERE account=? AND snapshot_date=? AND snapshot_type=?",
        (ACCOUNT, date, stype)).fetchone()[0]
    eq = conn.execute(
        "SELECT market_value FROM account_equity_daily "
        "WHERE account=? AND date=? AND snapshot_type=?",
        (ACCOUNT, date, stype)).fetchone()
    if eq is None:
        return pos, None, None
    return pos, eq[0], round(pos - eq[0], 2)


def verify_preconditions(conn):
    """改数之前先确认库里确实是我们预期的那份错误数据。"""
    problems = []
    for fix in POSITION_FIXES:
        row = fetch_position(conn, fix)
        if row is None:
            problems.append(f"找不到行: {fix['date']} {fix['type']} {fix['code']}")
            continue
        actual = {'volume': row[0], 'cost_price': row[2], 'market_value': row[5]}
        for key, expected in fix['expect_before'].items():
            if abs(actual[key] - expected) > 0.005:
                problems.append(
                    f"{fix['date']} {fix['code']}.{key} 现值 {actual[key]} "
                    f"与预期的错误值 {expected} 不符 —— 数据已被改过，中止")
    return problems


def verify_equity_preconditions(conn):
    problems = []
    for fix in EQUITY_FIXES:
        row = fetch_equity(conn, fix)
        if row is None:
            problems.append(f"找不到净值行: {fix['date']} {fix['type']}")
            continue
        actual = {'total_asset': row[0], 'market_value': row[1], 'cash': row[2]}
        for key, expected in fix['expect_before'].items():
            if abs(actual[key] - expected) > 0.005:
                problems.append(
                    f"{fix['date']} equity.{key} 现值 {actual[key]} "
                    f"与预期 {expected} 不符 —— 中止")
        if row[3] not in (0, 0.0, None):
            problems.append(f"{fix['date']} frozen_cash={row[3]} 非 0，反推公式不适用")
    return problems


def show_plan(conn, with_equity):
    print("=" * 78)
    print("修正前 —— 持仓快照 vs 净值快照 一致性")
    print("=" * 78)
    for date in ('2026-09-14', '2026-09-15', '2026-09-16'):
        for stype in ('open', 'close'):
            pos, eq, gap = check_consistency(conn, date, stype)
            if eq is None:
                continue
            flag = 'OK' if abs(gap) < 0.01 else 'MISMATCH'
            print(f"  {date} {stype:5s} 持仓合计={pos:>12.2f}  净值={eq:>12.2f}  "
                  f"差={gap:>+10.2f}  [{flag}]")

    print()
    print("=" * 78)
    print("计划修改的持仓快照行")
    print("=" * 78)
    for fix in POSITION_FIXES:
        row = fetch_position(conn, fix)
        print(f"\n  {fix['date']} {fix['type']} {fix['code']}")
        print(f"    依据: {fix['why']}")
        names = ['volume', 'available', 'cost_price', 'base_cost_price',
                 'current_price', 'market_value', 'profit_ratio', 'source']
        for i, name in enumerate(names):
            before = row[i]
            if name in fix['set']:
                print(f"    {name:18s} {before!r:>16}  ->  {fix['set'][name]!r}")
            else:
                print(f"    {name:18s} {before!r:>16}      (不改)")

    if with_equity:
        print()
        print("=" * 78)
        print("计划修改的净值快照行")
        print("=" * 78)
        for fix in EQUITY_FIXES:
            row = fetch_equity(conn, fix)
            print(f"\n  {fix['date']} {fix['type']}")
            print(f"    依据: {fix['why']}")
            for i, name in enumerate(['total_asset', 'market_value', 'cash',
                                      'frozen_cash', 'source']):
                if name in fix['set']:
                    print(f"    {name:14s} {row[i]!r:>14}  ->  {fix['set'][name]!r}")
                else:
                    print(f"    {name:14s} {row[i]!r:>14}      (不改)")


def apply_fixes(conn, with_equity):
    changed = 0
    for fix in POSITION_FIXES:
        cols = ', '.join(f"{k}=?" for k in fix['set'])
        params = list(fix['set'].values()) + [ACCOUNT, fix['date'],
                                              fix['type'], fix['code']]
        cur = conn.execute(
            f"UPDATE position_snapshot SET {cols} WHERE account=? AND "
            f"snapshot_date=? AND snapshot_type=? AND code=?", params)
        changed += cur.rowcount

    if with_equity:
        for fix in EQUITY_FIXES:
            cols = ', '.join(f"{k}=?" for k in fix['set'])
            params = list(fix['set'].values()) + [ACCOUNT, fix['date'], fix['type']]
            cur = conn.execute(
                f"UPDATE account_equity_daily SET {cols} WHERE account=? AND "
                f"date=? AND snapshot_type=?", params)
            changed += cur.rowcount
    conn.commit()
    return changed


def log_audit_event(db_path, with_equity):
    """在 run_events 留痕，让这次人工改数在审计链里可见。"""
    try:
        import settlement_db as sdb
        detail = {
            'script': os.path.basename(__file__),
            'position_rows': [f"{f['date']}/{f['type']}/{f['code']}"
                              for f in POSITION_FIXES],
            'equity_rows': [f"{f['date']}/{f['type']}" for f in EQUITY_FIXES]
                           if with_equity else [],
            'reason': 'database is locked 导致同步断链 + 盘后补录未回源实盘',
        }
        sdb.log_event('snapshot_manual_fix', 'WARNING', detail, db_path=db_path)
        return True
    except Exception as e:
        print(f"  [!] run_events 留痕失败（不影响数据修正）: {e}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--apply', action='store_true', help='实际写入（默认只做 dry-run）')
    ap.add_argument('--with-equity', action='store_true',
                    help='一并修正 09-14 close 的 cash=0 清算时段脏读')
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"[ERROR] 数据库不存在: {args.db}")
        return 2

    conn = sqlite3.connect(args.db)
    try:
        # 停机守卫：改数期间绝不能有交易进程连着
        try:
            conn.execute("BEGIN EXCLUSIVE")
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError as e:
            print(f"[ABORT] 数据库被占用，请先停止所有 miniQMT 进程: {e}")
            return 3

        problems = verify_preconditions(conn)
        if args.with_equity:
            problems += verify_equity_preconditions(conn)
        if problems:
            print("[ABORT] 前置校验不通过：")
            for p in problems:
                print("  -", p)
            return 4

        show_plan(conn, args.with_equity)

        if not args.apply:
            print()
            print("=" * 78)
            print("DRY-RUN：未写入任何数据。确认无误后加 --apply 执行。")
            print("=" * 78)
            return 0

        backup = f"{args.db}.bak_snapshot_fix_{datetime.now():%Y%m%d_%H%M%S}"
        shutil.copy2(args.db, backup)
        print(f"\n已备份: {backup}")

        changed = apply_fixes(conn, args.with_equity)
        print(f"已更新 {changed} 行")

        print()
        print("=" * 78)
        print("修正后 —— 持仓快照 vs 净值快照 一致性")
        print("=" * 78)
        bad = []
        for date in ('2026-09-14', '2026-09-15', '2026-09-16'):
            for stype in ('open', 'close'):
                pos, eq, gap = check_consistency(conn, date, stype)
                if eq is None:
                    continue
                ok = abs(gap) < 0.01
                if not ok:
                    bad.append(f"{date} {stype}")
                print(f"  {date} {stype:5s} 持仓合计={pos:>12.2f}  净值={eq:>12.2f}  "
                      f"差={gap:>+10.2f}  [{'OK' if ok else 'MISMATCH'}]")
        if bad:
            print(f"\n[WARN] 仍不一致: {bad}")
            return 5
    finally:
        conn.close()

    log_audit_event(args.db, args.with_equity)
    print("\n全部一致，修正完成。")
    return 0


if __name__ == '__main__':
    sys.exit(main())
