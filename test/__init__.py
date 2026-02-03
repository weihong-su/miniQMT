"""
miniQMT Regression Test Framework

A comprehensive test suite for pre-release validation following MECE principles.

Test Modules:
1. test_config_management.py - Configuration loading and validation
2. test_database_operations.py - Database CRUD operations
3. test_grid_trading.py - Grid trading functionality
4. test_qmt_connection.py - QMT connection diagnostics
5. test_sell_monitoring.py - Sell monitoring and alerts
6. test_stop_loss_profit.py - Stop loss and take profit logic
7. test_system_integration.py - End-to-end integration
8. test_thread_monitoring.py - Thread health monitoring
9. test_unattended_operation.py - Unattended operation features
10. test_web_data_refresh.py - Web SSE data push

Usage:
    python test/run_all_tests.py
    or
    quick_test.bat
"""

__version__ = "1.0.0"
__author__ = "miniQMT Team"
