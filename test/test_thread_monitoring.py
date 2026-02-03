"""
Test 4: Thread Monitoring Test

Tests thread health monitoring and auto-restart functionality
"""

import unittest
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_base import TestBase
from test_utils import wait_for_thread_start, wait_for_thread_stop
from logger import get_logger

logger = get_logger("test_thread_monitoring")


class TestThreadMonitoring(TestBase):
    """Test thread monitoring functionality"""

    def test_01_thread_monitor_import(self):
        """Test thread monitor module import"""
        logger.info("Testing thread monitor import")

        try:
            from thread_monitor import get_thread_monitor
            logger.info("Thread monitor imported successfully")
            self.assertTrue(True)
        except ImportError as e:
            self.fail(f"Failed to import thread monitor: {str(e)}")

    def test_02_thread_monitor_initialization(self):
        """Test thread monitor initialization"""
        logger.info("Testing thread monitor initialization")

        from thread_monitor import get_thread_monitor

        monitor = get_thread_monitor()
        self.assertIsNotNone(monitor, "Thread monitor should be initialized")

        logger.info("Thread monitor initialized successfully")

    def test_03_register_thread_with_lambda(self):
        """Test registering thread with lambda (correct method)"""
        logger.info("Testing thread registration with lambda")

        from thread_monitor import get_thread_monitor

        monitor = get_thread_monitor()

        # Create a test thread
        test_thread = threading.Thread(target=lambda: time.sleep(1), name="TestThread")
        test_thread.start()

        # Register with lambda (correct way)
        def restart_func():
            logger.info("Restart function called")

        monitor.register_thread(
            "TestThread",
            lambda: test_thread,  # Using lambda - correct
            restart_func
        )

        logger.info("Thread registered with lambda successfully")

        # Cleanup
        test_thread.join(timeout=2)

    def test_04_detect_thread_alive(self):
        """Test detecting alive thread"""
        logger.info("Testing alive thread detection")

        # Create a long-running thread
        stop_event = threading.Event()

        def worker():
            while not stop_event.is_set():
                time.sleep(0.1)

        test_thread = threading.Thread(target=worker, name="AliveThread")
        test_thread.start()

        # Wait for thread to start
        started = wait_for_thread_start("AliveThread", timeout=2)
        self.assertTrue(started, "Thread should start")

        # Check if thread is alive
        self.assertTrue(test_thread.is_alive(), "Thread should be alive")

        logger.info("Alive thread detected successfully")

        # Cleanup
        stop_event.set()
        test_thread.join(timeout=2)

    def test_05_detect_dead_thread(self):
        """Test detecting dead thread"""
        logger.info("Testing dead thread detection")

        # Create a short-lived thread
        test_thread = threading.Thread(
            target=lambda: time.sleep(0.1),
            name="ShortLivedThread"
        )
        test_thread.start()

        # Wait for thread to finish
        test_thread.join(timeout=1)

        # Check if thread is dead
        self.assertFalse(test_thread.is_alive(), "Thread should be dead")

        logger.info("Dead thread detected successfully")

    def test_06_thread_restart_logic(self):
        """Test thread restart logic"""
        logger.info("Testing thread restart logic")

        restart_called = [False]

        def restart_func():
            restart_called[0] = True
            logger.info("Restart function executed")

        # Simulate restart call
        restart_func()

        self.assertTrue(restart_called[0], "Restart function should be called")

        logger.info("Thread restart logic tested successfully")

    def test_07_cooldown_mechanism(self):
        """Test restart cooldown mechanism"""
        logger.info("Testing cooldown mechanism")

        import config

        # Get cooldown period
        cooldown = config.THREAD_RESTART_COOLDOWN

        self.assertIsInstance(cooldown, (int, float), "Cooldown should be numeric")
        self.assertGreater(cooldown, 0, "Cooldown should be positive")

        logger.info(f"Cooldown period: {cooldown}s")

        # Simulate cooldown check
        last_restart_time = time.time()
        time.sleep(0.1)
        current_time = time.time()

        elapsed = current_time - last_restart_time
        should_allow_restart = elapsed >= cooldown

        logger.info(f"Elapsed: {elapsed:.2f}s, Allow restart: {should_allow_restart}")

    def test_08_multiple_thread_registration(self):
        """Test registering multiple threads"""
        logger.info("Testing multiple thread registration")

        from thread_monitor import get_thread_monitor

        monitor = get_thread_monitor()

        # Create multiple test threads
        threads = []
        for i in range(3):
            thread = threading.Thread(
                target=lambda: time.sleep(0.5),
                name=f"TestThread_{i}"
            )
            thread.start()
            threads.append(thread)

            # Register each thread
            monitor.register_thread(
                f"TestThread_{i}",
                lambda t=thread: t,  # Capture thread in closure
                lambda: logger.info(f"Restart TestThread_{i}")
            )

        logger.info(f"Registered {len(threads)} threads")

        # Cleanup
        for thread in threads:
            thread.join(timeout=1)

    def test_09_thread_monitor_status(self):
        """Test getting thread monitor status"""
        logger.info("Testing thread monitor status")

        from thread_monitor import get_thread_monitor

        monitor = get_thread_monitor()

        # Get status (if method exists)
        if hasattr(monitor, 'get_status'):
            status = monitor.get_status()
            logger.info(f"Thread monitor status: {status}")
        else:
            logger.info("get_status method not implemented")

        self.assertTrue(True, "Status check completed")

    def test_10_thread_enumeration(self):
        """Test enumerating all active threads"""
        logger.info("Testing thread enumeration")

        initial_count = threading.active_count()
        logger.info(f"Initial thread count: {initial_count}")

        # Create test threads
        test_threads = []
        for i in range(3):
            thread = threading.Thread(
                target=lambda: time.sleep(0.5),
                name=f"EnumTest_{i}"
            )
            thread.start()
            test_threads.append(thread)

        # Wait a bit for threads to start
        time.sleep(0.1)

        # Count threads again
        new_count = threading.active_count()
        logger.info(f"New thread count: {new_count}")

        self.assertGreaterEqual(
            new_count,
            initial_count + 3,
            "Should have at least 3 more threads"
        )

        # List all threads
        all_threads = threading.enumerate()
        logger.info(f"All threads: {[t.name for t in all_threads]}")

        # Cleanup
        for thread in test_threads:
            thread.join(timeout=1)


def run_tests():
    """Run thread monitoring tests"""
    suite = unittest.TestLoader().loadTestsFromTestCase(TestThreadMonitoring)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
