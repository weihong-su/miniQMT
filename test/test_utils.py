"""
Test Utilities - Helper functions for testing

Provides common utilities used across test modules
"""

import os
import sys
import time
import threading
from datetime import datetime, timedelta

# Try to import psutil (optional dependency)
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from logger import get_logger

logger = get_logger("test_utils")


def is_qmt_running():
    """
    Check if QMT process is running

    Returns:
        bool: True if QMT is running, False otherwise
    """
    if not PSUTIL_AVAILABLE:
        logger.warning("psutil not available, cannot check if QMT is running")
        return False

    qmt_process_names = [
        'XtMiniQMT.exe',
        'XtItClient.exe',
        'xtquant.exe'
    ]

    for proc in psutil.process_iter(['name']):
        try:
            if proc.info['name'] in qmt_process_names:
                logger.debug(f"QMT process found: {proc.info['name']}")
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    return False


def is_qmt_path_valid():
    """
    Check if QMT path in config is valid

    Returns:
        bool: True if path exists, False otherwise
    """
    qmt_path = config.QMT_PATH
    valid = os.path.exists(qmt_path)

    if valid:
        logger.debug(f"QMT path valid: {qmt_path}")
    else:
        logger.warning(f"QMT path not found: {qmt_path}")

    return valid


def wait_for_thread_start(thread_name, timeout=10):
    """
    Wait for a thread to start

    Args:
        thread_name: Name of thread to wait for
        timeout: Maximum wait time in seconds

    Returns:
        bool: True if thread started, False otherwise
    """
    start_time = time.time()

    while time.time() - start_time < timeout:
        for thread in threading.enumerate():
            if thread_name.lower() in thread.name.lower():
                logger.debug(f"Thread started: {thread.name}")
                return True
        time.sleep(0.1)

    logger.warning(f"Thread '{thread_name}' did not start within {timeout}s")
    return False


def wait_for_thread_stop(thread_name, timeout=10):
    """
    Wait for a thread to stop

    Args:
        thread_name: Name of thread to wait for
        timeout: Maximum wait time in seconds

    Returns:
        bool: True if thread stopped, False otherwise
    """
    start_time = time.time()

    while time.time() - start_time < timeout:
        found = False
        for thread in threading.enumerate():
            if thread_name.lower() in thread.name.lower():
                found = True
                break

        if not found:
            logger.debug(f"Thread stopped: {thread_name}")
            return True

        time.sleep(0.1)

    logger.warning(f"Thread '{thread_name}' still running after {timeout}s")
    return False


def get_active_threads():
    """
    Get list of all active threads

    Returns:
        list: List of thread names
    """
    threads = []
    for thread in threading.enumerate():
        threads.append(thread.name)
    return threads


def is_port_in_use(port):
    """
    Check if a port is in use

    Args:
        port: Port number to check

    Returns:
        bool: True if port is in use, False otherwise
    """
    for conn in psutil.net_connections():
        if conn.laddr.port == port and conn.status == 'LISTEN':
            logger.debug(f"Port {port} is in use")
            return True

    return False


def kill_process_on_port(port):
    """
    Kill process using a specific port

    Args:
        port: Port number

    Returns:
        bool: True if process killed, False otherwise
    """
    for conn in psutil.net_connections():
        if conn.laddr.port == port and conn.status == 'LISTEN':
            try:
                process = psutil.Process(conn.pid)
                process.terminate()
                process.wait(timeout=5)
                logger.info(f"Process on port {port} terminated")
                return True
            except Exception as e:
                logger.error(f"Failed to kill process on port {port}: {str(e)}")
                return False

    return False


def create_test_stock_data(stock_code, base_price=10.0, days=30):
    """
    Create test stock price data

    Args:
        stock_code: Stock code
        base_price: Starting price
        days: Number of days of data

    Returns:
        list: List of price records [{date, open, high, low, close, volume}, ...]
    """
    import random

    data = []
    current_price = base_price

    for i in range(days):
        date = (datetime.now() - timedelta(days=days-i)).strftime("%Y-%m-%d")

        # Random price movement
        change_pct = random.uniform(-0.05, 0.05)  # -5% to +5%
        open_price = current_price
        close_price = current_price * (1 + change_pct)

        high_price = max(open_price, close_price) * random.uniform(1.0, 1.02)
        low_price = min(open_price, close_price) * random.uniform(0.98, 1.0)

        volume = random.randint(100000, 10000000)

        data.append({
            'date': date,
            'open': round(open_price, 2),
            'high': round(high_price, 2),
            'low': round(low_price, 2),
            'close': round(close_price, 2),
            'volume': volume
        })

        current_price = close_price

    return data


def format_test_result(test_name, status, duration, details=None):
    """
    Format test result for display

    Args:
        test_name: Name of the test
        status: Status ('PASS', 'FAIL', 'SKIP')
        duration: Duration in seconds
        details: Optional details dict

    Returns:
        str: Formatted test result
    """
    status_symbols = {
        'PASS': '[PASS]',
        'FAIL': '[FAIL]',
        'SKIP': '[SKIP]'
    }

    symbol = status_symbols.get(status, '[UNKNOWN]')
    result = f"{symbol} {test_name} ({duration:.2f}s)"

    if details:
        result += f" - {details}"

    return result


def calculate_profit_ratio(cost_price, current_price):
    """
    Calculate profit ratio

    Args:
        cost_price: Cost price
        current_price: Current price

    Returns:
        float: Profit ratio (e.g., 0.06 for 6% profit)
    """
    if cost_price == 0:
        return 0.0
    return (current_price - cost_price) / cost_price


def should_trigger_stop_loss(cost_price, current_price):
    """
    Check if stop loss should be triggered

    Args:
        cost_price: Cost price
        current_price: Current price

    Returns:
        bool: True if stop loss should trigger
    """
    profit_ratio = calculate_profit_ratio(cost_price, current_price)
    return profit_ratio <= config.STOP_LOSS_RATIO


def should_trigger_take_profit(cost_price, current_price, profit_triggered=False):
    """
    Check if take profit should be triggered

    Args:
        cost_price: Cost price
        current_price: Current price
        profit_triggered: Whether initial take profit already triggered

    Returns:
        bool: True if take profit should trigger
    """
    profit_ratio = calculate_profit_ratio(cost_price, current_price)

    if not profit_triggered:
        # Check initial take profit
        return profit_ratio >= config.INITIAL_TAKE_PROFIT_RATIO
    else:
        # Check dynamic take profit
        # This is simplified - real logic is more complex
        return False


def cleanup_test_files(file_patterns):
    """
    Clean up test files matching patterns

    Args:
        file_patterns: List of file patterns (e.g., ['test_*.db', 'test_*.log'])
    """
    import glob

    for pattern in file_patterns:
        for file_path in glob.glob(pattern):
            try:
                os.remove(file_path)
                logger.debug(f"Removed test file: {file_path}")
            except Exception as e:
                logger.warning(f"Failed to remove {file_path}: {str(e)}")


def ensure_directory_exists(directory):
    """
    Ensure a directory exists, create if not

    Args:
        directory: Directory path
    """
    if not os.path.exists(directory):
        os.makedirs(directory)
        logger.debug(f"Created directory: {directory}")
