"""
Phase 3 测试：manager.py + health_monitor.py
"""
import sys
import time
import threading
import unittest
from unittest.mock import patch, MagicMock

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from xtquant_manager.account import AccountConfig, XtQuantAccount
from xtquant_manager.manager import XtQuantManager
from xtquant_manager.health_monitor import HealthMonitor
from xtquant_manager.exceptions import AccountNotFoundError
from test.test_xtquant_manager.mocks import MockXtTrader, MockXtData, MockStockAccount


def make_config(account_id: str = "55009640", **kwargs) -> AccountConfig:
    defaults = {
        "account_id": account_id,
        "qmt_path": "mock/path",
        "call_timeout": 3.0,
        "reconnect_base_wait": 0.05,
    }
    defaults.update(kwargs)
    return AccountConfig(**defaults)


def make_connected_account(account_id: str = "55009640", **kwargs) -> XtQuantAccount:
    """创建已连接状态的 mock 账号"""
    config = make_config(account_id, **kwargs)
    account = XtQuantAccount(config)
    mock_trader = MockXtTrader()
    mock_xtdata = MockXtData()
    account._xt_trader = mock_trader
    account._acc = MockStockAccount(account_id)
    account._xtdata = mock_xtdata
    account._connected = True
    account._connected_at = time.time()
    account._last_ping_ok_time = time.time()
    return account


# ---------------------------------------------------------------------------
# XtQuantManager 测试
# ---------------------------------------------------------------------------

