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
        # 优化: 移除UNIQUE(stock_code, status) ON CONFLICT REPLACE约束
        # 改用应用层检查,确保一个股票只有一个active session
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
                (stock_code, status, center_price, current_center_price,
                 price_interval, position_ratio, callback_ratio,
                 max_investment, max_deviation, target_profit, stop_loss,
                 start_time, end_time, risk_level, template_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            trade_id = cursor.lastrowid
            logger.info(f"[GRID-DB] record_grid_trade: 记录成功 id={trade_id}, session_id={trade_data.get('session_id')}, "
                       f"trade_type={trade_data.get('trade_type')}, volume={trade_data.get('volume')}, amount={trade_data.get('amount')}")
            return trade_id

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
        """删除网格配置模板"""
        with self.lock:
            cursor = self.conn.cursor()
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
        logger.info("=" * 60)
        logger.info("开始初始化风险等级模板...")
        logger.info("=" * 60)

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
                logger.info(f"   - 档位间隔: {template['price_interval']*100}%")
                logger.info(f"   - 目标盈利: {template['target_profit']*100}%")
                logger.info(f"   - 止损比例: {template['stop_loss']*100}%")
                initialized_count += 1
            except Exception as e:
                logger.error(f"❌ 初始化模板失败: {template['template_name']}, 错误: {str(e)}")

        logger.info("=" * 60)
        logger.info(f"风险等级模板初始化完成: 新增{initialized_count}个, 跳过{skipped_count}个")
        logger.info("=" * 60)

        return initialized_count

    def close(self):
        """关闭数据库连接"""
        if self.conn:
            self.conn.close()
            logger.info("数据库连接已关闭")
