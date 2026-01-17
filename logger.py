"""
日志管理模块，提供日志记录和清理功能
"""
import os
import logging
from logging.handlers import RotatingFileHandler
import time
from datetime import datetime, timedelta
import glob
import config

# 创建日志目录
if not os.path.exists('logs'):
    os.makedirs('logs')

# 日志文件路径
log_file = os.path.join('logs', config.LOG_FILE)

# 模块名称映射(精简日志输出)
MODULE_NAME_MAP = {
    'position_manager': 'pm',
    'data_manager': 'dm',
    'trading_executor': 'te',
    'strategy': 'st',
    'web_server': 'ws',
    'thread_monitor': 'tm',
    'premarket_sync': 'ps',
    'config_manager': 'cm',
    'indicator_calculator': 'ic',
    'sell_monitor': 'sm',
    'main': 'main',
}

# 日志格式(优化: 使用单字母级别,精简模块名)
log_formatter = logging.Formatter('%(asctime)s [%(levelname).1s] %(name)s - %(message)s')

# 创建日志处理器
file_handler = RotatingFileHandler(
    log_file,
    encoding='utf-8',  # 指定编码为 UTF-8
    maxBytes=config.LOG_MAX_SIZE, 
    backupCount=config.LOG_BACKUP_COUNT
)
file_handler.setFormatter(log_formatter)

# 控制台处理器
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)

# 创建logger
logger = logging.getLogger('miniQMT')
logger.setLevel(getattr(logging, config.LOG_LEVEL))
logger.addHandler(file_handler)
logger.addHandler(console_handler)

# 设置调试模式下的详细日志
if config.DEBUG:
    logger.setLevel(logging.DEBUG)
    
def get_logger(name=None):
    """获取指定名称的logger,自动应用模块名称映射"""
    if name:
        # 应用模块名称映射
        short_name = MODULE_NAME_MAP.get(name, name)
        child_logger = logger.getChild(short_name)
        return child_logger
    return logger

def clean_old_logs(days=None):
    """清理指定天数前的日志文件"""
    if days is None:
        days = config.LOG_CLEANUP_DAYS

    logger.info(f"清理{days}天前日志")

    # 获取当前日期
    current_date = datetime.now()

    # 计算截止日期
    cutoff_date = current_date - timedelta(days=days)
    cutoff_timestamp = cutoff_date.timestamp()

    # 获取日志目录下的所有日志文件
    log_pattern = os.path.join('logs', '*.log*')
    log_files = glob.glob(log_pattern)

    # 检查每个日志文件的修改时间
    for log_file in log_files:
        file_mtime = os.path.getmtime(log_file)
        if file_mtime < cutoff_timestamp:
            try:
                os.remove(log_file)
                logger.info(f"删除旧日志: {os.path.basename(log_file)}")
            except Exception as e:
                logger.error(f"删除失败: {os.path.basename(log_file)} - {str(e)[:30]}")

    logger.info("日志清理完成")

def schedule_log_cleanup():
    """定时清理日志"""
    if not config.ENABLE_LOG_CLEANUP:
        return
    
    while True:
        # 获取当前时间
        now = datetime.now()
        cleanup_time = datetime.strptime(config.LOG_CLEANUP_TIME, "%H:%M:%S").time()
        
        # 如果当前时间是清理时间，执行清理
        if now.time().hour == cleanup_time.hour and now.time().minute == cleanup_time.minute:
            clean_old_logs()
            # 等待60秒，避免在同一分钟内多次执行
            time.sleep(60)
        else:
            # 等待10分钟检查一次
            time.sleep(600)