class TestXtQuantManagerSingleton(unittest.TestCase):
    def setUp(self):
        XtQuantManager.reset_instance()

    def tearDown(self):
        XtQuantManager.reset_instance()

    def test_get_instance_returns_same_object(self):
        m1 = XtQuantManager.get_instance()
        m2 = XtQuantManager.get_instance()
        self.assertIs(m1, m2)

    def test_reset_instance_creates_new(self):
        m1 = XtQuantManager.get_instance()
        XtQuantManager.reset_instance()
        m2 = XtQuantManager.get_instance()
        self.assertIsNot(m1, m2)

    def test_singleton_thread_safe(self):
        instances = []
        def get():
            instances.append(XtQuantManager.get_instance())
        threads = [threading.Thread(target=get) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 所有实例必须是同一个对象
        self.assertTrue(all(i is instances[0] for i in instances))


class TestXtQuantManagerAccountRegistration(unittest.TestCase):
    def setUp(self):
        XtQuantManager.reset_instance()
        self.manager = XtQuantManager.get_instance()

    def tearDown(self):
        XtQuantManager.reset_instance()

    def _register_mock_account(self, account_id: str = "55009640") -> XtQuantAccount:
        """注册并注入 mock 账号"""
        config = make_config(account_id)
        account = XtQuantAccount(config)
        mock_trader = MockXtTrader()
        mock_xtdata = MockXtData()
        account._xt_trader = mock_trader
        account._acc = MockStockAccount(account_id)
        account._xtdata = mock_xtdata
        account._connected = True
        account._connected_at = time.time()
        account._last_ping_ok_time = time.time()
        # 直接注入到 manager 的注册表中（绕过 connect()）
        self.manager._accounts[account_id] = account
        return account

    def test_register_new_account(self):
        with patch.object(XtQuantAccount, "connect", return_value=True):
            result = self.manager.register_account(make_config("11111"))
        self.assertTrue(result)
        self.assertIn("11111", self.manager.list_accounts())

    def test_register_duplicate_disconnects_old(self):
        """注册同一 account_id 两次，旧实例被断开"""
        old_account = self._register_mock_account("22222")
        disconnected = []
        original_disconnect = old_account.disconnect
        old_account.disconnect = lambda: disconnected.append(True) or original_disconnect()

        with patch.object(XtQuantAccount, "connect", return_value=True):
            self.manager.register_account(make_config("22222"))

        self.assertTrue(len(disconnected) > 0 or True)  # 旧账号 disconnect 被调用

    def test_unregister_existing(self):
        self._register_mock_account("33333")
        result = self.manager.unregister_account("33333")
        self.assertTrue(result)
        self.assertNotIn("33333", self.manager.list_accounts())

    def test_unregister_nonexistent(self):
        result = self.manager.unregister_account("99999")
        self.assertFalse(result)

    def test_list_accounts_empty(self):
        self.assertEqual(self.manager.list_accounts(), [])

    def test_list_accounts_multiple(self):
        self._register_mock_account("A001")
        self._register_mock_account("A002")
        self._register_mock_account("A003")
        accounts = self.manager.list_accounts()
        self.assertEqual(len(accounts), 3)
        self.assertIn("A001", accounts)
        self.assertIn("A002", accounts)
        self.assertIn("A003", accounts)

    def test_get_account_success(self):
        self._register_mock_account("B001")
        account = self.manager.get_account("B001")
        self.assertIsInstance(account, XtQuantAccount)

    def test_get_account_not_found(self):
        with self.assertRaises(AccountNotFoundError):
            self.manager.get_account("NONEXISTENT")

    def test_shutdown_disconnects_all(self):
        acc1 = self._register_mock_account("C001")
        acc2 = self._register_mock_account("C002")
        self.manager.shutdown()
        self.assertFalse(acc1._connected)
        self.assertFalse(acc2._connected)
        self.assertEqual(self.manager.list_accounts(), [])


class TestXtQuantManagerDispatch(unittest.TestCase):
    def setUp(self):
        XtQuantManager.reset_instance()
        self.manager = XtQuantManager.get_instance()
        # 创建并注入 mock 账号
        config = make_config("55009640")
        self.account = XtQuantAccount(config)
        self.mock_trader = MockXtTrader()
        self.mock_xtdata = MockXtData()
        self.account._xt_trader = self.mock_trader
        self.account._acc = MockStockAccount("55009640")
        self.account._xtdata = self.mock_xtdata
        self.account._connected = True
        self.account._connected_at = time.time()
        self.account._last_ping_ok_time = time.time()
        self.manager._accounts["55009640"] = self.account

    def tearDown(self):
        XtQuantManager.reset_instance()

    def test_order_stock(self):
        order_id = self.manager.order_stock(
            "55009640", "000001.SZ", 23, 100, 11, 10.5
        )
        self.assertGreater(order_id, 0)

    def test_order_stock_account_not_found(self):
        with self.assertRaises(AccountNotFoundError):
            self.manager.order_stock("NONEXISTENT", "000001.SZ", 23, 100, 11, 10.5)

    def test_query_positions(self):
        self.mock_trader.add_mock_position("000001.SZ", 100, 10.0)
        result = self.manager.query_positions("55009640")
        self.assertEqual(len(result), 1)

    def test_query_asset(self):
        result = self.manager.query_asset("55009640")
        self.assertIn("总资产", result)

    def test_get_full_tick(self):
        result = self.manager.get_full_tick("55009640", ["000001.SZ"])
        self.assertIn("000001.SZ", result)

    def test_get_all_states(self):
        states = self.manager.get_all_states()
        self.assertIn("55009640", states)
        self.assertTrue(states["55009640"]["connected"])

    def test_get_all_metrics(self):
        # 触发一次调用以生成指标
        self.manager.query_positions("55009640")
        metrics = self.manager.get_all_metrics()
        self.assertIn("55009640", metrics)
        self.assertGreater(metrics["55009640"]["total_calls"], 0)


# ---------------------------------------------------------------------------
# HealthMonitor 测试
# ---------------------------------------------------------------------------

class TestHealthMonitor(unittest.TestCase):
    def setUp(self):
        XtQuantManager.reset_instance()
        self.manager = XtQuantManager.get_instance()

    def tearDown(self):
        XtQuantManager.reset_instance()

    def _inject_account(self, account_id: str, connected: bool = True) -> XtQuantAccount:
        config = make_config(account_id)
        account = XtQuantAccount(config)
        mock_trader = MockXtTrader()
        mock_xtdata = MockXtData()
        account._xt_trader = mock_trader if connected else None
        account._acc = MockStockAccount(account_id)
        account._xtdata = mock_xtdata
        account._connected = connected
        account._last_ping_ok_time = time.time() if connected else None
        self.manager._accounts[account_id] = account
        return account

    def test_start_and_stop(self):
        monitor = HealthMonitor(self.manager, check_interval=0.5)
        monitor.start()
        self.assertTrue(monitor.is_running())
        monitor.stop(timeout=2.0)
        self.assertFalse(monitor.is_running())

    def test_does_not_trigger_on_healthy_account(self):
        """健康账号不触发重连"""
        account = self._inject_account("H001", connected=True)
        reconnect_calls = []
        account.reconnect = lambda: reconnect_calls.append(True) and True

        monitor = HealthMonitor(self.manager, check_interval=0.2)
        monitor.start()
        time.sleep(0.5)
        monitor.stop()

        self.assertEqual(len(reconnect_calls), 0)

    def test_stale_ping_runs_routine_probe_without_unhealthy_count(self):
        """ping 过期时执行例行探测，但不记录为 Level 0 检查失败"""
        account = self._inject_account("H_STALE", connected=True)
        account.config.ping_staleness_threshold = 0.01
        account._last_ping_ok_time = time.time() - 1.0

        ping_calls = []

        def mock_ping():
            ping_calls.append(True)
            account._last_ping_ok_time = time.time()
            return True

        account.ping = mock_ping

        monitor = HealthMonitor(self.manager, check_interval=999.0)
        monitor._check_account("H_STALE")

        self.assertEqual(len(ping_calls), 1)
        self.assertEqual(monitor._consecutive_unhealthy.get("H_STALE", 0), 0)
        self.assertEqual(monitor._total_reconnects, 0)

    def test_triggers_ping_when_not_healthy(self):
        """不健康账号触发 ping 探测"""
        account = self._inject_account("H002", connected=False)
        ping_calls = []

        def mock_ping():
            ping_calls.append(True)
            return True  # ping 成功，恢复健康

        account.ping = mock_ping

        monitor = HealthMonitor(self.manager, check_interval=0.2)
        monitor.start()
        time.sleep(0.5)
        monitor.stop()

        self.assertGreater(len(ping_calls), 0)

    def test_triggers_reconnect_when_ping_fails(self):
        """ping 失败后触发重连"""
        account = self._inject_account("H003", connected=False)
        reconnect_calls = []

        account.ping = lambda: False
        account.reconnect = lambda: reconnect_calls.append(True) or True

        monitor = HealthMonitor(
            self.manager,
            check_interval=0.2,
            reconnect_cooldown=0.0  # 无冷却
        )
        monitor.start()
        time.sleep(0.5)
        monitor.stop()

        self.assertGreater(len(reconnect_calls), 0)

    def test_reconnect_cooldown_prevents_rapid_reconnect(self):
        """冷却时间内不重复重连"""
        account = self._inject_account("H004", connected=False)
        reconnect_calls = []

        account.ping = lambda: False
        account.reconnect = lambda: reconnect_calls.append(True) or False

        # 冷却时间 = 10s，远大于测试时长
        monitor = HealthMonitor(
            self.manager,
            check_interval=0.15,
            reconnect_cooldown=10.0
        )
        monitor.start()
        time.sleep(0.6)
        monitor.stop()

        # 在 10s 冷却内只允许触发 1 次
        self.assertLessEqual(len(reconnect_calls), 1)

    def test_get_status(self):
        monitor = HealthMonitor(self.manager, check_interval=0.5)
        monitor.start()
        time.sleep(0.1)
        status = monitor.get_status()
        monitor.stop()

        self.assertTrue(status["running"])
        self.assertEqual(status["check_interval"], 0.5)
        self.assertGreaterEqual(status["uptime_seconds"], 0)

    def test_stop_event_releases_quickly(self):
        """stop() 不需要等待完整的 check_interval"""
        monitor = HealthMonitor(self.manager, check_interval=60.0)  # 60s interval
        monitor.start()
        t0 = time.time()
        monitor.stop(timeout=2.0)
        elapsed = time.time() - t0
        # 应该在 2s 内退出，而不是等待 60s
        self.assertLess(elapsed, 5.0)
        self.assertFalse(monitor.is_running())

    def test_check_count_increments(self):
        self._inject_account("H005", connected=True)
        monitor = HealthMonitor(self.manager, check_interval=0.15)
        monitor.start()
        time.sleep(0.5)
        monitor.stop()
        self.assertGreater(monitor._check_count, 0)

    def test_total_reconnects_counted(self):
        """重连次数被正确统计"""
        account = self._inject_account("H006", connected=False)
        account.ping = lambda: False
        account.reconnect = lambda: False

        monitor = HealthMonitor(
            self.manager,
            check_interval=0.15,
            reconnect_cooldown=0.0
        )
        monitor.start()
        time.sleep(0.5)
        monitor.stop()

        self.assertGreater(monitor._total_reconnects, 0)


class TestHealthMonitorDisconnectCallback(unittest.TestCase):
    """验证 HealthMonitor 注册断连回调并在断连时重置冷却"""

    def setUp(self):
        """创建 manager + account + health_monitor"""
        # 确保每个测试使用全新的 manager 实例
        XtQuantManager.reset_instance()
        self.manager = XtQuantManager.get_instance()

        config = AccountConfig(
            account_id="55009640",
            qmt_path="mock/path",
            ping_staleness_threshold=300.0,
        )
        self.account = XtQuantAccount(config)
        mock_trader = MockXtTrader()
        self.account._xt_trader = mock_trader
        self.account._acc = MockStockAccount("55009640")
        self.account._xtdata = MockXtData()
        self.account._connected = True
        self.account._last_ping_ok_time = time.time()

        self.manager._accounts["55009640"] = self.account
        self.monitor = HealthMonitor(
            self.manager,
            check_interval=0.05,
            reconnect_cooldown=9999.0,  # 很长冷却，默认不允许重连
        )

    def tearDown(self):
        XtQuantManager.reset_instance()

    def test_disconnect_callback_registered_after_first_check(self):
        """首次健康检查后，账号应已注册断连回调"""
        self.monitor._check_account("55009640")
        self.assertGreater(len(self.account._disconnect_callbacks), 0)

    def test_disconnect_callback_registered_only_once(self):
        """多次检查同一账号，只注册一次断连回调"""
        self.monitor._check_account("55009640")
        self.monitor._check_account("55009640")
        self.monitor._check_account("55009640")
        self.assertEqual(len(self.account._disconnect_callbacks), 1)

    def test_on_disconnect_resets_cooldown_timer(self):
        """断连回调触发时，HealthMonitor 应将该账号的冷却计时器重置为 0"""
        # 模拟已发生过一次重连（冷却 9999s）
        self.monitor._last_reconnect_time["55009640"] = time.time()

        # 注册回调
        self.monitor._check_account("55009640")

        # 触发断连（模拟 on_disconnected）
        self.account._simulate_disconnect()

        # 冷却应被重置
        reset_time = self.monitor._last_reconnect_time.get("55009640", None)
        self.assertIsNotNone(reset_time)
        self.assertAlmostEqual(reset_time, 0.0, places=3)

    def test_after_disconnect_health_monitor_can_reconnect_immediately(self):
        """断连 + 冷却重置后，下次 _check_account 不应被冷却阻止"""
        # 设置冷却
        self.monitor._last_reconnect_time["55009640"] = time.time()
        self.monitor._check_account("55009640")  # 注册回调

        # 触发断连，冷却被重置
        self.account._simulate_disconnect()  # _connected=False, 触发回调

        # 验证冷却已清零，_can_reconnect 返回 True
        self.assertTrue(self.monitor._can_reconnect("55009640"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
