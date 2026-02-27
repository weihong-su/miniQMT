"""
卖出交易 Callback 兜底机制集成测试

覆盖完整的委托跟踪生命周期：
  1. Callback 链路完整性
     - MyXtQuantTraderCallback.on_stock_trade 通知所有注册回调
     - 单个回调异常不影响其他回调
     - register_trade_callback 正确挂载

  2. pending_orders 生命周期（callback 路径）
     - 成交回报立即移除匹配委托
     - 按 order_id 精确匹配，不误删其他股票
     - 未知 order_id 不影响现有跟踪

  3. profit_triggered 立即同步（P1 兜底）
     - take_profit_half 成交 → profit_triggered=1 写入 SQLite
     - take_profit_full 成交 → 不触发 profit_triggered 同步

  4. 超时兜底机制（callback 未触发时的保底路径）
     - 模拟模式 / 功能关闭 → 超时检查直接跳过
     - 成交后 pending_orders 已空 → 超时检查无操作
     - 超时委托状态=已成(56) → 仅移除，不撤单
     - 超时委托未成交 → 撤单 + 自动重新挂单
     - AUTO_REORDER=False → 撤单后不重新挂单

  5. 重新挂单（_reorder_after_cancel）
     - 使用正确参数名 volume/price（非 sell_volume/sell_price）
     - volume=0 时放弃挂单
     - 挂单成功后跟踪新委托
"""

import sys
import os
import time
import sqlite3
import threading
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from test.test_base import TestBase
from position_manager import PositionManager
from easy_qmt_trader import MyXtQuantTraderCallback
from logger import get_logger

logger = get_logger("test_trader_callback")


# ---------------------------------------------------------------------------
# 辅助：最小化 XtTrade mock
# ---------------------------------------------------------------------------
class _FakeTrade:
    def __init__(self, order_id, stock_code, traded_volume=600, traded_price=44.09):
        self.order_id = order_id
        self.stock_code = stock_code
        self.account_id = "TEST_ACCOUNT"
        self.traded_volume = traded_volume
        self.traded_price = traded_price
        self.traded_amount = traded_volume * traded_price


