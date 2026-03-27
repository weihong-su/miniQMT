"""
数据管理模块，负责历史数据的获取与存储
"""
import os
import pandas as pd
import sqlite3
import time
from datetime import datetime, timedelta
import threading
import xtquant.xtdata as xt
import Methods
import config
from logger import get_logger, suppress_stdout_stderr
# from realtime_data_manager import get_realtime_data_manager

# 获取logger
logger = get_logger("data_manager")


def _create_xtdata():
    """
    工厂函数：根据 config.ENABLE_XTQUANT_MANAGER 返回行情接口对象。

    Returns:
        XtDataAdapter: ENABLE_XTQUANT_MANAGER=True 时，返回 HTTP 适配器
        xtquant.xtdata module: ENABLE_XTQUANT_MANAGER=False 时，返回原始模块
    """
    if getattr(config, "ENABLE_XTQUANT_MANAGER", False):
        from xtquant_manager.client import XtQuantClient, ClientConfig, XtDataAdapter
        account_config = config.get_account_config()
        client = XtQuantClient(
            config=ClientConfig(
                base_url=getattr(config, "XTQUANT_MANAGER_URL", "http://127.0.0.1:8888"),
                account_id=account_config.get("account_id", ""),
                api_token=getattr(config, "XTQUANT_MANAGER_TOKEN", ""),
            )
        )
        return XtDataAdapter(client)
    else:
        import xtquant.xtdata as _xtdata
        return _xtdata

