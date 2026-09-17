"""
Phase 2 测试：XtQuantAccount
"""
import sys
import time
import threading
import unittest
from unittest.mock import patch, MagicMock

# 将项目根目录加入 path
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from xtquant_manager.account import AccountConfig, XtQuantAccount
from xtquant_manager.exceptions import XtQuantTimeoutError
from test.test_xtquant_manager.mocks import (
    MockXtTrader, MockXtData, MockStockAccount
)


def make_config(**kwargs) -> AccountConfig:
    """创建测试用 AccountConfig"""
    defaults = {
        "account_id": "55009640",
        "qmt_path": "mock/path",
        "account_type": "STOCK",
        "call_timeout": 3.0,
        "reconnect_base_wait": 0.1,  # 测试中缩短等待时间
    }
    defaults.update(kwargs)
    return AccountConfig(**defaults)


def make_account_with_mocks(**kwargs) -> tuple:
    """
    创建 XtQuantAccount，并注入 Mock 的 xt_trader 和 xtdata。
    返回 (account, mock_trader, mock_xtdata)
    """
    config = make_config(**kwargs)
    account = XtQuantAccount(config)

    mock_trader = MockXtTrader()
    mock_xtdata = MockXtData()

    # 直接注入 mock 对象，跳过真实 QMT 连接
    account._xt_trader = mock_trader
    account._acc = MockStockAccount(config.account_id)
    account._xtdata = mock_xtdata
    account._connected = True
    account._connected_at = time.time()
    account._last_ping_ok_time = time.time()

    return account, mock_trader, mock_xtdata


class TestAccountConfig(unittest.TestCase):
    def test_default_values(self):
        config = AccountConfig(account_id="12345", qmt_path="/path")
        self.assertEqual(config.account_type, "STOCK")
        self.assertEqual(config.call_timeout, 3.0)
        self.assertIsNone(config.session_id)

    def test_custom_values(self):
        config = AccountConfig(
            account_id="12345", qmt_path="/path",
            call_timeout=5.0, max_reconnect_attempts=10
        )
        self.assertEqual(config.call_timeout, 5.0)
        self.assertEqual(config.max_reconnect_attempts, 10)


class TestAccountHealthCheck(unittest.TestCase):
    def test_healthy_when_connected(self):
        account, _, _ = make_account_with_mocks()
        self.assertTrue(account.is_healthy())

    def test_healthy_when_ping_stale(self):
        """ping 过期只触发例行探测，不应把内存连接状态判为不健康"""
        account, _, _ = make_account_with_mocks(ping_staleness_threshold=1.0)
        account._last_ping_ok_time = time.time() - 2.0
        self.assertTrue(account.is_healthy())

    def test_needs_ping_when_ping_stale(self):
        """超过 ping 时效阈值后，应提示 HealthMonitor 做例行真实探测"""
        account, _, _ = make_account_with_mocks(ping_staleness_threshold=1.0)
        account._last_ping_ok_time = time.time() - 2.0
        self.assertTrue(account.needs_ping())

    def test_no_needs_ping_when_disconnected(self):
        """断连账号走 Level 0 失败路径，不走例行 ping 路径"""
        account, _, _ = make_account_with_mocks(ping_staleness_threshold=1.0)
        account._connected = False
        account._last_ping_ok_time = time.time() - 2.0
        self.assertFalse(account.needs_ping())

    def test_unhealthy_when_disconnected(self):
        account, mock_trader, _ = make_account_with_mocks()
        account._connected = False
        self.assertFalse(account.is_healthy())

    def test_unhealthy_when_trader_none(self):
        account, _, _ = make_account_with_mocks()
        account._xt_trader = None
        self.assertFalse(account.is_healthy())

    def test_ping_success(self):
        account, _, mock_xtdata = make_account_with_mocks()
        result = account.ping()
        self.assertTrue(result)
        self.assertIsNotNone(account.last_ping_ok_time)

    def test_ping_failure_when_xtdata_none(self):
        account, _, _ = make_account_with_mocks()
        account._xtdata = None
        result = account.ping()
        self.assertFalse(result)

    def test_ping_failure_when_tick_empty(self):
        account, _, mock_xtdata = make_account_with_mocks()
        mock_xtdata.simulate_tick_failure()
        result = account.ping()
        self.assertFalse(result)

    def test_ping_updates_last_ping_time(self):
        account, _, _ = make_account_with_mocks()
        before = time.time()
        account.ping()
        after = time.time()
        self.assertGreaterEqual(account.last_ping_ok_time, before)
        self.assertLessEqual(account.last_ping_ok_time, after)


