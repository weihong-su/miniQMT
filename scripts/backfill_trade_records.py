# -*- coding: utf-8 -*-
"""
trade_records 历史回填。

用途：把改造前落库的历史行补齐归因字段。

回填规则（刻意保守，**不猜**）：
- account          : 按库路径推导（data_<id>/trading.db → <id>）
- strategy_label   : 按 strategy 映射；映射不到的写 'UNKNOWN'，不留空
- time_source      : 一律 'local_fallback' —— 历史 trade_time 是本地入库时刻，
                     **绝不伪装成 'exchange'**。导入券商对账单后由
                    import_broker_statement.py 升级为 'broker'
- commission       : 缺失或为 0 时按现行税费估算，标 commission_source='estimated'；
                     **不会覆盖已有的非零值**（可能是真实值）
- fills / fill_ids : 置 1 / 自身 trade_id（库里存的是原始 deal 粒度）
- trade_id_source : 按 trade_id 形态分类

幂等：重复执行不会改变已经是目标状态的行；输出前后行数对比。
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config            # noqa: E402
import db_migrate        # noqa: E402
import settlement_db as sdb  # noqa: E402


# 旧版代码写死的估算费率（trading_executor 与 position_manager 里的字面量）。
# 用它来判定某个非零手续费到底是"可证实的旧估算"还是"来源不明的真实值"。
LEGACY_ESTIMATE_RATE = 0.0003
LEGACY_ESTIMATE_TOLERANCE = 1e-6


def classify_commission(amount, trade_type, commission):
    """判定一条已有手续费的来源与应采取的处置。

    返回 (new_commission, commission_source, commission_rate, should_update)。

    四种情形：
    - 值为 0/NULL          → 按现行税费估算，标 estimated
    - 值 == 现行费率估算值   → 已经是本脚本写的（**保证重复运行幂等**），标 estimated
    - 值 == amount×0.0003  → **可证实**是旧版写死的估算（卖出还漏了印花税），
                             用现行费率重算并标 estimated
    - 其它非零值            → 来源不可知，**一律不动**，标 unknown

    先判定再计算，避免"非零就不动"把已知错误的旧估算也保留下来。
    """
    amount = float(amount or 0)
    current = estimate_commission(amount, trade_type)
    rate_label = commission_rate_label(trade_type)

    if commission is None or abs(float(commission)) < 1e-12:
        return (current, 'estimated', rate_label, True)

    value = float(commission)
    if amount > 0 and abs(value - current) < LEGACY_ESTIMATE_TOLERANCE:
        return (value, 'estimated', rate_label, True)

    if amount > 0 and abs(value - amount * LEGACY_ESTIMATE_RATE) < LEGACY_ESTIMATE_TOLERANCE:
        return (current, 'estimated', rate_label, True)

    return (value, 'unknown', None, True)


def estimate_commission(amount, trade_type):
    """按现行税费估算手续费。买入无印花税。"""
    amount = float(amount or 0)
    if amount <= 0:
        return 0.0
    rate = (config.SETTLEMENT_COMMISSION_RATE
            + config.SETTLEMENT_TRANSFER_FEE_RATE)
    if str(trade_type).upper() == 'SELL':
        rate += config.SETTLEMENT_STAMP_DUTY_RATE
    return round(amount * rate, 4)


def describe_plan(conn, account):
    """统计将要回填的内容，不改库。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trade_records)")}
    missing = [c for c in ('account', 'strategy_label', 'time_source',
                           'commission_source', 'fills', 'trade_id_source',
                           'recorded_at') if c not in cols]
    if missing:
        return None, missing

    plan = {'total': 0}
    for r in conn.execute("SELECT COUNT(*) FROM trade_records"):
        plan['total'] = r[0]

    def count(sql, params=()):
        return conn.execute(sql, params).fetchone()[0]

    plan['account_null'] = count(
        "SELECT COUNT(*) FROM trade_records WHERE account IS NULL")
    plan['label_null'] = count(
        "SELECT COUNT(*) FROM trade_records WHERE strategy_label IS NULL")
    plan['label_unknown'] = count(
        "SELECT COUNT(*) FROM trade_records WHERE strategy_label='UNKNOWN'")
    plan['time_source_null'] = count(
        "SELECT COUNT(*) FROM trade_records WHERE time_source IS NULL")
    plan['comm_source_null'] = count(
        "SELECT COUNT(*) FROM trade_records WHERE commission_source IS NULL")
    plan['comm_zero'] = count(
        "SELECT COUNT(*) FROM trade_records WHERE COALESCE(commission,0)=0")
    plan['fills_null'] = count(
        "SELECT COUNT(*) FROM trade_records WHERE fills IS NULL")
    plan['recorded_null'] = count(
        "SELECT COUNT(*) FROM trade_records WHERE recorded_at IS NULL")
    plan['id_source_null'] = count(
        "SELECT COUNT(*) FROM trade_records WHERE trade_id_source IS NULL")
    return plan, []


