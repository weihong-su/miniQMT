"""
运行日志增强的轻量回归测试。

覆盖进程生命周期身份串、心跳状态行与资源指标、活跃网格会话统计、
日志文件切换、重复日志节流、以及最高价可见变化判断。
"""
import logging
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
import utils
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
        self.assertIn("无人值守总开关:开启", status_line)
        self.assertIn("自动止盈:关闭", status_line)
        self.assertNotIn("自动网格", status_line)
        self.assertEqual("   自动网格:开启 | 活跃网格会话数:2", grid_line)

    def test_resource_line_reports_thread_count_and_memory(self):
        line = main._format_resource_line()

        self.assertIn(f"线程数:{threading.active_count()}", line)
        self.assertRegex(line, r"内存:RSS \d+MB / VMS \d+MB")

    def test_resource_line_reports_os_thread_count_and_handles(self):
        """OS 口径必须与 Python 口径同时出现：xtquant 的原生线程只在前者可见。"""
        line = main._format_resource_line()

        self.assertRegex(line, r"线程数:\d+\(OS \d+\) \| 句柄:\d+")

        stats = utils.process_resource_stats()
        self.assertIsNotNone(stats)
        # OS 线程必然不少于 Python 线程：每个 Thread 对象背后都有一条原生线程
        self.assertGreaterEqual(stats['os_threads'], threading.active_count())
        self.assertGreater(stats['handles'], 0)

    def test_resource_line_degrades_when_os_stats_unavailable(self):
        with patch("utils.process_resource_stats", return_value=None):
            line = main._format_resource_line()

        self.assertRegex(line, r"线程数:\d+ \|")
        self.assertNotIn("OS ", line)
        self.assertNotIn("句柄", line)

    def test_resource_line_degrades_when_memory_unavailable(self):
        with patch("utils.memory_usage", return_value=None):
            line = main._format_resource_line()

        self.assertIn("线程数:", line)
        self.assertIn("内存:获取失败", line)

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


class TestLogThrottle(unittest.TestCase):
    """重复日志节流：持续性状态刷屏会喂大终端缓冲，2026-09-09 曾引发进程级故障。"""

    def setUp(self):
        logger_module.reset_log_throttle()
        self.log = logger_module.get_logger("throttle_test")
        self.records = []
        self.log.addHandler(_ListHandler(self.records))
        self.log.propagate = False

    def tearDown(self):
        self.log.handlers = []
        self.log.propagate = True
        logger_module.reset_log_throttle()

    def test_repeated_state_logs_only_once_within_window(self):
        for _ in range(1500):
            logger_module.log_throttled(
                self.log, logging.WARNING, "stop_loss_detect:300879",
                "300879 触发固定止损", interval=300
            )

        self.assertEqual(len(self.records), 1)
        self.assertEqual(self.records[0].getMessage(), "300879 触发固定止损")

    def test_suppressed_count_is_reported_when_window_expires(self):
        key = "available_zero_block:300879"
        logger_module.log_throttled(self.log, logging.WARNING, key, "阻断", interval=0)
        for _ in range(9):
            logger_module.log_throttled(self.log, logging.WARNING, key, "阻断", interval=300)
        # interval=0 让窗口立即到期，下一次应输出并带上抑制计数
        logger_module.log_throttled(self.log, logging.WARNING, key, "阻断", interval=0)

        self.assertEqual(len(self.records), 2)
        self.assertIn("期间重复 9 次未打印", self.records[1].getMessage())

    def test_different_keys_are_independent(self):
        logger_module.log_throttled(self.log, logging.WARNING, "k:300879", "A", interval=300)
        logger_module.log_throttled(self.log, logging.WARNING, "k:000001", "B", interval=300)

        self.assertEqual(len(self.records), 2)

    def test_reset_restores_immediate_output(self):
        """状态翻转（价格回到止损位上方、委托成交）后必须能立刻再报，否则节流会掩盖真实变化。"""
        key = "stop_loss_detect:300879"
        logger_module.log_throttled(self.log, logging.WARNING, key, "触发", interval=300)
        logger_module.log_throttled(self.log, logging.WARNING, key, "触发", interval=300)
        self.assertEqual(len(self.records), 1)

        logger_module.reset_log_throttle(key)
        logger_module.log_throttled(self.log, logging.WARNING, key, "触发", interval=300)

        self.assertEqual(len(self.records), 2)
        self.assertNotIn("期间重复", self.records[1].getMessage())

    def test_level_is_honoured(self):
        logger_module.log_throttled(self.log, logging.ERROR, "lvl", "错误", interval=300)

        self.assertEqual(self.records[0].levelno, logging.ERROR)


class _ListHandler(logging.Handler):
    def __init__(self, sink):
        super().__init__()
        self.sink = sink

    def emit(self, record):
        self.sink.append(record)


if __name__ == "__main__":
    unittest.main()
