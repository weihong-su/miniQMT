# -*- coding: utf-8 -*-
"""
数据库 schema 迁移工具。

设计约束（见改造计划「通用铁律」）：
- 只用 ALTER TABLE ... ADD COLUMN，幂等；禁止对生产库 DROP/rebuild。
- 迁移前自动备份，备份落在 data/backup/migrations/ 下 —— 不与 test_base
  的备份混在 data/ 根目录（那里已堆了 280 个 trading.db.backup_*）。
- 破坏性操作必须先过 assert_test_db()。

本模块只负责「表结构」，不含业务写入逻辑（那在 settlement_db.py）。
"""
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime

import config
from logger import get_logger

logger = get_logger('db_migrate')

# 迁移备份保留份数，超出按时间删最老的
MIGRATION_BACKUP_KEEP = 10


# ============================== 守卫 ==============================

def is_test_db(db_path):
    """判断是否测试库。

    仓库里原本没有这个判断（生产代码 0 处按文件名判库），
    但 _rebuild_table 这类破坏性逻辑必须靠它兜住。
    """
    if not db_path:
        return False
    path = str(db_path)
    if path == ':memory:':
        return True
    return 'test' in os.path.basename(path).lower()


def assert_test_db(db_path, operation):
    """破坏性操作守卫：不是测试库就直接抛错，不给「继续执行」的余地。"""
    if not is_test_db(db_path):
        raise RuntimeError(
            f"拒绝在生产库上执行 [{operation}]: {db_path}\n"
            f"该操作仅允许在文件名含 'test' 的库或 :memory: 上执行。"
        )


# ============================== 备份 ==============================

def backup_db(db_path=None, reason='migration'):
    """迁移前备份数据库。

    先做 WAL checkpoint —— 两个生产库都是 WAL 模式，不 checkpoint 的话
    最近的写入还在 -wal 文件里，直接 copy 主库文件会丢数据。
    """
    db_path = db_path or config.DB_PATH
    if not os.path.exists(db_path):
        logger.info(f"数据库不存在，跳过备份: {db_path}")
        return None

    try:
        conn = sqlite3.connect(db_path, timeout=30.0)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"WAL checkpoint 失败（仍继续备份，但可能丢最近写入）: {e}")

    backup_dir = os.path.join(os.path.dirname(db_path) or '.', 'backup', 'migrations')
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    name = f"{os.path.basename(db_path)}.{reason}_{stamp}"
    dest = os.path.join(backup_dir, name)

    shutil.copy2(db_path, dest)
    logger.info(f"迁移前备份完成: {dest}")
    _prune_backups(backup_dir, os.path.basename(db_path))
    return dest


def _prune_backups(backup_dir, db_filename):
    """只保留最近 MIGRATION_BACKUP_KEEP 份，避免重演 data/ 目录堆积 280 个备份。"""
    try:
        items = [f for f in os.listdir(backup_dir) if f.startswith(db_filename + '.')]
        items.sort(reverse=True)
        for stale in items[MIGRATION_BACKUP_KEEP:]:
            os.remove(os.path.join(backup_dir, stale))
            logger.debug(f"清理旧迁移备份: {stale}")
    except Exception as e:
        logger.warning(f"清理旧备份失败: {e}")


# ============================== 幂等 DDL ==============================

def ensure_column(conn, table, column, typedef):
    """幂等补列，返回 True 表示本次新增。

    泛化自 data_manager._migrate_legacy_schema()（那里把表名写死成 positions）。
    表名与列名均为代码内常量，不来自外部输入。
    """
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(%s)" % table)
    existing = {row[1] for row in cursor.fetchall()}
    if column in existing:
        return False
    cursor.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, typedef))
    logger.info(f"DB迁移: {table} 补齐字段 {column} {typedef}")
    return True


def ensure_columns(conn, table, migrations):
    """批量补列，migrations 为 [(列名, 类型定义), ...]，返回新增的列名列表。"""
    added = []
    for column, typedef in migrations:
        if ensure_column(conn, table, column, typedef):
            added.append(column)
    return added


def table_exists(conn, table):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


# ============================== 新表 DDL ==============================