def classify_trade_id_source(trade_id):
    text = str(trade_id or '')
    if text.startswith('ORDER_'):
        return 'placeholder'
    if text.upper().startswith('SIM_'):
        return 'sim_trade_id'
    if text.isdigit() and 8 <= len(text) <= 12:
        return 'order_id'
    if text.isdigit() and len(text) >= 18:
        return 'traded_id'
    return 'unknown' if text else None


def backfill_account(conn, account):
    if not account:
        return 0
    cur = conn.execute(
        "UPDATE trade_records SET account=? WHERE account IS NULL", (account,))
    return cur.rowcount or 0


def backfill_strategy_label(conn):
    """按映射写 strategy_label。映射不到的写 UNKNOWN —— 不留空、不猜。"""
    updated = 0
    rows = conn.execute(
        "SELECT DISTINCT strategy FROM trade_records "
        "WHERE strategy_label IS NULL").fetchall()
    for (strategy,) in rows:
        label = sdb.strategy_label_for(strategy)
        cur = conn.execute(
            "UPDATE trade_records SET strategy_label=?, strategy_code=? "
            "WHERE strategy_label IS NULL AND COALESCE(strategy,'')=?",
            (label, strategy, strategy or ''))
        updated += cur.rowcount or 0
    return updated


def backfill_time_source(conn):
    """历史行的 trade_time 是本地入库时刻，一律标 local_fallback。

    **绝不标 exchange** —— 那会把"不知道成交时间"伪装成"成交时间准确"，
    恰恰是本次改造要根治的问题。
    """
    cur = conn.execute(
        "UPDATE trade_records SET time_source=? "
        "WHERE time_source IS NULL", (sdb.TIME_SOURCE_LOCAL,))
    return cur.rowcount or 0


def backfill_commission(conn):
    """按现行税费回填手续费来源与金额。

    已经是 broker 来源的行不动（对账单导入的成果不能被覆盖）。
    其它行按 classify_commission 的判定处理，整体幂等。
    """
    updated = 0
    rows = conn.execute(
        "SELECT id, amount, trade_type, commission FROM trade_records "
        "WHERE COALESCE(commission_source,'') <> 'broker'").fetchall()
    for row_id, amount, trade_type, commission in rows:
        value, source, rate, _ = classify_commission(amount, trade_type, commission)
        cur = conn.execute(
            "UPDATE trade_records SET commission=?, commission_source=?, "
            "commission_rate=? WHERE id=? AND COALESCE(commission_source,'')<>'broker'",
            (value, source, rate, row_id))
        if cur.rowcount:
            updated += 1
    return updated


def commission_rate_label(trade_type):
    rate = config.SETTLEMENT_COMMISSION_RATE + config.SETTLEMENT_TRANSFER_FEE_RATE
    if str(trade_type).upper() == 'SELL':
        rate += config.SETTLEMENT_STAMP_DUTY_RATE
    return '%.5f' % rate


def backfill_fills(conn):
    """历史库存的是原始 deal 粒度，fills 恒为 1、fill_ids 即本行 trade_id。

    注意不能只判 `fills IS NULL` —— 迁移时给的 DEFAULT 1 已经填好了 fills，
    但 fill_ids 仍为空，必须一并补上。
    """
    cur = conn.execute(
        "UPDATE trade_records SET fills=1 WHERE fills IS NULL OR fills=0")
    updated = cur.rowcount or 0
    cur = conn.execute(
        "UPDATE trade_records SET fill_ids=trade_id WHERE fill_ids IS NULL")
    return updated + (cur.rowcount or 0)


def backfill_recorded_at(conn, fallback_time_col='trade_time'):
    cur = conn.execute(
        f"UPDATE trade_records SET recorded_at=trade_time WHERE recorded_at IS NULL")
    return cur.rowcount or 0


