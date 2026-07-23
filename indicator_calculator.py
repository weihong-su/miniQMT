"""
指标计算模块，负责计算各种技术指标
"""
import pandas as pd
import numpy as np
import sqlite3
from datetime import datetime
from MyTT import *

import config
from logger import get_logger
from data_manager import get_data_manager

# 获取logger
logger = get_logger("indicator_calculator")

class IndicatorCalculator:
    """指标计算类"""
    
    def __init__(self):
        """初始化指标计算器"""
        self.data_manager = get_data_manager()
        self.conn = self.data_manager.conn
        self._empty_data_warning_times = {}

    def _should_log_empty_data_warning(self, stock_code, purpose):
        """同一股票同一指标目的的空数据告警限频。"""
        interval = getattr(config, 'INDICATOR_EMPTY_DATA_LOG_INTERVAL_SECONDS', 300)
        if interval <= 0:
            return True

        if not hasattr(self, '_empty_data_warning_times'):
            self._empty_data_warning_times = {}

        key = (str(stock_code), str(purpose))
        now_ts = datetime.now().timestamp()
        last_ts = self._empty_data_warning_times.get(key, 0)
        if now_ts - last_ts < interval:
            return False

        self._empty_data_warning_times[key] = now_ts
        return True
    
    def calculate_all_indicators(self, stock_code, force_update=False):
        """
        计算所有技术指标

        参数:
        stock_code (str): 股票代码
        force_update (bool): 是否强制更新所有数据的指标

        返回:
        bool: 是否计算成功
        """
        try:
            # 获取全量历史数据（用于滑动窗口计算上下文）
            df_full = self.data_manager.get_history_data_from_db(stock_code)
            if df_full.empty:
                if self._should_log_empty_data_warning(stock_code, 'history'):
                    logger.warning(f"没有 {stock_code} 的历史数据，无法计算指标")
                else:
                    logger.debug(f"没有 {stock_code} 的历史数据，无法计算指标，重复告警已降噪")
                return False

            # 按日期排序并重置索引
            df_full = df_full.sort_values('date').reset_index(drop=True)

            # 确定需要保存指标的新行数量
            n_new = len(df_full)  # 默认：全量计算（force_update 或首次计算）

            # 如果不是强制更新，检查是否有新数据需要计算
            if not force_update:
                # 获取指标表中的最新日期
                cursor = self.conn.cursor()
                cursor.execute(
                    "SELECT MAX(date) FROM stock_indicators WHERE stock_code=?",
                    (stock_code,)
                )
                result = cursor.fetchone()
                latest_indicator_date = result[0] if result and result[0] else None

                # 如果没有新数据，不需要计算
                if latest_indicator_date:
                    latest_df_date = df_full['date'].max()
                    if latest_indicator_date >= latest_df_date:
                        logger.debug(f"{stock_code} 的指标已是最新，不需要更新")
                        return True

                    df_new = df_full[df_full['date'] > latest_indicator_date]
                    if df_new.empty:
                        logger.debug(f"{stock_code} 没有新的数据需要计算指标")
                        return True
                    n_new = len(df_new)

            # 在全量历史数据上计算指标：
            #   - MyTT 的 SMA/MACD 均为 EMA 风格（路径依赖），使用完整历史才能保证数值正确
            #   - 同时保证滑动窗口始终有足够数据，彻底消除"数据长度不足"警告
            df_calc = df_full

            # 在完整窗口数据上计算指标
            result_df = pd.DataFrame()
            result_df['stock_code'] = df_calc['stock_code']
            result_df['date'] = df_calc['date']

            # 计算均线指标
            for period in config.MA_PERIODS:
                ma_col = f'ma{period}'
                result_df[ma_col] = self._calculate_ma(df_calc, period)

            # 计算MACD指标
            macd_df = self._calculate_macd(df_calc)
            for col in macd_df.columns:
                result_df[col] = macd_df[col]

            # 只保留新行的计算结果（末尾 n_new 行），避免重复写入已有指标
            result_df = result_df.tail(n_new).reset_index(drop=True)

            # 保存指标结果到数据库
            self._save_indicators(result_df)

            logger.info(f"成功计算 {stock_code} 的技术指标，共 {len(result_df)} 条记录")
            return True

        except Exception as e:
            logger.error(f"计算 {stock_code} 的技术指标时出错: {str(e)}")
            return False
    
    def _calculate_ma(self, df, period):
        """
        计算移动平均线
        
        参数:
        df (pandas.DataFrame): 历史数据
        period (int): 周期
        
        返回:
        pandas.Series: 移动平均线数据
        """
        try:
            # 检查数据长度是否足够
            if len(df) < period:
                logger.warning(f"数据长度不足以计算MA{period}，需要{period}条数据，实际{len(df)}条")
                return pd.Series([None] * len(df))
            
            # 使用MyTT计算MA，并检查结果
            ma = SMA(df['close'].values.astype(float), N=period)
            
            # 检查计算结果
            if ma is None or len(ma) == 0:
                logger.warning(f"MA{period}计算结果为空")
                return pd.Series([None] * len(df))
                
            # 转换为pandas Series并处理NaN
            ma_series = pd.Series(ma)
            ma_series = ma_series.replace([np.nan, np.inf, -np.inf], None)
            
            return ma_series
            
        except Exception as e:
            logger.error(f"计算MA{period}指标时出错: {str(e)}")
            return pd.Series([None] * len(df))
    
    def _calculate_macd(self, df):
        """
        计算MACD指标
        
        参数:
        df (pandas.DataFrame): 历史数据
        
        返回:
        pandas.DataFrame: MACD指标数据
        """
        try:
            # 检查数据长度是否足够
            min_periods = max(config.MACD_FAST, config.MACD_SLOW, config.MACD_SIGNAL) + 10
            if len(df) < min_periods:
                logger.warning(f"数据长度不足以计算MACD，需要至少{min_periods}条数据，实际{len(df)}条")
                return pd.DataFrame({
                    'macd': [None] * len(df),
                    'macd_signal': [None] * len(df),
                    'macd_hist': [None] * len(df)
                })
            
            # 使用MyTT计算MACD
            macd, signal, hist = MACD(
                df['close'].values.astype(float),
                SHORT=config.MACD_FAST,
                LONG=config.MACD_SLOW,
                M=config.MACD_SIGNAL
            )
            
            # 检查计算结果
            if any(x is None or len(x) == 0 for x in [macd, signal, hist]):
                logger.warning("MACD计算结果包含空值")
                return pd.DataFrame({
                    'macd': [None] * len(df),
                    'macd_signal': [None] * len(df),
                    'macd_hist': [None] * len(df)
                })
            
            # 创建结果DataFrame并处理异常值
            result = pd.DataFrame({
                'macd': pd.Series(macd).replace([np.nan, np.inf, -np.inf], None),
                'macd_signal': pd.Series(signal).replace([np.nan, np.inf, -np.inf], None),
                'macd_hist': pd.Series(hist).replace([np.nan, np.inf, -np.inf], None)
            })
            
            return result
            
        except Exception as e:
            logger.error(f"计算MACD指标时出错: {str(e)}")
            # 返回包含None值的DataFrame
            return pd.DataFrame({
                'macd': [None] * len(df),
                'macd_signal': [None] * len(df),
                'macd_hist': [None] * len(df)
            })
    
    def _save_indicators(self, df):
        """
        保存指标到数据库
        
        参数:
        df (pandas.DataFrame): 指标数据
        """
        try:
            # 处理NaN值
            df = df.replace({np.nan: None})

            # 先删除已存在的同日期记录，确保幂等（避免 force_update 重复插入）
            cursor = self.conn.cursor()
            pairs = list(zip(df['stock_code'], df['date']))
            cursor.executemany(
                "DELETE FROM stock_indicators WHERE stock_code=? AND date=?",
                pairs
            )

            # 保存到数据库
            df.to_sql('stock_indicators', self.conn, if_exists='append', index=False, method='multi')
            self.conn.commit()

        except Exception as e:
            logger.error(f"保存指标数据时出错: {str(e)}")
            self.conn.rollback()
    
    def get_latest_indicators(self, stock_code):
        """
        获取最新的指标数据
        
        参数:
        stock_code (str): 股票代码
        
        返回:
        dict: 最新指标数据
        """
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT * FROM stock_indicators 
                WHERE stock_code=? 
                ORDER BY date DESC 
                LIMIT 1
            """, (stock_code,))
            
            row = cursor.fetchone()
            if not row:
                logger.warning(f"未找到 {stock_code} 的指标数据")
                return None
            
            # 获取列名
            columns = [description[0] for description in cursor.description]
            
            # 转换为字典
            indicators = dict(zip(columns, row))
            return indicators
            
        except Exception as e:
            logger.error(f"获取 {stock_code} 的最新指标数据时出错: {str(e)}")
            return None
    
    def get_indicators_history(self, stock_code, days=60):
        """
        获取历史指标数据
        
        参数:
        stock_code (str): 股票代码
        days (int): 获取最近的天数
        
        返回:
        pandas.DataFrame: 历史指标数据
        """
        try:
            query = f"""
                SELECT * FROM stock_indicators 
                WHERE stock_code=? 
                ORDER BY date DESC 
                LIMIT {days}
            """
            
            df = pd.read_sql_query(query, self.conn, params=(stock_code,))
            
            # 按日期排序（从早到晚）
            df = df.sort_values('date')
            
            return df
            
        except Exception as e:
            logger.error(f"获取 {stock_code} 的历史指标数据时出错: {str(e)}")
            return pd.DataFrame()
    
    def check_buy_signal(self, stock_code):
        """
        检查买入信号
        
        参数:
        stock_code (str): 股票代码
        
        返回:
        bool: 是否有买入信号
        """
        try:
            # 获取最近的指标数据
            indicators_df = self.get_indicators_history(stock_code, days=10)
            if indicators_df.empty:
                if self._should_log_empty_data_warning(stock_code, 'buy_signal'):
                    logger.warning(f"没有足够的 {stock_code} 指标数据来检查买入信号")
                else:
                    logger.debug(f"没有足够的 {stock_code} 指标数据来检查买入信号，重复告警已降噪")
                return False
            
            # 计算MACD金叉信号
            if len(indicators_df) >= 2:
                # 检查前一天MACD柱为负，当天MACD柱为正（MACD金叉）
                prev_hist = indicators_df.iloc[-2]['macd_hist']
                curr_hist = indicators_df.iloc[-1]['macd_hist']

                # 添加None值检查
                if prev_hist is None or curr_hist is None:
                    logger.debug(f"{stock_code} MACD数据包含None值，跳过金叉检查")
                    return False                
                
                macd_cross = prev_hist < 0 and curr_hist > 0
                
                # 检查均线多头排列（MA10 > MA20 > MA30 > MA60）
                latest = indicators_df.iloc[-1]
                ma10 = latest['ma10']
                ma20 = latest['ma20'] 
                ma30 = latest['ma30']
                ma60 = latest['ma60']
                
                # 添加None值检查
                if any(ma is None for ma in [ma10, ma20, ma30, ma60]):
                    logger.debug(f"{stock_code} 均线数据包含None值，跳过均线排列检查")
                    return False
                
                ma_alignment = ma10 > ma20 > ma30 > ma60
                
                # 检查是否满足买入条件
                if macd_cross and ma_alignment:
                    logger.debug(f"{stock_code} 满足买入条件: MACD金叉 + 均线多头排列")
                    return True
            
            return False
            
        except Exception as e:
            logger.error(f"检查 {stock_code} 的买入信号时出错: {str(e)}")
            return False
    
    def check_sell_signal(self, stock_code):
        """
        检查卖出信号
        
        参数:
        stock_code (str): 股票代码
        
        返回:
        bool: 是否有卖出信号
        """
        try:
            # 获取最近的指标数据
            indicators_df = self.get_indicators_history(stock_code, days=10)
            if indicators_df.empty:
                if self._should_log_empty_data_warning(stock_code, 'sell_signal'):
                    logger.warning(f"没有足够的 {stock_code} 指标数据来检查卖出信号")
                else:
                    logger.debug(f"没有足够的 {stock_code} 指标数据来检查卖出信号，重复告警已降噪")
                return False
            
            # 计算MACD死叉信号
            if len(indicators_df) >= 2:
                # 检查前一天MACD柱为正，当天MACD柱为负（MACD死叉）
                prev_hist = indicators_df.iloc[-2]['macd_hist']
                curr_hist = indicators_df.iloc[-1]['macd_hist']

                # 添加None值检查
                if prev_hist is None or curr_hist is None:
                    logger.debug(f"{stock_code} MACD数据包含None值，跳过死叉检查")
                    return False
                
                macd_cross = prev_hist > 0 and curr_hist < 0
                
                # 检查均线空头排列（MA10 < MA20 < MA30 < MA60）
                latest = indicators_df.iloc[-1]
                ma10 = latest['ma10']
                ma20 = latest['ma20']
                ma30 = latest['ma30'] 
                ma60 = latest['ma60']
                
                # 添加None值检查
                if any(ma is None for ma in [ma10, ma20, ma30, ma60]):
                    logger.debug(f"{stock_code} 均线数据包含None值，跳过均线排列检查")
                    return False
                
                ma_alignment = ma10 < ma20 < ma30 < ma60
                
                # 检查是否满足卖出条件
                if macd_cross and ma_alignment:
                    logger.info(f"{stock_code} 满足卖出条件: MACD死叉 + 均线空头排列")
                    return True
            
            return False
            
        except Exception as e:
            logger.error(f"检查 {stock_code} 的卖出信号时出错: {str(e)}")
            return False
    
    def update_all_stock_indicators(self, force_update=False):
        """
        更新所有股票的技术指标
        
        参数:
        force_update (bool): 是否强制更新所有数据的指标
        """
        for stock_code in config.STOCK_POOL:
            self.calculate_all_indicators(stock_code, force_update)


# 单例模式
_instance = None

def get_indicator_calculator():
    """获取IndicatorCalculator单例"""
    global _instance
    if _instance is None:
        _instance = IndicatorCalculator()
    return _instance
