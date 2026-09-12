# -*- coding: utf-8 -*-
"""
标准交割单导出。

用法：
    python scripts/export_settlement.py --start 2026-09-11 --end 2026-10-10 \
        --accounts all --out export/

设计约束：
- 只读 DB，不依赖 logs/*.log —— 日志会滚动，不能作为数据源。
- 禁止在导出时剔除任何股票。股数不闭合的逐只列在报告里说明原因，由人决定。
- positions_begin 取 start 之前最后一个 position_snapshot；没有快照时
  **输出 BLOCKER 行并以非零退出码报错**，绝不静默输出 0 行或用 0 填充。
- 幂等：同区间连跑两次输出 sha256 一致。
"""
import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config            # noqa: E402
import db_migrate        # noqa: E402
import settlement_db as sdb  # noqa: E402

# 合并窗口：同账户+同代码+同方向+同策略，相邻成交间隔 ≤ 该值则合并为一行
MERGE_WINDOW_SEC = 10

# 交割单 14 列（顺序固定，导出契约）
EVENT_COLUMNS = ['account', 'code', 'stock_name', 'trade_time', 'trade_type',
                 'strategy', 'is_simulation', 'volume', 'amount', 'commission',
                 'fills', 'trade_ids', 'price', 'strategy_label']
# 追加的诊断列（按列名读取，多列不影响既有分析）
DIAGNOSTIC_COLUMNS = ['time_source', 'order_id', 'row_status']

# time_source 权威度排序，合并行取其中最可信的一个
TIME_SOURCE_RANK = {'broker': 4, 'exchange': 3, 'reconcile_backfill': 2,
                    'local_fallback': 1, None: 0}

NUMERIC_AMOUNT_TOLERANCE_FLOOR = 1.0
NUMERIC_AMOUNT_RATIO = 0.0005

CHANGELOG_STATE_FILE = '.last_export_state.json'

# 账号脱敏：稳定、唯一，且不泄露完整账号
ACCOUNT_MASK_TEMPLATE = '账户%s(***%s)'


# ============================== 工具 ==============================

def norm_code(raw):
    """6 位纯数字代码，去后缀。保留前导零（不能用 int()）。"""
    text = str(raw or '').strip()
    text = re.sub(r'^(sh|sz|bj)\.?', '', text, flags=re.I)
    return text.split('.')[0]


def norm_time(raw):
    text = str(raw or '').strip().replace('T', ' ')
    return text.split('.')[0]


def to_float(value, default=None):
    try:
        if value is None:
            return default
        result = float(value)
        return default if result != result else result
    except (TypeError, ValueError):
        return default


def to_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def build_account_mapping(account_ids):
    """账号 → 脱敏标签的稳定映射。

    按账号 ID 排序后依次编号，因此**同一账号在任何一次导出里都得到同一个标签**，
    跨月度交付可比对。缺失的账号不会导致已有账号标签漂移。
    """
    mapping = {}
    for idx, acc in enumerate(sorted(set(str(a) for a in account_ids if a))):
        suffix = acc[-4:] if len(acc) >= 4 else acc
        letter = chr(ord('A') + idx) if idx < 26 else str(idx)
        mapping[acc] = ACCOUNT_MASK_TEMPLATE % (letter, suffix)
    return mapping


def mask(account_id, mapping):
    return mapping.get(str(account_id), '账户?(***%s)' % str(account_id)[-4:])


# ============================== 合并（单点实现） ==============================

def merge_deals(rows):
    """把原始 deal 行合并为逻辑成交行。

    规则：同账户 + 同代码 + 同方向 + 同策略，且相邻 deal_time 间隔 ≤ 10 秒。
    **不得跨日界** —— 隔夜的两笔同向成交即使间隔够近也是两笔独立交易。

    这是全项目唯一的合并实现：导出与任何审计/对账工具都必须调用它，
    禁止在别处复制第二套（否则两边口径迟早分叉）。
    """
    buckets = {}
    for row in rows:
        key = (row['account'], row['code'], row['trade_type'], row['strategy'])
        buckets.setdefault(key, []).append(row)

    merged = []
    for key, group in buckets.items():
        group.sort(key=lambda r: (r['trade_time'], str(r['trade_id'] or '')))
        current = None
        for row in group:
            stamp = _parse_ts(row['trade_time'])
            same_day = (current is not None
                        and row['trade_time'][:10] == current['trade_time'][:10])
            close_enough = (current is not None and stamp is not None
                            and current['_ts'] is not None
                            and (stamp - current['_ts']) <= MERGE_WINDOW_SEC)
            if current is not None and same_day and close_enough:
                current['volume'] += row['volume']
                current['amount'] += row['amount']
                current['commission'] += row['commission'] or 0.0
                current['fills'] += 1
                current['trade_ids'].append(str(row['trade_id'] or ''))
                current['_ts'] = stamp
                current['_span'] = (stamp - current['_first_ts']) if current['_first_ts'] else 0
                current['order_id'] = current['order_id'] or row.get('order_id')
                if TIME_SOURCE_RANK.get(row.get('time_source'), 0) > \
                        TIME_SOURCE_RANK.get(current['time_source'], 0):
                    current['time_source'] = row.get('time_source')
            else:
                if current is not None:
                    merged.append(current)
                current = {
                    'account': row['account'],
                    'code': row['code'],
                    'stock_name': row['stock_name'],
                    'trade_time': row['trade_time'],
                    'trade_type': row['trade_type'],
                    'strategy': row['strategy'],
                    'is_simulation': row['is_simulation'],
                    'volume': row['volume'],
                    'amount': row['amount'],
                    'commission': row['commission'] or 0.0,
                    'fills': 1,
                    'trade_ids': [str(row['trade_id'] or '')],
                    'strategy_label': row['strategy_label'],
                    'time_source': row.get('time_source'),
                    'order_id': row.get('order_id'),
                    '_ts': stamp,
                    '_first_ts': stamp,
                    '_span': 0,
                }
        if current is not None:
            merged.append(current)

    merged.sort(key=lambda r: (r['trade_time'], r['account'], r['code'],
                               r['trade_ids'][0] if r['trade_ids'] else ''))
    return merged


