"""
Test 1: Configuration Management Test

Tests configuration loading, validation, and persistence
"""

import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from test_base import TestBase
from test_config import validate_config, CONFIG_VALIDATION_RULES
from logger import get_logger

logger = get_logger("test_config_management")


class TestConfigManagement(TestBase):
    """Test configuration management functionality"""

    def test_01_load_default_configuration(self):
        """Test that default configuration loads successfully"""
        logger.info("Testing default configuration loading")

        # Check that key configuration values are loaded
        self.assertIsNotNone(config.ENABLE_SIMULATION_MODE)
        self.assertIsNotNone(config.ENABLE_AUTO_TRADING)
        self.assertIsNotNone(config.DB_PATH)
        self.assertIsNotNone(config.SIMULATION_BALANCE)

        logger.info("Default configuration loaded successfully")

    def test_02_validate_required_fields(self):
        """Test that all required configuration fields are present"""
        logger.info("Testing required configuration fields")

        required_fields = [
            'ENABLE_SIMULATION_MODE',
            'ENABLE_AUTO_TRADING',
            'DB_PATH',
            'SIMULATION_BALANCE',
            'STOP_LOSS_RATIO',
            'INITIAL_TAKE_PROFIT_RATIO'
        ]

        for field in required_fields:
            self.assertTrue(
                hasattr(config, field),
                f"Required config field missing: {field}"
            )

        logger.info(f"All {len(required_fields)} required fields present")

    def test_03_validate_field_types(self):
        """Test that configuration fields have correct types"""
        logger.info("Testing configuration field types")

        type_checks = [
            ('ENABLE_SIMULATION_MODE', bool),
            ('ENABLE_AUTO_TRADING', bool),
            ('DB_PATH', str),
            ('SIMULATION_BALANCE', (int, float)),
            ('STOP_LOSS_RATIO', float),
            ('INITIAL_TAKE_PROFIT_RATIO', float)
        ]

        for field, expected_type in type_checks:
            value = getattr(config, field)
            self.assertIsInstance(
                value,
                expected_type,
                f"Field '{field}' has wrong type: "
                f"expected {expected_type}, got {type(value)}"
            )

        logger.info(f"All {len(type_checks)} type validations passed")

    def test_04_validate_value_ranges(self):
        """Test that configuration values are within valid ranges"""
        logger.info("Testing configuration value ranges")

        # Stop loss ratio should be between -1.0 and 0.0
        stop_loss = config.STOP_LOSS_RATIO
        self.assertGreaterEqual(stop_loss, -1.0, "Stop loss ratio too low")
        self.assertLessEqual(stop_loss, 0.0, "Stop loss ratio too high")

        # Take profit ratio should be positive
        take_profit = config.INITIAL_TAKE_PROFIT_RATIO
        self.assertGreater(take_profit, 0.0, "Take profit ratio must be positive")
        self.assertLess(take_profit, 1.0, "Take profit ratio too high")

        # Simulation balance should be positive
        balance = config.SIMULATION_BALANCE
        self.assertGreater(balance, 0.0, "Simulation balance must be positive")

        logger.info("All value range validations passed")

    def test_05_validate_with_rules(self):
        """Test configuration validation using validation rules"""
        logger.info("Testing configuration with validation rules")

        is_valid, errors = validate_config(config)

        if not is_valid:
            logger.error(f"Configuration validation failed: {errors}")

        self.assertTrue(is_valid, f"Configuration validation failed: {errors}")

        logger.info("Configuration validation passed")

    def test_06_override_configuration(self):
        """Test configuration override functionality"""
        logger.info("Testing configuration override")

        original_value = config.ENABLE_AUTO_TRADING

        # Override value
        config.ENABLE_AUTO_TRADING = False
        self.assertFalse(config.ENABLE_AUTO_TRADING)

        # Restore value
        config.ENABLE_AUTO_TRADING = original_value
        self.assertEqual(config.ENABLE_AUTO_TRADING, original_value)

        logger.info("Configuration override test passed")

    def test_07_test_mode_config(self):
        """Test that test mode configuration is correctly applied"""
        logger.info("Testing test mode configuration")

        # In test mode, simulation should be enabled
        self.assertTrue(
            config.ENABLE_SIMULATION_MODE,
            "Simulation mode should be enabled in tests"
        )

        # Auto trading should be disabled in tests
        self.assertFalse(
            config.ENABLE_AUTO_TRADING,
            "Auto trading should be disabled in tests"
        )

        logger.info("Test mode configuration verified")

    def test_08_qmt_path_exists(self):
        """Test that QMT path is configured (may not exist)"""
        logger.info("Testing QMT path configuration")

        self.assertIsNotNone(config.QMT_PATH, "QMT_PATH should be configured")
        self.assertIsInstance(config.QMT_PATH, str, "QMT_PATH should be a string")

        if os.path.exists(config.QMT_PATH):
            logger.info(f"QMT path exists: {config.QMT_PATH}")
        else:
            logger.warning(f"QMT path not found: {config.QMT_PATH}")

    def test_09_account_config_loading(self):
        """Test account configuration loading"""
        logger.info("Testing account configuration")

        account_config = config.ACCOUNT_CONFIG

        self.assertIsInstance(account_config, dict, "Account config should be a dict")

        # Check for expected fields
        expected_fields = ['account_id', 'account_type']
        for field in expected_fields:
            self.assertIn(
                field,
                account_config,
                f"Account config missing field: {field}"
            )

        logger.info("Account configuration validated")

    def test_10_dynamic_stop_profit_config(self):
        """Test dynamic stop profit configuration"""
        logger.info("Testing dynamic stop profit configuration")

        dynamic_config = config.DYNAMIC_TAKE_PROFIT

        self.assertIsInstance(dynamic_config, list, "DYNAMIC_TAKE_PROFIT should be a list")
        self.assertGreater(len(dynamic_config), 0, "DYNAMIC_TAKE_PROFIT should not be empty")

        # Validate structure: [(profit_ratio, stop_ratio), ...]
        for item in dynamic_config:
            self.assertIsInstance(item, tuple, "Each item should be a tuple")
            self.assertEqual(len(item), 2, "Each tuple should have 2 elements")

            profit_ratio, stop_ratio = item
            self.assertGreater(profit_ratio, 0, "Profit ratio should be positive")
            self.assertGreater(stop_ratio, 0, "Stop ratio should be positive")
            self.assertLess(stop_ratio, 1, "Stop ratio should be less than 1")

        logger.info(f"Dynamic stop profit config validated ({len(dynamic_config)} levels)")


def run_tests():
    """Run configuration management tests"""
    suite = unittest.TestLoader().loadTestsFromTestCase(TestConfigManagement)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
