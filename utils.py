"""
工具函数模块，提供各种辅助功能
"""
import os
import json
import csv
import time
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

import config
from logger import get_logger

# 获取logger
logger = get_logger("utils")

def format_number(number, decimal_places=2):
    """
    格式化数字，保留指定小数位
    
    参数:
    number (float): 要格式化的数字
    decimal_places (int): 保留的小数位数
    
    返回:
    str: 格式化后的字符串
    """
    if number is None:
        return "N/A"
    
    try:
        return f"{float(number):.{decimal_places}f}"
    except (ValueError, TypeError):
        return "N/A"

def format_percentage(number, decimal_places=2):
    """
    格式化百分比，保留指定小数位
    
    参数:
    number (float): 要格式化的数字
    decimal_places (int): 保留的小数位数
    
    返回:
    str: 格式化后的字符串
    """
    if number is None:
        return "N/A"
    
    try:
        return f"{float(number) * 100:.{decimal_places}f}%"
    except (ValueError, TypeError):
        return "N/A"

def format_datetime(dt, format_str="%Y-%m-%d %H:%M:%S"):
    """
    格式化日期时间
    
    参数:
    dt (datetime或str): 要格式化的日期时间
    format_str (str): 格式化字符串
    
    返回:
    str: 格式化后的字符串
    """
    if dt is None:
        return "N/A"
    
    try:
        if isinstance(dt, str):
            dt = pd.to_datetime(dt)
        return dt.strftime(format_str)
    except:
        return "N/A"

def is_valid_stock_code(stock_code):
    """
    检查股票代码是否有效
    
    参数:
    stock_code (str): 股票代码
    
    返回:
    bool: 是否有效
    """
    if not stock_code:
        return False
    
    # 股票代码格式检查
    parts = stock_code.split('.')
    if len(parts) != 2:
        return False
    
    code, market = parts
    
    # 检查市场代码
    if market not in ['SH', 'SZ']:
        return False
    
    # 检查股票代码
    if not code.isdigit():
        return False
    
    # 上交所：6开头，6位数
    if market == 'SH' and (not code.startswith('6') or len(code) != 6):
        return False
    
    # 深交所：0或3开头，6位数
    if market == 'SZ' and (not (code.startswith('0') or code.startswith('3')) or len(code) != 6):
        return False
    
    return True

def calculate_trade_metrics(trades_df):
    """
    计算交易指标
    
    参数:
    trades_df (pandas.DataFrame): 交易记录
    
    返回:
    dict: 交易指标
    """
    if trades_df.empty:
        return {
            'total_trades': 0,
            'win_trades': 0,
            'lose_trades': 0,
            'win_rate': 0,
            'avg_profit': 0,
            'avg_loss': 0,
            'profit_factor': 0,
            'max_profit': 0,
            'max_loss': 0,
            'total_profit': 0,
            'total_commission': 0
        }
    
    # 计算每笔交易的盈亏
    trades_df = trades_df.sort_values('trade_time')
    trades_df['profit'] = 0
    
    # 按股票代码和交易时间分组，计算盈亏
    for stock_code, group in trades_df.groupby('stock_code'):
        buy_records = group[group['trade_type'] == 'BUY']
        sell_records = group[group['trade_type'] == 'SELL']
        
        if buy_records.empty or sell_records.empty:
            continue
        
        # 简单计算：假设先买后卖
        avg_buy_price = buy_records['price'].mean()
        avg_sell_price = sell_records['price'].mean()
        sell_volume = sell_records['volume'].sum()
        
        # 计算盈亏
        profit = (avg_sell_price - avg_buy_price) * sell_volume
        
        # 更新盈亏
        for idx in sell_records.index:
            trades_df.loc[idx, 'profit'] = profit * (trades_df.loc[idx, 'volume'] / sell_volume)
    
    # 计算交易指标
    win_trades = trades_df[trades_df['profit'] > 0]
    lose_trades = trades_df[trades_df['profit'] < 0]
    
    metrics = {
        'total_trades': len(trades_df),
        'win_trades': len(win_trades),
        'lose_trades': len(lose_trades),
        'win_rate': len(win_trades) / len(trades_df) if len(trades_df) > 0 else 0,
        'avg_profit': win_trades['profit'].mean() if len(win_trades) > 0 else 0,
        'avg_loss': lose_trades['profit'].mean() if len(lose_trades) > 0 else 0,
        'profit_factor': abs(win_trades['profit'].sum() / lose_trades['profit'].sum()) if len(lose_trades) > 0 and lose_trades['profit'].sum() != 0 else 0,
        'max_profit': win_trades['profit'].max() if len(win_trades) > 0 else 0,
        'max_loss': lose_trades['profit'].min() if len(lose_trades) > 0 else 0,
        'total_profit': trades_df['profit'].sum(),
        'total_commission': trades_df['commission'].sum()
    }
    
    return metrics

