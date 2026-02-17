"""
持仓管理模块，负责跟踪和管理持仓
优化版本：统一止盈止损判断逻辑，支持模拟交易直接持仓调整
"""
import pandas as pd
import sqlite3
from datetime import datetime
import time
import threading
import sys
import os
import json
import Methods
import config
from logger import get_logger
from data_manager import get_data_manager
from easy_qmt_trader import easy_qmt_trader


# 获取logger
logger = get_logger("position_manager")

class PositionManager:
    """持仓管理类，负责跟踪和管理持仓"""
    
    def __init__(self):
        """初始化持仓管理器"""
        self.data_manager = get_data_manager()
        self.conn = self.data_manager.conn
        self.stock_positions_file = config.STOCK_POOL_FILE

        # 持仓监控线程
        self.monitor_thread = None
        self.stop_flag = False
        
        # 初始化easy_qmt_trader
        account_config = config.get_account_config()
        self.qmt_trader = easy_qmt_trader(
            path= config.QMT_PATH,
            account=account_config.get('account_id'),
            account_type=account_config.get('account_type', 'STOCK')
        )

        # 🔧 修复：检查QMT连接结果
        connect_result = self.qmt_trader.connect()

        if connect_result is None:
            logger.error("❌ QMT未连接")
            logger.warning("⚠️ 离线模式")
            # 🔧 设置标志位，标记QMT未连接
            self.qmt_connected = False
        else:
            logger.info("✅ QMT已连接")
            self.qmt_connected = True

        # 创建内存数据库
        self.memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
        # 🔒 C2修复：添加内存数据库连接线程安全锁
        self.memory_conn_lock = threading.Lock()
        self._create_memory_table()
        self._sync_db_to_memory()

        # 添加模拟交易模式的提示日志
        if hasattr(config, 'ENABLE_SIMULATION_MODE') and config.ENABLE_SIMULATION_MODE:
            logger.warning("模拟模式:仅内存持仓")

        # 添加缓存机制
        self.last_position_update_time = 0
        self.position_update_interval = config.QMT_POSITION_QUERY_INTERVAL  # ⭐ 优化: 使用配置10秒
        self.positions_cache = None        

        # 新增，持仓数据版本控制
        self.data_version = 0
        self.data_changed = False
        self.version_lock = threading.Lock()

        # 新增：全量刷新控制 - 在这里添加缺失的属性
        self.last_full_refresh_time = 0
        self.full_refresh_interval = 60  # 1分钟全量刷新间隔
        # 最高价低频校准（避免每轮阻塞）
        self.last_update_highest_time = 0
        self.update_highest_interval = 60  # 秒
        # 行情缓存（用于最高价校准，避免频繁调用行情接口）
        self.history_high_cache = {}  # {stock_code: {'high': float, 'open_date': str, 'ts': float}}
        self.history_high_cache_ttl = 3600  # 1小时刷新一次

        # 新增：定期版本升级控制
        self.last_version_increment_time = time.time()
        self.version_increment_interval = config.VERSION_INCREMENT_INTERVAL if hasattr(config, 'VERSION_INCREMENT_INTERVAL') else 15  # 默认15秒

        # 定时同步线程
        self.sync_thread = None
        self.sync_stop_flag = False
        self.start_sync_thread()

        # 添加信号状态管理
        self.signal_lock = threading.Lock()
        self.latest_signals = {}  # 存储最新检测到的信号
        self.signal_timestamps = {}  # 信号时间戳

        # 🔑 新增：委托单跟踪管理
        self.pending_orders_lock = threading.Lock()
        self.pending_orders = {}  # 存储待处理的委托单: {stock_code: {'order_id', 'submit_time', 'signal_type', ...}}
        self.order_check_interval = 30  # 委托单检查间隔（秒）
        self.last_order_check_time = 0

        # 🔴 P0修复：添加同步操作线程锁，防止并发调用导致递归异常
        self.sync_lock = threading.RLock()  # 可重入锁
        self._deleting_stocks = set()  # 正在删除的股票代码集合

        # 网格交易数据库管理器(用于网格交易会话和记录)
        if config.ENABLE_GRID_TRADING:
            try:
                from grid_database import DatabaseManager
                self.db_manager = DatabaseManager()
                # 自动初始化网格交易表
                self.db_manager.init_grid_tables()
                logger.info("网格交易数据库管理器初始化完成")
            except Exception as e:
                logger.error(f"网格交易数据库管理器初始化失败: {str(e)}")
                self.db_manager = None
        else:
            self.db_manager = None

        # 网格交易管理器(延迟初始化)
        self.grid_manager = None


    def _increment_data_version(self):
        """递增数据版本号（内部方法）"""
        with self.version_lock:
            self.data_version += 1
            self.data_changed = True
            logger.debug(f"持仓数据版本更新: v{self.data_version}")

    def increment_data_version(self):
        """递增数据版本号（公开方法，供外部模块调用）"""
        self._increment_data_version()

    def _create_memory_table(self):
        """创建内存数据库表结构"""
        with self.memory_conn_lock:
            cursor = self.memory_conn.cursor()
            cursor.execute('''
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
            ''')
            self.memory_conn.commit()
        logger.info("内存表已创建")

    def _sync_real_positions_to_memory(self, real_positions_df):
        """将实盘持仓数据同步到内存数据库"""
        # 🔴 P0修复：添加线程锁保护，防止并发调用
        with self.sync_lock:
            try:
                # 首先检查输入数据
                if real_positions_df is None or not isinstance(real_positions_df, pd.DataFrame) or real_positions_df.empty:
                    logger.warning("实盘数据无效,跳过")
                    return

                # 确保必要的列存在
                required_columns = ['证券代码', '股票余额', '可用余额', '成本价', '市值']
                missing_columns = [col for col in required_columns if col not in real_positions_df.columns]
                if missing_columns:
                    logger.warning(f"缺少列:{missing_columns}")
                    return

                # 获取内存数据库中所有持仓的股票代码（P0修复: 添加锁保护）
                with self.memory_conn_lock:
                    cursor = self.memory_conn.cursor()
                    cursor.execute("SELECT stock_code FROM positions")
                    memory_stock_codes = {row[0] for row in cursor.fetchall() if row[0] is not None}
                current_positions = set()

                # 新增：记录更新过程中的错误
                update_errors = []

                # 遍历实盘持仓数据
                for _, row in real_positions_df.iterrows():
                    try:
                        # 安全提取并转换数据
                        stock_code = str(row['证券代码']) if row['证券代码'] is not None else None
                        if not stock_code:
                            continue  # 跳过无效数据

                        # 安全提取并转换数值
                        try:
                            volume = int(float(row['股票余额'])) if row['股票余额'] is not None else 0
                        except (ValueError, TypeError):
                            volume = 0

                        try:
                            available = int(float(row['可用余额'])) if row['可用余额'] is not None else 0
                        except (ValueError, TypeError):
                            available = 0

                        try:
                            cost_price = float(row['成本价']) if row['成本价'] is not None else 0.0
                        except (ValueError, TypeError):
                            cost_price = 0.0

                        try:
                            market_value = float(row['市值']) if row['市值'] is not None else 0.0
                        except (ValueError, TypeError):
                            market_value = 0.0

                        # 获取当前价格
                        current_price = cost_price  # 默认使用成本价
                        try:
                            latest_quote = self.data_manager.get_latest_data(stock_code)
                            if latest_quote and isinstance(latest_quote, dict) and 'lastPrice' in latest_quote and latest_quote['lastPrice'] is not None:
                                current_price = float(latest_quote['lastPrice'])
                        except Exception as e:
                            logger.warning(f"{stock_code[:6]} 价格失败→成本价")

                        # 查询内存数据库中是否已存在该股票的持仓记录
                        # 🔧 修复: 同时查询base_cost_price,用于处理QMT成本价异常的情况
                        cursor.execute("SELECT profit_triggered, open_date, highest_price, stop_loss_price, base_cost_price FROM positions WHERE stock_code=?", (stock_code,))
                        result = cursor.fetchone()

                        if result:
                            # 如果存在，则更新持仓信息，但不修改open_date
                            profit_triggered = result[0] if result[0] is not None else False
                            open_date = result[1] if result[1] is not None else datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            highest_price = result[2] if result[2] is not None else 0.0
                            stop_loss_price = result[3] if result[3] is not None else 0.0
                            base_cost_price = result[4] if result[4] is not None else None

                            # 所有参数都确保有有效值
                            self.update_position(
                                stock_code=stock_code,
                                volume=volume,
                                cost_price=cost_price,
                                available=available,
                                market_value=market_value,
                                current_price=current_price,
                                profit_triggered=profit_triggered,
                                highest_price=highest_price,
                                open_date=open_date,
                                stop_loss_price=stop_loss_price,
                                base_cost_price=base_cost_price  # 🔧 传递base_cost_price
                            )
                        else:
                            # 如果不存在，则新增持仓记录
                            self.update_position(
                                stock_code=stock_code,
                                volume=volume,
                                cost_price=cost_price,
                                available=available,
                                market_value=market_value,
                                current_price=current_price,
                                open_date=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            )

                        # 添加到当前持仓集合
                        current_positions.add(stock_code)
                        memory_stock_codes.discard(stock_code)

                    except Exception as e:
                        logger.error(f"处理持仓行数据时出错: {str(e)}")
                        update_errors.append(f"处理 {stock_code if 'stock_code' in locals() else '未知'} 时出错: {str(e)}")
                        continue  # 跳过这一行，继续处理其他行

                # 关键修改：只有在没有更新错误且数据完整时才执行删除
                if update_errors:
                    logger.error(f"数据更新过程中出现 {len(update_errors)} 个错误，跳过删除操作以保护数据")
                    for error in update_errors:
                        logger.error(f"  - {error}")
                    return

                # 数据完整性检查
                if len(current_positions) == 0:
                    logger.warning("外部持仓数据为空，可能是接口异常，跳过删除操作")
                    return

                # 数据量合理性检查
                if len(memory_stock_codes) > 0 and len(current_positions) < len(memory_stock_codes) * 0.3:
                    logger.warning(f"外部持仓数据过少 ({len(current_positions)}) 相比内存数据 ({len(memory_stock_codes)})，可能是接口异常，跳过删除操作")
                    return

                # 修改：在模拟交易模式下，不删除内存中存在但实盘中不存在的持仓记录
                if not hasattr(config, 'ENABLE_SIMULATION_MODE') or not config.ENABLE_SIMULATION_MODE:
                    # 🔴 P0修复：优化删除逻辑，添加去重和中断机制
                    if memory_stock_codes:  # 有需要删除的记录
                        # 检查是否已在删除中（去重机制）
                        stocks_to_delete = memory_stock_codes - self._deleting_stocks
                        if not stocks_to_delete:
                            logger.debug(f"所有待删除股票 {list(memory_stock_codes)} 正在处理中，跳过重复删除")
                            return

                        # 标记正在删除
                        self._deleting_stocks.update(stocks_to_delete)

                        try:
                            logger.info(f"准备删除 {len(stocks_to_delete)} 个不在外部数据中的持仓: {list(stocks_to_delete)}")

                            # 逐个删除并记录结果
                            successfully_deleted = []
                            failed_deletions = []

                            for stock_code in stocks_to_delete:
                                if stock_code:
                                    try:
                                        if self.remove_position(stock_code):
                                            successfully_deleted.append(stock_code)
                                        else:
                                            failed_deletions.append(stock_code)
                                    except Exception as e:
                                        logger.error(f"删除 {stock_code} 时出错: {str(e)}")
                                        failed_deletions.append(stock_code)

                            if successfully_deleted:
                                logger.info(f"成功删除持仓: {successfully_deleted}")
                            if failed_deletions:
                                logger.error(f"删除失败的持仓: {failed_deletions}")
                        finally:
                            # 删除完成后清除标记
                            self._deleting_stocks -= stocks_to_delete
                else:
                    logger.info(f"模拟交易模式：保留内存中的模拟持仓记录，不与实盘同步删除")

                # 更新 stock_positions.json
                self._update_stock_positions_file(current_positions)

            except Exception as e:
                logger.error(f"同步实盘持仓数据到内存数据库时出错: {str(e)}")
                # P0修复: rollback也需要锁保护
                with self.memory_conn_lock:
                    self.memory_conn.rollback()

    def _sync_db_to_memory(self):
        """将数据库数据同步到内存数据库"""
        try:
            db_positions = pd.read_sql_query("SELECT * FROM positions", self.conn)
            if not db_positions.empty:
                # 确保stock_name字段存在，如果不存在则添加默认值
                if 'stock_name' not in db_positions.columns:
                    db_positions['stock_name'] = db_positions['stock_code']  # 使用股票代码作为默认名称
                    logger.warning("SQLite数据库中缺少stock_name字段，使用股票代码作为默认值")

                # 确保base_cost_price字段存在，如果不存在则使用cost_price
                if 'base_cost_price' not in db_positions.columns:
                    db_positions['base_cost_price'] = db_positions['cost_price']
                    logger.warning("SQLite数据库中缺少base_cost_price字段，使用cost_price作为默认值")

                with self.memory_conn_lock:
                    db_positions.to_sql("positions", self.memory_conn, if_exists="replace", index=False)
                    self.memory_conn.commit()
                logger.info("DB→内存同步完成")
        except Exception as e:
            logger.error(f"数据库数据同步到内存数据库时出错: {str(e)}")

    def _sync_memory_to_db(self):
        """将内存数据库数据同步到数据库"""
        try:
            # 添加模拟交易模式检查，模拟模式下不同步到SQLite
            if hasattr(config, 'ENABLE_SIMULATION_MODE') and config.ENABLE_SIMULATION_MODE:
                logger.debug("模拟交易模式：跳过内存数据库到SQLite数据库的同步")
                return

            # 添加交易时间检查 - 非交易时间不同步到SQLite
            if not config.is_trade_time():
                logger.debug("非交易时间，跳过内存数据库到SQLite的同步")
                return

            # 使用独立的数据库连接避免事务冲突
            sync_db_conn = sqlite3.connect(config.DB_PATH)
            sync_db_conn.execute("PRAGMA busy_timeout = 30000")  # 设置30秒超时

            try:
                # 获取内存数据库中的所有股票代码
                with self.memory_conn_lock:
                    memory_positions = pd.read_sql_query("SELECT * FROM positions", self.memory_conn)
                memory_stock_codes = set(memory_positions['stock_code'].tolist()) if not memory_positions.empty else set()

                # 获取SQLite数据库中的所有股票代码
                cursor = sync_db_conn.cursor()
                cursor.execute("SELECT stock_code FROM positions")
                sqlite_stock_codes = {row[0] for row in cursor.fetchall() if row[0] is not None}

                # 删除SQLite中存在但内存数据库中不存在的记录
                stocks_to_delete = sqlite_stock_codes - memory_stock_codes
                if stocks_to_delete:
                    deleted_count = 0
                    for stock_code in stocks_to_delete:
                        try:
                            cursor.execute("DELETE FROM positions WHERE stock_code=?", (stock_code,))
                            if cursor.rowcount > 0:
                                deleted_count += 1
                                logger.info(f"从SQLite删除持仓记录: {stock_code}")
                        except Exception as e:
                            logger.error(f"删除SQLite中的 {stock_code} 记录时出错: {str(e)}")

                    if deleted_count > 0:
                        logger.info(f"SQLite同步：删除了 {deleted_count} 个过期的持仓记录")

                if not memory_positions.empty:
                    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    update_count = 0
                    insert_count = 0

                    for _, row in memory_positions.iterrows():
                        stock_code = row['stock_code']
                        stock_name = row['stock_name']
                        volume = row['volume']
                        available = row['available']
                        cost_price = row['cost_price']
                        open_date = row['open_date']
                        profit_triggered = row['profit_triggered']
                        highest_price = row['highest_price']
                        stop_loss_price = row['stop_loss_price']
                        base_cost_price = row['base_cost_price']
                        profit_breakout_triggered = row['profit_breakout_triggered']
                        breakout_highest_price = row['breakout_highest_price']

                        # 查询数据库中的对应记录
                        cursor.execute("SELECT stock_name, open_date, profit_triggered, highest_price, stop_loss_price, profit_breakout_triggered, breakout_highest_price FROM positions WHERE stock_code=?", (stock_code,))
                        db_row = cursor.fetchone()

                        if db_row:
                            db_stock_name, db_open_date, db_profit_triggered, db_highest_price, db_stop_loss_price, db_profit_breakout_triggered, db_breakout_highest_price = db_row
                            # 比较字段是否不同
                            if (db_stock_name != stock_name) or (db_open_date != open_date) or (db_profit_triggered != profit_triggered) or (db_highest_price != highest_price) or (db_stop_loss_price != stop_loss_price) or (db_profit_breakout_triggered != profit_breakout_triggered) or (db_breakout_highest_price != breakout_highest_price):
                                # 如果内存数据库中的 open_date 与 SQLite 数据库中的不一致，则使用 SQLite 数据库中的值
                                if db_open_date != open_date:
                                    open_date = db_open_date
                                    # row['open_date'] = open_date  # 更新内存数据库中的 open_date
                                    with self.memory_conn_lock:
                                        memory_cursor = self.memory_conn.cursor()
                                        memory_cursor.execute("UPDATE positions SET open_date=? WHERE stock_code=?", (open_date, stock_code))
                                        self.memory_conn.commit()

                                if db_profit_triggered != profit_triggered:
                                    logger.info(f"---内存数据库的 {stock_code} 的profit_triggered与sqlite不一致---")
                                # 更新数据库，确保所有字段都得到更新
                                cursor.execute("""
                                    UPDATE positions
                                    SET stock_name=?, open_date=?, profit_triggered=?, highest_price=?, stop_loss_price=?, profit_breakout_triggered=?, breakout_highest_price=?, last_update=?
                                    WHERE stock_code=?
                                """, (stock_name, open_date, profit_triggered, highest_price, stop_loss_price, profit_breakout_triggered, breakout_highest_price, now, stock_code))
                                update_count += 1
                                logger.debug(f"更新SQLite记录: {stock_code}, 最高价:{highest_price}, 止损价:{stop_loss_price}")
                        else:
                            # 插入新记录，使用当前日期作为 open_date
                            current_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            cursor.execute("""
                                INSERT INTO positions (stock_code, stock_name, volume, available, cost_price, base_cost_price, open_date, profit_triggered, highest_price, stop_loss_price, profit_breakout_triggered, breakout_highest_price, last_update)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (stock_code, stock_name, volume, available, cost_price, base_cost_price, current_date, profit_triggered, highest_price, stop_loss_price, profit_breakout_triggered, breakout_highest_price, now))

                            insert_count += 1
                            # 插入新记录后，立即从数据库读取 open_date，以确保内存数据库与数据库一致
                            cursor.execute("SELECT open_date FROM positions WHERE stock_code=?", (stock_code,))
                            db_open_date = cursor.fetchone()[0]
                            with self.memory_conn_lock:
                                memory_cursor = self.memory_conn.cursor()
                                memory_cursor.execute("UPDATE positions SET open_date=? WHERE stock_code=?", (db_open_date, stock_code))
                                self.memory_conn.commit()
                            logger.info(f"插入新的SQLite记录: {stock_code}, 使用日期: {current_date}")


                    sync_db_conn.commit()
                    # 只在有实际变化时输出日志
                    if insert_count > 0:
                        logger.info(f"SQLite同步: 更新{update_count}条, 插入{insert_count}条新记录")
                    elif (update_count > 0) and (config.VERBOSE_LOOP_LOGGING or config.DEBUG):
                        logger.debug(f"SQLite同步: 更新{update_count}条持仓数据")

            except Exception as e:
                logger.error(f"独立连接同步失败: {str(e)}")
                sync_db_conn.rollback()
                raise
            finally:
                sync_db_conn.close()

        except Exception as e:
            logger.error(f"内存数据库数据同步到数据库时出错: {str(e)}")
            # 添加重试机制
            if not hasattr(self, '_sync_retry_count'):
                self._sync_retry_count = 0

            self._sync_retry_count += 1
            if self._sync_retry_count <= 2:  # 最多重试2次
                logger.info(f"安排第 {self._sync_retry_count} 次同步重试，5秒后执行")
                threading.Timer(5.0, self._retry_sync).start()
            else:
                logger.error("同步重试次数已达上限，重置计数器")
                self._sync_retry_count = 0

    def _retry_sync(self):
        """重试同步"""
        try:
            logger.info("执行同步重试")
            self._sync_memory_to_db()
            # 重试成功，重置计数器
            self._sync_retry_count = 0
            logger.info("同步重试成功")
        except Exception as e:
            logger.error(f"同步重试失败: {str(e)}")

    def start_sync_thread(self):
        """启动定时同步线程"""
        self.sync_stop_flag = False
        self.sync_thread = threading.Thread(target=self._sync_loop)
        self.sync_thread.daemon = True
        self.sync_thread.start()
        logger.info("同步线程启动")

    def stop_sync_thread(self):
        """停止定时同步线程"""
        if self.sync_thread and self.sync_thread.is_alive():
            self.sync_stop_flag = True
            self.sync_thread.join(timeout=5)
            logger.info("同步线程停止")

    # position_manager.py:_sync_loop() 方法修改
    def _sync_loop(self):
        """定时同步循环 - 增强版（支持定期版本升级）"""
        while not self.sync_stop_flag:
            try:
                # 原有的数据库同步
                self._sync_memory_to_db()

                # 新增：每1分钟执行一次全量刷新
                current_time = time.time()
                if (current_time - self.last_full_refresh_time) >= self.full_refresh_interval:
                    if hasattr(config, 'ENABLE_SIMULATION_MODE') and config.ENABLE_SIMULATION_MODE:
                        logger.info("执行模拟交易全量数据刷新")
                        self._full_refresh_simulation_data()
                        self.last_full_refresh_time = current_time

                # 新增：模拟交易模式下的价格更新
                if hasattr(config, 'ENABLE_SIMULATION_MODE') and config.ENABLE_SIMULATION_MODE:
                    # 在交易时间内更频繁地更新价格
                    if config.is_trade_time():
                        logger.debug("模拟交易模式：更新持仓价格和指标")
                        self.update_all_positions_price()  # 更新价格
                        self._increment_data_version()      # 触发版本更新

                # ⭐ 新增：定期自动升级版本号（确保Web界面定期刷新）
                if (current_time - self.last_version_increment_time) >= self.version_increment_interval:
                    with self.version_lock:
                        self.data_version += 1
                        self.last_version_increment_time = current_time
                        # logger.info(f"⏰ 定期版本升级: v{self.data_version} (间隔: {self.version_increment_interval}秒)")

                # ⭐ 优化: 使用配置文件中的同步间隔(15秒)
                sleep_time = int(config.POSITION_SYNC_INTERVAL)

                for _ in range(sleep_time):
                    if self.sync_stop_flag:
                        break
                    time.sleep(1)

            except Exception as e:
                logger.error(f"定时同步循环出错: {str(e)}")
                time.sleep(60)  # 出错后等待一分钟再继续

    def get_all_positions(self):
        """获取所有持仓"""
        try:
            # 模拟模式：直接从内存数据库返回，不调用实盘API
            if hasattr(config, 'ENABLE_SIMULATION_MODE') and config.ENABLE_SIMULATION_MODE:
                with self.memory_conn_lock:
                    query = "SELECT * FROM positions"
                    positions_df = pd.read_sql_query(query, self.memory_conn)

                # 确保数值列类型正确
                if not positions_df.empty:
                    numeric_columns = ['volume', 'available', 'cost_price', 'current_price',
                                        'market_value', 'profit_ratio', 'highest_price', 'stop_loss_price','breakout_highest_price']
                    for col in numeric_columns:
                        if col in positions_df.columns:
                            positions_df[col] = pd.to_numeric(positions_df[col], errors='coerce').fillna(0)

                    if 'profit_triggered' in positions_df.columns:
                        positions_df['profit_triggered'] = positions_df['profit_triggered'].fillna(False)
                    if 'profit_breakout_triggered' in positions_df.columns:
                        positions_df['profit_breakout_triggered'] = positions_df['profit_breakout_triggered'].fillna(False)

                return positions_df.copy() if not positions_df.empty else pd.DataFrame()

            # 实盘模式：调用QMT API
            current_time = time.time()

            # 只在时间间隔到达后更新数据
            if (current_time - self.last_position_update_time) >= self.position_update_interval:
                # 获取实盘持仓数据
                try:
                    real_positions_df = self.qmt_trader.position()

                    # 检查实盘数据
                    if real_positions_df is None:
                        logger.warning("实盘持仓数据获取失败，返回None")
                        real_positions_df = pd.DataFrame()  # 使用空DataFrame而不是None
                    elif not isinstance(real_positions_df, pd.DataFrame):
                        logger.warning(f"实盘持仓数据类型错误: {type(real_positions_df)}，将转换为DataFrame")
                        try:
                            # 尝试转换为DataFrame
                            real_positions_df = pd.DataFrame(real_positions_df)
                        except:
                            real_positions_df = pd.DataFrame()  # 转换失败则使用空DataFrame

                    # 同步实盘持仓数据到内存数据库
                    if not real_positions_df.empty:
                        self._sync_real_positions_to_memory(real_positions_df)

                    # 读取数据到局部变量，避免就地修改 self.positions_cache 时与其他
                    # 线程的 .copy() 调用产生竞态（Gaps in blk ref_locs）
                    with self.memory_conn_lock:
                        query = "SELECT * FROM positions"
                        new_cache = pd.read_sql_query(query, self.memory_conn)

                    # 在局部变量上完成所有列类型修正，不触碰 self.positions_cache
                    if not new_cache.empty:
                        numeric_columns = ['volume', 'available', 'cost_price', 'current_price',
                                            'market_value', 'profit_ratio', 'highest_price', 'stop_loss_price','breakout_highest_price']
                        for col in numeric_columns:
                            if col in new_cache.columns:
                                new_cache[col] = pd.to_numeric(new_cache[col], errors='coerce').fillna(0)

                        if 'profit_triggered' in new_cache.columns:
                            new_cache['profit_triggered'] = new_cache['profit_triggered'].fillna(False)

                        if 'profit_breakout_triggered' in new_cache.columns:
                            new_cache['profit_breakout_triggered'] = new_cache['profit_breakout_triggered'].fillna(False)

                    # 原子赋值：CPython STORE_ATTR 是单字节码操作，确保其他线程
                    # 读到的 self.positions_cache 始终是完整对象
                    self.positions_cache = new_cache
                    self.last_position_update_time = current_time
                    logger.debug(f"更新持仓缓存，共 {len(self.positions_cache)} 条记录")
                except Exception as e:
                    logger.error(f"获取和处理持仓数据时出错: {str(e)}")
                    # 如果出错，返回上次的缓存，或者空DataFrame
                    if self.positions_cache is None:
                        self.positions_cache = pd.DataFrame()

            # 返回缓存数据的副本
            return self.positions_cache.copy() if self.positions_cache is not None else pd.DataFrame()
        except Exception as e:
            logger.error(f"获取所有持仓信息时出错: {str(e)}")
            return pd.DataFrame()  # 出错时返回空DataFrame
    
    def get_position(self, stock_code):
        """
        获取指定股票的持仓 - 修复版本：基于get_all_positions从QMT接口获取最新持仓
        🔑 关键修复：使用字典映射避免字段索引依赖
        """
        try:
            if not stock_code:
                return None
                
            # 🔑 关键修复：从QMT接口获取所有最新持仓
            all_positions = self.get_all_positions()
            
            if all_positions is None or all_positions.empty:
                logger.debug(f"{stock_code} 未找到任何持仓")
                return None
            
            # 🔑 标准化股票代码进行匹配（处理带后缀的情况）
            stock_code_simple = stock_code.split('.')[0] if '.' in stock_code else stock_code
            
            # 🔑 从QMT持仓数据中筛选指定股票（避免字段索引依赖）
            position_row = None
            
            # 检查可能的股票代码字段名
            possible_code_fields = ['stock_code', '证券代码', 'code']
            code_field = None
            
            for field in possible_code_fields:
                if field in all_positions.columns:
                    code_field = field
                    break
            
            if code_field is None:
                logger.error(f"持仓数据中未找到股票代码字段，可用字段: {list(all_positions.columns)}")
                return None
            
            # 筛选指定股票
            for _, row in all_positions.iterrows():
                row_stock_code = str(row[code_field])
                row_stock_code_simple = row_stock_code.split('.')[0] if '.' in row_stock_code else row_stock_code
                
                if row_stock_code_simple == stock_code_simple:
                    position_row = row
                    break
            
            if position_row is None:
                logger.debug(f"{stock_code} 在持仓中未找到")
                return None
            
            # 🔑 字段映射：将QMT中文字段名映射到标准英文字段名
            field_mapping = {
                # QMT接口字段名 -> 标准字段名
                '证券代码': 'stock_code',
                '证券名称': 'stock_name', 
                '股票余额': 'volume',
                '可用余额': 'available',
                '成本价': 'cost_price',
                '参考成本价': 'cost_price',
                '平均建仓成本': 'cost_price',
                '市值': 'market_value',
                '市价': 'current_price',
                '盈亏': 'profit_loss',
                '盈亏比(%)': 'profit_ratio',
                
                # 如果已经是英文字段名，保持不变
                'stock_code': 'stock_code',
                'stock_name': 'stock_name',
                'volume': 'volume',
                'available': 'available', 
                'cost_price': 'cost_price',
                'current_price': 'current_price',
                'market_value': 'market_value',
                'profit_ratio': 'profit_ratio',
                'highest_price': 'highest_price',
                'stop_loss_price': 'stop_loss_price',
                'profit_triggered': 'profit_triggered',
                'open_date': 'open_date',
                'profit_breakout_triggered': 'profit_breakout_triggered',
                'breakout_highest_price': 'breakout_highest_price',
                'last_update': 'last_update'
            }
            
            # 🔑 构建标准化的持仓字典
            position_dict = {}
            
            # 映射已有字段
            for original_field, standard_field in field_mapping.items():
                if original_field in position_row.index and position_row[original_field] is not None:
                    position_dict[standard_field] = position_row[original_field]
            
            # 🔑 确保基础字段存在，添加默认值
            if 'stock_code' not in position_dict:
                position_dict['stock_code'] = stock_code
                
            if 'stock_name' not in position_dict:
                position_dict['stock_name'] = stock_code
                
            # 🔑 计算缺失的字段
            try:
                volume = float(position_dict.get('volume', 0))
                market_value = float(position_dict.get('market_value', 0))
                cost_price = float(position_dict.get('cost_price', 0))
                
                # 计算当前价格（如果缺失）
                if 'current_price' not in position_dict and volume > 0 and market_value > 0:
                    position_dict['current_price'] = round(market_value / volume, 2)
                
                # 计算盈亏比例（如果缺失）
                if 'profit_ratio' not in position_dict and cost_price > 0:
                    current_price = float(position_dict.get('current_price', cost_price))
                    position_dict['profit_ratio'] = round(100 * (current_price - cost_price) / cost_price, 2)
                    
            except (ValueError, TypeError, ZeroDivisionError):
                pass  # 计算失败时保持原值
            
            # 🔑 为策略字段添加默认值（这些字段通常不在QMT接口中）
            strategy_defaults = {
                'highest_price': position_dict.get('current_price', position_dict.get('cost_price', 0)),
                'stop_loss_price': 0.0,
                'profit_triggered': False,
                'profit_breakout_triggered': False,
                'breakout_highest_price': 0.0,
                'open_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'last_update': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            
            for field, default_value in strategy_defaults.items():
                if field not in position_dict:
                    position_dict[field] = default_value
            
            # 🔑 数据类型安全转换
            # 数值字段
            numeric_fields = ['volume', 'available', 'cost_price', 'current_price', 'market_value', 
                            'profit_ratio', 'highest_price', 'stop_loss_price', 'breakout_highest_price']
            
            for field in numeric_fields:
                if field in position_dict and position_dict[field] is not None:
                    try:
                        position_dict[field] = float(position_dict[field])
                    except (ValueError, TypeError):
                        logger.warning(f"{stock_code} 字段 {field} 转换失败: {position_dict[field]}")
                        position_dict[field] = 0.0
            
            # 整数字段
            integer_fields = ['volume', 'available']
            for field in integer_fields:
                if field in position_dict:
                    try:
                        position_dict[field] = int(float(position_dict[field]))
                    except (ValueError, TypeError):
                        position_dict[field] = 0
            
            # 布尔字段
            boolean_fields = ['profit_triggered', 'profit_breakout_triggered']
            for field in boolean_fields:
                if field in position_dict:
                    if isinstance(position_dict[field], str):
                        position_dict[field] = position_dict[field].lower() in ['true', '1', 't', 'y', 'yes']
                    else:
                        position_dict[field] = bool(position_dict[field]) if position_dict[field] is not None else False
            
            # 🔑 数据合理性验证
            cost_price = position_dict.get('cost_price', 0)
            if cost_price > 0:
                # 验证最高价
                highest_price = position_dict.get('highest_price', 0)
                current_price = position_dict.get('current_price', cost_price)

                if highest_price <= 0 or highest_price > cost_price * 20 or highest_price < cost_price * 0.1:
                    logger.warning(f"{stock_code} 最高价数据异常: {highest_price}，修正为当前价格")
                    position_dict['highest_price'] = max(cost_price, current_price)

                # 修复：验证止损价 - 区分固定止损和动态止盈
                stop_loss_price = position_dict.get('stop_loss_price', 0)
                profit_triggered = position_dict.get('profit_triggered', False)

                if profit_triggered:
                    # 动态止盈场景：止损价应该在最高价的0.75-1.0倍之间（允许15%-25%回撤）
                    if stop_loss_price > highest_price:
                        logger.warning(f"{stock_code} 动态止盈价数据异常: {stop_loss_price} > 最高价 {highest_price}，重新计算")
                        # 重新计算止损价
                        base_cost_price = position_dict.get('base_cost_price')
                        effective_cost = cost_price if cost_price > 0 else (base_cost_price if base_cost_price and base_cost_price > 0 else 0.01)
                        recalculated_stop_loss = self.calculate_stop_loss_price(effective_cost, highest_price, profit_triggered)
                        position_dict['stop_loss_price'] = recalculated_stop_loss if recalculated_stop_loss else 0.0
                        # 更新内存数据库（P0修复: 添加锁保护）
                        with self.memory_conn_lock:
                            cursor = self.memory_conn.cursor()
                            cursor.execute("UPDATE positions SET stop_loss_price=? WHERE stock_code=?",
                                         (position_dict['stop_loss_price'], stock_code))
                            self.memory_conn.commit()
                    elif stop_loss_price == 0 or stop_loss_price < highest_price * 0.7:
                        # 止损价为0或异常小，重新计算
                        if stop_loss_price > 0:
                            logger.warning(f"{stock_code} 动态止盈价数据异常: {stop_loss_price} < 最高价*0.7 ({highest_price * 0.7:.2f})，重新计算")
                        # 重新计算止损价
                        base_cost_price = position_dict.get('base_cost_price')
                        effective_cost = cost_price if cost_price > 0 else (base_cost_price if base_cost_price and base_cost_price > 0 else 0.01)
                        recalculated_stop_loss = self.calculate_stop_loss_price(effective_cost, highest_price, profit_triggered)
                        position_dict['stop_loss_price'] = recalculated_stop_loss if recalculated_stop_loss else 0.0
                        # 更新内存数据库（P0修复: 添加锁保护）
                        with self.memory_conn_lock:
                            cursor = self.memory_conn.cursor()
                            cursor.execute("UPDATE positions SET stop_loss_price=? WHERE stock_code=?",
                                         (position_dict['stop_loss_price'], stock_code))
                            self.memory_conn.commit()
                    # else: 动态止盈价正常，不警告
                else:
                    # 固定止损场景：止损价应该在成本价的0.85-1.0倍之间（0-15%止损）
                    if stop_loss_price > cost_price:
                        logger.warning(f"{stock_code} 固定止损价数据异常: {stop_loss_price} > 成本价 {cost_price}，重新计算")
                        recalculated_stop_loss = self.calculate_stop_loss_price(cost_price, highest_price, profit_triggered)
                        position_dict['stop_loss_price'] = recalculated_stop_loss if recalculated_stop_loss else 0.0
                        # 更新内存数据库（P0修复: 添加锁保护）
                        with self.memory_conn_lock:
                            cursor = self.memory_conn.cursor()
                            cursor.execute("UPDATE positions SET stop_loss_price=? WHERE stock_code=?",
                                         (position_dict['stop_loss_price'], stock_code))
                            self.memory_conn.commit()
                    elif stop_loss_price == 0 or stop_loss_price < cost_price * 0.85:
                        if stop_loss_price > 0:
                            logger.warning(f"{stock_code} 固定止损价数据异常: {stop_loss_price} < 成本价*0.85 ({cost_price * 0.85:.2f})，重新计算")
                        recalculated_stop_loss = self.calculate_stop_loss_price(cost_price, highest_price, profit_triggered)
                        position_dict['stop_loss_price'] = recalculated_stop_loss if recalculated_stop_loss else 0.0
                        # 更新内存数据库（P0修复: 添加锁保护）
                        with self.memory_conn_lock:
                            cursor = self.memory_conn.cursor()
                            cursor.execute("UPDATE positions SET stop_loss_price=? WHERE stock_code=?",
                                         (position_dict['stop_loss_price'], stock_code))
                            self.memory_conn.commit()
            
            logger.debug(f"获取 {stock_code} 持仓成功: 数量={position_dict.get('volume', 0)}, 成本价={position_dict.get('cost_price', 0):.2f}")
            return position_dict
            
        except Exception as e:
            logger.error(f"获取 {stock_code} 的持仓信息时出错: {str(e)}")
            return None
        
    def _is_test_environment(self):
        """判断是否为测试环境"""
        # 可以根据需要修改判断逻辑
        return 'unittest' in sys.modules

    def _update_stock_positions_file(self, current_positions):
        """
        更新 stock_positions.json 文件，如果内容有变化则写入。

        参数:
        current_positions (set): 当前持仓的股票代码集合
        """
        try:
            if os.path.exists(self.stock_positions_file):
                with open(self.stock_positions_file, "r") as f:
                    try:
                        existing_positions = set(json.load(f))
                    except json.JSONDecodeError:
                        logger.warning(f"Error decoding JSON from {self.stock_positions_file}. Overwriting with current positions.")
                        existing_positions = set()
            else:
                existing_positions = set()

            if existing_positions != current_positions:
                with open(self.stock_positions_file, "w") as f:
                    json.dump(sorted(list(current_positions)), f, indent=4, ensure_ascii=False)  # Sort for consistency
                logger.info(f"更新 {self.stock_positions_file} with new positions.")

        except Exception as e:
            logger.error(f"更新出错 {self.stock_positions_file}: {str(e)}")

    def update_position(self, stock_code, volume, cost_price, current_price=None,
                   profit_ratio=None, market_value=None, available=None, open_date=None,
                   profit_triggered=None, highest_price=None, stop_loss_price=None,
                   stock_name=None,base_cost_price=None):
        """
        更新持仓信息 - 最小修改版本：仅将位置索引改为字典访问
        """
        try:
            # 确保stock_code有效
            if stock_code is None or stock_code == "":
                logger.error("股票代码不能为空")
                return False

            if stock_name is None:
                try:
                    # 使用data_manager获取股票名称
                    from data_manager import get_data_manager
                    data_manager = get_data_manager()
                    stock_name = data_manager.get_stock_name(stock_code)
                except Exception as e:
                    logger.warning(f"获取股票 {stock_code} 名称时出错: {str(e)}")
                    stock_name = stock_code  # 如果无法获取名称，使用代码代替

            # 数据预处理和验证
            p_volume = int(float(volume)) if volume is not None else 0

            if p_volume <= 0:
                # 修复: 当持仓量为0时,优先使用base_cost_price保留历史成本,避免成本价变为0
                if base_cost_price is not None and base_cost_price > 0:
                    # 优先使用base_cost_price(初次建仓成本)
                    final_cost_price = float(base_cost_price)
                    logger.debug(f"{stock_code} 持仓已清空,使用base_cost_price保留历史成本: {final_cost_price}")
                elif cost_price is not None and cost_price > 0:
                    # 其次使用QMT返回的cost_price
                    final_cost_price = float(cost_price)
                    logger.debug(f"{stock_code} 持仓已清空,使用QMT返回的cost_price: {final_cost_price}")
                else:
                    # 从数据库获取最后的有效成本价
                    try:
                        with self.memory_conn_lock:
                            db_cursor = self.memory_conn.cursor()
                            db_cursor.execute("SELECT cost_price, base_cost_price FROM positions WHERE stock_code=?", (stock_code,))
                            db_row = db_cursor.fetchone()
                        if db_row:
                            db_cost = db_row[0]
                            db_base_cost = db_row[1]
                            if db_base_cost is not None and db_base_cost > 0:
                                final_cost_price = float(db_base_cost)
                                logger.info(f"{stock_code} 持仓已清空,从数据库保留base_cost: {final_cost_price}")
                            elif db_cost is not None and db_cost > 0:
                                final_cost_price = float(db_cost)
                                logger.info(f"{stock_code} 持仓已清空,从数据库保留cost_price: {final_cost_price}")
                            else:
                                final_cost_price = 0.0
                                logger.warning(f"{stock_code} 持仓已清空且无有效成本价,设为0(建议删除此持仓记录)")
                        else:
                            final_cost_price = 0.0
                            logger.warning(f"{stock_code} 持仓已清空且数据库无记录,成本价设为0")
                    except Exception as e:
                        logger.error(f"{stock_code} 查询数据库历史成本时出错: {e}")
                        final_cost_price = 0.0
            else:
                # 有持仓时，成本价处理逻辑
                # 🔧 修复: 当QMT返回负值或无效成本价时,使用base_cost_price
                if cost_price is not None and cost_price > 0:
                    final_cost_price = float(cost_price)
                elif base_cost_price is not None and base_cost_price > 0:
                    # QMT成本价无效,使用base_cost_price
                    final_cost_price = float(base_cost_price)
                    logger.debug(f"{stock_code} QMT成本价无效({cost_price}),使用base_cost_price: {final_cost_price}")
                else:
                    # 最后兜底,设最小值0.01
                    final_cost_price = 0.01

            # 同时确保base_cost_price始终保留
            if base_cost_price is not None and base_cost_price > 0:
                p_base_cost_price = float(base_cost_price)
            else:
                # 如果base_cost_price无效,尝试使用final_cost_price
                p_base_cost_price = final_cost_price if final_cost_price > 0 else None

            final_current_price = float(current_price) if current_price is not None else final_cost_price
            final_highest_price = float(current_price) if current_price is not None else final_cost_price
            p_market_value = float(market_value) if market_value is not None else (p_volume * final_current_price)
            p_available = int(available) if available is not None else p_volume

            if final_cost_price > 0:
                p_profit_ratio = float(profit_ratio) if profit_ratio is not None else (
                    round(100 * (final_current_price - final_cost_price) / final_cost_price, 2)
                )
            else:
                # 成本价为0时，盈亏率也设为0
                p_profit_ratio = 0.0

            # profit_triggered 布尔值转换
            if isinstance(profit_triggered, str):
                p_profit_triggered = profit_triggered.lower() in ['true', '1', 't', 'y', 'yes']
            else:
                p_profit_triggered = bool(profit_triggered)

            p_profit_triggered = bool(profit_triggered) if profit_triggered is not None else False


            # 获取当前时间
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            with self.memory_conn_lock:
                cursor = self.memory_conn.cursor()

                # P0修复: 不修改全局row_factory，使用cursor.description手动构建字典
                dict_cursor = self.memory_conn.cursor()
                dict_cursor.execute("SELECT open_date, profit_triggered, highest_price, cost_price, stop_loss_price FROM positions WHERE stock_code=?", (stock_code,))
                row = dict_cursor.fetchone()

                # 手动构建字典以避免修改全局row_factory
                result_row = None
                if row:
                    columns = [desc[0] for desc in dict_cursor.description]
                    result_row = dict(zip(columns, row))

                if result_row:
                    # 更新持仓 - 【关键修改】使用字典访问替代位置索引
                    if open_date is None:
                        open_date = result_row['open_date']  # 替代 result[0]

                    # 保护profit_triggered状态 - 【关键修改】
                    existing_profit_triggered = bool(result_row['profit_triggered']) if result_row['profit_triggered'] is not None else False  # 替代 result[1]
                    final_profit_triggered = p_profit_triggered if p_profit_triggered == True else existing_profit_triggered

                    # 更新最高价 - 【关键修改】增加异常处理
                    try:
                        old_db_highest_price = float(result_row['highest_price']) if result_row['highest_price'] is not None else None  # 替代 result[2]
                    except (ValueError, TypeError):
                        logger.warning(f"{stock_code} 数据库中的最高价数据异常，重置为None")
                        old_db_highest_price = None

                    if old_db_highest_price is not None and old_db_highest_price > 0:
                        final_highest_price = max(old_db_highest_price, final_current_price)
                    else:
                        final_highest_price = max(final_cost_price, final_current_price)

                    # 【修复变量赋值逻辑】先处理传入的stop_loss_price参数
                    if stop_loss_price is not None:
                        final_stop_loss_price = round(float(stop_loss_price), 2)
                    else:
                        final_stop_loss_price = None

                    # 获取数据库中的旧成本价
                    old_db_cost_price = float(result_row['cost_price']) if result_row['cost_price'] is not None else None

                    # 如果最高价发生变化，强制重新计算止损价格
                    if old_db_highest_price != final_highest_price:
                        logger.info(f"{stock_code} 最高价变化：{old_db_highest_price} -> {final_highest_price}，重新计算止损价格")
                        calculated_slp = self.calculate_stop_loss_price(final_cost_price, final_highest_price, final_profit_triggered)
                        final_stop_loss_price = round(calculated_slp, 2) if calculated_slp is not None else None

                    # 🔑 如果成本价发生变化（补仓摊薄），也强制重新计算止损价格
                    elif old_db_cost_price is not None and abs(old_db_cost_price - final_cost_price) > 0.01:
                        logger.info(f"{stock_code} 成本价变化：{old_db_cost_price:.2f} -> {final_cost_price:.2f}，重新计算止损价格")
                        calculated_slp = self.calculate_stop_loss_price(final_cost_price, final_highest_price, final_profit_triggered)
                        final_stop_loss_price = round(calculated_slp, 2) if calculated_slp is not None else None

                    elif final_stop_loss_price is None:
                        # 如果没有传入止损价且最高价没变化，则重新计算
                        calculated_slp = self.calculate_stop_loss_price(final_cost_price, final_highest_price, final_profit_triggered)
                        final_stop_loss_price = round(calculated_slp, 2) if calculated_slp is not None else None


                    # 使用普通cursor执行更新（保持原有UPDATE语句不变）
                    cursor.execute("""
                        UPDATE positions
                        SET volume=?, cost_price=?, current_price=?, market_value=?, available=?,
                            profit_ratio=?, last_update=?, highest_price=?, stop_loss_price=?, profit_triggered=?, stock_name=?
                        WHERE stock_code=?
                    """, (int(p_volume), final_cost_price, final_current_price, p_market_value, int(p_available),
                        p_profit_ratio, now, final_highest_price, final_stop_loss_price, final_profit_triggered, stock_name, stock_code))

                    # 【关键修改】使用字典访问记录变化
                    if final_profit_triggered != existing_profit_triggered:
                        logger.info(f"更新 {stock_code} 持仓: 首次止盈触发: 从 {existing_profit_triggered} 到 {final_profit_triggered}")
                    elif abs(final_highest_price - (old_db_highest_price or 0)) > 0.01:
                        logger.info(f"更新 {stock_code} 持仓: 最高价: 从 {old_db_highest_price} 到 {final_highest_price}")
                    elif final_stop_loss_price != (float(result_row['stop_loss_price']) if result_row['stop_loss_price'] is not None else None):  # 替代 result[3]
                        logger.info(f"更新 {stock_code} 持仓: 止损价: 从 {result_row['stop_loss_price']} 到 {final_stop_loss_price}")

                else:
                    # 新增持仓（保持原有逻辑不变）
                    if open_date is None:
                        open_date = now  # 新建仓时记录当前时间为open_date
                    profit_triggered = False
                    if final_highest_price is None:
                        final_highest_price = final_current_price
                    if p_base_cost_price is None:
                        p_base_cost_price = final_cost_price

                    # 计算止损价格
                    calculated_slp = self.calculate_stop_loss_price(final_cost_price, final_highest_price, profit_triggered)
                    final_stop_loss_price = round(calculated_slp, 2) if calculated_slp is not None else None

                    if stock_name is None:
                        stock_name = stock_code

                    cursor.execute("""
                        INSERT INTO positions
                        (stock_code, stock_name, volume, cost_price, base_cost_price, current_price, market_value, available, profit_ratio, last_update, open_date, profit_triggered, highest_price, stop_loss_price)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (stock_code, stock_name, int(p_volume), final_cost_price, p_base_cost_price, final_current_price, p_market_value,
                        int(p_available), p_profit_ratio, now, open_date, profit_triggered, final_highest_price, final_stop_loss_price))

                    logger.info(f"新增 {stock_code} 持仓: 数量={p_volume}, 成本价={final_cost_price}, 最高价={final_highest_price}, 止损价={final_stop_loss_price}")

                # P0修复: commit操作（移除finally块和row_factory恢复）
                self.memory_conn.commit()

            # 强制触发版本更新（保持原有逻辑）
            self._increment_data_version()

            return True

        except Exception as e:
            logger.error(f"更新 {stock_code} 持仓Error: {str(e)}")
            try:
                with self.memory_conn_lock:
                    self.memory_conn.rollback()
            except:
                pass
            return False

    def remove_position(self, stock_code):
        """
        删除持仓记录

        参数:
        stock_code (str): 股票代码

        返回:
        bool: 是否删除成功
        """
        try:

            position = self.get_position(stock_code)
            if position:
                profit_triggered = position.get('profit_triggered', False)
                profit_ratio = position.get('profit_ratio', 0)

                if profit_triggered:
                    logger.warning(f"⚠️  删除已触发止盈的持仓 {stock_code}，盈亏率: {profit_ratio:.2f}%")
                else:
                    logger.info(f"删除持仓 {stock_code}，盈亏率: {profit_ratio:.2f}%")

            with self.memory_conn_lock:
                cursor = self.memory_conn.cursor()
                cursor.execute("DELETE FROM positions WHERE stock_code=?", (stock_code,))
                self.memory_conn.commit()

                if cursor.rowcount > 0:
                    # 触发持仓数据版本更新
                    self._increment_data_version()
                    logger.info(f"已删除 {stock_code} 的持仓记录")
                    return True
                else:
                    logger.warning(f"未找到 {stock_code} 的持仓记录，无需删除")
                    return False

        except Exception as e:
            logger.error(f"删除 {stock_code} 的持仓记录时出错: {str(e)}")
            with self.memory_conn_lock:
                self.memory_conn.rollback()
            return False


    def get_data_version_info(self):
        """获取持仓数据版本信息"""
        with self.version_lock:
            return {
                'version': self.data_version,
                'changed': self.data_changed,
                'timestamp': datetime.now().isoformat()
            }

    def mark_data_consumed(self):
        """标记持仓数据已被消费"""
        with self.version_lock:
            self.data_changed = False

    def update_all_positions_highest_price(self):
        """更新所有持仓的最高价"""
        try:
            positions = self.get_all_positions()
            if positions.empty:
                logger.debug("当前没有持仓，无需更新最高价")
                return

            now_ts = time.time()
            for _, position in positions.iterrows():
                stock_code = position['stock_code']

                # 安全获取最高价，确保不为None
                current_highest_price = 0.0
                if position['highest_price'] is not None:
                    try:
                        current_highest_price = float(position['highest_price'])
                    except (ValueError, TypeError):
                        current_highest_price = 0.0
                
                # 安全获取开仓日期
                open_date_str = position['open_date']
                try:
                    if isinstance(open_date_str, str):
                        open_date = datetime.strptime(open_date_str, '%Y-%m-%d %H:%M:%S')
                    else:
                        open_date = datetime.now()
                    
                    open_date_formatted = open_date.strftime('%Y-%m-%d')
                except (ValueError, TypeError):
                    open_date_formatted = datetime.now().strftime('%Y-%m-%d')

                # open_date_formatted 已在上方处理完成（避免解析失败导致未定义）

                # Get today's date for getStockData
                today_formatted = datetime.now().strftime('%Y-%m-%d')

                # ===== 使用缓存的历史最高价（避免频繁调用行情接口）=====
                highest_price = 0.0
                cache = self.history_high_cache.get(stock_code)
                cache_valid = (
                    cache
                    and cache.get('open_date') == open_date_formatted
                    and (now_ts - cache.get('ts', 0)) < self.history_high_cache_ttl
                )

                if cache_valid:
                    highest_price = float(cache.get('high', 0.0) or 0.0)
                else:
                    # 优先使用本地数据库缓存
                    history_df = self.data_manager.get_history_data_from_db(
                        stock_code=stock_code,
                        start_date=open_date_formatted
                    )
                    if history_df is not None and not history_df.empty and 'high' in history_df.columns:
                        try:
                            highest_price = history_df['high'].astype(float).max()
                        except Exception:
                            highest_price = 0.0

                    # 如果本地无数据，才尝试从行情接口拉取（日线）
                    if highest_price <= 0:
                        try:
                            history_data = Methods.getStockData(
                                code=stock_code,
                                fields="high",
                                start_date=open_date_formatted,
                                freq='d',  # 日线
                                adjustflag='2'
                            )
                            if history_data is not None and not history_data.empty:
                                highest_price = history_data['high'].astype(float).max()
                            else:
                                highest_price = 0.0
                                logger.warning(f"未能获取 {stock_code} 从 {open_date_formatted} 到 {today_formatted} 的历史数据，跳过更新最高价")
                        except Exception as e:
                            logger.error(f"获取 {stock_code} 从 {open_date_formatted} 到 {today_formatted} 的历史数据时出错: {str(e)}")
                            highest_price = 0.0

                    # 更新历史最高价缓存（1小时刷新一次）
                    self.history_high_cache[stock_code] = {
                        'high': highest_price,
                        'open_date': open_date_formatted,
                        'ts': now_ts
                    }

                # 开盘时间，直接从行情接口获取最新tick数据（不使用缓存）
                if config.is_trade_time():
                    latest_data = self.data_manager.get_latest_data(stock_code)
                    if latest_data:
                        current_price = latest_data.get('lastPrice')
                        current_high_price = latest_data.get('high')
                        if current_high_price and current_high_price > highest_price:
                            highest_price = current_high_price
                
                if highest_price > current_highest_price:
                    # 更新持仓"最高价"信息（P0修复: 添加锁保护）
                    with self.memory_conn_lock:
                        cursor = self.memory_conn.cursor()
                        cursor.execute("""
                            UPDATE positions
                            SET highest_price = ?, last_update = ?
                            WHERE stock_code = ?
                        """, (
                            round(highest_price, 2),
                            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            stock_code
                        ))
                        self.memory_conn.commit()
                    logger.info(f"更新 {stock_code} 的最高价为 {highest_price:.2f}")    
               
        except Exception as e:
            logger.error(f"更新所有持仓的最高价时出错: {str(e)}")

    def update_all_positions_price(self):
        """更新所有持仓的最新价格"""
        try:
            # 首先检查是否有持仓数据
            positions = self.get_all_positions()
            
            # 检查positions是否为None或空DataFrame
            if positions is None or positions.empty:
                logger.debug("当前没有持仓，无需更新价格")
                return
            
            # 检查positions是否含有必要的列
            required_columns = ['stock_code', 'volume', 'cost_price', 'current_price', 'highest_price']
            missing_columns = [col for col in required_columns if col not in positions.columns]
            if missing_columns:
                logger.warning(f"持仓数据缺少必要列: {missing_columns}，无法更新价格")
                return
            
            for _, position in positions.iterrows():
                try:
                    # 提取数据并安全转换
                    stock_code = position['stock_code']
                    if stock_code is None:
                        continue  # 跳过无效数据
                    
                    # 安全提取和转换所有数值
                    safe_numeric_values = {}
                    for field in ['volume', 'cost_price', 'current_price', 'highest_price', 'profit_triggered', 'available', 'market_value', 'stop_loss_price']:
                        if field in position:
                            value = position[field]
                            # 布尔值特殊处理
                            if field == 'profit_triggered':
                                safe_numeric_values[field] = bool(value) if value is not None else False
                            # 数值处理
                            elif field in ['volume', 'available']:
                                safe_numeric_values[field] = int(float(value)) if value is not None else 0
                            else:
                                safe_numeric_values[field] = float(value) if value is not None else 0.0
                        else:
                            # 设置默认值
                            if field == 'profit_triggered':
                                safe_numeric_values[field] = False
                            elif field in ['volume', 'available']:
                                safe_numeric_values[field] = 0
                            else:
                                safe_numeric_values[field] = 0.0
                    
                    # 安全处理open_date
                    open_date = position.get('open_date')
                    if open_date is None:
                        open_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    
                    # 获取最新价格
                    try:
                        latest_quote = self.data_manager.get_latest_data(stock_code)
                        if latest_quote and isinstance(latest_quote, dict) and 'lastPrice' in latest_quote and latest_quote['lastPrice'] is not None:
                            current_price = float(latest_quote['lastPrice'])

                            # 只有价格有显著变化时才更新
                            old_price = safe_numeric_values['current_price']
                            if abs(current_price - old_price) / max(old_price, 0.01) > 0.003:  # 防止除零
                                # 使用安全转换后的值来更新
                                self.update_position(
                                    stock_code=stock_code,
                                    volume=safe_numeric_values['volume'],
                                    cost_price=safe_numeric_values['cost_price'],
                                    available=safe_numeric_values['available'],
                                    market_value=safe_numeric_values['market_value'],
                                    current_price=current_price,  # 使用最新价格
                                    profit_triggered=safe_numeric_values['profit_triggered'],
                                    highest_price=safe_numeric_values['highest_price'],
                                    open_date=open_date,
                                    stop_loss_price=safe_numeric_values['stop_loss_price']
                                )
                                logger.debug(f"更新 {stock_code} 的最新价格为 {current_price:.2f}")

                    except Exception as e:
                        logger.error(f"获取 {stock_code} 最新价格时出错: {str(e)}")
                        continue  # 跳过这只股票，继续处理其他股票
                        
                except Exception as e:
                    logger.error(f"处理 {position.get('stock_code', 'unknown')} 持仓数据时出错: {str(e)}")
                    continue  # 跳过这只股票，继续处理其他股票
            
        except Exception as e:
            logger.error(f"更新所有持仓价格时出错: {str(e)}")

    def get_account_info(self):
        """获取账户信息"""
        try:

            # 如果是模拟交易模式，直接返回模拟账户信息（由trading_executor模块管理）
            if hasattr(config, 'ENABLE_SIMULATION_MODE') and config.ENABLE_SIMULATION_MODE:
                logger.debug(f"返回模拟账户信息，余额: {config.SIMULATION_BALANCE}")
                # 计算持仓市值
                positions = self.get_all_positions()
                market_value = 0
                if not positions.empty:
                    for _, pos in positions.iterrows():
                        pos_market_value = pos.get('market_value')
                        if pos_market_value is not None:
                            try:
                                market_value += float(pos_market_value)
                            except (ValueError, TypeError):
                                # 忽略无效值
                                pass
                
                # 计算总资产
                available = float(config.SIMULATION_BALANCE)
                total_asset = available + market_value  # 可用资金 + 持仓市值
                
                return {
                    'account_id': 'SIMULATION',
                    'account_type': 'SIMULATION',
                    'available': available,
                    'market_value': float(market_value),
                    'total_asset': total_asset,  # 添加总资产字段
                    'profit_loss': 0.0,
                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }

            # 使用qmt_trader获取账户信息
            account_df = self.qmt_trader.balance()

            # ===== 新增：None检查和类型检查 =====
            if account_df is None or not isinstance(account_df, pd.DataFrame) or account_df.empty:
                return None
            
            # 转换为字典格式
            account_info = {
                'account_id': account_df['资金账户'].iloc[0] if '资金账户' in account_df.columns and not account_df['资金账户'].empty else '--',
                'account_type': account_df['账号类型'].iloc[0] if '账号类型' in account_df.columns and not account_df['账号类型'].empty else '--',
                'available': float(account_df['可用金额'].iloc[0]) if '可用金额' in account_df.columns and not account_df['可用金额'].empty else 0.0,
                'frozen_cash': float(account_df['冻结金额'].iloc[0]) if '冻结金额' in account_df.columns and not account_df['冻结金额'].empty else 0.0,
                'market_value': float(account_df['持仓市值'].iloc[0]) if '持仓市值' in account_df.columns and not account_df['持仓市值'].empty else 0.0,
                'total_asset': float(account_df['总资产'].iloc[0]) if '总资产' in account_df.columns and not account_df['总资产'].empty else 0.0,
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            return account_info
        except Exception as e:
            logger.error(f"获取账户信息时出错: {str(e)}")
            return None
    
    # ===== 旧的网格交易方法已废弃，请使用GridTradingManager =====
    # get_grid_trades(), add_grid_trade(), update_grid_trade_status(), check_grid_trade_signals()
    # 已被grid_trading_manager.py中的GridTradingManager替代


    def calculate_stop_loss_price(self, cost_price, highest_price, profit_triggered):
        """
        计算止损价格 - 统一的止损价格计算逻辑
        
        注意：当profit_triggered=True时，实际计算的是动态止盈价格，
        这个价格在首次止盈后作为新的"止损位"来保护已获得的收益
        
        参数:
        cost_price (float): 成本价
        highest_price (float): 历史最高价
        profit_triggered (bool): 是否已经触发首次止盈
        
        返回:
        float: 止损价格
        """
        try:
            # 确保输入都是有效的数值
            if cost_price is None or cost_price <= 0:
                logger.warning(f"成本价无效: {cost_price}, 使用最小止损价")
                return 0.0  # 如果成本价无效，返回0作为止损价
                
            if highest_price is None or highest_price <= 0:
                highest_price = cost_price  # 如果最高价无效，使用成本价
            
            # 确保profit_triggered是布尔值
            if isinstance(profit_triggered, str):
                profit_triggered = profit_triggered.lower() in ['true', '1', 't', 'y', 'yes']
            else:
                profit_triggered = bool(profit_triggered)
            
            if profit_triggered:
                # 检查配置有效性
                if not config.DYNAMIC_TAKE_PROFIT:
                    logger.warning("动态止盈配置为空，使用保守止盈位")
                    return highest_price * 0.95  # 保守的5%回撤止盈

                # 动态止损：基于最高价和分级止损
                if cost_price > 0:  # 防止除零
                    highest_profit_ratio = (highest_price - cost_price) / cost_price
                else:
                    highest_profit_ratio = 0.0
                    
                # 修正：从高到低遍历，找到最高匹配区间
                take_profit_coefficient = 1.0  # 默认值改为1.0，表示不进行动态止损
                matched_level = None
                
                for profit_level, coefficient in sorted(config.DYNAMIC_TAKE_PROFIT, reverse=True):
                    if highest_profit_ratio >= profit_level:
                        take_profit_coefficient = coefficient
                        matched_level = profit_level
                        break  # 找到最高匹配级别后停止
                
                # 计算动态止损价
                dynamic_stop_loss_price = highest_price * take_profit_coefficient
                
                # 添加调试日志
                if matched_level is not None:
                    logger.debug(f"动态止损计算：成本价={cost_price:.2f}, 最高价={highest_price:.2f}, "
                            f"最高盈利={highest_profit_ratio:.1%}, 匹配区间={matched_level:.1%}, "
                            f"系数={take_profit_coefficient}, 止损价={dynamic_stop_loss_price:.2f}")
                else:
                    logger.debug(f"动态止损计算：未达到任何盈利区间，使用最高价作为止损价")
                
                return dynamic_stop_loss_price
            else:
                # 固定止损：基于成本价
                stop_loss_ratio = getattr(config, 'STOP_LOSS_RATIO', -0.07)  # 默认-7%
                return cost_price * (1 + stop_loss_ratio)
        except Exception as e:
            logger.error(f"计算止损价格时出错: {str(e)}")
            return 0.0  # 出错时返回0作为止损价


    def check_add_position_signal(self, stock_code):
        """
        检查补仓信号 - 使用web页面现有参数
        启用开关：stopLossBuyEnabled
        补仓阈值：stopLossBuy（通过BUY_GRID_LEVELS[1]获取）

        修复说明:
        - 增加止损排除逻辑: 如果亏损达到止损阈值,不执行补仓
        - 确保补仓阈值小于止损阈值,避免冲突

        参数:
        stock_code (str): 股票代码

        返回:
        tuple: (信号类型, 详细信息) - ('add_position', {...}) 或 (None, None)
        """
        try:
            # 检查补仓功能是否启用（使用web页面的stopLossBuyEnabled）
            stop_loss_buy_enabled = getattr(config, 'ENABLE_STOP_LOSS_BUY', True)
            if not stop_loss_buy_enabled:
                logger.debug(f"{stock_code} 补仓功能已关闭")
                return None, None

            # 获取持仓数据
            position = self.get_position(stock_code)
            if not position:
                logger.debug(f"未持有 {stock_code}，无需检查补仓信号")
                return None, None

            # 获取最新行情数据
            latest_quote = self.data_manager.get_latest_data(stock_code)
            if not latest_quote:
                latest_quote = {'lastPrice': position.get('current_price', 0)}

            # 数据验证和转换
            try:
                current_price = float(latest_quote.get('lastPrice', 0)) if latest_quote else 0
                if current_price <= 0:
                    current_price = float(position.get('current_price', 0))

                cost_price = float(position.get('cost_price', 0))
                current_value = float(position.get('market_value', 0))
                profit_triggered = bool(position.get('profit_triggered', False))

                if cost_price <= 0 or current_price <= 0:
                    logger.debug(f"{stock_code} 价格数据无效")
                    return None, None

            except (TypeError, ValueError) as e:
                logger.error(f"补仓信号检查 - 价格数据转换错误 {stock_code}: {e}")
                return None, None

            # 如果已触发过首次止盈，不再补仓（保护已获得的收益）
            if profit_triggered:
                logger.debug(f"{stock_code} 已触发首次止盈，不再执行补仓策略")
                return None, None

            # 计算价格下跌比例
            price_drop_ratio = (cost_price - current_price) / cost_price

            # ========== 🔑 动态优先级判断 - 根据配置参数自动调整执行顺序 ==========
            # 获取动态优先级信息
            priority_info = config.determine_stop_loss_add_position_priority()
            add_position_threshold = priority_info['add_position_threshold']
            stop_loss_threshold = priority_info['stop_loss_threshold']
            priority_mode = priority_info['priority']
            scenario = priority_info['scenario']

            # 场景A: 补仓阈值 < 止损阈值 (例如补仓5% < 止损7%)
            # 执行逻辑: 先补仓,达到仓位上限后再止损
            if priority_mode == 'add_position_first':
                # 补仓条件: 补仓阈值 <= 下跌幅度 < 止损阈值
                if price_drop_ratio >= add_position_threshold and price_drop_ratio < stop_loss_threshold:
                    # 检查是否还有补仓空间
                    remaining_space = config.MAX_POSITION_VALUE - current_value
                    min_add_amount = 1000  # 最小补仓金额

                    if remaining_space >= min_add_amount:
                        # 还有补仓空间，执行补仓
                        # 补仓金额固定使用POSITION_UNIT，不使用BUY_AMOUNT_RATIO比例
                        # 这是补仓策略(止盈止损策略)与网格交易策略的核心区别
                        add_amount = min(config.POSITION_UNIT, remaining_space)

                        logger.info(f"✅ 【场景{scenario}】{stock_code} 触发补仓条件：成本价={cost_price:.2f}, 当前价={current_price:.2f}, "
                                f"下跌={price_drop_ratio:.2%}, 补仓阈值={add_position_threshold:.2%}, "
                                f"止损阈值={stop_loss_threshold:.2%}, 补仓金额={add_amount:.0f}")

                        return 'add_position', {
                            'stock_code': stock_code,
                            'current_price': current_price,
                            'cost_price': cost_price,
                            'add_amount': add_amount,
                            'price_drop_ratio': price_drop_ratio,
                            'threshold': add_position_threshold,
                            'current_value': current_value,
                            'remaining_space': remaining_space,
                            'reason': 'price_drop_add_position',
                            'scenario': scenario
                        }
                    else:
                        # 无补仓空间且已达到补仓条件，让止损逻辑处理
                        logger.warning(f"⚠️  【场景{scenario}】{stock_code} 达到补仓条件但已达仓位上限：下跌={price_drop_ratio:.2%}, "
                                    f"剩余空间={remaining_space:.0f}, 将由止损逻辑处理")

            # 场景B: 止损阈值 <= 补仓阈值 (例如止损5% <= 补仓7%)
            # 执行逻辑: 止损优先,永不补仓
            elif priority_mode == 'stop_loss_first':
                # 任何下跌幅度只要达到止损阈值,立即拒绝补仓
                if price_drop_ratio >= stop_loss_threshold:
                    logger.warning(f"⚠️  【场景{scenario}】{stock_code} 亏损已达止损线: 下跌{price_drop_ratio:.2%} >= 止损阈值{stop_loss_threshold:.2%}, "
                                 f"拒绝补仓，由止损逻辑处理")
                    return None, None

                # 即使下跌未达止损阈值,也要检查是否达到补仓阈值
                # 但由于补仓阈值 >= 止损阈值,一旦达到补仓条件就意味着已达止损条件
                # 因此这个分支永远不会触发补仓
                if price_drop_ratio >= add_position_threshold:
                    logger.warning(f"⚠️  【场景{scenario}】{stock_code} 下跌{price_drop_ratio:.2%}达到补仓阈值{add_position_threshold:.2%}, "
                                 f"但止损优先策略拒绝补仓")
                    return None, None

            # 兜底: 未达到任何条件
            return None, None

        except Exception as e:
            logger.error(f"检查 {stock_code} 补仓信号时出错: {str(e)}")
            return None, None

    # ========== 新增：统一的止盈止损检查逻辑 ==========
    
    def check_trading_signals(self, stock_code, current_price=None):
        """
        检查交易信号 - 修复字段映射错乱版本

        参数:
        stock_code (str): 股票代码
        current_price (float, optional): 当前价格,如果提供则跳过行情查询以避免重复调用

        返回:
        tuple: (信号类型, 详细信息) - ('stop_loss'/'take_profit_half'/'take_profit_full', {...}) 或 (None, None)
        """
        try:
            # 1. 获取持仓数据
            position = self.get_position(stock_code)
            if not position:
                logger.debug(f"未持有 {stock_code}，无需检查信号")
                return None, None

            # ⭐ 优化: 持仓已清空，跳过信号检测
            volume = int(position.get('volume', 0))
            available = int(position.get('available', 0))
            if volume == 0 and available == 0:
                logger.debug(f"{stock_code} 持仓已清空(volume=0, available=0)，跳过信号检测")
                return None, None

            # 2. 获取最新行情数据 (优化: 如果已提供current_price则跳过API调用)
            if current_price is None:
                latest_quote = self.data_manager.get_latest_data(stock_code)
                if not latest_quote:
                    latest_quote = {'lastPrice': position.get('current_price', 0)}
            else:
                latest_quote = {'lastPrice': current_price}

            # 3. 🔑 安全的数据类型转换和验证
            try:
                current_price = float(latest_quote.get('lastPrice', 0)) if latest_quote else 0
                if current_price <= 0:
                    current_price = float(position.get('current_price', 0))

                cost_price = float(position.get('cost_price', 0))
                profit_triggered = bool(position.get('profit_triggered', False))
                highest_price = float(position.get('highest_price', 0))
                stop_loss_price = float(position.get('stop_loss_price', 0))

                # 🔑 基础数据验证
                if cost_price <= 0:
                    logger.error(f"{stock_code} 成本价无效: {cost_price}")
                    return None, None

                if current_price <= 0:
                    logger.warning(f"{stock_code} 当前价格无效: {current_price}，使用成本价")
                    current_price = cost_price
                    
                # 🔑 关键验证：检查数据是否存在字段错乱
                if highest_price <= 0:
                    logger.warning(f"{stock_code} 最高价无效: {highest_price}，使用当前价格")
                    highest_price = max(cost_price, current_price)
                elif highest_price > cost_price * 20:  # 最高价超过成本价20倍，明显异常
                    logger.error(f"{stock_code} 最高价数据异常: {highest_price} > {cost_price} * 20，可能存在字段错乱")
                    highest_price = max(cost_price, current_price)
                elif highest_price < cost_price * 0.1:  # 最高价低于成本价10%，明显异常
                    logger.error(f"{stock_code} 最高价数据异常: {highest_price} < {cost_price} * 0.1，可能存在字段错乱")
                    highest_price = max(cost_price, current_price)
                    
            except (TypeError, ValueError) as e:
                logger.error(f"{stock_code} 价格数据类型转换错误: {e}")
                return None, None

            # 4. 优先检查止损条件（最高优先级）
            if not profit_triggered:
                # 🔑 使用安全计算的固定止损价格
                try:
                    stop_loss_ratio = getattr(config, 'STOP_LOSS_RATIO', -0.07)
                    safe_stop_loss_price = cost_price * (1 + stop_loss_ratio)
                    
                    # 如果数据库中的止损价格异常，使用安全计算的值
                    if stop_loss_price <= 0 or stop_loss_price > cost_price * 1.5 or stop_loss_price < cost_price * 0.5:
                        logger.warning(f"{stock_code} 数据库止损价异常: {stop_loss_price}，使用安全计算值: {safe_stop_loss_price:.2f}")
                        stop_loss_price = safe_stop_loss_price
                    
                    if current_price <= stop_loss_price:
                        # 🔑 最后验证：确保这是合理的止损
                        loss_ratio = (cost_price - current_price) / cost_price
                        expected_loss_ratio = abs(stop_loss_ratio)
                        
                        # 允许一定的误差范围
                        if loss_ratio >= expected_loss_ratio * 0.5:  # 至少达到预期止损的50%
                            logger.warning(f"{stock_code} 触发固定止损，当前价格: {current_price:.2f}, 止损价格: {stop_loss_price:.2f}")
                            return 'stop_loss', {
                                'current_price': current_price,
                                'stop_loss_price': stop_loss_price,
                                'cost_price': cost_price,
                                'volume': position['available'],
                                'reason': 'validated_stop_loss'
                            }
                        else:
                            logger.warning(f"🚨 {stock_code} 止损信号异常，亏损比例不符合预期: 实际{loss_ratio:.2%} vs 预期{expected_loss_ratio:.2%}")
                            return None, None
                            
                except Exception as stop_calc_error:
                    logger.error(f"{stock_code} 止损计算出错: {stop_calc_error}")
                    return None, None
            
            # 5. 检查止盈逻辑（如果启用动态止盈功能）
            if not config.ENABLE_DYNAMIC_STOP_PROFIT:
                return None, None
            
            # 计算利润率
            profit_ratio = (current_price - cost_price) / cost_price
            
            # 6. 首次止盈检查（增加回撤条件）
            if not profit_triggered:
                # 检查是否已突破初始止盈阈值
                profit_breakout_triggered_raw = position.get('profit_breakout_triggered', False)
                profit_breakout_triggered = bool(profit_breakout_triggered_raw) if profit_breakout_triggered_raw not in [None, '', 'False', '0', 0] else False
                breakout_highest_price = float(position.get('breakout_highest_price', 0) or 0)
                
                if not profit_breakout_triggered:
                    # 首次突破5%盈利阈值
                    if profit_ratio >= config.INITIAL_TAKE_PROFIT_RATIO:
                        logger.info(f"{stock_code} 首次突破止盈阈值 {config.INITIAL_TAKE_PROFIT_RATIO:.2%}，"
                                f"当前盈利: {profit_ratio:.2%}，开始监控回撤")
                        
                        # 标记突破状态并记录当前价格作为突破后最高价
                        self._mark_profit_breakout(stock_code, current_price)
                        return None, None  # 不立即执行交易，继续监控
                else:
                    # 已突破阈值，监控回撤条件
                    # 更新突破后最高价
                    if current_price > breakout_highest_price:
                        breakout_highest_price = current_price
                        self._update_breakout_highest_price(stock_code, current_price)
                        logger.debug(f"{stock_code} 更新突破后最高价: {current_price:.2f}")
                    
                    # 检查回撤条件
                    if breakout_highest_price > 0:
                        pullback_ratio = (breakout_highest_price - current_price) / breakout_highest_price
                        
                        if pullback_ratio >= config.INITIAL_TAKE_PROFIT_PULLBACK_RATIO:
                            logger.info(f"{stock_code} 触发回撤止盈，突破后最高价: {breakout_highest_price:.2f}, "
                                    f"当前价格: {current_price:.2f}, 回撤: {pullback_ratio:.2%}")

                            signal_info = {
                                'current_price': current_price,
                                'cost_price': cost_price,
                                'profit_ratio': profit_ratio,
                                'volume': position['available'],
                                'sell_ratio': config.INITIAL_TAKE_PROFIT_RATIO_PERCENTAGE,
                                'breakout_highest_price': breakout_highest_price,
                                'pullback_ratio': pullback_ratio
                            }
                            # 🔍 调试日志：确认返回信号
                            logger.info(f"[SIGNAL_RETURN] {stock_code} 准备返回take_profit_half信号, "
                                       f"available={position['available']}, volume={position.get('volume', 0)}")
                            return 'take_profit_half', signal_info
            
            # 7. 动态止盈检查（已触发首次止盈后）
            if profit_triggered and highest_price > 0:
                # 🔑 使用安全计算的动态止盈价格
                try:
                    dynamic_take_profit_price = self.calculate_stop_loss_price(
                        cost_price, highest_price, profit_triggered
                    )
                    
                    # 验证动态止盈价格的合理性
                    if dynamic_take_profit_price <= 0 or dynamic_take_profit_price > highest_price * 1.1:
                        logger.error(f"{stock_code} 动态止盈价格异常: {dynamic_take_profit_price}，跳过检查")
                        return None, None
                    
                    # 如果当前价格跌破动态止盈位，触发止盈
                    if current_price <= dynamic_take_profit_price:
                        # 获取匹配的级别信息（用于日志）
                        matched_level, take_profit_coefficient = self._get_profit_level_info(
                            cost_price, highest_price
                        )
                        
                        logger.info(f"{stock_code} 触发动态全仓止盈，当前价格: {current_price:.2f}, "
                                f"止盈位: {dynamic_take_profit_price:.2f}, 最高价: {highest_price:.2f}, "
                                f"最高达到区间: {matched_level:.1%}（系数{take_profit_coefficient})")
                                
                        return 'take_profit_full', {
                            'current_price': current_price,
                            'dynamic_take_profit_price': dynamic_take_profit_price,
                            'highest_price': highest_price,
                            'matched_level': matched_level,
                            'volume': position['available'],
                            'cost_price': cost_price
                        }
                        
                except Exception as dynamic_calc_error:
                    logger.error(f"{stock_code} 动态止盈计算出错: {dynamic_calc_error}")
                    return None, None
            
            return None, None
            
        except Exception as e:
            logger.error(f"检查 {stock_code} 的交易信号时出错: {str(e)}")
            return None, None


    def validate_trading_signal(self, stock_code, signal_type, signal_info):
        """
        交易信号最后验证 - 防止异常信号执行

        参数:
        stock_code (str): 股票代码
        signal_type (str): 信号类型
        signal_info (dict): 信号详细信息

        返回:
        bool: 是否通过验证
        """
        try:
            # 关键修改: 全仓止盈信号跳过活跃委托单检查
            # 全仓止盈是风险兜底机制,即使有活跃委托单也应该执行
            if signal_type != 'take_profit_full':
                # 检查是否有未成交委托单 (仅非全仓止盈信号)
                position = self.get_position(stock_code)
                if position:
                    available = int(position.get('available', 0))
                    volume = int(position.get('volume', 0))

                    # 如果available=0但volume>0，可能有未成交委托单
                    if available == 0 and volume > 0:
                        logger.warning(f"警告 {stock_code} 可用数量为0（总持仓{volume}），检查是否有未成交委托单...")

                        # 修复后的查询机制：使用标准化股票代码匹配
                        if self._has_pending_orders(stock_code):
                            logger.error(f"错误 {stock_code} 存在未成交委托单，拒绝新信号执行")
                            logger.error(f"   建议：等待委托单处理完毕或手动确认持仓状态")
                            return False
                        else:
                            logger.warning(f"警告 {stock_code} 未检测到活跃委托单，但available=0")
                            logger.warning(f"   可能原因: 1)委托单刚成交 2)系统数据未同步 3)其他原因")
                            # 采取保守策略：available=0时拒绝新信号，避免重复提交委托
                            logger.error(f"错误 {stock_code} 可用数量为0（总持仓{volume}），拒绝新信号执行")
                            logger.error(f"   原因：可能存在未成交委托单或数据同步延迟")
                            logger.error(f"   建议：等待委托单处理完毕或手动确认持仓状态")
                            logger.error(f"   修复说明：此为保守策略，避免在不确定情况下执行交易")
                            return False
            else:
                # 全仓止盈信号: 强制执行,不检查活跃委托单
                logger.warning(f"全仓止盈信号 {stock_code}: 跳过活跃委托单检查(风险兜底机制)")

            if signal_type == 'stop_loss':
                current_price = signal_info.get('current_price', 0)
                stop_loss_price = signal_info.get('stop_loss_price', 0)
                cost_price = signal_info.get('cost_price', 0)

                # 🔑 基础数据验证
                if current_price <= 0 or cost_price <= 0 or stop_loss_price <= 0:
                    logger.error(f"🚨 {stock_code} 止损信号数据包含无效值，拒绝执行")
                    logger.error(f"   current_price={current_price}, cost_price={cost_price}, stop_loss_price={stop_loss_price}")
                    return False

                # 🔑 价格比例检查 - 防止字段错乱导致的异常
                stop_ratio = stop_loss_price / cost_price
                if stop_ratio > 1.5 or stop_ratio < 0.5:
                    logger.error(f"🚨 {stock_code} 止损价比例异常 {stop_ratio:.3f}，疑似字段错乱，拒绝执行")
                    return False

                # 🔑 亏损比例检查
                loss_ratio = (cost_price - current_price) / cost_price
                if loss_ratio < 0.02:  # 亏损小于2%
                    logger.error(f"🚨 {stock_code} 亏损比例过小 {loss_ratio:.2%}，可能是误触发，拒绝执行")
                    return False

                # 🔑 异常值检查
                if current_price > cost_price * 10 or stop_loss_price > cost_price * 10:
                    logger.error(f"🚨 {stock_code} 价格数据异常，疑似单位错误，拒绝执行")
                    logger.error(f"   current_price={current_price}, stop_loss_price={stop_loss_price}, cost_price={cost_price}")
                    return False

                logger.info(f"✅ {stock_code} 止损信号验证通过: 亏损{loss_ratio:.2%}, 止损比例{stop_ratio:.3f}")

            elif signal_type in ['take_profit_half', 'take_profit_full']:
                current_price = signal_info.get('current_price', 0)
                signal_cost_price = signal_info.get('cost_price', 0)

                if current_price <= 0 or signal_cost_price <= 0:
                    logger.error(f"🚨 {stock_code} 止盈信号数据无效，拒绝执行")
                    return False

                # ⭐ 修复: 验证时重新获取实时成本价,避免使用历史base_cost
                position = self.get_position(stock_code)
                if position:
                    real_time_cost_price = float(position.get('cost_price', 0))
                    if real_time_cost_price > 0:
                        # 使用实时成本价进行验证
                        cost_price = real_time_cost_price
                        logger.debug(f"{stock_code} 使用实时成本价验证: {cost_price:.2f} (信号中成本价: {signal_cost_price:.2f})")
                    else:
                        # 如果实时成本价无效,使用信号中的成本价
                        cost_price = signal_cost_price
                        logger.warning(f"{stock_code} 实时成本价无效,使用信号成本价: {cost_price:.2f}")
                else:
                    cost_price = signal_cost_price
                    logger.warning(f"{stock_code} 未找到持仓,使用信号成本价: {cost_price:.2f}")

                # 确保是盈利状态
                profit_ratio = (current_price - cost_price) / cost_price if cost_price > 0 else 0
                if current_price <= cost_price:
                    logger.error(f"🚨 {stock_code} 止盈信号但当前亏损 {profit_ratio:.2%}，拒绝执行")
                    logger.error(f"   成本价: {cost_price:.2f}, 当前价: {current_price:.2f}")
                    return False

                logger.info(f"✅ {stock_code} 止盈信号验证通过，盈利 {profit_ratio:.2%}")

            return True

        except Exception as e:
            logger.error(f"🚨 {stock_code} 信号验证失败: {e}")
            return False

    def _get_real_order_id(self, returned_id):
        """
        将buy/sell返回的ID转换为真实order_id

        说明:
        - 同步模式(USE_SYNC_ORDER_API=True): buy/sell直接返回order_id
        - 异步模式(USE_SYNC_ORDER_API=False): buy/sell返回seq号，需要通过回调建立的映射获取order_id

        参数:
            returned_id: buy/sell方法返回的ID (可能是seq或order_id)

        返回:
            真实的order_id，如果映射失败返回None
        """
        if config.USE_SYNC_ORDER_API:
            # 同步模式直接返回order_id
            logger.debug(f"同步模式，直接使用order_id: {returned_id}")
            return returned_id
        else:
            # 异步模式需要从映射表获取
            import time
            logger.debug(f"异步模式，查找seq={returned_id}的映射")

            # 等待最多2秒让回调建立映射
            for i in range(20):
                if returned_id in self.qmt_trader.order_id_map:
                    real_order_id = self.qmt_trader.order_id_map[returned_id]
                    logger.debug(f"映射成功: seq={returned_id} -> order_id={real_order_id}")
                    return real_order_id
                time.sleep(0.1)

            logger.warning(f"seq={returned_id}未在order_id_map中找到映射，等待超时")
            logger.debug(f"当前order_id_map内容: {self.qmt_trader.order_id_map}")
            return None

    def _has_pending_orders(self, stock_code):
        """
        检查股票是否有未成交的委托单

        优化说明:
        - 主要方法: 使用 easy_qmt_trader.get_active_orders_by_stock() 直接查询活跃委托
        - 后备方法: 如果主要方法失败,使用原始 query_stock_orders() 查询
        - 优势: 更简洁、更准确、代码复用性更好

        参数:
        stock_code (str): 股票代码(可能带.SZ/.SH后缀)

        返回:
        bool: 是否有未成交委托单
        """
        logger.debug(f"查询 {stock_code} 的活跃委托单")

        try:
            # 在实盘模式下查询委托单
            if not config.ENABLE_SIMULATION_MODE and self.qmt_trader:
                logger.debug(f"实盘模式, QMT已连接: {self.qmt_connected}")

                try:
                    # 主要方法: 使用新增的活跃委托查询方法
                    active_orders = self.qmt_trader.get_active_orders_by_stock(stock_code)

                    logger.debug(f"主要查询方法: 查询到 {len(active_orders)} 个活跃委托")

                    if active_orders:
                        # 找到活跃委托
                        for order in active_orders:
                            logger.info(f"[OK] 发现活跃委托单: {stock_code}, "
                                      f"订单号={order.order_id}, 状态={order.order_status}, "
                                      f"委托量={order.order_volume}, 已成交={order.traded_volume}")
                        return True
                    else:
                        logger.debug(f"未找到 {stock_code} 的活跃委托单")
                        return False

                except AttributeError as ae:
                    # 如果 get_active_orders_by_stock 方法不存在,使用后备方法
                    logger.warning(f"主要查询方法不可用: {str(ae)}, 切换到后备查询方法")

                    # 后备方法: 使用原始 query_stock_orders 查询
                    return self._has_pending_orders_fallback(stock_code)

                except Exception as e:
                    logger.warning(f"主要查询方法失败: {str(e)}, 尝试后备查询方法")
                    logger.exception(e)

                    # 尝试后备方法
                    try:
                        return self._has_pending_orders_fallback(stock_code)
                    except Exception as fallback_error:
                        logger.error(f"后备查询方法也失败: {str(fallback_error)}")
                        # 查询完全失败时保守返回True,避免在不确定情况下执行交易
                        logger.error(f"[X] {stock_code} 委托查询异常，采取保守策略拒绝新信号")
                        return True
            else:
                logger.debug(f"跳过查询: 模拟模式={config.ENABLE_SIMULATION_MODE}, QMT连接={self.qmt_trader is not None}")
                return False

        except Exception as e:
            logger.error(f"_has_pending_orders 异常: {str(e)}")
            logger.exception(e)
            # 保守策略
            return True

    def _has_pending_orders_fallback(self, stock_code):
        """
        后备方法: 使用原始 query_stock_orders 查询活跃委托

        此方法作为 get_active_orders_by_stock() 的后备方案,
        在主要方法不可用或失败时使用

        参数:
        stock_code (str): 股票代码

        返回:
        bool: 是否有未成交委托单
        """
        # 标准化股票代码(去除市场后缀)
        stock_code_base = stock_code.split('.')[0]

        logger.debug(f"[后备方法] 查询 {stock_code} (标准化: {stock_code_base}) 的委托单")

        try:
            # 查询活跃委托单（未成交和部分成交）
            orders = self.qmt_trader.xt_trader.query_stock_orders(self.qmt_trader.acc, cancelable_only=False)

            logger.debug(f"[后备方法] 查询到 {len(orders) if orders else 0} 条委托单")

            if orders:
                for order in orders:
                    # 标准化订单中的股票代码
                    order_code_base = order.stock_code.split('.')[0] if '.' in order.stock_code else order.stock_code

                    logger.debug(f"  订单: {order.stock_code} (标准化: {order_code_base}), "
                               f"状态={order.order_status}, 委托量={order.order_volume}, 已成交={order.traded_volume}")

                    # 使用标准化后的代码进行比对
                    if order_code_base == stock_code_base:
                        # 扩展活跃委托状态码范围
                        # 48=未报, 49=待报, 50=已报, 51=已报待撤, 52=部分待撤, 55=部成
                        if order.order_status in [48, 49, 50, 51, 52, 55]:
                            logger.info(f"[后备方法][OK] 发现未成交委托单: {stock_code}, "
                                      f"订单代码={order.stock_code}, 状态={order.order_status}, "
                                      f"委托量={order.order_volume}, 已成交={order.traded_volume}")
                            return True

            logger.debug(f"[后备方法] 未找到 {stock_code} 的活跃委托单")
            return False

        except Exception as e:
            logger.error(f"[后备方法] 查询失败: {str(e)}")
            raise  # 抛出异常让上层处理

    def _get_profit_level_info(self, cost_price, highest_price):
        """获取当前匹配的止盈级别信息"""
        try:
            if cost_price <= 0 or highest_price <= 0:
                return 0.0, 1.0
                
            highest_profit_ratio = (highest_price - cost_price) / cost_price
            
            # 找到匹配的级别
            for profit_level, coefficient in sorted(config.DYNAMIC_TAKE_PROFIT, reverse=True):
                if highest_profit_ratio >= profit_level:
                    return profit_level, coefficient
                    
            return 0.0, 1.0  # 未匹配任何级别
            
        except Exception as e:
            logger.error(f"获取止盈级别信息时出错: {str(e)}")
            return 0.0, 1.0


    # ========== 新增：模拟交易持仓调整功能 ==========
    def simulate_buy_position(self, stock_code, buy_volume, buy_price, strategy='simu'):
        """
        模拟交易：买入股票，支持成本价加权平均计算
        
        参数:
        stock_code (str): 股票代码
        buy_volume (int): 买入数量
        buy_price (float): 买入价格
        strategy (str): 策略标识
        
        返回:
        bool: 是否操作成功
        """
        try:
            # 获取当前持仓
            position = self.get_position(stock_code)
            
            logger.info(f"[模拟交易] 开始处理 {stock_code} 买入，数量: {buy_volume}, 价格: {buy_price:.2f}")
            
            # 记录交易到数据库
            trade_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            trade_id = f"SIM_{datetime.now().strftime('%Y%m%d%H%M%S')}_{stock_code}_BUY"
            
            # 保存交易记录
            trade_saved = self._save_simulated_trade_record(
                stock_code=stock_code,
                trade_time=trade_time,
                trade_type='BUY',
                price=buy_price,
                volume=buy_volume,
                amount=buy_price * buy_volume,
                trade_id=trade_id,
                strategy=strategy
            )
            
            if not trade_saved:
                logger.error(f"[模拟交易] 保存交易记录失败: {stock_code}")
                return False
            
            # 计算买入成本（扣除手续费）
            commission_rate = 0.0003  # 买入手续费率
            cost = buy_price * buy_volume * (1 + commission_rate)
            
            if position:
                # 已有持仓，计算加权平均成本价
                old_volume = int(position.get('volume', 0))
                old_cost_price = float(position.get('cost_price', 0))
                old_available = int(position.get('available', old_volume))
                
                # 计算新的持仓数据
                new_volume = old_volume + buy_volume
                new_available = old_available + buy_volume
                
                # 加权平均成本价计算
                total_cost = (old_volume * old_cost_price) + (buy_volume * buy_price)
                new_cost_price = total_cost / new_volume
                
                logger.info(f"[模拟交易] {stock_code} 加仓:")
                logger.info(f"  - 原持仓: 数量={old_volume}, 成本价={old_cost_price:.2f}")
                logger.info(f"  - 新买入: 数量={buy_volume}, 价格={buy_price:.2f}")
                logger.info(f"  - 合并后: 数量={new_volume}, 新成本价={new_cost_price:.2f}")
                
                # 获取其他持仓信息
                current_price = position.get('current_price', buy_price)
                profit_triggered = position.get('profit_triggered', False)
                highest_price = max(float(position.get('highest_price', 0)), buy_price)
                open_date = position.get('open_date')  # 保持原开仓日期
                stock_name = position.get('stock_name')
                
            else:
                # 新建仓
                new_volume = buy_volume
                new_available = buy_volume
                new_cost_price = buy_price
                current_price = buy_price
                profit_triggered = False
                highest_price = buy_price
                open_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')  # 新开仓时间
                stock_name = self.data_manager.get_stock_name(stock_code)
                
                logger.info(f"[模拟交易] {stock_code} 新建仓: 数量={new_volume}, 成本价={new_cost_price:.2f}")
            
            # 重新计算止损价格
            new_stop_loss_price = self.calculate_stop_loss_price(
                new_cost_price, highest_price, profit_triggered
            )
            
            # 更新持仓 - 关键：在模拟模式下特殊处理
            success = self._simulate_update_position(
                stock_code=stock_code,
                volume=new_volume,
                available=new_available,
                cost_price=new_cost_price,
                current_price=current_price,
                profit_triggered=profit_triggered,
                highest_price=highest_price,
                open_date=open_date,
                stop_loss_price=new_stop_loss_price,
                stock_name=stock_name
            )
            
            if success:
                logger.info(f"[模拟交易] {stock_code} 买入完成")
                
                # 更新模拟账户资金
                config.SIMULATION_BALANCE -= cost
                logger.info(f"[模拟交易] 账户资金减少: -{cost:.2f}, 当前余额: {config.SIMULATION_BALANCE:.2f}")
                
                # 触发数据版本更新
                self._increment_data_version()
            else:
                logger.error(f"[模拟交易] {stock_code} 持仓更新失败")
            
            return success
            
        except Exception as e:
            logger.error(f"模拟买入 {stock_code} 时出错: {str(e)}")
            return False

    def _simulate_update_position(self, stock_code, volume, cost_price, available=None,
                                current_price=None, profit_triggered=False, highest_price=None,
                                open_date=None, stop_loss_price=None, stock_name=None):
        """
        模拟交易专用的持仓更新方法 - 只更新内存数据库

        这个方法确保模拟交易的数据变更只影响内存数据库，不会同步到SQLite
        """
        try:
            # 确保stock_code有效
            if stock_code is None or stock_code == "":
                logger.error("股票代码不能为空")
                return False

            if stock_name is None:
                stock_name = self.data_manager.get_stock_name(stock_code)

            # 类型转换
            p_volume = int(float(volume)) if volume is not None else 0
            p_cost_price = float(cost_price) if cost_price is not None else 0.0
            p_current_price = float(current_price) if current_price is not None else p_cost_price
            p_available = int(float(available)) if available is not None else p_volume
            p_highest_price = float(highest_price) if highest_price is not None else p_current_price
            p_stop_loss_price = float(stop_loss_price) if stop_loss_price is not None else None

            # 布尔值转换
            if isinstance(profit_triggered, str):
                p_profit_triggered = profit_triggered.lower() in ['true', '1', 't', 'y', 'yes']
            else:
                p_profit_triggered = bool(profit_triggered)

            # 如果当前价格为None，获取最新行情
            if p_current_price is None or p_current_price <= 0:
                latest_data = self.data_manager.get_latest_data(stock_code)
                if latest_data and 'lastPrice' in latest_data and latest_data['lastPrice'] is not None:
                    p_current_price = float(latest_data['lastPrice'])
                else:
                    p_current_price = p_cost_price

            # 计算市值和收益率
            p_market_value = round(p_volume * p_current_price, 2)

            if p_cost_price > 0:
                p_profit_ratio = round(100 * (p_current_price - p_cost_price) / p_cost_price, 2)
            else:
                p_profit_ratio = 0.0

            # 处理止损价格
            if p_stop_loss_price is None:
                calculated_slp = self.calculate_stop_loss_price(p_cost_price, p_highest_price, p_profit_triggered)
                p_stop_loss_price = round(calculated_slp, 2) if calculated_slp is not None else None

            # 获取当前时间
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            if open_date is None:
                open_date = now

            with self.memory_conn_lock:
                # 检查是否已有持仓记录
                cursor = self.memory_conn.cursor()
                cursor.execute("SELECT open_date FROM positions WHERE stock_code=?", (stock_code,))
                result = cursor.fetchone()

                if result:
                    # 更新持仓 - 保持原开仓日期
                    original_open_date = result[0]
                    cursor.execute("""
                        UPDATE positions
                        SET volume=?, cost_price=?, current_price=?, market_value=?, available=?,
                            profit_ratio=?, last_update=?, highest_price=?, stop_loss_price=?,
                            profit_triggered=?, stock_name=?
                        WHERE stock_code=?
                    """, (p_volume, round(p_cost_price, 2), round(p_current_price, 2), p_market_value,
                        p_available, p_profit_ratio, now, round(p_highest_price, 2),
                        round(p_stop_loss_price, 2) if p_stop_loss_price else None,
                        p_profit_triggered, stock_name, stock_code))
                else:
                    # 新增持仓
                    cursor.execute("""
                        INSERT INTO positions
                        (stock_code, stock_name, volume, cost_price, current_price, market_value,
                        available, profit_ratio, last_update, open_date, profit_triggered,
                        highest_price, stop_loss_price)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (stock_code, stock_name, p_volume, round(p_cost_price, 2),
                        round(p_current_price, 2), p_market_value, p_available, p_profit_ratio,
                        now, open_date, p_profit_triggered, round(p_highest_price, 2),
                        round(p_stop_loss_price, 2) if p_stop_loss_price else None))

                self.memory_conn.commit()

            # 注意：这里不调用 _increment_data_version()，由调用方决定何时触发
            self._increment_data_version()
            logger.debug(f"[模拟交易] 内存数据库更新成功: {stock_code}")
            return True

        except Exception as e:
            logger.error(f"模拟更新 {stock_code} 持仓时出错: {str(e)}")
            with self.memory_conn_lock:
                self.memory_conn.rollback()
            return False

    def simulate_sell_position(self, stock_code, sell_volume, sell_price, sell_type='partial'):
        """
        模拟交易：直接调整持仓数据 - 优化版本
        
        参数:
        stock_code (str): 股票代码
        sell_volume (int): 卖出数量
        sell_price (float): 卖出价格
        sell_type (str): 卖出类型，'partial'(部分卖出)或'full'(全部卖出)
        
        返回:
        bool: 是否操作成功
        """
        try:
            # 获取当前持仓
            position = self.get_position(stock_code)
            if not position:
                logger.error(f"模拟卖出失败：未持有 {stock_code}")
                return False
            
            # 安全获取当前持仓数据
            current_volume = int(position.get('volume', 0))
            current_available = int(position.get('available', current_volume))
            current_cost_price = float(position.get('cost_price', 0))
            
            # 检查卖出数量是否有效
            if sell_volume <= 0:
                logger.error(f"模拟卖出失败：卖出数量必须大于0，当前卖出数量: {sell_volume}")
                return False
                
            if sell_volume > current_volume:
                logger.error(f"模拟卖出失败：卖出数量超过持仓，当前持仓: {current_volume}, 卖出数量: {sell_volume}")
                return False
                
            if sell_volume > current_available:
                logger.error(f"模拟卖出失败：卖出数量超过可用数量，当前可用: {current_available}, 卖出数量: {sell_volume}")
                return False
            
            logger.info(f"[模拟交易] 开始处理 {stock_code} 卖出，数量: {sell_volume}, 价格: {sell_price:.2f}")
            logger.info(f"[模拟交易] 卖出前持仓：总数={current_volume}, 可用={current_available}, 成本价={current_cost_price:.2f}")
            
            # 记录交易到数据库
            trade_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            trade_id = f"SIM_{datetime.now().strftime('%Y%m%d%H%M%S')}_{stock_code}_{sell_type}"
            
            # 保存交易记录
            trade_saved = self._save_simulated_trade_record(
                stock_code=stock_code,
                trade_time=trade_time,
                trade_type='SELL',
                price=sell_price,
                volume=sell_volume,
                amount=sell_price * sell_volume,
                trade_id=trade_id,
                strategy=f'simu_{sell_type}'
            )
            
            if not trade_saved:
                logger.error(f"[模拟交易] 保存交易记录失败: {stock_code}")
                return False
            
            # 计算卖出收入（扣除手续费）
            commission_rate = 0.0013  # 卖出手续费率（含印花税）
            revenue = sell_price * sell_volume * (1 - commission_rate)
            
            if sell_type == 'full' or sell_volume >= current_volume:
                # 全仓卖出，从内存数据库删除持仓记录
                success = self._simulate_remove_position(stock_code)
                if success:
                    logger.info(f"[模拟交易] {stock_code} 全仓卖出完成，持仓已清零")
                    
                    # 更新模拟账户资金
                    config.SIMULATION_BALANCE += revenue
                    logger.info(f"[模拟交易] 账户资金增加: +{revenue:.2f}, 当前余额: {config.SIMULATION_BALANCE:.2f}")
                    
                    # 触发数据版本更新
                    self._increment_data_version()
                return success
            else:
                # 部分卖出，更新持仓数量
                new_volume = current_volume - sell_volume
                new_available = current_available - sell_volume
                
                # 确保新的可用数量不为负数
                new_available = max(0, new_available)
                
                # 获取其他持仓信息
                current_price = position.get('current_price', sell_price)
                profit_triggered = position.get('profit_triggered', False)
                highest_price = position.get('highest_price', current_price)
                open_date = position.get('open_date')
                stock_name = position.get('stock_name')
                
                # 关键修改：动态成本价计算
                if sell_type == 'partial' and not profit_triggered:
                    # 首次止盈卖出，计算获利分摊后的新成本价
                    sell_cost = sell_volume * current_cost_price  # 卖出部分的原成本
                    sell_profit = revenue - sell_cost  # 卖出获利
                    remaining_cost = new_volume * current_cost_price  # 剩余持仓原成本
                    
                    # 将获利分摊到剩余持仓，降低成本价
                    final_cost_price = max(0.01, (remaining_cost - sell_profit) / new_volume)
                    
                    logger.info(f"[模拟交易] {stock_code} 动态成本价计算:")
                    logger.info(f"  - 卖出获利: {sell_profit:.2f}元")
                    logger.info(f"  - 原成本价: {current_cost_price:.2f} -> 新成本价: {final_cost_price:.2f}")
                    
                    profit_triggered = True
                    self.mark_profit_triggered(stock_code)
                    logger.info(f"[模拟交易] {stock_code} 首次止盈完成，已标记profit_triggered=True")
                else:
                    # 其他情况保持原成本价
                    final_cost_price = current_cost_price
                
                # 重新计算止损价格
                new_stop_loss_price = self.calculate_stop_loss_price(
                    final_cost_price, highest_price, profit_triggered
                )
                
                # 更新持仓 - 使用模拟专用方法
                success = self._simulate_update_position(
                    stock_code=stock_code,
                    volume=new_volume,
                    available=new_available,
                    cost_price=final_cost_price,
                    current_price=current_price,
                    profit_triggered=profit_triggered,
                    highest_price=highest_price,
                    open_date=open_date,
                    stop_loss_price=new_stop_loss_price,
                    stock_name=stock_name
                )
                
                if success:
                    logger.info(f"[模拟交易] {stock_code} 部分卖出完成:")
                    logger.info(f"  - 剩余持仓: 总数={new_volume}, 可用={new_available}")
                    logger.info(f"  - 成本价: {final_cost_price:.2f} (保持不变)")
                    logger.info(f"  - 新止损价: {new_stop_loss_price:.2f}")
                    logger.info(f"  - 已触发首次止盈: {profit_triggered}")
                    
                    # 更新模拟账户资金
                    config.SIMULATION_BALANCE += revenue
                    logger.info(f"[模拟交易] 账户资金增加: +{revenue:.2f}, 当前余额: {config.SIMULATION_BALANCE:.2f}")
                    
                    # 触发数据版本更新
                    self._increment_data_version()
                    
                    # 验证更新结果
                    updated_position = self.get_position(stock_code)
                    if updated_position:
                        logger.info(f"[模拟交易] 验证更新结果: 总数={updated_position.get('volume')}, "
                                f"可用={updated_position.get('available')}, 成本价={updated_position.get('cost_price'):.2f}")
                    else:
                        logger.warning(f"[模拟交易] 无法获取更新后的持仓数据进行验证")
                else:
                    logger.error(f"[模拟交易] {stock_code} 持仓更新失败")
                
                return success
                
        except Exception as e:
            logger.error(f"模拟卖出 {stock_code} 时出错: {str(e)}")
            return False

    def _simulate_remove_position(self, stock_code):
        """
        模拟交易专用：从内存数据库删除持仓记录

        参数:
        stock_code (str): 股票代码

        返回:
        bool: 是否删除成功
        """
        try:
            with self.memory_conn_lock:
                cursor = self.memory_conn.cursor()
                cursor.execute("DELETE FROM positions WHERE stock_code=?", (stock_code,))
                self.memory_conn.commit()

                if cursor.rowcount > 0:
                    logger.info(f"[模拟交易] 已从内存数据库删除 {stock_code} 的持仓记录")
                    return True
                else:
                    logger.warning(f"[模拟交易] 未找到 {stock_code} 的持仓记录，无需删除")
                    return False

        except Exception as e:
            logger.error(f"删除 {stock_code} 的模拟持仓记录时出错: {str(e)}")
            with self.memory_conn_lock:
                self.memory_conn.rollback()
            return False

    def _save_simulated_trade_record(self, stock_code, trade_time, trade_type, price, volume, amount, trade_id, strategy='simu'):
        """保存模拟交易记录到数据库"""
        try:
            # 获取股票名称
            stock_name = self.data_manager.get_stock_name(stock_code)
            commission = amount * 0.0013 if trade_type == 'SELL' else amount * 0.0003  # 模拟手续费
            
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT INTO trade_records 
                (stock_code, stock_name, trade_time, trade_type, price, volume, amount, trade_id, commission, strategy)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (stock_code, stock_name, trade_time, trade_type, price, volume, amount, trade_id, commission, strategy))
            
            self.conn.commit()
            logger.info(f"[模拟交易] 保存交易记录: {stock_code}({stock_name}) {trade_type} 价格:{price:.2f} 数量:{volume} 策略:{strategy}")
            return True
        
        except Exception as e:
            logger.error(f"保存模拟交易记录时出错: {str(e)}")
            self.conn.rollback()
            return False

    def _full_refresh_simulation_data(self):
        """模拟交易模式下的全量数据刷新"""
        try:
            logger.info("开始执行模拟交易全量数据刷新")
            
            # 1. 获取所有持仓
            positions = self.get_all_positions()
            if positions.empty:
                logger.debug("没有持仓数据，跳过全量刷新")
                return
            
            refresh_count = 0
            
            # 2. 逐个更新每只股票的完整数据
            for _, position in positions.iterrows():
                stock_code = position['stock_code']
                if stock_code is None:
                    continue
                    
                try:
                    success = self._refresh_single_position_full_data(stock_code, position)
                    if success:
                        refresh_count += 1
                        
                except Exception as e:
                    logger.error(f"刷新 {stock_code} 完整数据时出错: {str(e)}")
                    continue
            
            # 3. 强制触发版本更新
            self._increment_data_version()
            
            logger.info(f"模拟交易全量刷新完成，更新了 {refresh_count} 只股票的数据")
            
        except Exception as e:
            logger.error(f"执行模拟交易全量刷新时出错: {str(e)}")

    def _refresh_single_position_full_data(self, stock_code, position):
        """刷新单只股票的完整持仓数据"""
        try:
            # 1. 获取最新行情数据
            latest_quote = self.data_manager.get_latest_data(stock_code)
            if not latest_quote:
                logger.debug(f"无法获取 {stock_code} 的最新行情，跳过刷新")
                return False
            
            current_price = float(latest_quote.get('lastPrice', 0))
            if current_price <= 0:
                logger.debug(f"{stock_code} 最新价格无效: {current_price}")
                return False
            
            # 2. 提取现有持仓数据
            volume = int(position.get('volume', 0))
            cost_price = float(position.get('cost_price', 0))
            base_cost_price = float(position.get('base_cost_price', 0)) if position.get('base_cost_price') else None
            available = int(position.get('available', volume))
            profit_triggered = bool(position.get('profit_triggered', False))
            open_date = position.get('open_date')
            stock_name = position.get('stock_name')

            # 🔧 修复: 当cost_price无效时,使用base_cost_price计算盈亏率
            effective_cost_price = cost_price
            if cost_price <= 0 and base_cost_price is not None and base_cost_price > 0:
                effective_cost_price = base_cost_price
                logger.info(f"[止损修复] {stock_code} cost_price无效({cost_price}),使用base_cost_price: {effective_cost_price}")
            elif cost_price <= 0:
                effective_cost_price = 0.01  # 兜底值
                logger.warning(f"[止损修复] {stock_code} cost_price和base_cost_price都无效,使用兜底值: {effective_cost_price}")

            # 3. 计算/更新最高价（重要：基于历史数据重新计算）
            updated_highest_price = self._calculate_highest_price_since_open(stock_code, open_date, current_price)

            # 4. 重新计算所有衍生数据 (使用effective_cost_price)
            market_value = round(volume * current_price, 2)
            profit_ratio = round(100 * (current_price - effective_cost_price) / effective_cost_price, 2) if effective_cost_price > 0 else 0.0

            # 5. 重新计算动态止损价格 (使用effective_cost_price)
            logger.debug(f"[止损修复] {stock_code} 计算止损价: effective_cost={effective_cost_price:.2f}, highest={updated_highest_price:.2f}, triggered={profit_triggered}")
            stop_loss_price = self.calculate_stop_loss_price(effective_cost_price, updated_highest_price, profit_triggered)
            stop_loss_value = stop_loss_price if stop_loss_price is not None else 0.0
            logger.debug(f"[止损修复] {stock_code} 计算结果: stop_loss_price={stop_loss_value:.2f}")

            # 6. 执行数据库更新（P0修复: 添加锁保护）
            with self.memory_conn_lock:
                cursor = self.memory_conn.cursor()
                cursor.execute("""
                    UPDATE positions
                    SET current_price=?, market_value=?, profit_ratio=?, highest_price=?,
                        stop_loss_price=?, last_update=?
                    WHERE stock_code=?
                """, (
                    round(current_price, 2),
                    market_value,
                    profit_ratio,
                    round(updated_highest_price, 2),
                    round(stop_loss_price, 2) if stop_loss_price else None,
                    datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    stock_code
                ))

                self.memory_conn.commit()

            logger.debug(f"全量刷新 {stock_code}: 价格={current_price:.2f}, 最高价={updated_highest_price:.2f}, "
                        f"盈亏率={profit_ratio:.2f}%, 止损价={stop_loss_price:.2f}")

            return True

        except Exception as e:
            logger.error(f"刷新 {stock_code} 完整数据时出错: {str(e)}")
            # P0修复: rollback也需要锁保护
            with self.memory_conn_lock:
                self.memory_conn.rollback()
            return False

    def _calculate_highest_price_since_open(self, stock_code, open_date, current_price):
        """计算开仓以来的最高价 - 基于历史数据"""
        try:
            # 1. 从持仓记录获取当前最高价
            position = self.get_position(stock_code)
            current_highest = float(position.get('highest_price', current_price)) if position else current_price
            
            # 2. 在交易时间内，尝试获取当日高点
            if config.is_trade_time():
                latest_quote = self.data_manager.get_latest_data(stock_code)
                if latest_quote:
                    today_high = latest_quote.get('high', current_price)
                    if today_high and today_high > current_highest:
                        current_highest = float(today_high)
            
            # 3. 确保最高价不低于当前价
            final_highest = max(current_highest, current_price)
            
            return final_highest
            
        except Exception as e:
            logger.error(f"计算 {stock_code} 开仓以来最高价时出错: {str(e)}")
            return current_price

        
    def _mark_profit_breakout(self, stock_code, current_price):
        """标记已突破盈利阈值 - 修正版本"""
        try:
            # 更新内存数据库（P0修复: 添加锁保护）
            with self.memory_conn_lock:
                cursor = self.memory_conn.cursor()
                cursor.execute("""
                    UPDATE positions
                    SET profit_breakout_triggered = ?, breakout_highest_price = ?
                    WHERE stock_code = ?
                """, (True, current_price, stock_code))
                self.memory_conn.commit()

                if cursor.rowcount > 0:
                    logger.debug(f"{stock_code} 标记突破状态成功")
                    return True
                else:
                    logger.warning(f"{stock_code} 标记突破状态失败，未找到记录")
                    return False
                    
        except Exception as e:
            logger.error(f"标记 {stock_code} 突破状态失败: {str(e)}")
            return False

    def _update_breakout_highest_price(self, stock_code, new_highest_price):
        """更新突破后最高价 - 修正版本"""
        try:
            # 更新内存数据库（P0修复: 添加锁保护）
            with self.memory_conn_lock:
                cursor = self.memory_conn.cursor()
                cursor.execute("""
                    UPDATE positions
                    SET breakout_highest_price = ?
                    WHERE stock_code = ?
                """, (new_highest_price, stock_code))
                self.memory_conn.commit()
            
            if cursor.rowcount > 0:
                logger.debug(f"{stock_code} 更新突破后最高价成功: {new_highest_price:.2f}")
                return True
            else:
                logger.warning(f"{stock_code} 更新突破后最高价失败，未找到记录")
                return False
                    
        except Exception as e:
            logger.error(f"更新 {stock_code} 突破后最高价失败: {str(e)}")
            return False


    def initialize_all_positions_data(self):
        """
        初始化所有持仓数据 - 重新计算"买后最高"和"动态止损"
        复用现有逻辑，支持实盘和模拟交易
        """
        try:
            logger.info("开始初始化所有持仓数据...")
            
            # 1. 获取所有持仓（复用现有方法）
            positions = self.get_all_positions()
            if positions.empty:
                logger.info("没有持仓数据需要初始化")
                return {
                    'success': True, 
                    'message': '没有持仓数据需要初始化', 
                    'updated_count': 0
                }
            
            logger.info(f"找到 {len(positions)} 只股票需要初始化")
            
            refresh_count = 0
            error_count = 0
            
            # 2. 逐个更新每只股票（复用现有的刷新逻辑）
            for _, position in positions.iterrows():
                stock_code = position['stock_code']
                if stock_code is None:
                    continue
                    
                try:
                    # 直接使用现有的单股票刷新方法
                    success = self._refresh_single_position_full_data(stock_code, position)
                    if success:
                        refresh_count += 1
                        logger.debug(f"初始化 {stock_code} 成功")
                    else:
                        error_count += 1
                        logger.warning(f"初始化 {stock_code} 失败")
                        
                except Exception as e:
                    error_count += 1
                    logger.error(f"初始化 {stock_code} 时出错: {str(e)}")
                    continue
            
            # 3. 强制触发版本更新（复用现有机制）
            self._increment_data_version()
            
            # 4. 清理缓存
            self.positions_cache = None
            
            success_message = f"持仓数据初始化完成！成功更新 {refresh_count} 只股票"
            if error_count > 0:
                success_message += f"，{error_count} 只股票处理失败"
            
            logger.info(success_message)
            
            return {
                'success': True,
                'message': success_message,
                'updated_count': refresh_count,
                'error_count': error_count
            }
            
        except Exception as e:
            logger.error(f"初始化持仓数据时发生错误: {str(e)}")
            return {
                'success': False,
                'message': f'初始化失败: {str(e)}',
                'updated_count': 0
            }

    def mark_profit_triggered(self, stock_code):
        """标记股票已触发首次止盈"""
        try:
            # P0修复: 添加锁保护
            with self.memory_conn_lock:
                cursor = self.memory_conn.cursor()
                cursor.execute("UPDATE positions SET profit_triggered = ? WHERE stock_code = ?", (True, stock_code))
                self.memory_conn.commit()
            logger.info(f"已标记 {stock_code} profit_triggered已标记为True")
            return True
        except Exception as e:
            logger.error(f"标记 {stock_code} profit_triggered时出错: {str(e)}")
            # P0修复: rollback也需要锁保护
            with self.memory_conn_lock:
                self.memory_conn.rollback()
            return False

    def start_position_monitor_thread(self):
        """启动持仓监控线程"""
        
        if self.monitor_thread and self.monitor_thread.is_alive():
            logger.warning("持仓监控线程已在运行")
            return
            
        self.stop_flag = False
        self.monitor_thread = threading.Thread(target=self._position_monitor_loop)
        self.monitor_thread.daemon = True
        self.monitor_thread.start()
       
        logger.info("持仓监控线程已启动")
    
    def stop_position_monitor_thread(self):
        """停止持仓监控线程"""
        if self.monitor_thread and self.monitor_thread.is_alive():
            self.stop_flag = True
            self.monitor_thread.join(timeout=5)
            
            logger.info("持仓监控线程已停止")

    def get_all_positions_with_all_fields(self):
        """获取所有持仓的所有字段（包括内存数据库中的所有字段）"""
        try:
            query = "SELECT * FROM positions"
            # P0修复: 添加锁保护防止并发访问导致 "Gaps in blk ref_locs" 错误
            with self.memory_conn_lock:
                df = pd.read_sql_query(query, self.memory_conn)
            
            # 批量获取所有股票的行情
            if not df.empty:
                stock_codes = df['stock_code'].tolist()
                all_latest_data = {}
                
                # 批量获取所有股票的最新行情（如果交易时间）
                # if config.is_trade_time():
                for stock_code in stock_codes:
                    latest_data = self.data_manager.get_latest_data(stock_code)
                    if latest_data:
                        all_latest_data[stock_code] = latest_data
                
                # 计算涨跌幅
                change_percentages = {}
                for stock_code in df['stock_code']:
                    latest_data = all_latest_data.get(stock_code)
                    if latest_data:
                        lastPrice = latest_data.get('lastPrice')
                        lastClose = latest_data.get('lastClose')
                        if lastPrice is not None and lastClose is not None and lastClose != 0:
                            change_percentage = round((lastPrice - lastClose) / lastClose * 100, 2)
                            change_percentages[stock_code] = change_percentage
                        else:
                            change_percentages[stock_code] = 0.0
                    else:
                        change_percentages[stock_code] = 0.0
                
                # 将涨跌幅添加到 DataFrame 中
                df['change_percentage'] = df['stock_code'].map(change_percentages)
            
            logger.debug(f"获取到 {len(df)} 条持仓记录（所有字段），并计算了涨跌幅")
            return df
        except Exception as e:
            logger.error(f"获取所有持仓信息（所有字段）时出错: {str(e)}")
            return pd.DataFrame()

    def get_pending_signals(self):
        """获取待处理的信号 - 增加时效性检查"""
        with self.signal_lock:
            current_time = datetime.now()
            valid_signals = {}
            
            for stock_code, signal_data in self.latest_signals.items():
                signal_timestamp = signal_data.get('timestamp', current_time)
                # 信号有效期5分钟
                if (current_time - signal_timestamp).total_seconds() < 300:
                    valid_signals[stock_code] = signal_data
                else:
                    logger.debug(f"{stock_code} 信号已过期，自动清除")
            
            # 更新有效信号
            self.latest_signals = valid_signals
            return dict(valid_signals)
    
    def mark_signal_processed(self, stock_code):
        """标记信号已处理 - 增加状态跟踪"""
        with self.signal_lock:
            if stock_code in self.latest_signals:
                signal_type = self.latest_signals[stock_code]['type']
                logger.info(f"{stock_code} {signal_type}信号已标记为已处理并清除")
                self.latest_signals.pop(stock_code, None)
            else:
                logger.debug(f"{stock_code} 信号已不存在，无需处理")

    def _position_monitor_loop(self):
        """持仓监控循环 - 鲁棒性优化版本,支持无人值守运行"""
        logger.info("🚀 持仓监控循环已启动")

        # 线程异常监控（智能告警机制）
        loop_count = 0
        last_loop_time = time.time()
        consecutive_errors = 0  # 连续错误计数
        last_gap_warning_time = 0  # 最后一次GAP告警时间(去重机制)
        max_gap = 0  # 最大空档时间记录
        gap_count = 0  # 空档次数统计

        while not self.stop_flag:
            try:
                loop_start = time.time()
                loop_count += 1

                # ⭐ 关键优化1: 非交易时段立即跳过,避免无效API调用
                if not config.is_trade_time():
                    logger.debug(f"非交易时间(第{loop_count}次循环), 休眠{config.MONITOR_NON_TRADE_SLEEP}秒")
                    time.sleep(config.MONITOR_NON_TRADE_SLEEP)
                    last_loop_time = time.time()
                    continue

                # 检测循环间隔异常(仅交易时段)
                gap = loop_start - last_loop_time
                if gap > 10:
                    gap_count += 1
                    if gap > max_gap:
                        max_gap = gap

                    # 去重机制：60秒内只告警一次
                    if loop_start - last_gap_warning_time > 60:
                        logger.warning(
                            f"⚠ [MONITOR_GAP] 监控线程空档 {gap:.1f}秒"
                            f"（累计{gap_count}次,最大{max_gap:.1f}秒,已执行{loop_count}次循环）"
                        )
                        last_gap_warning_time = loop_start

                    # 严重阻塞(>60秒)触发ERROR级别告警
                    if gap > 60:
                        logger.error(f"❌ [MONITOR_CRITICAL] 严重阻塞 {gap:.1f}秒！")

                # ⭐ 关键优化2: 更新最高价使用短超时,失败不阻塞
                if time.time() - self.last_update_highest_time >= self.update_highest_interval:
                    try:
                        import concurrent.futures
                        timeout = config.MONITOR_CALL_TIMEOUT

                        with concurrent.futures.ThreadPoolExecutor() as executor:
                            future = executor.submit(self.update_all_positions_highest_price)
                            try:
                                future.result(timeout=timeout)
                            except concurrent.futures.TimeoutError:
                                logger.warning(f"[MONITOR_TIMEOUT] 更新最高价超时({timeout}秒),跳过")
                                # 不阻塞,继续执行
                    except Exception as e:
                        logger.error(f"[MONITOR_ERROR] 更新最高价异常: {e}")
                        # 同样不阻塞
                    finally:
                        # 无论成功与否都记录时间，避免频繁阻塞
                        self.last_update_highest_time = time.time()

                # ⭐ 关键优化3: 获取持仓使用短超时
                try:
                    timeout = config.MONITOR_CALL_TIMEOUT

                    with concurrent.futures.ThreadPoolExecutor() as executor:
                        future = executor.submit(self.get_all_positions)
                        try:
                            positions_df = future.result(timeout=timeout)
                            consecutive_errors = 0  # 重置错误计数
                        except concurrent.futures.TimeoutError:
                            consecutive_errors += 1
                            logger.warning(f"[MONITOR_TIMEOUT] 获取持仓超时,连续{consecutive_errors}次")
                            if consecutive_errors >= 3:
                                logger.error(f"❌ [MONITOR_CRITICAL] 连续{consecutive_errors}次超时!")
                            time.sleep(5)
                            last_loop_time = time.time()
                            continue
                except Exception as e:
                    consecutive_errors += 1
                    logger.error(f"[MONITOR_ERROR] 获取持仓失败: {e}")
                    if consecutive_errors >= 3:
                        logger.error(f"❌ [MONITOR_CRITICAL] 连续{consecutive_errors}次失败!")
                    time.sleep(5)
                    last_loop_time = time.time()
                    continue

                if positions_df.empty:
                    logger.debug("当前没有持仓，无需监控")
                    time.sleep(60)
                    last_loop_time = time.time()
                    continue

                # 处理所有持仓
                for _, position_row in positions_df.iterrows():
                    stock_code = position_row['stock_code']

                    # 🔑 优化: 一次性获取行情数据,避免重复调用API
                    try:
                        latest_quote = self.data_manager.get_latest_data(stock_code)
                        if not latest_quote:
                            logger.warning(f"{stock_code} 获取行情失败,跳过本次检查")
                            continue

                        current_price = float(latest_quote.get('lastPrice', 0))
                        if current_price <= 0:
                            logger.warning(f"{stock_code} 价格无效: {current_price},跳过本次检查")
                            continue
                    except Exception as e:
                        logger.error(f"{stock_code} 获取行情异常: {e}")
                        continue

                    # 调试日志
                    logger.debug(f"[MONITOR_CALL] 开始检查 {stock_code} 的交易信号 (价格: {current_price:.2f})")

                    # 使用统一的信号检查函数 (传入价格,避免内部重复调用API)
                    signal_type, signal_info = self.check_trading_signals(stock_code, current_price)

                    with self.signal_lock:
                        if signal_type:
                            existing_signal = self.latest_signals.get(stock_code)

                            # 🔑 信号优先级体系: stop_loss > grid_* > take_profit_*
                            # 止损信号优先级最高,可以覆盖任何信号
                            if signal_type == 'stop_loss':
                                self.latest_signals[stock_code] = {
                                    'type': signal_type,
                                    'info': signal_info,
                                    'timestamp': datetime.now()
                                }
                                logger.info(f"🔔 {stock_code} 检测到止损信号(最高优先级),覆盖已有信号")
                            # 普通止盈信号不能覆盖网格信号
                            elif existing_signal and existing_signal.get('type') in ['grid_buy', 'grid_sell']:
                                logger.info(f"{stock_code} 已有网格信号 {existing_signal.get('type')},跳过止盈信号 {signal_type}")
                            else:
                                self.latest_signals[stock_code] = {
                                    'type': signal_type,
                                    'info': signal_info,
                                    'timestamp': datetime.now()
                                }
                                logger.info(f"🔔 {stock_code} 检测到信号: {signal_type},等待策略处理")
                        else:
                            # 清除已不存在的信号（但保留网格信号，网格信号由网格检测逻辑管理）
                            # 已在锁保护范围内，无需再次获取
                            existing = self.latest_signals.get(stock_code)
                            if existing and existing.get('type', '').startswith('grid_'):
                                pass  # 保留网格信号，不清除
                            else:
                                self.latest_signals.pop(stock_code, None)

                    # ===== 网格交易信号检测 (使用已获取的价格) =====
                    # 网格信号检测应该独立于止盈止损信号
                    if self.grid_manager and config.ENABLE_GRID_TRADING:
                        try:
                            grid_signal = self.grid_manager.check_grid_signals(stock_code, current_price)
                            if grid_signal:
                                # 转换信号格式：'BUY' -> 'grid_buy', 'SELL' -> 'grid_sell'
                                grid_signal_type = f"grid_{grid_signal['signal_type'].lower()}"
                                with self.signal_lock:
                                    # 🔑 信号优先级保护: stop_loss > grid_* > take_profit_*
                                    existing = self.latest_signals.get(stock_code)
                                    # 止损信号优先级最高,不被网格信号覆盖
                                    if existing and existing.get('type') == 'stop_loss':
                                        logger.warning(f"[GRID] {stock_code} 已有止损信号,网格信号 {grid_signal_type} 不覆盖")
                                    else:
                                        self.latest_signals[stock_code] = {
                                            'type': grid_signal_type,
                                            'info': grid_signal,
                                            'timestamp': datetime.now()
                                        }
                                        logger.info(f"[GRID] {stock_code} 检测到网格信号: {grid_signal_type}")
                        except Exception as e:
                            logger.error(f"[GRID] {stock_code} 网格信号检测异常: {e}")

                    # 更新最高价（如果当前价格更高,使用已获取的价格）
                    try:
                        highest_price = float(position_row.get('highest_price', 0))

                        if current_price > highest_price:
                            new_highest_price = current_price
                            new_stop_loss_price = self.calculate_stop_loss_price(
                                float(position_row.get('cost_price', 0)),
                                new_highest_price,
                                bool(position_row.get('profit_triggered', False))
                            )
                            self.update_position(
                                stock_code=stock_code,
                                volume=int(position_row.get('volume', 0)),
                                cost_price=float(position_row.get('cost_price', 0)),
                                highest_price=new_highest_price,
                                profit_triggered=bool(position_row.get('profit_triggered', False)),
                                open_date=position_row.get('open_date'),
                                stop_loss_price=new_stop_loss_price
                            )
                    except (TypeError, ValueError) as e:
                        logger.error(f"更新最高价时类型转换错误 - {stock_code}: {e}")

                # 检查委托单超时
                self.check_pending_orders_timeout()

                # 记录本次循环耗时（只在异常时告警）
                loop_end = time.time()
                loop_duration = loop_end - loop_start
                if loop_duration > 7:  # 循环超过7秒告警
                    logger.warning(f"⚠ [MONITOR_SLOW] 耗时 {loop_duration:.2f}秒（超7秒），"
                                 f"已处理{len(positions_df)}只股票")
                last_loop_time = loop_end

                # 等待下一次监控
                time.sleep(config.MONITOR_LOOP_INTERVAL)

            except Exception as e:
                logger.error(f"🚨 [MONITOR_FATAL] 持仓监控循环出错: {str(e)}", exc_info=True)
                time.sleep(60)  # 出错后等待一分钟再继续

    # ========== 委托单超时管理功能 ==========

    def track_order(self, stock_code, order_id, signal_type, signal_info):
        """
        跟踪新提交的委托单

        参数:
        stock_code (str): 股票代码
        order_id (str): 委托单ID
        signal_type (str): 信号类型
        signal_info (dict): 信号详细信息
        """
        try:
            with self.pending_orders_lock:
                self.pending_orders[stock_code] = {
                    'order_id': order_id,
                    'submit_time': datetime.now(),
                    'signal_type': signal_type,
                    'signal_info': signal_info,
                    'stock_code': stock_code
                }
                logger.info(f"📋 开始跟踪委托单: {stock_code} {signal_type} order_id={order_id}")
        except Exception as e:
            logger.error(f"跟踪委托单失败: {str(e)}")

    def check_pending_orders_timeout(self):
        """
        检查所有待处理委托单是否超时
        在持仓监控线程中定期调用
        """
        try:
            # 功能开关检查
            if not config.ENABLE_PENDING_ORDER_AUTO_CANCEL:
                return

            # 仅在实盘模式下检查
            if config.ENABLE_SIMULATION_MODE:
                return

            # 检查间隔控制
            current_time = time.time()
            if current_time - self.last_order_check_time < self.order_check_interval:
                return

            self.last_order_check_time = current_time

            # 检查每个待处理委托单
            timeout_orders = []

            with self.pending_orders_lock:
                for stock_code, order_info in list(self.pending_orders.items()):
                    submit_time = order_info['submit_time']
                    elapsed_minutes = (datetime.now() - submit_time).total_seconds() / 60

                    # 检查是否超时
                    if elapsed_minutes >= config.PENDING_ORDER_TIMEOUT_MINUTES:
                        timeout_orders.append(order_info)

            # 处理超时委托单
            for order_info in timeout_orders:
                self._handle_timeout_order(order_info)

        except Exception as e:
            logger.error(f"检查委托单超时失败: {str(e)}")

    def _handle_timeout_order(self, order_info):
        """
        处理超时的委托单

        参数:
        order_info (dict): 委托单信息
        """
        try:
            stock_code = order_info['stock_code']
            order_id = order_info['order_id']
            signal_type = order_info['signal_type']
            signal_info = order_info['signal_info']
            submit_time = order_info['submit_time']
            elapsed = (datetime.now() - submit_time).total_seconds() / 60

            logger.warning(f"⏰ {stock_code} 委托单超时: order_id={order_id}, "
                         f"信号类型={signal_type}, 已等待{elapsed:.1f}分钟")

            # 查询委托单当前状态
            order_status = self._query_order_status(stock_code, order_id)

            if order_status is None:
                logger.error(f"❌ 无法查询委托单状态: {stock_code} {order_id}")
                # 从跟踪列表移除
                with self.pending_orders_lock:
                    self.pending_orders.pop(stock_code, None)
                return

            # 如果已成交，移除跟踪
            if order_status in [56]:  # 56=已成
                logger.info(f"✅ {stock_code} 委托单已成交: {order_id}")
                with self.pending_orders_lock:
                    self.pending_orders.pop(stock_code, None)
                return

            # 如果是未成交状态，执行撤单
            if order_status in [48, 49, 50, 55]:  # 未成交状态
                logger.warning(f"🚨 {stock_code} 委托单超时未成交，准备撤单...")

                # 执行撤单
                cancel_result = self._cancel_order(stock_code, order_id)

                if cancel_result:
                    logger.info(f"✅ {stock_code} 委托单撤销成功: {order_id}")

                    # 如果配置了自动重新挂单
                    if config.PENDING_ORDER_AUTO_REORDER:
                        logger.info(f"🔄 {stock_code} 准备重新挂单...")
                        self._reorder_after_cancel(stock_code, signal_type, signal_info)

                    # 从跟踪列表移除
                    with self.pending_orders_lock:
                        self.pending_orders.pop(stock_code, None)
                else:
                    logger.error(f"❌ {stock_code} 委托单撤销失败: {order_id}")
                    # 失败后也移除，避免重复处理
                    with self.pending_orders_lock:
                        self.pending_orders.pop(stock_code, None)
            else:
                # 其他状态（已撤、废单等），直接移除跟踪
                logger.info(f"ℹ️ {stock_code} 委托单状态={order_status}, 移除跟踪")
                with self.pending_orders_lock:
                    self.pending_orders.pop(stock_code, None)

        except Exception as e:
            logger.error(f"处理超时委托单失败: {str(e)}")

    def _query_order_status(self, stock_code, order_id):
        """
        查询委托单状态

        参数:
        stock_code (str): 股票代码
        order_id (str or int): 委托单ID (会自动转换为int类型)

        返回:
        int: 委托单状态码，查询失败返回None
        """
        try:
            if not self.qmt_trader or not self.qmt_connected:
                return None

            # 修复: 确保order_id是int类型
            if isinstance(order_id, str):
                try:
                    order_id_int = int(order_id)
                    logger.debug(f"{stock_code} 委托单ID从str转换为int: '{order_id}' -> {order_id_int}")
                    order_id = order_id_int
                except ValueError:
                    logger.error(f"{stock_code} 委托单ID无法转换为int: '{order_id}'")
                    return None
            elif not isinstance(order_id, int):
                logger.error(f"{stock_code} 委托单ID类型不支持: {type(order_id)}")
                return None

            # 查询单个委托单 (order_id已确保是int类型)
            order = self.qmt_trader.xt_trader.query_stock_order(
                self.qmt_trader.acc, order_id
            )

            if order:
                return order.order_status

            return None

        except Exception as e:
            logger.error(f"查询委托单状态失败: {str(e)}")
            return None

    def _cancel_order(self, stock_code, order_id):
        """
        撤销委托单

        参数:
        stock_code (str): 股票代码
        order_id (str or int): 委托单ID (会自动转换为int类型)

        返回:
        bool: 是否撤单成功
        """
        try:
            if not self.qmt_trader or not self.qmt_connected:
                logger.error("QMT未连接，无法撤单")
                return False

            # 修复: 确保order_id是int类型
            if isinstance(order_id, str):
                try:
                    order_id_int = int(order_id)
                    logger.debug(f"{stock_code} 撤单ID从str转换为int: '{order_id}' -> {order_id_int}")
                    order_id = order_id_int
                except ValueError:
                    logger.error(f"{stock_code} 撤单ID无法转换为int: '{order_id}'")
                    return False
            elif not isinstance(order_id, int):
                logger.error(f"{stock_code} 撤单ID类型不支持: {type(order_id)}")
                return False

            # 调用QMT撤单接口 (order_id已确保是int类型)
            result = self.qmt_trader.xt_trader.cancel_order_stock(
                self.qmt_trader.acc, order_id
            )

            # 0表示成功，-1表示失败
            return result == 0

        except Exception as e:
            logger.error(f"撤单失败: {str(e)}")
            return False

    def _reorder_after_cancel(self, stock_code, signal_type, signal_info):
        """
        撤单后重新挂单

        参数:
        stock_code (str): 股票代码
        signal_type (str): 信号类型
        signal_info (dict): 原信号信息
        """
        try:
            # 获取最新价格
            latest_quote = self.data_manager.get_latest_data(stock_code)
            if not latest_quote:
                logger.error(f"{stock_code} 无法获取最新价格，放弃重新挂单")
                return

            current_price = latest_quote.get('close', 0)

            # 根据配置的价格模式确定新挂单价格
            price_mode = config.PENDING_ORDER_REORDER_PRICE_MODE

            if price_mode == "market":
                # 市价模式：使用当前价
                new_price = current_price
                logger.info(f"📌 使用市价模式: {new_price:.2f}")

            elif price_mode == "best":
                # 对手价模式：卖单用买三价，买单用卖三价
                # 对于卖出信号，使用买三价
                bid3 = latest_quote.get('bid3', latest_quote.get('bid1', current_price))
                new_price = bid3
                logger.info(f"📌 使用对手价模式(买三价): {new_price:.2f}")

            else:  # "limit"
                # 限价模式：使用原价格
                new_price = signal_info.get('current_price', current_price)
                logger.info(f"📌 使用限价模式(原价格): {new_price:.2f}")

            # 获取卖出数量
            volume = signal_info.get('volume', 0)

            if volume <= 0:
                logger.error(f"{stock_code} 卖出数量无效: {volume}，放弃重新挂单")
                return

            # 调用交易执行器重新挂单
            from trading_executor import get_trading_executor
            trading_executor = get_trading_executor()

            logger.info(f"🔄 {stock_code} 重新挂单: 数量={volume}, 价格={new_price:.2f}")

            # 执行卖出
            result = trading_executor.sell_stock(
                stock_code=stock_code,
                sell_volume=volume,
                sell_price=new_price,
                strategy=f"reorder_{signal_type}"
            )

            if result:
                logger.info(f"✅ {stock_code} 重新挂单成功")
                # 跟踪新委托单
                new_order_id = result.get('order_id')
                if new_order_id:
                    self.track_order(stock_code, new_order_id, signal_type, signal_info)
            else:
                logger.error(f"❌ {stock_code} 重新挂单失败")

        except Exception as e:
            logger.error(f"重新挂单失败: {str(e)}")

    def init_grid_manager(self, trading_executor):
        """初始化网格交易管理器"""
        if not config.ENABLE_GRID_TRADING:
            logger.info("网格交易功能未启用")
            return

        try:
            from grid_trading_manager import GridTradingManager
            self.grid_manager = GridTradingManager(
                self.db_manager,
                self,
                trading_executor
            )
            logger.info("网格交易管理器初始化完成")
        except Exception as e:
            logger.error(f"网格交易管理器初始化失败: {str(e)}")


# 单例模式
_instance = None

def get_position_manager():
    """获取PositionManager单例"""
    global _instance
    if _instance is None:
        _instance = PositionManager()
    return _instance
