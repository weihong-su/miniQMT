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
        """初始化基础表(持仓、交易记录)"""
        cursor = self.conn.cursor()

        # 创建持仓表
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

        # 创建交易记录表
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
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS grid_trading_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stock_code TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',

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

                UNIQUE(stock_code, status) ON CONFLICT REPLACE
            )
        """)

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
        """创建网格会话"""
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT INTO grid_trading_sessions
                (stock_code, status, center_price, current_center_price,
                 price_interval, position_ratio, callback_ratio,
                 max_investment, max_deviation, target_profit, stop_loss,
                 start_time, end_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                session_data['stock_code'],
                'active',
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
                session_data['end_time']
            ))
            self.conn.commit()
            return cursor.lastrowid

    def update_grid_session(self, session_id: int, updates: dict):
        """更新网格会话"""
        with self.lock:
            set_clause = ', '.join([f"{k}=?" for k in updates.keys()])
            values = list(updates.values()) + [session_id]

            cursor = self.conn.cursor()
            cursor.execute(f"""
                UPDATE grid_trading_sessions
                SET {set_clause}, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, values)
            self.conn.commit()

    def stop_grid_session(self, session_id: int, reason: str):
        """停止网格会话"""
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                UPDATE grid_trading_sessions
                SET status=?, stop_time=?, stop_reason=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, ('stopped', datetime.now().isoformat(), reason, session_id))
            self.conn.commit()

    def get_active_grid_sessions(self) -> list:
        """获取所有活跃的网格会话"""
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT * FROM grid_trading_sessions
                WHERE status='active'
                ORDER BY start_time DESC
            """)
            return cursor.fetchall()

    def get_grid_session_by_stock(self, stock_code: str):
        """获取指定股票的活跃网格会话"""
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT * FROM grid_trading_sessions
                WHERE stock_code=? AND status='active'
                LIMIT 1
            """, (stock_code,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def record_grid_trade(self, trade_data: dict) -> int:
        """记录网格交易"""
        with self.lock:
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
            self.conn.commit()
            return cursor.lastrowid

    def get_grid_trades(self, session_id: int, limit=50, offset=0) -> list:
        """获取网格交易历史"""
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT * FROM grid_trades
                WHERE session_id=?
                ORDER BY trade_time DESC
                LIMIT ? OFFSET ?
            """, (session_id, limit, offset))
            return [dict(row) for row in cursor.fetchall()]

    def get_grid_trade_count(self, session_id: int) -> int:
        """获取网格交易总数"""
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) FROM grid_trades WHERE session_id=?
            """, (session_id,))
            return cursor.fetchone()[0]

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
        """删除网格配置模板"""
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                DELETE FROM grid_config_templates WHERE template_name=?
            """, (template_name,))
            self.conn.commit()

    def close(self):
        """关闭数据库连接"""
        if self.conn:
            self.conn.close()
            logger.info("数据库连接已关闭")
