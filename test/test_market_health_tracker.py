import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import data_manager as data_manager_module


class TestMarketDataHealthTrackerCore(unittest.TestCase):
    """行情源健康评分器核心规则测试。"""

    def _record_at(self, tracker, timestamp, source="xtdata", purpose="realtime",
                   stock_code="000920.SZ", ok=True, latency_ms=0, reason="",
                   data_quality_ok=True):
        with patch("data_manager.time.time", return_value=timestamp):
            tracker.record(
                source=source,
                purpose=purpose,
                stock_code=stock_code,
                ok=ok,
                latency_ms=latency_ms,
                reason=reason,
                data_quality_ok=data_quality_ok,
            )

    def _score_at(self, tracker, timestamp, **filters):
        with patch("data_manager.time.time", return_value=timestamp):
            return tracker.get_score(**filters)

    def test_disabled_tracker_ignores_records(self):
        tracker = data_manager_module.MarketDataHealthTracker()

        with patch.object(config, "MARKET_HEALTH_ENABLED", False):
            self._record_at(tracker, 1000, ok=True)

        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["overall"]["event_count"], 0)
        self.assertEqual(snapshot["sources"], {})
        self.assertEqual(tracker.format_summary(), "行情健康: 暂无样本")

    def test_less_than_min_events_returns_unknown_but_keeps_counts(self):
        tracker = data_manager_module.MarketDataHealthTracker()

        with patch.object(config, "MARKET_HEALTH_MIN_EVENTS", 3):
            self._record_at(tracker, 1000, ok=True)
            self._record_at(tracker, 1001, ok=True)
            score = self._score_at(tracker, 1002, source="xtdata")

            self.assertIsNone(score["score"])
            self.assertEqual(score["status"], "unknown")
            self.assertEqual(score["event_count"], 2)
            self.assertEqual(score["success_count"], 2)
            self.assertEqual(score["failure_count"], 0)
            self.assertEqual(score["last_reason"], "success")

            self._record_at(tracker, 1003, ok=True)
            score = self._score_at(tracker, 1003, source="xtdata")

        self.assertEqual(score["score"], 100)
        self.assertEqual(score["status"], "healthy")
        self.assertEqual(score["event_count"], 3)

    def test_window_filters_expired_events_and_summary_marks_mootdx_idle(self):
        tracker = data_manager_module.MarketDataHealthTracker()

        with patch.object(config, "MARKET_HEALTH_MIN_EVENTS", 1), \
             patch.object(config, "MARKET_HEALTH_WINDOW_SECONDS", 300):
            self._record_at(
                tracker,
                1000,
                source="Mootdx",
                stock_code="000920",
                ok=True,
            )
            self._record_at(
                tracker,
                1000,
                source="Tushare",
                purpose="history",
                stock_code="000920",
                ok=True,
            )
            score = self._score_at(tracker, 1301, source="Mootdx")
            with patch("data_manager.time.time", return_value=1301):
                summary = tracker.format_summary()

        self.assertIsNone(score["score"])
        self.assertEqual(score["status"], "unknown")
        self.assertEqual(score["event_count"], 0)
        self.assertIn("Mootdx=idle", summary)
        self.assertIn("Tushare=idle", summary)

    def test_max_events_limits_each_source_purpose_stock_bucket(self):
        with patch.object(config, "MARKET_HEALTH_MAX_EVENTS", 3), \
             patch.object(config, "MARKET_HEALTH_MIN_EVENTS", 1):
            tracker = data_manager_module.MarketDataHealthTracker()
            for index in range(5):
                self._record_at(tracker, 1000 + index, reason=f"event-{index}")
            score = self._score_at(tracker, 1005, source="xtdata")

        self.assertEqual(score["event_count"], 3)
        self.assertEqual(score["last_reason"], "event-4")

    def test_score_formula_uses_success_latency_freshness_quality_and_streak(self):
        tracker = data_manager_module.MarketDataHealthTracker()

        with patch.object(config, "MARKET_HEALTH_MIN_EVENTS", 3), \
             patch.object(config, "MARKET_HEALTH_WINDOW_SECONDS", 300):
            self._record_at(tracker, 1000, ok=True, latency_ms=30)
            self._record_at(tracker, 1001, ok=False, latency_ms=3000, reason="timeout")
            self._record_at(
                tracker,
                1002,
                ok=False,
                latency_ms=0,
                reason="invalid_price",
                data_quality_ok=False,
            )
            score = self._score_at(tracker, 1002, source="xtdata")

        self.assertEqual(score["event_count"], 3)
        self.assertEqual(score["success_count"], 1)
        self.assertEqual(score["failure_count"], 2)
        self.assertEqual(score["consecutive_failures"], 2)
        self.assertEqual(score["avg_latency_ms"], 30)
        self.assertEqual(score["score"], 64)
        self.assertEqual(score["status"], "degraded")

    def test_status_threshold_boundaries(self):
        tracker = data_manager_module.MarketDataHealthTracker()

        self.assertEqual(tracker._status_for_score(None), "unknown")
        self.assertEqual(tracker._status_for_score(80), "healthy")
        self.assertEqual(tracker._status_for_score(79), "degraded")
        self.assertEqual(tracker._status_for_score(60), "degraded")
        self.assertEqual(tracker._status_for_score(59), "unstable")
        self.assertEqual(tracker._status_for_score(40), "unstable")
        self.assertEqual(tracker._status_for_score(39), "down")

    def test_snapshot_and_filtering_are_source_purpose_stock_scoped(self):
        tracker = data_manager_module.MarketDataHealthTracker()

        with patch.object(config, "MARKET_HEALTH_MIN_EVENTS", 1):
            self._record_at(tracker, 1000, source="xtdata", purpose="realtime", stock_code="000920.SZ")
            self._record_at(tracker, 1001, source="xtdata", purpose="history", stock_code="000920.SZ")
            self._record_at(tracker, 1002, source="Mootdx", purpose="realtime", stock_code="000920")
            self._record_at(tracker, 1003, source="xtdata", purpose="realtime", stock_code="300712.SZ")

            realtime_score = self._score_at(
                tracker,
                1003,
                source="xtdata",
                purpose="realtime",
                stock_code="000920.SZ",
            )
            snapshot = tracker.snapshot()

        self.assertEqual(realtime_score["event_count"], 1)
        self.assertIn("xtdata", snapshot["sources"])
        self.assertIn("Mootdx", snapshot["sources"])
        self.assertIn("000920.SZ", snapshot["stocks"])
        self.assertIn("300712.SZ", snapshot["stocks"])
        self.assertIn("realtime", snapshot["stocks"]["000920.SZ"])
        self.assertIn("history", snapshot["stocks"]["000920.SZ"])

    def test_format_summary_lists_low_score_realtime_stocks(self):
        tracker = data_manager_module.MarketDataHealthTracker()

        with patch.object(config, "MARKET_HEALTH_MIN_EVENTS", 3):
            for index in range(3):
                self._record_at(
                    tracker,
                    1000 + index,
                    ok=False,
                    reason="invalid_price",
                    data_quality_ok=False,
                )
            with patch("data_manager.time.time", return_value=1002):
                summary = tracker.format_summary()

        self.assertIn("xtdata=down", summary)
        self.assertIn("异常股票:000920.SZ", summary)


