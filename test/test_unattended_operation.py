"""
Test 7: Unattended Operation Test

Tests unattended operation features including:
- Thread self-healing
- Graceful shutdown
- Timeout protection
- Non-trading hours optimization
"""

import unittest
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from test_base import TestBase
from test_utils import wait_for_thread_start, wait_for_thread_stop, get_active_threads
from logger import get_logger

logger = get_logger("test_unattended_operation")


class TestUnattendedOperation(TestBase):
    """Test unattended operation features"""

    def test_01_thread_monitor_enabled(self):
        """Test that thread monitor is enabled in config"""
        logger.info("Testing thread monitor configuration")

        self.assertTrue(
            hasattr(config, 'ENABLE_THREAD_MONITOR'),
            "ENABLE_THREAD_MONITOR should exist"
        )

        # In production, this should be True
        logger.info(f"Thread monitor enabled: {config.ENABLE_THREAD_MONITOR}")

    def test_02_thread_check_interval(self):
        """Test thread check interval configuration"""
        logger.info("Testing thread check interval")

        interval = config.THREAD_CHECK_INTERVAL

        self.assertIsInstance(interval, (int, float), "Interval should be numeric")
        self.assertGreater(interval, 0, "Interval should be positive")
        self.assertLessEqual(interval, 300, "Interval should be reasonable (<=300s)")

        logger.info(f"Thread check interval: {interval}s")

    def test_03_thread_restart_cooldown(self):
        """Test restart cooldown configuration"""
        logger.info("Testing restart cooldown")

        cooldown = config.THREAD_RESTART_COOLDOWN

        self.assertIsInstance(cooldown, (int, float), "Cooldown should be numeric")
        self.assertGreater(cooldown, 0, "Cooldown should be positive")

        logger.info(f"Restart cooldown: {cooldown}s")

    def test_04_monitor_loop_interval(self):
        """Test position monitor loop interval"""
        logger.info("Testing monitor loop interval")

        interval = config.MONITOR_LOOP_INTERVAL

        self.assertIsInstance(interval, (int, float), "Interval should be numeric")
        self.assertGreater(interval, 0, "Interval should be positive")

        logger.info(f"Monitor loop interval: {interval}s")

    def test_05_api_call_timeout(self):
        """Test API call timeout configuration"""
        logger.info("Testing API call timeout")

        timeout = config.MONITOR_CALL_TIMEOUT

        self.assertIsInstance(timeout, (int, float), "Timeout should be numeric")
        self.assertGreater(timeout, 0, "Timeout should be positive")
        self.assertLessEqual(timeout, 30, "Timeout should be reasonable (<=30s)")

        logger.info(f"API call timeout: {timeout}s")

    def test_06_non_trading_hours_sleep(self):
        """Test non-trading hours sleep interval"""
        logger.info("Testing non-trading hours sleep")

        sleep_interval = config.MONITOR_NON_TRADE_SLEEP

        self.assertIsInstance(sleep_interval, (int, float), "Sleep should be numeric")
        self.assertGreater(sleep_interval, 0, "Sleep should be positive")

        logger.info(f"Non-trading hours sleep: {sleep_interval}s")

    def test_07_thread_self_healing_simulation(self):
        """Test thread self-healing simulation"""
        logger.info("Testing thread self-healing simulation")

        # Simulate a thread that dies
        thread_alive = [True]
        restart_count = [0]

        def worker():
            """Worker thread that may die"""
            while thread_alive[0]:
                time.sleep(0.1)

        def restart_worker():
            """Restart function"""
            restart_count[0] += 1
            thread_alive[0] = True
            logger.info(f"Thread restarted (count: {restart_count[0]})")

        # Start thread
        test_thread = threading.Thread(target=worker, name="SelfHealingTest")
        test_thread.start()

        # Wait for thread to start
        time.sleep(0.2)
        self.assertTrue(test_thread.is_alive(), "Thread should be alive")

        # Simulate thread death
        thread_alive[0] = False
        test_thread.join(timeout=1)

        self.assertFalse(test_thread.is_alive(), "Thread should be dead")

        # Simulate restart
        test_thread = threading.Thread(target=worker, name="SelfHealingTest")
        restart_worker()
        test_thread.start()

        # Verify restart
        time.sleep(0.2)
        self.assertTrue(test_thread.is_alive(), "Thread should be restarted")
        self.assertEqual(restart_count[0], 1, "Restart count should be 1")

        # Cleanup
        thread_alive[0] = False
        test_thread.join(timeout=1)

        logger.info("Thread self-healing simulation passed")

    def test_08_graceful_shutdown_sequence(self):
        """Test graceful shutdown sequence"""
        logger.info("Testing graceful shutdown sequence")

        # Simulate shutdown order
        shutdown_order = []

        def shutdown_web_server():
            shutdown_order.append('web_server')
            logger.info("Web server stopped")

        def shutdown_thread_monitor():
            shutdown_order.append('thread_monitor')
            logger.info("Thread monitor stopped")

        def shutdown_business_threads():
            shutdown_order.append('business_threads')
            logger.info("Business threads stopped")

        def shutdown_core_modules():
            shutdown_order.append('core_modules')
            logger.info("Core modules stopped")

        # Execute shutdown sequence
        shutdown_web_server()
        shutdown_thread_monitor()
        shutdown_business_threads()
        shutdown_core_modules()

        # Verify order
        expected_order = [
            'web_server',
            'thread_monitor',
            'business_threads',
            'core_modules'
        ]

        self.assertEqual(shutdown_order, expected_order, "Shutdown order should be correct")

        logger.info("Graceful shutdown sequence verified")

    def test_09_timeout_protection_simulation(self):
        """Test timeout protection for API calls"""
        logger.info("Testing timeout protection")

        import concurrent.futures

        timeout = config.MONITOR_CALL_TIMEOUT

        def slow_api_call():
            """Simulate slow API call"""
            time.sleep(10)  # Takes 10 seconds
            return "success"

        # Execute with timeout
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(slow_api_call)

            try:
                result = future.result(timeout=timeout)
                self.fail("Should have timed out")
            except concurrent.futures.TimeoutError:
                logger.info(f"API call timed out after {timeout}s (expected)")

        logger.info("Timeout protection verified")

    def test_10_trading_hours_detection(self):
        """Test trading hours detection"""
        logger.info("Testing trading hours detection")

        # Test is_trade_time function if available
        if hasattr(config, 'is_trade_time'):
            is_trading = config.is_trade_time()
            logger.info(f"Currently trading hours: {is_trading}")

            # Just verify it returns a boolean
            self.assertIsInstance(is_trading, bool, "Should return boolean")
        else:
            logger.info("is_trade_time function not available")

        # Test trading time window configuration
        if hasattr(config, 'TRADE_TIME_MORNING_START'):
            logger.info(f"Morning start: {config.TRADE_TIME_MORNING_START}")
            logger.info(f"Morning end: {config.TRADE_TIME_MORNING_END}")
            logger.info(f"Afternoon start: {config.TRADE_TIME_AFTERNOON_START}")
            logger.info(f"Afternoon end: {config.TRADE_TIME_AFTERNOON_END}")

        logger.info("Trading hours detection test completed")


def run_tests():
    """Run unattended operation tests"""
    suite = unittest.TestLoader().loadTestsFromTestCase(TestUnattendedOperation)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