# 持仓快照：positions 表是「当前持仓」会被覆盖写，不能当历史用。
# 每交易日 09:25(open) 与 15:05(close) 各一份全量。
DDL_POSITION_SNAPSHOT = '''
CREATE TABLE IF NOT EXISTS position_snapshot (
    account TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    snapshot_type TEXT NOT NULL,
    code TEXT NOT NULL,
    stock_name TEXT,
    volume REAL,
    available REAL,
    cost_price REAL,
    base_cost_price REAL,
    current_price REAL,
    market_value REAL,
    profit_ratio REAL,
    source TEXT,
    recorded_at TEXT,
    PRIMARY KEY (account, snapshot_date, code, snapshot_type)
)
'''

# 每日净值。字段按「能拿到什么就存什么」：
# QMT 的 XtAsset 只有 cash/frozen_cash/market_value/total_asset 四个数值字段，
# 没有 available（它与 cash 是同一个数），更没有任何出入金接口。
# deposit/withdraw/cum_deposit 只能由 cash_flow 导入或人工填报回填，默认 NULL。
DDL_ACCOUNT_EQUITY_DAILY = '''
CREATE TABLE IF NOT EXISTS account_equity_daily (
    account TEXT NOT NULL,
    date TEXT NOT NULL,
    snapshot_type TEXT NOT NULL,
    total_asset REAL,
    market_value REAL,
    cash REAL,
    frozen_cash REAL,
    deposit REAL,
    withdraw REAL,
    cum_deposit REAL,
    deposit_source TEXT,
    daily_pnl REAL,
    unexplained_delta REAL,
    source TEXT,
    recorded_at TEXT,
    PRIMARY KEY (account, date, snapshot_type)
)
'''

# 模拟成交独立表：模拟单不得与实盘同表（否则归因时极易污染）。
# 列结构与 trade_records 完全一致，便于同一套 SQL 复用。
DDL_TRADE_RECORDS_SIM = '''
CREATE TABLE IF NOT EXISTS trade_records_sim (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT,
    stock_code TEXT,
    stock_name TEXT,
    trade_time TIMESTAMP,
    trade_type TEXT,
    price REAL,
    volume INTEGER,
    amount REAL,
    trade_id TEXT,
    commission REAL,
    strategy TEXT,
    deal_time INTEGER,
    deal_time_str TEXT,
    recorded_at TIMESTAMP,
    time_source TEXT,
    order_id TEXT,
    fill_ids TEXT,
    fills INTEGER DEFAULT 1,
    strategy_code TEXT,
    strategy_label TEXT,
    is_simulation INTEGER DEFAULT 1,
    commission_source TEXT,
    commission_rate TEXT,
    side_source TEXT,
    row_status TEXT DEFAULT 'active',
    duplicate_of INTEGER,
    trade_id_source TEXT
)
'''

# 券商对账单原始成交。导入后用于回填真实成交时间与真实手续费 ——
# 这是历史成交时间的**唯一**来源：QMT 的 xttrader 没有任何历史成交查询接口
# （query_stock_trades 只返回当日），数据库里既成的 time_source='local_fallback'
# 记录只能靠它升级为 'broker'。
DDL_BROKER_DEALS = '''
CREATE TABLE IF NOT EXISTS broker_deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT,
    deal_date TEXT,
    deal_time TEXT,
    deal_time_str TEXT,
    code TEXT,
    stock_name TEXT,
    side TEXT,
    price REAL,
    volume INTEGER,
    amount REAL,
    commission REAL,
    stamp_duty REAL,
    transfer_fee REAL,
    broker_traded_id TEXT,
    broker_order_id TEXT,
    broker_order_ref TEXT,
    strategy_name TEXT,
    matched_trade_id INTEGER,
    match_status TEXT,
    match_method TEXT,
    import_file TEXT,
    imported_at TEXT
)
'''

# 券商对账单委托（含废单/撤单原因）。用于 orders 表的对账基准。
DDL_BROKER_ORDERS = '''
CREATE TABLE IF NOT EXISTS broker_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT,
    order_date TEXT,
    order_time TEXT,
    code TEXT,
    side TEXT,
    order_ref TEXT,
    broker_order_id TEXT,
    status TEXT,
    price REAL,
    volume INTEGER,
    traded_volume INTEGER,
    cancel_volume INTEGER,
    reject_reason TEXT,
    strategy_name TEXT,
    import_file TEXT,
    imported_at TEXT
)
'''