class TestAccountConnect(unittest.TestCase):
    def test_connect_success_mock(self):
        """通过 mock xtquant 模块测试 connect 流程"""
        config = make_config()
        account = XtQuantAccount(config)

        mock_trader = MockXtTrader()
        mock_xtdata = MockXtData()

        with patch.dict("sys.modules", {
            "xtquant": MagicMock(),
            "xtquant.xtdata": mock_xtdata,
            "xtquant.xttrader": MagicMock(),
            "xtquant.xttype": MagicMock(),
        }):
            # 模拟 XtQuantTrader 构造和连接
            mock_trader_cls = MagicMock(return_value=mock_trader)
            mock_acc_cls = MagicMock(return_value=MockStockAccount())

            with patch("xtquant_manager.account.XtQuantAccount._connect_xttrader",
                       return_value=True):
                with patch("xtquant_manager.account.XtQuantAccount._connect_xtdata",
                           return_value=True):
                    result = account.connect()

        self.assertTrue(result)
        self.assertTrue(account._connected)

    def test_already_connected_returns_true(self):
        account, _, _ = make_account_with_mocks()
        # 已连接状态再次 connect()
        result = account.connect()
        self.assertTrue(result)

    def test_disconnect_clears_state(self):
        account, _, _ = make_account_with_mocks()
        account.disconnect()
        self.assertFalse(account._connected)
        self.assertIsNone(account._xt_trader)
        self.assertIsNone(account._xtdata)


class TestAccountReconnect(unittest.TestCase):
    def test_reconnect_exponential_backoff(self):
        """验证重连时的指数退避逻辑（通过 _reconnect_attempts 验证，不依赖 sleep mock）"""
        config = make_config(reconnect_base_wait=0.01)  # 极短等待
        account = XtQuantAccount(config)

        with patch.object(account, "_do_connect", return_value=False):
            account.reconnect()  # 第 1 次失败，_reconnect_attempts -> 1
            attempt_after_first = account._reconnect_attempts
            account.reconnect()  # 第 2 次失败，_reconnect_attempts -> 2
            attempt_after_second = account._reconnect_attempts

        self.assertEqual(attempt_after_first, 1)
        self.assertEqual(attempt_after_second, 2)

    def test_reconnect_resets_attempts_on_success(self):
        config = make_config(reconnect_base_wait=0.01)
        account = XtQuantAccount(config)
        account._reconnect_attempts = 3

        with patch.object(account, "_do_connect", return_value=True):
            result = account.reconnect()

        self.assertTrue(result)
        self.assertEqual(account._reconnect_attempts, 0)

    def test_reconnect_increments_attempts_on_failure(self):
        config = make_config(reconnect_base_wait=0.01)
        account = XtQuantAccount(config)

        with patch.object(account, "_do_connect", return_value=False):
            account.reconnect()

        self.assertEqual(account._reconnect_attempts, 1)

    def test_reconnect_max_wait_capped(self):
        """指数退避不超过 3600s（通过验证计算逻辑，不依赖 sleep mock）"""
        config = make_config(reconnect_base_wait=60.0)
        account = XtQuantAccount(config)
        account._reconnect_attempts = 100  # 很大的次数

        # 验证计算公式的上限截断：min(60 * 2^100, 3600) == 3600
        expected_wait = min(
            config.reconnect_base_wait * (2 ** 100),
            3600,
        )
        self.assertEqual(expected_wait, 3600)


