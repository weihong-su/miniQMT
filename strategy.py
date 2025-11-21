"""
交易策略模块，实现具体的交易策略逻辑
优化版本：统一止盈止损逻辑，优先处理止损，支持模拟交易
"""
import time
import threading
from datetime import datetime
import pandas as pd
import numpy as np

import config
from logger import get_logger
from data_manager import get_data_manager
from indicator_calculator import get_indicator_calculator
from position_manager import get_position_manager
from trading_executor import get_trading_executor

# 获取logger
logger = get_logger("strategy")

class TradingStrategy:
    """交易策略类，实现各种交易策略"""
    
    def __init__(self):
        """初始化交易策略"""
        self.data_manager = get_data_manager()
        self.indicator_calculator = get_indicator_calculator()
        self.position_manager = get_position_manager()
        self.trading_executor = get_trading_executor()
        
        # 策略运行线程
        self.strategy_thread = None
        self.stop_flag = False
        
        # 防止频繁交易的冷却时间记录
        self.last_trade_time = {}
        
        # 已处理的止盈止损信号记录
        self.processed_signals = set()

        # 添加这行 - 重试计数器
        self.retry_counts = {}
    
    def init_grid_trading(self, stock_code):
        """
        初始化网格交易
        
        参数:
        stock_code (str): 股票代码
        
        返回:
        bool: 是否初始化成功
        """
        try:
            if not config.ENABLE_GRID_TRADING:
                logger.info(f"网格交易功能未启用，跳过 {stock_code} 的网格初始化")
                return False
            
            # 获取持仓信息
            position = self.position_manager.get_position(stock_code)
            if not position:
                logger.warning(f"未持有 {stock_code}，无法初始化网格交易")
                return False
            
            # 获取最新行情
            latest_quote = self.data_manager.get_latest_data(stock_code)
            if not latest_quote:
                logger.error(f"未能获取 {stock_code} 的最新行情，无法初始化网格交易")
                return False
            
            current_price = latest_quote.get('lastPrice')
            position_volume = position['volume']
            
            # 清除旧的网格记录
            # 这里需要在position_manager中添加一个清除网格交易的方法，暂时跳过
            
            # 创建网格
            grid_count = min(config.GRID_MAX_LEVELS, 5)  # 最多创建5个网格
            grid_volume = int(position_volume * config.GRID_POSITION_RATIO / grid_count)
            
            if grid_volume < 100:
                logger.warning(f"{stock_code} 持仓量不足，无法创建有效的网格交易")
                return False
            
            for i in range(grid_count):
                # 买入价格递减，卖出价格递增
                buy_price = current_price * (1 - config.GRID_STEP_RATIO * (i + 1))
                sell_price = current_price * (1 + config.GRID_STEP_RATIO * (i + 1))
                
                # 创建网格交易
                grid_id = self.position_manager.add_grid_trade(
                    stock_code, i + 1, buy_price, sell_price, grid_volume
                )
                
                if grid_id < 0:
                    logger.error(f"创建 {stock_code} 的网格交易记录失败")
                    return False
            
            logger.info(f"初始化 {stock_code} 的网格交易成功，创建了 {grid_count} 个网格")
            return True
            
        except Exception as e:
            logger.error(f"初始化 {stock_code} 的网格交易时出错: {str(e)}")
            return False
    
    def execute_grid_trading(self, stock_code):
        """
        执行网格交易策略
        
        参数:
        stock_code (str): 股票代码
        
        返回:
        bool: 是否执行成功
        """
        try:
            if not config.ENABLE_GRID_TRADING:
                return False
            
            # 检查是否有网格交易信号
            grid_signals = self.position_manager.check_grid_trade_signals(stock_code)
            
            # 处理买入信号
            for signal in grid_signals['buy_signals']:
                grid_id = signal['grid_id']
                price = signal['price']
                volume = signal['volume']
                
                # 检查同一网格是否已经在冷却期
                cool_key = f"grid_buy_{stock_code}_{grid_id}"
                if cool_key in self.last_trade_time:
                    last_time = self.last_trade_time[cool_key]
                    if (datetime.now() - last_time).total_seconds() < 300:  # 5分钟冷却期
                        logger.debug(f"{stock_code} 网格 {grid_id} 买入信号在冷却期内，跳过")
                        continue
                
                # 执行买入
                logger.info(f"执行 {stock_code} 网格 {grid_id} 买入，价格: {price}, 数量: {volume}")
                order_id = self.trading_executor.buy_stock(stock_code, volume, price, strategy='grid')
                
                if order_id:
                    # 更新网格状态为活跃
                    self.position_manager.update_grid_trade_status(grid_id, 'ACTIVE')
                    
                    # 记录交易时间
                    self.last_trade_time[cool_key] = datetime.now()
            
            # 处理卖出信号
            for signal in grid_signals['sell_signals']:
                grid_id = signal['grid_id']
                price = signal['price']
                volume = signal['volume']
                
                # 检查同一网格是否已经在冷却期
                cool_key = f"grid_sell_{stock_code}_{grid_id}"
                if cool_key in self.last_trade_time:
                    last_time = self.last_trade_time[cool_key]
                    if (datetime.now() - last_time).total_seconds() < 300:  # 5分钟冷却期
                        logger.debug(f"{stock_code} 网格 {grid_id} 卖出信号在冷却期内，跳过")
                        continue
                
                # 执行卖出
                logger.info(f"执行 {stock_code} 网格 {grid_id} 卖出，价格: {price}, 数量: {volume}")
                order_id = self.trading_executor.sell_stock(stock_code, volume, price, strategy='grid')
                
                if order_id:
                    # 更新网格状态为完成
                    self.position_manager.update_grid_trade_status(grid_id, 'COMPLETED')
                    
                    # 记录交易时间
                    self.last_trade_time[cool_key] = datetime.now()
            
            return True
            
        except Exception as e:
            logger.error(f"执行 {stock_code} 的网格交易时出错: {str(e)}")
            return False

    # ========== 新增：统一的止盈止损执行逻辑 ==========
    def execute_trading_signal_direct(self, stock_code, signal_type, signal_info):
        """直接执行指定的交易信号"""
        try:
                    # 🔑 添加统一信号验证
            if not self.position_manager.validate_trading_signal(stock_code, signal_type, signal_info):
                logger.error(f"🚨 {stock_code} {signal_type} 信号验证失败，拒绝执行")
                return False
        
            if signal_type == 'stop_loss':
                return self._execute_stop_loss_signal(stock_code, signal_info)
            elif signal_type == 'take_profit_half':
                return self._execute_take_profit_half_signal(stock_code, signal_info)
            elif signal_type == 'take_profit_full':
                return self._execute_take_profit_full_signal(stock_code, signal_info)
            else:
                logger.warning(f"未知的信号类型: {signal_type}")
                return False

        except Exception as e:
            logger.error(f"执行 {stock_code} 的 {signal_type} 信号时出错: {str(e)}")
            return False

    def execute_add_position_strategy(self, stock_code, add_position_info):
        """
        执行补仓策略
        
        参数:
        stock_code (str): 股票代码
        add_position_info (dict): 补仓信号详细信息
        
        返回:
        bool: 是否执行成功
        """
        try:
            # 最终持仓限制检查（防止时差导致的超限）
            position = self.position_manager.get_position(stock_code)
            if position:
                current_value = float(position.get('market_value', 0))
                add_amount = add_position_info['add_amount']
                
                if current_value + add_amount > config.MAX_POSITION_VALUE:
                    logger.warning(f"{stock_code} 补仓被拒绝: 当前市值{current_value} + 补仓{add_amount} = {current_value + add_amount} > 限制{config.MAX_POSITION_VALUE}")
                    return False
                            
            # 冷却期检查
            cool_key = f"add_position_{stock_code}"
            if cool_key in getattr(self, 'last_trade_time', {}):
                last_time = self.last_trade_time[cool_key]
                if (datetime.now() - last_time).total_seconds() < 120:  # 2分钟冷却期
                    logger.debug(f"{stock_code} 补仓信号在冷却期内，跳过")
                    return False   
                         
            add_amount = add_position_info['add_amount']
            current_price = add_position_info['current_price']
            
            logger.info(f"执行 {stock_code} 补仓策略，补仓金额: {add_amount:.2f}, 当前价格: {current_price:.2f}")
            
            # 检查是否为模拟交易模式
            if hasattr(config, 'ENABLE_SIMULATION_MODE') and config.ENABLE_SIMULATION_MODE:
                # 模拟交易：计算买入数量
                volume = int(add_amount // current_price / 100) * 100  # 向下取整到100的倍数
                if volume < 100:
                    logger.warning(f"{stock_code} 计算的补仓数量过小: {volume}，跳过")
                    return False
                
                # 模拟买入
                success = self.position_manager.simulate_buy_position(
                    stock_code=stock_code,
                    volume=volume,
                    price=current_price
                )
                
                if success:
                    logger.info(f"[模拟交易] {stock_code} 补仓执行完成，数量: {volume}")
                    return True
            else:
                # 实盘交易：调用交易接口
                logger.info(f"[实盘交易] {stock_code} 补仓信号已识别，执行实盘补仓")
                
                # 使用金额买入方式
                order_id = self.trading_executor.buy_stock(
                    stock_code, amount=add_amount, price_type=5, strategy='add_position'
                )

                if not hasattr(self, 'last_trade_time'):
                    self.last_trade_time = {}
                self.last_trade_time[cool_key] = datetime.now()
                logger.info(f"{stock_code} 补仓成功，设置2分钟冷却期")

                return order_id is not None
            
            return False
            
        except Exception as e:
            logger.error(f"执行 {stock_code} 补仓策略时出错: {str(e)}")
            return False

    # def execute_trading_signal(self, stock_code):
    #     """
    #     执行统一的交易信号处理 - 优化版本
        
    #     参数:
    #     stock_code (str): 股票代码
        
    #     返回:
    #     bool: 是否执行了交易操作
    #     """
    #     try:
    #         # 使用统一的信号检查函数
    #         signal_type, signal_info = self.position_manager.check_trading_signals(stock_code)
            
    #         if not signal_type:
    #             return False
            
    #         # 检查是否已处理过该信号（防重复处理）
    #         signal_key = f"{signal_type}_{stock_code}_{datetime.now().strftime('%Y%m%d_%H')}"
    #         if signal_key in self.processed_signals:
    #             logger.debug(f"{stock_code} {signal_type} 信号已处理，跳过")
    #             return False
            
    #         logger.info(f"处理 {stock_code} 的 {signal_type} 信号")
            
    #         # 根据信号类型执行相应操作
    #         success = False
            
    #         if signal_type == 'stop_loss':
    #             success = self._execute_stop_loss_signal(stock_code, signal_info)
    #         elif signal_type == 'take_profit_half':
    #             success = self._execute_take_profit_half_signal(stock_code, signal_info)
    #         elif signal_type == 'take_profit_full':
    #             success = self._execute_take_profit_full_signal(stock_code, signal_info)
            
    #         if success:
    #             # 记录已处理信号
    #             self.processed_signals.add(signal_key)
    #             logger.info(f"{stock_code} {signal_type} 信号处理成功")
            
    #         return success
            
    #     except Exception as e:
    #         logger.error(f"执行 {stock_code} 的交易信号时出错: {str(e)}")
    #         return False

    def _execute_stop_loss_signal(self, stock_code, signal_info):
        """
        执行止损信号
        
        参数:
        stock_code (str): 股票代码
        signal_info (dict): 信号详细信息
        
        返回:
        bool: 是否执行成功
        """
        try:

            # 🔑 添加信号验证 - 在执行前进行最后防护
            if not self.position_manager.validate_trading_signal(stock_code, 'stop_loss', signal_info):
                logger.error(f"🚨 {stock_code} 止损信号验证失败，拒绝执行")
                return False
        
            volume = signal_info['volume']
            current_price = signal_info['current_price']
            
            logger.warning(f"执行 {stock_code} 止损操作，数量: {volume}, 当前价格: {current_price:.2f}")
            
            # 检查是否为模拟交易模式
            if hasattr(config, 'ENABLE_SIMULATION_MODE') and config.ENABLE_SIMULATION_MODE:
                # 模拟交易：调用优化后的模拟卖出方法
                success = self.position_manager.simulate_sell_position(
                    stock_code=stock_code,
                    sell_volume=volume,
                    sell_price=current_price,
                    sell_type='full'
                )
                
                if success:
                    logger.warning(f"[模拟交易] {stock_code} 止损执行完成，持仓已清零")
                    return success
            else:
                # 实盘交易：调用交易接口
                logger.warning(f"[实盘交易] {stock_code} 止损信号已识别，执行实盘交易stop_loss")
                
                # 实盘交易功能
                order_id = self.trading_executor.sell_stock(
                    stock_code, volume, price_type=5, strategy='stop_loss'
                )
                return order_id is not None
            
            return False  # 暂时返回False，表示未执行实盘交易
                
        except Exception as e:
            logger.error(f"执行 {stock_code} 止损信号时出错: {str(e)}")
            return False

    def _execute_take_profit_half_signal(self, stock_code, signal_info):
        """
        执行首次止盈信号（卖出半仓）
        
        参数:
        stock_code (str): 股票代码
        signal_info (dict): 信号详细信息
        
        返回:
        bool: 是否执行成功
        """
        try:
            # 🔑 添加信号验证
            if not self.position_manager.validate_trading_signal(stock_code, 'take_profit_half', signal_info):
                logger.error(f"🚨 {stock_code} 首次止盈信号验证失败，拒绝执行")
                return False


            total_volume = signal_info['volume']
            current_price = signal_info['current_price']
            sell_ratio = signal_info['sell_ratio']
            breakout_highest_price = signal_info.get('breakout_highest_price', 0)
            pullback_ratio = signal_info.get('pullback_ratio', 0)

            # 计算卖出数量
            sell_volume = int(total_volume * sell_ratio / 100) * 100
            sell_volume = max(sell_volume, 100)  # 至少100股
            
            logger.info(f"执行 {stock_code} 首次止盈，卖出半仓，数量: {sell_volume}, 价格: {current_price:.2f}")
            if breakout_highest_price > 0:
                logger.info(f"  - 突破后最高价: {breakout_highest_price:.2f}, 回撤幅度: {pullback_ratio:.2%}")            
            
            # 检查是否为模拟交易模式
            if hasattr(config, 'ENABLE_SIMULATION_MODE') and config.ENABLE_SIMULATION_MODE:
                # 模拟交易：调用优化后的模拟卖出方法
                success = self.position_manager.simulate_sell_position(
                    stock_code=stock_code,
                    sell_volume=sell_volume,
                    sell_price=current_price,
                    sell_type='partial'
                )
                
                if success:
                    # 验证执行结果
                    updated_position = self.position_manager.get_position(stock_code)
                    if updated_position and updated_position.get('profit_triggered'):
                        logger.info(f"[验证成功] {stock_code} 首次止盈执行完成并已标记")
                        return True
                    else:
                        logger.error(f"[验证失败] {stock_code} 首次止盈执行后状态异常")
                        return False
            
                return success
            else:
                # 实盘交易：调用交易接口
                logger.info(f"[实盘交易] {stock_code} 首次止盈信号已识别，执行实盘卖出交易take_profit_half")
                
                # 实盘交易
                order_id = self.trading_executor.sell_stock(
                    stock_code, sell_volume, price_type=5, strategy='auto_partial'
                )
                if order_id:
                    logger.info(f"[实盘交易] {stock_code} 首次止盈卖出委托已下达，委托号: {order_id}")
                    # 标记已触发首次止盈
                    mark_success = self.position_manager.mark_profit_triggered(stock_code)
                    if mark_success:
                        logger.info(f"[状态标记] {stock_code} profit_triggered已标记为True")                        
                        # 返回True表示整个止盈操作成功
                        return True
                    else:
                        logger.error(f"[状态标记] {stock_code} profit_triggered标记失败")
                        return False            
                else:
                    logger.error(f"[实盘交易] {stock_code} 首次止盈卖出委托下达失败")
                    return False
                
        except Exception as e:
            logger.error(f"执行 {stock_code} 首次止盈信号时出错: {str(e)}")
            return False

    def _execute_take_profit_full_signal(self, stock_code, signal_info):
        """
        执行动态止盈信号（卖出剩余仓位）
        
        参数:
        stock_code (str): 股票代码
        signal_info (dict): 信号详细信息
        
        返回:
        bool: 是否执行成功
        """
        try:
            # 🔑 添加信号验证
            if not self.position_manager.validate_trading_signal(stock_code, 'take_profit_full', signal_info):
                logger.error(f"🚨 {stock_code} 动态止盈信号验证失败，拒绝执行")
                return False

            volume = signal_info['volume']
            current_price = signal_info['current_price']
            dynamic_take_profit_price = signal_info['dynamic_take_profit_price']
            
            logger.info(f"执行 {stock_code} 动态止盈，卖出剩余仓位，数量: {volume}, "
                       f"当前价格: {current_price:.2f}, 止盈位: {dynamic_take_profit_price:.2f}")
            
            # 检查是否为模拟交易模式
            if hasattr(config, 'ENABLE_SIMULATION_MODE') and config.ENABLE_SIMULATION_MODE:
                # 模拟交易：直接调整持仓
                success = self.position_manager.simulate_sell_position(
                    stock_code=stock_code,
                    sell_volume=volume,
                    sell_price=current_price,
                    sell_type='full'
                )
                
                if success:
                    logger.info(f"[模拟交易] {stock_code} 动态止盈执行完成，持仓已清零")
                    return success
            else:
                # 实盘交易：调用交易接口
                logger.info(f"[实盘交易] {stock_code} 动态止盈信号已识别，执行实盘卖出交易take_profit_full")
                
                # 实盘交易
                order_id = self.trading_executor.sell_stock(
                    stock_code, volume, price_type=5, strategy='auto_full'
                )

                if order_id:
                    logger.info(f"[实盘交易] {stock_code} 止盈全仓卖出委托已下达，委托号: {order_id}")
                    return True
                else:
                    logger.error(f"[实盘交易] {stock_code} 全仓止盈卖出委托下达失败")
                    return False
                
            return False  # 暂时返回False，表示未执行实盘交易
                
        except Exception as e:
            logger.error(f"执行 {stock_code} 动态全仓止盈信号时出错: {str(e)}")
            return False


    # ========== 向后兼容的旧版本接口 ==========
    
    # def execute_stop_loss(self, stock_code):
    #     """
    #     执行止损策略 - 向后兼容接口
        
    #     参数:
    #     stock_code (str): 股票代码
        
    #     返回:
    #     bool: 是否执行成功
    #     """
    #     try:
    #         # 使用新的统一信号检查
    #         signal_type, signal_info = self.position_manager.check_trading_signals(stock_code)
            
    #         if signal_type == 'stop_loss':
    #             # 检查是否已处理过该信号
    #             signal_key = f"stop_loss_{stock_code}_{datetime.now().strftime('%Y%m%d')}"
    #             if signal_key in self.processed_signals:
    #                 logger.debug(f"{stock_code} 止损信号已处理，跳过")
    #                 return False
                
    #             success = self._execute_stop_loss_signal(stock_code, signal_info)
    #             if success:
    #                 self.processed_signals.add(signal_key)
    #             return success
            
    #         return False
            
    #     except Exception as e:
    #         logger.error(f"执行 {stock_code} 的止损策略时出错: {str(e)}")
    #         return False
    
    # def execute_dynamic_take_profit(self, stock_code):
    #     """
    #     执行动态止盈策略 - 向后兼容接口
        
    #     参数:
    #     stock_code (str): 股票代码
        
    #     返回:
    #     bool: 是否执行成功
    #     """
    #     try:
    #         # 使用新的统一信号检查
    #         signal_type, signal_info = self.position_manager.check_trading_signals(stock_code)
            
    #         if signal_type in ['take_profit_half', 'take_profit_full']:
    #             # 检查是否已处理过该信号
    #             signal_key = f"take_profit_{stock_code}_{signal_type}_{datetime.now().strftime('%Y%m%d')}"
    #             if signal_key in self.processed_signals:
    #                 logger.debug(f"{stock_code} {signal_type} 止盈信号已处理，跳过")
    #                 return False
                
    #             success = False
    #             if signal_type == 'take_profit_half':
    #                 success = self._execute_take_profit_half_signal(stock_code, signal_info)
    #             elif signal_type == 'take_profit_full':
    #                 success = self._execute_take_profit_full_signal(stock_code, signal_info)
                
    #             if success:
    #                 self.processed_signals.add(signal_key)
    #             return success
            
    #         return False
            
    #     except Exception as e:
    #         logger.error(f"执行 {stock_code} 的动态止盈策略时出错: {str(e)}")
    #         return False
    
    def execute_buy_strategy(self, stock_code):
        """
        执行买入策略
        
        参数:
        stock_code (str): 股票代码
        
        返回:
        bool: 是否执行成功
        """
        try:
            # 检查是否有买入信号
            buy_signal = self.indicator_calculator.check_buy_signal(stock_code)
            
            if buy_signal:
                # 检查是否已处理过该信号
                signal_key = f"buy_{stock_code}_{datetime.now().strftime('%Y%m%d')}"
                if signal_key in self.processed_signals:
                    logger.debug(f"{stock_code} 买入信号已处理，跳过")
                    return False
                
                # 检查是否已有持仓
                position = self.position_manager.get_position(stock_code)
                
                # 确定买入金额
                if position:
                    # 已有持仓，检查是否达到补仓条件
                    current_price = position['current_price']
                    cost_price = position['cost_price']
                    current_value = position['market_value']
                    
                    # 检查是否满足补仓格点要求
                    price_ratio = current_price / cost_price
                    
                    # 寻找满足条件的补仓格点
                    buy_level = None
                    for i, level in enumerate(config.BUY_GRID_LEVELS):
                        if i > 0 and price_ratio <= level:  # 不是第一格且价格比例小于等于格点比例
                            buy_level = i
                            break
                    
                    if buy_level is None:
                        logger.info(f"{stock_code} 当前价格不满足补仓条件")
                        return False
                    
                    # 检查是否达到最大持仓限制
                    if current_value >= config.MAX_POSITION_VALUE:
                        logger.info(f"{stock_code} 持仓已达到最大限制，不再补仓")
                        return False
                    
                    # 确定补仓金额
                    buy_amount = config.POSITION_UNIT * config.BUY_AMOUNT_RATIO[buy_level]
                    
                    logger.info(f"执行 {stock_code} 补仓策略，当前价格比例: {price_ratio:.2f}, 补仓格点: {buy_level}, 补仓金额: {buy_amount}")
                else:
                    # 新建仓，使用第一个格点的金额
                    buy_amount = config.POSITION_UNIT * config.BUY_AMOUNT_RATIO[0]
                    logger.info(f"执行 {stock_code} 首次建仓，金额: {buy_amount}")
                
                # 执行买入
                order_id = self.trading_executor.buy_stock(stock_code, amount=buy_amount, price_type=0)
                
                if order_id:
                    # 记录已处理信号
                    self.processed_signals.add(signal_key)
                    
                    # 如果是新建仓，初始化网格交易
                    if not position and config.ENABLE_GRID_TRADING:
                        # 等待买入成交后再初始化网格
                        # 实际应用中应该通过回调函数处理
                        time.sleep(5)  # 简单等待一下
                        self.init_grid_trading(stock_code)
                    
                    return True
            
            return False
            
        except Exception as e:
            logger.error(f"执行 {stock_code} 的买入策略时出错: {str(e)}")
            return False
    
    def execute_sell_strategy(self, stock_code):
        """
        执行卖出策略
        
        参数:
        stock_code (str): 股票代码
        
        返回:
        bool: 是否执行成功
        """
        try:
            # 检查是否有卖出信号
            sell_signal = self.indicator_calculator.check_sell_signal(stock_code)
            
            if sell_signal:
                # 检查是否已处理过该信号
                signal_key = f"sell_{stock_code}_{datetime.now().strftime('%Y%m%d')}"
                if signal_key in self.processed_signals:
                    logger.debug(f"{stock_code} 卖出信号已处理，跳过")
                    return False
                
                # 获取持仓
                position = self.position_manager.get_position(stock_code)
                if not position:
                    logger.warning(f"未持有 {stock_code}，无法执行卖出策略")
                    return False
                
                volume = position['volume']
                
                # 执行卖出
                logger.info(f"执行 {stock_code} 卖出策略，数量: {volume}")
                order_id = self.trading_executor.sell_stock(stock_code, volume, price_type=0)
                
                if order_id:
                    # 记录已处理信号
                    self.processed_signals.add(signal_key)
                    return True
            
            return False
            
        except Exception as e:
            logger.error(f"执行 {stock_code} 的卖出策略时出错: {str(e)}")
            return False
    
    def check_and_execute_strategies(self, stock_code):
        """
        检查并执行所有交易策略 - 重构版本
        策略检测始终运行，但交易执行依赖ENABLE_AUTO_TRADING
        """
        try:
            # 添加调试日志
            logger.debug(f"开始检查 {stock_code} 的交易策略，自动交易状态: {config.ENABLE_AUTO_TRADING}")
            
            # 更新数据（始终执行）
            self.data_manager.update_stock_data(stock_code)
            self.indicator_calculator.calculate_all_indicators(stock_code)
            
            # 1. 检查止盈止损信号（如果启用）
            if config.ENABLE_DYNAMIC_STOP_PROFIT:
                pending_signals = self.position_manager.get_pending_signals()
                
                # 添加调试日志
                logger.debug(f"{stock_code} 待处理信号: {list(pending_signals.keys())}")
                
                if stock_code in pending_signals:
                    signal_data = pending_signals[stock_code]
                    signal_type = signal_data['type']
                    signal_info = signal_data['info']

                    # 只处理止盈信号，止损信号留到后面处理
                    if signal_type in ['take_profit_half', 'take_profit_full']:

                        logger.info(f"{stock_code} 处理待执行的{signal_type}信号")
                        
                        # 检查是否已处理过该信号（防重复,每分钟3次）
                        retry_key = f"{signal_type}_{stock_code}_{datetime.now().strftime('%Y%m%d_%H%M')}"
                        retry_count = self.retry_counts.get(retry_key, 0)
                        if retry_count >= 3:
                            logger.warning(f"{stock_code} {signal_type}信号重试次数已达上限")
                            self.position_manager.mark_signal_processed(stock_code)
                            return

                        if config.ENABLE_AUTO_TRADING:
                            # 添加调试日志
                            logger.info(f"{stock_code} 开始执行{signal_type}信号，重试次数: {retry_count}")

                            success = self.execute_trading_signal_direct(stock_code, signal_type, signal_info)

                            if success:
                                self.position_manager.mark_signal_processed(stock_code)
                                self.retry_counts.pop(retry_key, None)
                                logger.info(f"{stock_code} {signal_type}信号执行成功")
                            else:
                                self.retry_counts[retry_key] = retry_count + 1
                                logger.warning(f"{stock_code} {signal_type}执行失败，重试次数: {retry_count + 1}")

                                # 🔑 修复: 如果信号验证失败(返回False)，立即清除信号避免阻塞
                                # 检查是否是验证失败(available=0等严重错误)
                                if retry_count + 1 >= 3:
                                    logger.error(f"🚨 {stock_code} {signal_type}信号重试{retry_count + 1}次仍失败，立即清除避免阻塞其他信号")
                                    self.position_manager.mark_signal_processed(stock_code)
                                    self.retry_counts.pop(retry_key, None)
                        else:
                            logger.info(f"{stock_code} 检测到{signal_type}信号，但自动交易已关闭")
                            self.position_manager.mark_signal_processed(stock_code)
                else:
                    logger.debug(f"{stock_code} 当前无待处理信号")


            # 2. 检查补仓信号（第二优先级 - 下跌时摊平成本）
            add_position_signal, add_position_info = self.position_manager.check_add_position_signal(stock_code)
            if add_position_signal == 'add_position':
                logger.info(f"{stock_code} 检测到补仓信号")
                
                if config.ENABLE_AUTO_TRADING:
                    if self.execute_add_position_strategy(stock_code, add_position_info):
                        logger.info(f"{stock_code} 执行补仓策略成功")
                        return  # 补仓执行后直接返回
                else:
                    logger.info(f"{stock_code} 检测到补仓信号，但自动交易已关闭")

            # 3. 检查止损信号（第三优先级 - 最终风控底线）
            if config.ENABLE_DYNAMIC_STOP_PROFIT:
                pending_signals = self.position_manager.get_pending_signals()
                
                if stock_code in pending_signals:
                    signal_data = pending_signals[stock_code]
                    signal_type = signal_data['type']
                    signal_info = signal_data['info']
                    
                    # 处理止损信号
                    if signal_type == 'stop_loss':
                        logger.warning(f"{stock_code} 处理待执行的{signal_type}信号")
                        
                        if config.ENABLE_AUTO_TRADING:
                            success = self.execute_trading_signal_direct(stock_code, signal_type, signal_info)
                            if success:
                                self.position_manager.mark_signal_processed(stock_code)
                                logger.warning(f"{stock_code} {signal_type}信号执行成功")
                                return  # 止损执行后直接返回
                        else:
                            logger.warning(f"{stock_code} 检测到{signal_type}信号，但自动交易已关闭")
                            self.position_manager.mark_signal_processed(stock_code)
                            return

            # 4. 检查网格交易信号（如果启用）
            if config.ENABLE_GRID_TRADING:
                grid_signals = self.position_manager.check_grid_trade_signals(stock_code)
                if grid_signals['buy_signals'] or grid_signals['sell_signals']:
                    logger.info(f"{stock_code} 检测到网格交易信号")
                    
                    # 只有在启用自动交易时才执行
                    if config.ENABLE_AUTO_TRADING:
                        if self.execute_grid_trading(stock_code):
                            logger.info(f"{stock_code} 执行网格交易策略成功")
                            return
                    else:
                        logger.info(f"{stock_code} 检测到网格信号，但自动交易已关闭")
            
            # 5. 检查技术指标买入信号
            buy_signal = self.indicator_calculator.check_buy_signal(stock_code)
            if buy_signal:
                logger.info(f"{stock_code} 检测到买入信号")
                
                # 只有在启用自动交易时才执行
                if config.ENABLE_AUTO_TRADING:
                    if self.execute_buy_strategy(stock_code):
                        logger.info(f"{stock_code} 执行买入策略成功")
                        return
                else:
                    logger.info(f"{stock_code} 检测到买入信号，但自动交易已关闭")
            
            # 6. 检查技术指标卖出信号
            sell_signal = self.indicator_calculator.check_sell_signal(stock_code)
            if sell_signal:
                logger.info(f"{stock_code} 检测到卖出信号")
                
                # 只有在启用自动交易时才执行
                if config.ENABLE_AUTO_TRADING:
                    if self.execute_sell_strategy(stock_code):
                        logger.info(f"{stock_code} 执行卖出策略成功")
                        return
                else:
                    logger.info(f"{stock_code} 检测到卖出信号，但自动交易已关闭")
            
            logger.debug(f"{stock_code} 没有检测到交易信号")
            
        except Exception as e:
            logger.error(f"检查 {stock_code} 的交易策略时出错: {str(e)}")
    
    def start_strategy_thread(self):
        """启动策略运行线程 - 始终启动，不依赖ENABLE_AUTO_TRADING"""
        if self.strategy_thread and self.strategy_thread.is_alive():
            logger.warning("策略线程已在运行")
            return
            
        self.stop_flag = False
        self.strategy_thread = threading.Thread(target=self._strategy_loop)
        self.strategy_thread.daemon = True
        self.strategy_thread.start()
        logger.info("策略线程已启动（独立于自动交易开关）")
    
    def stop_strategy_thread(self):
        """停止策略运行线程"""
        if self.strategy_thread and self.strategy_thread.is_alive():
            self.stop_flag = True
            self.strategy_thread.join(timeout=5)
            logger.info("策略线程已停止")
    
    def _strategy_loop(self):
        """策略运行循环"""
        while not self.stop_flag:
            try:
                # 判断是否在交易时间
                if config.is_trade_time():
                    logger.info("开始执行交易策略")
                    
                    # 遍历股票池中的每只股票
                    for stock_code in config.STOCK_POOL:
                        # 检查并执行交易策略
                        self.check_and_execute_strategies(stock_code)
                        
                        # 避免请求过于频繁
                        time.sleep(1)
                    
                    logger.info("交易策略执行完成")
                
                # 等待下一次策略执行
                for _ in range(10):  # 每10s执行一次策略
                    if self.stop_flag:
                        break
                    time.sleep(1)
                    
            except Exception as e:
                logger.error(f"策略循环出错: {str(e)}")
                time.sleep(60)  # 出错后等待一分钟再继续
    
    def manual_buy(self, stock_code, volume=None, price=None, amount=None):
        """
        手动买入股票 - 不受ENABLE_AUTO_TRADING限制
        """
        try:
            # 手动交易不检查ENABLE_AUTO_TRADING，但要检查ENABLE_ALLOW_BUY
            if not config.ENABLE_ALLOW_BUY:
                logger.warning(f"系统当前不允许买入操作")
                return None

            # 根据交易模式选择策略标识
            is_simulation = hasattr(config, 'ENABLE_SIMULATION_MODE') and config.ENABLE_SIMULATION_MODE
            strategy = 'M_simu' if is_simulation else 'M_real'

            order_id = self.trading_executor.buy_stock(
                stock_code, volume, price, amount, strategy=strategy
            )
            
            if order_id:
                logger.info(f"手动买入 {stock_code} 成功，委托号: {order_id}，模式: {'模拟' if is_simulation else '实盘'}")
            
            return order_id
            
        except Exception as e:
            logger.error(f"手动买入 {stock_code} 时出错: {str(e)}")
            return None
    
    def manual_sell(self, stock_code, volume=None, price=None, ratio=None):
        """
        手动卖出股票 - 不受ENABLE_AUTO_TRADING限制
        """
        try:
            # 手动交易不检查ENABLE_AUTO_TRADING，但要检查ENABLE_ALLOW_SELL
            if not config.ENABLE_ALLOW_SELL:
                logger.warning(f"系统当前不允许卖出操作")
                return None

            # 根据交易模式选择策略标识
            is_simulation = hasattr(config, 'ENABLE_SIMULATION_MODE') and config.ENABLE_SIMULATION_MODE
            strategy = 'manual_simu' if is_simulation else 'manual_real'

            order_id = self.trading_executor.sell_stock(
                stock_code, volume, price, ratio, strategy=strategy
            )
            
            if order_id:
                logger.info(f"手动卖出 {stock_code} 成功，委托号: {order_id}，模式: {'模拟' if is_simulation else '实盘'}")
            
            return order_id
            
        except Exception as e:
            logger.error(f"手动卖出 {stock_code} 时出错: {str(e)}")
            return None


# 单例模式
_instance = None

def get_trading_strategy():
    """获取TradingStrategy单例"""
    global _instance
    if _instance is None:
        _instance = TradingStrategy()
    return _instance            