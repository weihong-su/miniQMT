"""
QMT量化交易系统主程序
"""
import os
import time
import threading
import signal
import sys
from datetime import datetime

import config
from logger import get_logger, schedule_log_cleanup, clean_old_logs
from data_manager import get_data_manager
from indicator_calculator import get_indicator_calculator
from position_manager import get_position_manager
from trading_executor import get_trading_executor
from strategy import get_trading_strategy
from web_server import start_web_server
from config_manager import get_config_manager

# 获取logger
logger = get_logger("main")

# 全局变量
threads = []
stop_event = threading.Event()

def signal_handler(sig, frame):
    """信号处理函数，用于捕获退出信号"""
    logger.info("接收到退出信号，开始清理...")
    stop_event.set()
    sys.exit(0)

def load_persisted_configs():
    """从数据库加载持久化配置"""
    logger.info("开始加载持久化配置...")
    try:
        config_manager = get_config_manager()
        applied_count = config_manager.apply_configs_to_runtime()
        logger.info(f"成功加载并应用 {applied_count} 个持久化配置")
        return applied_count
    except Exception as e:
        logger.error(f"加载持久化配置失败: {str(e)}")
        return 0

def init_system():
    """初始化系统"""
    logger.info("开始初始化系统...")

    # 创建数据目录
    if not os.path.exists(config.DATA_DIR):
        os.makedirs(config.DATA_DIR)
        logger.info(f"创建数据目录: {config.DATA_DIR}")

    # 加载持久化配置（在初始化其他模块之前）
    load_persisted_configs()

    # 获取各个模块的实例
    data_manager = get_data_manager()
    indicator_calculator = get_indicator_calculator()
    position_manager = get_position_manager()
    trading_executor = get_trading_executor()
    trading_strategy = get_trading_strategy()

    logger.info("系统初始化完成")
    return data_manager, indicator_calculator, position_manager, trading_executor, trading_strategy

def start_data_thread(data_manager):
    """启动数据更新线程"""
    if config.ENABLE_DATA_SYNC:
        logger.info("启动数据更新线程")
        data_manager.start_data_update_thread()
        threads.append(("data_thread", data_manager.stop_data_update_thread))

def start_position_thread(position_manager):
    """启动持仓监控线程"""
    if config.ENABLE_POSITION_MONITOR:
        logger.info("启动持仓监控线程")
        position_manager.start_position_monitor_thread()
        threads.append(("position_thread", position_manager.stop_position_monitor_thread))

def start_strategy_thread(trading_strategy):
    """启动策略线程"""
    # if config.ENABLE_AUTO_TRADING:
    logger.info("启动策略线程")
    trading_strategy.start_strategy_thread()
    threads.append(("strategy_thread", trading_strategy.stop_strategy_thread))

def start_log_cleanup_thread():
    """启动日志清理线程"""
    if config.ENABLE_LOG_CLEANUP:
        logger.info("启动日志清理线程")
        log_thread = threading.Thread(target=schedule_log_cleanup)
        log_thread.daemon = True
        log_thread.start()
        threads.append(("log_thread", lambda: None))  # 没有停止函数，依赖于daemon=True

def start_web_server_thread():
    """启动Web服务器线程"""
    logger.info("启动Web服务器线程")
    web_thread = threading.Thread(target=start_web_server)
    web_thread.daemon = True
    web_thread.start()
    threads.append(("web_thread", lambda: None))  # 没有停止函数，依赖于daemon=True

def download_initial_data(data_manager):
    """下载初始数据"""
    logger.info("开始下载初始数据...")
    for stock_code in config.STOCK_POOL:
        try:
            logger.info(f"下载 {stock_code} 的历史数据")
            data_df = data_manager.download_history_data(stock_code)
            if data_df is not None and not data_df.empty:
                data_manager.save_history_data(stock_code, data_df)
            # 避免请求过于频繁
            time.sleep(1)
        except Exception as e:
            logger.error(f"下载 {stock_code} 的历史数据时出错: {str(e)}")
    logger.info("初始数据下载完成")

def calculate_initial_indicators(indicator_calculator):
    """计算初始指标"""
    logger.info("开始计算初始指标...")
    indicator_calculator.update_all_stock_indicators()
    logger.info("初始指标计算完成")

def cleanup():
    """清理资源"""
    logger.info("开始清理资源...")
    
    # 停止所有线程
    for thread_name, stop_func in threads:
        try:
            logger.info(f"停止 {thread_name}...")
            stop_func()
        except Exception as e:
            logger.error(f"停止 {thread_name} 时出错: {str(e)}")
    
    # 关闭各个模块
    try:
        trading_strategy = get_trading_strategy()
        trading_strategy.close()
    except Exception as e:
        logger.error(f"关闭交易策略时出错: {str(e)}")
    
    try:
        trading_executor = get_trading_executor()
        trading_executor.close()
    except Exception as e:
        logger.error(f"关闭交易执行器时出错: {str(e)}")
    
    try:
        data_manager = get_data_manager()
        data_manager.close()
    except Exception as e:
        logger.error(f"关闭数据管理器时出错: {str(e)}")
    
    logger.info("资源清理完成")

def main():
    """主函数"""
    try:
        logger.info("=" * 50)
        logger.info(f"= QMT量化交易系统启动 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ")
        logger.info("=" * 50)
        
        # 设置信号处理
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # 初始化系统
        data_manager, indicator_calculator, position_manager, trading_executor, trading_strategy = init_system()
        
        # 下载初始数据
        download_initial_data(data_manager)
        
        # 计算初始指标
        calculate_initial_indicators(indicator_calculator)
        
        # 启动各个线程
        start_data_thread(data_manager)
        start_position_thread(position_manager)
        start_strategy_thread(trading_strategy)
        start_log_cleanup_thread()
        
        # 最后启动Web服务器
        start_web_server_thread()
        
        # 等待退出信号
        logger.info("系统启动完成，按 Ctrl+C 退出")
        while not stop_event.is_set():
            time.sleep(1)
            
    except Exception as e:
        logger.error(f"系统运行时出错: {str(e)}")
    finally:
        cleanup()
        logger.info("系统已退出")

if __name__ == "__main__":
    main()