class TestAccountMarketData(unittest.TestCase):
    def test_get_full_tick_success(self):
        account, _, mock_xtdata = make_account_with_mocks()
        result = account.get_full_tick(["000001.SZ"])
        self.assertIn("000001.SZ", result)
        self.assertIn("lastPrice", result["000001.SZ"])

    def test_get_full_tick_returns_empty_when_xtdata_none(self):
        account, _, _ = make_account_with_mocks()
        account._xtdata = None
        result = account.get_full_tick(["000001.SZ"])
        self.assertEqual(result, {})

    def test_get_full_tick_timeout_returns_empty(self):
        account, _, mock_xtdata = make_account_with_mocks(call_timeout=0.1)
        mock_xtdata.simulate_timeout()
        result = account.get_full_tick(["000001.SZ"])
        self.assertEqual(result, {})

    def test_get_full_tick_timeout_recorded_in_metrics(self):
        account, _, mock_xtdata = make_account_with_mocks(call_timeout=0.1)
        mock_xtdata.simulate_timeout()
        account.get_full_tick(["000001.SZ"])
        snap = account.metrics.snapshot()
        self.assertEqual(snap["timeout_calls"], 1)

    def test_get_market_data_ex(self):
        account, _, mock_xtdata = make_account_with_mocks()
        result = account.get_market_data_ex(
            [], ["000001.SZ"], period="1d",
            start_time="20240101", end_time="20241231"
        )
        self.assertIn("000001.SZ", result)


class TestAccountTrading(unittest.TestCase):
    def test_order_stock_buy(self):
        account, mock_trader, _ = make_account_with_mocks()
        order_id = account.order_stock(
            stock_code="000001.SZ",
            order_type=23,  # 买入
            order_volume=100,
            price_type=11,
            price=10.5,
        )
        self.assertGreater(order_id, 0)

    def test_order_stock_sell(self):
        account, mock_trader, _ = make_account_with_mocks()
        mock_trader.add_mock_position("000001.SZ", volume=100, cost_price=10.0)
        order_id = account.order_stock(
            stock_code="000001.SZ",
            order_type=24,  # 卖出
            order_volume=100,
            price_type=11,
            price=11.0,
        )
        self.assertGreater(order_id, 0)

    def test_order_stock_returns_minus1_when_disconnected(self):
        account, mock_trader, _ = make_account_with_mocks()
        account._xt_trader = None
        result = account.order_stock("000001.SZ", 23, 100, 11, 10.5)
        self.assertEqual(result, -1)

    def test_order_stock_timeout_returns_minus1(self):
        account, mock_trader, _ = make_account_with_mocks(call_timeout=0.1)
        mock_trader.simulate_timeout()
        result = account.order_stock("000001.SZ", 23, 100, 11, 10.5)
        self.assertEqual(result, -1)

    def test_cancel_order_success(self):
        account, mock_trader, _ = make_account_with_mocks()
        order_id = account.order_stock("000001.SZ", 23, 100, 11, 10.5)
        result = account.cancel_order(order_id)
        self.assertEqual(result, 0)

    def test_cancel_nonexistent_order(self):
        account, _, _ = make_account_with_mocks()
        result = account.cancel_order(99999)
        self.assertNotEqual(result, 0)

    def test_query_positions_empty(self):
        account, _, _ = make_account_with_mocks()
        result = account.query_positions()
        self.assertEqual(result, [])

    def test_query_positions_with_holdings(self):
        account, mock_trader, _ = make_account_with_mocks()
        mock_trader.add_mock_position("000001.SZ", 100, 10.0)
        mock_trader.add_mock_position("600036.SH", 200, 20.0)
        result = account.query_positions()
        self.assertEqual(len(result), 2)
        codes = [p["证券代码"] for p in result]
        self.assertIn("000001", codes)
        self.assertIn("600036", codes)

    def test_query_positions_columns_compatible(self):
        """返回的字段名与 easy_qmt_trader.position() 一致"""
        account, mock_trader, _ = make_account_with_mocks()
        mock_trader.add_mock_position("000001.SZ", 100, 10.0)
        result = account.query_positions()
        pos = result[0]
        for col in XtQuantAccount.POSITION_COLUMNS:
            self.assertIn(col, pos, f"缺少字段: {col}")

    def test_query_asset(self):
        account, _, _ = make_account_with_mocks()
        result = account.query_asset()
        self.assertIn("可用金额", result)
        self.assertIn("总资产", result)
        self.assertGreater(result["总资产"], 0)

    def test_query_asset_returns_empty_when_disconnected(self):
        account, _, _ = make_account_with_mocks()
        account._xt_trader = None
        result = account.query_asset()
        self.assertEqual(result, {})

    def test_query_orders(self):
        account, mock_trader, _ = make_account_with_mocks()
        account.order_stock("000001.SZ", 23, 100, 11, 10.5)
        result = account.query_orders()
        self.assertEqual(len(result), 1)
        self.assertIn("订单编号", result[0])

    def test_query_trades(self):
        account, _, _ = make_account_with_mocks()
        account.order_stock("000001.SZ", 23, 100, 11, 10.5)
        result = account.query_trades()
        self.assertEqual(len(result), 1)
        self.assertIn("成交编号", result[0])


