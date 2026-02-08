"""
Standalone TC01 test - 诊断信号检测问题
"""
import sys
import os
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
config.ENABLE_SIMULATION_MODE = True
config.DEBUG = True
config.DB_PATH = "data/trading_test.db"

from datetime import datetime
from position_manager import PositionManager
from logger import get_logger

logger = get_logger("test_tc01")

def test_tc01():
    """TC01: 动态止盈信号检测"""

    # 1. 初始化position_manager
    logger.info("初始化position_manager...")
    pm = PositionManager()

    # 2. 创建测试持仓
    logger.info("创建测试持仓...")
    conn = pm.conn
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            stock_code TEXT PRIMARY KEY,
            volume INTEGER,
            available INTEGER,
            cost_price REAL,
            current_price REAL,
            open_date TEXT,
            profit_triggered INTEGER,
            highest_price REAL,
            stop_loss_price REAL
        )
    """)

    cursor.execute("""
        INSERT OR REPLACE INTO positions
        (stock_code, volume, available, cost_price, current_price,
         open_date, profit_triggered, highest_price, stop_loss_price)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ('TEST001.SZ', 1000, 1000, 10.00, 10.60,
          datetime.now().strftime("%Y-%m-%d"),
          1, 10.60, 10.00 * (1 + config.STOP_LOSS_RATIO)))

    conn.commit()
    pm._sync_db_to_memory()

    # 3. 验证持仓数据
    position = pm.get_position('TEST001.SZ')
    logger.info(f"持仓数据: {position}")

    # 4. 模拟价格下跌
    logger.info("模拟价格下跌到10.10...")
    cursor.execute("UPDATE positions SET current_price = ? WHERE stock_code = ?",
                  (10.10, 'TEST001.SZ'))
    conn.commit()
    pm._sync_db_to_memory()

    # 5. 检测信号
    logger.info("检测交易信号...")

    # Mock data_manager.get_latest_data
    with unittest.mock.patch.object(
        pm.data_manager,
        'get_latest_data',
        return_value={'lastPrice': 10.10}
    ):
        signal_type, signal_info = pm.check_trading_signals('TEST001.SZ')

    logger.info(f"信号类型: {signal_type}")
    logger.info(f"信号详情: {signal_info}")

    # 6. 检查latest_signals
    with pm.signal_lock:
        latest = pm.latest_signals.get('TEST001.SZ')
        logger.info(f"latest_signals: {latest}")

    if signal_type:
        logger.info(f"✅ TC01成功: 检测到{signal_type}信号")
    else:
        logger.error("❌ TC01失败: 未检测到信号")

        # 诊断信息
        position = pm.get_position('TEST001.SZ')
        logger.error(f"诊断 - 持仓数据: {position}")
        logger.error(f"诊断 - profit_triggered: {position.get('profit_triggered')}")
        logger.error(f"诊断 - highest_price: {position.get('highest_price')}")
        logger.error(f"诊断 - current_price: {position.get('current_price')}")

if __name__ == '__main__':
    test_tc01()