def backfill_order_id(conn):
    """把网格路径误写进 trade_id 的 order_id 回填到专属列。

    网格落库时 trade_id = str(order_id)，两者本是一回事。
    回填 order_id 让 deal 键能把「委托」与「成交编号」分开表达 ——
    trade_id 的语义混杂正是短 id 跨标的"假重复"的根源。
    """
    cur = conn.execute(
        "UPDATE trade_records SET order_id=trade_id "
        "WHERE order_id IS NULL AND trade_id_source='order_id'")
    updated = cur.rowcount or 0
    # 真实成交编号的 20 位行没有 order_id 可用，保持 NULL（不猜）
    return updated


def backfill_trade_id_source(conn):
    updated = 0
    for row_id, trade_id in conn.execute(
            "SELECT id, trade_id FROM trade_records WHERE trade_id_source IS NULL"):
        conn.execute("UPDATE trade_records SET trade_id_source=? WHERE id=?",
                     (classify_trade_id_source(trade_id), row_id))
        updated += 1
    return updated


def backfill_is_simulation(conn):
    """按 trade_id 前缀 / strategy 推断并回填 is_simulation。

    迁移给该列加了 DEFAULT 0，历史模拟单因此全部被标成实盘 ——
    下游只要按 is_simulation 过滤，模拟成交就会混进实盘归因。
    """
    updated = 0
    for row_id, trade_id, strategy in conn.execute(
            "SELECT id, trade_id, strategy FROM trade_records"):
        flag = 1 if sdb.is_simulation_trade(trade_id, strategy) else 0
        cur = conn.execute(
            "UPDATE trade_records SET is_simulation=? WHERE id=? AND "
            "COALESCE(is_simulation,0) <> ?", (flag, row_id, flag))
        updated += cur.rowcount or 0
    return updated


def find_simulated_rows(conn):
    """列出**当前标志已标记**为模拟的实盘表行。"""
    return [dict(r) for r in conn.execute(
        "SELECT id, stock_code, trade_time, trade_id, strategy FROM trade_records "
        "WHERE COALESCE(is_simulation,0)=1")]


def predict_simulated_rows(conn):
    """按推断规则预测**将被**标记为模拟的行。

    dry-run 必须用这个而不是 find_simulated_rows：迁移给 is_simulation 的
    DEFAULT 全是 0，按当前标志统计只会得到 0，操作员会误以为没有模拟单要迁移。
    预测必须与实跑使用同一套推断规则（sdb.is_simulation_trade）。
    """
    return [dict(r) for r in conn.execute(
        "SELECT id, stock_code, trade_time, trade_id, strategy FROM trade_records")
        if sdb.is_simulation_trade(r['trade_id'], r['strategy'])]


def relocate_simulated_rows(conn):
    """把误落在实盘表的模拟成交迁到 trade_records_sim。

    规格要求模拟单**物理隔离**。只把 is_simulation 改成 1 还不够 ——
    行还留在 trade_records 里，任何不做过滤的查询照样会把模拟单算进实盘。

    返回迁移行数。幂等：迁完源表里就没有 is_simulation=1 的行了。
    """
    if not db_migrate.table_exists(conn, 'trade_records_sim'):
        return 0
    src_cols = [r[1] for r in conn.execute("PRAGMA table_info(trade_records)")]
    dst_cols = [r[1] for r in conn.execute("PRAGMA table_info(trade_records_sim)")]
    shared = [c for c in src_cols if c in dst_cols and c != 'id']
    if not shared:
        return 0
    col_list = ', '.join(shared)
    cur = conn.execute(
        f"INSERT OR IGNORE INTO trade_records_sim({col_list}) "
        f"SELECT {col_list} FROM trade_records WHERE COALESCE(is_simulation,0)=1")
    moved = cur.rowcount or 0
    conn.execute("DELETE FROM trade_records WHERE COALESCE(is_simulation,0)=1")
    return moved