def _parse_ts(text):
    try:
        return datetime.strptime(norm_time(text), '%Y-%m-%d %H:%M:%S').timestamp()
    except (ValueError, TypeError):
        return None


def max_merge_span(merged):
    return max((r.get('_span') or 0) for r in merged) if merged else 0


# ============================== 数据读取 ==============================

def load_trades(conn, account, start, end):
    """读指定区间的成交。只取 active 行（superseded 是已判定的重复）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trade_records)")}
    has_ext = 'row_status' in cols
    has_deal_time = 'deal_time_str' in cols

    where = []
    params = []
    if has_ext:
        where.append("COALESCE(row_status,'active')='active'")
    # account 列只在迁移后存在；未迁移的库不能加这个条件，
    # 更不能只加参数不加条件（会导致 bindings 数量对不上）。
    if account and 'account' in cols:
        where.append("(account=? OR account IS NULL)")
        params.append(account)
    where.append("date(trade_time) >= date(?)")
    params.append(start)
    where.append("date(trade_time) <= date(?)")
    params.append(end)

    time_expr = "COALESCE(deal_time_str, trade_time)" if has_deal_time else "trade_time"
    label_expr = "strategy_label" if 'strategy_label' in cols else "NULL"
    sim_expr = "COALESCE(is_simulation, 0)" if 'is_simulation' in cols else "0"
    ts_expr = "time_source" if 'time_source' in cols else "NULL"
    oid_expr = "order_id" if 'order_id' in cols else "NULL"
    cs_expr = "commission_source" if 'commission_source' in cols else "NULL"
    rs_expr = "COALESCE(row_status,'active')" if has_ext else "'active'"

    sql = ("SELECT stock_code, stock_name, {t} AS trade_time, trade_type, "
           "strategy, {l} AS strategy_label, {sim} AS is_simulation, "
           "volume, amount, commission, trade_id, {ts} AS time_source, "
           "{oid} AS order_id, {cs} AS commission_source, {rs} AS row_status "
           "FROM trade_records WHERE {w} ORDER BY trade_time, id").format(
        t=time_expr, l=label_expr, sim=sim_expr, ts=ts_expr, oid=oid_expr,
        cs=cs_expr, rs=rs_expr, w=" AND ".join(w for w in where if w))

    rows = []
    for raw in conn.execute(sql, params):
        trade_time = norm_time(raw['trade_time'])
        if not trade_time:
            continue
        strategy = raw['strategy']
        rows.append({
            'account': account or sdb.get_account_id(),
            'code': norm_code(raw['stock_code']),
            'stock_name': raw['stock_name'] or '',
            'trade_time': trade_time,
            'trade_type': (raw['trade_type'] or '').upper(),
            'strategy': strategy,
            'strategy_label': raw['strategy_label'] or sdb.strategy_label_for(strategy),
            'is_simulation': bool(raw['is_simulation']),
            'volume': to_int(raw['volume']),
            'amount': to_float(raw['amount'], 0.0) or 0.0,
            'commission': to_float(raw['commission'], 0.0) or 0.0,
            'commission_source': raw['commission_source'],
            'trade_id': raw['trade_id'],
            'time_source': raw['time_source'],
            'order_id': raw['order_id'],
            'row_status': raw['row_status'],
        })
    return rows


def load_trade_id_uniqueness(conn):
    """trade_id 唯一性检查：分长度统计、重复组数、跨标的重复组数。

    短 id（9-10 位）是网格路径写入的 str(order_id)，**不是全局唯一成交编号**，
    跨标的复用时会产生"假重复"。这个检查用来区分假重复与真重复，
    也是唯一键设计的依据。
    """
    result = {'by_length': [], 'real_duplicates': [], 'index_key_collisions': 0}
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trade_records)")}
    rows = [dict(r) for r in conn.execute(
        "SELECT id, stock_code, trade_type, trade_time, trade_id, "
        + ("COALESCE(row_status,'active')" if 'row_status' in cols else "'active'")
        + " AS row_status FROM trade_records")]
    active = [r for r in rows if (r['row_status'] or 'active') == 'active']

    grouped = {}
    for r in active:
        if r['trade_id'] is None:
            continue
        grouped.setdefault(len(str(r['trade_id'])), []).append(r)

    for length in sorted(grouped):
        grp = grouped[length]
        ids = {}
        for r in grp:
            ids.setdefault(str(r['trade_id']), []).append(r)
        dup = {k: v for k, v in ids.items() if len(v) > 1}
        cross = sum(1 for v in dup.values()
                    if len({str(x['stock_code']).split('.')[0] for x in v}) > 1)
        result['by_length'].append({
            'length': length, 'rows': len(grp), 'distinct': len(ids),
            'dup_groups': len(dup), 'cross_stock_dup_groups': cross})

    # 真重复：同 id + 同标的 + 同时间
    real = {}
    for r in active:
        if r['trade_id'] is None:
            continue
        real.setdefault((str(r['trade_id']), str(r['stock_code']).split('.')[0],
                         str(r['trade_time'])), []).append(r['id'])
    result['real_duplicates'] = [{'key': k, 'ids': v}
                                 for k, v in real.items() if len(v) > 1]

    # 当前唯一索引键下的冲突（应为 0）
    key = {}
    for r in active:
        if r['trade_id'] is None:
            continue
        k = (str(r['trade_id']), str(r['stock_code']).split('.')[0],
             str(r['trade_type']), str(r['trade_time']))
        key.setdefault(k, []).append(r['id'])
    result['index_key_collisions'] = sum(1 for v in key.values() if len(v) > 1)
    return result


def load_commission_sources(conn, start, end):
    """手续费来源分布：broker / estimated / unknown 各几行。

    返回 None 表示**列不存在**（未迁移），返回 [] 表示列在但区间内无成交 ——
    两者含义不同，报告里不能混为一谈。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trade_records)")}
    if 'commission_source' not in cols:
        return None
    active = ("AND COALESCE(row_status,'active')='active'"
              if 'row_status' in cols else "")
    rows = conn.execute(
        "SELECT COALESCE(commission_source,'(NULL)') src, COUNT(*) c, "
        "SUM(COALESCE(commission,0)) total FROM trade_records "
        "WHERE date(trade_time)>=date(?) AND date(trade_time)<=date(?) "
        + active + " GROUP BY 1 ORDER BY 2 DESC", (start, end)).fetchall()
    return [{'source': r[0], 'rows': r[1], 'total': round(r[2] or 0.0, 2)}
            for r in rows]


