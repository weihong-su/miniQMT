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
        self.qmt_trader.connect()

        # 创建内存数据库
        self.memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._create_memory_table()
        self._sync_db_to_memory()

        # 添加模拟交易模式的提示日志
        if hasattr(config, 'ENABLE_SIMULATION_MODE') and config.ENABLE_SIMULATION_MODE:
            logger.warning("系统以模拟交易模式运行 - 持仓变更只在内存中进行，不会写入数据库")

        # 添加缓存机制
        self.last_position_update_time = 0
        self.position_update_interval = 3  # 3秒更新间隔
        self.positions_cache = None        

        # 新增，持仓数据版本控制
        self.data_version = 0
        self.data_changed = False
        self.version_lock = threading.Lock()

        # 新增：全量刷新控制 - 在这里添加缺失的属性
        self.last_full_refresh_time = 0
        self.full_refresh_interval = 60  # 1分钟全量刷新间隔

        # 定时同步线程
        self.sync_thread = None
        self.sync_stop_flag = False
        self.start_sync_thread()

        # 添加信号状态管理
        self.signal_lock = threading.Lock()
        self.latest_signals = {}  # 存储最新检测到的信号
        self.signal_timestamps = {}  # 信号时间戳      


    def _increment_data_version(self):
        """递增数据版本号"""
        with self.version_lock:
            self.data_version += 1
            self.data_changed = True
            logger.debug(f"持仓数据版本更新: v{self.data_version}")

    def _create_memory_table(self):
        """创建内存数据库表结构"""
        cursor = self.memory_conn.cursor()
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS positions (
            stock_code TEXT PRIMARY KEY,
            stock_name TEXT,
            volume REAL,
            available REAL,           
            cost_price REAL,
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
        logger.info("内存数据库表结构已创建")

    def _sync_real_positions_to_memory(self, real_positions_df):
        """将实盘持仓数据同步到内存数据库"""
        try:
            # 首先检查输入数据
            if real_positions_df is None or not isinstance(real_positions_df, pd.DataFrame) or real_positions_df.empty:
                logger.warning("传入的实盘持仓数据无效，跳过同步")
                return
                
            # 确保必要的列存在
            required_columns = ['证券代码', '股票余额', '可用余额', '成本价', '市值']
            missing_columns = [col for col in required_columns if col not in real_positions_df.columns]
            if missing_columns:
                logger.warning(f"实盘持仓数据缺少必要列: {missing_columns}，无法同步")
                return

            # 获取内存数据库中所有持仓的股票代码
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
                        logger.warning(f"获取 {stock_code} 的最新价格失败: {str(e)}，使用成本价")
                    
                    # 查询内存数据库中是否已存在该股票的持仓记录
                    cursor.execute("SELECT profit_triggered, open_date, highest_price, stop_loss_price FROM positions WHERE stock_code=?", (stock_code,))
                    result = cursor.fetchone()
                    
                    if result:
                        # 如果存在，则更新持仓信息，但不修改open_date
                        profit_triggered = result[0] if result[0] is not None else False
                        open_date = result[1] if result[1] is not None else datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        highest_price = result[2] if result[2] is not None else 0.0
                        stop_loss_price = result[3] if result[3] is not None else 0.0
                        
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
                            stop_loss_price=stop_loss_price
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
                # 只有通过所有检查后才执行删除
                if memory_stock_codes:  # 有需要删除的记录
                    logger.info(f"准备删除 {len(memory_stock_codes)} 个不在外部数据中的持仓: {list(memory_stock_codes)}")
                    
                    # 逐个删除并记录结果
                    successfully_deleted = []
                    failed_deletions = []
                    
                    for stock_code in memory_stock_codes:
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
            else:
                logger.info(f"模拟交易模式：保留内存中的模拟持仓记录，不与实盘同步删除")

            # 更新 stock_positions.json
            self._update_stock_positions_file(current_positions)

        except Exception as e:
            logger.error(f"同步实盘持仓数据到内存数据库时出错: {str(e)}")
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

                db_positions.to_sql("positions", self.memory_conn, if_exists="replace", index=False)
                self.memory_conn.commit()
                logger.info("数据库数据已同步到内存数据库")
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
                # memory_positions = pd.read_sql_query("SELECT stock_code, stock_name, open_date, profit_triggered, highest_price, stop_loss_price FROM positions", self.memory_conn)
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
                                INSERT INTO positions (stock_code, stock_name, volume, available, cost_price, open_date, profit_triggered, highest_price, stop_loss_price, profit_breakout_triggered, breakout_highest_price, last_update) 
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (stock_code, stock_name, volume, available, cost_price, current_date, profit_triggered, highest_price, stop_loss_price, profit_breakout_triggered, breakout_highest_price, now))
                            
                            insert_count += 1
                            # 插入新记录后，立即从数据库读取 open_date，以确保内存数据库与数据库一致
                            cursor.execute("SELECT open_date FROM positions WHERE stock_code=?", (stock_code,))
                            db_open_date = cursor.fetchone()[0]
                            memory_cursor = self.memory_conn.cursor()
                            memory_cursor.execute("UPDATE positions SET open_date=? WHERE stock_code=?", (db_open_date, stock_code))
                            self.memory_conn.commit()
                            logger.info(f"插入新的SQLite记录: {stock_code}, 使用日期: {current_date}")


                    sync_db_conn.commit()
                    if update_count > 0 or insert_count > 0:
                        logger.info(f"SQLite同步完成: 更新{update_count}条, 插入{insert_count}条记录")

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
        logger.info("定时同步线程已启动")

    def stop_sync_thread(self):
        """停止定时同步线程"""
        if self.sync_thread and self.sync_thread.is_alive():
            self.sync_stop_flag = True
            self.sync_thread.join(timeout=5)
            logger.info("定时同步线程已停止")

    # position_manager.py:_sync_loop() 方法修改
    def _sync_loop(self):
        """定时同步循环 - 增强版"""
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
                
                # 调整休眠时间
                sleep_time = 3 if (hasattr(config, 'ENABLE_SIMULATION_MODE') and 
                                config.ENABLE_SIMULATION_MODE and 
                                config.is_trade_time()) else 5
                
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
                    
                    # 更新缓存和时间戳
                    query = "SELECT * FROM positions"
                    self.positions_cache = pd.read_sql_query(query, self.memory_conn)
                    
                    # 确保所有列都有合适的默认值
                    if not self.positions_cache.empty:
                        # 确保数值列为数值类型
                        numeric_columns = ['volume', 'available', 'cost_price', 'current_price', 
                                            'market_value', 'profit_ratio', 'highest_price', 'stop_loss_price','breakout_highest_price']
                        for col in numeric_columns:
                            if col in self.positions_cache.columns:
                                # 转换为数值，无效值替换为0
                                self.positions_cache[col] = pd.to_numeric(self.positions_cache[col], errors='coerce').fillna(0)
                        
                        # 确保布尔列为布尔类型
                        if 'profit_triggered' in self.positions_cache.columns:
                            self.positions_cache['profit_triggered'] = self.positions_cache['profit_triggered'].fillna(False)

                        # 确保布尔列为布尔类型
                        if 'profit_breakout_triggered' in self.positions_cache.columns:
                            self.positions_cache['profit_breakout_triggered'] = self.positions_cache['profit_breakout_triggered'].fillna(False)    
                    
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
                
                # 验证止损价
                stop_loss_price = position_dict.get('stop_loss_price', 0)
                if stop_loss_price > cost_price * 2 or stop_loss_price < cost_price * 0.3:
                    logger.warning(f"{stock_code} 止损价数据异常: {stop_loss_price}，重置为0")
                    position_dict['stop_loss_price'] = 0.0
            
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
                   stock_name=None):
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
            p_volume = int(volume) if volume is not None else 0
            final_cost_price = float(cost_price) if cost_price is not None else 0.0
            final_current_price = float(current_price) if current_price is not None else final_cost_price
            final_highest_price = float(current_price) if current_price is not None else final_cost_price
            p_market_value = float(market_value) if market_value is not None else (p_volume * final_current_price)
            p_available = int(available) if available is not None else p_volume
            p_profit_ratio = float(profit_ratio) if profit_ratio is not None else (
                round(100 * (final_current_price - final_cost_price) / final_cost_price, 2) if final_cost_price > 0 else 0.0
            )
            # profit_triggered 布尔值转换
            if isinstance(profit_triggered, str):
                p_profit_triggered = profit_triggered.lower() in ['true', '1', 't', 'y', 'yes']
            else:
                p_profit_triggered = bool(profit_triggered)
                
            p_profit_triggered = bool(profit_triggered) if profit_triggered is not None else False

            
            # 获取当前时间
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            cursor = self.memory_conn.cursor()
            
            # 【关键修改】设置row_factory为字典模式，然后立即恢复
            original_row_factory = self.memory_conn.row_factory
            self.memory_conn.row_factory = sqlite3.Row
            
            try:
                # 【关键修改】使用字典查询替代位置索引
                dict_cursor = self.memory_conn.cursor()
                dict_cursor.execute("SELECT open_date, profit_triggered, highest_price, stop_loss_price FROM positions WHERE stock_code=?", (stock_code,))
                result_row = dict_cursor.fetchone()
                              
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
                    
                    # 如果最高价发生变化，强制重新计算止损价格
                    if old_db_highest_price != final_highest_price:
                        logger.info(f"{stock_code} 最高价变化：{old_db_highest_price} -> {final_highest_price}，重新计算止损价格")
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
                    # 计算止损价格
                    calculated_slp = self.calculate_stop_loss_price(final_cost_price, final_highest_price, profit_triggered)
                    final_stop_loss_price = round(calculated_slp, 2) if calculated_slp is not None else None
                    
                    if stock_name is None:
                        stock_name = stock_code

                    cursor.execute("""
                        INSERT INTO positions 
                        (stock_code, stock_name, volume, cost_price, current_price, market_value, available, profit_ratio, last_update, open_date, profit_triggered, highest_price, stop_loss_price)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (stock_code, stock_name, int(p_volume), final_cost_price, final_current_price, p_market_value, 
                        int(p_available), p_profit_ratio, now, open_date, profit_triggered, final_highest_price, final_stop_loss_price))
                    
                    logger.info(f"新增 {stock_code} 持仓: 数量={p_volume}, 成本价={final_cost_price}, 最高价={final_highest_price}, 止损价={final_stop_loss_price}")

            finally:
                # 【关键修改】确保恢复原始row_factory
                self.memory_conn.row_factory = original_row_factory
            
            self.memory_conn.commit()
            
            # 强制触发版本更新（保持原有逻辑）
            self._increment_data_version()
            
            return True

        except Exception as e:
            logger.error(f"更新 {stock_code} 持仓Error: {str(e)}")
            try:
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

                # Format open_date to YYYY-MM-DD for getStockData
                open_date_formatted = open_date.strftime('%Y-%m-%d')

                # Get today's date for getStockData
                today_formatted = datetime.now().strftime('%Y-%m-%d')

                # 获取从开仓日期到今天的历史数据
                try:
                    # Get the latest data 
                    history_data = Methods.getStockData(
                        code=stock_code,
                        fields="high",
                        start_date=open_date_formatted,
                        freq= 'd',  # 日线
                        adjustflag= '2'
                    )                    

                except Exception as e:
                    logger.error(f"获取 {stock_code} 从 {open_date_formatted} 到 {today_formatted} 的历史数据时出错: {str(e)}")
                    continue

                if history_data is not None and not history_data.empty:
                    # 找到开仓后日线数据最高价
                    highest_price = history_data['high'].astype(float).max()
                else:
                    highest_price = 0.0
                    logger.warning(f"未能获取 {stock_code} 从 {open_date_formatted} 到 {today_formatted} 的历史数据，跳过更新最高价")

                # 开盘时间，获取最新tick数据
                if config.is_trade_time:
                    latest_data = self.data_manager.get_latest_data(stock_code)
                    if latest_data:
                        current_price = latest_data.get('lastPrice')
                        current_high_price = latest_data.get('high')
                        if current_high_price and current_high_price > highest_price:
                            highest_price = current_high_price
                
                if highest_price > current_highest_price:
                    # 更新持仓"最高价"信息
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
            
            if account_df.empty:
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
    
    def get_grid_trades(self, stock_code, status=None):
        """
        获取网格交易记录
        
        参数:
        stock_code (str): 股票代码
        status (str): 状态筛选，如 'PENDING', 'ACTIVE', 'COMPLETED'
        
        返回:
        pandas.DataFrame: 网格交易记录
        """
        try:
            query = "SELECT * FROM grid_trades WHERE stock_code=?"
            params = [stock_code]
            
            if status:
                query += " AND status=?"
                params.append(status)
                
            query += " ORDER BY grid_level"
            
            df = pd.read_sql_query(query, self.conn, params=params)
            logger.debug(f"获取到 {stock_code} 的 {len(df)} 条网格交易记录")
            return df
            
        except Exception as e:
            logger.error(f"获取 {stock_code} 的网格交易记录时出错: {str(e)}")
            return pd.DataFrame()
    
    def add_grid_trade(self, stock_code, grid_level, buy_price, sell_price, volume):
        """
        添加网格交易记录
        
        参数:
        stock_code (str): 股票代码
        grid_level (int): 网格级别
        buy_price (float): 买入价格
        sell_price (float): 卖出价格
        volume (int): 交易数量
        
        返回:
        int: 新增网格记录的ID，失败返回-1
        """
        try:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT INTO grid_trades 
                (stock_code, grid_level, buy_price, sell_price, volume, status, create_time, update_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (stock_code, grid_level, buy_price, sell_price, volume, 'PENDING', now, now))
            
            self.conn.commit()
            grid_id = cursor.lastrowid
            
            logger.info(f"添加 {stock_code} 的网格交易记录成功，ID: {grid_id}, 级别: {grid_level}, 买入价: {buy_price}, 卖出价: {sell_price}, 数量: {volume}")
            return grid_id
            
        except Exception as e:
            logger.error(f"添加 {stock_code} 的网格交易记录时出错: {str(e)}")
            self.conn.rollback()
            return -1
    
    def update_grid_trade_status(self, grid_id, status):
        """
        更新网格交易状态
        
        参数:
        grid_id (int): 网格交易ID
        status (str): 新状态，如 'PENDING', 'ACTIVE', 'COMPLETED'
        
        返回:
        bool: 是否更新成功
        """
        try:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            cursor = self.conn.cursor()
            cursor.execute("""
                UPDATE grid_trades 
                SET status=?, update_time=?
                WHERE id=?
            """, (status, now, grid_id))
            
            self.conn.commit()
            
            if cursor.rowcount > 0:
                logger.info(f"更新网格交易 {grid_id} 的状态为 {status} 成功")
                return True
            else:
                logger.warning(f"未找到网格交易 {grid_id}，无法更新状态")
                return False
                
        except Exception as e:
            logger.error(f"更新网格交易 {grid_id} 的状态时出错: {str(e)}")
            self.conn.rollback()
            return False
    
    def check_grid_trade_signals(self, stock_code):
        """
        检查网格交易信号
        
        参数:
        stock_code (str): 股票代码
        
        返回:
        dict: 网格交易信号，包含 'buy_signals' 和 'sell_signals'
        """
        try:
            # 检查是否启用网格交易功能
            if not config.ENABLE_GRID_TRADING:
                logger.debug(f"{stock_code} 网格交易功能已关闭，跳过信号检查")
                return {'buy_signals': [], 'sell_signals': []}


            # 获取最新价格
            latest_quote = self.data_manager.get_latest_data(stock_code)
            if not latest_quote:
                logger.warning(f"未能获取 {stock_code} 的最新行情，无法检查网格信号")
                return {'buy_signals': [], 'sell_signals': []}
            
            current_price = latest_quote.get('lastPrice')
            
            # 获取网格交易记录
            grid_trades = self.get_grid_trades(stock_code)
            
            buy_signals = []
            sell_signals = []
            
            # 检查每个网格的买入/卖出信号
            for _, grid in grid_trades.iterrows():
                grid_id = grid['id']
                status = grid['status']
                buy_price = grid['buy_price']
                sell_price = grid['sell_price']
                volume = grid['volume']
                
                # 检查买入信号
                if status == 'PENDING' and current_price <= buy_price:
                    buy_signals.append({
                        'grid_id': grid_id,
                        'price': buy_price,
                        'volume': volume
                    })
                
                # 检查卖出信号
                if status == 'ACTIVE' and current_price >= sell_price:
                    sell_signals.append({
                        'grid_id': grid_id,
                        'price': sell_price,
                        'volume': volume
                    })
            
            signals = {
                'buy_signals': buy_signals,
                'sell_signals': sell_signals
            }
            
            if buy_signals or sell_signals:
                logger.info(f"{stock_code} 网格交易信号: 买入={len(buy_signals)}, 卖出={len(sell_signals)}")
            
            return signals
            
        except Exception as e:
            logger.error(f"检查 {stock_code} 的网格交易信号时出错: {str(e)}")
            return {'buy_signals': [], 'sell_signals': []}

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
            
            # 从配置获取补仓跌幅阈值（使用web页面的stopLossBuy参数）
            add_position_threshold = 1 - config.BUY_GRID_LEVELS[1]  # 由stopLossBuy参数控制
            
            # 检查是否达到止损线（如果下跌超过止损比例且无法补仓，应该止损而非补仓）
            stop_loss_threshold = abs(config.STOP_LOSS_RATIO)
            
            # 优先级判断：
            # 1. 如果下跌幅度达到补仓条件，且还有补仓空间 → 补仓
            # 2. 如果下跌幅度达到止损条件，且无补仓空间 → 让止损逻辑处理
            
            if price_drop_ratio >= add_position_threshold:
                # 检查是否还有补仓空间
                remaining_space = config.MAX_POSITION_VALUE - current_value
                min_add_amount = 1000  # 最小补仓金额
                
                if remaining_space >= min_add_amount:
                    # 还有补仓空间，执行补仓
                    add_amount = min(config.POSITION_UNIT, remaining_space)
                    
                    logger.info(f"{stock_code} 触发补仓条件：成本价={cost_price:.2f}, 当前价={current_price:.2f}, "
                            f"下跌={price_drop_ratio:.2%}, 补仓阈值={add_position_threshold:.2%}, "
                            f"补仓金额={add_amount:.0f}")
                    
                    return 'add_position', {
                        'stock_code': stock_code,
                        'current_price': current_price,
                        'cost_price': cost_price,
                        'add_amount': add_amount,
                        'price_drop_ratio': price_drop_ratio,
                        'threshold': add_position_threshold,
                        'current_value': current_value,
                        'remaining_space': remaining_space,
                        'reason': 'price_drop_add_position'
                    }
                else:
                    # 无补仓空间且已达到补仓条件，记录日志但不执行补仓
                    # 让后续的止损逻辑来处理
                    logger.warning(f"{stock_code} 达到补仓条件但无补仓空间：下跌={price_drop_ratio:.2%}, "
                                f"剩余空间={remaining_space:.0f}, 将由止损逻辑处理")
            
            return None, None
            
        except Exception as e:
            logger.error(f"检查 {stock_code} 补仓信号时出错: {str(e)}")
            return None, None

    # ========== 新增：统一的止盈止损检查逻辑 ==========
    
    def check_trading_signals(self, stock_code):
        """
        检查交易信号 - 修复字段映射错乱版本
        
        参数:
        stock_code (str): 股票代码
        
        返回:
        tuple: (信号类型, 详细信息) - ('stop_loss'/'take_profit_half'/'take_profit_full', {...}) 或 (None, None)
        """
        try:
            # 1. 获取持仓数据
            position = self.get_position(stock_code)
            if not position:
                logger.debug(f"未持有 {stock_code}，无需检查信号")
                return None, None
            
            # 2. 获取最新行情数据
            latest_quote = self.data_manager.get_latest_data(stock_code)
            if not latest_quote:
                latest_quote = {'lastPrice': position.get('current_price', 0)}
            
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
                                'volume': position['volume'],
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
                profit_breakout_triggered = position.get('profit_breakout_triggered', False)
                breakout_highest_price = float(position.get('breakout_highest_price', 0))
                
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
                            
                            return 'take_profit_half', {
                                'current_price': current_price,
                                'cost_price': cost_price,
                                'profit_ratio': profit_ratio,
                                'volume': position['volume'],
                                'sell_ratio': config.INITIAL_TAKE_PROFIT_RATIO_PERCENTAGE,
                                'breakout_highest_price': breakout_highest_price,
                                'pullback_ratio': pullback_ratio
                            }
            
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
                            'volume': position['volume']
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
                cost_price = signal_info.get('cost_price', 0)
                
                if current_price <= 0 or cost_price <= 0:
                    logger.error(f"🚨 {stock_code} 止盈信号数据无效，拒绝执行")
                    return False
                
                # 确保是盈利状态
                if current_price <= cost_price:
                    logger.error(f"🚨 {stock_code} 止盈信号但当前亏损，拒绝执行")
                    return False
                
                logger.info(f"✅ {stock_code} 止盈信号验证通过")
            
            return True
            
        except Exception as e:
            logger.error(f"🚨 {stock_code} 信号验证失败: {e}")
            return False

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
            available = int(position.get('available', volume))
            profit_triggered = bool(position.get('profit_triggered', False))
            open_date = position.get('open_date')
            stock_name = position.get('stock_name')
            
            # 3. 计算/更新最高价（重要：基于历史数据重新计算）
            updated_highest_price = self._calculate_highest_price_since_open(stock_code, open_date, current_price)
            
            # 4. 重新计算所有衍生数据
            market_value = round(volume * current_price, 2)
            profit_ratio = round(100 * (current_price - cost_price) / cost_price, 2) if cost_price > 0 else 0.0
            
            # 5. 重新计算动态止损价格
            stop_loss_price = self.calculate_stop_loss_price(cost_price, updated_highest_price, profit_triggered)
            
            # 6. 执行数据库更新
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
            # 更新内存数据库
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
            # 更新内存数据库
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
            cursor = self.memory_conn.cursor()
            cursor.execute("UPDATE positions SET profit_triggered = ? WHERE stock_code = ?", (True, stock_code))
            self.memory_conn.commit()
            logger.info(f"已标记 {stock_code} profit_triggered已标记为True")
            return True
        except Exception as e:
            logger.error(f"标记 {stock_code} profit_triggered时出错: {str(e)}")
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
        """持仓监控循环 - 优化版本，使用统一的信号检查"""
        while not self.stop_flag:
            try:
                # 判断是否在交易时间
                if config.is_trade_time():

                    # 首先更新所有持仓的最高价
                    self.update_all_positions_highest_price()

                    # 一次性获取所有持仓数据
                    positions_df = self.get_all_positions()
                    
                    if positions_df.empty:
                        logger.debug("当前没有持仓，无需监控")
                        time.sleep(60)
                        continue
                    
                    # 处理所有持仓
                    for _, position_row in positions_df.iterrows():
                        stock_code = position_row['stock_code']
                        
                        # 使用统一的信号检查函数
                        signal_type, signal_info = self.check_trading_signals(stock_code)
                        
                        with self.signal_lock:
                            if signal_type:
                                self.latest_signals[stock_code] = {
                                    'type': signal_type,
                                    'info': signal_info,
                                    'timestamp': datetime.now()
                                }
                                logger.debug(f"{stock_code} 检测到信号: {signal_type}，等待策略处理")
                            else:
                                # 清除已不存在的信号
                                self.latest_signals.pop(stock_code, None)
                        
                        # 更新最高价（如果当前价格更高）
                        try:
                            latest_quote = self.data_manager.get_latest_data(stock_code)
                            if latest_quote:
                                current_price = float(latest_quote.get('lastPrice', 0))
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
                    
                    # 等待下一次监控
                    for _ in range(5):  # 每5s检查一次
                        if self.stop_flag:
                            break
                        time.sleep(2)
                        
            except Exception as e:
                logger.error(f"持仓监控循环出错: {str(e)}")
                time.sleep(60)  # 出错后等待一分钟再继续


# 单例模式
_instance = None

def get_position_manager():
    """获取PositionManager单例"""
    global _instance
    if _instance is None:
        _instance = PositionManager()
    return _instance