BROKER_INDEXES = [
    ("idx_broker_deals_key",
     "CREATE INDEX IF NOT EXISTS idx_broker_deals_key "
     "ON broker_deals(account, code, side, deal_date)"),
    ("idx_broker_deals_traded",
     "CREATE INDEX IF NOT EXISTS idx_broker_deals_traded "
     "ON broker_deals(broker_traded_id)"),
    ("idx_broker_deals_order",
     "CREATE INDEX IF NOT EXISTS idx_broker_deals_order "
     "ON broker_deals(broker_order_id)"),
    ("idx_broker_orders_key",
     "CREATE INDEX IF NOT EXISTS idx_broker_orders_key "
     "ON broker_orders(account, code, order_date)"),
]

# 运行事件。logger.error 的结构化镜像 + 对账失配告警。
DDL_RUN_EVENTS = '''
CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT,
    event_time TEXT NOT NULL,
    level TEXT,
    event_type TEXT,
    code TEXT,
    order_id TEXT,
    trade_id TEXT,
    detail TEXT
)
'''

# 占位流水归档表：结构对齐 trade_records 并加归档元信息，供审计与回滚。
DDL_PLACEHOLDER_ARCHIVE = '''
CREATE TABLE IF NOT EXISTS trade_records_placeholder_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    orig_id INTEGER,
    stock_code TEXT,
    stock_name TEXT,
    trade_time TIMESTAMP,
    trade_type TEXT,
    price REAL,
    volume INTEGER,
    amount REAL,
    trade_id TEXT,
    commission REAL,
    strategy TEXT,
    archived_at TEXT,
    archive_reason TEXT,
    script_version TEXT,
    operator TEXT
)
'''

# trade_records 扩展列。ALTER TABLE ADD COLUMN 逐列幂等执行。
#
# 注意 commission 不在此列表 —— 它在 data_manager.py:627 建表时就有，
# 规格里给的 ALTER 会直接报 duplicate column name。
TRADE_RECORDS_NEW_COLUMNS = [
    ('account',            'TEXT'),
    ('deal_time',          'INTEGER'),      # XtTrade.traded_time 原值（Unix 秒）
    ('deal_time_str',      'TEXT'),         # 东八区可读串
    ('recorded_at',        'TIMESTAMP'),    # 落库时刻
    ('time_source',        'TEXT'),         # exchange/local_fallback/reconcile_backfill/broker
    ('order_id',           'TEXT'),
    ('fill_ids',           'TEXT'),
    ('fills',              'INTEGER DEFAULT 1'),
    ('strategy_code',      'TEXT'),
    ('strategy_label',     'TEXT'),
    ('is_simulation',      'INTEGER DEFAULT 0'),
    ('commission_source',  'TEXT'),         # broker/estimated/unknown
    ('commission_rate',    'TEXT'),
    ('side_source',        'TEXT'),         # deal/order/broker
    ('row_status',         "TEXT DEFAULT 'active'"),   # active/superseded
    ('duplicate_of',       'INTEGER'),      # 指向保留行的 id
    ('trade_id_source',    'TEXT'),         # traded_id/order_id/placeholder/manual
]