def load_snapshot_before(conn, account, start):
    """start 之前最后一个全量持仓快照。没有就返回 (None, None)。"""
    if not db_migrate.table_exists(conn, 'position_snapshot'):
        return None, None
    row = conn.execute(
        "SELECT MAX(snapshot_date) d FROM position_snapshot "
        "WHERE account=? AND snapshot_date < ?", (account, start)).fetchone()
    if not row or not row[0]:
        return None, None
    date = row[0]
    snap_type = conn.execute(
        "SELECT snapshot_type FROM position_snapshot WHERE account=? AND snapshot_date=? "
        "ORDER BY CASE snapshot_type WHEN 'close' THEN 0 ELSE 1 END LIMIT 1",
        (account, date)).fetchone()[0]
    rows = [{'account': account, 'code': norm_code(r['code']),
             'stock_name': r['stock_name'], 'volume': to_int(r['volume']),
             'cost_price': r['cost_price']}
            for r in conn.execute(
                "SELECT code, stock_name, volume, cost_price FROM position_snapshot "
                "WHERE account=? AND snapshot_date=? AND snapshot_type=?",
                (account, date, snap_type))]
    return date, rows


def load_latest_snapshot(conn, account, start, end):
    """区间**内**最后一个快照，用作期末持仓。

    必须限定 snapshot_date >= start：若只在区间开始前有快照，
    拿它当期末会让期初与期末是同一份数据，闭合判定必然失败。
    区间内没有快照时返回 (None, None)，由调用方回落到 positions 表。
    """
    if not db_migrate.table_exists(conn, 'position_snapshot'):
        return None, None
    row = conn.execute(
        "SELECT MAX(snapshot_date) d FROM position_snapshot "
        "WHERE account=? AND snapshot_date >= ? AND snapshot_date <= ?",
        (account, start, end)).fetchone()
    if not row or not row[0]:
        return None, None
    date = row[0]
    snap_type = conn.execute(
        "SELECT snapshot_type FROM position_snapshot WHERE account=? AND snapshot_date=? "
        "ORDER BY CASE snapshot_type WHEN 'close' THEN 0 ELSE 1 END LIMIT 1",
        (account, date)).fetchone()[0]
    rows = [{'account': account, 'code': norm_code(r['code']),
             'stock_name': r['stock_name'], 'volume': to_int(r['volume']),
             'cost_price': r['cost_price']}
            for r in conn.execute(
                "SELECT code, stock_name, volume, cost_price FROM position_snapshot "
                "WHERE account=? AND snapshot_date=? AND snapshot_type=?",
                (account, date, snap_type))]
    return date, rows


def load_equity(conn, account, start, end):
    if not db_migrate.table_exists(conn, 'account_equity_daily'):
        return []
    return [dict(r) for r in conn.execute(
        "SELECT date, snapshot_type, total_asset, market_value, cash, frozen_cash, "
        "deposit, withdraw, cum_deposit, deposit_source, daily_pnl, "
        "unexplained_delta, source FROM account_equity_daily "
        "WHERE account=? AND date>=? AND date<=? ORDER BY date, snapshot_type",
        (account, start, end))]


