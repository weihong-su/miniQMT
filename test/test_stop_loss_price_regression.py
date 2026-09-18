"""
专项回归：动态止盈止损价不得回退

2026-09-18 实盘日志(301085)出现止损价 72.34 -> 72.13 的回退：

    10:12:51,721  更新 301085 的最高价为 77.78          # update_all_positions_highest_price 直写内存表
    10:12:51,741  更新 301085 持仓: 止损价: 从 72.17 到 72.34   # 77.78 * 0.93
    10:12:52,170  更新 301085 持仓: 止损价: 从 72.34 到 72.13   # 77.56 * 0.93  <-- 回退

根因：update_all_positions_price() 的持仓数据取自 get_all_positions() 的
10 秒缓存(positions_cache)，它把缓存快照里的 stop_loss_price 原样回传给
update_position()。update_position 在"最高价无可见变化 + 成本价无变化"时会
保留传入值，于是刚按新高点算出的止损价被旧快照值覆盖。

修复：update_all_positions_price() 不再回传 stop_loss_price，让
update_position 基于内存表的最新最高价重算。
"""

import sys
import os
from unittest.mock import patch, MagicMock

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from test.test_base import TestBase
from position_manager import PositionManager
from logger import get_logger

logger = get_logger("test_stop_loss_price_regression")

STOCK = "301085.SZ"
COST_PRICE = 70.0
# 档位 (0.10, 0.93)：最高浮盈 >=10% 时止盈位 = 最高价 * 0.93
STALE_HIGHEST = 77.56      # 缓存快照里的最高价
STALE_STOP_LOSS = 72.13    # 77.56 * 0.93
FRESH_HIGHEST = 77.78      # 内存表里已被推高的最高价
FRESH_STOP_LOSS = 72.34    # 77.78 * 0.93


