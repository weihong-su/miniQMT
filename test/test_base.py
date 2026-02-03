"""
Test Base Class - Foundation for all test modules

Provides:
- Setup/teardown with database backup/restore
- Test mode configuration
- Common assertions and utilities
- Mock QMT trader when unavailable
"""

import unittest
import os
import sys
import shutil
import sqlite3
from datetime import datetime
import threading
import time

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from logger import get_logger

logger = get_logger("test_base")


class TestBase(unittest.TestCase):
    """
    Base class for all miniQMT tests

    Features:
    - Automatic database backup/restore
    - Test mode configuration
    - Common test utilities
    - Cleanup after tests
    """

    # Class-level flags
    _db_backed_up = False
    _original_db_path = None
    _backup_db_path = None

    @classmethod
    def setUpClass(cls):
        """
        Setup test environment (runs once per test class)

        Actions:
        1. Backup production database
        2. Set test mode configuration
        3. Initialize test database
        """
        logger.info(f"=== Setting up test class: {cls.__name__} ===")

        # Backup production database
        cls._backup_database()

        # Override config for test mode
        cls._setup_test_config()

        logger.info(f"Test class {cls.__name__} setup complete")

    @classmethod
    def tearDownClass(cls):
        """
        Cleanup test environment (runs once per test class)

        Actions:
        1. Restore production database
        2. Clean up test artifacts
        """
        logger.info(f"=== Tearing down test class: {cls.__name__} ===")

        # Restore production database
        cls._restore_database()

        # Clean up test artifacts
        cls._cleanup_test_artifacts()

        logger.info(f"Test class {cls.__name__} teardown complete")

    def setUp(self):
        """Setup before each test method"""
        logger.info(f"--- Running test: {self._testMethodName} ---")
        self.start_time = time.time()

    def tearDown(self):
        """Cleanup after each test method"""
        duration = time.time() - self.start_time
        logger.info(f"--- Test {self._testMethodName} completed in {duration:.2f}s ---")

    @classmethod
    def _backup_database(cls):
        """Backup production database before testing"""
        if cls._db_backed_up:
            return

        db_path = config.DB_PATH
        if not os.path.exists(db_path):
            logger.warning(f"Production database not found: {db_path}")
            return

        # Create backup path
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{db_path}.backup_{timestamp}"

        try:
            shutil.copy2(db_path, backup_path)
            cls._original_db_path = db_path
            cls._backup_db_path = backup_path
            cls._db_backed_up = True
            logger.info(f"Database backed up: {backup_path}")
        except Exception as e:
            logger.error(f"Failed to backup database: {str(e)}")

    @classmethod
    def _restore_database(cls):
        """Restore production database after testing"""
        if not cls._db_backed_up or not cls._backup_db_path:
            return

        try:
            if os.path.exists(cls._backup_db_path):
                shutil.copy2(cls._backup_db_path, cls._original_db_path)
                logger.info(f"Database restored from: {cls._backup_db_path}")

                # Remove backup file
                os.remove(cls._backup_db_path)
                logger.info(f"Backup file removed: {cls._backup_db_path}")
        except Exception as e:
            logger.error(f"Failed to restore database: {str(e)}")

    @classmethod
    def _setup_test_config(cls):
        """Override configuration for test mode"""
        # Force simulation mode
        config.ENABLE_SIMULATION_MODE = True
        config.ENABLE_AUTO_TRADING = False
        config.DEBUG = True

        # Use test database path
        config.DB_PATH = "data/trading_test.db"

        # Use test log file
        config.LOG_FILE = "test/logs/test.log"

        # Set test simulation balance
        config.SIMULATION_BALANCE = 100000.0

        logger.info("Test configuration applied")

    @classmethod
    def _cleanup_test_artifacts(cls):
        """Clean up test-generated files"""
        # Remove test database
        test_db = "data/trading_test.db"
        if os.path.exists(test_db):
            try:
                os.remove(test_db)
                logger.info(f"Test database removed: {test_db}")
            except Exception as e:
                logger.warning(f"Failed to remove test database: {str(e)}")

    # ==================== Helper Methods ====================

    def create_test_db_connection(self):
        """Create a connection to test database"""
        return sqlite3.connect(config.DB_PATH)

    def create_memory_db(self):
        """Create an in-memory database for isolated testing"""
        return sqlite3.connect(":memory:")

    def create_test_position(self, conn, stock_code, volume, cost_price,
                            available=None, current_price=None):
        """
        Insert a test position into database

        Args:
            conn: Database connection
            stock_code: Stock code (e.g., '000001.SZ')
            volume: Total volume
            cost_price: Cost price
            available: Available volume (defaults to volume)
            current_price: Current price (defaults to cost_price)

        Returns:
            Position data dict
        """
        if available is None:
            available = volume
        if current_price is None:
            current_price = cost_price

        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO positions
            (stock_code, volume, available, cost_price, current_price, open_date)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (stock_code, volume, available, cost_price, current_price,
              datetime.now().strftime("%Y-%m-%d")))
        conn.commit()

        return {
            'stock_code': stock_code,
            'volume': volume,
            'available': available,
            'cost_price': cost_price,
            'current_price': current_price
        }

    def assert_thread_running(self, thread_name, timeout=5):
        """
        Assert that a specific thread is running

        Args:
            thread_name: Name of the thread to check
            timeout: Maximum wait time in seconds
        """
        start_time = time.time()
        while time.time() - start_time < timeout:
            for thread in threading.enumerate():
                if thread_name.lower() in thread.name.lower():
                    logger.debug(f"Thread found: {thread.name}")
                    return True
            time.sleep(0.1)

        self.fail(f"Thread '{thread_name}' not found within {timeout}s")

    def assert_thread_stopped(self, thread_name, timeout=5):
        """
        Assert that a specific thread has stopped

        Args:
            thread_name: Name of the thread to check
            timeout: Maximum wait time in seconds
        """
        start_time = time.time()
        while time.time() - start_time < timeout:
            found = False
            for thread in threading.enumerate():
                if thread_name.lower() in thread.name.lower():
                    found = True
                    break

            if not found:
                logger.debug(f"Thread stopped: {thread_name}")
                return True

            time.sleep(0.1)

        self.fail(f"Thread '{thread_name}' still running after {timeout}s")

    def assert_file_exists(self, file_path):
        """Assert that a file exists"""
        self.assertTrue(os.path.exists(file_path),
                       f"File not found: {file_path}")

    def assert_database_table_exists(self, conn, table_name):
        """Assert that a database table exists"""
        cursor = conn.cursor()
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name=?
        """, (table_name,))
        result = cursor.fetchone()
        self.assertIsNotNone(result,
                            f"Table '{table_name}' does not exist")

    def get_table_row_count(self, conn, table_name):
        """Get row count from a table"""
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
        return cursor.fetchone()[0]

    def wait_for_condition(self, condition_func, timeout=10, interval=0.1):
        """
        Wait for a condition to become True

        Args:
            condition_func: Function that returns True when condition is met
            timeout: Maximum wait time in seconds
            interval: Check interval in seconds

        Returns:
            True if condition met, False otherwise
        """
        start_time = time.time()
        while time.time() - start_time < timeout:
            if condition_func():
                return True
            time.sleep(interval)
        return False
