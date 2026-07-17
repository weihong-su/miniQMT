"""
运行日志增强的轻量回归测试。

覆盖进程生命周期身份串、心跳状态行、活跃网格会话统计、日志文件切换、
以及最高价可见变化判断。
"""
import os
import threading
import types
import unittest
from logging.handlers import RotatingFileHandler
from unittest.mock import patch

os.environ.setdefault("MINIQMT_LOG_FILE", "test/logs/test_runtime_logging.log")

import config
config.ENABLE_SIMULATION_MODE = True
config.DB_PATH = "data/trading_test.db"
config.LOG_FILE = "test/logs/test_runtime_logging.log"

import logger as logger_module
import main
from position_manager import _price_changed_at_display_precision


class TestRuntimeLogging(unittest.TestCase):
    def setUp(self):
        self._original_values = {
            "ENABLE_SIMULATION_MODE": config.ENABLE_SIMULATION_MODE,
            "ENABLE_AUTO_OPERATION": config.ENABLE_AUTO_OPERATION,
            "ENABLE_AUTO_TRADING": config.ENABLE_AUTO_TRADING,
            "ENABLE_GRID_TRADING": config.ENABLE_GRID_TRADING,
            "WEB_SERVER_PORT": config.WEB_SERVER_PORT,
        }

    def tearDown(self):
        for name, value in self._original_values.items():
            setattr(config, name, value)

    def test_heartbeat_status_lines_include_global_switch_and_active_grid_count(self):
        config.ENABLE_SIMULATION_MODE = False
        config.ENABLE_AUTO_OPERATION = True
        config.ENABLE_AUTO_TRADING = False
        config.ENABLE_GRID_TRADING = True

        status_line, grid_line = main._format_heartbeat_status_lines(2)

        self.assertIn("模式:实盘", status_line)
        self.assertIn("自动操作:开启", status_line)
        self.assertIn("自动交易:关闭", status_line)
        self.assertIn("网格交易:开启", status_line)
        self.assertNotIn("活跃网格会话数", status_line)
        self.assertEqual("   活跃网格会话数:2", grid_line)

    def test_active_grid_session_count_only_counts_enabled_active_sessions(self):
        grid_manager = types.SimpleNamespace(
            lock=threading.RLock(),
            sessions={
                "000001.SZ": types.SimpleNamespace(status="active", enabled=True),
                "000002.SZ": types.SimpleNamespace(status="active", enabled=False),
                "000003.SZ": types.SimpleNamespace(status="stopping", enabled=True),
            },
        )
        position_manager = types.SimpleNamespace(grid_manager=grid_manager)

        self.assertEqual(main._get_active_grid_session_count(position_manager), 1)

    def test_lifecycle_log_contains_runtime_identity(self):
        config.WEB_SERVER_PORT = 5007
        position_manager = types.SimpleNamespace(
            qmt_trader=types.SimpleNamespace(session_id=123456)
        )

        with patch.dict(os.environ, {"QMT_ACCOUNT_ID": "ACC_TEST"}), \
             patch.object(main.logger, "info") as mock_info:
            main._log_process_lifecycle("开始清理", position_manager)

        message = mock_info.call_args[0][0]
        self.assertIn("进程生命周期: 开始清理", message)
        self.assertIn("account_id=ACC_TEST", message)
        self.assertIn("port=5007", message)
        self.assertIn("session_id=123456", message)

    def test_set_log_file_replaces_main_file_handler(self):
        target = logger_module.set_log_file("test/logs/test_runtime_logging_route.log")
        file_handlers = [
            handler for handler in logger_module.logger.handlers
            if isinstance(handler, RotatingFileHandler)
        ]

        self.assertEqual(len(file_handlers), 1)
        self.assertEqual(
            os.path.abspath(file_handlers[0].baseFilename),
            os.path.abspath(target),
        )

    def test_price_changed_at_display_precision_suppresses_invisible_change(self):
        self.assertFalse(_price_changed_at_display_precision(8.7891, 8.7901))
        self.assertTrue(_price_changed_at_display_precision(8.79, 8.80))
        self.assertTrue(_price_changed_at_display_precision(None, 8.79))


if __name__ == "__main__":
    unittest.main()