# 索引。ux_trade_records_deal 是「deal 级唯一」的落点。
#
# 两个必须知道的约束：
#
# 1) 不能只按 (account, trade_id)。库里 traded_id 与 order_id 混用 ——
#    网格路径 grid_trading_manager.py:2582 写的是 str(order_id)，
#    58 行网格流水中 15 组 order_id 跨多只股票/多个日期复用。
#    索引必须带上 stock_code 与 trade_time。
#
# 2) account 必须用 COALESCE 包起来。SQLite 在唯一索引中把 NULL 视为
#    互不相等（即使 NULL 对 NULL），而历史行 account 全为 NULL，
#    若直接写 account，整个索引对历史数据形同虚设 —— 看着建成了，
#    却一条重复都拦不住。COALESCE 把这个洞堵死（配合 backfill_account
#    双保险：回填保证导出可用，COALESCE 保证索引无死角）。
TRADE_RECORDS_INDEXES = [
    ("ux_trade_records_deal",
     "CREATE UNIQUE INDEX IF NOT EXISTS ux_trade_records_deal "
     "ON trade_records("
     "COALESCE(account,''), COALESCE(order_id,''), stock_code, trade_type, "
     "trade_id, COALESCE(deal_time, CAST(strftime('%s', trade_time) AS INTEGER), 0), "
     "volume, price"
     ") WHERE trade_id IS NOT NULL AND row_status='active'"),
    ("ix_trade_records_merge",
     "CREATE INDEX IF NOT EXISTS ix_trade_records_merge "
     "ON trade_records(COALESCE(account,''), stock_code, trade_type, "
     "strategy, deal_time)"),
    ("ux_trade_records_backfill",
     "CREATE UNIQUE INDEX IF NOT EXISTS ux_trade_records_backfill "
     "ON trade_records(COALESCE(account,''), stock_code, trade_type, "
     "COALESCE(deal_time, CAST(strftime('%s', trade_time) AS INTEGER), 0), "
     "volume, price) WHERE trade_id IS NULL"),
]

# deal 唯一键的语义说明（改键前必读）：
#
# 1) **不能只按 (account, trade_id)**。trade_id 混存三种东西：
#    - 'ORDER_xxx' 占位流水（已归档）
#    - 9-10 位短数字：网格路径写入的 str(order_id)，**不是全局唯一成交编号**。
#      实测 15 组重复 100% 跨标的（同一 order_id 在不同股票/日期上复用）
#    - 20 位长数字：真实 traded_id（成交编号），全局唯一
#    只按 trade_id 建唯一键，跨标的的短 id 会互相冲突。
#
# 2) **trade_type 必须入键**。同 id 同股同日但方向不同的两笔是不同成交。
#
# 3) **时间分量必须 NULL 安全，且只能用成交自身的属性**。
#    deal_time 对全部历史行为 NULL，直接入键会让 SQLite 把 NULL 视为互不相等，
#    索引对历史数据彻底失效（看着建成了，一条重复都拦不住）。
#    曾经用 recorded_at（落库时刻）兜底 —— 那是错的：落库时刻不是成交属性，
#    同一秒落库的两笔**不同**成交会因此撞键而被静默丢弃。
#    现改用 trade_time（成交时间）兜底。
#
# 4) **volume/price 必须入键**。同 id 同股同向同秒但价量不同的两笔，
#    现实中是同一委托的分笔成交，必须都保留。只有价量也完全相同才算同一条 deal。
#
# 5) order_id 单独入键，不与被写进 trade_id 的短 id 重复计数。