class DataManager:
    """数据管理类，处理历史行情数据的获取与存储"""
    
    def __init__(self):
        """初始化数据管理器"""
        # 创建数据目录
        if not os.path.exists(config.DATA_DIR):
            os.makedirs(config.DATA_DIR)
            
        # 连接数据库
        self.conn = self._connect_db()
        # 数据库操作锁，防止多线程并发访问同一连接导致事务冲突
        self._db_lock = threading.Lock()

        # 创建表结构
        self._create_tables()
        
        # 已订阅的股票代码列表
        self.subscribed_stocks = []

        # xtdata断连自动重连控制
        self._xtdata_reconnect_lock = threading.Lock()
        self._xtdata_last_reconnect_time = 0.0
        self._xtdata_reconnect_errors = 0
        self._xtdata_reconnect_threshold = getattr(config, "XTQUANT_DATA_RECONNECT_ON_ERRORS", 3)
        self._xtdata_reconnect_interval = getattr(config, "XTQUANT_DATA_RECONNECT_INTERVAL", 60)
        
        # # 初始化行情接口 
        self._init_xtquant()
        # self.realtime_manager = get_realtime_data_manager()        

        # 数据更新线程
        self.update_thread = None
        self.stop_flag = False

    def _init_xtquant(self):
        """初始化行情接口（根据 ENABLE_XTQUANT_MANAGER 选择本地或 HTTP 适配器）"""
        try:
            self.xt = _create_xtdata()

            if self.xt.connect():
                logger.info("行情服务连接成功")
            else:
                logger.error("行情服务连接失败")
                self.xt = None
                return

            # 验证连接状态
            self._verify_connection()

            # 订阅股票池，确保交易时段 get_full_tick 返回实时价格
            self._subscribe_stocks_to_xtdata(config.STOCK_POOL)

        except Exception as e:
            logger.error(f"初始化行情接口出错: {str(e)}")
            self.xt = None

    def _subscribe_stocks_to_xtdata(self, stock_list):
        """订阅股票池到 xtdata，保证交易时段 get_full_tick 返回实时推送价格。
        subscribe_quote(count=0) 表示只订阅实时推送，不拉取历史数据。
        """
        if not self.xt or not stock_list:
            return
        ok, fail = 0, 0
        for stock in stock_list:
            code = self._adjust_stock(stock)
            try:
                self.xt.subscribe_quote(
                    code,
                    period='tick',
                    start_time='',
                    end_time='',
                    count=0,
                    callback=None,
                )
                if code not in self.subscribed_stocks:
                    self.subscribed_stocks.append(code)
                ok += 1
            except Exception as e:
                logger.warning(f"xtdata subscribe_quote 失败: {code} - {e}")
                fail += 1
        logger.info(f"xtdata 订阅股票池完成: 成功 {ok} 只，失败 {fail} 只，共 {ok+fail} 只")

    def ensure_subscribed(self, stock_code):
        """确保股票已订阅到 xtdata 实时推送。
        盘中新持仓加入时调用，保证下一个心跳就能拿到实时价格。
        已订阅则直接返回；未订阅则立即订阅并追踪到 subscribed_stocks。
        """
        if not self.xt:
            return
        code = self._adjust_stock(stock_code)
        if code in self.subscribed_stocks:
            return  # 已订阅，无需重复操作
        try:
            self.xt.subscribe_quote(
                code,
                period='tick',
                start_time='',
                end_time='',
                count=0,
                callback=None,
            )
            self.subscribed_stocks.append(code)
            logger.info(f"xtdata 动态订阅新股票: {code}")
        except Exception as e:
            logger.warning(f"xtdata 动态订阅失败: {code} - {e}")

    def _verify_connection(self):
        """验证连接状态"""
        try:
            # 使用一个简单的测试来验证连接
            test_codes = ['000001.SZ']  # 测试股票
            test_data = self.xt.get_full_tick(test_codes)
            if test_data:
                logger.debug("xtquant连接状态验证成功")
                return True
            else:
                logger.warning("xtquant连接状态验证失败")
                return False
        except Exception as e:
            logger.warning(f"xtquant连接验证出错: {str(e)}")
            return False

    def _record_xtdata_failure(self, reason=""):
        """记录xtdata失败并按阈值触发重连"""
        self._xtdata_reconnect_errors += 1
        if self._xtdata_reconnect_errors >= self._xtdata_reconnect_threshold:
            if self._attempt_xtdata_reconnect(reason):
                self._xtdata_reconnect_errors = 0

    def _reset_xtdata_failure(self):
        """重置xtdata失败计数"""
        self._xtdata_reconnect_errors = 0

    def _attempt_xtdata_reconnect(self, reason=""):
        """尝试重连xtdata并恢复订阅"""
        if not self.xt:
            return False

        with self._xtdata_reconnect_lock:
            now = time.time()
            if now - self._xtdata_last_reconnect_time < self._xtdata_reconnect_interval:
                return False
            self._xtdata_last_reconnect_time = now

        try:
            reconnect_ok = False
            if hasattr(self.xt, "reconnect"):
                reconnect_ok = self.xt.reconnect()
            elif hasattr(self.xt, "connect"):
                reconnect_ok = self.xt.connect()

            if reconnect_ok:
                # 复订阅：优先使用已订阅列表，否则用股票池
                stocks = list(dict.fromkeys(self.subscribed_stocks)) if self.subscribed_stocks else list(config.STOCK_POOL)
                if stocks:
                    self._subscribe_stocks_to_xtdata(stocks)
                return True
            return False
        except Exception as e:
            logger.warning(f"xtdata重连失败: {e}")
            return False

    def _connect_db(self):
        """连接SQLite数据库"""
        try:
            # ⭐ 超时优化：添加30秒超时和WAL模式
            conn = sqlite3.connect(
                config.DB_PATH,
                timeout=30.0,  # 30秒超时（默认5秒）
                check_same_thread=False
            )
            # 启用WAL模式，允许读写并发（减少锁冲突）
            conn.execute('PRAGMA journal_mode=WAL')
            logger.info(f"已连接数据库: {config.DB_PATH}")
            return conn
        except Exception as e:
            logger.error(f"连接数据库失败: {str(e)}")
            raise
    
    def _create_tables(self):
        """创建数据表结构"""
        cursor = self.conn.cursor()
        
        # 创建股票历史数据表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS stock_daily_data (
            stock_code TEXT,
            stock_name TEXT,            
            date TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            amount REAL,
            PRIMARY KEY (stock_code, date)
        )
        ''')
        
        # 创建指标数据表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS stock_indicators (
            stock_code TEXT,
            date TEXT,
            ma10 REAL,
            ma20 REAL,
            ma30 REAL,
            ma60 REAL,
            macd REAL,
            macd_signal REAL,
            macd_hist REAL,
            PRIMARY KEY (stock_code, date)
        )
        ''')
        
        # 创建交易记录表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS trade_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_code TEXT,
            stock_name TEXT,            
            trade_time TIMESTAMP,
            trade_type TEXT,  -- BUY, SELL
            price REAL,
            volume INTEGER,
            amount REAL,
            trade_id TEXT,
            commission REAL,
            strategy TEXT
        )
        ''')
        
        # 创建持仓表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS positions (
            stock_code TEXT PRIMARY KEY,
            stock_name TEXT,            
            volume INTEGER,
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
            stop_loss_price REAL                      
        )
        ''')
        
        # 创建网格交易表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS grid_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_code TEXT,
            grid_level INTEGER,
            buy_price REAL,
            sell_price REAL,
            volume INTEGER,
            status TEXT,  -- PENDING, ACTIVE, COMPLETED
            create_time TIMESTAMP,
            update_time TIMESTAMP
        )
        ''')
        
        self.conn.commit()
        logger.info("数据表结构已创建")

        # 启动时修复可能损坏的索引（机器强制重启后可能出现索引损坏）
        self._repair_indexes_if_needed()

    def _repair_indexes_if_needed(self):
        """检查并修复损坏的数据库索引（快速完整性检查）"""
        try:
            result = self.conn.execute("PRAGMA quick_check").fetchone()
            if result and result[0] != 'ok':
                logger.warning(f"检测到数据库索引异常: {result[0]}，执行 REINDEX 修复...")
                self.conn.execute("REINDEX")
                self.conn.commit()
                # 再次验证
                result2 = self.conn.execute("PRAGMA quick_check").fetchone()
                if result2 and result2[0] == 'ok':
                    logger.info("数据库索引修复成功")
                else:
                    logger.error(f"数据库索引修复后仍有异常: {result2[0] if result2 else '未知'}")
            else:
                logger.debug("数据库索引完整性检查通过")
        except Exception as e:
            logger.warning(f"数据库索引检查失败（不影响运行）: {str(e)}")
    
    # def _init_xtquant(self):
    #     """初始化迅投行情接口"""
    #     try:
    #         # 根据文档，首先调用connect连接到行情服务器
    #         if not xt.connect():
    #             logger.error("行情服务连接失败")
    #             return
                
    #         logger.info("行情服务连接成功")
            
    #         # 根据测试结果，我们不使用subscribe_quote方法（会失败）
    #         # 改为验证股票代码是否可以通过get_full_tick获取数据
    #         valid_stocks = []
    #         for stock_code in config.STOCK_POOL:
    #             try:
    #                 stock_code = self._adjust_stock(stock_code)
    #                 # 尝试adjust_stock(stock_code)
    #                 # 尝试获取Tick数据验证股票代码有效性
    #                 tick_data = xt.get_full_tick([stock_code])
    #                 if tick_data and stock_code in tick_data:
    #                     valid_stocks.append(stock_code)
    #                     logger.info(f"股票 {stock_code} 数据获取成功")
    #                 else:
    #                     logger.warning(f"无法获取 {stock_code} 的Tick数据")
    #             except Exception as e:
    #                 logger.warning(f"获取 {stock_code} 的Tick数据失败: {str(e)}")
            
    #         self.subscribed_stocks = valid_stocks
            
    #         if self.subscribed_stocks:
    #             logger.info(f"成功验证 {len(self.subscribed_stocks)} 只股票可获取数据")
    #         else:
    #             logger.warning("没有有效的股票，请检查股票代码格式")
                
    #     except Exception as e:
    #         logger.error(f"初始化迅投行情接口出错: {str(e)}")

    # # 股票代码转换
    # def _select_data_type(self, stock='600031'):
    #     '''
    #     选择数据类型
    #     '''
    #     return Methods.select_data_type(stock)
    
    def _adjust_stock(self, stock='600031.SH'):
        '''
        调整代码
        '''
        return Methods.add_xt_suffix(stock)

    def download_history_data(self, stock_code, period=None, start_date=None, end_date=None):
        """
        下载股票历史数据 (使用Mootdx)
        
        参数:
        stock_code (str): 股票代码
        period (str): 周期，默认为日线 'day'
        start_date (str): 开始日期，格式为'2022-01-01'
        end_date (str): 结束日期，格式为'2022-01-01'
        
        返回:
        pandas.DataFrame: 历史数据，若失败则返回None
        """
        try:
            import Methods  # Import the Methods module

            # Determine frequency code for Mootdx
            if period == 'day':
                freq = 9  # 日线
            elif period == 'week':
                freq = 5  # 周线
            elif period == 'mon':
                freq = 6  # 月线
            elif period == '5m':
                freq = 0  # 5分钟
            elif period == '15m':
                freq = 1  # 15分钟
            elif period == '30m':
                freq = 2  # 30分钟
            elif period == '1h':
                freq = 3  # 小时线
            else:
                freq = 9  # Default to 日线

            # Adjust stock code if necessary
            if stock_code.endswith((".SH", ".SZ")):
                stock_code = stock_code[:-3]  # Remove suffix

            # ⭐ 超时优化：为Mootdx调用添加超时保护
            # 注意：不使用 with 语句，避免 __exit__ 调用 shutdown(wait=True) 阻塞等待超时线程
            import concurrent.futures

            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(
                Methods.getStockData,
                code=stock_code,
                offset=60,
                freq=freq,
                adjustflag='qfq'  # 前复权
            )
            executor.shutdown(wait=False)  # 不等待线程，让其在后台独立结束
            try:
                df = future.result(timeout=10.0)  # 10秒超时（历史数据可以稍长）
            except concurrent.futures.TimeoutError:
                logger.warning(f"Mootdx: 下载 {stock_code} 历史数据超时（10秒）")
                return None
            except RuntimeError as e:
                # 捕获"cannot schedule new futures after interpreter shutdown"错误
                if "interpreter shutdown" in str(e).lower() or "shutdown" in str(e).lower():
                    logger.debug(f"[DATA] 解释器正在关闭，跳过下载 {stock_code} 历史数据")
                    return None
                raise

            if df is None or df.empty:
                logger.warning(f"使用Mootdx获取 {stock_code} 的历史数据为空")
                return None

            # Rename columns to match expected format
            df = df.rename(columns={
                'datetime': 'date',
                'open': 'open',
                'high': 'high',
                'low': 'low',
                'close': 'close',
                'volume': 'volume',
                'amount': 'amount'
            })

            # Ensure date column is in the correct format
            if 'date' in df.columns:
                df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')

            # Ensure 'close' column is numeric
            df['close'] = pd.to_numeric(df['close'], errors='coerce')

            # Add stock_code column
            df['stock_code'] = stock_code

            # logger.info(f"成功使用Mootdx获取 {stock_code} 的历史数据, 共 {len(df)} 条记录")
            return df

        except Exception as e:
            # 区分正常关闭错误和真正的错误
            error_str = str(e).lower()
            if "interpreter shutdown" in error_str or "cannot schedule" in error_str:
                logger.debug(f"[DATA] 系统正在关闭，跳过下载 {stock_code} 历史数据")
                return None

            # 检查是否是长度不匹配错误
            if "Length mismatch" in str(e):
                logger.warning(f"下载 {stock_code} 数据时发生长度不匹配错误，使用默认数据")
                
                # 创建包含默认值的DataFrame
                df = pd.DataFrame({
                    'date': [datetime.now().strftime('%Y-%m-%d')],
                    'open': [0.0],
                    'high': [0.0],
                    'low': [0.0],
                    'close': [0.0],
                    'volume': [0],
                    'amount': [0],
                    'stock_code': [stock_code]
                })
            else:
                # 其他错误，记录并返回None
                logger.error(f"下载 {stock_code} 的历史数据时出错: {str(e)}")
                return None


    def download_history_xtdata(self, stock_code, period=None, start_date=None, end_date=None):
        """
        下载股票历史数据

        参数:
        stock_code (str): 股票代码
        period (str): 周期，默认为日线 '1d'
        start_date (str): 开始日期，格式为'20220101'
        end_date (str): 结束日期，格式为'20220101'

        返回:
        pandas.DataFrame: 历史数据，若失败则返回None
        """
        if not period:
            period = '1d'  # 修复bug: xtquant API要求使用'1d'而非'day'
            
        if not start_date:
            start_date = '20200101'  # 默认从2020年开始
        
        if not end_date:
            # 默认到今天
            end_date = datetime.now().strftime('%Y%m%d')
        
        logger.info(f"下载 {stock_code} 的历史数据, 周期: {period}, 从 {start_date} 到 {end_date}")

        if not self.xt:
            logger.debug(f"xtdata未连接，跳过下载 {stock_code} 历史数据")
            return None

        try:
            # ⭐ 超时优化：为xtquant API调用添加超时保护
            import concurrent.futures

            # 首先使用XtQuant API下载数据到本地
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(
                self.xt.download_history_data,
                stock_code,
                period=period,
                start_time=start_date,
                end_time=end_date,
                incrementally=True  # 使用增量下载
            )
            executor.shutdown(wait=False)  # 不等待线程，让其在后台独立结束
            try:
                future.result(timeout=15.0)  # 15秒超时
            except concurrent.futures.TimeoutError:
                logger.warning(f"xtquant: 下载 {stock_code} 历史数据超时（15秒）")
                return None
            except RuntimeError as e:
                if "interpreter shutdown" in str(e).lower() or "shutdown" in str(e).lower():
                    logger.debug(f"[DATA] 解释器正在关闭，跳过下载 {stock_code} 历史数据")
                    return None
                raise

            # 等待数据下载完成
            time.sleep(0.5)

            # 使用get_market_data_ex从本地获取下载的数据
            # 注意第一个参数是字段列表，可以为空
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(
                self.xt.get_market_data_ex,
                [],  # 空字段列表表示获取所有可用字段
                [stock_code],
                period=period,
                start_time=start_date,
                end_time=end_date
            )
            executor.shutdown(wait=False)  # 不等待线程，让其在后台独立结束
            try:
                result = future.result(timeout=10.0)  # 10秒超时
            except concurrent.futures.TimeoutError:
                logger.warning(f"xtquant: 获取 {stock_code} 历史数据超时（10秒）")
                return None
            except RuntimeError as e:
                if "interpreter shutdown" in str(e).lower() or "shutdown" in str(e).lower():
                    logger.debug(f"[DATA] 解释器正在关闭，跳过获取 {stock_code} 历史数据")
                    return None
                raise
            
            if not result:
                logger.warning(f"获取 {stock_code} 的历史数据为空")
                return None
                
            if stock_code in result:
                stock_data = result[stock_code]
                df = pd.DataFrame(stock_data)
            else:
                logger.warning(f"获取的数据中没有 {stock_code}, 可用的键: {list(result.keys())}")
                if result:
                    first_key = list(result.keys())[0]
                    stock_data = result[first_key]
                    df = pd.DataFrame(stock_data)
                else:
                    return None
            
            # 确保日期列格式正确
            if 'date' in df.columns:
                try:
                    df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
                except Exception as e:
                    logger.warning(f"转换日期格式失败: {str(e)}")
            elif 'time' in df.columns:
                try:
                    df = df.rename(columns={'time': 'date'})
                    df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
                except Exception as e:
                    logger.warning(f"转换time列为日期格式失败: {str(e)}")
            
            if not df.empty:
                logger.info(f"成功下载 {stock_code} 的历史数据, 共 {len(df)} 条记录")
                return df
            else:
                logger.warning(f"下载的 {stock_code} 数据为空")
                return None
            
        except Exception as e:
            # 区分正常关闭错误和真正的错误
            error_str = str(e).lower()
            if "interpreter shutdown" in error_str or "cannot schedule" in error_str:
                logger.debug(f"[DATA] 系统正在关闭，跳过下载 {stock_code} 历史数据")
                return None
            logger.error(f"下载 {stock_code} 的历史数据时出错: {str(e)}")
            return None
        
    def get_stock_name(self, stock_code):
        """
        获取股票名称
        
        参数:
        stock_code (str): 股票代码
        
        返回:
        str: 股票名称，如果未找到则返回股票代码
        """
        try:
            # 初始化名称缓存（如果不存在）
            if not hasattr(self, 'stock_names_cache'):
                self.stock_names_cache = {}
                
            # 尝试从缓存获取名称
            if stock_code in self.stock_names_cache:
                return self.stock_names_cache[stock_code]
            
            # 从QMT交易接口获取（仅实盘模式）
            try:
                # 模拟模式下跳过实盘API调用
                if hasattr(config, 'ENABLE_SIMULATION_MODE') and config.ENABLE_SIMULATION_MODE:
                    raise Exception("模拟模式，跳过QMT API")

                from position_manager import get_position_manager
                position_manager = get_position_manager()

                if hasattr(position_manager, 'qmt_trader') and position_manager.qmt_trader:
                    positions_df = position_manager.qmt_trader.position()
                    if not positions_df.empty and '证券代码' in positions_df.columns and '证券名称' in positions_df.columns:
                        # 简化股票代码以匹配
                        stock_code_simple = stock_code.split('.')[0] if '.' in stock_code else stock_code
                        stock_info = positions_df[positions_df['证券代码'] == stock_code_simple]
                        if not stock_info.empty:
                            stock_name = stock_info.iloc[0]['证券名称']
                            # 保存到缓存
                            self.stock_names_cache[stock_code] = stock_name
                            return stock_name
            except Exception as e:
                logger.debug(f"通过qmt_trader获取股票名称出错: {str(e)}")

            # 尝试使用baostock查询
            try:
                import baostock as bs
                with suppress_stdout_stderr():
                    lg = bs.login()
                if lg.error_code != '0':
                    logger.warning(f"baostock登录失败: {lg.error_msg}")
                    return stock_code
                
                # 调整股票代码格式
                if '.' in stock_code:
                    formatted_code = stock_code  # 假设已经是bs格式
                else:
                    # 转换为baostock格式
                    if stock_code.startswith(('600', '601', '603', '688', '510')):
                        formatted_code = f"sh.{stock_code}"
                    else:
                        formatted_code = f"sz.{stock_code}"
                
                # 查询股票基本信息
                rs = bs.query_stock_basic(code=formatted_code)
                if rs.error_code != '0':
                    logger.warning(f"查询股票基本信息失败: {rs.error_msg}")
                    bs.logout()
                    return stock_code
                
                # 获取结果
                data_list = []
                while (rs.error_code == '0') & rs.next():
                    data_list.append(rs.get_row_data())
                bs.logout()
                
                if data_list:
                    # 股票名称通常是结果的第二列
                    stock_name = data_list[0][1] if len(data_list[0]) > 1 else stock_code
                    
                    # 保存到缓存
                    self.stock_names_cache[stock_code] = stock_name
                    return stock_name
                
                return stock_code
                
            except ImportError:
                logger.warning("未安装baostock，无法获取股票名称")
                return stock_code
            except Exception as e:
                logger.error(f"获取股票 {stock_code} 名称时出错: {str(e)}")
                return stock_code
                
        except Exception as e:
            logger.error(f"获取股票 {stock_code} 名称时出错: {str(e)}")
            return stock_code
    
    def save_history_data(self, stock_code, data_df):
        """
        保存历史数据到数据库
        
        参数:
        stock_code (str): 股票代码
        data_df (pandas.DataFrame): 历史数据
        """
        if data_df is None or data_df.empty:
            logger.warning(f"没有 {stock_code} 的数据可保存")
            return
        
        try:
            # 立即创建工作副本
            work_df = data_df.copy()
            
            # 数据验证和清理
            required_columns = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount']
            missing_cols = [col for col in required_columns if col not in work_df.columns]
            if missing_cols:
                logger.error(f"{stock_code} 缺少必要列: {missing_cols}")
                return
            
            # 清理空数据
            initial_count = len(work_df)
            work_df = work_df.dropna(subset=['date'])
            final_count = len(work_df)
            
            if initial_count != final_count:
                logger.warning(f"{stock_code} 过滤了 {initial_count - final_count} 行空date数据")
            
            if work_df.empty:
                logger.warning(f"{stock_code} 无有效数据可保存")
                return
            
            # 数据处理
            work_df['stock_code'] = stock_code
            for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
                work_df[col] = pd.to_numeric(work_df[col], errors='coerce')

            # 方案A优化：使用逐行REPLACE避免主键冲突
            # 相比DELETE+INSERT，REPLACE在并发场景下更安全

            # 准备数据
            data_to_insert = list(zip(
                work_df['stock_code'].tolist(),
                work_df['date'].tolist(),
                work_df['open'].tolist(),
                work_df['high'].tolist(),
                work_df['low'].tolist(),
                work_df['close'].tolist(),
                work_df['volume'].tolist(),
                work_df['amount'].tolist()
            ))

            # 使用REPLACE INTO语句（SQLite特性，自动处理主键冲突）
            # _db_lock 保证多线程不并发访问 self.conn，避免隐式事务冲突
            with self._db_lock:
                with self.conn:
                    self.conn.executemany('''
                        REPLACE INTO stock_daily_data
                        (stock_code, date, open, high, low, close, volume, amount)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', data_to_insert)

            logger.debug(f"已保存 {stock_code} 的历史数据到数据库, 共 {len(data_to_insert)} 条记录（使用REPLACE模式避免主键冲突）")

        except Exception as e:
            logger.error(f"保存 {stock_code} 的历史数据时出错: {str(e)}")


    def get_latest_data(self, stock_code):
        """
        获取最新行情数据 (使用Mootdx)
        
        参数:
        stock_code (str): 股票代码
        
        返回:
        dict: 最新行情数据
        """
        try:
            # 在交易时间内，优先使用实时数据管理器
            if config.is_trade_time():
                # # 添加频率控制，避免过于频繁调用
                # if not hasattr(self, '_last_realtime_call_time'):
                #     self._last_realtime_call_time = {}
                
                # current_time = time.time()
                # last_call_time = self._last_realtime_call_time.get(stock_code, 0)
                
                # # 限制调用频率：每只股票最多每秒调用一次
                # if current_time - last_call_time >= 1.0:
                #     self._last_realtime_call_time[stock_code] = current_time
                    
                try:
                    # realtime_data = self.realtime_manager.get_realtime_data(stock_code)
                    realtime_data = self.get_latest_xtdata(stock_code)
                    if realtime_data and realtime_data.get('lastPrice', 0) > 0:
                        logger.debug(f"XT获取 {stock_code} 实时数据 {realtime_data.get('lastPrice')}")
                        return realtime_data
                    if realtime_data:
                        # 有数据但 lastPrice=0：判断是否已订阅
                        code = self._adjust_stock(stock_code)
                        if code in self.subscribed_stocks:
                            # 已订阅但价格为0，属于异常（推送延迟或连接不稳定），降级并警告
                            logger.warning(f"xtdata: {stock_code} 已订阅但 lastPrice=0，降级到Mootdx")
                        else:
                            # 未订阅（盘中新增持仓的典型情况），立即触发订阅，本次降级
                            self.ensure_subscribed(stock_code)
                            logger.info(f"xtdata: {stock_code} 未订阅(lastPrice=0)，已触发订阅，本次降级到Mootdx")
                    else:
                        # 空 dict：xt 连接超时或股票代码不存在，静默降级
                        logger.debug(f"xtdata: {stock_code} 无数据，降级到Mootdx")
                except Exception as e:
                    logger.debug(f"实时数据管理器获取{stock_code}失败，降级到Mootdx: {str(e)}")
                    
            # 非交易时段：优先尝试 xtdata get_full_tick（盘后返回收盘快照，lastClose 可靠）
            # 这样可以正确计算涨跌幅，不依赖 Mootdx 的 offset=2 返回行数是否足够
            if not config.is_trade_time() and self.xt:
                try:
                    xt_data = self.get_latest_xtdata(stock_code)
                    if xt_data and xt_data.get('lastPrice', 0) > 0 and xt_data.get('lastClose', 0) > 0:
                        logger.debug(f"非交易时段 xtdata 快照: {stock_code} lastPrice={xt_data.get('lastPrice')} lastClose={xt_data.get('lastClose')}")
                        return xt_data
                except Exception as e:
                    logger.debug(f"非交易时段 xtdata 获取失败，降级到Mootdx: {e}")

            # 继续尝试从Mootdx获取数据
            # Adjust stock code if necessary
            if stock_code.endswith((".SH", ".SZ")):
                stock_code = stock_code[:-3]  # Remove suffix

            # ⭐ 超时优化：为Mootdx降级路径添加超时保护
            # 注意：不使用 with 语句，避免 __exit__ 调用 shutdown(wait=True) 阻塞等待超时线程
            import concurrent.futures

            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(
                Methods.getStockData,
                code=stock_code,
                offset=2,  # Get only the latest data
                freq=9,  # 日线
                adjustflag='qfq'
            )
            executor.shutdown(wait=False)  # 不等待线程，让其在后台独立结束
            try:
                df = future.result(timeout=5.0)  # 5秒超时
            except concurrent.futures.TimeoutError:
                logger.warning(f"Mootdx: 获取 {stock_code} 行情超时（5秒）")
                return None
            except RuntimeError as e:
                # 捕获"cannot schedule new futures after interpreter shutdown"错误
                if "interpreter shutdown" in str(e).lower() or "shutdown" in str(e).lower():
                    logger.debug(f"[DATA] 解释器正在关闭，跳过Mootdx获取 {stock_code} 行情")
                    return None
                raise

            if df is None or df.empty:
                logger.warning(f"使用Mootdx获取 {stock_code} 的最新行情为空")
                return None

            if len(df) < 2:
                logger.warning(f"Mootdx: {stock_code} 数据行数不足({len(df)}行)，无法计算lastClose")
                return None

            # Extract the latest data
            latest_data = df.iloc[-1].to_dict()
            lastday_data = df.iloc[-2].to_dict()

            # Rename columns to match expected format
            latest_data = {
                'lastPrice': float(latest_data.get('close', 0)),
                'lastClose': float(lastday_data.get('close', 0)),
                'volume': float(latest_data.get('volume', 0)),
                'amount': float(latest_data.get('amount', 0)),
                'date': latest_data.get('datetime', None)
            }


            logger.debug(f"Mootdx:{stock_code} 最新行情: {latest_data}")
            return latest_data

        except Exception as e:
            # 区分正常关闭错误和真正的错误
            error_str = str(e).lower()
            if "interpreter shutdown" in error_str or "cannot schedule" in error_str:
                logger.debug(f"[DATA] 系统正在关闭，跳过获取 {stock_code} 行情")
                return None
            logger.error(f"获取 {stock_code} 的latest_data出错: {str(e)}")
            return None


    def get_latest_xtdata(self, stock_code):
        """获取最新行情数据"""
        stock_code = self._adjust_stock(stock_code)

        if not self.xt:
            return {}

        try:
            # ⚠️ 检测Python解释器是否正在关闭
            # sys.is_finalizing 是函数对象（truthy），必须调用才能获取布尔值
            import sys
            if hasattr(sys, 'is_finalizing') and sys.is_finalizing():
                logger.debug(f"[DATA] 检测到解释器正在关闭，跳过获取 {stock_code} 行情")
                return {}

            # ⭐ 超时优化：添加超时保护
            # 注意：不使用 with 语句，避免 __exit__ 调用 shutdown(wait=True) 阻塞等待超时线程
            import concurrent.futures

            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(self.xt.get_full_tick, [stock_code])
            executor.shutdown(wait=False)  # 不等待线程，让其在后台独立结束
            try:
                latest_quote = future.result(timeout=3.0)  # 3秒超时
            except concurrent.futures.TimeoutError:
                logger.warning(f"xtdata: 获取 {stock_code} 行情超时（3秒）")
                self._record_xtdata_failure("timeout")
                return {}  # 返回空字典，与原逻辑一致
            except RuntimeError as e:
                # 捕获"cannot schedule new futures after interpreter shutdown"错误
                if "interpreter shutdown" in str(e).lower() or "shutdown" in str(e).lower():
                    logger.debug(f"[DATA] 解释器正在关闭，跳过获取 {stock_code} 行情")
                    return {}
                raise

            if not latest_quote or stock_code not in latest_quote:
                # 交易时段返回空是异常（WARNING）；非交易时段是正常行为（DEBUG），避免盘前/盘后刷屏
                if config.is_trade_time():
                    logger.warning(f"xtdata:未获取到 {stock_code} 的tick行情，返回值: {latest_quote}")
                else:
                    logger.debug(f"xtdata:未获取到 {stock_code} 的tick行情（非交易时段）")
                self._record_xtdata_failure("empty_quote")
                return {}  # 返回空字典而不是None

            quote_data = latest_quote[stock_code]
            logger.debug(f"xtdata: {stock_code} 最新行情: {quote_data}")
            self._reset_xtdata_failure()
            return quote_data

        except Exception as e:
            # 区分正常关闭错误和真正的错误
            error_str = str(e).lower()
            if "interpreter shutdown" in error_str or "cannot schedule" in error_str:
                logger.debug(f"[DATA] 系统正在关闭，跳过获取 {stock_code} 行情")
                return {}
            logger.error(f"xtdata: 获取 {stock_code} 的最新行情时出错: {str(e)}", exc_info=True)
            self._record_xtdata_failure("exception")
            return {}  # 返回空字典而不是None

    def get_history_data_from_db(self, stock_code, start_date=None, end_date=None):
        """
        从数据库获取历史数据

        参数:
        stock_code (str): 股票代码
        start_date (str): 开始日期，如 '2021-01-01'
        end_date (str): 结束日期，如 '2021-03-31'

        返回:
        pandas.DataFrame: 历史数据
        """
        query = "SELECT * FROM stock_daily_data WHERE stock_code=?"
        params = [stock_code]

        if start_date:
            query += " AND date>=?"
            params.append(start_date)

        if end_date:
            query += " AND date<=?"
            params.append(end_date)

        query += " ORDER BY date"

        try:
            with self._db_lock:
                df = pd.read_sql_query(query, self.conn, params=params)
            logger.debug(f"从数据库获取 {stock_code} 的历史数据, 共 {len(df)} 条记录")
            return df
        except Exception as e:
            logger.error(f"从数据库获取 {stock_code} 的历史数据时出错: {str(e)}")
            return pd.DataFrame()

    
    def update_all_stock_data(self):
        """更新所有股票的历史数据"""
        for stock_code in config.STOCK_POOL:
            self.update_stock_data(stock_code)
            # 避免请求过于频繁
            time.sleep(1)
    
    def update_stock_data(self, stock_code):
        """
        更新单只股票的数据
        
        参数:
        stock_code (str): 股票代码
        """
        # 从数据库获取最新的数据日期
        latest_date_query = "SELECT MAX(date) FROM stock_daily_data WHERE stock_code=?"
        with self._db_lock:
            cursor = self.conn.cursor()
            cursor.execute(latest_date_query, (stock_code,))
            result = cursor.fetchone()
        
        if result and result[0]:
            latest_date = result[0]
            # 从最新日期的下一天开始获取
            start_date = (datetime.strptime(latest_date, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y%m%d')
            # logger.info(f"更新 {stock_code} 的数据，从 {start_date} 开始")
        else:
            # 如果没有历史数据，获取完整的历史数据
            start_date = None
            logger.info(f"获取 {stock_code} 的完整历史数据")
        
        # 下载并保存数据
        data_df = self.download_history_data(stock_code, start_date=start_date)
        if data_df is not None and not data_df.empty:
            self.save_history_data(stock_code, data_df)
    
    def start_data_update_thread(self):
        """启动数据更新线程"""
        if not config.ENABLE_DATA_SYNC:
            logger.info("数据同步功能已关闭，不启动更新线程")
            return
            
        if self.update_thread and self.update_thread.is_alive():
            logger.warning("数据更新线程已在运行")
            return
            
        self.stop_flag = False
        self.update_thread = threading.Thread(target=self._data_update_loop)
        self.update_thread.daemon = True
        self.update_thread.start()
        logger.info("数据更新线程已启动")
    
    def stop_data_update_thread(self):
        """停止数据更新线程"""
        if self.update_thread and self.update_thread.is_alive():
            self.stop_flag = True
            self.update_thread.join(timeout=5)
            logger.info("数据更新线程已停止")
    
    def _data_update_loop(self):
        """数据更新循环"""
        while not self.stop_flag:
            try:
                # 判断是否在交易时间
                if config.is_trade_time():
                    if config.VERBOSE_LOOP_LOGGING or config.DEBUG:
                        logger.debug("开始更新所有股票数据")
                    self.update_all_stock_data()
                    if config.VERBOSE_LOOP_LOGGING or config.DEBUG:
                        logger.debug("股票数据更新完成")

                # 等待下一次更新
                for _ in range(config.UPDATE_INTERVAL):
                    if self.stop_flag:
                        break
                    time.sleep(1)
                    
            except Exception as e:
                logger.error(f"数据更新循环出错: {str(e)}")
                time.sleep(60)  # 出错后等待一分钟再继续
    
    def close(self):
        """关闭数据管理器"""
        self.stop_data_update_thread()
        
        if self.conn:
            self.conn.close()
            logger.info("数据库连接已关闭")

        # xtquant的xtdata模块不需要显式断开连接
        # 连接会在进程退出时自动释放
        logger.info("数据管理器已关闭")


# 单例模式
_instance = None

def get_data_manager():
    """获取DataManager单例"""
    global _instance
    if _instance is None:
        _instance = DataManager()
    return _instance
