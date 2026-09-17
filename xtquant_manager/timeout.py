"""
统一超时保护工具

所有对 xtquant API 的调用都应通过 call_with_timeout 包装，
避免 xtquant 阻塞调用挂住整个线程。

注意：xtquant 的阻塞调用无法被中断，超时后原始线程仍会继续运行
直到 API 返回，因此使用独立线程隔离。每次创建新的 executor 以
避免超时线程占用 worker 导致后续调用排队。
"""
import concurrent.futures
import functools
import time

from .exceptions import XtQuantCallError, XtQuantTimeoutError


def call_with_timeout(func, *args, timeout: float = 3.0, **kwargs):
    """
    在独立线程中执行 func，超过 timeout 秒则抛 XtQuantTimeoutError。

    Args:
        func: 要执行的可调用对象
        *args: 传递给 func 的位置参数
        timeout: 超时秒数，默认 3.0
        **kwargs: 传递给 func 的关键字参数

    Returns:
        func 的返回值

    Raises:
        XtQuantTimeoutError: 超时
        XtQuantCallError: 调用失败（非超时）
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(func, *args, **kwargs)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise XtQuantTimeoutError(
                f"调用超时 ({timeout}s): {getattr(func, '__name__', str(func))}"
            )
        except XtQuantManagerError:
            raise
        except Exception as e:
            raise XtQuantCallError(
                f"调用失败 [{getattr(func, '__name__', str(func))}]: {e}"
            ) from e


def with_timeout(timeout: float = 3.0):
    """
    装饰器版本，为方法添加超时保护。

    Usage:
        @with_timeout(3.0)
        def my_api_call(self):
            return xt.get_full_tick(...)
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return call_with_timeout(func, *args, timeout=timeout, **kwargs)
        return wrapper
    return decorator


# 导入时避免循环依赖
from .exceptions import XtQuantManagerError  # noqa: E402