class TestMarketHealthTradingGate(unittest.TestCase):
    """行情健康评分参与交易信号检测的放行/拦截规则。"""

    def setUp(self):
        self.dm = data_manager_module.DataManager.__new__(data_manager_module.DataManager)
        self.dm.market_health = data_manager_module.MarketDataHealthTracker()

    def _record_good_xtdata_samples(self, stock_code="000920.SZ"):
        with patch.object(config, "MARKET_HEALTH_MIN_EVENTS", 3):
            for index in range(3):
                with patch("data_manager.time.time", return_value=1000 + index):
                    self.dm.market_health.record(
                        "xtdata",
                        "realtime",
                        stock_code,
                        True,
                        latency_ms=0,
                    )

    def test_disabled_or_observe_only_mode_never_blocks(self):
        with patch.object(config, "MARKET_HEALTH_ENABLED", False):
            self.assertTrue(self.dm.is_quote_tradable("000920.SZ", None))

        with patch.object(config, "MARKET_HEALTH_ENABLED", True), \
             patch.object(config, "MARKET_HEALTH_OBSERVE_ONLY", True):
            self.assertTrue(self.dm.is_quote_tradable("000920.SZ", None))

    def test_strict_mode_blocks_missing_quote_and_unknown_score(self):
        with patch.object(config, "MARKET_HEALTH_ENABLED", True), \
             patch.object(config, "MARKET_HEALTH_OBSERVE_ONLY", False):
            self.assertFalse(self.dm.is_quote_tradable("000920.SZ", None))
            self.assertFalse(self.dm.is_quote_tradable(
                "000920.SZ",
                {"lastPrice": 10.0, "_source": "xtdata", "_purpose": "realtime"},
            ))

    def test_strict_mode_uses_tracker_score_when_quote_has_no_score(self):
        self._record_good_xtdata_samples()
        quote = {"lastPrice": 10.0, "_source": "xtdata", "_purpose": "realtime"}

        with patch.object(config, "MARKET_HEALTH_OBSERVE_ONLY", False), \
             patch.object(config, "MARKET_HEALTH_TRADING_MIN_SCORE", 70), \
             patch("data_manager.time.time", return_value=1002):
            self.assertTrue(self.dm.is_quote_tradable("000920.SZ", quote))

    def test_strict_mode_applies_score_threshold_and_mootdx_source_policy(self):
        xtdata_low_score = {
            "lastPrice": 10.0,
            "_source": "xtdata",
            "_purpose": "realtime",
            "_health_score": 69,
        }
        mootdx_high_score = {
            "lastPrice": 10.0,
            "_source": "Mootdx",
            "_purpose": "realtime",
            "_health_score": 100,
        }

        with patch.object(config, "MARKET_HEALTH_OBSERVE_ONLY", False), \
             patch.object(config, "MARKET_HEALTH_TRADING_MIN_SCORE", 70), \
             patch.object(config, "MARKET_HEALTH_ALLOW_MOOTDX_FOR_TRADING", False):
            self.assertFalse(self.dm.is_quote_tradable("000920.SZ", xtdata_low_score))
            self.assertFalse(self.dm.is_quote_tradable("000920", mootdx_high_score))

        with patch.object(config, "MARKET_HEALTH_OBSERVE_ONLY", False), \
             patch.object(config, "MARKET_HEALTH_TRADING_MIN_SCORE", 70), \
             patch.object(config, "MARKET_HEALTH_ALLOW_MOOTDX_FOR_TRADING", True):
            self.assertTrue(self.dm.is_quote_tradable("000920", mootdx_high_score))


if __name__ == "__main__":
    unittest.main(verbosity=2)