class TestStopLossPriceRegression(TestBase):
    """止损价单调性回归测试"""

    def setUp(self):
        super().setUp()
        with patch.object(PositionManager, "start_sync_thread", return_value=None):
            self.pm = PositionManager()
        self.pm.stop_sync_thread()

        cursor = self.pm.memory_conn.cursor()
        cursor.execute("DELETE FROM positions")
        self.pm.memory_conn.commit()

        mock_dm = MagicMock()
        mock_dm.get_latest_data.return_value = {"lastPrice": 77.30, "high": FRESH_HIGHEST}
        mock_dm.get_stock_name.return_value = "亚康股份"
        self.pm.data_manager = mock_dm

        # 固定动态止盈档位，避免受运行时配置改写影响
        self._dtp_patcher = patch.object(
            config, "DYNAMIC_TAKE_PROFIT",
            [(0.05, 0.96), (0.10, 0.93), (0.15, 0.90), (0.20, 0.87), (0.30, 0.85)]
        )
        self._dtp_patcher.start()

    def tearDown(self):
        try:
            self._dtp_patcher.stop()
            self.pm.stop_sync_thread()
            self.pm.memory_conn.close()
        finally:
            super().tearDown()

    # ------------------------------------------------------------------ 工具

    def _seed_position(self, highest_price, stop_loss_price, current_price=77.20,
                       profit_triggered=1):
        """在内存表写入一条持仓（代表"最新"状态）"""
        cursor = self.pm.memory_conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO positions
            (stock_code, stock_name, volume, available, cost_price, base_cost_price,
             current_price, market_value, open_date, profit_triggered,
             highest_price, stop_loss_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            STOCK, "亚康股份", 1000, 1000, COST_PRICE, COST_PRICE,
            current_price, 1000 * current_price, "2026-09-10 09:30:00",
            profit_triggered, highest_price, stop_loss_price,
        ))
        self.pm.memory_conn.commit()

    def _stale_cache_df(self, highest_price, stop_loss_price, current_price=77.20,
                        profit_triggered=True):
        """构造 get_all_positions() 返回的陈旧缓存快照"""
        return pd.DataFrame([{
            "stock_code": STOCK,
            "stock_name": "亚康股份",
            "volume": 1000,
            "available": 1000,
            "cost_price": COST_PRICE,
            "base_cost_price": COST_PRICE,
            "current_price": current_price,
            "market_value": 1000 * current_price,
            "open_date": "2026-09-10 09:30:00",
            "profit_triggered": profit_triggered,
            "highest_price": highest_price,
            "stop_loss_price": stop_loss_price,
        }])

    def _db_stop_loss(self):
        cursor = self.pm.memory_conn.cursor()
        cursor.execute(
            "SELECT stop_loss_price, highest_price FROM positions WHERE stock_code=?",
            (STOCK,)
        )
        return cursor.fetchone()

    # ------------------------------------------------------------------ 用例

    def test_stale_cache_must_not_roll_back_stop_loss(self):
        """陈旧缓存快照不得把已按新高点算好的止损价改回旧值（核心回归）"""
        # 内存表已是新状态：最高价 77.78 / 止损价 72.34
        self._seed_position(FRESH_HIGHEST, FRESH_STOP_LOSS)

        # 但 get_all_positions() 仍返回 10 秒前的快照：77.56 / 72.13
        stale = self._stale_cache_df(STALE_HIGHEST, STALE_STOP_LOSS)

        with patch.object(self.pm, "get_all_positions", return_value=stale):
            self.pm.update_all_positions_price()

        stop_loss, highest = self._db_stop_loss()
        self.assertAlmostEqual(highest, FRESH_HIGHEST, places=2,
                               msg="最高价不应被陈旧快照拉低")
        self.assertAlmostEqual(
            stop_loss, FRESH_STOP_LOSS, places=2,
            msg=f"止损价被陈旧缓存覆盖回退: {stop_loss} (期望 {FRESH_STOP_LOSS})"
        )

    def test_stop_loss_follows_new_high(self):
        """最高价推高后，止损价应随之抬升到新高点对应值"""
        # 内存表最高价已推到 77.78，但止损价仍停留在旧高点 77.56 对应的 72.13
        self._seed_position(FRESH_HIGHEST, STALE_STOP_LOSS)
        stale = self._stale_cache_df(FRESH_HIGHEST, STALE_STOP_LOSS)

        with patch.object(self.pm, "get_all_positions", return_value=stale):
            self.pm.update_all_positions_price()

        stop_loss, _ = self._db_stop_loss()
        self.assertAlmostEqual(
            stop_loss, FRESH_STOP_LOSS, places=2,
            msg="止损价未跟随最高价抬升"
        )

    def test_repeated_refresh_is_monotonic(self):
        """连续多轮刷新（缓存始终陈旧）时止损价单调不降"""
        self._seed_position(FRESH_HIGHEST, FRESH_STOP_LOSS)
        stale = self._stale_cache_df(STALE_HIGHEST, STALE_STOP_LOSS)

        last = FRESH_STOP_LOSS
        for i in range(5):
            # 现价每轮小幅抖动，保证跨过 1 分钱阈值触发 update_position
            price = 77.30 if i % 2 == 0 else 77.45
            self.pm.data_manager.get_latest_data.return_value = {
                "lastPrice": price, "high": FRESH_HIGHEST
            }
            with patch.object(self.pm, "get_all_positions", return_value=stale):
                self.pm.update_all_positions_price()

            stop_loss, _ = self._db_stop_loss()
            self.assertGreaterEqual(
                round(stop_loss, 2), round(last, 2),
                msg=f"第{i + 1}轮刷新止损价回退: {last} -> {stop_loss}"
            )
            last = stop_loss

    def test_fixed_stop_loss_unaffected(self):
        """未触发首次止盈时仍走固定止损（成本价 * (1+STOP_LOSS_RATIO)），行为不变"""
        expected = round(COST_PRICE * (1 + config.STOP_LOSS_RATIO), 2)
        self._seed_position(FRESH_HIGHEST, expected, profit_triggered=0)
        stale = self._stale_cache_df(FRESH_HIGHEST, expected, profit_triggered=False)

        with patch.object(self.pm, "get_all_positions", return_value=stale):
            self.pm.update_all_positions_price()

        stop_loss, _ = self._db_stop_loss()
        self.assertAlmostEqual(
            stop_loss, expected, places=2,
            msg="固定止损场景止损价被改动"
        )


if __name__ == "__main__":
    import unittest
    unittest.main()
