"""
QMT量化交易系统主程序
"""
import os
import time
import threading
import signal
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
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
system_start_time = None  # 系统启动时间
heartbeat_thread = None  # 心跳日志线程

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

def heartbeat_logger():
    """系统心跳日志 - 定期输出运行状态摘要"""
    while not stop_event.is_set():
        try:
            # 等待指定间隔
            if stop_event.wait(config.HEARTBEAT_INTERVAL):
                break  # 收到停止信号

            # 计算运行时长
            if system_start_time:
                uptime = datetime.now() - system_start_time
                hours, remainder = divmod(int(uptime.total_seconds()), 3600)
                minutes, seconds = divmod(remainder, 60)
                uptime_str = f"{hours}h{minutes}m{seconds}s"
            else:
                uptime_str = "未知"

            # 获取持仓信息
            try:
                position_manager = get_position_manager()
                positions = position_manager.get_all_positions()
                position_count = 0 if positions is None or positions.empty else len(positions)

                # 获取账户信息
                account_info = position_manager.get_account_info()
                if account_info:
                    total_asset = account_info.get('total_asset', 0)
                    available = account_info.get('available', 0)
                    asset_str = f"总资产:{total_asset:.2f} 可用:{available:.2f}"
                else:
                    asset_str = "资产信息获取失败"
            except Exception as e:
                position_count = "获取失败"
                asset_str = f"获取失败:{str(e)[:20]}"

            # 输出心跳日志
            logger.info("=" * 50)
            logger.info(f"💓 系统心跳 - 运行时长:{uptime_str}")
            logger.info(f"   模式:{'模拟' if config.ENABLE_SIMULATION_MODE else '实盘'} | "
                       f"自动交易:{'开启' if config.ENABLE_AUTO_TRADING else '关闭'} | "
                       f"网格交易:{'开启' if config.ENABLE_GRID_TRADING else '关闭'}")
            logger.info(f"   持仓数量:{position_count} | {asset_str}")
            logger.info("=" * 50)

        except Exception as e:
            logger.error(f"心跳日志出错:{str(e)[:50]}")
            time.sleep(60)  # 出错后等待一分钟再继续

def start_heartbeat_logger():
    """启动心跳日志线程"""
    global heartbeat_thread, system_start_time

    if not config.ENABLE_HEARTBEAT_LOG:
        logger.info("心跳日志未启用 (ENABLE_HEARTBEAT_LOG=False)")
        return

    system_start_time = datetime.now()
    heartbeat_thread = threading.Thread(target=heartbeat_logger, daemon=True, name="HeartbeatLogger")
    heartbeat_thread.start()
    logger.info(f"✅ 心跳日志已启动 (间隔:{config.HEARTBEAT_INTERVAL}秒)")

def stop_heartbeat_logger():
    """停止心跳日志线程"""
    global heartbeat_thread
    if heartbeat_thread and heartbeat_thread.is_alive():
        logger.info("停止心跳日志线程")
        stop_event.set()
        heartbeat_thread.join(timeout=2)

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

def start_web_server_thread(position_manager):
    """启动Web服务器线程

    Args:
        position_manager: 已初始化的position_manager实例
    """
    logger.info("启动Web服务器线程")
    # logger.info(f"[DEBUG main.py] 传入Web服务器的position_manager id: {id(position_manager)}")

    # 创建线程并传入position_manager
    web_thread = threading.Thread(target=lambda: start_web_server(position_manager))
    web_thread.daemon = True
    web_thread.start()

    # 使用shutdown_web_server进行资源清理
    from web_server import shutdown_web_server
    threads.append(("web_thread", shutdown_web_server))

def download_initial_data(data_manager):
    """下载初始数据，每只股票有超时保护"""
    logger.info("下载初始数据")
    timeout = config.HISTORY_DATA_DOWNLOAD_TIMEOUT
    for stock_code in config.STOCK_POOL:
        try:
            logger.info(f"下载 {stock_code[:6]} 历史数据（超时{timeout}秒）")
            executor = ThreadPoolExecutor(max_workers=1)
            future = executor.submit(data_manager.download_history_data, stock_code)
            try:
                data_df = future.result(timeout=timeout)
                if data_df is not None and not data_df.empty:
                    data_manager.save_history_data(stock_code, data_df)
            except FuturesTimeoutError:
                logger.warning(f"下载 {stock_code[:6]} 超时（>{timeout}秒），跳过")
            finally:
                executor.shutdown(wait=False)  # 不阻塞等待后台线程，立即继续下一只
        except Exception as e:
            logger.error(f"下载 {stock_code[:6]} 失败:{str(e)[:30]}")
        # 避免请求过于频繁
        time.sleep(1)
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

    # 第2.5步: 停止心跳日志线程
    try:
        stop_heartbeat_logger()
    except Exception as e:
        logger.error(f"心跳日志停止失败:{str(e)[:30]}")

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

        # 初始化网格交易管理器
        logger.info(f"检查网格交易配置: ENABLE_GRID_TRADING = {config.ENABLE_GRID_TRADING}")
        # logger.info(f"[DEBUG main.py] position_manager id: {id(position_manager)}")
        # logger.info(f"[DEBUG main.py] position_manager有grid_manager属性: {hasattr(position_manager, 'grid_manager')}")

        if config.ENABLE_GRID_TRADING:
            try:
                logger.info("初始化网格交易管理器")
                # logger.info(f"[DEBUG main.py] 调用init_grid_manager前，grid_manager={getattr(position_manager, 'grid_manager', 'NO_ATTR')}")
                position_manager.init_grid_manager(trading_executor)
                # logger.info(f"[DEBUG main.py] 调用init_grid_manager后，grid_manager={position_manager.grid_manager}")
                # logger.info(f"[DEBUG main.py] grid_manager类型: {type(position_manager.grid_manager)}")
                logger.info("✓ 网格交易管理器初始化完成")

                # ⚠️ 新增: 初始化风险等级模板(仅首次运行或更新时)
                try:
                    logger.info("开始初始化风险等级模板...")
                    # 从position_manager获取db_manager
                    db_mgr = position_manager.db_manager
                    initialized_count = db_mgr.init_risk_level_templates()
                    if initialized_count > 0:
                        logger.info(f"✓ 新增 {initialized_count} 个风险等级模板")
                    else:
                        logger.info("✓ 风险等级模板已存在,无需初始化")
                except Exception as e:
                    logger.warning(f"初始化风险等级模板失败(不影响系统运行): {str(e)}")

            except Exception as e:
                logger.error(f"网格交易管理器初始化失败: {str(e)}")
                logger.info("系统继续运行(网格交易功能不可用)")
        else:
            logger.warning("网格交易功能未启用 (ENABLE_GRID_TRADING=False)")

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

        # ============ 新增: 启动系统心跳日志 ============
        start_heartbeat_logger()

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

        # 最后启动Web服务器，传入已初始化的position_manager
        start_web_server_thread(position_manager)

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
