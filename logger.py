"""
日志管理模块，提供日志记录和清理功能

功能特性:
- 双输出架构: 同时输出到文件和控制台
- 颜色化输出: WARNING显示为黄色，ERROR/CRITICAL显示为红色
- 文件轮转: 超过配置大小时自动创建备份
- 安全处理: 程序退出时避免I/O错误
- 模块名简化: position_manager → pm，提高可读性
"""
import os
import sys
import logging
from logging.handlers import RotatingFileHandler
import time
from datetime import datetime, timedelta
import glob
import threading
from contextlib import contextmanager
import config

# 导入colorama用于跨平台颜色输出
try:
    from colorama import init as colorama_init, Fore, Style
    colorama_init(autoreset=True)  # 自动重置颜色
    COLORAMA_AVAILABLE = True
except ImportError:
    COLORAMA_AVAILABLE = False
    # 如果colorama不可用，使用空字符串作为颜色代码
    class _DummyStyle:
        RESET_ALL = ''
    class _DummyFore:
        YELLOW = ''
        RED = ''
        RESET = ''
    Fore = _DummyFore()
    Style = _DummyStyle()

def _resolve_log_file_path(log_file_name):
    """解析日志文件路径：裸文件名写入 logs/，带目录时按项目相对路径处理。"""
    if not log_file_name:
        log_file_name = config.LOG_FILE
    if os.path.isabs(log_file_name):
        return log_file_name
    if os.path.dirname(log_file_name):
        return log_file_name
    return os.path.join('logs', log_file_name)


# 日志文件路径
log_file = _resolve_log_file_path(os.environ.get("MINIQMT_LOG_FILE", config.LOG_FILE))

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
    'grid_trading_manager': 'gtm',
    'grid_database': 'gdb',
    'main': 'main',
    'easy_qmt_trader': 'qt',
    'maintenance': 'mt',
}

# ============ 颜色化Formatter ============

class ColoredFormatter(logging.Formatter):
    """为不同日志级别添加颜色的Formatter

    颜色方案:
    - DEBUG: 默认颜色
    - INFO: 默认颜色
    - WARNING: 黄色
    - ERROR: 红色
    - CRITICAL: 红色
    """

    # 定义级别颜色映射
    LEVEL_COLORS = {
        logging.DEBUG: '',
        logging.INFO: '',
        logging.WARNING: Fore.YELLOW,
        logging.ERROR: Fore.RED,
        logging.CRITICAL: Fore.RED,
    }

    def format(self, record):
        """格式化日志记录，为WARNING和ERROR添加颜色"""
        # 获取该级别对应的颜色
        level_color = self.LEVEL_COLORS.get(record.levelno, '')

        # 格式化原始消息
        formatted_message = super().format(record)

        # 如果需要颜色且颜色可用，则添加颜色
        if level_color and COLORAMA_AVAILABLE:
            return f"{level_color}{formatted_message}{Style.RESET_ALL}"
        else:
            return formatted_message

# 日志格式(优化: 使用单字母级别,精简模块名)
log_formatter = logging.Formatter('%(asctime)s [%(levelname).1s] %(name)s - %(message)s')
colored_formatter = ColoredFormatter('%(asctime)s [%(levelname).1s] %(name)s - %(message)s')

