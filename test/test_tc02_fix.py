"""
TC02修复验证 - 独立测试
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
config.ENABLE_SIMULATION_MODE = True
config.DEBUG = True
config.DB_PATH = "data/trading_test.db"
config.ENABLE_DYNAMIC_STOP_PROFIT = False  # 关闭止盈功能
config.ENABLE_GRID_TRADING = True

from position_manager import PositionManager
from logger import get_logger

logger = get_logger("test_tc02_fix")

def test_tc02_fix():
    """验证TC02修复: check_trading_signals应该返回(None, None)"""

    logger.info("初始化position_manager...")
    pm = PositionManager()

    logger.info(f"配置检查:")
    logger.info(f"  ENABLE_DYNAMIC_STOP_PROFIT = {config.ENABLE_DYNAMIC_STOP_PROFIT}")
    logger.info(f"  ENABLE_GRID_TRADING = {config.ENABLE_GRID_TRADING}")

    # 创建测试持仓
    from datetime import datetime
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
    """, ('TEST002.SZ', 600, 600, 10.00, 10.60,
          datetime.now().strftime("%Y-%m-%d"),
          1, 10.60, 9.30))

    conn.commit()
    pm._sync_db_to_memory()

    logger.info("测试持仓已创建")

    # 调用check_trading_signals
    logger.info("调用check_trading_signals...")
    signal_type, signal_info = pm.check_trading_signals('TEST002.SZ')

    logger.info(f"返回值: signal_type={signal_type}, signal_info={signal_info}")

    # 验证
    if signal_type is None and signal_info is None:
        logger.info("✅ TC02修复成功: 止盈功能关闭时返回(None, None)")
        return True
    else:
        logger.error(f"❌ TC02修复失败: 预期(None, None), 实际({signal_type}, {signal_info})")
        return False

if __name__ == '__main__':
    success = test_tc02_fix()
    sys.exit(0 if success else 1)