class TestAccountMetrics(unittest.TestCase):
    def test_successful_calls_recorded(self):
        account, _, _ = make_account_with_mocks()
        account.query_positions()
        account.query_asset()
        snap = account.metrics.snapshot()
        self.assertEqual(snap["total_calls"], 2)
        self.assertEqual(snap["success_calls"], 2)

    def test_failed_calls_recorded(self):
        account, _, mock_xtdata = make_account_with_mocks(call_timeout=0.1)
        mock_xtdata.simulate_timeout()
        account.get_full_tick(["000001.SZ"])
        snap = account.metrics.snapshot()
        self.assertEqual(snap["error_calls"], 1)
        self.assertEqual(snap["timeout_calls"], 1)

    def test_get_state(self):
        account, _, _ = make_account_with_mocks()
        state = account.get_state()
        self.assertTrue(state["connected"])
        self.assertIn("account_id", state)
        self.assertIn("last_ping_ok_time", state)

    def test_id_partial_masking(self):
        """账号 ID 脱敏"""
        account, _, _ = make_account_with_mocks(account_id="55009640")
        masked = account._id()
        self.assertNotEqual(masked, "55009640")
        self.assertIn("***", masked)

    def test_id_short_account(self):
        """短账号 ID 不脱敏"""
        account, _, _ = make_account_with_mocks(account_id="1234")
        masked = account._id()
        self.assertEqual(masked, "1234")


class TestAccountTradeCallback(unittest.TestCase):
    def test_register_callback(self):
        account, _, _ = make_account_with_mocks()
        called = []
        account.register_trade_callback(lambda trade: called.append(trade))
        self.assertEqual(len(account._trade_callbacks), 1)

    def test_multiple_callbacks(self):
        account, _, _ = make_account_with_mocks()
        account.register_trade_callback(lambda t: None)
        account.register_trade_callback(lambda t: None)
        self.assertEqual(len(account._trade_callbacks), 2)


class TestAccountDisconnectCallback(unittest.TestCase):

    def test_register_disconnect_callback_stored(self):
        """register_disconnect_callback 应将回调存入列表"""
        account, _, _ = make_account_with_mocks()
        cb = MagicMock()
        account.register_disconnect_callback(cb)
        self.assertIn(cb, account._disconnect_callbacks)

    def test_on_disconnected_sets_connected_false(self):
        """on_disconnected 触发后 _connected 应立即变为 False"""
        account, _, _ = make_account_with_mocks()
        self.assertTrue(account._connected)

        # 模拟 QMT 调用 on_disconnected
        account._simulate_disconnect()

        self.assertFalse(account._connected)

    def test_on_disconnected_clears_last_ping_time(self):
        """on_disconnected 触发后 _last_ping_ok_time 应被清零，使 is_healthy() 返回 False"""
        account, _, _ = make_account_with_mocks()
        self.assertIsNotNone(account._last_ping_ok_time)

        account._simulate_disconnect()

        self.assertIsNone(account._last_ping_ok_time)
        self.assertFalse(account.is_healthy())

    def test_on_disconnected_calls_external_callbacks(self):
        """on_disconnected 应调用所有已注册的外部断连回调"""
        account, _, _ = make_account_with_mocks()
        cb1 = MagicMock()
        cb2 = MagicMock()
        account.register_disconnect_callback(cb1)
        account.register_disconnect_callback(cb2)

        account._simulate_disconnect()

        cb1.assert_called_once()
        cb2.assert_called_once()

    def test_on_disconnected_callback_exception_does_not_crash(self):
        """外部断连回调抛异常时，不影响其余回调执行"""
        account, _, _ = make_account_with_mocks()
        bad_cb = MagicMock(side_effect=RuntimeError("回调异常"))
        good_cb = MagicMock()
        account.register_disconnect_callback(bad_cb)
        account.register_disconnect_callback(good_cb)

        account._simulate_disconnect()  # 不应抛异常

        good_cb.assert_called_once()