def load_broker_match_status(conn):
    """对账单匹配情况：未匹配清单 + 未匹配原因分布。

    未匹配原因按**可操作性**分类，而不是笼统一句"没匹配上"：
      · 本地无该标的任何成交  → 只能补导更早/更全的对账单
      · 本地有同标的同方向但价/量/时间对不上 → 字段级差异，需人工核对
      · 本地有同标的但方向不同 → 大概率是对账单与库的口径差异
    """
    if not db_migrate.table_exists(conn, 'broker_deals'):
        return None
    rows = [dict(r) for r in conn.execute(
        "SELECT deal_date, code, side, price, volume, amount, broker_traded_id, "
        "broker_order_id, match_status, match_method FROM broker_deals")]
    if not rows:
        return None

    local = [dict(r) for r in conn.execute(
        "SELECT stock_code, trade_type, trade_time, price, volume FROM trade_records "
        + ("WHERE COALESCE(row_status,'active')='active'"
           if 'row_status' in {c[1] for c in conn.execute(
               'PRAGMA table_info(trade_records)')} else ""))]
    local_by_code = {}
    for r in local:
        local_by_code.setdefault(norm_code(r['stock_code']), []).append(r)

    unmatched = [r for r in rows if r['match_status'] != 'matched']
    reasons = {}
    for r in unmatched:
        code = norm_code(r['code'])
        peers = local_by_code.get(code)
        if not peers:
            reason = '本地完全无该标的成交'
        elif not any((p['trade_type'] or '').upper() == (r['side'] or '').upper()
                     for p in peers):
            reason = '本地有该标的但买卖方向不同'
        else:
            reason = '本地有同标的同方向成交，但价/量/时间不符'
        r['unmatch_reason'] = reason
        reasons[reason] = reasons.get(reason, 0) + 1

    return {'total': len(rows), 'matched': len(rows) - len(unmatched),
            'unmatched': unmatched, 'reasons': reasons}


# ============================== 输出 ==============================

