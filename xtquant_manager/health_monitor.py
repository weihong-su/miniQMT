"""
HealthMonitor — 后台健康检查线程

三级检查策略（参照 thread_monitor.py 的设计哲学）：
  Level 0 每 check_interval 秒：is_healthy()（内存，无 I/O）
    → 不健康 → Level 1
  Level 1：ping()（真实探测，3s 超时）
    → 失败 → Level 2
  Level 2：reconnect()（指数退避，在后台线程中 sleep）
    → 重连冷却防风暴（默认 60s）

与 thread_monitor.py 的一致性：
- 使用 threading.Event.wait() 替代 time.sleep（支持快速 stop()）
- 每账号维护独立的重连冷却时间
- 完整的重连历史计数
"""
import threading
import time
from typing import Dict, Optional, TYPE_CHECKING

from .exceptions import AccountNotFoundError

if TYPE_CHECKING:
    from .manager import XtQuantManager

try:
    from logger import get_logger
    logger = get_logger("xqm_health")
except Exception:
    import logging
    logger = logging.getLogger("xtquant_manager.health_monitor")


class HealthMonitor:
    """
    后台健康检查线程。

    Usage:
        monitor = HealthMonitor(manager, check_interval=30.0)
        monitor.start()
        # ... 运行中 ...
        monitor.stop()
    """

    def __init__(
        self,
        manager: "XtQuantManager",
        check_interval: float = 30.0,
        reconnect_cooldown: float = 60.0,
    ):
        """
        Args:
            manager: XtQuantManager 实例
            check_interval: 每次健康检查间隔（秒）
            reconnect_cooldown: 两次重连之间的最小冷却时间（秒）
        """
        self._manager = manager
        self._check_interval = check_interval
        self._reconnect_cooldown = reconnect_cooldown

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # 每账号的重连冷却追踪
        self._last_reconnect_time: Dict[str, float] = {}
        self._reconnect_counts: Dict[str, int] = {}

        # 统计
        self._check_count = 0
        self._total_reconnects = 0
        self._start_time: Optional[float] = None

    def start(self) -> None:
        """启动后台守护线程"""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("HealthMonitor 已在运行")
            return

        self._stop_event.clear()
        self._start_time = time.time()
        self._thread = threading.Thread(
            target=self._check_loop,
            name="XtQuantHealthMonitor",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            f"HealthMonitor 已启动 "
            f"(check_interval={self._check_interval}s, "
            f"reconnect_cooldown={self._reconnect_cooldown}s)"
        )

    def stop(self, timeout: float = 5.0) -> None:
        """
        停止后台线程。
        设置 stop_event 后等待线程退出（最多 timeout 秒）。
        """
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("HealthMonitor 线程未在超时内退出")
            else:
                logger.info("HealthMonitor 已停止")
        self._thread = None

    def is_running(self) -> bool:
        """返回线程是否在运行"""
        return self._thread is not None and self._thread.is_alive()

    def get_status(self) -> dict:
        """返回监控器状态快照"""
        return {
            "running": self.is_running(),
            "check_interval": self._check_interval,
            "reconnect_cooldown": self._reconnect_cooldown,
            "check_count": self._check_count,
            "total_reconnects": self._total_reconnects,
            "reconnect_counts": dict(self._reconnect_counts),
            "uptime_seconds": (
                round(time.time() - self._start_time, 1)
                if self._start_time else 0
            ),
        }

    # ------------------------------------------------------------------
    # 内部逻辑
    # ------------------------------------------------------------------

    def _check_loop(self) -> None:
        """主循环：使用 stop_event.wait() 代替 sleep，便于快速退出"""
        logger.debug("HealthMonitor 检查循环已开始")
        while not self._stop_event.wait(self._check_interval):
            self._run_one_check()
        logger.debug("HealthMonitor 检查循环已结束")

    def _run_one_check(self) -> None:
        """执行一轮健康检查"""
        self._check_count += 1
        account_ids = self._manager.list_accounts()

        for account_id in account_ids:
            if self._stop_event.is_set():
                break
            try:
                self._check_account(account_id)
            except Exception as e:
                logger.error(f"检查账号 {account_id[:4]}*** 时出错: {e}")

    def _check_account(self, account_id: str) -> None:
        """对单个账号执行三级健康检查"""
        try:
            account = self._manager.get_account(account_id)
        except AccountNotFoundError:
            return  # 账号已被注销，忽略

        # Level 0: 快速内存检查
        if account.is_healthy():
            return

        logger.info(f"[{account_id[:4]}***] Level 0 检查失败，进入 Level 1 ping 探测")

        # Level 1: 真实探测
        if account.ping():
            logger.info(f"[{account_id[:4]}***] ping 成功，恢复健康")
            return

        logger.warning(f"[{account_id[:4]}***] Level 1 ping 失败，进入 Level 2 重连")

        # Level 2: 指数退避重连（检查冷却时间）
        if not self._can_reconnect(account_id):
            logger.debug(
                f"[{account_id[:4]}***] 重连冷却中，跳过本次重连"
            )
            return

        # 记录重连时间（在实际 sleep 之前更新，防止短时间内重复触发）
        self._last_reconnect_time[account_id] = time.time()
        self._reconnect_counts[account_id] = (
            self._reconnect_counts.get(account_id, 0) + 1
        )
        self._total_reconnects += 1

        logger.warning(
            f"[{account_id[:4]}***] 开始重连 "
            f"(第 {self._reconnect_counts[account_id]} 次)"
        )

        # 在后台线程执行重连，避免 reconnect() 内的 time.sleep(wait) 阻塞监控主循环。
        # 多账号场景下，一个账号的重连等待不影响其他账号的健康检查。
        t = threading.Thread(
            target=account.reconnect,
            name=f"XtQuantReconnect-{account_id[:4]}",
            daemon=True,
        )
        t.start()
        logger.info(f"[{account_id[:4]}***] 已在后台线程发起重连")

    def _can_reconnect(self, account_id: str) -> bool:
        """检查是否超过冷却时间"""
        last_time = self._last_reconnect_time.get(account_id, 0)
        elapsed = time.time() - last_time
        return elapsed >= self._reconnect_cooldown