def export_trades_to_csv(trades_df, filename=None):
    """
    导出交易记录到CSV文件
    
    参数:
    trades_df (pandas.DataFrame): 交易记录
    filename (str): 文件名，如果为None则使用日期时间生成文件名
    
    返回:
    str: 文件路径
    """
    if trades_df.empty:
        logger.warning("没有交易记录可导出")
        return None
    
    # 创建导出目录
    export_dir = os.path.join(config.DATA_DIR, 'exports')
    if not os.path.exists(export_dir):
        os.makedirs(export_dir)
    
    # 生成文件名
    if filename is None:
        filename = f"trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    
    file_path = os.path.join(export_dir, filename)
    
    try:
        # 导出到CSV
        trades_df.to_csv(file_path, index=False, encoding='utf-8-sig')
        logger.info(f"交易记录已导出到 {file_path}")
        return file_path
    except Exception as e:
        logger.error(f"导出交易记录到CSV时出错: {str(e)}")
        return None

def export_positions_to_csv(positions_df, filename=None):
    """
    导出持仓记录到CSV文件
    
    参数:
    positions_df (pandas.DataFrame): 持仓记录
    filename (str): 文件名，如果为None则使用日期时间生成文件名
    
    返回:
    str: 文件路径
    """
    if positions_df.empty:
        logger.warning("没有持仓记录可导出")
        return None
    
    # 创建导出目录
    export_dir = os.path.join(config.DATA_DIR, 'exports')
    if not os.path.exists(export_dir):
        os.makedirs(export_dir)
    
    # 生成文件名
    if filename is None:
        filename = f"positions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    
    file_path = os.path.join(export_dir, filename)
    
    try:
        # 导出到CSV
        positions_df.to_csv(file_path, index=False, encoding='utf-8-sig')
        logger.info(f"持仓记录已导出到 {file_path}")
        return file_path
    except Exception as e:
        logger.error(f"导出持仓记录到CSV时出错: {str(e)}")
        return None

def load_stock_pool_from_csv(file_path):
    """
    从CSV文件加载股票池
    
    参数:
    file_path (str): CSV文件路径
    
    返回:
    list: 股票代码列表
    """
    if not os.path.exists(file_path):
        logger.error(f"文件 {file_path} 不存在")
        return []
    
    try:
        stock_codes = []
        with open(file_path, 'r', encoding='utf-8-sig') as f:
            csv_reader = csv.reader(f)
            for row in csv_reader:
                if row and row[0]:
                    stock_code = row[0].strip()
                    if is_valid_stock_code(stock_code):
                        stock_codes.append(stock_code)
        
        logger.info(f"从 {file_path} 加载了 {len(stock_codes)} 只股票")
        return stock_codes
    except Exception as e:
        logger.error(f"从CSV加载股票池时出错: {str(e)}")
        return []

def save_stock_pool_to_json(stock_codes, file_path=None):
    """
    保存股票池到JSON文件
    
    参数:
    stock_codes (list): 股票代码列表
    file_path (str): JSON文件路径，如果为None则使用默认路径
    
    返回:
    bool: 是否保存成功
    """
    if file_path is None:
        file_path = "stock_pool.json"
    
    try:
        with open(file_path, 'w') as f:
            json.dump(stock_codes, f)
        
        logger.info(f"股票池已保存到 {file_path}")
        return True
    except Exception as e:
        logger.error(f"保存股票池到JSON时出错: {str(e)}")
        return False

def calculate_position_metrics(positions_df):
    """
    计算持仓指标
    
    参数:
    positions_df (pandas.DataFrame): 持仓记录
    
    返回:
    dict: 持仓指标
    """
    if positions_df.empty:
        return {
            'total_positions': 0,
            'total_market_value': 0,
            'total_cost': 0,
            'total_profit': 0,
            'profit_ratio': 0,
            'win_positions': 0,
            'lose_positions': 0,
            'win_ratio': 0,
            'max_profit_ratio': 0,
            'max_loss_ratio': 0
        }
    
    # 计算总市值和总成本
    total_market_value = positions_df['market_value'].sum()
    total_cost = (positions_df['cost_price'] * positions_df['volume']).sum()
    
    # 计算总盈亏
    total_profit = total_market_value - total_cost
    profit_ratio = total_profit / total_cost if total_cost > 0 else 0
    
    # 计算盈亏持仓数量
    win_positions = positions_df[positions_df['profit_ratio'] > 0]
    lose_positions = positions_df[positions_df['profit_ratio'] < 0]
    
    metrics = {
        'total_positions': len(positions_df),
        'total_market_value': total_market_value,
        'total_cost': total_cost,
        'total_profit': total_profit,
        'profit_ratio': profit_ratio,
        'win_positions': len(win_positions),
        'lose_positions': len(lose_positions),
        'win_ratio': len(win_positions) / len(positions_df) if len(positions_df) > 0 else 0,
        'max_profit_ratio': positions_df['profit_ratio'].max() if len(positions_df) > 0 else 0,
        'max_loss_ratio': positions_df['profit_ratio'].min() if len(positions_df) > 0 else 0
    }
    
    return metrics

