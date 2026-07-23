"""超时执行辅助工具。"""
import concurrent.futures


def run_with_timeout(func, timeout):
    """在线程中执行阻塞调用，超时后不等待底层调用返回。"""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(func)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise
    finally:
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            # Python 3.8 不支持 cancel_futures 参数
            executor.shutdown(wait=False)