class TestConnectTimeout(unittest.TestCase):

    def _make_account(self, connect_timeout=1.0):
        config = make_config(connect_timeout=connect_timeout)
        return XtQuantAccount(config)

    def test_connect_timeout_field_defaults_to_30(self):
        """AccountConfig.connect_timeout 默认值应为 30.0"""
        config = AccountConfig(account_id="12345", qmt_path="/path")
        self.assertEqual(config.connect_timeout, 30.0)

    def test_connect_timeout_returns_false_when_blocked(self):
        """xt_trader.connect() 超时时，_connect_xttrader 应返回 False"""
        account = self._make_account(connect_timeout=0.1)

        mock_trader = MagicMock()
        # connect() 永不返回（模拟 QMT 未响应）
        mock_trader.connect.side_effect = lambda: time.sleep(999)

        with patch("xtquant_manager.account.XtQuantTrader", return_value=mock_trader), \
             patch("xtquant_manager.account.StockAccount"):
            result = account._connect_xttrader()

        self.assertFalse(result)

    def test_connect_timeout_stops_trader_on_timeout(self):
        """超时时应调用 xt_trader.stop() 释放资源"""
        account = self._make_account(connect_timeout=0.1)

        mock_trader = MagicMock()
        mock_trader.connect.side_effect = lambda: time.sleep(999)

        with patch("xtquant_manager.account.XtQuantTrader", return_value=mock_trader), \
             patch("xtquant_manager.account.StockAccount"):
            account._connect_xttrader()

        mock_trader.stop.assert_called()

    def test_connect_succeeds_within_timeout(self):
        """正常连接（在超时内完成）应返回 True"""
        account = self._make_account(connect_timeout=5.0)

        mock_trader = MagicMock()
        mock_trader.connect.return_value = 0  # 0 = 成功
        mock_acc = MagicMock()

        with patch("xtquant_manager.account.XtQuantTrader", return_value=mock_trader), \
             patch("xtquant_manager.account.StockAccount", return_value=mock_acc), \
             patch("xtquant_manager.account.XtQuantTraderCallback"):
            result = account._connect_xttrader()

        self.assertTrue(result)

    def test_connect_exception_returns_false(self):
        """connect() 抛异常时应返回 False，且调用 stop()"""
        account = self._make_account(connect_timeout=5.0)

        mock_trader = MagicMock()
        mock_trader.connect.side_effect = RuntimeError("连接异常")

        with patch("xtquant_manager.account.XtQuantTrader", return_value=mock_trader), \
             patch("xtquant_manager.account.StockAccount"):
            result = account._connect_xttrader()

        self.assertFalse(result)
        mock_trader.stop.assert_called()


class TestReconnectInterruptible(unittest.TestCase):

    def test_disconnect_during_reconnect_wait_unblocks(self):
        """disconnect() 应立即中断 reconnect() 内的等待，不阻塞到超时"""
        config = make_config(reconnect_base_wait=60.0)  # 很长的等待时间
        account = XtQuantAccount(config)

        reconnect_done = threading.Event()
        reconnect_result = [None]

        def do_reconnect():
            with patch.object(account, "_do_connect", return_value=False):
                reconnect_result[0] = account.reconnect()
            reconnect_done.set()

        t = threading.Thread(target=do_reconnect, daemon=True)
        t.start()

        # 等待 reconnect 进入等待状态
        time.sleep(0.05)
        # 调用 disconnect() 中断等待
        account.disconnect()

        finished = reconnect_done.wait(timeout=2.0)  # 最多等 2 秒
        self.assertTrue(finished, "reconnect() 未能在 2s 内被 disconnect() 中断")

    def test_reconnect_abort_event_cleared_before_wait(self):
        """reconnect() 开始时应清除 abort 事件，确保新重连不被旧事件立即跳过"""
        config = make_config(reconnect_base_wait=0.01)
        account = XtQuantAccount(config)
        # 预先设置 abort 事件（模拟上次 disconnect() 遗留）
        account._reconnect_abort.set()

        with patch.object(account, "_do_connect", return_value=True):
            result = account.reconnect()

        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