def ensure_index(conn, name, ddl):
    """按 DDL 建索引；若同名索引定义已变，先删后建。

    索引不是表 —— 删索引不涉及数据搬迁，可安全重建。
    只对索引做这件事，表仍然只用 ADD COLUMN。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone()
    desired = " ".join(ddl.split())
    if row and row[0]:
        existing = " ".join(str(row[0]).split())
        if existing == desired:
            return 'unchanged'
        conn.execute("DROP INDEX IF EXISTS %s" % name)
        conn.execute(ddl)
        logger.info(f"DB迁移: 索引 {name} 定义已更新，已重建")
        return 'rebuilt'
    conn.execute(ddl)
    return 'created'


def derive_account_id(db_path):
    """从库路径推导账号：data_<account_id>/trading.db → account_id。"""
    dirname = os.path.basename(os.path.dirname(os.path.abspath(db_path)))
    if dirname.startswith('data_') and len(dirname) > len('data_'):
        return dirname[len('data_'):]
    return None


def backfill_account(conn, db_path, account_override=None):
    """把 trade_records.account 回填为库所属账号。

    必须排在唯一索引之前：account 是索引首列，全为 NULL 时索引失效。
    返回回填行数。
    """
    account = account_override or derive_account_id(db_path)
    if not account:
        logger.warning(
            f"无法从路径推导账号（{db_path}），account 保持 NULL；"
            f"唯一索引靠 COALESCE 兜底，但导出的 account 列会是空值")
        return 0
    n = conn.execute(
        "SELECT COUNT(*) FROM trade_records WHERE account IS NULL").fetchone()[0]
    if n:
        conn.execute("UPDATE trade_records SET account=? WHERE account IS NULL",
                     (account,))
        logger.info(f"DB迁移: trade_records.account 回填 {n} 行 → {account}")
    return n

INDEXES = [
    ("idx_position_snapshot_date",
     "CREATE INDEX IF NOT EXISTS idx_position_snapshot_date "
     "ON position_snapshot(account, snapshot_date, snapshot_type)"),
    ("idx_account_equity_date",
     "CREATE INDEX IF NOT EXISTS idx_account_equity_date "
     "ON account_equity_daily(account, date)"),
    ("idx_run_events_time",
     "CREATE INDEX IF NOT EXISTS idx_run_events_time "
     "ON run_events(account, event_time)"),
    ("idx_run_events_type",
     "CREATE INDEX IF NOT EXISTS idx_run_events_type "
     "ON run_events(event_type, event_time)"),
]
def migrate_settlement_schema(db_path=None, do_backup=True, dry_run=False):
    """建立交割单改造所需的新表（幂等）。

    只做 CREATE TABLE IF NOT EXISTS 与 CREATE INDEX IF NOT EXISTS，
    不触碰任何既有表与数据。

    dry_run 同样走临时副本 —— CREATE TABLE 也是隐式提交的 DDL，
    在真库上跑一遍就没法撤回了。
    """
    db_path = db_path or config.DB_PATH

    if dry_run:
        fd, tmp_path = tempfile.mkstemp(suffix='_dryrun.db')
        os.close(fd)
        try:
            if os.path.exists(db_path):
                src = sqlite3.connect(db_path, timeout=30.0)
                dst = sqlite3.connect(tmp_path)
                src.backup(dst)
                dst.close()
                src.close()
            created = migrate_settlement_schema(
                tmp_path, do_backup=False, dry_run=False)
            return created
        finally:
            for suffix in ('', '-wal', '-shm'):
                try:
                    os.remove(tmp_path + suffix)
                except OSError:
                    pass

    created = []

    if do_backup:
        backup_db(db_path, reason='settlement_schema')

    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        for table, ddl in (
            ('position_snapshot', DDL_POSITION_SNAPSHOT),
            ('account_equity_daily', DDL_ACCOUNT_EQUITY_DAILY),
            ('run_events', DDL_RUN_EVENTS),
            ('trade_records_sim', DDL_TRADE_RECORDS_SIM),
            ('broker_deals', DDL_BROKER_DEALS),
            ('broker_orders', DDL_BROKER_ORDERS),
        ):
            existed = table_exists(conn, table)
            conn.execute(ddl)
            if not existed:
                created.append(table)
                logger.info(f"DB迁移: 已创建表 {table}")

        for _, index_ddl in INDEXES + BROKER_INDEXES:
            conn.execute(index_ddl)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    logger.info(f"交割单 schema 迁移完成: {db_path} (新建表 {len(created)} 张)")
    return created


SCRIPT_VERSION = '1.0.0'


def archive_placeholder_rows(conn, account):
    """把 trade_id LIKE 'ORDER_%' 的占位流水归档后从主表删除。

    占位流水是「下单成功先写一条假成交、成交回报到了再删掉替换」的产物，
    它的 trade_id 是 f"ORDER_{order_id}"，而 QMT 的 order_id 会跨日复用，
    因此同一个 trade_id 会散落在不同日期的不同股票上 —— 这也是
    UNIQUE(account, trade_id) 建不出来的主因之一。

    返回归档行数。
    """
    rows = conn.execute(
        "SELECT * FROM trade_records WHERE trade_id LIKE 'ORDER_%'").fetchall()
    if not rows:
        return 0

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.executemany(
        "INSERT INTO trade_records_placeholder_archive("
        "orig_id, stock_code, stock_name, trade_time, trade_type, price, "
        "volume, amount, trade_id, commission, strategy, archived_at, "
        "archive_reason, script_version, operator) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(r['id'], r['stock_code'], r['stock_name'], r['trade_time'],
          r['trade_type'], r['price'], r['volume'], r['amount'],
          r['trade_id'], r['commission'], r['strategy'], now,
          'placeholder_purge: trade_id 为 ORDER_ 前缀的预写假成交',
          SCRIPT_VERSION, account) for r in rows])
    conn.execute("DELETE FROM trade_records WHERE trade_id LIKE 'ORDER_%'")
    return len(rows)


def mark_duplicate_deals(conn):
    """把「同 trade_id + 同 stock_code + 同时间」的重复行标记为 superseded。

    只标记不物理删除：保留 id 最小的一条为 active，其余指向它。
    原始事实不丢，可审计可回滚，唯一索引也就能建起来了。

    判定必须带 stock_code 与 trade_time —— 网格路径写入的是 order_id
    （grid_trading_manager.py:2582 写 str(order_id)），同一个 id 会跨越
    多只股票、多个日期出现，那是 ID 复用，不是重复成交。
    """
    dups = conn.execute('''
        SELECT trade_id, stock_code, trade_time,
               MIN(id) keep_id, GROUP_CONCAT(id) ids
        FROM trade_records
        WHERE trade_id IS NOT NULL AND row_status='active'
        GROUP BY trade_id, stock_code, trade_time
        HAVING COUNT(*) > 1
    ''').fetchall()

    marked = []
    for d in dups:
        ids = [int(x) for x in str(d['ids']).split(',')]
        victims = [i for i in ids if i != d['keep_id']]
        conn.executemany(
            "UPDATE trade_records SET row_status='superseded', duplicate_of=? "
            "WHERE id=?", [(d['keep_id'], v) for v in victims])
        marked.append({'trade_id': d['trade_id'], 'stock_code': d['stock_code'],
                       'trade_time': d['trade_time'], 'keep_id': d['keep_id'],
                       'superseded': victims})
    return marked


def apply_trade_records_extension(db_path=None, do_backup=True, dry_run=False,
                                  account_override=None):
    """trade_records 扩展 + 数据清理 + 索引（停机后执行）。

    顺序强依赖：必须先补列 → 回填 account → 归档占位流水 → 标记重复行
    → 最后建唯一索引。任何一步跳过，唯一索引都会 IntegrityError。

    dry_run 的实现是「在临时副本上真跑一遍」而不是「跳过写操作」——
    ALTER TABLE / CREATE INDEX 在 SQLite 里会隐式提交，靠 rollback() 撤不掉，
    因此要真正无副作用，只能换一个库跑。
    """
    db_path = db_path or config.DB_PATH
    account = account_override or derive_account_id(db_path)

    if dry_run:
        fd, tmp_path = tempfile.mkstemp(suffix='_dryrun.db')
        os.close(fd)
        try:
            src = sqlite3.connect(db_path, timeout=60.0)
            dst = sqlite3.connect(tmp_path)
            src.backup(dst)
            dst.close()
            src.close()

            report = apply_trade_records_extension(
                tmp_path, do_backup=False, dry_run=False,
                # 副本在系统临时目录里推不出账号，用原库的推导结果覆盖，
                # 否则报告里的 account 回填数会失真
                account_override=account)
            report['db_path'] = db_path        # 报告里仍显示真实目标库
            report['dry_run'] = True
            report.pop('backup', None)
            return report
        finally:
            for suffix in ('', '-wal', '-shm'):
                try:
                    os.remove(tmp_path + suffix)
                except OSError:
                    pass

    report = {'db_path': db_path, 'added_columns': [], 'account_backfilled': 0,
              'archived_placeholder': 0, 'deleted_placeholder': 0,
              'rows_before': 0, 'rows_after': 0, 'marked_duplicates': [],
              'indexes': [], 'dry_run': False}

    if do_backup:
        report['backup'] = backup_db(db_path, reason='trade_records_extension')

    conn = sqlite3.connect(db_path, timeout=60.0)
    conn.row_factory = sqlite3.Row
    try:
        report['rows_before'] = conn.execute(
            "SELECT COUNT(*) FROM trade_records").fetchone()[0]

        # 1) 补列
        report['added_columns'] = ensure_columns(
            conn, 'trade_records', TRADE_RECORDS_NEW_COLUMNS)

        # 2) 回填 account —— 必须早于建索引，否则索引首列全 NULL 而失效
        report['account_backfilled'] = backfill_account(
            conn, db_path, account_override=account)

        # 3) 归档占位流水
        conn.execute(DDL_PLACEHOLDER_ARCHIVE)
        report['archived_placeholder'] = archive_placeholder_rows(
            conn, account or os.path.basename(os.path.dirname(db_path)))
        report['deleted_placeholder'] = report['archived_placeholder']

        # 4) 标记重复成交行
        report['marked_duplicates'] = mark_duplicate_deals(conn)

        conn.commit()

        # 5) 建索引
        for name, ddl in TRADE_RECORDS_INDEXES:
            try:
                action = ensure_index(conn, name, ddl)
                report['indexes'].append(f"{name}:{action}")
            except sqlite3.IntegrityError as e:
                # 唯一索引建不上说明还有漏网的重复，宁可报错也不要静默失败
                raise RuntimeError(
                    f"唯一索引 {name} 建立失败，仍有重复数据: {e}") from e

        conn.commit()
        report['rows_after'] = conn.execute(
            "SELECT COUNT(*) FROM trade_records").fetchone()[0]
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return report


# ============================== 账号发现 ==============================
def discover_account_dbs(root=None):
    """发现所有账号库，返回 [(account_id, db_path, source), ...]。

    只信 config.get_all_accounts_config() 会漏账号：当前 account_config.json
    只声明了一个账号，但磁盘上 data_25106531/ 里有 18 条真实流水。
    因此用「配置」并「目录扫描」取并集，差集打警告而不是静默丢弃。
    """
    import glob as _glob

    root = root or os.path.dirname(os.path.abspath(config.__file__))
    found = {}

    # 来源一：配置声明
    configured = set()
    try:
        for acc in config.get_all_accounts_config():
            acc_id = str(acc.get('account_id') or '').strip()
            if not acc_id:
                continue
            configured.add(acc_id)
            path = os.path.join(root, f"data_{acc_id}", "trading.db")
            if os.path.exists(path):
                found[acc_id] = (path, 'config')
            else:
                logger.warning(f"账号 {acc_id} 在配置中声明，但库不存在: {path}")
    except Exception as e:
        logger.warning(f"读取账号配置失败: {e}")

    # 来源二：目录扫描。精确匹配 trading.db 结尾 ——
    # data/ 下混着 280 个 trading.db.backup_* 文件，模糊匹配会全部命中。
    for path in _glob.glob(os.path.join(root, "data_*", "trading.db")):
        acc_id = os.path.basename(os.path.dirname(path)).replace("data_", "", 1)
        if acc_id not in found:
            found[acc_id] = (path, 'disk_only')
            if acc_id not in configured:
                logger.warning(
                    f"账号 {acc_id} 存在库文件但未在 account_config.json 中声明: {path}")

    # 默认单账号库（未设 QMT_ACCOUNT_ID 时使用）
    default_db = os.path.join(root, "data", "trading.db")
    if os.path.exists(default_db) and 'default' not in found:
        found['default'] = (default_db, 'default')

    result = [(acc_id, path, src) for acc_id, (path, src) in sorted(found.items())]
    return result


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='交割单改造 schema 迁移')
    parser.add_argument('--db', help='目标数据库路径，默认取 config.DB_PATH')
    parser.add_argument('--accounts', help="'all' 表示迁移所有已发现的账号库")
    parser.add_argument('--no-backup', action='store_true', help='跳过迁移前备份')
    parser.add_argument('--dry-run', action='store_true', help='只列出将要迁移的库，不执行')
    args = parser.parse_args()

    if args.accounts == 'all':
        targets = discover_account_dbs()
        print(f"发现 {len(targets)} 个账号库:")
        for acc_id, path, src in targets:
            print(f"  {acc_id:<12} {path}  [{src}]")
    else:
        targets = [('current', args.db or config.DB_PATH, 'arg')]
        print(f"目标库: {targets[0][1]}")

    if args.dry_run:
        print("\n--dry-run：未执行任何写操作")
        raise SystemExit(0)

    print()
    for acc_id, path, _src in targets:
        try:
            new_tables = migrate_settlement_schema(path, do_backup=not args.no_backup)
            print(f"  [OK]   {acc_id:<12} 新建表 {new_tables if new_tables else '（已存在）'}")
        except Exception as exc:
            print(f"  [FAIL] {acc_id:<12} {exc}")
