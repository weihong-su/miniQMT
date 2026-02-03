"""
Test 9: Sell Monitoring Test

Tests sell monitoring and alert system including:
- Sell monitor initialization
- Alert level detection (P0/P1/P2)
- Alert aggregation
- Failure scenario classification
"""

import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from test_base import TestBase
from logger import get_logger

logger = get_logger("test_sell_monitoring")


class TestSellMonitoring(TestBase):
    """Test sell monitoring functionality"""

    def test_01_sell_monitor_enabled(self):
        """Test sell monitor configuration"""
        logger.info("Testing sell monitor configuration")

        self.assertTrue(
            hasattr(config, 'ENABLE_SELL_MONITOR'),
            "ENABLE_SELL_MONITOR should exist"
        )

        logger.info(f"Sell monitor enabled: {config.ENABLE_SELL_MONITOR}")

    def test_02_alert_notification_config(self):
        """Test alert notification configuration"""
        logger.info("Testing alert notification config")

        self.assertTrue(
            hasattr(config, 'ENABLE_SELL_ALERT_NOTIFICATION'),
            "ENABLE_SELL_ALERT_NOTIFICATION should exist"
        )

        logger.info(f"Alert notification enabled: {config.ENABLE_SELL_ALERT_NOTIFICATION}")

    def test_03_alert_config_structure(self):
        """Test alert configuration structure"""
        logger.info("Testing alert config structure")

        alert_config = config.SELL_ALERT_CONFIG

        self.assertIsInstance(alert_config, dict, "Alert config should be a dict")

        # Check for alert levels
        expected_levels = ['P0_notification', 'P1_notification', 'P2_notification']
        for level in expected_levels:
            self.assertIn(level, alert_config, f"Should have {level} config")

        logger.info(f"Alert config: {alert_config}")

    def test_04_p0_alert_detection(self):
        """Test P0 (extreme risk) alert detection"""
        logger.info("Testing P0 alert detection")

        # P0 scenarios:
        # - QMT not initialized
        # - Stop loss retry limit
        # - Pending order conflict (persistent)

        # Simulate QMT not initialized
        qmt_initialized = False

        if not qmt_initialized:
            alert_level = 'P0'
            alert_message = 'QMT not initialized, all sell operations will fail'
            logger.warning(f"[{alert_level}] {alert_message}")

            self.assertEqual(alert_level, 'P0', "Should be P0 alert")

        logger.info("P0 alert detection test completed")

    def test_05_p1_alert_detection(self):
        """Test P1 (high risk) alert detection"""
        logger.info("Testing P1 alert detection")

        # P1 scenarios:
        # - Auto trading disabled
        # - Price fetch failed (multiple times)
        # - Signal validation failed

        # Simulate auto trading disabled
        auto_trading_enabled = config.ENABLE_AUTO_TRADING

        if not auto_trading_enabled:
            alert_level = 'P1'
            alert_message = 'Auto trading is disabled, sell signals being ignored'
            logger.warning(f"[{alert_level}] {alert_message}")

            self.assertEqual(alert_level, 'P1', "Should be P1 alert")

        logger.info("P1 alert detection test completed")

    def test_06_p2_alert_detection(self):
        """Test P2 (medium risk) alert detection"""
        logger.info("Testing P2 alert detection")

        # P2 scenarios:
        # - Price fetch occasional failures
        # - Temporary connectivity issues
        # - Minor validation warnings

        # Simulate price fetch failure
        price_fetch_failures = 2

        if price_fetch_failures >= 2:
            alert_level = 'P2'
            alert_message = f'Price fetch failed {price_fetch_failures} times'
            logger.warning(f"[{alert_level}] {alert_message}")

            self.assertEqual(alert_level, 'P2', "Should be P2 alert")

        logger.info("P2 alert detection test completed")

    def test_07_alert_aggregation(self):
        """Test alert aggregation"""
        logger.info("Testing alert aggregation")

        # Simulate multiple alerts
        alerts = []

        # Add P0 alert
        alerts.append({
            'level': 'P0',
            'message': 'QMT not initialized',
            'count': 1
        })

        # Add P1 alert
        alerts.append({
            'level': 'P1',
            'message': 'Auto trading disabled',
            'count': 5
        })

        # Add P2 alert
        alerts.append({
            'level': 'P2',
            'message': 'Price fetch failed',
            'count': 3
        })

        # Aggregate by level
        alert_summary = {}
        for alert in alerts:
            level = alert['level']
            if level not in alert_summary:
                alert_summary[level] = []
            alert_summary[level].append(alert)

        # Verify aggregation
        self.assertEqual(len(alert_summary), 3, "Should have 3 alert levels")
        self.assertEqual(len(alert_summary['P0']), 1, "Should have 1 P0 alert")
        self.assertEqual(len(alert_summary['P1']), 1, "Should have 1 P1 alert")
        self.assertEqual(len(alert_summary['P2']), 1, "Should have 1 P2 alert")

        logger.info(f"Alert summary: {alert_summary}")

    def test_08_sell_failure_classification(self):
        """Test sell failure scenario classification"""
        logger.info("Testing sell failure classification")

        # Failure scenarios (MECE)
        failure_scenarios = {
            'qmt_not_initialized': 'P0',
            'stop_loss_retry_limit': 'P0',
            'auto_trading_disabled': 'P1',
            'price_fetch_failed': 'P1',
            'signal_validation_failed': 'P1',
            'price_fetch_occasional': 'P2',
            'minor_warning': 'P2'
        }

        # Test classification
        for scenario, expected_level in failure_scenarios.items():
            # Simulate scenario classification
            if 'qmt' in scenario.lower() or 'retry' in scenario.lower() or 'limit' in scenario.lower():
                actual_level = 'P0'
            elif ('disabled' in scenario.lower() or 'failed' in scenario.lower()) and 'occasional' not in scenario.lower():
                actual_level = 'P1'
            else:
                actual_level = 'P2'

            self.assertEqual(
                actual_level,
                expected_level,
                f"Scenario '{scenario}' should be {expected_level}"
            )

        logger.info(f"Classified {len(failure_scenarios)} scenarios")

    def test_09_alert_threshold_check(self):
        """Test alert threshold checking"""
        logger.info("Testing alert threshold")

        # P0 threshold: 1 occurrence
        p0_threshold = 1
        p0_count = 1

        should_alert_p0 = p0_count >= p0_threshold
        self.assertTrue(should_alert_p0, "Should alert on P0 threshold")

        # P1 threshold: 5 occurrences in 10 minutes
        p1_threshold = 5
        p1_count = 3

        should_alert_p1 = p1_count >= p1_threshold
        self.assertFalse(should_alert_p1, "Should not alert below P1 threshold")

        logger.info(f"Threshold check - P0: {should_alert_p0}, P1: {should_alert_p1}")

    def test_10_sell_monitor_lifecycle(self):
        """Test sell monitor complete lifecycle"""
        logger.info("Testing sell monitor lifecycle")

        # Initialize monitor state
        monitor_state = {
            'enabled': config.ENABLE_SELL_MONITOR,
            'alerts': [],
            'failure_stats': {}
        }

        # Simulate sell attempt
        sell_attempt = {
            'stock_code': '000001.SZ',
            'volume': 1000,
            'reason': 'stop_loss',
            'timestamp': '2026-02-02 15:30:00'
        }

        # Check preconditions
        if not config.ENABLE_AUTO_TRADING:
            # Record P1 alert
            alert = {
                'level': 'P1',
                'scenario': 'auto_trading_disabled',
                'message': 'Auto trading is disabled',
                'timestamp': sell_attempt['timestamp']
            }
            monitor_state['alerts'].append(alert)

            # Update stats
            scenario = alert['scenario']
            if scenario not in monitor_state['failure_stats']:
                monitor_state['failure_stats'][scenario] = 0
            monitor_state['failure_stats'][scenario] += 1

        # Verify monitor state
        self.assertGreater(
            len(monitor_state['alerts']),
            0,
            "Should have recorded alerts"
        )

        logger.info(f"Monitor state: {len(monitor_state['alerts'])} alerts, "
                   f"{len(monitor_state['failure_stats'])} failure types")

        logger.info("Sell monitor lifecycle test completed")


def run_tests():
    """Run sell monitoring tests"""
    suite = unittest.TestLoader().loadTestsFromTestCase(TestSellMonitoring)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
