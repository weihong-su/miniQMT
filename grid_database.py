"""
数据库管理模块

提供统一的数据库操作接口:
- 持仓数据持久化
- 网格交易会话管理
- 交易记录管理
"""

import sqlite3
import threading
from datetime import datetime
from typing import Optional, List, Dict
import config
from logger import get_logger

logger = get_logger(__name__)


class DatabaseManager:
    """数据库管理器"""

    def __init__(self, db_path: str = None):
        """
        初始化数据库管理器

        Args:
            db_path: 数据库文件路径,默认使用config.DB_PATH
        """
        self.db_path = db_path or config.DB_PATH
        self.conn = None
        self.lock = threading.RLock()

        # 连接数据库
        self._connect()

        # 初始化基础表
        self._init_base_tables()

        logger.info(f"数据库管理器初始化完成: {self.db_path}")

    def _connect(self):
        """连接数据库"""
        try:
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row  # 返回字典格式
            self.conn.execute("PRAGMA busy_timeout = 30000")  # 30秒超时
            logger.debug(f"数据库连接成功: {self.db_path}")
        except Exception as e:
            logger.error(f"数据库连接失败: {str(e)}")
            raise

    def _init_base_tables(self):
        """初始化基础表(持仓、交易记录)

        注意: positions 和 trade_records 表的权威 Schema 定义在
        position_manager.py 的 _create_memory_table() 中，由 PositionManager
        在启动时统一建表并管理。此处仅补全可能尚未创建的场景（如单独使用
        DatabaseManager 时），以防 grid 模块在 PositionManager 之前初始化。
        字段定义必须与 position_manager.py 保持一致，任何 Schema 变更须
        同步修改两处。
        """
        cursor = self.conn.cursor()

        # 创建持仓表（权威定义见 position_manager.py _create_memory_table）
        # available 字段在 SQLite 层不持久化实时值，INSERT 时由 position_manager
        # 统一写 0，由实盘同步覆盖，此处仅保证表结构存在。
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                stock_code TEXT PRIMARY KEY,
                stock_name TEXT,
                volume REAL,
                available REAL,
                cost_price REAL,
                base_cost_price REAL,
                current_price REAL,
                market_value REAL,
                profit_ratio REAL,
                last_update TIMESTAMP,
                open_date TIMESTAMP,
                profit_triggered BOOLEAN DEFAULT FALSE,
                highest_price REAL,
                stop_loss_price REAL,
                profit_breakout_triggered BOOLEAN DEFAULT FALSE,
                breakout_highest_price REAL
            )
        """)

        # 创建交易记录表（权威定义见 position_manager.py）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trade_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stock_code TEXT NOT NULL,
                trade_type TEXT NOT NULL,
                price REAL NOT NULL,
                volume INTEGER NOT NULL,
                amount REAL NOT NULL,
                trade_id TEXT,
                strategy TEXT,
                trade_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_trade_records_stock
            ON trade_records(stock_code)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_trade_records_time
            ON trade_records(trade_time)
        """)

        self.conn.commit()
        logger.debug("基础表初始化完成")

    def init_grid_tables(self):
        """初始化网格交易表"""
        # 启用外键约束
        self.conn.execute("PRAGMA foreign_keys = ON")
        cursor = self.conn.cursor()

        # 创建grid_trading_sessions表
        # 优化: 移除UNIQUE(stock_code, status) ON CONFLICT REPLACE约束
        # 改用应用层检查,确保一个股票只有一个active session
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS grid_trading_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stock_code TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                enabled INTEGER NOT NULL DEFAULT 1,

                -- 价格配置
                center_price REAL NOT NULL,
                current_center_price REAL,
                price_interval REAL NOT NULL DEFAULT 0.05,

                -- 交易配置
                position_ratio REAL NOT NULL DEFAULT 0.25,
                callback_ratio REAL NOT NULL DEFAULT 0.005,

                -- 资金配置
                max_investment REAL NOT NULL,
                current_investment REAL DEFAULT 0,

                -- 退出配置
                max_deviation REAL DEFAULT 0.15,
                target_profit REAL DEFAULT 0.10,
                stop_loss REAL DEFAULT -0.10,

                -- 统计数据
                trade_count INTEGER DEFAULT 0,
                buy_count INTEGER DEFAULT 0,
                sell_count INTEGER DEFAULT 0,
                total_buy_amount REAL DEFAULT 0,
                total_sell_amount REAL DEFAULT 0,

                -- 时间戳
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                stop_time TEXT,
                stop_reason TEXT,

                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,

                -- ⚠️ 新增字段: 风险等级和模板名称
                risk_level TEXT DEFAULT 'moderate',
                template_name TEXT
            )
        """)

        # 数据库迁移: 为已存在的表添加新字段
        try:
            cursor.execute("ALTER TABLE grid_trading_sessions ADD COLUMN risk_level TEXT DEFAULT 'moderate'")
            logger.info("数据库迁移: 添加 risk_level 字段")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e):
                pass  # 字段已存在,跳过
            else:
                raise

        try:
            cursor.execute("ALTER TABLE grid_trading_sessions ADD COLUMN template_name TEXT")
            logger.info("数据库迁移: 添加 template_name 字段")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e):
                pass  # 字段已存在,跳过
            else:
                raise

        try:
            cursor.execute("ALTER TABLE grid_trading_sessions ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
            logger.info("数据库迁移: 添加 enabled 字段")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e):
                pass
            else:
                raise

        # True P&L volume tracking fields
        try:
            cursor.execute("ALTER TABLE grid_trading_sessions ADD COLUMN total_buy_volume INTEGER DEFAULT 0")
            logger.info("数据库迁移: 添加 total_buy_volume 字段")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e):
                pass
            else:
                raise

        try:
            cursor.execute("ALTER TABLE grid_trading_sessions ADD COLUMN total_sell_volume INTEGER DEFAULT 0")
            logger.info("数据库迁移: 添加 total_sell_volume 字段")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e):
                pass
            else:
                raise

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_grid_sessions_stock
            ON grid_trading_sessions(stock_code)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_grid_sessions_status
            ON grid_trading_sessions(status)
        """)

        # 创建grid_trades表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS grid_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                stock_code TEXT NOT NULL,

                trade_type TEXT NOT NULL,
                grid_level REAL NOT NULL,
                trigger_price REAL NOT NULL,
                volume INTEGER NOT NULL,
                amount REAL NOT NULL,

                peak_price REAL,
                valley_price REAL,
                callback_ratio REAL,

                trade_id TEXT,
                trade_time TEXT NOT NULL,

                grid_center_before REAL,
                grid_center_after REAL,

                created_at TEXT DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY (session_id) REFERENCES grid_trading_sessions(id) ON DELETE CASCADE
            )
        """)

        # 数据库迁移: 检查grid_trades表结构
        try:
            cursor.execute("SELECT session_id FROM grid_trades LIMIT 1")
        except sqlite3.OperationalError as e:
            if "no such column" in str(e):
                # 旧表结构,需要重建
                logger.warning("检测到grid_trades表结构过旧,正在重建...")
                cursor.execute("DROP TABLE IF EXISTS grid_trades")
                # 重新创建表
                cursor.execute("""
                    CREATE TABLE grid_trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id INTEGER NOT NULL,
                        stock_code TEXT NOT NULL,

                        trade_type TEXT NOT NULL,
                        grid_level REAL NOT NULL,
                        trigger_price REAL NOT NULL,
                        volume INTEGER NOT NULL,
                        amount REAL NOT NULL,

                        peak_price REAL,
                        valley_price REAL,
                        callback_ratio REAL,

                        trade_id TEXT,
                        trade_time TEXT NOT NULL,

                        grid_center_before REAL,
                        grid_center_after REAL,

                        created_at TEXT DEFAULT CURRENT_TIMESTAMP,

                        FOREIGN KEY (session_id) REFERENCES grid_trading_sessions(id) ON DELETE CASCADE
                    )
                """)
                logger.info("grid_trades表重建完成")

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_grid_trades_session
            ON grid_trades(session_id)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_grid_trades_stock
            ON grid_trades(stock_code)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_grid_trades_time
            ON grid_trades(trade_time)
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS grid_orders (
                order_id TEXT PRIMARY KEY,
                session_id INTEGER NOT NULL,
                stock_code TEXT NOT NULL,
                side TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'submitted',
                requested_volume INTEGER NOT NULL,
                expected_price REAL NOT NULL,
                reserved_price REAL,
                filled_volume INTEGER DEFAULT 0,
                filled_amount REAL DEFAULT 0,
                last_error TEXT,
                submitted_at TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                raw_signal TEXT,
                FOREIGN KEY (session_id) REFERENCES grid_trading_sessions(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_grid_orders_session
            ON grid_orders(session_id)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_grid_orders_status
            ON grid_orders(status)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_grid_orders_stock
            ON grid_orders(stock_code)
        """)

        try:
            cursor.execute("ALTER TABLE grid_orders ADD COLUMN reserved_price REAL")
            logger.info("数据库迁移: 添加 grid_orders.reserved_price 字段")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e):
                pass
            else:
                raise

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS grid_lots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                stock_code TEXT NOT NULL,
                buy_trade_id TEXT NOT NULL,
                buy_order_id TEXT,
                buy_price REAL NOT NULL,
                original_volume INTEGER NOT NULL,
                remaining_volume INTEGER NOT NULL,
                realized_volume INTEGER DEFAULT 0,
                buy_amount REAL NOT NULL,
                opened_at TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                status TEXT NOT NULL DEFAULT 'open',
                FOREIGN KEY (session_id) REFERENCES grid_trading_sessions(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_grid_lots_session_status
            ON grid_lots(session_id, status)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_grid_lots_stock
            ON grid_lots(stock_code)
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS grid_lot_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                stock_code TEXT NOT NULL,
                buy_lot_id INTEGER,
                sell_trade_id TEXT NOT NULL,
                sell_order_id TEXT,
                match_type TEXT NOT NULL DEFAULT 'matched',
                volume INTEGER NOT NULL,
                buy_price REAL,
                sell_price REAL NOT NULL,
                buy_amount REAL DEFAULT 0,
                sell_amount REAL NOT NULL,
                realized_pnl REAL DEFAULT 0,
                matched_at TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES grid_trading_sessions(id) ON DELETE CASCADE,
                FOREIGN KEY (buy_lot_id) REFERENCES grid_lots(id) ON DELETE SET NULL
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_grid_lot_matches_session
            ON grid_lot_matches(session_id)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_grid_lot_matches_sell_trade
            ON grid_lot_matches(sell_trade_id)
        """)

        # 创建grid_config_templates表(网格配置模板)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS grid_config_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_name TEXT NOT NULL UNIQUE,
                price_interval REAL NOT NULL DEFAULT 0.05,
                position_ratio REAL NOT NULL DEFAULT 0.25,
                callback_ratio REAL NOT NULL DEFAULT 0.005,
                max_deviation REAL DEFAULT 0.15,
                target_profit REAL DEFAULT 0.10,
                stop_loss REAL DEFAULT -0.10,
                duration_days INTEGER DEFAULT 7,
                max_investment_ratio REAL DEFAULT 0.5,
                description TEXT,
                is_default BOOLEAN DEFAULT FALSE,
                usage_count INTEGER DEFAULT 0,
                last_used_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_grid_templates_name
            ON grid_config_templates(template_name)
        """)

        self.conn.commit()
        logger.info("网格交易表初始化完成")

    def create_grid_session(self, session_data: dict) -> int:
        """创建网格会话

        优化: 确保一个股票只有一个active session
        1. 在创建前检查是否已存在active session
        2. 如果存在,先停止旧session
        3. 创建新session
        """
        # 检查end_time，如果为None则设置默认值
        if session_data.get('end_time') is None:
            from datetime import timedelta
            start_time_str = session_data.get('start_time')
            if start_time_str:
                # 如果start_time是字符串，解析它
                if isinstance(start_time_str, str):
                    # 处理ISO格式的日期字符串
                    # 时区安全: 统一剥离时区信息转为 naive datetime，避免与 datetime.now() 比较时 TypeError
                    if 'T' in start_time_str:
                        start_time = datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
                        if start_time.tzinfo is not None:
                            start_time = start_time.replace(tzinfo=None)
                    else:
                        # 如果只是日期,转为datetime
                        start_time = datetime.fromisoformat(start_time_str)
                else:
                    start_time = start_time_str
            else:
                start_time = datetime.now()

            # 设置默认end_time为start_time + 30天
            session_data['end_time'] = (start_time + timedelta(days=30)).isoformat()

        stock_code = session_data.get('stock_code')
        logger.debug(f"[GRID-DB] create_grid_session: 开始创建会话 stock_code={stock_code}")
        logger.debug(f"[GRID-DB] create_grid_session: session_data={session_data}")

        with self.lock:
            cursor = self.conn.cursor()

            # 检查是否已存在active session
            cursor.execute("""
                SELECT id FROM grid_trading_sessions
                WHERE stock_code=? AND status='active'
            """, (stock_code,))
            existing = cursor.fetchone()

            if existing:
                # 先停止旧的active session
                old_session_id = existing[0]
                logger.warning(f"[GRID-DB] create_grid_session: {stock_code}已有活跃session(id={old_session_id}), 先停止")
                self.stop_grid_session(old_session_id, 'replaced')

            # 创建新session
            cursor.execute("""
                INSERT INTO grid_trading_sessions
                (stock_code, status, enabled, center_price, current_center_price,
                 price_interval, position_ratio, callback_ratio,
                 max_investment, max_deviation, target_profit, stop_loss,
                 start_time, end_time, risk_level, template_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                session_data['stock_code'],
                'active',
                1,
                session_data['center_price'],
                session_data['center_price'],
                session_data['price_interval'],
                session_data['position_ratio'],
                session_data['callback_ratio'],
                session_data['max_investment'],
                session_data['max_deviation'],
                session_data['target_profit'],
                session_data['stop_loss'],
                session_data['start_time'],
                session_data['end_time'],
                session_data.get('risk_level', 'moderate'),
                session_data.get('template_name')
            ))
            self.conn.commit()
            session_id = cursor.lastrowid
            logger.info(f"[GRID-DB] create_grid_session: 创建成功 session_id={session_id}, stock_code={stock_code}")
            return session_id

    def update_grid_session(self, session_id: int, updates: dict):
        """更新网格会话"""
        logger.debug(f"[GRID-DB] update_grid_session: session_id={session_id}, updates={updates}")

        # D-1修复: 字段名白名单校验，防止动态拼接 SQL 时引入非法列名
        _ALLOWED_SESSION_FIELDS = {
            'status', 'current_center_price', 'current_investment',
            'trade_count', 'buy_count', 'sell_count',
            'total_buy_amount', 'total_sell_amount',
            'total_buy_volume', 'total_sell_volume', 'enabled',
            'stop_time', 'stop_reason', 'risk_level', 'template_name'
        }
        invalid_fields = set(updates.keys()) - _ALLOWED_SESSION_FIELDS
        if invalid_fields:
            raise ValueError(f"update_grid_session: 非法字段名 {invalid_fields}，拒绝执行以防 SQL 注入")

        with self.lock:
            should_commit = not self.conn.in_transaction
            set_clause = ', '.join([f"{k}=?" for k in updates.keys()])
            values = list(updates.values()) + [session_id]

            cursor = self.conn.cursor()
            cursor.execute(f"""
                UPDATE grid_trading_sessions
                SET {set_clause}, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, values)
            if should_commit:
                self.conn.commit()
            logger.debug(f"[GRID-DB] update_grid_session: 更新完成 session_id={session_id}, affected_rows={cursor.rowcount}")

    def stop_grid_session(self, session_id: int, reason: str):
        """停止网格会话"""
        logger.info(f"[GRID-DB] stop_grid_session: session_id={session_id}, reason={reason}")

        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                UPDATE grid_trading_sessions
                SET status=?, stop_time=?, stop_reason=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, ('stopped', datetime.now().isoformat(), reason, session_id))
            self.conn.commit()
            logger.debug(f"[GRID-DB] stop_grid_session: 停止完成 session_id={session_id}, affected_rows={cursor.rowcount}")

    def get_all_grid_sessions(self) -> list:
        """获取所有网格会话(包括stopped状态)

        返回:
            所有会话的列表,按创建时间倒序排列
        """
        logger.debug(f"[GRID-DB] get_all_grid_sessions: 查询所有会话")

        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT * FROM grid_trading_sessions
                ORDER BY start_time DESC
            """)
            results = cursor.fetchall()
            logger.debug(f"[GRID-DB] get_all_grid_sessions: 查询到 {len(results)} 个会话")
            return results

    def get_active_grid_sessions(self) -> list:
        """获取所有活跃的网格会话"""
        logger.debug(f"[GRID-DB] get_active_grid_sessions: 查询活跃会话")

        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT * FROM grid_trading_sessions
                WHERE status='active'
                ORDER BY start_time DESC
            """)
            results = cursor.fetchall()
            logger.debug(f"[GRID-DB] get_active_grid_sessions: 查询到 {len(results)} 个活跃会话")
            return results

    def get_grid_session_by_stock(self, stock_code: str):
        """获取指定股票的活跃网格会话"""
        logger.debug(f"[GRID-DB] get_grid_session_by_stock: stock_code={stock_code}")

        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT * FROM grid_trading_sessions
                WHERE stock_code=? AND status='active'
                LIMIT 1
            """, (stock_code,))
            row = cursor.fetchone()
            result = dict(row) if row else None
            logger.debug(f"[GRID-DB] get_grid_session_by_stock: stock_code={stock_code}, found={result is not None}")
            return result

    def record_grid_trade(self, trade_data: dict) -> int:
        """记录网格交易"""
        logger.info(f"[GRID-DB] record_grid_trade: 记录交易 session_id={trade_data.get('session_id')}, "
                   f"stock_code={trade_data.get('stock_code')}, trade_type={trade_data.get('trade_type')}")
        logger.debug(f"[GRID-DB] record_grid_trade: trade_data={trade_data}")

        with self.lock:
            should_commit = not self.conn.in_transaction
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT INTO grid_trades
                (session_id, stock_code, trade_type, grid_level, trigger_price,
                 volume, amount, peak_price, valley_price, callback_ratio,
                 trade_id, trade_time, grid_center_before, grid_center_after)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_data['session_id'],
                trade_data['stock_code'],
                trade_data['trade_type'],
                trade_data['grid_level'],
                trade_data['trigger_price'],
                trade_data['volume'],
                trade_data['amount'],
                trade_data.get('peak_price'),
                trade_data.get('valley_price'),
                trade_data.get('callback_ratio'),
                trade_data.get('trade_id'),
                trade_data['trade_time'],
                trade_data.get('grid_center_before'),
                trade_data.get('grid_center_after')
            ))
            if should_commit:
                self.conn.commit()
            trade_id = cursor.lastrowid
            logger.info(f"[GRID-DB] record_grid_trade: 记录成功 id={trade_id}, session_id={trade_data.get('session_id')}, "
                       f"trade_type={trade_data.get('trade_type')}, volume={trade_data.get('volume')}, amount={trade_data.get('amount')}")
            return trade_id

    def create_grid_order(self, order_data: dict) -> None:
        """持久化网格委托，用于重启恢复和撤废单处理"""
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT INTO grid_orders
                (order_id, session_id, stock_code, side, status,
                 requested_volume, expected_price, reserved_price, filled_volume, filled_amount,
                 submitted_at, raw_signal)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    session_id=excluded.session_id,
                    stock_code=excluded.stock_code,
                    side=excluded.side,
                    status=excluded.status,
                    requested_volume=excluded.requested_volume,
                    expected_price=excluded.expected_price,
                    reserved_price=excluded.reserved_price,
                    filled_volume=excluded.filled_volume,
                    filled_amount=excluded.filled_amount,
                    submitted_at=excluded.submitted_at,
                    raw_signal=excluded.raw_signal,
                    updated_at=CURRENT_TIMESTAMP
            """, (
                str(order_data['order_id']),
                order_data['session_id'],
                order_data['stock_code'],
                order_data['side'],
                order_data.get('status', 'submitted'),
                int(order_data['requested_volume']),
                float(order_data['expected_price']),
                float(order_data.get('reserved_price') or order_data['expected_price']),
                int(order_data.get('filled_volume', 0)),
                float(order_data.get('filled_amount', 0.0)),
                order_data.get('submitted_at', datetime.now().isoformat()),
                order_data.get('raw_signal')
            ))
            self.conn.commit()

    def update_grid_order(self, order_id: str, updates: dict) -> None:
        """更新网格委托状态"""
        allowed_fields = {
            'status', 'filled_volume', 'filled_amount', 'last_error', 'raw_signal'
        }
        invalid_fields = set(updates.keys()) - allowed_fields
        if invalid_fields:
            raise ValueError(f"update_grid_order: 非法字段名 {invalid_fields}")
        if not updates:
            return

        with self.lock:
            should_commit = not self.conn.in_transaction
            set_clause = ', '.join([f"{k}=?" for k in updates.keys()])
            values = list(updates.values()) + [str(order_id)]
            cursor = self.conn.cursor()
            cursor.execute(f"""
                UPDATE grid_orders
                SET {set_clause}, updated_at=CURRENT_TIMESTAMP
                WHERE order_id=?
            """, values)
            if should_commit:
                self.conn.commit()

    def get_grid_order(self, order_id: str):
        """获取网格委托"""
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT * FROM grid_orders WHERE order_id=?
            """, (str(order_id),))
            row = cursor.fetchone()
            return dict(row) if row else None

    def grid_trade_exists(self, trade_id: str) -> bool:
        """检查成交回报是否已经落账，用于重启后的幂等保护"""
        if not trade_id:
            return False
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT 1 FROM grid_trades WHERE trade_id=? LIMIT 1
            """, (str(trade_id),))
            return cursor.fetchone() is not None

    def get_open_grid_orders(self) -> list:
        """获取尚未终结的网格委托"""
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT * FROM grid_orders
                WHERE status IN ('submitted', 'partial_filled', 'cancel_requested', 'cancel_failed')
                ORDER BY submitted_at ASC
            """)
            return [dict(row) for row in cursor.fetchall()]

    def get_unmatched_grid_sell_volume(self, session_id: int) -> int:
        """获取尚未被买回动作回补的先卖出数量。"""
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT COALESCE(SUM(volume), 0)
                FROM grid_lot_matches
                WHERE session_id=? AND match_type='unmatched'
            """, (session_id,))
            return int(cursor.fetchone()[0] or 0)

    def _insert_grid_buy_lot(self, cursor, trade_data: dict, volume: int,
                             remaining_volume: int, realized_volume: int,
                             buy_amount: float, status: str):
        """写入一笔网格买入批次，可表示未平库存或已回补卖出。"""
        price = float(trade_data['trigger_price'])
        cursor.execute("""
            INSERT INTO grid_lots
            (session_id, stock_code, buy_trade_id, buy_order_id, buy_price,
             original_volume, remaining_volume, realized_volume, buy_amount,
             opened_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade_data['session_id'],
            trade_data['stock_code'],
            str(trade_data.get('trade_id') or ''),
            str(trade_data.get('order_id') or '') if trade_data.get('order_id') else None,
            price,
            int(volume),
            int(remaining_volume),
            int(realized_volume),
            float(buy_amount),
            trade_data.get('trade_time') or datetime.now().isoformat(),
            status
        ))
        return cursor.lastrowid

    def _record_grid_buy_lot(self, cursor, trade_data: dict):
        """记录买入；若存在先卖出的未匹配底仓，则按 LIFO 最近优先回补并确认收益。"""
        remaining = int(trade_data['volume'])
        price = float(trade_data['trigger_price'])

        cursor.execute("""
            SELECT * FROM grid_lot_matches
            WHERE session_id=? AND match_type='unmatched' AND volume > 0
            ORDER BY matched_at DESC, id DESC
        """, (trade_data['session_id'],))
        unmatched_sells = cursor.fetchall()

        for sell_row in unmatched_sells:
            if remaining <= 0:
                break

            sell_volume = int(sell_row['volume'])
            match_volume = min(remaining, sell_volume)
            buy_amount = price * match_volume
            sell_price = float(sell_row['sell_price'])
            sell_amount = sell_price * match_volume
            realized_pnl = sell_amount - buy_amount

            buy_lot_id = self._insert_grid_buy_lot(
                cursor,
                trade_data,
                volume=match_volume,
                remaining_volume=0,
                realized_volume=match_volume,
                buy_amount=buy_amount,
                status='closed'
            )

            cursor.execute("""
                UPDATE grid_lot_matches
                SET buy_lot_id=?, match_type='matched', volume=?, buy_price=?,
                    buy_amount=?, sell_amount=?, realized_pnl=?
                WHERE id=?
            """, (
                buy_lot_id,
                match_volume,
                price,
                buy_amount,
                sell_amount,
                realized_pnl,
                sell_row['id']
            ))

            unmatched_remainder = sell_volume - match_volume
            if unmatched_remainder > 0:
                cursor.execute("""
                    INSERT INTO grid_lot_matches
                    (session_id, stock_code, buy_lot_id, sell_trade_id, sell_order_id,
                     match_type, volume, buy_price, sell_price, buy_amount, sell_amount,
                     realized_pnl, matched_at)
                    VALUES (?, ?, NULL, ?, ?, 'unmatched', ?, NULL, ?, 0, ?, 0, ?)
                """, (
                    sell_row['session_id'],
                    sell_row['stock_code'],
                    sell_row['sell_trade_id'],
                    sell_row['sell_order_id'],
                    unmatched_remainder,
                    sell_price,
                    sell_price * unmatched_remainder,
                    sell_row['matched_at']
                ))

            remaining -= match_volume

        if remaining > 0:
            self._insert_grid_buy_lot(
                cursor,
                trade_data,
                volume=remaining,
                remaining_volume=remaining,
                realized_volume=0,
                buy_amount=price * remaining,
                status='open'
            )

    def _match_grid_sell_lots(self, cursor, trade_data: dict):
        """按 LIFO 将已确认卖出匹配到网格买入批次，未匹配部分标记为底仓卖出。"""
        remaining = int(trade_data['volume'])
        sell_price = float(trade_data['trigger_price'])
        sell_trade_id = str(trade_data.get('trade_id') or '')
        sell_order_id = str(trade_data.get('order_id') or '') if trade_data.get('order_id') else None
        matched_at = trade_data.get('trade_time') or datetime.now().isoformat()

        cursor.execute("""
            SELECT * FROM grid_lots
            WHERE session_id=? AND remaining_volume > 0
            ORDER BY opened_at DESC, id DESC
        """, (trade_data['session_id'],))
        lots = cursor.fetchall()

        for lot in lots:
            if remaining <= 0:
                break

            lot_volume = int(lot['remaining_volume'])
            match_volume = min(remaining, lot_volume)
            buy_price = float(lot['buy_price'])
            buy_amount = buy_price * match_volume
            sell_amount = sell_price * match_volume
            realized_pnl = sell_amount - buy_amount
            new_remaining = lot_volume - match_volume
            new_realized = int(lot['realized_volume'] or 0) + match_volume
            new_status = 'closed' if new_remaining <= 0 else 'open'

            cursor.execute("""
                INSERT INTO grid_lot_matches
                (session_id, stock_code, buy_lot_id, sell_trade_id, sell_order_id,
                 match_type, volume, buy_price, sell_price, buy_amount, sell_amount,
                 realized_pnl, matched_at)
                VALUES (?, ?, ?, ?, ?, 'matched', ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_data['session_id'],
                trade_data['stock_code'],
                lot['id'],
                sell_trade_id,
                sell_order_id,
                match_volume,
                buy_price,
                sell_price,
                buy_amount,
                sell_amount,
                realized_pnl,
                matched_at
            ))

            cursor.execute("""
                UPDATE grid_lots
                SET remaining_volume=?, realized_volume=?, status=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (new_remaining, new_realized, new_status, lot['id']))
            remaining -= match_volume

        if remaining > 0:
            sell_amount = sell_price * remaining
            cursor.execute("""
                INSERT INTO grid_lot_matches
                (session_id, stock_code, buy_lot_id, sell_trade_id, sell_order_id,
                 match_type, volume, buy_price, sell_price, buy_amount, sell_amount,
                 realized_pnl, matched_at)
                VALUES (?, ?, NULL, ?, ?, 'unmatched', ?, NULL, ?, 0, ?, 0, ?)
            """, (
                trade_data['session_id'],
                trade_data['stock_code'],
                sell_trade_id,
                sell_order_id,
                remaining,
                sell_price,
                sell_amount,
                matched_at
            ))

    def _apply_grid_ledger(self, cursor, trade_data: dict):
        """根据成交方向更新真实网格账本。"""
        trade_type = str(trade_data.get('trade_type') or '').upper()
        if trade_type == 'BUY':
            self._record_grid_buy_lot(cursor, trade_data)
        elif trade_type == 'SELL':
            self._match_grid_sell_lots(cursor, trade_data)

    def record_grid_trade_and_update_session(self, trade_data: dict, session_updates: dict,
                                             order_id: str = None, order_updates: dict = None) -> int:
        """在一个事务中写入成交明细、更新会话汇总、委托状态和真实网格账本。"""
        with self.lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute("BEGIN")
                if order_id:
                    trade_data = dict(trade_data)
                    trade_data['order_id'] = str(order_id)
                grid_trade_id = self.record_grid_trade(trade_data)

                self._apply_grid_ledger(cursor, trade_data)
                self.update_grid_session(trade_data['session_id'], session_updates)

                if order_id and order_updates:
                    self.update_grid_order(order_id, order_updates)

                self.conn.commit()
                return grid_trade_id
            except Exception:
                self.conn.rollback()
                raise

    def get_grid_lots(self, session_id: int, open_only: bool = False) -> list:
        """获取网格库存批次。"""
        with self.lock:
            cursor = self.conn.cursor()
            if open_only:
                cursor.execute("""
                    SELECT * FROM grid_lots
                    WHERE session_id=? AND remaining_volume > 0
                    ORDER BY opened_at ASC, id ASC
                """, (session_id,))
            else:
                cursor.execute("""
                    SELECT * FROM grid_lots
                    WHERE session_id=?
                    ORDER BY opened_at ASC, id ASC
                """, (session_id,))
            return [dict(row) for row in cursor.fetchall()]

    def get_grid_lot_matches(self, session_id: int) -> list:
        """获取网格批次匹配明细。"""
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT * FROM grid_lot_matches
                WHERE session_id=?
                ORDER BY matched_at ASC, id ASC
            """, (session_id,))
            return [dict(row) for row in cursor.fetchall()]

    def _get_grid_ledger_summary_unlocked(self, cursor, session_id: int,
                                          current_price: float = None) -> dict:
        cursor.execute("""
            SELECT
                COUNT(*) AS lot_count,
                COALESCE(SUM(original_volume), 0) AS bought_volume,
                COALESCE(SUM(remaining_volume), 0) AS open_volume,
                COALESCE(SUM(remaining_volume * buy_price), 0) AS open_cost
            FROM grid_lots
            WHERE session_id=?
        """, (session_id,))
        lot_row = dict(cursor.fetchone())

        cursor.execute("""
            SELECT
                COUNT(*) AS match_count,
                COALESCE(SUM(CASE WHEN match_type='matched' THEN volume ELSE 0 END), 0) AS matched_volume,
                COALESCE(SUM(CASE WHEN match_type='unmatched' THEN volume ELSE 0 END), 0) AS unmatched_volume,
                COALESCE(SUM(CASE WHEN match_type='matched' THEN realized_pnl ELSE 0 END), 0) AS realized_pnl
            FROM grid_lot_matches
            WHERE session_id=?
        """, (session_id,))
        match_row = dict(cursor.fetchone())

        current_price = float(current_price) if current_price is not None else None
        open_market_value = (
            float(lot_row['open_volume']) * current_price
            if current_price is not None and current_price > 0
            else 0.0
        )
        unrealized_pnl = open_market_value - float(lot_row['open_cost'])
        true_pnl = float(match_row['realized_pnl']) + unrealized_pnl

        return {
            'has_ledger': bool(lot_row['lot_count'] or match_row['match_count']),
            'lot_count': int(lot_row['lot_count'] or 0),
            'match_count': int(match_row['match_count'] or 0),
            'bought_volume': int(lot_row['bought_volume'] or 0),
            'open_volume': int(lot_row['open_volume'] or 0),
            'matched_volume': int(match_row['matched_volume'] or 0),
            'unmatched_volume': int(match_row['unmatched_volume'] or 0),
            'open_cost': float(lot_row['open_cost'] or 0.0),
            'open_market_value': open_market_value,
            'realized_pnl': float(match_row['realized_pnl'] or 0.0),
            'unrealized_pnl': unrealized_pnl,
            'true_pnl': true_pnl
        }

    def get_grid_ledger_summary(self, session_id: int, current_price: float = None) -> dict:
        """汇总真实网格账本盈亏。"""
        with self.lock:
            cursor = self.conn.cursor()
            return self._get_grid_ledger_summary_unlocked(cursor, session_id, current_price)

    def rebuild_grid_ledger_for_session(self, session_id: int) -> dict:
        """按成交明细重建单个会话账本，用于修复历史先卖后买未配对数据。"""
        with self.lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute("BEGIN")
                cursor.execute("""
                    SELECT * FROM grid_trades
                    WHERE session_id=?
                    ORDER BY trade_time ASC, id ASC
                """, (session_id,))
                trades = [dict(row) for row in cursor.fetchall()]
                if not trades:
                    summary = self._get_grid_ledger_summary_unlocked(cursor, session_id, None)
                    cursor.execute("""
                        SELECT current_investment
                        FROM grid_trading_sessions
                        WHERE id=?
                    """, (session_id,))
                    session_row = cursor.fetchone()
                    self.conn.commit()
                    summary['session_id'] = session_id
                    summary['trade_count'] = 0
                    summary['current_investment'] = (
                        float(session_row['current_investment'] or 0.0)
                        if session_row else summary['open_cost']
                    )
                    return summary

                cursor.execute("DELETE FROM grid_lot_matches WHERE session_id=?", (session_id,))
                cursor.execute("DELETE FROM grid_lots WHERE session_id=?", (session_id,))
                for trade in trades:
                    self._apply_grid_ledger(cursor, trade)

                summary = self._get_grid_ledger_summary_unlocked(cursor, session_id, None)
                cursor.execute("""
                    UPDATE grid_trading_sessions
                    SET current_investment=?, updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                """, (summary['open_cost'], session_id))
                self.conn.commit()
                summary['session_id'] = session_id
                summary['trade_count'] = len(trades)
                summary['current_investment'] = summary['open_cost']
                return summary
            except Exception:
                self.conn.rollback()
                raise

    def rebuild_grid_ledger(self) -> list:
        """按成交明细幂等重建所有网格会话账本。"""
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT DISTINCT session_id FROM grid_trades ORDER BY session_id")
            session_ids = [int(row[0]) for row in cursor.fetchall()]

        return [self.rebuild_grid_ledger_for_session(session_id) for session_id in session_ids]

    def get_grid_trades(self, session_id: int, limit=50, offset=0) -> list:
        """获取网格交易历史"""
        logger.debug(f"[GRID-DB] get_grid_trades: session_id={session_id}, limit={limit}, offset={offset}")

        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT * FROM grid_trades
                WHERE session_id=?
                ORDER BY trade_time DESC
                LIMIT ? OFFSET ?
            """, (session_id, limit, offset))
            results = [dict(row) for row in cursor.fetchall()]
            logger.debug(f"[GRID-DB] get_grid_trades: session_id={session_id}, 查询到 {len(results)} 条记录")
            return results

    def get_grid_session(self, session_id: int):
        """获取指定网格会话"""
        logger.debug(f"[GRID-DB] get_grid_session: session_id={session_id}")

        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT * FROM grid_trading_sessions
                WHERE id=?
            """, (session_id,))
            row = cursor.fetchone()
            result = dict(row) if row else None
            logger.debug(f"[GRID-DB] get_grid_session: session_id={session_id}, found={result is not None}")
            return result

    def get_grid_trade_count(self, session_id: int) -> int:
        """获取网格交易总数"""
        logger.debug(f"[GRID-DB] get_grid_trade_count: session_id={session_id}")

        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) FROM grid_trades WHERE session_id=?
            """, (session_id,))
            count = cursor.fetchone()[0]
            logger.debug(f"[GRID-DB] get_grid_trade_count: session_id={session_id}, count={count}")
            return count

    # ======================= 网格配置模板管理 =======================

    def save_grid_template(self, template_data: dict) -> int:
        """保存网格配置模板"""
        with self.lock:
            cursor = self.conn.cursor()

            # 如果设置为默认模板,先取消其他默认模板
            if template_data.get('is_default'):
                cursor.execute("""
                    UPDATE grid_config_templates SET is_default=FALSE
                """)

            cursor.execute("""
                INSERT INTO grid_config_templates
                (template_name, price_interval, position_ratio, callback_ratio,
                 max_deviation, target_profit, stop_loss, duration_days,
                 max_investment_ratio, description, is_default)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(template_name) DO UPDATE SET
                    price_interval=excluded.price_interval,
                    position_ratio=excluded.position_ratio,
                    callback_ratio=excluded.callback_ratio,
                    max_deviation=excluded.max_deviation,
                    target_profit=excluded.target_profit,
                    stop_loss=excluded.stop_loss,
                    duration_days=excluded.duration_days,
                    max_investment_ratio=excluded.max_investment_ratio,
                    description=excluded.description,
                    is_default=excluded.is_default,
                    updated_at=CURRENT_TIMESTAMP
            """, (
                template_data['template_name'],
                template_data.get('price_interval', 0.05),
                template_data.get('position_ratio', 0.25),
                template_data.get('callback_ratio', 0.005),
                template_data.get('max_deviation', 0.15),
                template_data.get('target_profit', 0.10),
                template_data.get('stop_loss', -0.10),
                template_data.get('duration_days', 7),
                template_data.get('max_investment_ratio', 0.5),
                template_data.get('description', ''),
                template_data.get('is_default', False)
            ))
            self.conn.commit()
            return cursor.lastrowid

    def get_grid_template(self, template_name: str):
        """获取指定网格配置模板"""
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT * FROM grid_config_templates WHERE template_name=?
            """, (template_name,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_all_grid_templates(self) -> list:
        """获取所有网格配置模板"""
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT * FROM grid_config_templates ORDER BY is_default DESC, created_at DESC
            """)
            return [dict(row) for row in cursor.fetchall()]

    def get_default_grid_template(self):
        """获取默认网格配置模板"""
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT * FROM grid_config_templates WHERE is_default=TRUE LIMIT 1
            """)
            row = cursor.fetchone()
            return dict(row) if row else None

    def delete_grid_template(self, template_name: str):
        """删除网格配置模板

        引用保护: 拒绝删除正在被活跃网格会话引用的模板，防止悬空引用。
        """
        with self.lock:
            # 检查是否有活跃会话正在引用此模板
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT id, stock_code FROM grid_trading_sessions
                WHERE template_name=? AND status='active'
                LIMIT 1
            """, (template_name,))
            active_ref = cursor.fetchone()
            if active_ref:
                ref_dict = dict(active_ref)
                raise ValueError(
                    f"模板'{template_name}'正在被活跃会话"
                    f"(id={ref_dict['id']}, {ref_dict['stock_code']})引用，"
                    f"请先停止该会话再删除模板"
                )

            cursor.execute("""
                DELETE FROM grid_config_templates WHERE template_name=?
            """, (template_name,))
            self.conn.commit()

    def increment_template_usage(self, template_name: str):
        """增加模板使用次数"""
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                UPDATE grid_config_templates
                SET usage_count = usage_count + 1,
                    last_used_at = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE template_name = ?
            """, (datetime.now().isoformat(), template_name))
            self.conn.commit()
            logger.debug(f"模板使用统计已更新: {template_name}")

    def init_risk_level_templates(self):
        """初始化三档风险等级预设模板

        ⚠️ 重要: 止损比例已调整
        - 激进型: -15% (容忍大回撤)
        - 稳健型: -10% (平衡)
        - 保守型: -8% (快速止损)
        """
        logger.info("=" * 50)
        logger.info("开始初始化风险等级模板...")
        logger.info("=" * 50)

        templates = [
            {
                'template_name': '激进型网格',
                'price_interval': 0.03,
                'position_ratio': 0.30,
                'callback_ratio': 0.003,
                'max_deviation': 0.10,
                'target_profit': 0.15,
                'stop_loss': -0.15,  # ⚠️ 调整后: 容忍大回撤
                'duration_days': 7,
                'max_investment_ratio': 0.5,
                'description': '适合高波动成长股,档位密集(3%),容忍大回撤(-15%),追求高收益(+15%)',
                'is_default': False
            },
            {
                'template_name': '稳健型网格',
                'price_interval': 0.05,
                'position_ratio': 0.25,
                'callback_ratio': 0.005,
                'max_deviation': 0.15,
                'target_profit': 0.10,
                'stop_loss': -0.10,
                'duration_days': 7,
                'max_investment_ratio': 0.5,
                'description': '适合主流蓝筹股,平衡风险收益,默认推荐策略',
                'is_default': True  # 默认模板
            },
            {
                'template_name': '保守型网格',
                'price_interval': 0.08,
                'position_ratio': 0.20,
                'callback_ratio': 0.008,
                'max_deviation': 0.20,
                'target_profit': 0.08,
                'stop_loss': -0.08,  # ⚠️ 调整后: 快速止损
                'duration_days': 7,
                'max_investment_ratio': 0.5,
                'description': '适合低波动指数或大盘股,档位稀疏(8%),快速止损(-8%),稳健盈利(+8%)',
                'is_default': False
            }
        ]

        initialized_count = 0
        skipped_count = 0

        for template in templates:
            try:
                # 检查是否已存在,避免重复插入
                existing = self.get_grid_template(template['template_name'])
                if existing:
                    logger.info(f"⏭️  模板已存在,跳过: {template['template_name']}")
                    skipped_count += 1
                    continue

                self.save_grid_template(template)
                logger.info(f"✅ 初始化模板成功: {template['template_name']}")
                logger.info(f"   - 档位间隔: {template['price_interval']*100:.2f}%")
                logger.info(f"   - 目标盈利: {template['target_profit']*100:.2f}%")
                logger.info(f"   - 止损比例: {template['stop_loss']*100:.2f}%")
                initialized_count += 1
            except Exception as e:
                logger.error(f"❌ 初始化模板失败: {template['template_name']}, 错误: {str(e)}")

        logger.info("=" * 50)
        logger.info(f"风险等级模板初始化完成: 新增{initialized_count}个, 跳过{skipped_count}个")
        logger.info("=" * 50)

        return initialized_count

    def close(self):
        """关闭数据库连接"""
        if self.conn:
            self.conn.close()
            logger.info("数据库连接已关闭")
