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

# 导入卖出监控器 (容错处理)
try:
    from sell_monitor import record_sell_attempt
    SELL_MONITOR_ENABLED = True
except ImportError:
    SELL_MONITOR_ENABLED = False
    def record_sell_attempt(*args, **kwargs):
        pass  # 空函数

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

        # 🔒 线程安全：添加锁保护共享数据 (修复C1)
        self.signal_lock = threading.Lock()

        # 已处理的止盈止损信号记录
        self.processed_signals = set()

        # 添加这行 - 重试计数器
        self.retry_counts = {}
    
    # ===== 旧的网格交易方法已废弃，请使用GridTradingManager =====
    # init_grid_trading(), execute_grid_trading()
    # 已被grid_trading_manager.py中的GridTradingManager替代

    

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
            # ✅ 修复C2: 删除重复验证，信号验证已在execute_trading_signal_direct()中完成
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
                # 🔑 新增：传递信号信息用于委托单跟踪
                order_id = self.trading_executor.sell_stock(
                    stock_code, volume, price_type=5, strategy='stop_loss',
                    signal_type='stop_loss',
                    signal_info={
                        'current_price': current_price,
                        'cost_price': signal_info.get('cost_price', 0),
                        'volume': volume,
                        'loss_ratio': signal_info.get('loss_ratio', 0)
                    }
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
            # ✅ 修复C2: 删除重复验证，信号验证已在execute_trading_signal_direct()中完成

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
                # 🔑 新增：传递信号信息用于委托单跟踪
                order_id = self.trading_executor.sell_stock(
                    stock_code, sell_volume, price_type=5, strategy='auto_partial',
                    signal_type='take_profit_half',
                    signal_info={
                        'current_price': current_price,
                        'cost_price': signal_info.get('cost_price', 0),
                        'volume': sell_volume,
                        'sell_ratio': sell_ratio,
                        'breakout_highest_price': breakout_highest_price,
                        'pullback_ratio': pullback_ratio
                    }
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
            # ✅ 修复C2: 删除重复验证，信号验证已在execute_trading_signal_direct()中完成

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
                # 🔑 新增：传递信号信息用于委托单跟踪
                order_id = self.trading_executor.sell_stock(
                    stock_code, volume, price_type=5, strategy='auto_full',
                    signal_type='take_profit_full',
                    signal_info={
                        'current_price': current_price,
                        'cost_price': signal_info.get('cost_price', 0),
                        'volume': volume,
                        'dynamic_take_profit_price': dynamic_take_profit_price,
                        'highest_price': signal_info.get('highest_price', 0)
                    }
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

                    # 🔑 注意: execute_buy_strategy()仅处理技术指标买入信号的首次建仓
                    # 补仓策略已由position_manager.check_add_position_signal()独立处理
                    logger.info(f"{stock_code} 已有持仓，技术指标买入信号不触发补仓（补仓由独立策略处理）")
                    return False
                else:
                    # 新建仓，使用POSITION_UNIT作为首次建仓金额
                    buy_amount = config.POSITION_UNIT
                    logger.info(f"执行 {stock_code} 首次建仓，金额: {buy_amount}")
                
                # 执行买入
                order_id = self.trading_executor.buy_stock(stock_code, amount=buy_amount, price_type=0)
                
                if order_id:
                    # 记录已处理信号
                    self.processed_signals.add(signal_key)

                    # 旧的网格交易初始化已废弃，请使用GridTradingManager

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
        检查并执行所有交易策略 - 修复版本
        策略检测始终运行，但交易执行依赖ENABLE_AUTO_TRADING

        修复说明:
        - 调整信号处理优先级: 止损 > 止盈 > 补仓 > 其他
        - 止损作为最高优先级,确保风控底线
        - 补仓前检查是否有止损信号,避免冲突
        """
        try:
            # 添加调试日志
            logger.debug(f"开始检查 {stock_code} 的交易策略，自动交易状态: {config.ENABLE_AUTO_TRADING}")

            # 更新数据（始终执行）
            self.data_manager.update_stock_data(stock_code)
            self.indicator_calculator.calculate_all_indicators(stock_code)

            # ========== 🔑 动态优先级信号处理 - 根据配置参数自动调整执行顺序 ==========
            # 获取动态优先级信息
            priority_info = config.determine_stop_loss_add_position_priority()
            priority_mode = priority_info['priority']
            scenario = priority_info['scenario']

            # 场景A: 补仓优先 (补仓阈值 < 止损阈值, 例如补仓5% < 止损7%)
            # 执行顺序: 止盈 → 补仓 → 止损
            if priority_mode == 'add_position_first':
                logger.debug(f"【场景{scenario}】使用补仓优先策略: 止盈 → 补仓 → 止损")

                # 1️⃣ 止盈信号处理（第一优先级）
                if config.ENABLE_DYNAMIC_STOP_PROFIT:
                    pending_signals = self.position_manager.get_pending_signals()
                    if stock_code in pending_signals:
                        signal_data = pending_signals[stock_code]
                        signal_type = signal_data['type']
                        signal_info = signal_data['info']

                        if signal_type in ['take_profit_half', 'take_profit_full']:
                            logger.info(f"{stock_code} 处理待执行的{signal_type}信号")
                            retry_key = f"{signal_type}_{stock_code}_{datetime.now().strftime('%Y%m%d_%H%M')}"

                            # 🔒 线程安全：使用锁保护retry_counts访问 (修复C1)
                            with self.signal_lock:
                                retry_count = self.retry_counts.get(retry_key, 0)

                                if retry_count >= 3:
                                    logger.warning(f"{stock_code} {signal_type}信号重试次数已达上限")
                                    self.position_manager.mark_signal_processed(stock_code)
                                    return

                            if config.ENABLE_AUTO_TRADING:
                                success = self.execute_trading_signal_direct(stock_code, signal_type, signal_info)
                                if success:
                                    self.position_manager.mark_signal_processed(stock_code)
                                    # 🔒 线程安全：使用锁保护retry_counts访问 (修复C1)
                                    with self.signal_lock:
                                        self.retry_counts.pop(retry_key, None)
                                    logger.info(f"{stock_code} {signal_type}信号执行成功")
                                    return  # 止盈执行成功后直接返回
                                else:
                                    # 🔒 线程安全：使用锁保护retry_counts访问 (修复C1)
                                    with self.signal_lock:
                                        self.retry_counts[retry_key] = retry_count + 1
                                        if retry_count + 1 >= 3:
                                            logger.error(f"🚨 {stock_code} {signal_type}信号重试{retry_count + 1}次仍失败，立即清除")
                                            self.position_manager.mark_signal_processed(stock_code)
                                            self.retry_counts.pop(retry_key, None)
                            else:
                                logger.info(f"{stock_code} 检测到{signal_type}信号，但自动交易已关闭")
                                self.position_manager.mark_signal_processed(stock_code)

                # 2️⃣ 补仓信号处理（第二优先级）
                add_position_signal, add_position_info = self.position_manager.check_add_position_signal(stock_code)
                if add_position_signal == 'add_position':
                    logger.info(f"✅ 【场景{scenario}】{stock_code} 检测到补仓信号")

                    if config.ENABLE_AUTO_TRADING:
                        if self.execute_add_position_strategy(stock_code, add_position_info):
                            logger.info(f"{stock_code} 执行补仓策略成功")
                            return  # 补仓执行后直接返回
                    else:
                        logger.info(f"{stock_code} 检测到补仓信号，但自动交易已关闭")

                # 3️⃣ 止损信号处理（第三优先级 - 仅在仓位已满时触发）
                if config.ENABLE_DYNAMIC_STOP_PROFIT:
                    pending_signals = self.position_manager.get_pending_signals()
                    if stock_code in pending_signals:
                        signal_data = pending_signals[stock_code]
                        signal_type = signal_data['type']
                        signal_info = signal_data['info']

                        if signal_type == 'stop_loss':
                            logger.warning(f"⚠️  【场景{scenario}】{stock_code} 检测到止损信号(仓位已满)")

                            if config.ENABLE_AUTO_TRADING:
                                success = self.execute_trading_signal_direct(stock_code, signal_type, signal_info)
                                if success:
                                    self.position_manager.mark_signal_processed(stock_code)
                                    logger.warning(f"✅ {stock_code} 止损信号执行成功")
                                    return
                                else:
                                    logger.error(f"❌ {stock_code} 止损信号执行失败")
                                    return
                            else:
                                logger.warning(f"{stock_code} 检测到止损信号，但自动交易已关闭")
                                self.position_manager.mark_signal_processed(stock_code)
                                return

            # 场景B: 止损优先 (止损阈值 <= 补仓阈值, 例如止损5% <= 补仓7%)
            # 执行顺序: 止损 → 止盈 → (永不补仓)
            elif priority_mode == 'stop_loss_first':
                logger.debug(f"【场景{scenario}】使用止损优先策略: 止损 → 止盈 → (永不补仓)")

                # 1️⃣ 止损信号处理（最高优先级）
                if config.ENABLE_DYNAMIC_STOP_PROFIT:
                    pending_signals = self.position_manager.get_pending_signals()
                    if stock_code in pending_signals:
                        signal_data = pending_signals[stock_code]
                        signal_type = signal_data['type']
                        signal_info = signal_data['info']

                        if signal_type == 'stop_loss':
                            logger.warning(f"⚠️  【场景{scenario}】{stock_code} 检测到止损信号(最高优先级)，立即处理")

                            if config.ENABLE_AUTO_TRADING:
                                success = self.execute_trading_signal_direct(stock_code, signal_type, signal_info)
                                if success:
                                    self.position_manager.mark_signal_processed(stock_code)
                                    logger.warning(f"✅ {stock_code} 止损信号执行成功，跳过其他策略")
                                    return  # 止损执行后直接返回
                                else:
                                    logger.error(f"❌ {stock_code} 止损信号执行失败")
                                    return
                            else:
                                logger.warning(f"{stock_code} 检测到止损信号，但自动交易已关闭")
                                self.position_manager.mark_signal_processed(stock_code)
                                return

                # 2️⃣ 止盈信号处理（第二优先级）
                if config.ENABLE_DYNAMIC_STOP_PROFIT:
                    pending_signals = self.position_manager.get_pending_signals()
                    if stock_code in pending_signals:
                        signal_data = pending_signals[stock_code]
                        signal_type = signal_data['type']
                        signal_info = signal_data['info']

                        if signal_type in ['take_profit_half', 'take_profit_full']:
                            logger.info(f"{stock_code} 处理待执行的{signal_type}信号")
                            retry_key = f"{signal_type}_{stock_code}_{datetime.now().strftime('%Y%m%d_%H%M')}"

                            # 🔒 线程安全：使用锁保护retry_counts访问 (修复C1)
                            with self.signal_lock:
                                retry_count = self.retry_counts.get(retry_key, 0)

                                if retry_count >= 3:
                                    logger.warning(f"{stock_code} {signal_type}信号重试次数已达上限")
                                    self.position_manager.mark_signal_processed(stock_code)
                                    return

                            if config.ENABLE_AUTO_TRADING:
                                success = self.execute_trading_signal_direct(stock_code, signal_type, signal_info)
                                if success:
                                    self.position_manager.mark_signal_processed(stock_code)
                                    # 🔒 线程安全：使用锁保护retry_counts访问 (修复C1)
                                    with self.signal_lock:
                                        self.retry_counts.pop(retry_key, None)
                                    logger.info(f"{stock_code} {signal_type}信号执行成功")
                                    return
                                else:
                                    # 🔒 线程安全：使用锁保护retry_counts访问 (修复C1)
                                    with self.signal_lock:
                                        self.retry_counts[retry_key] = retry_count + 1
                                        if retry_count + 1 >= 3:
                                            logger.error(f"🚨 {stock_code} {signal_type}信号重试{retry_count + 1}次仍失败，立即清除")
                                            self.position_manager.mark_signal_processed(stock_code)
                                            self.retry_counts.pop(retry_key, None)
                            else:
                                logger.info(f"{stock_code} 检测到{signal_type}信号，但自动交易已关闭")
                                self.position_manager.mark_signal_processed(stock_code)

                # 3️⃣ 补仓信号 - 在场景B中永远不会触发
                # check_add_position_signal() 已在 position_manager 中拒绝补仓
                logger.debug(f"【场景{scenario}】补仓功能已禁用(止损优先策略)")

            # 4. 检查网格交易信号（如果启用）
            if config.ENABLE_GRID_TRADING and self.position_manager.grid_manager:
                # 从信号队列中获取网格信号
                pending_signals = self.position_manager.get_pending_signals()
                logger.debug(f"[GRID-STRATEGY] 检查网格信号: pending_signals中有 {len(pending_signals)} 个待处理信号")

                if stock_code in pending_signals:
                    signal_data = pending_signals[stock_code]
                    signal_type = signal_data['type']
                    signal_info = signal_data['info']

                    # 检查是否为网格交易信号
                    if signal_type in ['grid_buy', 'grid_sell', 'grid_exit']:
                        logger.info(f"[GRID-STRATEGY] {stock_code} 检测到网格交易信号: signal_type={signal_type}, session_id={signal_info.get('session_id', 'N/A')}, 价格={signal_info.get('trigger_price', 'N/A')}")

                        # ⭐ 检查网格交易开关状态
                        if not config.ENABLE_GRID_TRADING:
                            logger.info(f"[GRID-STRATEGY] {stock_code} 网格交易已关闭,清除残留信号 {signal_type}")
                            self.position_manager.mark_signal_processed(stock_code)
                            return

                        # ⭐ 网格交易使用独立开关 ENABLE_GRID_TRADING 控制执行
                        # 与 ENABLE_AUTO_TRADING（止盈止损开关）互不影响
                        try:
                            logger.debug(f"[GRID-STRATEGY] 开始执行网格交易: {stock_code}, signal_type={signal_type}, session_id={signal_info.get('session_id', 'N/A')}")
                            success = self.position_manager.grid_manager.execute_grid_trade(signal_info)
                            if success:
                                self.position_manager.mark_signal_processed(stock_code)
                                logger.info(f"[GRID-STRATEGY] {stock_code} 网格交易执行成功: signal_type={signal_type}, session_id={signal_info.get('session_id', 'N/A')}")
                                return
                            else:
                                logger.error(f"[GRID-STRATEGY] {stock_code} 网格交易执行失败: signal_type={signal_type}, session_id={signal_info.get('session_id', 'N/A')}")
                        except Exception as e:
                            logger.error(f"[GRID-STRATEGY] {stock_code} 网格交易执行异常: signal_type={signal_type}, session_id={signal_info.get('session_id', 'N/A')}, 错误={str(e)}")

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
        """策略运行循环 - 修复版本: 优先处理所有持仓股票"""
        while not self.stop_flag:
            try:
                # 判断是否在交易时间
                if config.is_trade_time():
                    if config.VERBOSE_LOOP_LOGGING or config.DEBUG:
                        logger.debug("开始执行交易策略")

                    # 🔑 修复: 优先处理所有持仓股票 (止盈止损信号优先级最高)
                    positions = self.position_manager.get_all_positions()
                    processed_stocks = set()

                    if positions is not None and not positions.empty:
                        logger.debug(f"处理 {len(positions)} 只持仓股票的信号")
                        # ✅ 修复: 直接提取股票代码列表进行迭代
                        stock_codes = positions['stock_code'].tolist()
                        for stock_code in stock_codes:
                            self.check_and_execute_strategies(stock_code)
                            processed_stocks.add(stock_code)
                            time.sleep(1)

                    # 再处理STOCK_POOL中的其他股票 (买入信号等)
                    for stock_code in config.STOCK_POOL:
                        if stock_code not in processed_stocks:
                            self.check_and_execute_strategies(stock_code)
                            time.sleep(1)

                    if config.VERBOSE_LOOP_LOGGING or config.DEBUG:
                        logger.debug("交易策略执行完成")

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

    def close(self):
        """关闭策略，释放资源"""
        try:
            logger.info("正在关闭交易策略...")
            # 策略线程已经通过stop_strategy_thread()停止
            # 这里只需要清理资源
            # 🔒 线程安全：使用锁保护共享数据清理 (修复C1)
            with self.signal_lock:
                self.processed_signals.clear()
                self.retry_counts.clear()
            self.last_trade_time.clear()
            logger.info("交易策略已关闭")
        except Exception as e:
            logger.error(f"关闭交易策略时出错: {str(e)}")


# 单例模式
_instance = None

def get_trading_strategy():
    """获取TradingStrategy单例"""
    global _instance
    if _instance is None:
        _instance = TradingStrategy()
    return _instance            