def _create_file_handler(path):
    log_dir = os.path.dirname(path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    handler = RotatingFileHandler(
        path,
        encoding='utf-8',  # 指定编码为 UTF-8
        maxBytes=config.LOG_MAX_SIZE,
        backupCount=config.LOG_BACKUP_COUNT
    )
    handler.setFormatter(log_formatter)
    return handler


# 创建日志处理器
file_handler = _create_file_handler(log_file)
_file_handler_lock = threading.RLock()

# 控制台处理器 - 添加错误处理,避免程序退出时的I/O错误
class SafeStreamHandler(logging.StreamHandler):
    """安全的StreamHandler,捕获I/O错误

    主要解决两个问题:
    1. 程序退出时colorama关闭wrapped stdout导致的I/O错误
    2. 多线程环境下(如Flask Web服务器)的日志竞态条件
    """
    def emit(self, record):
        try:
            # 写日志前清除 spinner 可能留在行首的旋转字符（仅 TTY）
            stream = self.stream
            if hasattr(stream, 'isatty') and stream.isatty():
                stream.write('\r\033[K')
                stream.flush()
            super().emit(record)
        except (ValueError, OSError, AttributeError):
            # 忽略以下错误:
            # - ValueError: I/O operation on closed file (colorama关闭stdout)
            # - OSError: 文件描述符无效
            # - AttributeError: 对象属性不存在(极少见)
            # 这些错误通常发生在程序退出或线程清理时,不影响功能
            pass
        except Exception:
            # 捕获其他所有异常,避免日志错误导致程序崩溃
            # 注意: 这里不能使用logger记录(会导致递归),所以静默处理
            pass

# 🔧 关键修复: Monkey patch logging.StreamHandler.emit方法
# 这样所有使用StreamHandler的logger(包括werkzeug)都能安全处理I/O错误
_original_stream_handler_emit = logging.StreamHandler.emit

def _safe_emit(self, record):
    """安全的emit方法,捕获I/O异常"""
    try:
        # 检查stream是否已关闭
        if hasattr(self, 'stream'):
            stream = self.stream
            # 处理colorama包装的stream (stream.wrapped)
            if hasattr(stream, 'wrapped'):
                if hasattr(stream.wrapped, 'closed') and stream.wrapped.closed:
                    return
            # 处理普通stream
            elif hasattr(stream, 'closed') and stream.closed:
                return

        _original_stream_handler_emit(self, record)
    except (ValueError, OSError, AttributeError, BrokenPipeError):
        # 静默处理I/O错误:
        # - ValueError: I/O operation on closed file
        # - OSError: 文件描述符无效
        # - AttributeError: 对象属性不存在
        # - BrokenPipeError: 管道破裂
        pass
    except Exception:
        # 捕获所有其他异常
        pass

# 替换logging.StreamHandler的emit方法
logging.StreamHandler.emit = _safe_emit

console_handler = SafeStreamHandler()
# 使用彩色formatter用于控制台输出
console_handler.setFormatter(colored_formatter)

# 创建logger
logger = logging.getLogger('miniQMT')
logger.setLevel(getattr(logging, config.LOG_LEVEL))
logger.addHandler(file_handler)
logger.addHandler(console_handler)
# 🔧 关键修复: 禁止向根logger传播，避免重复输出
logger.propagate = False

# 设置调试模式下的详细日志
if config.DEBUG:
    logger.setLevel(logging.DEBUG)

def set_log_file(log_file_name=None):
    """运行期切换主日志文件，用于测试日志与实盘日志隔离。"""
    global file_handler, log_file
    target_file = _resolve_log_file_path(log_file_name or config.LOG_FILE)

    with _file_handler_lock:
        if os.path.abspath(target_file) == os.path.abspath(log_file):
            return target_file

        new_handler = _create_file_handler(target_file)
        logger.addHandler(new_handler)

        old_handler = file_handler
        if old_handler:
            logger.removeHandler(old_handler)
            old_handler.close()

        file_handler = new_handler
        log_file = target_file
        if log_file_name:
            config.LOG_FILE = log_file_name
        return target_file

def get_logger(name=None):
    """获取指定名称的logger,自动应用模块名称映射"""
    if name:
        # 应用模块名称映射
        short_name = MODULE_NAME_MAP.get(name, name)
        child_logger = logger.getChild(short_name)
        return child_logger
    return logger

_throttle_state = {}
_throttle_lock = threading.RLock()


def log_throttled(target_logger, level, key, message, interval=None):
    """对持续性状态的重复日志做节流：首次立即输出，之后按间隔输出并附抑制计数。

    用于高频轮询里反复命中同一状态的场景——典型是 T+1 冻结导致 available=0，
    监控线程每 3 秒、策略线程每 16 秒各命中一次，一个下午能刷出数千行同样的话。
    刷屏本身不只是噪音：控制台输出会喂大终端的回滚缓冲，2026-09-09 就出现过
    终端进程吃满系统提交内存、反过来让本进程 can't start new thread 的连锁故障。

    参数:
    target_logger: 目标 logger
    level (int): logging 级别常量
    key (str): 节流键，同键视为同一条持续状态（建议含股票代码与事件名）
    message (str): 日志正文
    interval (float): 节流间隔秒数，None 时取 config.LOG_THROTTLE_INTERVAL

    返回:
    bool: 本次是否真的输出了（调用方可据此决定要不要打印附属明细）
    """
    if interval is None:
        interval = getattr(config, 'LOG_THROTTLE_INTERVAL', 300)

    now = time.time()
    with _throttle_lock:
        last_time, suppressed = _throttle_state.get(key, (0.0, 0))
        if last_time and (now - last_time) < interval:
            _throttle_state[key] = (last_time, suppressed + 1)
            return False
        _throttle_state[key] = (now, 0)

    if suppressed:
        message = f"{message}（期间重复 {suppressed} 次未打印）"
    target_logger.log(level, message)
    return True


def reset_log_throttle(key=None):
    """清除节流记录，使下一次同键日志立即输出。

    状态恢复（如委托成交、持仓同步完成）后调用，避免下一次真正的异常被抑制。
    key 为 None 时清空全部。
    """
    with _throttle_lock:
        if key is None:
            _throttle_state.clear()
        else:
            _throttle_state.pop(key, None)


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

# ============ 第三方库输出抑制工具 ============

_stdout_lock = threading.Lock()

@contextmanager
def suppress_stdout_stderr():
    """
    优雅地抑制标准输出和标准错误输出

    用途：某些第三方库（如baostock）会直接打印到stdout，
         使用此上下文管理器可以临时抑制这些输出。

    特性：
    - 线程安全（使用锁保护）
    - 异常安全（确保stdout/stderr一定恢复）
    - 跨平台兼容

    示例：
        with suppress_stdout_stderr():
            lg = bs.login()  # 不会打印 "login success!"
    """
    with _stdout_lock:
        # 保存原始的 stdout 和 stderr
        old_stdout = sys.stdout
        old_stderr = sys.stderr

        try:
            # 重定向到 devnull
            with open(os.devnull, 'w') as devnull:
                sys.stdout = devnull
                sys.stderr = devnull
                yield
        finally:
            # 恢复原始的 stdout 和 stderr
            sys.stdout = old_stdout
            sys.stderr = old_stderr
