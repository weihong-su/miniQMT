import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from data_manager import DataManager


class TestHistoryDateNormalization(unittest.TestCase):
    def setUp(self):
        self.dm = DataManager.__new__(DataManager)
        self.dm._history_invalid_date_warnings = {}
        self.dm._history_update_attempts = {}

    def test_filters_invalid_date_rows_and_keeps_valid_rows(self):
        df = pd.DataFrame({
            "date": [
                "2026-06-12 15:00",
                "13598-74-57 15:00",
                "0-00-00 15:00",
                20260615,
                "20260616150000",
            ],
            "close": [1, 2, 3, 4, 5],
        })

        result = self.dm._normalize_history_dates(df, "999999.SH", source="unit")

        self.assertEqual(result["date"].tolist(), [
            "2026-06-12",
            "2026-06-15",
            "2026-06-16",
        ])
        self.assertEqual(result["close"].tolist(), [1, 4, 5])

    def test_renames_time_column_before_normalizing(self):
        df = pd.DataFrame({
            "time": ["20260612", "13598-74-57 15:00", "2026-06-15 15:00"],
            "close": [10, 20, 30],
        })

        result = self.dm._normalize_history_dates(df, "399001.SZ", source="unit")

        self.assertIn("date", result.columns)
        self.assertNotIn("time", result.columns)
        self.assertEqual(result["date"].tolist(), ["2026-06-12", "2026-06-15"])
        self.assertEqual(result["close"].tolist(), [10, 30])

    def test_normalizes_xtdata_epoch_millisecond_time(self):
        df = pd.DataFrame({
            "time": [1774972800000, 1775059200000],
            "close": [3948.552, 3919.285],
        })

        result = self.dm._normalize_history_dates(df, "000001.SH", source="unit")

        self.assertEqual(result["date"].tolist(), ["2026-04-01", "2026-04-02"])

    def test_adjusts_baostock_style_code_to_xt_code(self):
        self.assertEqual(self.dm._adjust_stock("sh.000001"), "000001.SH")
        self.assertEqual(self.dm._adjust_stock("sz.399001"), "399001.SZ")

    def test_filters_history_by_requested_date_range(self):
        df = pd.DataFrame({
            "date": ["2026-06-14", "2026-06-15", "2026-06-16"],
            "close": [1, 2, 3],
        })

        result = self.dm._filter_history_date_range(
            df,
            start_date="20260615",
            end_date="2026-06-15",
        )

        self.assertEqual(result["date"].tolist(), ["2026-06-15"])
        self.assertEqual(result["close"].tolist(), [2])

    def test_invalid_history_date_warning_is_throttled(self):
        df = pd.DataFrame({
            "date": ["13598-74-57 15:00", "2026-06-16"],
            "close": [1, 2],
        })

        with patch("config.HISTORY_INVALID_DATE_LOG_INTERVAL", 600), \
             self.assertLogs("miniQMT.dm", level="DEBUG") as cm:
            self.dm._normalize_history_dates(df, "003025", source="Mootdx")
            self.dm._normalize_history_dates(df, "003025", source="Mootdx")

        warning_logs = [m for m in cm.output if "WARNING" in m and "非法历史日期" in m]
        debug_logs = [m for m in cm.output if "DEBUG" in m and "重复告警已降噪" in m]
        self.assertEqual(len(warning_logs), 1)
        self.assertEqual(len(debug_logs), 1)

    def test_download_history_prefers_xtdata_when_available(self):
        xt_df = pd.DataFrame({
            "date": ["2026-06-16"],
            "close": [10],
        })
        self.dm.xt = MagicMock()
        self.dm.download_history_xtdata = MagicMock(return_value=xt_df)

        with patch("Methods.getStockData") as mock_mootdx:
            result = self.dm.download_history_data("003025", start_date="20260616")

        self.dm.download_history_xtdata.assert_called_once()
        mock_mootdx.assert_not_called()
        self.assertIs(result, xt_df)

    def test_download_history_fallback_to_mootdx_and_filters_start_date(self):
        self.dm.xt = MagicMock()
        self.dm.download_history_xtdata = MagicMock(return_value=None)
        mootdx_df = pd.DataFrame({
            "datetime": ["2026-06-14 15:00", "2026-06-16 15:00"],
            "open": [1, 2],
            "high": [1, 2],
            "low": [1, 2],
            "close": [1, 2],
            "volume": [100, 200],
            "amount": [1000, 2000],
        })

        with patch("Methods.getStockData", return_value=mootdx_df):
            result = self.dm.download_history_data("003025", start_date="20260615")

        self.assertEqual(result["date"].tolist(), ["2026-06-16"])
        self.assertEqual(result["stock_code"].tolist(), ["003025"])

    def test_update_stock_data_skips_when_latest_is_today(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = ("2026-06-17",)
        conn = MagicMock()
        conn.cursor.return_value = cursor
        self.dm.conn = conn
        self.dm._db_lock = MagicMock()
        self.dm.download_history_data = MagicMock()

        with patch("data_manager.datetime") as mock_datetime:
            mock_datetime.now.return_value.strftime.return_value = "2026-06-17"
            self.dm.update_stock_data("003025")

        self.dm.download_history_data.assert_not_called()

    def test_update_stock_data_throttles_repeated_same_window(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = ("2026-06-15",)
        conn = MagicMock()
        conn.cursor.return_value = cursor
        self.dm.conn = conn
        self.dm._db_lock = MagicMock()
        self.dm.download_history_data = MagicMock(return_value=None)

        with patch("config.HISTORY_UPDATE_THROTTLE_SECONDS", 300), \
             patch("data_manager.datetime") as mock_datetime:
            mock_datetime.now.return_value.strftime.return_value = "2026-06-17"
            mock_datetime.strptime.side_effect = __import__("datetime").datetime.strptime
            self.dm.update_stock_data("003025")
            self.dm.update_stock_data("003025")

        self.dm.download_history_data.assert_called_once_with("003025", start_date="20260616")


if __name__ == "__main__":
    unittest.main()
