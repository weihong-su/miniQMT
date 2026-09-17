"""
HealthMonitor — 后台健康检查线程

三级检查策略（参照 thread_monitor.py 的设计哲学）：
  Level 0 每 check_interval 秒：is_healthy()（内存，无 I/O）
    → 健康但 ping 过期 → 例行 ping 探测（成功时不记为失败）
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

        # 已注册断连回调的账号集合（lazy-register，首次检查时注册，防止重复）
        self._disconnect_cb_registered: set = set()

        # 连续不健康计数（用于日志降级，避免永久失败账号刷屏）
        self._consecutive_unhealthy: Dict[str, int] = {}
        # 连续失败超过此阈值后，Level 0/1 失败日志降级为 DEBUG
        _LOG_THROTTLE = 3
        self._LOG_THROTTLE = _LOG_THROTTLE

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
                logger.error(f"检查账号 {self._mask_id(account_id)} 时出错: {e}")

    def _check_account(self, account_id: str) -> None:
        """对单个账号执行三级健康检查"""
        try:
            account = self._manager.get_account(account_id)
        except AccountNotFoundError:
            return  # 账号已被注销，忽略

        # ── 首次检查时，注册断连回调（lazy-register，确保只注册一次）──
        if account_id not in self._disconnect_cb_registered:
            account.register_disconnect_callback(
                lambda aid=account_id: self._on_disconnect(aid)
            )
            self._disconnect_cb_registered.add(account_id)

        # Level 0: 快速内存检查
        if account.is_healthy():
            if account.needs_ping():
                if account.ping():
                    logger.debug(f"[{self._mask_id(account_id)}] 例行 ping 探测成功")
                    self._consecutive_unhealthy[account_id] = 0
                    return
                logger.warning(
                    f"[{self._mask_id(account_id)}] 例行 ping 探测失败，进入 Level 2 重连"
                )
            else:
                self._mark_healthy_if_recovered(account_id)
                return
        else:
            consecutive = self._consecutive_unhealthy.get(account_id, 0) + 1
            self._consecutive_unhealthy[account_id] = consecutive

            # 连续失败超过阈值后，Level 0/1 日志降级为 DEBUG 以避免刷屏
            # 每隔 LOG_THROTTLE 次仍输出一次 INFO，告知用户账号持续异常
            verbose = consecutive <= self._LOG_THROTTLE
            periodic = (consecutive % 10 == 0)  # 每 10 次仍提示一次

            if verbose or periodic:
                log_fn = logger.info if verbose else logger.warning
                log_fn(
                    f"[{self._mask_id(account_id)}] Level 0 检查失败，进入 Level 1 ping 探测"
                    + (f" (连续第 {consecutive} 次)" if periodic else "")
                )
            else:
                logger.debug(
                    f"[{self._mask_id(account_id)}] Level 0 检查失败 (连续第 {consecutive} 次)"
                )

            # Level 1: 真实探测
            if account.ping():
                logger.info(f"[{self._mask_id(account_id)}] ping 成功，恢复健康")
                self._consecutive_unhealthy[account_id] = 0
                return

            if verbose or periodic:
                logger.warning(
                    f"[{self._mask_id(account_id)}] Level 1 ping 失败，进入 Level 2 重连"
                    + (f" (连续第 {consecutive} 次)" if periodic else "")
                )
            else:
                logger.debug(
                    f"[{self._mask_id(account_id)}] Level 1 ping 失败 (连续第 {consecutive} 次)"
                )

        # Level 2: 若账号内部已有重连流程在进行（等待退避或正在建连），跳过本次调度
        # account.is_reconnecting 为 True 时，新线程调用 reconnect() 会立即返回 False，
        # 只会产生误导性的"第 N 次"计数和无谓的线程创建开销。
        if account.is_reconnecting:
            logger.debug(
                f"[{self._mask_id(account_id)}] 已有重连线程在运行，跳过本次调度"
            )
            return

        # 检查冷却时间（防止断连回调短时间内重复触发）
        if not self._can_reconnect(account_id):
            logger.debug(
                f"[{self._mask_id(account_id)}] 重连冷却中，跳过本次重连"
            )
            return

        # 记录重连时间（在实际 sleep 之前更新，防止短时间内重复触发）
        self._last_reconnect_time[account_id] = time.time()
        self._reconnect_counts[account_id] = (
            self._reconnect_counts.get(account_id, 0) + 1
        )
        self._total_reconnects += 1

        logger.warning(
            f"[{self._mask_id(account_id)}] 开始重连 "
            f"(第 {self._reconnect_counts[account_id]} 次)"
        )

        # 在后台线程执行重连，避免 reconnect() 内的 Event.wait(wait) 阻塞监控主循环。
        # 多账号场景下，一个账号的重连等待不影响其他账号的健康检查。
        t = threading.Thread(
            target=account.reconnect,
            name=f"XtQuantReconnect-{self._mask_id(account_id)}",
            daemon=True,
        )
        t.start()
        logger.info(f"[{self._mask_id(account_id)}] 已在后台线程发起重连")

    def _mark_healthy_if_recovered(self, account_id: str) -> None:
        """账号恢复健康时，重置连续失败计数并记录恢复日志。"""
        prev_count = self._consecutive_unhealthy.get(account_id, 0)
        if prev_count > 0:
            logger.info(
                f"[{self._mask_id(account_id)}] 账号恢复健康 "
                f"(经过 {prev_count} 次连续检查失败)"
            )
            self._consecutive_unhealthy[account_id] = 0

    @staticmethod
    def _mask_id(account_id: str) -> str:
        """日志用账号 ID（部分脱敏，与 XtQuantAccount._id() 格式一致：前4位 + *** + 末1位）"""
        if len(account_id) > 4:
            return account_id[:4] + "***" + account_id[-1]
        return account_id

    def _can_reconnect(self, account_id: str) -> bool:
        """检查是否超过冷却时间"""
        last_time = self._last_reconnect_time.get(account_id, 0)
        elapsed = time.time() - last_time
        return elapsed >= self._reconnect_cooldown

    def _on_disconnect(self, account_id: str) -> None:
        """
        账号断连回调：立即重置冷却计时器。

        当 XtQuantAccount.on_disconnected() 触发时，此方法被调用。
        将 _last_reconnect_time 重置为 0，使下次 _check_account 不受冷却阻拦，
        可立即尝试重连（而非等待 reconnect_cooldown 秒）。

        参照 position_manager.py:319-328 的已验证实现。
        """
        logger.warning(
            f"[{self._mask_id(account_id)}] 收到断连通知，重置重连冷却计时器"
        )
        self._last_reconnect_time[account_id] = 0.0