def convert_volume_to_chinese(volume):
    """
    将数量转换为中文表示（万、亿）
    
    参数:
    volume (float): 数量
    
    返回:
    str: 中文表示
    """
    if volume is None:
        return "N/A"
    
    try:
        volume = float(volume)
        if volume >= 100000000:  # 亿
            return f"{volume / 100000000:.2f}亿"
        elif volume >= 10000:  # 万
            return f"{volume / 10000:.2f}万"
        else:
            return f"{volume:.0f}"
    except (ValueError, TypeError):
        return "N/A"

def convert_amount_to_chinese(amount):
    """
    将金额转换为中文表示（万、亿）
    
    参数:
    amount (float): 金额
    
    返回:
    str: 中文表示
    """
    if amount is None:
        return "N/A"
    
    try:
        amount = float(amount)
        if amount >= 100000000:  # 亿
            return f"{amount / 100000000:.2f}亿"
        elif amount >= 10000:  # 万
            return f"{amount / 10000:.2f}万"
        else:
            return f"{amount:.2f}"
    except (ValueError, TypeError):
        return "N/A"

def get_trading_days(start_date, end_date=None):
    """
    获取交易日列表
    
    参数:
    start_date (str): 开始日期，格式 'YYYY-MM-DD'
    end_date (str): 结束日期，格式 'YYYY-MM-DD'，如果为None则使用当前日期
    
    返回:
    list: 交易日列表
    """
    # 由于迅投API没有提供交易日历接口，这里简单处理，忽略了节假日
    if end_date is None:
        end_date = datetime.now().strftime('%Y-%m-%d')
    
    start = pd.to_datetime(start_date)
    end = pd.to_datetime(end_date)
    
    all_days = pd.date_range(start=start, end=end)
    
    # 周末过滤
    trading_days = [day.strftime('%Y-%m-%d') for day in all_days if day.weekday() < 5]
    
    return trading_days

def _memory_usage_windows():
    """未安装 psutil 时，通过 Win32 API 获取当前进程内存占用。

    口径与 psutil 保持一致：WorkingSetSize->rss, PagefileUsage->vms。
    """
    import ctypes
    from ctypes import wintypes

    class _ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32")
    # 必须显式声明句柄类型：64 位下默认 c_int 会截断伪句柄，导致调用失败
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    get_memory_info = kernel32.K32GetProcessMemoryInfo
    get_memory_info.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_ProcessMemoryCounters), ctypes.c_ulong
    ]
    get_memory_info.restype = wintypes.BOOL

    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
    if not get_memory_info(
        kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        return None

    return {
        'rss': counters.WorkingSetSize,
        'vms': counters.PagefileUsage,
        'rss_mb': counters.WorkingSetSize / (1024 * 1024),
        'vms_mb': counters.PagefileUsage / (1024 * 1024)
    }


def memory_usage():
    """
    获取当前进程的内存使用情况
    
    返回:
    dict: 内存使用情况
    """
    try:
        import psutil
        process = psutil.Process(os.getpid())
        memory_info = process.memory_info()
        
        return {
            'rss': memory_info.rss,  # 常驻内存
            'vms': memory_info.vms,  # 虚拟内存
            'rss_mb': memory_info.rss / (1024 * 1024),  # MB
            'vms_mb': memory_info.vms / (1024 * 1024)  # MB
        }
    except ImportError:
        # psutil 非必装依赖，Windows 下回退到 Win32 API
        try:
            return _memory_usage_windows()
        except Exception as e:
            logger.warning(f"未安装psutil模块，Win32回退也失败，无法获取内存使用情况: {str(e)}")
            return None
    except Exception as e:
        logger.error(f"获取内存使用情况时出错: {str(e)}")
        return None

def disk_usage(path=None):
    """
    获取磁盘使用情况
    
    参数:
    path (str): 路径，如果为None则使用当前目录
    
    返回:
    dict: 磁盘使用情况
    """
    if path is None:
        path = '.'
    
    try:
        import psutil
        usage = psutil.disk_usage(path)
        
        return {
            'total': usage.total,
            'used': usage.used,
            'free': usage.free,
            'percent': usage.percent,
            'total_gb': usage.total / (1024 * 1024 * 1024),  # GB
            'used_gb': usage.used / (1024 * 1024 * 1024),  # GB
            'free_gb': usage.free / (1024 * 1024 * 1024)  # GB
        }
    except ImportError:
        logger.warning("未安装psutil模块，无法获取磁盘使用情况")
        return None
    except Exception as e:
        logger.error(f"获取磁盘使用情况时出错: {str(e)}")
        return None

def system_info():
    """
    获取系统信息
    
    返回:
    dict: 系统信息
    """
    try:
        import platform
        
        return {
            'system': platform.system(),
            'node': platform.node(),
            'release': platform.release(),
            'version': platform.version(),
            'machine': platform.machine(),
            'processor': platform.processor(),
            'python_version': platform.python_version()
        }
    except Exception as e:
        logger.error(f"获取系统信息时出错: {str(e)}")
        return None
