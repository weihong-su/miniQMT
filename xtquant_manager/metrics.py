"""
调用指标统计

每个 XtQuantAccount 实例持有一个 MetricsCollector，
记录 API 调用次数、延迟分布、错误率等信息。

使用内存滑动窗口实现，不依赖外部系统。
"""
import threading
import time
from collections import defaultdict, deque
from typing import Dict, Optional


class MetricsCollector:
    """
    轻量级调用指标收集器。

    记录内容：
    - 按操作名分组的调用计数（total / success / error / timeout）
    - 延迟统计：滑动窗口 P50 / P95 / avg（最近 1000 次）
    - 最后一次错误时间和错误消息
    - 总体错误率（滑动窗口）
    """

    _WINDOW_SIZE = 1000  # 延迟滑动窗口大小

    def __init__(self):
        self._lock = threading.Lock()
        # 按操作名分组的计数器
        self._total: Dict[str, int] = defaultdict(int)
        self._success: Dict[str, int] = defaultdict(int)
        self._error: Dict[str, int] = defaultdict(int)
        self._timeout: Dict[str, int] = defaultdict(int)
        # 延迟滑动窗口（所有操作合并，毫秒）
        self._latencies: deque = deque(maxlen=self._WINDOW_SIZE)
        # 错误滑动窗口（True=错误，False=成功，用于计算错误率）
        self._error_window: deque = deque(maxlen=100)
        # 时间戳
        self._start_time: float = time.time()
        self._last_error_time: Optional[float] = None
        self._last_error_msg: str = ""

    def record_call(
        self,
        op: str,
        success: bool,
        latency_ms: float,
        is_timeout: bool = False,
        error_msg: str = "",
    ) -> None:
        """
        记录一次调用。

        Args:
            op: 操作名称，如 "order_stock", "query_positions"
            success: 是否成功
            latency_ms: 调用耗时（毫秒）
            is_timeout: 是否超时（超时也算 error）
            error_msg: 错误消息（失败时填写）
        """
        with self._lock:
            self._total[op] += 1
            self._latencies.append(latency_ms)
            self._error_window.append(not success)

            if success:
                self._success[op] += 1
            else:
                self._error[op] += 1
                self._last_error_time = time.time()
                self._last_error_msg = error_msg
                if is_timeout:
                    self._timeout[op] += 1

    def snapshot(self) -> dict:
        """
        返回当前指标快照（线程安全）。

        Returns:
            dict，包含所有统计数据
        """
        with self._lock:
            latencies = list(self._latencies)
            error_window = list(self._error_window)

        # 计算延迟分位数
        avg_ms, p50_ms, p95_ms = 0.0, 0.0, 0.0
        if latencies:
            sorted_lat = sorted(latencies)
            avg_ms = sum(sorted_lat) / len(sorted_lat)
            p50_ms = sorted_lat[int(len(sorted_lat) * 0.50)]
            p95_ms = sorted_lat[int(len(sorted_lat) * 0.95)]

        # 计算错误率（最近 100 次）
        error_rate = sum(error_window) / len(error_window) if error_window else 0.0

        # 按操作汇总
        ops = {}
        all_ops = set(
            list(self._total.keys())
            + list(self._success.keys())
            + list(self._error.keys())
        )
        for op in all_ops:
            ops[op] = {
                "total": self._total.get(op, 0),
                "success": self._success.get(op, 0),
                "error": self._error.get(op, 0),
                "timeout": self._timeout.get(op, 0),
            }

        total_calls = sum(self._total.values())
        total_errors = sum(self._error.values())

        return {
            "total_calls": total_calls,
            "success_calls": sum(self._success.values()),
            "error_calls": total_errors,
            "timeout_calls": sum(self._timeout.values()),
            "error_rate": round(error_rate, 4),
            "avg_latency_ms": round(avg_ms, 2),
            "p50_latency_ms": round(p50_ms, 2),
            "p95_latency_ms": round(p95_ms, 2),
            "last_error_time": self._last_error_time,
            "last_error_msg": self._last_error_msg,
            "uptime_seconds": round(time.time() - self._start_time, 1),
            "ops": ops,
        }

    def reset(self) -> None:
        """重置所有统计数据。"""
        with self._lock:
            self._total.clear()
            self._success.clear()
            self._error.clear()
            self._timeout.clear()
            self._latencies.clear()
            self._error_window.clear()
            self._start_time = time.time()
            self._last_error_time = None
            self._last_error_msg = ""
