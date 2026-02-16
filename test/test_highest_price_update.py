"""
专项测试：最高价更新机制（缓存历史高点 + 实时tick不缓存）
验证点：
1. 历史高点在TTL内复用缓存，避免频繁行情请求
2. 缓存过期后重新拉取历史高点
3. tick数据每次调用都会从接口获取（不缓存）
"""

import sys
import os
import time
import pandas as pd
from unittest.mock import patch

# 添加父目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test.test_base import TestBase
from position_manager import PositionManager
from logger import get_logger

logger = get_logger("test_highest_price_update")


class TestHighestPriceUpdate(TestBase):
    """最高价更新机制专项测试"""

    def setUp(self):
        super().setUp()
        self.pm = PositionManager()
        # 停止同步线程，避免后台影响测试
        self.pm.stop_sync_thread()

        # 清理内存持仓，确保用例隔离
        cursor = self.pm.memory_conn.cursor()
        cursor.execute("DELETE FROM positions")
        self.pm.memory_conn.commit()

        # 插入一条持仓到内存数据库
        stock_code = "000001.SZ"
        cost_price = 10.0
        open_date = "2026-02-01 09:30:00"
        cursor = self.pm.memory_conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO positions
            (stock_code, volume, available, cost_price, current_price,
             open_date, profit_triggered, highest_price, stop_loss_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            stock_code, 1000, 1000, cost_price, cost_price,
            open_date, 0, cost_price, cost_price * (1 + 0.0)
        ))
        self.pm.memory_conn.commit()

        # 缩短缓存TTL，便于测试
        self.pm.history_high_cache_ttl = 1

    def tearDown(self):
        try:
            self.pm.stop_sync_thread()
            self.pm.memory_conn.close()
        finally:
            super().tearDown()

    def test_history_cache_and_tick_no_cache(self):
        """
        验证：
        - 历史高点在TTL内使用缓存
        - 缓存过期后重新拉取历史数据
        - tick数据每次调用均实时获取
        """
        stock_code = "000001.SZ"

        # 历史高点模拟数据（11.5）
        history_df = pd.DataFrame({"high": [11.0, 11.5]})

        with patch("position_manager.Methods.getStockData", return_value=history_df) as mock_history, \
             patch.object(self.pm.data_manager, "get_history_data_from_db", return_value=pd.DataFrame()) as mock_db, \
             patch.object(self.pm.data_manager, "get_latest_data",
                          return_value={"high": 12.5, "lastPrice": 12.4}) as mock_tick:

            # 第一次调用：应拉取历史数据 + tick
            self.pm.update_all_positions_highest_price()
            self.assertEqual(mock_history.call_count, 1, "首次应拉取历史数据")
            self.assertEqual(mock_tick.call_count, 1, "tick数据应实时获取")

            # 最高价应更新到tick高点 12.5
            cursor = self.pm.memory_conn.cursor()
            cursor.execute("SELECT highest_price FROM positions WHERE stock_code=?", (stock_code,))
            row = cursor.fetchone()
            self.assertIsNotNone(row, "持仓应存在")
            self.assertAlmostEqual(row[0], 12.5, places=2, msg="最高价应更新为tick高点")

            # TTL内再次调用：历史数据不应重复拉取，tick仍应获取
            self.pm.update_all_positions_highest_price()
            self.assertEqual(mock_history.call_count, 1, "TTL内不应重复拉取历史数据")
            self.assertEqual(mock_tick.call_count, 2, "tick数据每次应实时获取")

            # 让缓存过期
            self.pm.history_high_cache[stock_code]["ts"] = time.time() - 2
            self.pm.update_all_positions_highest_price()
            self.assertEqual(mock_history.call_count, 2, "缓存过期后应重新拉取历史数据")
            self.assertEqual(mock_tick.call_count, 3, "tick数据每次应实时获取")

        logger.info("最高价更新机制专项测试通过")