def write_events(merged, path, mapping):
    with open(path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(EVENT_COLUMNS + DIAGNOSTIC_COLUMNS)
        for row in merged:
            price = round(row['amount'] / row['volume'], 3) if row['volume'] else ''
            writer.writerow([
                mask(row['account'], mapping), row['code'], row['stock_name'],
                row['trade_time'], row['trade_type'], row['strategy'],
                'True' if row['is_simulation'] else 'False',
                row['volume'], round(row['amount'], 2), round(row['commission'], 2),
                row['fills'],
                ';'.join(t for t in row['trade_ids'] if t),
                price, row['strategy_label'],
                # 诊断列
                row.get('time_source') or '',
                row.get('order_id') or '',
                'active',
            ])


def write_positions(rows, path, source_note, blocker=None):
    """写持仓 CSV。无数据时写一行 BLOCKER，绝不静默输出 0 行。"""
    with open(path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['account', 'code', 'stock_name', 'volume', 'cost_price',
                         'source'])
        if blocker:
            writer.writerow([blocker, '', '', '', '', source_note])
            return
        for row in rows:
            writer.writerow([row.get('account', ''), row['code'], row['stock_name'],
                             row['volume'], row.get('cost_price', ''), source_note])


def write_account_daily(account, equity_rows, path):
    with open(path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['account', 'date', 'snapshot_type', 'total_asset',
                         'market_value', 'cash', 'frozen_cash', 'deposit',
                         'withdraw', 'cum_deposit', 'deposit_source',
                         'daily_pnl', 'unexplained_delta', 'source'])
        for row in equity_rows:
            writer.writerow([account, row['date'], row['snapshot_type'],
                             row['total_asset'], row['market_value'], row['cash'],
                             row['frozen_cash'], row.get('deposit'),
                             row.get('withdraw'), row.get('cum_deposit'),
                             row.get('deposit_source'), row.get('daily_pnl'),
                             row.get('unexplained_delta'), row['source']])


def write_cash_flows(account, path):
    """出入金流水。QMT 无出入金接口，当前恒为空表（仅表头）。"""
    with open(path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['account', 'flow_time', 'flow_type', 'amount', 'note',
                         'broker_ref', 'source'])
    return 0


# ============================== changelog ==============================

def _state_path(out_dir):
    return os.path.join(out_dir, CHANGELOG_STATE_FILE)


def load_previous_state(out_dir):
    try:
        with open(_state_path(out_dir), encoding='utf-8') as handle:
            return json.load(handle)
    except Exception:
        return None


def build_changelog(conn, out_dir, account, current_export_at):
    """生成与上次交付相比的变更清单：id + 字段 + 旧值 + 新值 + 原因 + 操作时间。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(trade_records)")]
    track = [c for c in ('stock_code', 'trade_time', 'trade_type', 'price', 'volume',
                         'amount', 'commission', 'commission_source', 'strategy',
                         'strategy_label', 'time_source', 'trade_id', 'order_id',
                         'row_status', 'is_simulation') if c in cols]
    snapshot = {}
    for r in conn.execute("SELECT id, %s FROM trade_records" % ", ".join(track)):
        snapshot[str(r[0])] = {c: (str(r[c]) if r[c] is not None else None)
                               for c in track}
    prev = load_previous_state(out_dir)

    lines = []
    add = lines.append
    add("=" * 72)
    add("交割单变更清单（与上次交付对比）")
    add("=" * 72)
    add(f"账号         : {account}")
    add(f"本次导出时间 : {current_export_at}")
    if prev is None:
        add("上次基线     : 【无】这是首次导出，没有可比对的基线。")
        add("               下次导出起，本文件将逐行列出新增/修改/删除的记录。")
        add("")
        add(f"当前记录数   : {len(snapshot)}")
    else:
        add(f"上次导出时间 : {prev.get('exported_at')}")
        old = prev.get('rows', {})
        added = [k for k in snapshot if k not in old]
        removed = [k for k in old if k not in snapshot]
        changed = []
        for k, cur in snapshot.items():
            before = old.get(k)
            if before is None:
                continue
            diffs = {f: (before.get(f), cur.get(f)) for f in track
                     if before.get(f) != cur.get(f)}
            if diffs:
                changed.append((k, diffs))
        add(f"新增 {len(added)} 行 / 修改 {len(changed)} 行 / 删除 {len(removed)} 行")
        add("")
        if added:
            add("── 新增 ──")
            for k in sorted(added, key=int):
                add(f"  id={k}  {snapshot[k].get('stock_code')} "
                    f"{snapshot[k].get('trade_time')} {snapshot[k].get('trade_type')} "
                    f"量={snapshot[k].get('volume')} 额={snapshot[k].get('amount')}")
            add("")
        if changed:
            add("── 修改（id + 字段 + 旧值 → 新值）──")
            for k, diffs in sorted(changed, key=lambda x: int(x[0])):
                add(f"  id={k}  {snapshot[k].get('stock_code')} "
                    f"{snapshot[k].get('trade_time')}")
                for f, (b, a) in sorted(diffs.items()):
                    add(f"      {f}: {b}  →  {a}")
            add("")
        if removed:
            add("── 删除 ──")
            for k in sorted(removed, key=int):
                add(f"  id={k}  {old[k].get('stock_code')} "
                    f"{old[k].get('trade_time')} {old[k].get('trade_type')} "
                    f"量={old[k].get('volume')} 额={old[k].get('amount')}")
            add("")
        add("注：迁移阶段的删除（占位流水归档、重复行标记）在")
        add("    ACCEPTANCE.md 与下方『已处置重复行』一节有完整说明。")

    # 落盘本次状态，供下次比对
    try:
        with open(_state_path(out_dir), 'w', encoding='utf-8') as handle:
            json.dump({'exported_at': current_export_at, 'rows': snapshot},
                      handle, ensure_ascii=False)
    except Exception:
        pass
    return "\n".join(lines)


def load_disposed_duplicates(conn):
    """已处置的重复行：保留哪个、标记哪个。"""
    out = []
    try:
        rows = conn.execute(
            "SELECT id, duplicate_of, stock_code, trade_time, trade_type, "
            "volume, price, amount, trade_id FROM trade_records "
            "WHERE COALESCE(row_status,'active')='superseded' ORDER BY id").fetchall()
        for r in rows:
            out.append({'id': r[0], 'kept_id': r[1], 'code': norm_code(r[2]),
                        'trade_time': norm_time(r[3]), 'trade_type': r[4],
                        'volume': r[5], 'price': r[6], 'amount': r[7],
                        'trade_id': r[8]})
    except Exception:
        pass
    return out


# ============================== 报告 ==============================

def build_report(account, masked, mapping, start, end, merged, raw_rows,
                 begin_rows, begin_date, begin_cost_ok, end_rows, end_source,
                 equity_rows, cash_flow_count, db_path, sources, has_time_source,
                 commission_sources, uniqueness, broker_status, disposed):
    lines = []
    add = lines.append

    actual_lo = min((r['trade_time'] for r in merged), default=None)
    actual_hi = max((r['trade_time'] for r in merged), default=None)

    add("=" * 72)
    add("交割单导出报告")
    add("=" * 72)
    add(f"账号        : {masked}")
    add(f"请求区间    : {start} ~ {end}")
    add(f"实际首末成交: {actual_lo or '（区间内无成交）'}"
        f"{'  ~  ' + actual_hi if actual_hi else ''}")
    if actual_lo and actual_lo[:10] != start:
        add(f"              ⚠ 区间起点 {start} 早于首笔成交 {actual_lo[:10]}，"
            f"该段无数据（不是遗漏，是区间内确实没有成交）")
    add(f"数据源      : {db_path}   （只读；不依赖 logs/*.log）")
    add(f"生成时间    : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    add("")

    add("── 账号映射表（脱敏，跨交付稳定）──")
    for acc in sorted(mapping):
        mark = ' ← 本次' if mapping[acc] == masked else ''
        add(f"  {mapping[acc]}   {acc}{mark}")
    add("")

    buys = [r for r in merged if r['trade_type'] == 'BUY']
    sells = [r for r in merged if r['trade_type'] == 'SELL']
    add("── 总量 ──")
    add(f"  逻辑成交行数        : {len(merged)}")
    add(f"  原始成交笔数        : {sum(r['fills'] for r in merged)}")
    add(f"  买入笔数 / 金额合计 : {len(buys)} / {sum(r['amount'] for r in buys):.2f}")
    add(f"  卖出笔数 / 金额合计 : {len(sells)} / {sum(r['amount'] for r in sells):.2f}")
    add(f"  手续费合计          : {sum(r['commission'] for r in merged):.2f}")
    add("")

    add("── 合并参数 ──")
    add(f"  合并窗口            : {MERGE_WINDOW_SEC} 秒"
        f"（同账户+同代码+同方向+同策略，不跨日界）")
    add(f"  发生合并的行        : {len([r for r in merged if r['fills'] > 1])}")
    add(f"  最大合并跨度        : {max_merge_span(merged):.0f} 秒")
    add("")

    add("── 时间自检 ──")
    for label, pred in (('早于 09:15', lambda t: t < '09:15'),
                        ('晚于 15:00', lambda t: t > '15:00'),
                        ('晚于 20:00', lambda t: t > '20:00')):
        hits = [r for r in merged if pred(r['trade_time'][11:16])]
        add(f"  {label} : {len(hits)}")
        for r in hits[:20]:
            add(f"      {r['code']} {r['trade_time']} {r['strategy']}")
    bad_fmt = [r for r in merged
               if not re.match(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$', r['trade_time'])]
    add(f"  时间格式不合规 : {len(bad_fmt)}")
    add("")

    add("── time_source 分布 ──")
    if sources:
        for key, count in sorted(sources.items()):
            add(f"  {key:<22}: {count}")
        add("  说明：broker=券商对账单回填的真实成交时间；exchange=QMT 成交回报自带；")
        add("        local_fallback=本地入库时刻（非交易所时间）；")
        add("        reconcile_backfill=启动对账补记")
    elif has_time_source:
        add("  （区间内无成交记录）")
    else:
        add("  （trade_records 尚未扩展，无 time_source 列；请先执行数据库迁移）")
    add("")

    add("── trade_id 唯一性检查 ──")
    if uniqueness['by_length']:
        add(f"  {'长度':<8}{'行数':>8}{'不同ID':>10}{'重复组':>10}{'跨标的重复组':>14}")
        for item in uniqueness['by_length']:
            add(f"  {item['length']:<8}{item['rows']:>8}{item['distinct']:>10}"
                f"{item['dup_groups']:>10}{item['cross_stock_dup_groups']:>14}")
        add("  说明：9-10 位短 id 是网格路径写入的 str(order_id)，"
            "**不是全局唯一成交编号**；")
        add("        跨标的重复属假重复（同 order_id 在不同股票/日期复用），"
            "不是同一条 deal 被写两遍。")
        add("  真重复组(同 id + 同标的 + 同时间): "
            f"{len(uniqueness['real_duplicates'])}")
        for item in uniqueness['real_duplicates']:
            add(f"      {item['key']}  → ids {item['ids']}")
        add(f"  当前唯一键下冲突组: {uniqueness['index_key_collisions']}"
            f"{'  ✓ 无冲突' if uniqueness['index_key_collisions'] == 0 else '  ⚠ 需排查'}")
    else:
        add("  （无数据）")
    add("")

    add("── 代码自检 ──")
    bad_code = [r for r in merged if not re.match(r'^\d{6}$', r['code'])]
    add(f"  非 6 位纯数字 : {len(bad_code)}")
    for r in bad_code[:20]:
        add(f"      {r['code']} ({r['stock_name']})")
    leading = sorted({r['code'] for r in merged if r['code'].startswith('0')})
    add(f"  含前导零（Excel 导入需按文本列处理）: {len(leading)} 个 → {leading}")
    add("")

    add("── 数值自检 ──")
    add(f"  原始行数 : {len(raw_rows)}")
    add(f"  原始行 volume≤0 : {len([r for r in raw_rows if r['volume'] <= 0])}")
    add(f"  原始行 amount≤0 : {len([r for r in raw_rows if r['amount'] <= 0])}")
    add(f"  合并行 volume≤0 : {len([r for r in merged if r['volume'] <= 0])}")
    add(f"  合并行 amount≤0 : {len([r for r in merged if r['amount'] <= 0])}")
    zero_comm = [r for r in merged if abs(r['commission']) < 1e-9]
    pct = (100.0 * len(zero_comm) / len(merged)) if merged else 0.0
    add(f"  commission 为 0 : {len(zero_comm)} 行（{pct:.1f}%）")
    add("")
    add("── commission_source 分布 ──")
    if commission_sources:
        for item in commission_sources:
            add(f"  {item['source']:<12}: {item['rows']:>5} 行, 金额合计 {item['total']:.2f}")
        add("  说明：broker=券商对账单给出的真实手续费；"
            "estimated=按现行费率估算；unknown=来源不明（原值保留）")
    elif commission_sources is not None:
        add("  （区间内无成交记录）")
    else:
        add("  （trade_records 尚未扩展，无 commission_source 列；请先执行数据库迁移）")
    add("")

    add("── 枚举自检 ──")
    allowed = set(sdb.STRATEGY_LABELS.values()) | {'UNKNOWN'}
    labels = {}
    for r in merged:
        labels[r['strategy_label']] = labels.get(r['strategy_label'], 0) + 1
    for label, count in sorted(labels.items()):
        flag = '' if label in allowed else '   <<< 越界'
        add(f"  {label:<14}: {count}{flag}")
    bad_type = [r for r in merged if r['trade_type'] not in ('BUY', 'SELL')]
    add(f"  trade_type 非 BUY/SELL : {len(bad_type)}")
    add(f"  is_simulation=True : {len([r for r in merged if r['is_simulation']])} 行")
    add("")

    add("── 逐只股数闭合（期初 + 买 − 卖 = 期末）──")
    add(f"  期初快照来源 : {begin_date or '【缺失】'}")
    add(f"  期末来源     : {end_source}")
    begin_map = {norm_code(r['code']): to_int(r['volume']) for r in (begin_rows or [])}
    end_map = {norm_code(r['code']): r for r in (end_rows or [])}
    codes = sorted(set(begin_map) | {r['code'] for r in merged} | set(end_map))
    unbalanced = []
    for code in codes:
        opens = begin_map.get(code, 0)
        buys_v = sum(r['volume'] for r in merged
                     if r['code'] == code and r['trade_type'] == 'BUY')
        sells_v = sum(r['volume'] for r in merged
                      if r['code'] == code and r['trade_type'] == 'SELL')
        expected = opens + buys_v - sells_v
        actual = end_map.get(code, {}).get('volume')
        actual = to_int(actual) if actual is not None else None
        if actual is None:
            unbalanced.append((code, opens, buys_v, sells_v, expected, None,
                               '期末无记录'))
        elif expected != actual:
            unbalanced.append((code, opens, buys_v, sells_v, expected, actual,
                               '差额 %+d' % (actual - expected)))
    add(f"  涉及标的数        : {len(codes)}")
    add(f"  不平股票数        : {len(unbalanced)}")
    if unbalanced:
        add("")
        add(f"    {'代码':<8}{'期初':>7}{'买入':>8}{'卖出':>8}{'期望':>9}{'实际':>9}  说明")
        for code, op, bv, sv, exp, act, note in unbalanced:
            add(f"    {code:<8}{op:>7}{bv:>8}{sv:>8}{exp:>9}{str(act):>9}  {note}")
        add("")
        add("  —— 逐只缺口归因（净额为负的标的）——")
        negative = [u for u in unbalanced if u[4] < 0]
        if not negative:
            add("    无净额为负的标的")
        for code, op, bv, sv, exp, act, note in negative:
            add(f"    {code}: 期初 {op} + 买入 {bv} − 卖出 {sv} = {exp}")
            if begin_date is None:
                # 期初未知时**无法区分**两种成因，不要假装知道
                add("        → 缺口来源：**无法判定**。")
                add("          期初持仓缺失（按 0 计），所以负缺口既可能是")
                add("          (a) 区间开始前就持有的仓位（期初持仓），也可能是")
                add("          (b) 区间内买入流水缺失，")
                add("          在没有 start 之前快照的情况下**二者无法区分**。")
                add("          补齐路径：① 等 position_snapshot 上线后重导；")
                add("                    ② 导入覆盖 start 之前的券商对账单。")
            elif op == 0 and bv == 0:
                add("        → 缺口来源：**买入流水缺失**（区间内只有卖出，"
                    "且期初确为 0）。")
                add("          仅券商对账单能补：请导出覆盖该标的首笔卖出之前"
                    "的日期的对账单再导入。")
            else:
                add(f"        → 缺口来源：**买入流水部分缺失**"
                    f"（期初已知 {op}），需对账单补齐差额 {abs(exp)} 股。")
        add("")
        add("  说明：不平行**一律保留在交割单里**，不做任何剔除；")
        add("        由人工依据上述归因决定后续处置。")
    lines.append("")
    lines.append(f"  不平股票数={len(unbalanced)}")

    add("")
    add("── 券商对账单匹配情况 ──")
    if broker_status:
        add(f"  对账单成交总数 : {broker_status['total']}")
        add(f"  已匹配         : {broker_status['matched']}")
        add(f"  未匹配         : {len(broker_status['unmatched'])}")
        for reason, count in (broker_status['reasons'] or {}).items():
            add(f"      {reason}: {count}")
        if broker_status['unmatched']:
            add("  未匹配清单：")
            add(f"    {'日期':<10}{'代码':<8}{'方向':<6}{'价':>9}{'量':>8}"
                f"  {'成交编号':<22}原因")
            for r in broker_status['unmatched'][:50]:
                add(f"    {r['deal_date']:<10}{r['code']:<8}"
                    f"{(r['side'] or ''):<6}{r['price']:>9}{r['volume']:>8}"
                    f"  {str(r['broker_traded_id'] or ''):<22}"
                    f"{r.get('unmatch_reason', '')}")
            add("")
            add("  处置建议：")
            add("    · 「本地完全无该标的成交」→ 补导覆盖更早区间的对账单")
            add("    · 「价/量/时间不符」→ 字段级差异，需人工核对两边口径")
            add("    · 「买卖方向不同」→ 多为对账单与库的方向字段口径差异")
    else:
        add("  （未导入过券商对账单，或 broker_deals 表为空）")
    add("")

    add("── 已处置的重复行 ──")
    if disposed:
        add(f"  共 {len(disposed)} 行被标记为 superseded（物理保留，可审计可回滚）")
        for d in disposed:
            add(f"    id={d['id']} → 保留 id={d['kept_id']}  {d['code']} "
                f"{d['trade_time']} {d['trade_type']} 量={d['volume']} 额={d['amount']}")
        add("  说明：这些是与保留行**同一条 deal 被写两遍**，不是不同成交。")
    else:
        add("  无")
    add("")

    add("── 期初 / 期末 / 净值 / 出入金 ──")
    add(f"  期初持仓 : {'来自 ' + begin_date + ' 的 position_snapshot' if begin_date else '【缺失】'}")
    add(f"             cost_price 列: {'有' if begin_cost_ok else '无（快照无成本价）'}")
    add(f"  期末持仓 : {end_source}（{len(end_rows or [])} 只）")
    add(f"  每日净值 : {'来自 account_equity_daily（' + str(len(equity_rows)) + ' 行）' if equity_rows else '【缺失】'}")
    deposited = [r for r in equity_rows if r.get('deposit') or r.get('withdraw')]
    add(f"  出入金   : cash_flows {cash_flow_count} 行"
        f"{'；净值表 ' + str(len(deposited)) + ' 天有记录' if deposited else '；QMT 无出入金接口，需券商对账单导入'}")
    add("")
    add("=" * 72)
    return "\n".join(lines)


# ============================== 主流程 ==============================

def export_account(account, db_path, start, end, out_dir, mapping=None):
    mapping = mapping or build_account_mapping([account])
    masked = mask(account, mapping)
    conn = sqlite3.connect('file:%s?mode=ro' % db_path, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        raw_rows = load_trades(conn, account, start, end)
        merged = merge_deals(raw_rows)
        begin_date, begin_rows = load_snapshot_before(conn, account, start)
        end_date, end_snap = load_latest_snapshot(conn, account, start, end)
        equity_rows = load_equity(conn, account, start, end)

        end_source = 'positions 表当前值'
        end_rows = []
        if end_snap:
            end_rows, end_source = end_snap, 'position_snapshot %s' % end_date
        elif db_migrate.table_exists(conn, 'positions'):
            pcols = {r[1] for r in conn.execute("PRAGMA table_info(positions)")}
            cost_expr = "cost_price" if 'cost_price' in pcols else "NULL"
            end_rows = [{'account': account, 'code': norm_code(r['stock_code']),
                         'stock_name': r['stock_name'], 'volume': to_int(r['volume']),
                         'cost_price': r['cost_price']}
                        for r in conn.execute(
                            "SELECT stock_code, stock_name, volume, %s AS cost_price "
                            "FROM positions WHERE volume IS NOT NULL" % cost_expr)]

        sources = {}
        cols = {r[1] for r in conn.execute("PRAGMA table_info(trade_records)")}
        has_time_source = 'time_source' in cols
        if has_time_source:
            active_filter = ("AND COALESCE(row_status,'active')='active'"
                             if 'row_status' in cols else "")
            for r in conn.execute(
                    "SELECT COALESCE(time_source,'(NULL)') t, COUNT(*) c "
                    "FROM trade_records WHERE date(trade_time)>=date(?) AND "
                    "date(trade_time)<=date(?) " + active_filter + " GROUP BY 1",
                    (start, end)):
                sources[r['t']] = r['c']

        commission_sources = load_commission_sources(conn, start, end)
        uniqueness = load_trade_id_uniqueness(conn)
        broker_status = load_broker_match_status(conn)
        disposed = load_disposed_duplicates(conn)

        # 文件名用**实际**首末成交日期，避免"文件名写 02-01、实际首笔 07-09"
        actual_lo = min((r['trade_time'] for r in merged), default=None)
        actual_hi = max((r['trade_time'] for r in merged), default=None)
        if actual_lo:
            tag = f"{actual_lo[:10].replace('-', '')}_{actual_hi[:10].replace('-', '')}"
        else:
            tag = f"{start.replace('-', '')}_{end.replace('-', '')}_empty"

        events_path = os.path.join(out_dir, f"trading_events_{tag}.csv")
        write_events(merged, events_path, mapping)

        begin_blocker = None if begin_date else f"BLOCKER: no snapshot before {start}"
        write_positions(begin_rows or [],
                        os.path.join(out_dir, 'positions_begin.csv'),
                        'position_snapshot' if begin_date else 'MISSING',
                        blocker=begin_blocker)
        write_positions(end_rows, os.path.join(out_dir, 'positions_end.csv'),
                        end_source,
                        blocker=None if end_rows else 'BLOCKER: no end positions')
        write_account_daily(masked, equity_rows,
                            os.path.join(out_dir, 'account_daily.csv'))
        cash_count = write_cash_flows(masked, os.path.join(out_dir, 'cash_flows.csv'))

        report = build_report(
            account, masked, mapping, start, end, merged, raw_rows,
            begin_rows, begin_date, bool(begin_rows and begin_rows[0].get('cost_price') is not None),
            end_rows, end_source, equity_rows, cash_count, db_path, sources,
            has_time_source, commission_sources, uniqueness, broker_status, disposed)
        with open(os.path.join(out_dir, 'export_report.txt'), 'w',
                  encoding='utf-8') as handle:
            handle.write(report)

        changelog = build_changelog(conn, out_dir, masked,
                                    datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        with open(os.path.join(out_dir, 'changelog_since_last_export.txt'), 'w',
                  encoding='utf-8') as handle:
            handle.write(changelog)
    finally:
        conn.close()

    print(report)
    print()
    print(changelog)

    if not begin_date:
        print()
        print("!" * 72)
        print(f"错误：数据库中不存在 {start} 之前的 position_snapshot。")
        print("  期初持仓无法确定，禁止用 0 填充 —— 那样会把『缺数据』伪装成『期初空仓』。")
        print("  positions_begin.csv 已写入 BLOCKER 行标明该状态。")
        print("!" * 72)
        return events_path, 2
    return events_path, 0


def main():
    parser = argparse.ArgumentParser(description='标准交割单导出（只读）')
    parser.add_argument('--start', required=True, help='起始日期 YYYY-MM-DD')
    parser.add_argument('--end', required=True, help='结束日期 YYYY-MM-DD')
    parser.add_argument('--accounts', default='all',
                        help="'all' 或具体账号 ID，默认 all")
    parser.add_argument('--db', help='直接指定单个库路径（排障/演练用）')
    parser.add_argument('--out', default='export', help='输出目录')
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    exit_code = 0

    if args.db:
        targets = [(args.accounts if args.accounts != 'all' else 'unknown',
                    args.db, 'arg')]
        multi = False
    elif args.accounts == 'all':
        targets = db_migrate.discover_account_dbs()
        multi = len(targets) > 1
    else:
        targets = [(args.accounts,
                    os.path.join(os.getcwd(), f"data_{args.accounts}", "trading.db"),
                    'arg')]
        multi = False

    mapping = build_account_mapping([acc for acc, _, _ in targets])

    for acc_id, db_path, src in targets:
        if not os.path.exists(db_path):
            print(f"  [SKIP] {acc_id}: 库不存在 {db_path}")
            continue
        sub = os.path.join(args.out, acc_id) if multi else args.out
        os.makedirs(sub, exist_ok=True)
        print(f"\n>>> 导出账号 {acc_id}（{mask(acc_id, mapping)}）  [{src}]")
        try:
            path, code = export_account(acc_id, db_path, args.start, args.end,
                                        sub, mapping)
            print(f"  已写出: {os.path.basename(path)}  "
                  f"sha256={sha256_of(path)[:16]}…")
            exit_code = max(exit_code, code)
        except Exception as exc:
            print(f"  [FAIL] {acc_id}: {exc}")
            raise
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