def run_backfill(db_path, account, dry_run=False):
    conn = sqlite3.connect(db_path, timeout=60.0)
    conn.row_factory = sqlite3.Row
    try:
        before, missing = describe_plan(conn, account)
        if missing:
            return {'db_path': db_path, 'error': f"缺少扩展列: {missing}",
                    'before': None, 'after': None}

        result = {'db_path': db_path, 'account': account, 'before': before,
                  'dry_run': dry_run}
        if dry_run:
            conn.rollback()
            result['after'] = before
            result['plan'] = {
                'account': before['account_null'],
                'strategy_label': before['label_null'],
                'time_source': before['time_source_null'],
                'commission': before['comm_zero'] + before['comm_source_null'],
                'fills': before['fills_null'],
                'recorded_at': before['recorded_null'],
                'trade_id_source': before['id_source_null'],
                'is_simulation': len(predict_simulated_rows(conn)),
            }
            return result

        # is_simulation 必须先回填，relocate 依赖它做筛选
        sim_rows_before = len(find_simulated_rows(conn))
        stats = {
            'account': backfill_account(conn, account),
            'strategy_label': backfill_strategy_label(conn),
            'time_source': backfill_time_source(conn),
            'commission': backfill_commission(conn),
            'fills': backfill_fills(conn),
            'recorded_at': backfill_recorded_at(conn),
            'trade_id_source': backfill_trade_id_source(conn),
            # 依赖上一步的判定结果，顺序不能颠倒
            'order_id': backfill_order_id(conn),
            'is_simulation': backfill_is_simulation(conn),
        }
        # 规格要求模拟单物理隔离：只改标志不够，行还留在实盘表里，
        # 任何不做过滤的查询照样会把模拟单算进实盘。
        stats['relocated_to_sim'] = relocate_simulated_rows(conn)
        conn.commit()
        after, _ = describe_plan(conn, account)
        result['after'] = after
        result['stats'] = stats
        result['sim_rows_before'] = sim_rows_before
        return result
    finally:
        conn.close()


def print_report(result):
    print(f"  库路径: {result['db_path']}")
    if result.get('error'):
        print(f"  [SKIP] {result['error']}")
        return
    before, after = result['before'], result['after']
    print(f"  账号: {result['account'] or '(推不出)'}")

    moved = (result.get('stats') or {}).get('relocated_to_sim', 0)
    expected_delta = -moved
    actual_delta = after['total'] - before['total']
    verdict = ''
    if actual_delta != expected_delta:
        verdict = (f"   <<< 行数变化异常！期望 {expected_delta}（迁出模拟单 {moved} 行），"
                   f"实际 {actual_delta}")
    elif moved:
        verdict = f"   （-{moved} 行为迁出的模拟单，非丢失）"
    print(f"  总行数: {before['total']} -> {after['total']}{verdict}")

    fields = [('account', 'account_null'), ('strategy_label', 'label_null'),
              ('time_source', 'time_source_null'),
              ('commission_source', 'comm_source_null'),
              ('fills', 'fills_null'), ('recorded_at', 'recorded_null'),
              ('trade_id_source', 'id_source_null')]
    print(f"  {'字段':<20}{'回填前空值':>10}{'回填后空值':>12}")
    for label, key in fields:
        print(f"  {label:<20}{before.get(key, 0):>10}{after.get(key, 0):>12}")
    if result.get('dry_run'):
        plan = result.get('plan') or {}
        print(f"  待回填 is_simulation=1 的行: {plan.get('is_simulation', 0)}"
              f"（将迁往 trade_records_sim）")
    if 'stats' in result:
        print(f"  本次写入: {result['stats']}")
        if moved:
            print(f"  → 已把 {moved} 行模拟成交迁到 trade_records_sim（规格要求物理隔离）")
    if not result.get('dry_run'):
        print(f"  strategy_label=UNKNOWN 的行: {after.get('label_unknown', 0)}"
              f"（映射不到的值，按约定不猜）")


def main():
    parser = argparse.ArgumentParser(description='trade_records 历史回填')
    parser.add_argument('--accounts', default='all', help="'all' 或具体账号")
    parser.add_argument('--db', help='直接指定库路径（演练用）')
    parser.add_argument('--dry-run', action='store_true', help='只报告，不写库')
    args = parser.parse_args()

    print("=" * 70)
    print("trade_records 历史回填")
    print("=" * 70)
    if args.dry_run:
        print(">>> DRY RUN —— 不会写入任何数据 <<<")
    print()

    if args.db:
        targets = [(args.accounts if args.accounts != 'all' else None, args.db)]
    else:
        targets = [(acc, path) for acc, path, _ in db_migrate.discover_account_dbs()
                   if args.accounts == 'all' or acc == args.accounts]

    for account, db_path in targets:
        if not os.path.exists(db_path):
            print(f"[SKIP] {db_path} 不存在")
            continue
        acc = account or db_migrate.derive_account_id(db_path)
        print(f"─── {acc or db_path} ───")
        print_report(run_backfill(db_path, acc, dry_run=args.dry_run))
        print()

    print("=" * 70)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
