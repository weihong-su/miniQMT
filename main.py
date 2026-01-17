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
from thread_monitor import get_thread_monitor

# 获取logger
logger = get_logger("main")

# 全局变量
threads = []
stop_event = threading.Event()

def signal_handler(sig, frame):
    """信号处理函数，用于捕获退出信号"""
    logger.info("收到退出信号")
    stop_event.set()
    sys.exit(0)

def load_persisted_configs():
    """从数据库加载持久化配置"""
    logger.info("加载持久化配置")
    try:
        config_manager = get_config_manager()
        applied_count = config_manager.apply_configs_to_runtime()
        logger.info(f"✓ 配置{applied_count}项")
        return applied_count
    except Exception as e:
        logger.error(f"配置加载失败:{str(e)[:30]}")
        return 0

def init_system():
    """初始化系统"""
    logger.info("系统初始化")

    # 创建数据目录
    if not os.path.exists(config.DATA_DIR):
        os.makedirs(config.DATA_DIR)
        logger.info(f"✓ 创建目录:{config.DATA_DIR}")

    # 加载持久化配置（在初始化其他模块之前）
    load_persisted_configs()

    # 获取各个模块的实例
    data_manager = get_data_manager()
    indicator_calculator = get_indicator_calculator()
    position_manager = get_position_manager()
    trading_executor = get_trading_executor()
    trading_strategy = get_trading_strategy()

    logger.info("✓ 系统初始化完成")
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
        logger.info("启动持仓监控")
        position_manager.start_position_monitor_thread()

        # 🔑 验证线程启动
        time.sleep(0.5)  # 等待线程启动
        if position_manager.monitor_thread and position_manager.monitor_thread.is_alive():
            logger.info("✅ 持仓监控已启动")
        else:
            logger.error("❌ 持仓监控启动失败")

        threads.append(("position_thread", position_manager.stop_position_monitor_thread))
    else:
        logger.warning("⚠️ 持仓监控未启用")

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
    # 使用shutdown_web_server进行资源清理
    from web_server import shutdown_web_server
    threads.append(("web_thread", shutdown_web_server))

def download_initial_data(data_manager):
    """下载初始数据"""
    logger.info("下载初始数据")
    for stock_code in config.STOCK_POOL:
        try:
            logger.info(f"下载 {stock_code[:6]} 历史数据")
            data_df = data_manager.download_history_data(stock_code)
            if data_df is not None and not data_df.empty:
                data_manager.save_history_data(stock_code, data_df)
            # 避免请求过于频繁
            time.sleep(1)
        except Exception as e:
            logger.error(f"下载 {stock_code[:6]} 失败:{str(e)[:30]}")
    logger.info("初始数据下载完成")

def calculate_initial_indicators(indicator_calculator):
    """计算初始指标"""
    logger.info("计算初始指标")
    indicator_calculator.update_all_stock_indicators()
    logger.info("初始指标计算完成")

def cleanup():
    """清理资源 - 优雅关闭版本"""
    logger.info("清理资源")

    # 第1步: 先停止Web服务器(避免在关闭数据库后仍有请求)
    for thread_name, stop_func in threads:
        if thread_name == "web_thread":
            try:
                logger.info("停止Web服务器")
                stop_func()
            except Exception as e:
                logger.error(f"Web服务器停止失败:{str(e)[:30]}")
            break

    # 第2步: 停止线程监控器(如果启用)
    if config.ENABLE_THREAD_MONITOR:
        try:
            logger.info("停止线程监控")
            thread_monitor = get_thread_monitor()
            thread_monitor.stop()
        except Exception as e:
            logger.error(f"线程监控停止失败:{str(e)[:30]}")

    # 第3步: 停止其他业务线程
    for thread_name, stop_func in threads:
        if thread_name == "web_thread":
            continue  # 已经停止
        try:
            logger.info(f"停止{thread_name}")
            stop_func()
        except Exception as e:
            logger.error(f"{thread_name}停止失败:{str(e)[:30]}")

    # 第4步: 关闭各个模块(按依赖顺序)
    try:
        trading_strategy = get_trading_strategy()
        trading_strategy.close()
    except Exception as e:
        logger.error(f"策略关闭失败:{str(e)[:30]}")

    try:
        trading_executor = get_trading_executor()
        trading_executor.close()
    except Exception as e:
        logger.error(f"执行器关闭失败:{str(e)[:30]}")

    try:
        data_manager = get_data_manager()
        data_manager.close()
    except Exception as e:
        logger.error(f"数据管理器关闭失败:{str(e)[:30]}")

    logger.info("✓ 资源清理完成")

def main():
    """主函数"""
    try:
        logger.info("=" * 50)
        logger.info(f"QMT系统启动 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
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

        # ============ 新增: 启动盘前同步调度器 ============
        from premarket_sync import start_premarket_sync_scheduler
        start_premarket_sync_scheduler()
        logger.info("✓ 盘前同步调度器已启动")

        # ============ 新增: 启动线程健康监控 ============
        if config.ENABLE_THREAD_MONITOR:
            thread_monitor = get_thread_monitor()

            # 注册持仓监控线程
            thread_monitor.register_thread(
                "持仓监控线程",
                lambda: position_manager.monitor_thread,
                position_manager.start_position_monitor_thread
            )

            # 注册数据更新线程
            thread_monitor.register_thread(
                "数据更新线程",
                lambda: data_manager.update_thread,
                data_manager.start_data_update_thread
            )

            # 注册策略线程
            thread_monitor.register_thread(
                "策略线程",
                lambda: trading_strategy.strategy_thread,
                trading_strategy.start_strategy_thread
            )

            # 启动监控
            thread_monitor.start()
            logger.info("✅ 线程监控已启动")

        # ============ 新增: 启动卖出监控器 ============
        if hasattr(config, 'ENABLE_SELL_MONITOR') and config.ENABLE_SELL_MONITOR:
            try:
                from sell_monitor import get_sell_monitor
                sell_monitor = get_sell_monitor()
                logger.info("✅ 卖出监控器已启动")
                logger.info(f"   监控:{'启用' if sell_monitor.monitoring_enabled else '禁用'}")
                logger.info(f"   告警:{'启用' if config.ENABLE_SELL_ALERT_NOTIFICATION else '禁用'}")
            except Exception as e:
                logger.warning(f"⚠️ 卖出监控器失败:{str(e)[:30]}")
                logger.info("系统继续运行")

        # 最后启动Web服务器
        start_web_server_thread()

        # 等待退出信号
        logger.info("✅ 系统启动完成")
        while not stop_event.is_set():
            time.sleep(1)

    except Exception as e:
        logger.error(f"系统运行出错:{str(e)[:30]}")
    finally:
        cleanup()
        logger.info("系统已退出")

if __name__ == "__main__":
    main()
