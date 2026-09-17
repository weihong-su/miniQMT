"""
Phase 1 测试：exceptions.py, timeout.py, metrics.py
"""
import time
import unittest

from xtquant_manager.exceptions import (
    AccountAlreadyExistsError,
    AccountNotFoundError,
    XtQuantCallError,
    XtQuantConnectionError,
    XtQuantManagerError,
    XtQuantTimeoutError,
)
from xtquant_manager.metrics import MetricsCollector
from xtquant_manager.timeout import call_with_timeout, with_timeout


# ---------------------------------------------------------------------------
# 异常类测试
# ---------------------------------------------------------------------------
class TestExceptions(unittest.TestCase):
    def test_hierarchy(self):
        """所有异常都继承自 XtQuantManagerError"""
        for exc_cls in [
            XtQuantTimeoutError,
            XtQuantCallError,
            XtQuantConnectionError,
            AccountNotFoundError,
            AccountAlreadyExistsError,
        ]:
            with self.subTest(exc_cls.__name__):
                self.assertTrue(issubclass(exc_cls, XtQuantManagerError))

    def test_can_catch_as_base(self):
        with self.assertRaises(XtQuantManagerError):
            raise XtQuantTimeoutError("超时")

    def test_message_preserved(self):
        err = XtQuantCallError("测试消息")
        self.assertIn("测试消息", str(err))


# ---------------------------------------------------------------------------
# 超时工具测试
# ---------------------------------------------------------------------------
class TestCallWithTimeout(unittest.TestCase):
    def test_normal_call(self):
        """正常调用返回正确结果"""
        result = call_with_timeout(lambda: 42, timeout=1.0)
        self.assertEqual(result, 42)

    def test_timeout_raises(self):
        """超时抛 XtQuantTimeoutError"""
        def slow():
            time.sleep(10)
            return "never"

        with self.assertRaises(XtQuantTimeoutError):
            call_with_timeout(slow, timeout=0.1)

    def test_exception_wrapped(self):
        """被调用函数抛异常时包装为 XtQuantCallError"""
        def bad():
            raise ValueError("原始错误")

        with self.assertRaises(XtQuantCallError) as ctx:
            call_with_timeout(bad, timeout=1.0)

        self.assertIn("原始错误", str(ctx.exception))

    def test_args_passed(self):
        """参数正确传递"""
        result = call_with_timeout(lambda a, b: a + b, 3, 4, timeout=1.0)
        self.assertEqual(result, 7)

    def test_kwargs_passed(self):
        def greet(name="world"):
            return f"hello {name}"

        result = call_with_timeout(greet, name="test", timeout=1.0)
        self.assertEqual(result, "hello test")

    def test_timeout_error_msg_contains_timeout_value(self):
        def slow():
            time.sleep(10)

        with self.assertRaises(XtQuantTimeoutError) as ctx:
            call_with_timeout(slow, timeout=0.15)
        self.assertIn("0.15", str(ctx.exception))


class TestWithTimeoutDecorator(unittest.TestCase):
    def test_decorator_normal(self):
        @with_timeout(1.0)
        def fast():
            return "ok"

        self.assertEqual(fast(), "ok")

    def test_decorator_timeout(self):
        @with_timeout(0.1)
        def slow():
            time.sleep(10)

        with self.assertRaises(XtQuantTimeoutError):
            slow()

    def test_decorator_preserves_name(self):
        @with_timeout(1.0)
        def my_func():
            pass

        self.assertEqual(my_func.__name__, "my_func")


# ---------------------------------------------------------------------------
# 指标收集器测试
# ---------------------------------------------------------------------------
class TestMetricsCollector(unittest.TestCase):
    def setUp(self):
        self.metrics = MetricsCollector()

    def test_initial_state(self):
        snap = self.metrics.snapshot()
        self.assertEqual(snap["total_calls"], 0)
        self.assertEqual(snap["error_calls"], 0)
        self.assertEqual(snap["error_rate"], 0.0)

    def test_record_success(self):
        self.metrics.record_call("order_stock", success=True, latency_ms=50.0)
        snap = self.metrics.snapshot()
        self.assertEqual(snap["total_calls"], 1)
        self.assertEqual(snap["success_calls"], 1)
        self.assertEqual(snap["error_calls"], 0)
        self.assertEqual(snap["ops"]["order_stock"]["total"], 1)
        self.assertEqual(snap["ops"]["order_stock"]["success"], 1)

    def test_record_failure(self):
        self.metrics.record_call(
            "query_positions", success=False, latency_ms=3000.0,
            error_msg="连接超时"
        )
        snap = self.metrics.snapshot()
        self.assertEqual(snap["error_calls"], 1)
        self.assertEqual(snap["last_error_msg"], "连接超时")
        self.assertIsNotNone(snap["last_error_time"])

    def test_record_timeout(self):
        self.metrics.record_call(
            "get_full_tick", success=False, latency_ms=3000.0,
            is_timeout=True, error_msg="超时"
        )
        snap = self.metrics.snapshot()
        self.assertEqual(snap["timeout_calls"], 1)
        self.assertEqual(snap["error_calls"], 1)

    def test_error_rate_calculation(self):
        # 10 次调用，3 次失败 → 错误率约 0.3
        for _ in range(7):
            self.metrics.record_call("op", success=True, latency_ms=10.0)
        for _ in range(3):
            self.metrics.record_call("op", success=False, latency_ms=10.0)

        snap = self.metrics.snapshot()
        self.assertAlmostEqual(snap["error_rate"], 0.3, places=2)

    def test_latency_stats(self):
        latencies = [10.0, 20.0, 30.0, 40.0, 50.0]
        for lat in latencies:
            self.metrics.record_call("op", success=True, latency_ms=lat)

        snap = self.metrics.snapshot()
        self.assertAlmostEqual(snap["avg_latency_ms"], 30.0, places=1)
        self.assertGreater(snap["p95_latency_ms"], 0)

    def test_multiple_ops(self):
        self.metrics.record_call("order_stock", success=True, latency_ms=50.0)
        self.metrics.record_call("query_positions", success=True, latency_ms=80.0)
        self.metrics.record_call("get_full_tick", success=False, latency_ms=3000.0)

        snap = self.metrics.snapshot()
        self.assertEqual(snap["total_calls"], 3)
        self.assertEqual(len(snap["ops"]), 3)

    def test_reset(self):
        self.metrics.record_call("op", success=True, latency_ms=10.0)
        self.metrics.reset()
        snap = self.metrics.snapshot()
        self.assertEqual(snap["total_calls"], 0)
        self.assertEqual(snap["error_calls"], 0)

    def test_thread_safety(self):
        """并发记录不崩溃"""
        import threading

        def worker():
            for _ in range(50):
                self.metrics.record_call("op", success=True, latency_ms=10.0)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        snap = self.metrics.snapshot()
        self.assertEqual(snap["total_calls"], 250)

    def test_uptime_positive(self):
        snap = self.metrics.snapshot()
        self.assertGreaterEqual(snap["uptime_seconds"], 0)

    def test_sliding_window_bounded(self):
        """超过窗口大小后，旧数据被丢弃"""
        # 写入超过 1000 条，全部成功
        for _ in range(1100):
            self.metrics.record_call("op", success=True, latency_ms=10.0)
        # 再写入 50 条失败
        for _ in range(50):
            self.metrics.record_call("op", success=False, latency_ms=10.0)

        snap = self.metrics.snapshot()
        # 错误率应基于最近 100 次窗口（50/100 = 0.5）
        self.assertGreater(snap["error_rate"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
