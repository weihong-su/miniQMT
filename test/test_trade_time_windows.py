"""实盘下单窗口与连续竞价时间回归测试。"""

import os
import sys
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from premarket_sync import PreMarketSyncScheduler


class TestTradeTimeWindows(unittest.TestCase):
    """验证预挂允许下单，但休市时间不计入委托超时。"""

    def setUp(self):
        self.old_simulation = config.ENABLE_SIMULATION_MODE
        self.old_debug_data = config.DEBUG_SIMU_STOCK_DATA
        config.ENABLE_SIMULATION_MODE = False
        config.DEBUG_SIMU_STOCK_DATA = False

    def tearDown(self):
        config.ENABLE_SIMULATION_MODE = self.old_simulation
        config.DEBUG_SIMU_STOCK_DATA = self.old_debug_data

    @staticmethod
    def _at(hour, minute, second=0):
        return datetime(2026, 9, 11, hour, minute, second)

    def test_order_submission_window_includes_preopen_and_lunch(self):
        self.assertFalse(config.is_trade_time(self._at(9, 24, 59)))
        self.assertTrue(config.is_trade_time(self._at(9, 25)))
        self.assertTrue(config.is_trade_time(self._at(9, 29, 59)))
        self.assertTrue(config.is_trade_time(self._at(11, 45)))
        self.assertTrue(config.is_trade_time(self._at(12, 59, 59)))
        self.assertTrue(config.is_trade_time(self._at(15, 0)))
        self.assertFalse(config.is_trade_time(self._at(15, 0, 1)))
        self.assertFalse(config.is_trade_time(datetime(2026, 9, 12, 10, 0)))

    def test_continuous_trade_time_excludes_preopen_and_lunch(self):
        self.assertFalse(config.is_continuous_trade_time(self._at(9, 29, 59)))
        self.assertTrue(config.is_continuous_trade_time(self._at(9, 30)))
        self.assertTrue(config.is_continuous_trade_time(self._at(11, 30)))
        self.assertFalse(config.is_continuous_trade_time(self._at(11, 30, 1)))
        self.assertFalse(config.is_continuous_trade_time(self._at(12, 59, 59)))
        self.assertTrue(config.is_continuous_trade_time(self._at(13, 0)))
        self.assertTrue(config.is_continuous_trade_time(self._at(15, 0)))
        self.assertFalse(config.is_continuous_trade_time(self._at(15, 0, 1)))

    def test_market_hours_keep_strict_continuous_trade_semantics(self):
        self.assertFalse(config.is_market_hours(self._at(9, 26)))
        self.assertTrue(config.is_market_hours(self._at(10, 0)))
        self.assertFalse(config.is_market_hours(self._at(12, 42)))
        self.assertTrue(config.is_market_hours(self._at(14, 0)))

    def test_preopen_timeout_starts_at_open(self):
        submitted = self._at(9, 26)
        self.assertEqual(
            config.get_continuous_trading_seconds(submitted, self._at(9, 30)),
            0,
        )
        self.assertEqual(
            config.get_continuous_trading_seconds(submitted, self._at(9, 30, 29)),
            29,
        )
        self.assertEqual(
            config.get_continuous_trading_seconds(submitted, self._at(9, 30, 30)),
            30,
        )

    def test_lunch_timeout_starts_at_afternoon_open(self):
        submitted = self._at(12, 42)
        self.assertEqual(
            config.get_continuous_trading_seconds(submitted, self._at(13, 0)),
            0,
        )
        self.assertEqual(
            config.get_continuous_trading_seconds(submitted, self._at(13, 0, 29)),
            29,
        )
        self.assertEqual(
            config.get_continuous_trading_seconds(submitted, self._at(13, 0, 30)),
            30,
        )

    def test_order_crossing_lunch_keeps_morning_elapsed_seconds(self):
        submitted = self._at(11, 29, 50)
        self.assertEqual(
            config.get_continuous_trading_seconds(submitted, self._at(13, 0)),
            10,
        )
        self.assertEqual(
            config.get_continuous_trading_seconds(submitted, self._at(13, 0, 19)),
            29,
        )
        self.assertEqual(
            config.get_continuous_trading_seconds(submitted, self._at(13, 0, 20)),
            30,
        )

    def test_premarket_compensation_runs_inside_preorder_window(self):
        """09:25后允许预挂时，错过的盘前同步仍应在窗口内补偿执行。"""
        scheduler = PreMarketSyncScheduler.__new__(PreMarketSyncScheduler)
        scheduler.sync_time = (9, 25)
        scheduler.compensation_window = 5
        scheduler.running = False
        scheduler.timer = None
        scheduler.load_persisted_schedule = MagicMock(
            return_value=self._at(9, 24)
        )
        scheduler.schedule_next_sync = MagicMock()

        with patch("premarket_sync.datetime") as mock_datetime, \
             patch("premarket_sync.threading.Thread") as mock_thread, \
             patch("config.is_continuous_trade_time", return_value=False):
            mock_datetime.now.return_value = self._at(9, 27)
            scheduler.start()

        mock_thread.assert_called_once()
        mock_thread.return_value.start.assert_called_once()
        scheduler.schedule_next_sync.assert_called_once()


if __name__ == "__main__":
    unittest.main()
