"""
Test 2: QMT Connection Diagnostic Test

Tests QMT installation, process status, and connectivity
"""

import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from test_base import TestBase
from test_utils import is_qmt_running, is_qmt_path_valid
from logger import get_logger

logger = get_logger("test_qmt_connection")


class TestQmtConnection(TestBase):
    """Test QMT connection and diagnostics"""

    @classmethod
    def setUpClass(cls):
        """Setup test class"""
        super().setUpClass()
        cls.qmt_available = is_qmt_running()

        if not cls.qmt_available:
            logger.warning("QMT is not running - some tests will be skipped")
        else:
            logger.info("QMT is running - all tests will execute")

    def test_01_qmt_path_configuration(self):
        """Test that QMT path is configured"""
        logger.info("Testing QMT path configuration")

        qmt_path = config.QMT_PATH

        self.assertIsNotNone(qmt_path, "QMT_PATH should be configured")
        self.assertIsInstance(qmt_path, str, "QMT_PATH should be a string")
        self.assertNotEqual(qmt_path, "", "QMT_PATH should not be empty")

        logger.info(f"QMT path configured: {qmt_path}")

    def test_02_qmt_path_exists(self):
        """Test that QMT installation path exists"""
        logger.info("Testing QMT path existence")

        qmt_path = config.QMT_PATH
        path_valid = is_qmt_path_valid()

        if path_valid:
            self.assertTrue(
                os.path.exists(qmt_path),
                f"QMT path should exist: {qmt_path}"
            )
            logger.info(f"QMT path exists: {qmt_path}")
        else:
            logger.warning(f"QMT path not found: {qmt_path}")
            logger.warning("This is acceptable if QMT is not installed")

    def test_03_qmt_process_status(self):
        """Test QMT process status"""
        logger.info("Testing QMT process status")

        qmt_running = is_qmt_running()

        if qmt_running:
            logger.info("QMT process is running")
        else:
            logger.warning("QMT process is not running")
            logger.warning("Real trading features will not be available")

        # This test passes either way, just reports status
        self.assertTrue(True, "QMT process status checked")

    @unittest.skipIf(not is_qmt_running(), "QMT not running")
    def test_04_qmt_api_import(self):
        """Test that QMT API can be imported"""
        logger.info("Testing QMT API import")

        try:
            from easy_qmt_trader import EasyQmtTrader
            logger.info("QMT API imported successfully")
            self.assertTrue(True)
        except ImportError as e:
            self.fail(f"Failed to import QMT API: {str(e)}")

    @unittest.skipIf(not is_qmt_running(), "QMT not running")
    def test_05_qmt_trader_initialization(self):
        """Test QMT trader initialization"""
        logger.info("Testing QMT trader initialization")

        try:
            from easy_qmt_trader import EasyQmtTrader

            # Try to create trader instance
            trader = EasyQmtTrader(
                qmt_path=config.QMT_PATH,
                session_id=999999  # Test session
            )

            self.assertIsNotNone(trader, "Trader should be initialized")
            logger.info("QMT trader initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize QMT trader: {str(e)}")
            self.fail(f"QMT trader initialization failed: {str(e)}")

    @unittest.skipIf(not is_qmt_running(), "QMT not running")
    def test_06_qmt_connection(self):
        """Test actual connection to QMT"""
        logger.info("Testing QMT connection")

        try:
            from easy_qmt_trader import EasyQmtTrader

            trader = EasyQmtTrader(
                qmt_path=config.QMT_PATH,
                session_id=999999
            )

            # Try to connect
            connected = trader.connect()

            self.assertTrue(connected, "Should connect to QMT successfully")
            logger.info("Connected to QMT successfully")

            # Disconnect
            trader.disconnect()
            logger.info("Disconnected from QMT")

        except Exception as e:
            logger.error(f"QMT connection failed: {str(e)}")
            self.fail(f"QMT connection failed: {str(e)}")

    @unittest.skipIf(not is_qmt_running(), "QMT not running")
    def test_07_account_validation(self):
        """Test account configuration validation"""
        logger.info("Testing account configuration")

        account_config = config.ACCOUNT_CONFIG

        # Validate account_id
        account_id = account_config.get('account_id')
        self.assertIsNotNone(account_id, "Account ID should be configured")
        self.assertNotEqual(account_id, "", "Account ID should not be empty")

        # Validate account_type
        account_type = account_config.get('account_type')
        self.assertIsNotNone(account_type, "Account type should be configured")
        self.assertIn(
            account_type,
            ['STOCK', 'CREDIT'],
            "Account type should be STOCK or CREDIT"
        )

        logger.info(f"Account configuration validated: {account_id} ({account_type})")

    def test_08_mock_trader_fallback(self):
        """Test that mock trader works as fallback"""
        logger.info("Testing mock trader fallback")

        from test_mocks import create_mock_qmt_trader

        mock_trader = create_mock_qmt_trader()

        self.assertIsNotNone(mock_trader, "Mock trader should be created")

        # Test mock connection
        connected = mock_trader.connect()
        self.assertTrue(connected, "Mock trader should connect")

        # Test mock is_connected
        self.assertTrue(mock_trader.is_connected(), "Mock trader should show connected")

        # Disconnect
        mock_trader.disconnect()
        self.assertFalse(mock_trader.is_connected(), "Mock trader should show disconnected")

        logger.info("Mock trader works correctly as fallback")

    def test_09_detect_qmt_availability(self):
        """Test QMT availability detection"""
        logger.info("Testing QMT availability detection")

        qmt_running = is_qmt_running()
        qmt_path_valid = is_qmt_path_valid()

        logger.info(f"QMT running: {qmt_running}")
        logger.info(f"QMT path valid: {qmt_path_valid}")

        if qmt_running and qmt_path_valid:
            logger.info("QMT is fully available")
            availability = "FULL"
        elif qmt_path_valid:
            logger.info("QMT installed but not running")
            availability = "INSTALLED"
        else:
            logger.info("QMT not available")
            availability = "UNAVAILABLE"

        # Store for other tests to use
        self.__class__.qmt_availability = availability

        self.assertIn(
            availability,
            ['FULL', 'INSTALLED', 'UNAVAILABLE'],
            "QMT availability should be one of the expected states"
        )

    def test_10_recommend_action(self):
        """Recommend action based on QMT status"""
        logger.info("Recommending action based on QMT status")

        qmt_running = is_qmt_running()
        qmt_path_valid = is_qmt_path_valid()

        if not qmt_path_valid:
            logger.warning("RECOMMENDATION: Configure QMT_PATH in account_config.json")
            logger.warning(f"Current path: {config.QMT_PATH}")

        if qmt_path_valid and not qmt_running:
            logger.warning("RECOMMENDATION: Start QMT client for real trading")
            logger.warning("Tests will use mock trader instead")

        if qmt_running and qmt_path_valid:
            logger.info("QMT is ready - real trading features available")

        # Always pass - this is informational
        self.assertTrue(True, "Recommendation provided")


def run_tests():
    """Run QMT connection tests"""
    suite = unittest.TestLoader().loadTestsFromTestCase(TestQmtConnection)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
