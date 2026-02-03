"""
Test Configuration - Override config values for safe testing

Provides test-specific configuration that overrides production settings
"""

import os
import sys

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Test configuration overrides
TEST_CONFIG = {
    # Core switches
    'ENABLE_SIMULATION_MODE': True,
    'ENABLE_AUTO_TRADING': False,
    'ENABLE_DYNAMIC_STOP_PROFIT': True,
    'ENABLE_GRID_TRADING': True,
    'ENABLE_THREAD_MONITOR': True,
    'DEBUG': True,

    # Database
    'DB_PATH': 'data/trading_test.db',

    # Logging
    'LOG_FILE': 'test/logs/test.log',
    'LOG_LEVEL': 'DEBUG',

    # Simulation
    'SIMULATION_BALANCE': 100000.0,

    # Thread intervals (faster for testing)
    'UPDATE_INTERVAL': 5,  # Data update: 5s instead of 60s
    'MONITOR_LOOP_INTERVAL': 1,  # Position monitor: 1s instead of 3s
    'THREAD_CHECK_INTERVAL': 5,  # Thread monitor: 5s instead of 60s
    'POSITION_SYNC_INTERVAL': 3,  # Sync: 3s instead of 15s

    # Timeouts
    'MONITOR_CALL_TIMEOUT': 2.0,

    # Testing flags
    'DEBUG_SIMU_STOCK_DATA': True,  # Bypass trading time check
}


def apply_test_config():
    """
    Apply test configuration to config module

    Returns:
        dict: Original config values (for restoration)
    """
    import config

    original_values = {}

    for key, value in TEST_CONFIG.items():
        if hasattr(config, key):
            # Save original value
            original_values[key] = getattr(config, key)

            # Apply test value
            setattr(config, key, value)

    return original_values


def restore_config(original_values):
    """
    Restore original configuration

    Args:
        original_values: Dict of original config values
    """
    import config

    for key, value in original_values.items():
        setattr(config, key, value)


# Validation rules for configuration
CONFIG_VALIDATION_RULES = {
    'ENABLE_SIMULATION_MODE': {
        'type': bool,
        'required': True
    },
    'ENABLE_AUTO_TRADING': {
        'type': bool,
        'required': True
    },
    'DB_PATH': {
        'type': str,
        'required': True
    },
    'SIMULATION_BALANCE': {
        'type': (int, float),
        'required': True,
        'min': 0
    },
    'STOP_LOSS_RATIO': {
        'type': float,
        'required': True,
        'min': -1.0,
        'max': 0.0
    },
    'INITIAL_TAKE_PROFIT_RATIO': {
        'type': float,
        'required': True,
        'min': 0.0,
        'max': 1.0
    }
}


def validate_config(config_module):
    """
    Validate configuration against rules

    Args:
        config_module: Configuration module to validate

    Returns:
        tuple: (is_valid, errors)
            is_valid: bool, True if all validation passed
            errors: list of error messages
    """
    errors = []

    for field, rules in CONFIG_VALIDATION_RULES.items():
        # Check existence
        if not hasattr(config_module, field):
            if rules.get('required', False):
                errors.append(f"Required field missing: {field}")
            continue

        value = getattr(config_module, field)

        # Check type
        expected_type = rules.get('type')
        if expected_type and not isinstance(value, expected_type):
            errors.append(
                f"Field '{field}' has wrong type: "
                f"expected {expected_type}, got {type(value)}"
            )

        # Check range
        if 'min' in rules and value < rules['min']:
            errors.append(
                f"Field '{field}' below minimum: "
                f"{value} < {rules['min']}"
            )

        if 'max' in rules and value > rules['max']:
            errors.append(
                f"Field '{field}' above maximum: "
                f"{value} > {rules['max']}"
            )

    is_valid = len(errors) == 0
    return is_valid, errors