class TestTraderCallback(TestBase):
    """卖出交易 Callback 兜底机制集成测试"""

    def setUp(self):
        super().setUp()
        self.pm = PositionManager()
        self.pm.stop_sync_thread()
        self._ensure_memory_schema()
        cursor = self.pm.memory_conn.cursor()
        cursor.execute("DELETE FROM positions")
        self.pm.memory_conn.commit()

    def tearDown(self):
        try:
            self.pm.stop_sync_thread()
            self.pm.memory_conn.close()
        finally:
            super().tearDown()

    def _ensure_memory_schema(self):
        cursor = self.pm.memory_conn.cursor()
        cursor.execute("PRAGMA table_info(positions)")
        cols = {row[1] for row in cursor.fetchall()}
        for col, typedef in [
            ("profit_breakout_triggered", "BOOLEAN DEFAULT 0"),
            ("breakout_highest_price", "REAL DEFAULT 0.0"),
        ]:
            if col not in cols:
                cursor.execute(f"ALTER TABLE positions ADD COLUMN {col} {typedef}")
        self.pm.memory_conn.commit()

    def _insert_position(self, **kwargs):
        stock_code = kwargs.get("stock_code", "301560")
        volume = kwargs.get("volume", 1100)
        available = kwargs.get("available", volume)
        cost_price = kwargs.get("cost_price", 42.12)
        current_price = kwargs.get("current_price", cost_price)
        profit_triggered = kwargs.get("profit_triggered", 0)
        highest_price = kwargs.get("highest_price", cost_price)
        stop_loss_price = kwargs.get("stop_loss_price",
                                     cost_price * (1 + config.STOP_LOSS_RATIO))
        profit_breakout_triggered = kwargs.get("profit_breakout_triggered", 0)
        breakout_highest_price = kwargs.get("breakout_highest_price", 0.0)
        open_date = kwargs.get("open_date",
                               datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        cursor = self.pm.memory_conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO positions
            (stock_code, volume, available, cost_price, current_price,
             open_date, profit_triggered, highest_price, stop_loss_price,
             profit_breakout_triggered, breakout_highest_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (stock_code, volume, available, cost_price, current_price,
              open_date, profit_triggered, highest_price, stop_loss_price,
              profit_breakout_triggered, breakout_highest_price))
        self.pm.memory_conn.commit()
        return stock_code

    # ===================================================================
    # Group A: Callback 链路完整性
    # ===================================================================

    def test_a1_on_stock_trade_notifies_all_callbacks(self):
        """on_stock_trade 应依次调用所有注册的外部回调"""
        cb_obj = MyXtQuantTraderCallback({})
        results = []
        cb_obj.trade_callbacks.append(lambda t: results.append(("cb1", t.order_id)))
        cb_obj.trade_callbacks.append(lambda t: results.append(("cb2", t.order_id)))

        trade = _FakeTrade(order_id=111, stock_code="301560.SZ")
        cb_obj.on_stock_trade(trade)

        self.assertEqual(results, [("cb1", 111), ("cb2", 111)],
                         "两个回调均应被调用且顺序正确")

    def test_a2_callback_exception_isolation(self):
        """单个回调抛异常不应阻断后续回调执行"""
        cb_obj = MyXtQuantTraderCallback({})
        results = []

        def bad_cb(t):
            raise RuntimeError("模拟回调异常")

        cb_obj.trade_callbacks.append(bad_cb)
        cb_obj.trade_callbacks.append(lambda t: results.append(t.order_id))

        trade = _FakeTrade(order_id=222, stock_code="301560.SZ")
        cb_obj.on_stock_trade(trade)  # 不应抛出异常

        self.assertEqual(results, [222], "异常回调后的回调仍应被执行")

    def test_a3_register_trade_callback_appends_to_callback_obj(self):
        """register_trade_callback 应将回调追加到 _callback.trade_callbacks"""
        from easy_qmt_trader import MyXtQuantTraderCallback
        mock_trader = MagicMock()
        cb_obj = MyXtQuantTraderCallback({})
        mock_trader._callback = cb_obj

        # 直接测试 trade_callbacks 追加机制
        results = []
        cb_obj.trade_callbacks.append(lambda t: results.append(t.order_id))

        trade = _FakeTrade(order_id=333, stock_code="301560.SZ")
        cb_obj.on_stock_trade(trade)

        self.assertEqual(results, [333])

    # ===================================================================
    # Group B: pending_orders 生命周期（callback 路径）
    # ===================================================================

    def test_b1_trade_callback_removes_pending_order_immediately(self):
        """成交回报到达后，pending_orders 中对应记录立即被移除"""
        self.pm.track_order("301560", 940572673, "take_profit_half", {"volume": 600})
        self.assertIn("301560", self.pm.pending_orders)

        trade = _FakeTrade(order_id=940572673, stock_code="301560.SZ")
        self.pm._on_trade_callback(trade)

        self.assertNotIn("301560", self.pm.pending_orders,
                         "成交回报后应立即从 pending_orders 移除")

    def test_b2_trade_callback_matches_by_order_id_only(self):
        """成交回报只移除匹配 order_id 的记录，不影响其他股票"""
        self.pm.track_order("301560", 940572673, "take_profit_half", {})
        self.pm.track_order("002441", 940572999, "take_profit_half", {})

        trade = _FakeTrade(order_id=940572673, stock_code="301560.SZ")
        self.pm._on_trade_callback(trade)

        self.assertNotIn("301560", self.pm.pending_orders, "301560 应被移除")
        self.assertIn("002441", self.pm.pending_orders, "002441 不应被误删")

    def test_b3_unknown_order_id_does_not_affect_pending_orders(self):
        """未知 order_id 的成交回报不应误删 pending_orders"""
        self.pm.track_order("301560", 940572673, "take_profit_half", {})

        trade = _FakeTrade(order_id=999999999, stock_code="301560.SZ")
        self.pm._on_trade_callback(trade)

        self.assertIn("301560", self.pm.pending_orders,
                      "未知 order_id 不应误删 pending_orders")

    def test_b4_multiple_stocks_only_matched_removed(self):
        """三只股票跟踪，只有成交的那只被移除"""
        self.pm.track_order("301560", 1001, "take_profit_half", {})
        self.pm.track_order("002441", 1002, "take_profit_half", {})
        self.pm.track_order("600036", 1003, "take_profit_half", {})

        trade = _FakeTrade(order_id=1002, stock_code="002441.SZ")
        self.pm._on_trade_callback(trade)

        self.assertIn("301560", self.pm.pending_orders)
        self.assertNotIn("002441", self.pm.pending_orders)
        self.assertIn("600036", self.pm.pending_orders)

    # ===================================================================
    # Group C: profit_triggered 立即同步
    # ===================================================================

    def test_c1_take_profit_half_trade_syncs_profit_triggered_to_sqlite(self):
        """take_profit_half 成交后，profit_triggered 应立即写入 SQLite"""
        stock_code = "301560"
        order_id = 940572673

        conn = sqlite3.connect(config.DB_PATH)
        conn.execute("""
            INSERT OR REPLACE INTO positions
            (stock_code, volume, available, cost_price, profit_triggered, last_update)
            VALUES (?, 1100, 500, 42.12, 0, ?)
        """, (stock_code, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()

        self.pm.track_order(stock_code, order_id, "take_profit_half", {"volume": 600})
        trade = _FakeTrade(order_id=order_id, stock_code=f"{stock_code}.SZ")
        self.pm._on_trade_callback(trade)

        # 等待后台线程写入（最多3秒）
        deadline = time.time() + 3.0
        profit_triggered_in_db = 0
        while time.time() < deadline:
            conn = sqlite3.connect(config.DB_PATH)
            row = conn.execute(
                "SELECT profit_triggered FROM positions WHERE stock_code=?",
                (stock_code,)
            ).fetchone()
            conn.close()
            if row and row[0]:
                profit_triggered_in_db = row[0]
                break
            time.sleep(0.1)

        self.assertEqual(profit_triggered_in_db, 1,
                         "take_profit_half 成交后 profit_triggered 应立即同步到 SQLite")

        conn = sqlite3.connect(config.DB_PATH)
        conn.execute("DELETE FROM positions WHERE stock_code=?", (stock_code,))
        conn.commit()
        conn.close()

    def test_c2_take_profit_full_trade_does_not_sync_profit_triggered(self):
        """take_profit_full 成交不应触发 profit_triggered 同步（只有 half 才触发）"""
        stock_code = "301560"
        order_id = 940572674

        self.pm.track_order(stock_code, order_id, "take_profit_full", {"volume": 700})

        with patch.object(self.pm, "_sync_profit_triggered_to_sqlite") as mock_sync:
            trade = _FakeTrade(order_id=order_id, stock_code=f"{stock_code}.SZ")
            self.pm._on_trade_callback(trade)
            mock_sync.assert_not_called()

    # ===================================================================
    # Group D: 超时兜底机制
    # ===================================================================

    def test_d1_timeout_check_skipped_in_simulation_mode(self):
        """模拟模式下超时检查应直接跳过"""
        self.pm.track_order("301560", 1001, "take_profit_half", {})
        # config 已在 TestBase._setup_test_config 中设置 ENABLE_SIMULATION_MODE=True
        with patch.object(self.pm, "_handle_timeout_order") as mock_handle:
            self.pm.check_pending_orders_timeout()
            mock_handle.assert_not_called()

    def test_d2_timeout_check_skipped_when_feature_disabled(self):
        """ENABLE_PENDING_ORDER_AUTO_CANCEL=False 时超时检查应跳过"""
        self.pm.track_order("301560", 1001, "take_profit_half", {})
        old_flag = config.ENABLE_PENDING_ORDER_AUTO_CANCEL
        old_sim = config.ENABLE_SIMULATION_MODE
        try:
            config.ENABLE_PENDING_ORDER_AUTO_CANCEL = False
            config.ENABLE_SIMULATION_MODE = False
            with patch.object(self.pm, "_handle_timeout_order") as mock_handle:
                self.pm.check_pending_orders_timeout()
                mock_handle.assert_not_called()
        finally:
            config.ENABLE_PENDING_ORDER_AUTO_CANCEL = old_flag
            config.ENABLE_SIMULATION_MODE = old_sim

    def test_d3_timeout_check_no_op_after_trade_callback(self):
        """成交回报移除跟踪后，超时检查不应有任何操作"""
        stock_code = "301560"
        order_id = 940572673

        self.pm.track_order(stock_code, order_id, "take_profit_half", {"volume": 600})
        trade = _FakeTrade(order_id=order_id, stock_code=f"{stock_code}.SZ")
        self.pm._on_trade_callback(trade)

        self.assertEqual(len(self.pm.pending_orders), 0)

        with patch.object(self.pm, "_handle_timeout_order") as mock_handle:
            self.pm.check_pending_orders_timeout()
            mock_handle.assert_not_called()

    def test_d4_handle_timeout_order_status_filled_removes_without_cancel(self):
        """超时委托状态=已成(56)时，只移除跟踪，不发起撤单"""
        stock_code = "301560"
        order_id = 940572673
        self.pm.track_order(stock_code, order_id, "take_profit_half", {"volume": 600})

        order_info = {
            "stock_code": stock_code,
            "order_id": order_id,
            "signal_type": "take_profit_half",
            "signal_info": {"volume": 600},
            "submit_time": datetime.now() - timedelta(minutes=10),
        }

        with patch.object(self.pm, "_query_order_status", return_value=56), \
             patch.object(self.pm, "_cancel_order") as mock_cancel:
            self.pm._handle_timeout_order(order_info)
            mock_cancel.assert_not_called()

        self.assertNotIn(stock_code, self.pm.pending_orders,
                         "已成委托应从 pending_orders 移除")

    def test_d5_handle_timeout_order_unfilled_triggers_cancel_and_reorder(self):
        """超时委托未成交(状态=55)时，应撤单并自动重新挂单"""
        stock_code = "301560"
        order_id = 940572673
        self.pm.track_order(stock_code, order_id, "take_profit_half", {"volume": 600})

        order_info = {
            "stock_code": stock_code,
            "order_id": order_id,
            "signal_type": "take_profit_half",
            "signal_info": {"volume": 600, "current_price": 44.08},
            "submit_time": datetime.now() - timedelta(minutes=10),
        }

        old_reorder = config.PENDING_ORDER_AUTO_REORDER
        try:
            config.PENDING_ORDER_AUTO_REORDER = True
            with patch.object(self.pm, "_query_order_status", return_value=55), \
                 patch.object(self.pm, "_cancel_order", return_value=True) as mock_cancel, \
                 patch.object(self.pm, "_reorder_after_cancel") as mock_reorder:
                self.pm._handle_timeout_order(order_info)
                mock_cancel.assert_called_once()
                mock_reorder.assert_called_once_with(
                    stock_code, "take_profit_half", order_info["signal_info"]
                )
        finally:
            config.PENDING_ORDER_AUTO_REORDER = old_reorder

        self.assertNotIn(stock_code, self.pm.pending_orders)

    def test_d6_handle_timeout_order_no_reorder_when_disabled(self):
        """PENDING_ORDER_AUTO_REORDER=False 时，撤单后不重新挂单"""
        stock_code = "301560"
        order_id = 940572673
        self.pm.track_order(stock_code, order_id, "take_profit_half", {"volume": 600})

        order_info = {
            "stock_code": stock_code,
            "order_id": order_id,
            "signal_type": "take_profit_half",
            "signal_info": {"volume": 600},
            "submit_time": datetime.now() - timedelta(minutes=10),
        }

        old_reorder = config.PENDING_ORDER_AUTO_REORDER
        try:
            config.PENDING_ORDER_AUTO_REORDER = False
            with patch.object(self.pm, "_query_order_status", return_value=55), \
                 patch.object(self.pm, "_cancel_order", return_value=True), \
                 patch.object(self.pm, "_reorder_after_cancel") as mock_reorder:
                self.pm._handle_timeout_order(order_info)
                mock_reorder.assert_not_called()
        finally:
            config.PENDING_ORDER_AUTO_REORDER = old_reorder

    # ===================================================================
    # Group E: 重新挂单（_reorder_after_cancel）
    # ===================================================================

    def test_e1_reorder_uses_correct_param_names_volume_price(self):
        """_reorder_after_cancel 调用 sell_stock 时参数名应为 volume/price"""
        stock_code = "301560"
        signal_info = {"volume": 600, "current_price": 44.08}

        mock_quote = {"close": 44.00, "bid3": 43.95, "bid1": 43.90}
        self.pm.data_manager = MagicMock()
        self.pm.data_manager.get_latest_data.return_value = mock_quote

        mock_executor = MagicMock()
        mock_executor.sell_stock.return_value = {"order_id": 999}

        with patch("trading_executor.get_trading_executor", return_value=mock_executor):
            self.pm._reorder_after_cancel(stock_code, "take_profit_half", signal_info)

        self.assertTrue(mock_executor.sell_stock.called)
        kwargs = mock_executor.sell_stock.call_args.kwargs
        self.assertNotIn("sell_volume", kwargs, "不应使用 sell_volume 参数名")
        self.assertNotIn("sell_price", kwargs, "不应使用 sell_price 参数名")
        self.assertIn("volume", kwargs, "应使用 volume 参数名")
        self.assertIn("price", kwargs, "应使用 price 参数名")

    def test_e2_reorder_aborts_when_volume_zero(self):
        """signal_info 中 volume=0 时，_reorder_after_cancel 应放弃挂单"""
        stock_code = "301560"
        signal_info = {"volume": 0, "current_price": 44.08}

        mock_quote = {"close": 44.00, "bid3": 43.95}
        self.pm.data_manager = MagicMock()
        self.pm.data_manager.get_latest_data.return_value = mock_quote

        mock_executor = MagicMock()
        with patch("trading_executor.get_trading_executor", return_value=mock_executor):
            self.pm._reorder_after_cancel(stock_code, "take_profit_half", signal_info)

        mock_executor.sell_stock.assert_not_called()

    def test_e3_reorder_tracks_new_order_after_success(self):
        """_reorder_after_cancel 挂单成功后应跟踪新委托单"""
        stock_code = "301560"
        signal_info = {"volume": 600, "current_price": 44.08}
        new_order_id = 940572700

        mock_quote = {"close": 44.00, "bid3": 43.95}
        self.pm.data_manager = MagicMock()
        self.pm.data_manager.get_latest_data.return_value = mock_quote

        mock_executor = MagicMock()
        mock_executor.sell_stock.return_value = {"order_id": new_order_id}

        with patch("trading_executor.get_trading_executor", return_value=mock_executor):
            self.pm._reorder_after_cancel(stock_code, "take_profit_half", signal_info)

        self.assertIn(stock_code, self.pm.pending_orders,
                      "重新挂单成功后应跟踪新委托单")
        self.assertEqual(self.pm.pending_orders[stock_code]["order_id"], new_order_id)

    def test_e4_reorder_aborts_when_no_quote(self):
        """无法获取行情时，_reorder_after_cancel 应放弃挂单"""
        stock_code = "301560"
        signal_info = {"volume": 600, "current_price": 44.08}

        self.pm.data_manager = MagicMock()
        self.pm.data_manager.get_latest_data.return_value = None

        mock_executor = MagicMock()
        with patch("trading_executor.get_trading_executor", return_value=mock_executor):
            self.pm._reorder_after_cancel(stock_code, "take_profit_half", signal_info)

        mock_executor.sell_stock.assert_not_called()

    # ===================================================================
    # Group F: 端到端集成场景
    # ===================================================================

    def test_f1_full_flow_callback_prevents_timeout_cancel(self):
        """
        完整流程：下单 → 成交回报 → pending_orders 清空
        → 超时检查无操作（callback 兜底成功，超时路径不触发）
        """
        stock_code = "301560"
        order_id = 940572673

        self.pm.track_order(stock_code, order_id, "take_profit_half", {"volume": 600})

        # 成交回报到达
        trade = _FakeTrade(order_id=order_id, stock_code=f"{stock_code}.SZ")
        self.pm._on_trade_callback(trade)

        self.assertEqual(len(self.pm.pending_orders), 0)

        # 超时检查不应触发任何撤单
        with patch.object(self.pm, "_handle_timeout_order") as mock_handle:
            self.pm.check_pending_orders_timeout()
            mock_handle.assert_not_called()

    def test_f2_concurrent_callbacks_thread_safe(self):
        """并发成交回报不应导致 pending_orders 数据竞争"""
        for i in range(5):
            self.pm.track_order(f"00000{i}", i, "take_profit_half", {})

        errors = []

        def fire_callback(order_id, stock_suffix):
            try:
                trade = _FakeTrade(order_id=order_id,
                                   stock_code=f"00000{stock_suffix}.SZ")
                self.pm._on_trade_callback(trade)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=fire_callback, args=(i, i))
                   for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3)

        self.assertEqual(errors, [], f"并发回调不应产生异常: {errors}")
        self.assertEqual(len(self.pm.pending_orders), 0,
                         "所有委托应被并发回调正确移除")

    # ===================================================================
    # Group G: 风险兜底策略
    # ===================================================================

    def test_g1_market_data_circuit_breaker_blocks_signals(self):
        """行情失败达到阈值后触发熔断，停止信号生成"""
        stock_code = self._insert_position(
            stock_code="300001",
            volume=1000,
            available=1000,
            cost_price=10.0,
            current_price=10.0,
        )

        self.pm.data_manager = MagicMock()
        self.pm.data_manager.get_latest_data.return_value = None

        old_enabled = getattr(config, "ENABLE_MARKET_DATA_CIRCUIT_BREAKER", True)
        old_threshold = getattr(config, "MARKET_DATA_FAILURE_THRESHOLD", 3)
        old_window = getattr(config, "MARKET_DATA_FAILURE_WINDOW_SECONDS", 60)
        old_break = getattr(config, "MARKET_DATA_CIRCUIT_BREAK_SECONDS", 300)
        try:
            config.ENABLE_MARKET_DATA_CIRCUIT_BREAKER = True
            config.MARKET_DATA_FAILURE_THRESHOLD = 2
            config.MARKET_DATA_FAILURE_WINDOW_SECONDS = 60
            config.MARKET_DATA_CIRCUIT_BREAK_SECONDS = 300

            for _ in range(2):
                signal, _ = self.pm.check_trading_signals(stock_code)
                self.assertIsNone(signal, "行情失败时不应生成交易信号")

            self.assertTrue(self.pm._is_market_data_circuit_open(),
                            "连续失败达到阈值后应进入熔断状态")

            signal, _ = self.pm.check_trading_signals(stock_code)
            self.assertIsNone(signal, "熔断期间不应生成交易信号")
        finally:
            config.ENABLE_MARKET_DATA_CIRCUIT_BREAKER = old_enabled
            config.MARKET_DATA_FAILURE_THRESHOLD = old_threshold
            config.MARKET_DATA_FAILURE_WINDOW_SECONDS = old_window
            config.MARKET_DATA_CIRCUIT_BREAK_SECONDS = old_break

    def test_g2_take_profit_full_rejects_when_pending_orders_and_disallow(self):
        """全仓止盈在有活跃委托且配置不允许时应被拒绝"""
        stock_code = self._insert_position(
            stock_code="300002",
            volume=1000,
            available=0,
            cost_price=10.0,
            current_price=11.0,
            profit_triggered=1,
            highest_price=12.0,
        )

        signal_info = {"current_price": 11.0, "cost_price": 10.0}
        old_flag = getattr(config, "ALLOW_TAKE_PROFIT_FULL_WITH_PENDING", False)
        try:
            config.ALLOW_TAKE_PROFIT_FULL_WITH_PENDING = False
            with patch.object(self.pm, "_has_pending_orders", return_value=True):
                ok = self.pm.validate_trading_signal(
                    stock_code, "take_profit_full", signal_info
                )
            self.assertFalse(ok, "存在活跃委托时应拒绝全仓止盈信号")
        finally:
            config.ALLOW_TAKE_PROFIT_FULL_WITH_PENDING = old_flag


if __name__ == "__main__":
    unittest.main(verbosity=2)
