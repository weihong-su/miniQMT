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
     - 超时委托未成交 → 提交撤单请求，等待 54=已撤 后自动重新挂单
     - AUTO_REORDER=False → 撤单完成后不重新挂单

  5. 重新挂单（_reorder_after_cancel）
     - 使用正确参数名 volume/price（非 sell_volume/sell_price）
     - volume=0 时放弃挂单
     - 挂单成功后跟踪新委托
     - 方向门控：买入委托(add_position)撤单后不得被反手卖出
"""

import sys
import os
import time
import sqlite3
import threading
import unittest
import pandas as pd
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from test.test_base import TestBase
from position_manager import PositionManager
from trading_executor import TradingExecutor
from data_manager import DataManager
from easy_qmt_trader import MyXtQuantTraderCallback, easy_qmt_trader
from logger import get_logger

logger = get_logger("test_trader_callback")


# ---------------------------------------------------------------------------
# 辅助：最小化 XtTrade mock
# ---------------------------------------------------------------------------
class _FakeTrade:
    def __init__(self, order_id, stock_code, traded_volume=600, traded_price=44.09,
                 traded_id=None, order_type=24, traded_time=None,
                 strategy_name="", order_remark=""):
        self.order_id = order_id
        self.stock_code = stock_code
        self.account_id = "TEST_ACCOUNT"
        self.traded_volume = traded_volume
        self.traded_price = traded_price
        self.traded_amount = traded_volume * traded_price
        self.traded_id = traded_id or f"TRADE_{order_id}"
        self.order_type = order_type
        self.traded_time = traded_time if traded_time is not None else int(time.time())
        self.strategy_name = strategy_name
        self.order_remark = order_remark


class _FakeOrder:
    def __init__(self, stock_code, order_status, order_id=None,
                 order_type=24, order_volume=100, order_time=None,
                 strategy_name="", order_remark="", traded_volume=0):
        self.stock_code = stock_code
        self.order_status = order_status
        self.order_id = order_id
        self.order_type = order_type
        self.order_volume = order_volume
        self.order_time = order_time if order_time is not None else int(time.time())
        self.strategy_name = strategy_name
        self.order_remark = order_remark
        self.traded_volume = traded_volume


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

    def _make_live_executor(self):
        executor = TradingExecutor.__new__(TradingExecutor)
        executor.data_manager = MagicMock()
        executor.data_manager.get_stock_name.return_value = "测试股"
        executor.position_manager = MagicMock()
        executor.conn = sqlite3.connect(":memory:", check_same_thread=False)
        executor.conn.execute("""
            CREATE TABLE trade_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stock_code TEXT,
                stock_name TEXT,
                trade_time TIMESTAMP,
                trade_type TEXT,
                price REAL,
                volume INTEGER,
                amount REAL,
                trade_id TEXT,
                commission REAL,
                strategy TEXT
            )
        """)
        executor.conn.commit()
        executor.order_cache = {}
        executor.callbacks = {}
        executor.trade_lock = threading.Lock()
        executor._trade_record_lock = threading.RLock()
        executor._unknown_order_submissions = {}
        return executor

    def _make_name_resolving_data_manager(self):
        """构造最小 DataManager，让股票名称查询走真实逻辑但不访问网络。"""
        dm = object.__new__(DataManager)
        dm.xt = MagicMock()
        dm.xt.get_instrument_detail.return_value = None
        dm.stock_names_cache = {}
        dm._tushare_token_attempted = True
        dm._tushare_pro = None
        dm._ts_cooldown_until = 0.0
        dm._ts_consecutive_failures = 0
        dm._bs_consecutive_failures = 0
        dm._bs_cooldown_until = 0.0
        return dm

    def _make_orderable_executor(self):
        executor = self._make_live_executor()
        qmt_trader = MagicMock()
        qmt_trader.adjust_stock.side_effect = (
            lambda stock: stock if "." in stock else f"{stock}.SZ"
        )
        qmt_trader.check_stock_is_av_sell.return_value = True
        qmt_trader.check_stock_is_av_buy.return_value = True
        qmt_trader.ensure_trade_push_ready.return_value = True
        qmt_trader.sell.return_value = 1
        qmt_trader.buy.return_value = 1
        executor.position_manager.qmt_trader = qmt_trader
        executor.position_manager._get_real_order_id.return_value = 940572800
        executor.position_manager.track_order = MagicMock()
        return executor

    def test_order_format_bj_bare_code_bypasses_qmt_adjust_stock(self):
        """裸 920 段下单应本地识别为 .BJ，不再交给 qmt_trader 默认补 .SZ。"""
        executor = self._make_orderable_executor()
        qmt_trader = executor.position_manager.qmt_trader

        old_sim = config.ENABLE_SIMULATION_MODE
        old_allow_buy = getattr(config, "ENABLE_ALLOW_BUY", True)
        try:
            config.ENABLE_SIMULATION_MODE = False
            config.ENABLE_ALLOW_BUY = True
            with patch("config.is_trade_time", return_value=True):
                order_id = executor.buy_stock(
                    "920118",
                    volume=100,
                    price=10.5,
                    strategy="add_position",
                )
        finally:
            config.ENABLE_SIMULATION_MODE = old_sim
            config.ENABLE_ALLOW_BUY = old_allow_buy

        self.assertEqual(order_id, 940572800)
        qmt_trader.adjust_stock.assert_not_called()
        qmt_trader.buy.assert_called_once()
        self.assertEqual(qmt_trader.buy.call_args.kwargs["security"], "920118.BJ")

    def test_easy_qmt_adjust_stock_supports_bj_without_second_guessing(self):
        """直连交易适配层应保留 .BJ，避免把北交所代码二次补成 .SZ。"""
        trader = easy_qmt_trader.__new__(easy_qmt_trader)
        self.assertEqual(trader.adjust_stock("920118"), "920118.BJ")
        self.assertEqual(trader.adjust_stock("920118.BJ"), "920118.BJ")
        self.assertEqual(trader.adjust_stock("bj.920118"), "920118.BJ")
        self.assertEqual(trader.adjust_stock("830799.BJ"), "830799.BJ")
        self.assertEqual(trader.adjust_stock("000920"), "000920.SZ")

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
        mock_trader = MagicMock()
        cb_obj = MyXtQuantTraderCallback({})
        mock_trader._callback = cb_obj

        # 直接测试 trade_callbacks 追加机制
        results = []
        cb_obj.trade_callbacks.append(lambda t: results.append(t.order_id))

        trade = _FakeTrade(order_id=333, stock_code="301560.SZ")
        cb_obj.on_stock_trade(trade)

        self.assertEqual(results, [333])

    def test_a3b_async_response_stores_int_and_string_seq_mapping(self):
        """QMT异步响应到达后，应同时兼容 int/str 两种 seq 读取方式。"""
        order_id_map = {}
        cb_obj = MyXtQuantTraderCallback(order_id_map)
        response = MagicMock()
        response.account_id = "TEST_ACCOUNT"
        response.seq = 1277
        response.order_id = 940572675

        cb_obj.on_order_stock_async_response(response)

        self.assertEqual(order_id_map[1277], 940572675)
        self.assertEqual(order_id_map["1277"], 940572675)

    def test_a4_register_callbacks_are_deduplicated(self):
        """重复注册同一个外部回调时，不应在 callback 列表中堆叠多份。"""
        trader = easy_qmt_trader(path="dummy", account="TEST_ACCOUNT")
        trader._callback = MyXtQuantTraderCallback({})
        cb = lambda event: event

        trader.register_trade_callback(cb)
        trader.register_trade_callback(cb)
        trader.register_order_callback(cb)
        trader.register_order_callback(cb)
        trader.register_disconnect_callback(cb)
        trader.register_disconnect_callback(cb)

        self.assertEqual(trader._callback.trade_callbacks, [cb])
        self.assertEqual(trader._callback.order_callbacks, [cb])
        self.assertEqual(trader._callback.disconnect_callbacks, [cb])

    def test_a5_ensure_trade_push_ready_rebuilds_detached_callback(self):
        """callback 已失效时，ensure_trade_push_ready 应重建 callback 并重新订阅。"""
        class StubXtTrader:
            def __init__(self):
                self.registered_callback = None
                self.subscribe_calls = []

            def register_callback(self, callback):
                self.registered_callback = callback

            def subscribe(self, account):
                self.subscribe_calls.append(account)
                return 0

        trader = easy_qmt_trader(path="dummy", account="TEST_ACCOUNT")
        old_callback = MyXtQuantTraderCallback(trader.order_id_map)
        trade_cb = lambda trade: trade
        old_callback.trade_callbacks.append(trade_cb)
        old_callback.detach()
        trader._callback = old_callback
        trader.xt_trader = StubXtTrader()
        trader.acc = object()

        self.assertTrue(trader.ensure_trade_push_ready())
        self.assertIsNot(trader._callback, old_callback)
        self.assertFalse(trader._callback.detached)
        self.assertEqual(trader._callback.trade_callbacks, [trade_cb])
        self.assertIs(trader.xt_trader.registered_callback, trader._callback)
        self.assertEqual(trader.xt_trader.subscribe_calls, [trader.acc])

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

    def test_c1b_take_profit_half_trade_marks_memory_profit_triggered(self):
        """take_profit_half 成交后，内存持仓也应标记 profit_triggered"""
        stock_code = self._insert_position(
            stock_code="301560",
            volume=1100,
            available=500,
            cost_price=42.12,
            current_price=44.09,
            profit_triggered=0,
        )
        order_id = 940572674

        self.pm.track_order(stock_code, order_id, "take_profit_half", {"volume": 600})
        trade = _FakeTrade(order_id=order_id, stock_code=f"{stock_code}.SZ")
        self.pm._on_trade_callback(trade)

        position = self.pm.get_position(stock_code)
        self.assertTrue(position.get("profit_triggered"),
                        "成交回报后内存 profit_triggered 应为 True")

    def test_c2_take_profit_full_trade_does_not_sync_profit_triggered(self):
        """take_profit_full 成交不应触发 profit_triggered 同步（只有 half 才触发）"""
        stock_code = "301560"
        order_id = 940572674

        self.pm.track_order(stock_code, order_id, "take_profit_full", {"volume": 700})

        with patch.object(self.pm, "_sync_profit_triggered_to_sqlite") as mock_sync:
            trade = _FakeTrade(order_id=order_id, stock_code=f"{stock_code}.SZ")
            self.pm._on_trade_callback(trade)
            mock_sync.assert_not_called()

    def test_c2b_take_profit_full_trade_pauses_grid_session_when_enabled(self):
        """take_profit_full 成交后，默认应暂停该股票网格会话。"""
        stock_code = "301560"
        order_id = 940572675
        self.pm.track_order(stock_code, order_id, "take_profit_full", {"volume": 700})
        self.pm.grid_manager = MagicMock()
        self.pm.grid_manager.pause_session_by_stock.return_value = {
            "paused": True,
            "session_id": 12,
            "stock_code": f"{stock_code}.SZ"
        }

        old_flag = getattr(config, "ENABLE_PAUSE_GRID_AFTER_TAKE_PROFIT_FULL", True)
        try:
            config.ENABLE_PAUSE_GRID_AFTER_TAKE_PROFIT_FULL = True
            with patch.object(self.pm, "_request_immediate_position_refresh"):
                trade = _FakeTrade(order_id=order_id, stock_code=f"{stock_code}.SZ")
                self.pm._on_trade_callback(trade)
        finally:
            config.ENABLE_PAUSE_GRID_AFTER_TAKE_PROFIT_FULL = old_flag

        self.pm.grid_manager.pause_session_by_stock.assert_called_once_with(
            stock_code,
            reason='take_profit_full'
        )

    def test_c2c_take_profit_full_trade_does_not_pause_grid_when_disabled(self):
        """开关关闭时，take_profit_full 成交不应暂停网格会话。"""
        stock_code = "301560"
        order_id = 940572676
        self.pm.track_order(stock_code, order_id, "take_profit_full", {"volume": 700})
        self.pm.grid_manager = MagicMock()

        old_flag = getattr(config, "ENABLE_PAUSE_GRID_AFTER_TAKE_PROFIT_FULL", True)
        try:
            config.ENABLE_PAUSE_GRID_AFTER_TAKE_PROFIT_FULL = False
            with patch.object(self.pm, "_request_immediate_position_refresh"):
                trade = _FakeTrade(order_id=order_id, stock_code=f"{stock_code}.SZ")
                self.pm._on_trade_callback(trade)
        finally:
            config.ENABLE_PAUSE_GRID_AFTER_TAKE_PROFIT_FULL = old_flag

        self.pm.grid_manager.pause_session_by_stock.assert_not_called()

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

    def test_d2a_timeout_check_skipped_during_preorder_window(self):
        """预挂窗口内即使自然时间已超时，也不应查询或撤销委托。"""
        self.pm.track_order("301560.SZ", 1001, "stop_loss", {"volume": 600})
        old_sim = config.ENABLE_SIMULATION_MODE
        old_flag = config.ENABLE_PENDING_ORDER_AUTO_CANCEL
        try:
            config.ENABLE_SIMULATION_MODE = False
            config.ENABLE_PENDING_ORDER_AUTO_CANCEL = True
            self.pm.last_order_check_time = 0
            with self.pm.pending_orders_lock:
                self.pm.pending_orders["301560"]["submit_time"] = (
                    datetime.now() - timedelta(hours=1)
                )

            with patch("config.is_continuous_trade_time", return_value=False), \
                 patch.object(self.pm, "_handle_timeout_order") as mock_handle:
                self.pm.check_pending_orders_timeout()

            mock_handle.assert_not_called()
        finally:
            config.ENABLE_SIMULATION_MODE = old_sim
            config.ENABLE_PENDING_ORDER_AUTO_CANCEL = old_flag

    def test_d2b_timeout_check_uses_continuous_trading_seconds(self):
        """进入连续竞价后，达到30个有效交易秒才进入超时处理。"""
        self.pm.track_order("301560", 1001, "stop_loss", {"volume": 600})
        old_sim = config.ENABLE_SIMULATION_MODE
        old_flag = config.ENABLE_PENDING_ORDER_AUTO_CANCEL
        try:
            config.ENABLE_SIMULATION_MODE = False
            config.ENABLE_PENDING_ORDER_AUTO_CANCEL = True

            with patch("config.is_continuous_trade_time", return_value=True), \
                 patch("config.get_continuous_trading_seconds", return_value=29), \
                 patch.object(self.pm, "_handle_timeout_order") as mock_handle:
                self.pm.last_order_check_time = 0
                self.pm.check_pending_orders_timeout()
                mock_handle.assert_not_called()

            with patch("config.is_continuous_trade_time", return_value=True), \
                 patch("config.get_continuous_trading_seconds", return_value=30), \
                 patch.object(self.pm, "_handle_timeout_order") as mock_handle:
                self.pm.last_order_check_time = 0
                self.pm.check_pending_orders_timeout()
                mock_handle.assert_called_once()
                self.assertEqual(mock_handle.call_args.args[0]["elapsed_minutes"], 0.5)
        finally:
            config.ENABLE_SIMULATION_MODE = old_sim
            config.ENABLE_PENDING_ORDER_AUTO_CANCEL = old_flag

    def test_d2c_track_order_normalizes_stock_code_key(self):
        """同一委托的裸代码与市场后缀代码只能生成一条跟踪记录。"""
        self.pm.track_order("301560.SZ", 1001, "stop_loss", {"volume": 600})
        self.pm.track_order("301560", 1001, "stop_loss", {"volume": 600})

        self.assertEqual(list(self.pm.pending_orders), ["301560"])
        self.assertEqual(self.pm.pending_orders["301560"]["stock_code"], "301560")

    def test_d2_stop_loss_and_take_profit_use_shorter_timeout(self):
        """止损与止盈委托均使用 30 秒超时阈值，其他信号仍使用全局阈值。"""
        old_sim = config.ENABLE_SIMULATION_MODE
        old_flag = config.ENABLE_PENDING_ORDER_AUTO_CANCEL
        old_stop_loss_timeout = config.STOP_LOSS_PENDING_ORDER_TIMEOUT_MINUTES
        old_take_profit_timeout = config.TAKE_PROFIT_PENDING_ORDER_TIMEOUT_MINUTES
        old_default_timeout = config.PENDING_ORDER_TIMEOUT_MINUTES
        try:
            config.ENABLE_SIMULATION_MODE = False
            config.ENABLE_PENDING_ORDER_AUTO_CANCEL = True
            config.STOP_LOSS_PENDING_ORDER_TIMEOUT_MINUTES = 0.5
            config.TAKE_PROFIT_PENDING_ORDER_TIMEOUT_MINUTES = 0.5
            config.PENDING_ORDER_TIMEOUT_MINUTES = 5
            self.pm.last_order_check_time = 0

            self.pm.track_order("301560", 1001, "stop_loss", {})
            self.pm.track_order("301561", 1002, "take_profit_half", {})
            self.pm.track_order("301562", 1003, "take_profit_full", {})
            self.pm.track_order("301563", 1004, "add_position", {})
            with self.pm.pending_orders_lock:
                for code in ("301560", "301561", "301562", "301563"):
                    self.pm.pending_orders[code]["submit_time"] = datetime.now() - timedelta(minutes=0.6)

            with patch("config.is_continuous_trade_time", return_value=True), \
                 patch(
                     "config.get_continuous_trading_seconds",
                     side_effect=lambda start, end: (end - start).total_seconds()
                 ), \
                 patch.object(self.pm, "_handle_timeout_order") as mock_handle:
                self.pm.check_pending_orders_timeout()

            handled = {
                call.args[0]["stock_code"]: call.args[0]["timeout_minutes"]
                for call in mock_handle.call_args_list
            }
            # 止损/止盈已超过 0.5 分钟阈值，应被处理
            self.assertEqual(handled, {"301560": 0.5, "301561": 0.5, "301562": 0.5})
            # add_position 仍走 5 分钟全局阈值，0.6 分钟不应触发
            self.assertNotIn("301563", handled)
        finally:
            config.ENABLE_SIMULATION_MODE = old_sim
            config.ENABLE_PENDING_ORDER_AUTO_CANCEL = old_flag
            config.STOP_LOSS_PENDING_ORDER_TIMEOUT_MINUTES = old_stop_loss_timeout
            config.TAKE_PROFIT_PENDING_ORDER_TIMEOUT_MINUTES = old_take_profit_timeout
            config.PENDING_ORDER_TIMEOUT_MINUTES = old_default_timeout

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

    def test_d3_trade_callback_requests_position_refresh(self):
        """成交回报到达后应调度一次持仓快刷。"""
        stock_code = "301560"
        order_id = 940572673
        self.pm.track_order(stock_code, order_id, "stop_loss", {"volume": 200})

        trade = _FakeTrade(order_id=order_id, stock_code=f"{stock_code}.SZ")
        with patch.object(self.pm, "_request_immediate_position_refresh") as mock_refresh:
            self.pm._on_trade_callback(trade)

        mock_refresh.assert_called_once_with(stock_code, "成交回报")

    def test_d3_terminal_order_callback_requests_position_refresh(self):
        """委托状态进入终态后应调度一次持仓快刷。"""
        order = _FakeOrder("301560.SZ", 56)

        with patch.object(self.pm, "_request_immediate_position_refresh") as mock_refresh:
            self.pm._on_order_callback(order)

        mock_refresh.assert_called_once_with("301560", "委托终态(56)")

    def test_d4_handle_timeout_order_status_filled_confirms_without_cancel(self):
        """超时委托状态=已成(56)时，应走成交兜底确认，不发起撤单"""
        stock_code = "301560"
        order_id = 940572673
        self._insert_position(
            stock_code=stock_code,
            volume=1100,
            available=500,
            cost_price=42.12,
            current_price=44.09,
            profit_triggered=0,
        )
        self.pm.track_order(stock_code, order_id, "take_profit_half", {"volume": 600})

        order_info = {
            "stock_code": stock_code,
            "order_id": order_id,
            "signal_type": "take_profit_half",
            "signal_info": {"volume": 600},
            "submit_time": datetime.now() - timedelta(minutes=10),
        }

        with patch.object(self.pm, "_query_order_status", return_value=56), \
             patch.object(self.pm, "_cancel_order") as mock_cancel, \
             patch.object(self.pm, "_record_trade_after_confirmation") as mock_record, \
             patch.object(self.pm, "_request_immediate_position_refresh") as mock_refresh:
            self.pm._handle_timeout_order(order_info)
            mock_cancel.assert_not_called()
            mock_record.assert_called_once()
            mock_refresh.assert_called_once_with(stock_code, "成交兜底确认")

        self.assertNotIn(stock_code, self.pm.pending_orders,
                         "已成委托应从 pending_orders 移除")
        position = self.pm.get_position(stock_code)
        self.assertTrue(position.get("profit_triggered"),
                        "兜底确认 take_profit_half 后应标记 profit_triggered")

    def test_d4b_handle_timeout_order_unknown_status_keeps_pending(self):
        """无法查询委托状态时应保留 pending，避免不确定状态下重复下单"""
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

        with patch.object(self.pm, "_query_order_status", return_value=None), \
             patch.object(self.pm, "_cancel_order") as mock_cancel:
            self.pm._handle_timeout_order(order_info)

        mock_cancel.assert_not_called()
        self.assertIn(stock_code, self.pm.pending_orders,
                      "状态未知时 pending 应继续保留")

    def test_d5_handle_timeout_order_unfilled_marks_cancel_requested_without_reorder(self):
        """超时委托未成交(状态=55)时，只提交撤单请求，不立即重新挂单"""
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
                mock_reorder.assert_not_called()
        finally:
            config.PENDING_ORDER_AUTO_REORDER = old_reorder

        self.assertIn(stock_code, self.pm.pending_orders)
        self.assertEqual(self.pm.pending_orders[stock_code]["status"], "cancel_requested")
        self.assertTrue(self.pm.pending_orders[stock_code]["reorder_after_cancel"])

    def test_d5b_cancel_callback_reorders_after_cancel_confirmed(self):
        """收到 54=已撤 回调后才重挂，新委托不应被旧委托清理逻辑误删"""
        stock_code = "301560"
        old_order_id = 940572673
        new_order_id = 940572700
        self.pm.track_order(stock_code, old_order_id, "take_profit_half", {"volume": 600})

        order_info = {
            "stock_code": stock_code,
            "order_id": old_order_id,
            "signal_type": "take_profit_half",
            "signal_info": {"volume": 600, "current_price": 44.08},
            "submit_time": datetime.now() - timedelta(minutes=10),
        }
        self.pm.data_manager = MagicMock()
        self.pm.data_manager.get_latest_data.return_value = {
            "lastPrice": 44.02,
            "bid3": 43.95,
        }

        mock_executor = MagicMock()
        def submit_reordered_order(**kwargs):
            self.pm.track_order(
                kwargs["stock_code"],
                new_order_id,
                kwargs["signal_type"],
                kwargs["signal_info"],
            )
            return new_order_id

        mock_executor.sell_stock.side_effect = submit_reordered_order

        old_reorder = config.PENDING_ORDER_AUTO_REORDER
        try:
            config.PENDING_ORDER_AUTO_REORDER = True
            with patch.object(self.pm, "_query_order_status", return_value=55), \
                  patch.object(self.pm, "_cancel_order", return_value=True), \
                  patch("trading_executor.get_trading_executor", return_value=mock_executor):
                self.pm._handle_timeout_order(order_info)
                self.assertEqual(self.pm.pending_orders[stock_code]["order_id"], old_order_id)
                self.assertEqual(self.pm.pending_orders[stock_code]["status"], "cancel_requested")
                mock_executor.sell_stock.assert_not_called()
                self.pm._on_order_callback(_FakeOrder(stock_code, 54, old_order_id))
        finally:
            config.PENDING_ORDER_AUTO_REORDER = old_reorder

        self.assertIn(stock_code, self.pm.pending_orders)
        self.assertEqual(self.pm.pending_orders[stock_code]["order_id"], new_order_id)

    def test_d5c_cancel_status_query_reorders_without_callback(self):
        """没有撤单回调时，后续状态查询到 54=已撤 也应触发重挂"""
        stock_code = "301560"
        old_order_id = 940572673
        new_order_id = 940572700
        self.pm.track_order(stock_code, old_order_id, "take_profit_half", {"volume": 600})
        with self.pm.pending_orders_lock:
            self.pm.pending_orders[stock_code].update({
                "status": "cancel_requested",
                "reorder_after_cancel": True,
                "signal_info": {"volume": 600, "current_price": 44.08},
                "submit_time": datetime.now() - timedelta(minutes=10),
            })

        self.pm.data_manager = MagicMock()
        self.pm.data_manager.get_latest_data.return_value = {
            "lastPrice": 44.02,
            "bid3": 43.95,
        }
        mock_executor = MagicMock()
        def submit_reordered_order(**kwargs):
            self.pm.track_order(
                kwargs["stock_code"],
                new_order_id,
                kwargs["signal_type"],
                kwargs["signal_info"],
            )
            return new_order_id

        mock_executor.sell_stock.side_effect = submit_reordered_order

        with patch.object(self.pm, "_query_order_status", return_value=54), \
             patch("trading_executor.get_trading_executor", return_value=mock_executor):
            self.pm._handle_timeout_order(dict(self.pm.pending_orders[stock_code]))

        self.assertIn(stock_code, self.pm.pending_orders)
        self.assertEqual(self.pm.pending_orders[stock_code]["order_id"], new_order_id)

    def test_d5e_reorder_rejects_buy_side_order(self):
        """买入委托(order_side=BUY)撤单后不得被反手卖出"""
        self.pm.data_manager = MagicMock()
        mock_executor = MagicMock()

        with patch("trading_executor.get_trading_executor", return_value=mock_executor):
            self.pm._reorder_after_cancel(
                "301560",
                "add_position",
                {"volume": 600, "current_price": 44.08, "order_side": "BUY"}
            )

        mock_executor.sell_stock.assert_not_called()
        mock_executor.buy_stock.assert_not_called()
        # 门控应在取行情之前拦截，避免无谓的行情查询
        self.pm.data_manager.get_latest_data.assert_not_called()

    def test_d5f_reorder_allows_explicit_sell_side_order(self):
        """卖出委托(order_side=SELL)不受方向门控影响，正常重挂"""
        self.pm.data_manager = MagicMock()
        self.pm.data_manager.get_latest_data.return_value = {
            "lastPrice": 44.02,
            "bid3": 43.95,
        }
        mock_executor = MagicMock()
        mock_executor.sell_stock.return_value = 940572700

        with patch("trading_executor.get_trading_executor", return_value=mock_executor):
            self.pm._reorder_after_cancel(
                "301560",
                "take_profit_half",
                {"volume": 600, "current_price": 44.08, "order_side": "SELL"}
            )

        mock_executor.sell_stock.assert_called_once()
        self.assertEqual(mock_executor.sell_stock.call_args.kwargs["volume"], 600)
        self.assertEqual(
            mock_executor.sell_stock.call_args.kwargs["strategy"],
            "reorder_take_profit_half"
        )

    def test_d5g_reorder_allows_legacy_sell_signal_without_order_side(self):
        """历史 signal_info 缺 order_side 时，按卖出信号类型兜底放行"""
        self.pm.data_manager = MagicMock()
        self.pm.data_manager.get_latest_data.return_value = {
            "lastPrice": 44.02,
            "bid3": 43.95,
        }
        mock_executor = MagicMock()
        mock_executor.sell_stock.return_value = 940572700

        for signal_type in ("take_profit_half", "take_profit_full", "stop_loss"):
            mock_executor.reset_mock()
            with patch("trading_executor.get_trading_executor", return_value=mock_executor):
                self.pm._reorder_after_cancel(
                    "301560",
                    signal_type,
                    {"volume": 600, "current_price": 44.08}
                )
            mock_executor.sell_stock.assert_called_once()
            self.assertEqual(
                mock_executor.sell_stock.call_args.kwargs["strategy"],
                f"reorder_{signal_type}"
            )

    def test_d5h_reorder_rejects_unknown_signal_without_order_side(self):
        """方向无法判定时保守放弃重挂，不下任何单"""
        self.pm.data_manager = MagicMock()
        mock_executor = MagicMock()

        with patch("trading_executor.get_trading_executor", return_value=mock_executor):
            self.pm._reorder_after_cancel(
                "301560",
                "some_unknown_signal",
                {"volume": 600, "current_price": 44.08}
            )

        mock_executor.sell_stock.assert_not_called()
        self.pm.data_manager.get_latest_data.assert_not_called()

    def test_d5i_buy_order_timeout_does_not_trigger_reverse_sell(self):
        """端到端：补仓买单超时撤单后，全链路不得产生卖出委托"""
        stock_code = "301560"
        old_order_id = 940572673
        self.pm.track_order(
            stock_code, old_order_id, "add_position",
            {"volume": 600, "order_side": "BUY"}
        )

        order_info = {
            "stock_code": stock_code,
            "order_id": old_order_id,
            "signal_type": "add_position",
            "signal_info": {"volume": 600, "current_price": 44.08, "order_side": "BUY"},
            "submit_time": datetime.now() - timedelta(minutes=10),
        }
        self.pm.data_manager = MagicMock()
        self.pm.data_manager.get_latest_data.return_value = {
            "lastPrice": 44.02,
            "bid3": 43.95,
        }
        mock_executor = MagicMock()

        old_reorder = config.PENDING_ORDER_AUTO_REORDER
        try:
            config.PENDING_ORDER_AUTO_REORDER = True
            with patch.object(self.pm, "_query_order_status", return_value=55), \
                 patch.object(self.pm, "_cancel_order", return_value=True) as mock_cancel, \
                 patch("trading_executor.get_trading_executor", return_value=mock_executor):
                # 超时 → 提交撤单请求
                self.pm._handle_timeout_order(order_info)
                mock_cancel.assert_called_once()
                # 撤单成交回报 → 进入重挂分支，但应被方向门控拦截
                self.pm._on_order_callback(_FakeOrder(stock_code, 54, old_order_id))
        finally:
            config.PENDING_ORDER_AUTO_REORDER = old_reorder

        mock_executor.sell_stock.assert_not_called()

    def test_d5d_validate_blocks_when_local_pending_order_exists(self):
        """已有本地跟踪委托时，即使可卖数量大于0也应阻断新止盈信号"""
        stock_code = self._insert_position(
            stock_code="301560",
            volume=1100,
            available=500,
            cost_price=42.12,
            current_price=44.09,
            profit_triggered=0,
        )
        self.pm.track_order(stock_code, 940572675, "take_profit_half", {"volume": 600})

        ok, status, reason = self.pm.validate_trading_signal(
            stock_code,
            "take_profit_half",
            {"current_price": 44.09, "cost_price": 42.12},
            return_reason=True
        )

        self.assertFalse(ok)
        self.assertEqual(status, "blocked")
        self.assertEqual(reason, "pending_order")

    def test_d6_handle_timeout_order_no_reorder_when_disabled(self):
        """PENDING_ORDER_AUTO_REORDER=False 时，撤单完成后不重新挂单"""
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
                self.assertEqual(self.pm.pending_orders[stock_code]["status"], "cancel_requested")
                self.pm._on_order_callback(_FakeOrder(stock_code, 54, order_id))
                mock_reorder.assert_not_called()
        finally:
            config.PENDING_ORDER_AUTO_REORDER = old_reorder

        self.assertNotIn(stock_code, self.pm.pending_orders)

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

    def test_e1b_reorder_best_falls_back_when_bid3_zero(self):
        """对手价模式下买三价为0时，应降级使用有效行情价"""
        stock_code = "603466"
        signal_info = {"volume": 3100, "current_price": 11.93}

        mock_quote = {"bid3": 0, "lastPrice": 11.93, "close": 11.92}
        self.pm.data_manager = MagicMock()
        self.pm.data_manager.get_latest_data.return_value = mock_quote

        mock_executor = MagicMock()
        mock_executor.sell_stock.return_value = {"order_id": 999}

        old_mode = config.PENDING_ORDER_REORDER_PRICE_MODE
        try:
            config.PENDING_ORDER_REORDER_PRICE_MODE = "best"
            with patch("trading_executor.get_trading_executor", return_value=mock_executor):
                self.pm._reorder_after_cancel(stock_code, "take_profit_half", signal_info)
        finally:
            config.PENDING_ORDER_REORDER_PRICE_MODE = old_mode

        kwargs = mock_executor.sell_stock.call_args.kwargs
        self.assertEqual(kwargs["price"], 11.93)

    def test_e1c_live_take_profit_half_does_not_mark_before_deal(self):
        """实盘首次止盈委托提交成功后，不应在成交前标记 profit_triggered"""
        from strategy import TradingStrategy

        strategy = TradingStrategy.__new__(TradingStrategy)
        strategy.position_manager = MagicMock()
        strategy.trading_executor = MagicMock()
        strategy.trading_executor.sell_stock.return_value = 672137218

        old_sim = config.ENABLE_SIMULATION_MODE
        try:
            config.ENABLE_SIMULATION_MODE = False
            success = strategy._execute_take_profit_half_signal("603466", {
                "volume": 5200,
                "current_price": 11.93,
                "cost_price": 11.39,
                "sell_ratio": 0.6,
                "breakout_highest_price": 12.03,
                "pullback_ratio": 0.0083,
            })
        finally:
            config.ENABLE_SIMULATION_MODE = old_sim

        self.assertTrue(success)
        strategy.position_manager.mark_profit_triggered.assert_not_called()

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

    def test_e3_reorder_does_not_track_order_twice(self):
        """sell_stock 负责跟踪新委托，重挂层不得再次写入 pending_orders。"""
        stock_code = "301560"
        signal_info = {"volume": 600, "current_price": 44.08}
        new_order_id = 940572700

        mock_quote = {"close": 44.00, "bid3": 43.95}
        self.pm.data_manager = MagicMock()
        self.pm.data_manager.get_latest_data.return_value = mock_quote

        mock_executor = MagicMock()
        mock_executor.sell_stock.return_value = {"order_id": new_order_id}

        with patch("trading_executor.get_trading_executor", return_value=mock_executor), \
             patch.object(self.pm, "track_order") as mock_track:
            self.pm._reorder_after_cancel(stock_code, "take_profit_half", signal_info)

        mock_executor.sell_stock.assert_called_once()
        mock_track.assert_not_called()

    def test_e3b_reorder_string_result_does_not_track_order_twice(self):
        """sell_stock 返回字符串委托号时，重挂层同样不得重复跟踪。"""
        stock_code = "301560"
        signal_info = {"volume": 600, "current_price": 44.08}
        new_order_id = "940572701"

        mock_quote = {"close": 44.00, "bid3": 43.95}
        self.pm.data_manager = MagicMock()
        self.pm.data_manager.get_latest_data.return_value = mock_quote

        mock_executor = MagicMock()
        mock_executor.sell_stock.return_value = new_order_id

        with patch("trading_executor.get_trading_executor", return_value=mock_executor), \
             patch.object(self.pm, "track_order") as mock_track:
            self.pm._reorder_after_cancel(stock_code, "take_profit_half", signal_info)

        mock_executor.sell_stock.assert_called_once()
        mock_track.assert_not_called()

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

    # ===================================================================
    # Group H: 成交确认后写 trade_records
    # ===================================================================

    def test_h1_live_dynamic_sell_defers_trade_record_until_deal(self):
        """动态止盈卖出实盘委托提交后，不应立即写 trade_records"""
        executor = self._make_orderable_executor()
        executor._save_trade_record = MagicMock(return_value=True)

        old_sim = config.ENABLE_SIMULATION_MODE
        old_allow_sell = getattr(config, "ENABLE_ALLOW_SELL", True)
        try:
            config.ENABLE_SIMULATION_MODE = False
            config.ENABLE_ALLOW_SELL = True
            with patch("config.is_trade_time", return_value=True):
                order_id = executor.sell_stock(
                    "301560",
                    volume=600,
                    price=44.09,
                    strategy="auto_partial",
                    signal_type="take_profit_half",
                    signal_info={"volume": 600, "current_price": 44.09},
                )
        finally:
            config.ENABLE_SIMULATION_MODE = old_sim
            config.ENABLE_ALLOW_SELL = old_allow_sell

        self.assertEqual(order_id, 940572800)
        executor._save_trade_record.assert_not_called()
        executor.position_manager.track_order.assert_called_once()
        self.assertEqual(executor.order_cache[str(order_id)]["strategy"], "auto_partial")

    def test_h2_live_add_position_buy_defers_and_tracks_pending(self):
        """补仓买入实盘委托提交后，应等待成交确认写流水并登记 pending"""
        executor = self._make_orderable_executor()
        executor._save_trade_record = MagicMock(return_value=True)

        old_sim = config.ENABLE_SIMULATION_MODE
        old_allow_buy = getattr(config, "ENABLE_ALLOW_BUY", True)
        try:
            config.ENABLE_SIMULATION_MODE = False
            config.ENABLE_ALLOW_BUY = True
            with patch("config.is_trade_time", return_value=True):
                order_id = executor.buy_stock(
                    "301560",
                    amount=4409,
                    price=44.09,
                    strategy="add_position",
                    signal_type="add_position",
                    signal_info={"current_price": 44.09, "add_amount": 4409},
                )
        finally:
            config.ENABLE_SIMULATION_MODE = old_sim
            config.ENABLE_ALLOW_BUY = old_allow_buy

        self.assertEqual(order_id, 940572800)
        executor._save_trade_record.assert_not_called()
        executor.position_manager.track_order.assert_called_once()
        track_kwargs = executor.position_manager.track_order.call_args.kwargs
        self.assertEqual(track_kwargs["signal_type"], "add_position")
        self.assertEqual(track_kwargs["signal_info"]["order_side"], "BUY")
        self.assertEqual(track_kwargs["signal_info"]["volume"], 100)

    def test_h2a_sell_positive_seq_without_order_id_does_not_resubmit(self):
        """异步卖出返回正 seq 但无 order_id 回推时，必须停止重试并进入冷却。"""
        executor = self._make_orderable_executor()
        executor.position_manager._get_real_order_id.return_value = None
        executor.position_manager.qmt_trader.sell.return_value = 5209

        old_sim = config.ENABLE_SIMULATION_MODE
        old_allow_sell = getattr(config, "ENABLE_ALLOW_SELL", True)
        old_cooldown = config.ASYNC_ORDER_UNKNOWN_COOLDOWN_SECONDS
        try:
            config.ENABLE_SIMULATION_MODE = False
            config.ENABLE_ALLOW_SELL = True
            config.ASYNC_ORDER_UNKNOWN_COOLDOWN_SECONDS = 300
            with patch("config.is_trade_time", return_value=True):
                first = executor.sell_stock("301560", volume=100, price=44.09, strategy="grid")
                second = executor.sell_stock("301560", volume=100, price=44.09, strategy="grid")
        finally:
            config.ENABLE_SIMULATION_MODE = old_sim
            config.ENABLE_ALLOW_SELL = old_allow_sell
            config.ASYNC_ORDER_UNKNOWN_COOLDOWN_SECONDS = old_cooldown

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(
            executor.position_manager.qmt_trader.sell.call_count,
            1,
            "正 seq 未确认时，同一次和紧随其后的调用都不能重复提交卖单"
        )

    def test_h2b_buy_positive_seq_without_order_id_does_not_resubmit(self):
        """异步买入返回正 seq 但无 order_id 回推时，必须停止重试并进入冷却。"""
        executor = self._make_orderable_executor()
        executor.position_manager._get_real_order_id.return_value = None
        executor.position_manager.qmt_trader.buy.return_value = 6209

        old_sim = config.ENABLE_SIMULATION_MODE
        old_allow_buy = getattr(config, "ENABLE_ALLOW_BUY", True)
        old_cooldown = config.ASYNC_ORDER_UNKNOWN_COOLDOWN_SECONDS
        try:
            config.ENABLE_SIMULATION_MODE = False
            config.ENABLE_ALLOW_BUY = True
            config.ASYNC_ORDER_UNKNOWN_COOLDOWN_SECONDS = 300
            with patch("config.is_trade_time", return_value=True):
                first = executor.buy_stock("301560", volume=100, price=44.09, strategy="add_position")
                second = executor.buy_stock("301560", volume=100, price=44.09, strategy="add_position")
        finally:
            config.ENABLE_SIMULATION_MODE = old_sim
            config.ENABLE_ALLOW_BUY = old_allow_buy
            config.ASYNC_ORDER_UNKNOWN_COOLDOWN_SECONDS = old_cooldown

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(
            executor.position_manager.qmt_trader.buy.call_count,
            1,
            "正 seq 未确认时，同一次和紧随其后的调用都不能重复提交买单"
        )

    def test_h2c_unknown_submission_clears_after_order_id_map_arrives(self):
        """未知提交冷却期间若迟到的 seq->order_id 映射到达，应允许后续新请求。"""
        executor = self._make_orderable_executor()
        qmt_trader = executor.position_manager.qmt_trader
        qmt_trader.order_id_map = {5209: 940572811}
        executor._mark_unknown_order_submission(
            "301560.SZ", "SELL", 5209, 44.09, 100, "grid"
        )

        self.assertFalse(executor._has_recent_unknown_order_submission("301560.SZ", "SELL"))
        self.assertEqual(executor._unknown_order_submissions, {})

    def test_h2c2_unknown_submission_accepts_string_seq_mapping(self):
        """迟到映射只落在字符串seq键上时，也应解除未知委托保护并补齐订单缓存。"""
        executor = self._make_orderable_executor()
        qmt_trader = executor.position_manager.qmt_trader
        qmt_trader.order_id_map = {"5209": 940572812}
        executor._mark_unknown_order_submission(
            "301560.SZ", "SELL", 5209, 44.09, 100, "grid"
        )

        self.assertFalse(executor._has_recent_unknown_order_submission("301560.SZ", "SELL"))
        self.assertEqual(executor._unknown_order_submissions, {})
        self.assertIn("940572812", executor.order_cache)
        self.assertEqual(executor.order_cache["940572812"]["trade_type"], "SELL")

    def test_h2d_push_not_ready_blocks_live_order_submit(self):
        """实盘下单前若交易主推订阅不可用，应拒绝提交委托。"""
        executor = self._make_orderable_executor()
        qmt_trader = executor.position_manager.qmt_trader
        qmt_trader.ensure_trade_push_ready.return_value = False

        old_sim = config.ENABLE_SIMULATION_MODE
        old_allow_buy = getattr(config, "ENABLE_ALLOW_BUY", True)
        old_allow_sell = getattr(config, "ENABLE_ALLOW_SELL", True)
        try:
            config.ENABLE_SIMULATION_MODE = False
            config.ENABLE_ALLOW_BUY = True
            config.ENABLE_ALLOW_SELL = True
            with patch("config.is_trade_time", return_value=True):
                buy_order_id = executor.buy_stock("301560", volume=100, price=44.09)
                sell_order_id = executor.sell_stock("301560", volume=100, price=44.09)
        finally:
            config.ENABLE_SIMULATION_MODE = old_sim
            config.ENABLE_ALLOW_BUY = old_allow_buy
            config.ENABLE_ALLOW_SELL = old_allow_sell

        self.assertIsNone(buy_order_id)
        self.assertIsNone(sell_order_id)
        qmt_trader.buy.assert_not_called()
        qmt_trader.sell.assert_not_called()

    def test_h2e_get_real_order_id_falls_back_to_order_query(self):
        """seq回推未到时，应能从QMT委托列表反查真实order_id并回填映射。"""
        qmt_trader = MagicMock()
        qmt_trader.order_id_map = {}
        qmt_trader.acc = object()
        qmt_trader.xt_trader.query_stock_orders.return_value = [
            _FakeOrder(
                stock_code="000799.SZ",
                order_status=50,
                order_id=672137248,
                order_type=24,
                order_volume=100,
                order_time=int(time.time()),
                strategy_name="debug_live_sell_100",
                order_remark="auto_debug_live_sell_100",
            )
        ]
        self.pm.qmt_trader = qmt_trader

        old_sync = config.USE_SYNC_ORDER_API
        old_wait_timeout = config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS
        old_fallback_timeout = config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS
        try:
            config.USE_SYNC_ORDER_API = False
            config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS = 0.01
            config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS = 0
            order_id = self.pm._get_real_order_id(
                70,
                stock_code="000799.SZ",
                side="SELL",
                volume=100,
                strategy="debug_live_sell_100",
                order_remark="auto_debug_live_sell_100",
                submitted_at=time.time() - 1,
            )
        finally:
            config.USE_SYNC_ORDER_API = old_sync
            config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS = old_wait_timeout
            config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS = old_fallback_timeout

        self.assertEqual(order_id, 672137248)
        self.assertEqual(qmt_trader.order_id_map[70], 672137248)

    def test_h2e2_get_real_order_id_accepts_string_seq_map(self):
        """order_id_map 使用字符串seq作为键时，主程序也应直接命中。"""
        qmt_trader = MagicMock()
        qmt_trader.order_id_map = {"1277": 940572675}
        self.pm.qmt_trader = qmt_trader

        old_sync = config.USE_SYNC_ORDER_API
        old_wait_timeout = config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS
        try:
            config.USE_SYNC_ORDER_API = False
            config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS = 0.01
            order_id = self.pm._get_real_order_id(
                1277,
                stock_code="000799.SZ",
                side="SELL",
                volume=100,
                strategy="web_probe_sell-fill",
            )
        finally:
            config.USE_SYNC_ORDER_API = old_sync
            config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS = old_wait_timeout

        self.assertEqual(order_id, 940572675)

    def test_h2j_get_real_order_id_matches_active_info_hhmmss_time(self):
        """应优先使用英文活跃委托信息，并兼容QMT的HHMMSS报单时间。"""
        qmt_trader = MagicMock()
        qmt_trader.order_id_map = {}
        qmt_trader.acc = object()
        now = datetime.now()
        qmt_trader.get_active_order_info_by_stock.return_value = [{
            "stock_code": "000799.SZ",
            "order_status": 50,
            "order_id": 672137249,
            "order_type": 24,
            "order_volume": 100,
            "order_time": int(now.strftime("%H%M%S")),
            "strategy_name": "debug_live_sell_100",
            "order_remark": "auto_debug_live_sell_100",
        }]
        qmt_trader.xt_trader.query_stock_orders.return_value = []
        self.pm.qmt_trader = qmt_trader

        old_sync = config.USE_SYNC_ORDER_API
        old_wait_timeout = config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS
        old_fallback_timeout = config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS
        try:
            config.USE_SYNC_ORDER_API = False
            config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS = 0.01
            config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS = 0
            order_id = self.pm._get_real_order_id(
                42,
                stock_code="000799.SZ",
                side="SELL",
                volume=100,
                strategy="debug_live_sell_100",
                order_remark="auto_debug_live_sell_100",
                submitted_at=now.timestamp() - 1,
            )
        finally:
            config.USE_SYNC_ORDER_API = old_sync
            config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS = old_wait_timeout
            config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS = old_fallback_timeout

        self.assertEqual(order_id, 672137249)
        self.assertEqual(qmt_trader.order_id_map[42], 672137249)
        qmt_trader.get_active_order_info_by_stock.assert_called_with("000799")

    def test_h2k_get_real_order_id_matches_yyyymmddhhmmss_time_and_price(self):
        """应兼容QMT的YYYYMMDDHHMMSS报单时间，并允许价格最小跳动差异。"""
        qmt_trader = MagicMock()
        qmt_trader.order_id_map = {}
        qmt_trader.acc = object()
        submitted = datetime.now().replace(microsecond=0)
        qmt_trader.get_active_order_info_by_stock.return_value = []
        qmt_trader.xt_trader.query_stock_orders.return_value = [
            _FakeOrder(
                stock_code="000799.SZ",
                order_status=50,
                order_id=672137250,
                order_type=24,
                order_volume=100,
                order_time=int(submitted.strftime("%Y%m%d%H%M%S")),
                strategy_name="debug_live_sell_100",
                order_remark="auto_debug_live_sell_100",
            )
        ]
        qmt_trader.xt_trader.query_stock_orders.return_value[0].price = 45.59
        self.pm.qmt_trader = qmt_trader

        old_sync = config.USE_SYNC_ORDER_API
        old_wait_timeout = config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS
        old_fallback_timeout = config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS
        try:
            config.USE_SYNC_ORDER_API = False
            config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS = 0.01
            config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS = 0
            order_id = self.pm._get_real_order_id(
                41,
                stock_code="000799.SZ",
                side="SELL",
                volume=100,
                strategy="debug_live_sell_100",
                order_remark="auto_debug_live_sell_100",
                submitted_at=submitted.timestamp() - 1,
                price=45.60,
            )
        finally:
            config.USE_SYNC_ORDER_API = old_sync
            config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS = old_wait_timeout
            config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS = old_fallback_timeout

        self.assertEqual(order_id, 672137250)
        self.assertEqual(qmt_trader.order_id_map[41], 672137250)

    def test_h2l_get_real_order_id_does_not_require_remark_exact_match(self):
        """券商可能改写委托备注；强条件唯一命中时不应因备注差异丢失order_id。"""
        qmt_trader = MagicMock()
        qmt_trader.order_id_map = {}
        qmt_trader.acc = object()
        submitted = datetime.now().replace(microsecond=0)
        qmt_trader.get_active_order_info_by_stock.return_value = [{
            "stock_code": "000799.SZ",
            "order_status": 50,
            "order_id": 672137251,
            "order_type": 24,
            "order_volume": 100,
            "order_time": int(submitted.timestamp()),
            "strategy_name": "debug_live_sell_100",
            "order_remark": "broker_rewritten_remark",
            "price": 45.59,
        }]
        qmt_trader.xt_trader.query_stock_orders.return_value = []
        self.pm.qmt_trader = qmt_trader

        old_sync = config.USE_SYNC_ORDER_API
        old_wait_timeout = config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS
        old_fallback_timeout = config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS
        try:
            config.USE_SYNC_ORDER_API = False
            config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS = 0.01
            config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS = 0
            order_id = self.pm._get_real_order_id(
                33,
                stock_code="000799.SZ",
                side="SELL",
                volume=100,
                strategy="debug_live_sell_100",
                order_remark="auto_debug_live_sell_100",
                submitted_at=submitted.timestamp() - 1,
                price=45.60,
            )
        finally:
            config.USE_SYNC_ORDER_API = old_sync
            config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS = old_wait_timeout
            config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS = old_fallback_timeout

        self.assertEqual(order_id, 672137251)
        self.assertEqual(qmt_trader.order_id_map[33], 672137251)

    def test_h2l2_get_real_order_id_accepts_truncated_strategy_name(self):
        """券商若截断较长 strategy_name，唯一候选仍应能匹配真实 order_id。"""
        qmt_trader = MagicMock()
        qmt_trader.order_id_map = {}
        qmt_trader.acc = object()
        submitted = datetime.now().replace(microsecond=0)
        qmt_trader.get_active_order_info_by_stock.return_value = [{
            "stock_code": "000799.SZ",
            "order_status": 56,
            "order_id": 940572675,
            "order_type": 24,
            "order_volume": 100,
            "order_time": int(submitted.timestamp()),
            "strategy_name": "web_probe_sell-fill",
            "order_remark": "web_probe_sell-fill_178",
            "price": 41.54,
            "traded_volume": 100,
        }]
        qmt_trader.xt_trader.query_stock_orders.return_value = []
        self.pm.qmt_trader = qmt_trader

        old_sync = config.USE_SYNC_ORDER_API
        old_wait_timeout = config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS
        old_fallback_timeout = config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS
        try:
            config.USE_SYNC_ORDER_API = False
            config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS = 0.01
            config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS = 0
            order_id = self.pm._get_real_order_id(
                1277,
                stock_code="000799.SZ",
                side="SELL",
                volume=100,
                strategy="web_probe_sell-fill_debug_extra_suffix",
                order_remark="web_probe_sell-fill_1786425835",
                submitted_at=submitted.timestamp() - 1,
            )
        finally:
            config.USE_SYNC_ORDER_API = old_sync
            config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS = old_wait_timeout
            config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS = old_fallback_timeout

        self.assertEqual(order_id, 940572675)
        self.assertEqual(qmt_trader.order_id_map[1277], 940572675)

    def test_h2m_get_real_order_id_normalizes_float_order_type(self):
        """QMT/表格层可能把委托类型返回为24.0，应按卖出方向精确匹配。"""
        qmt_trader = MagicMock()
        qmt_trader.order_id_map = {}
        qmt_trader.acc = object()
        now = int(time.time())
        qmt_trader.get_active_order_info_by_stock.return_value = []
        qmt_trader.xt_trader.query_stock_orders.return_value = [
            _FakeOrder("000799.SZ", 50, 672137252, 23.0, 100, now, "debug_live_sell_100", "auto_debug_live_sell_100"),
            _FakeOrder("000799.SZ", 50, 672137253, 24.0, 100, now, "debug_live_sell_100", "auto_debug_live_sell_100"),
        ]
        self.pm.qmt_trader = qmt_trader

        old_sync = config.USE_SYNC_ORDER_API
        old_wait_timeout = config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS
        old_fallback_timeout = config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS
        try:
            config.USE_SYNC_ORDER_API = False
            config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS = 0.01
            config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS = 0
            order_id = self.pm._get_real_order_id(
                34,
                stock_code="000799.SZ",
                side="SELL",
                volume=100,
                strategy="debug_live_sell_100",
                order_remark="auto_debug_live_sell_100",
                submitted_at=time.time() - 1,
            )
        finally:
            config.USE_SYNC_ORDER_API = old_sync
            config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS = old_wait_timeout
            config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS = old_fallback_timeout

        self.assertEqual(order_id, 672137253)
        self.assertEqual(qmt_trader.order_id_map[34], 672137253)

    def test_h2f_get_real_order_id_query_fallback_rejects_ambiguous_matches(self):
        """委托列表反查到多个候选时必须保守失败，不能猜order_id。"""
        qmt_trader = MagicMock()
        qmt_trader.order_id_map = {}
        qmt_trader.acc = object()
        now = int(time.time())
        qmt_trader.xt_trader.query_stock_orders.return_value = [
            _FakeOrder("000799.SZ", 50, 672137248, 24, 100, now, "grid", "auto_grid"),
            _FakeOrder("000799.SZ", 50, 672137249, 24, 100, now, "grid", "auto_grid"),
        ]
        self.pm.qmt_trader = qmt_trader

        old_sync = config.USE_SYNC_ORDER_API
        old_wait_timeout = config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS
        old_fallback_timeout = config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS
        try:
            config.USE_SYNC_ORDER_API = False
            config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS = 0.01
            config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS = 0
            order_id = self.pm._get_real_order_id(
                70,
                stock_code="000799.SZ",
                side="SELL",
                volume=100,
                strategy="grid",
                order_remark="auto_grid",
                submitted_at=time.time() - 1,
            )
        finally:
            config.USE_SYNC_ORDER_API = old_sync
            config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS = old_wait_timeout
            config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS = old_fallback_timeout

        self.assertIsNone(order_id)
        self.assertNotIn(70, qmt_trader.order_id_map)

    def test_h2g_sell_stock_resolves_order_id_from_order_query_fallback(self):
        """卖出路径在callback映射缺失时，应通过委托列表拿到真实order_id且不重试。"""
        executor = self._make_live_executor()
        executor.position_manager = self.pm

        qmt_trader = MagicMock()
        qmt_trader.adjust_stock.side_effect = (
            lambda stock: stock if "." in stock else f"{stock}.SZ"
        )
        qmt_trader.check_stock_is_av_sell.return_value = True
        qmt_trader.ensure_trade_push_ready.return_value = True
        qmt_trader.sell.return_value = 70
        qmt_trader.order_id_map = {}
        qmt_trader.acc = object()
        qmt_trader.xt_trader.query_stock_orders.return_value = [
            _FakeOrder(
                stock_code="000799.SZ",
                order_status=50,
                order_id=672137248,
                order_type=24,
                order_volume=100,
                order_time=int(time.time()),
                strategy_name="debug_live_sell_100",
                order_remark="auto_debug_live_sell_100",
            )
        ]
        self.pm.qmt_trader = qmt_trader

        old_sim = config.ENABLE_SIMULATION_MODE
        old_allow_sell = getattr(config, "ENABLE_ALLOW_SELL", True)
        old_sync = config.USE_SYNC_ORDER_API
        old_wait_timeout = config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS
        old_fallback_timeout = config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS
        try:
            config.ENABLE_SIMULATION_MODE = False
            config.ENABLE_ALLOW_SELL = True
            config.USE_SYNC_ORDER_API = False
            config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS = 0.01
            config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS = 0
            with patch("config.is_trade_time", return_value=True):
                order_id = executor.sell_stock(
                    "000799",
                    volume=100,
                    price=41.50,
                    strategy="debug_live_sell_100",
                )
        finally:
            config.ENABLE_SIMULATION_MODE = old_sim
            config.ENABLE_ALLOW_SELL = old_allow_sell
            config.USE_SYNC_ORDER_API = old_sync
            config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS = old_wait_timeout
            config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS = old_fallback_timeout

        self.assertEqual(order_id, 672137248)
        qmt_trader.sell.assert_called_once()
        self.assertEqual(qmt_trader.order_id_map[70], 672137248)
        self.assertIn("672137248", executor.order_cache)

    def test_h2h_get_real_order_id_falls_back_to_dataframe_query(self):
        """原始QMT委托查询失败时，应能降级到包装查询返回的DataFrame反查order_id。"""
        qmt_trader = MagicMock()
        qmt_trader.order_id_map = {}
        qmt_trader.acc = object()
        qmt_trader.xt_trader.query_stock_orders.side_effect = RuntimeError("raw query failed")
        qmt_trader.query_stock_orders.return_value = pd.DataFrame([{
            "证券代码": "000799",
            "订单编号": 672137250,
            "报单时间": datetime.now(),
            "委托类型": 24,
            "委托数量": 100,
            "策略名称": "debug_live_sell_100",
            "委托备注": "auto_debug_live_sell_100",
        }])
        self.pm.qmt_trader = qmt_trader

        old_sync = config.USE_SYNC_ORDER_API
        old_wait_timeout = config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS
        old_fallback_timeout = config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS
        try:
            config.USE_SYNC_ORDER_API = False
            config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS = 0.01
            config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS = 0
            order_id = self.pm._get_real_order_id(
                71,
                stock_code="000799.SZ",
                side="SELL",
                volume=100,
                strategy="debug_live_sell_100",
                order_remark="auto_debug_live_sell_100",
                submitted_at=time.time() - 1,
            )
        finally:
            config.USE_SYNC_ORDER_API = old_sync
            config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS = old_wait_timeout
            config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS = old_fallback_timeout

        self.assertEqual(order_id, 672137250)
        self.assertEqual(qmt_trader.order_id_map[71], 672137250)

    def test_h2h2_get_real_order_id_falls_back_to_trade_query(self):
        """委托回推缺失且委托列表为空时，应能从今日成交反查真实order_id。"""
        qmt_trader = MagicMock()
        qmt_trader.order_id_map = {}
        qmt_trader.acc = object()
        qmt_trader.xt_trader.query_stock_orders.return_value = []
        qmt_trader.xt_trader.query_stock_trades.return_value = [
            _FakeTrade(
                order_id=672137251,
                stock_code="301218.SZ",
                traded_volume=1000,
                traded_price=47.08,
                order_type=24,
                traded_time=int(time.time()),
                strategy_name="auto_partial",
                order_remark="auto_auto_partial",
            )
        ]
        qmt_trader.query_stock_orders.return_value = pd.DataFrame()
        qmt_trader.query_stock_trades.return_value = pd.DataFrame()
        self.pm.qmt_trader = qmt_trader

        old_sync = config.USE_SYNC_ORDER_API
        old_wait_timeout = config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS
        old_fallback_timeout = config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS
        try:
            config.USE_SYNC_ORDER_API = False
            config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS = 0.01
            config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS = 0
            order_id = self.pm._get_real_order_id(
                10935,
                stock_code="301218.SZ",
                side="SELL",
                volume=1000,
                strategy="auto_partial",
                order_remark="auto_auto_partial",
                submitted_at=time.time() - 1,
                price=47.07,
            )
        finally:
            config.USE_SYNC_ORDER_API = old_sync
            config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS = old_wait_timeout
            config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS = old_fallback_timeout

        self.assertEqual(order_id, 672137251)
        self.assertEqual(qmt_trader.order_id_map[10935], 672137251)

    def test_h2h3_get_real_order_id_accepts_chinese_deal_fields(self):
        """XtQuantManager/包装查询返回中文成交字段时，也应能反查order_id。"""
        qmt_trader = MagicMock()
        qmt_trader.order_id_map = {}
        qmt_trader.acc = object()
        qmt_trader.xt_trader.query_stock_orders.return_value = []
        qmt_trader.xt_trader.query_stock_trades.return_value = []
        qmt_trader.query_stock_orders.return_value = pd.DataFrame()
        qmt_trader.query_stock_trades.return_value = pd.DataFrame([{
            "证券代码": "301218",
            "订单编号": 672137252,
            "成交时间": datetime.now(),
            "操作": "证券卖出",
            "成交数量": 1000,
            "成交均价": 47.08,
            "策略名称": "auto_partial",
            "委托备注": "auto_auto_partial",
        }])
        self.pm.qmt_trader = qmt_trader

        old_sync = config.USE_SYNC_ORDER_API
        old_wait_timeout = config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS
        old_fallback_timeout = config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS
        try:
            config.USE_SYNC_ORDER_API = False
            config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS = 0.01
            config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS = 0
            order_id = self.pm._get_real_order_id(
                10936,
                stock_code="301218.SZ",
                side="SELL",
                volume=1000,
                strategy="auto_partial",
                order_remark="auto_auto_partial",
                submitted_at=time.time() - 1,
                price=47.07,
            )
        finally:
            config.USE_SYNC_ORDER_API = old_sync
            config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS = old_wait_timeout
            config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS = old_fallback_timeout

        self.assertEqual(order_id, 672137252)
        self.assertEqual(qmt_trader.order_id_map[10936], 672137252)

    def test_h2h4_get_real_order_id_accepts_unique_partial_fills(self):
        """成交被拆成多笔时，只要唯一order_id可确认，也应完成seq映射。"""
        qmt_trader = MagicMock()
        qmt_trader.order_id_map = {}
        qmt_trader.acc = object()
        qmt_trader.xt_trader.query_stock_orders.return_value = []
        qmt_trader.xt_trader.query_stock_trades.return_value = [
            _FakeTrade(
                order_id=672137253,
                stock_code="301218.SZ",
                traded_volume=400,
                traded_price=47.08,
                order_type=24,
                traded_time=int(time.time()),
                strategy_name="auto_partial",
                order_remark="auto_auto_partial",
            ),
            _FakeTrade(
                order_id=672137253,
                stock_code="301218.SZ",
                traded_volume=600,
                traded_price=47.08,
                order_type=24,
                traded_time=int(time.time()),
                strategy_name="auto_partial",
                order_remark="auto_auto_partial",
            ),
        ]
        qmt_trader.query_stock_orders.return_value = pd.DataFrame()
        qmt_trader.query_stock_trades.return_value = pd.DataFrame()
        self.pm.qmt_trader = qmt_trader

        old_sync = config.USE_SYNC_ORDER_API
        old_wait_timeout = config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS
        old_fallback_timeout = config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS
        try:
            config.USE_SYNC_ORDER_API = False
            config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS = 0.01
            config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS = 0
            order_id = self.pm._get_real_order_id(
                10937,
                stock_code="301218.SZ",
                side="SELL",
                volume=1000,
                strategy="auto_partial",
                order_remark="auto_auto_partial",
                submitted_at=time.time() - 1,
                price=47.07,
            )
        finally:
            config.USE_SYNC_ORDER_API = old_sync
            config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS = old_wait_timeout
            config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS = old_fallback_timeout

        self.assertEqual(order_id, 672137253)
        self.assertEqual(qmt_trader.order_id_map[10937], 672137253)

    def test_h2i_sell_stock_stops_retry_when_order_query_fails(self):
        """委托反查接口异常时，卖出应标记未知提交并停止重试，避免重复实盘发单。"""
        executor = self._make_live_executor()
        executor.position_manager = self.pm

        qmt_trader = MagicMock()
        qmt_trader.adjust_stock.side_effect = (
            lambda stock: stock if "." in stock else f"{stock}.SZ"
        )
        qmt_trader.check_stock_is_av_sell.return_value = True
        qmt_trader.ensure_trade_push_ready.return_value = True
        qmt_trader.sell.return_value = 71
        qmt_trader.order_id_map = {}
        qmt_trader.acc = object()
        qmt_trader.xt_trader.query_stock_orders.side_effect = RuntimeError("raw query failed")
        qmt_trader.query_stock_orders.side_effect = RuntimeError("wrapper query failed")
        self.pm.qmt_trader = qmt_trader

        old_sim = config.ENABLE_SIMULATION_MODE
        old_allow_sell = getattr(config, "ENABLE_ALLOW_SELL", True)
        old_sync = config.USE_SYNC_ORDER_API
        old_wait_timeout = config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS
        old_fallback_timeout = config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS
        try:
            config.ENABLE_SIMULATION_MODE = False
            config.ENABLE_ALLOW_SELL = True
            config.USE_SYNC_ORDER_API = False
            config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS = 0.01
            config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS = 0
            with patch("config.is_trade_time", return_value=True):
                order_id = executor.sell_stock(
                    "000799",
                    volume=100,
                    price=41.50,
                    strategy="debug_live_sell_100",
                )
        finally:
            config.ENABLE_SIMULATION_MODE = old_sim
            config.ENABLE_ALLOW_SELL = old_allow_sell
            config.USE_SYNC_ORDER_API = old_sync
            config.ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS = old_wait_timeout
            config.ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS = old_fallback_timeout

        self.assertIsNone(order_id)
        qmt_trader.sell.assert_called_once()
        self.assertIn(("000799.SZ", "SELL"), executor._unknown_order_submissions)

    def test_h2i2_sell_stock_non_fixed_price_does_not_filter_order_query_by_price(self):
        """price_type=5 等非固定价委托，反查 order_id 时不应使用提交前价格做过滤。"""
        executor = self._make_orderable_executor()
        qmt_trader = executor.position_manager.qmt_trader
        qmt_trader.sell.return_value = 1277

        old_sim = config.ENABLE_SIMULATION_MODE
        old_allow_sell = getattr(config, "ENABLE_ALLOW_SELL", True)
        old_sync = config.USE_SYNC_ORDER_API
        try:
            config.ENABLE_SIMULATION_MODE = False
            config.ENABLE_ALLOW_SELL = True
            config.USE_SYNC_ORDER_API = False
            with patch("config.is_trade_time", return_value=True):
                order_id = executor.sell_stock(
                    "000799",
                    volume=100,
                    price=41.55,
                    price_type=5,
                    strategy="web_probe_sell-fill",
                )
        finally:
            config.ENABLE_SIMULATION_MODE = old_sim
            config.ENABLE_ALLOW_SELL = old_allow_sell
            config.USE_SYNC_ORDER_API = old_sync

        self.assertEqual(order_id, 940572800)
        _, kwargs = executor.position_manager._get_real_order_id.call_args
        self.assertIsNone(kwargs["price"])

    def test_h3_confirmed_dynamic_deal_writes_trade_record_once(self):
        """真实成交确认后才写 trade_records，同一成交号重复确认应幂等"""
        executor = self._make_live_executor()
        order_id = 940572801
        executor.order_cache[str(order_id)] = {
            "stock_code": "301560",
            "strategy": "auto_partial",
            "trade_type": "SELL",
            "price": 44.09,
            "volume": 600,
        }

        trade = _FakeTrade(
            order_id=order_id,
            stock_code="301560.SZ",
            traded_volume=600,
            traded_price=44.09,
            traded_id="DEAL_940572801",
            order_type=24,
        )

        self.assertTrue(executor.confirm_live_order_filled(order_id, deal_info=trade))
        self.assertTrue(executor.confirm_live_order_filled(order_id, deal_info=trade))

        rows = executor.conn.execute(
            "SELECT stock_code, trade_type, price, volume, trade_id, strategy FROM trade_records"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "301560")
        self.assertEqual(rows[0][1], "SELL")
        self.assertAlmostEqual(rows[0][2], 44.09)
        self.assertEqual(rows[0][3], 600)
        self.assertEqual(rows[0][4], "DEAL_940572801")
        self.assertEqual(rows[0][5], "auto_partial")

    def test_h3a_reused_trade_id_for_different_stock_is_not_skipped(self):
        """QMT 编号跨股票复用时，不应被全局 trade_id 去重误跳过。"""
        executor = self._make_live_executor()

        self.assertTrue(executor._save_trade_record(
            stock_code="002083.SZ",
            trade_time="2026-07-29 09:54:39",
            trade_type="BUY",
            price=10.67,
            volume=2300,
            amount=24541.0,
            trade_id="1477443585",
            commission=0.0,
            strategy="grid"
        ))
        self.assertTrue(executor._save_trade_record(
            stock_code="300454.SZ",
            trade_time="2026-08-05 10:11:15",
            trade_type="SELL",
            price=128.0,
            volume=100,
            amount=12800.0,
            trade_id="1477443585",
            commission=0.0,
            strategy="grid"
        ))

        rows = executor.conn.execute(
            "SELECT stock_code, trade_type, volume, trade_id FROM trade_records ORDER BY id"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "002083.SZ")
        self.assertEqual(rows[1][0], "300454.SZ")

    def test_h3aa_concurrent_duplicate_trade_record_writes_once(self):
        """同一成交回报并发重复到达时，只写入一条 trade_records。"""
        executor = self._make_live_executor()
        errors = []

        def write_once():
            try:
                executor._save_trade_record(
                    stock_code="300454.SZ",
                    trade_time="2026-08-05 10:14:05",
                    trade_type="SELL",
                    price=127.50,
                    volume=200,
                    amount=25500.0,
                    trade_id="74640105000030409438",
                    commission=0.0,
                    strategy="auto_partial"
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=write_once) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        count = executor.conn.execute("SELECT COUNT(*) FROM trade_records").fetchone()[0]
        self.assertEqual(count, 1)

    def test_h3ab_trade_record_time_is_saved_without_microseconds(self):
        """trade_records.trade_time 入库时应统一为秒级格式。"""
        executor = self._make_live_executor()

        self.assertTrue(executor._save_trade_record(
            stock_code="300454.SZ",
            trade_time="2026-08-07T09:45:28.310700",
            trade_type="BUY",
            price=120.71,
            volume=100,
            amount=12071.0,
            trade_id="GRID_MICROSECOND_TIME",
            commission=0.0,
            strategy="grid"
        ))

        row = executor.conn.execute(
            "SELECT trade_time FROM trade_records WHERE trade_id=?",
            ("GRID_MICROSECOND_TIME",)
        ).fetchone()
        self.assertEqual(row[0], "2026-08-07 09:45:28")
        self.assertNotIn(".", row[0])
        self.assertNotIn("T", row[0])

    def test_h3b_unmatched_live_deal_writes_external_trade_record(self):
        """非本机 pending 的实盘成交回报应按 external 补写流水，并保持幂等。"""
        executor = self._make_live_executor()
        trade = _FakeTrade(
            order_id=940572806,
            stock_code="301560.SZ",
            traded_volume=400,
            traded_price=44.20,
            traded_id="EXTERNAL_DEAL_940572806",
            order_type=24,
        )
        trade.m_nDirection = 48  # QMT 回报可能同时带 48 和 order_type=24，应以委托类型判定为卖出

        old_sim = config.ENABLE_SIMULATION_MODE
        try:
            config.ENABLE_SIMULATION_MODE = False
            with patch("trading_executor.get_trading_executor", return_value=executor), \
                    patch.object(self.pm, "_request_immediate_position_refresh"):
                self.pm._on_trade_callback(trade)
                self.pm._on_trade_callback(trade)
        finally:
            config.ENABLE_SIMULATION_MODE = old_sim

        rows = executor.conn.execute(
            "SELECT stock_code, trade_type, price, volume, trade_id, strategy FROM trade_records"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "301560")
        self.assertEqual(rows[0][1], "SELL")
        self.assertAlmostEqual(rows[0][2], 44.20)
        self.assertEqual(rows[0][3], 400)
        self.assertEqual(rows[0][4], "EXTERNAL_DEAL_940572806")
        self.assertEqual(rows[0][5], "external")

    def test_h3d_external_deal_record_does_not_query_qmt_position_for_stock_name(self):
        """外部成交补账不能为了股票名称回查 QMT 持仓，避免成交回调重入 xttrader。"""
        executor = self._make_live_executor()
        executor.data_manager = self._make_name_resolving_data_manager()
        qmt_trader = MagicMock()
        qmt_trader.position.return_value = pd.DataFrame([{
            "证券代码": "000799",
            "证券名称": "酒鬼酒",
        }])
        self.pm.qmt_trader = qmt_trader

        trade = _FakeTrade(
            order_id=1745879041,
            stock_code="000799.SZ",
            traded_volume=100,
            traded_price=52.30,
            traded_id="EXTERNAL_DEAL_1745879041",
            order_type=23,
        )

        old_sim = config.ENABLE_SIMULATION_MODE
        old_baostock = config.ENABLE_BAOSTOCK_STOCK_NAME_LOOKUP
        try:
            config.ENABLE_SIMULATION_MODE = False
            config.ENABLE_BAOSTOCK_STOCK_NAME_LOOKUP = False
            with patch("trading_executor.get_trading_executor", return_value=executor), \
                    patch("position_manager.get_position_manager", return_value=self.pm), \
                    patch.object(self.pm, "_request_immediate_position_refresh"):
                self.pm._on_trade_callback(trade)
        finally:
            config.ENABLE_SIMULATION_MODE = old_sim
            config.ENABLE_BAOSTOCK_STOCK_NAME_LOOKUP = old_baostock

        qmt_trader.position.assert_not_called()
        rows = executor.conn.execute(
            "SELECT stock_code, stock_name, trade_type, price, volume, strategy FROM trade_records"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "000799")
        self.assertEqual(rows[0][1], "000799")
        self.assertEqual(rows[0][2], "BUY")
        self.assertEqual(rows[0][5], "external")

    def test_h3e_external_deal_does_not_request_immediate_position_refresh(self):
        """外部成交回调不应立即调度持仓快刷，后续由监控线程正常同步。"""
        executor = self._make_live_executor()
        trade = _FakeTrade(
            order_id=1745879042,
            stock_code="000799.SZ",
            traded_volume=100,
            traded_price=52.30,
            traded_id="EXTERNAL_DEAL_1745879042",
            order_type=23,
        )

        old_sim = config.ENABLE_SIMULATION_MODE
        try:
            config.ENABLE_SIMULATION_MODE = False
            with patch("trading_executor.get_trading_executor", return_value=executor), \
                    patch.object(self.pm, "_request_immediate_position_refresh") as mock_refresh:
                self.pm._on_trade_callback(trade)
        finally:
            config.ENABLE_SIMULATION_MODE = old_sim

        mock_refresh.assert_not_called()

    def test_h3f_manual_order_placeholder_is_replaced_by_real_deals(self):
        """手动买入下单时预写的 ORDER_ 占位流水，应被真实成交回报取代而非重复记账。"""
        executor = self._make_live_executor()
        order_id = 403701763
        executor.order_cache[str(order_id)] = {
            "stock_code": "603757.SH",
            "strategy": "M_real",
            "trade_type": "BUY",
            "price": 69.67,
            "volume": 1100,
        }

        # 下单成功时按卖三价预写的占位流水（M_real 不在延迟记账白名单内）
        self.assertTrue(executor._save_trade_record(
            stock_code="603757.SH",
            trade_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            trade_type="BUY",
            price=69.67,
            volume=1100,
            amount=69.67 * 1100,
            trade_id=f"ORDER_{order_id}",
            commission=0.0,
            strategy="M_real",
        ))

        # 同一委托分两笔成交回报
        for traded_volume, traded_id in ((1000, "DEAL_1"), (100, "DEAL_2")):
            trade = _FakeTrade(
                order_id=order_id,
                stock_code="603757.SH",
                traded_volume=traded_volume,
                traded_price=69.64,
                traded_id=traded_id,
                order_type=23,
            )
            self.assertTrue(executor.confirm_live_order_filled(order_id, deal_info=trade))

        rows = executor.conn.execute(
            "SELECT trade_id, trade_type, volume FROM trade_records ORDER BY id"
        ).fetchall()
        self.assertEqual([row[0] for row in rows], ["DEAL_1", "DEAL_2"])
        self.assertEqual({row[1] for row in rows}, {"BUY"})
        self.assertEqual(sum(row[2] for row in rows), 1100)

    def test_h3g_placeholder_cleanup_keeps_history_with_reused_order_id(self):
        """QMT order_id 跨日复用，清理占位流水不得误删历史同名 ORDER_ 记录。"""
        executor = self._make_live_executor()
        order_id = 1209008130

        # 历史流水：同一 order_id 在更早的日期/别的股票上被复用过
        self.assertTrue(executor._save_trade_record(
            stock_code="002674",
            trade_time="2026-06-04 09:51:33",
            trade_type="SELL",
            price=14.16,
            volume=3200,
            amount=14.16 * 3200,
            trade_id=f"ORDER_{order_id}",
            commission=0.0,
            strategy="stop_loss",
        ))

        executor.order_cache[str(order_id)] = {
            "stock_code": "300122.SZ",
            "strategy": "M_real",
            "trade_type": "BUY",
            "price": 14.26,
            "volume": 5600,
        }
        self.assertTrue(executor._save_trade_record(
            stock_code="300122.SZ",
            trade_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            trade_type="BUY",
            price=14.26,
            volume=5600,
            amount=14.26 * 5600,
            trade_id=f"ORDER_{order_id}",
            commission=0.0,
            strategy="M_real",
        ))

        trade = _FakeTrade(
            order_id=order_id,
            stock_code="300122.SZ",
            traded_volume=5600,
            traded_price=14.23,
            traded_id="DEAL_300122",
            order_type=23,
        )
        self.assertTrue(executor.confirm_live_order_filled(order_id, deal_info=trade))

        rows = executor.conn.execute(
            "SELECT stock_code, trade_time, volume, trade_id FROM trade_records ORDER BY id"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        # 历史记录原样保留
        self.assertEqual(rows[0][0], "002674")
        self.assertEqual(rows[0][3], f"ORDER_{order_id}")
        # 当天占位记录被真实成交取代
        self.assertEqual(rows[1][0], "300122")
        self.assertEqual(rows[1][2], 5600)
        self.assertEqual(rows[1][3], "DEAL_300122")

    def test_h3c_grid_handled_deal_does_not_write_external_record(self):
        """网格管理器已接住的成交回报不应额外补写 external。"""
        executor = self._make_live_executor()
        self.pm.grid_manager = MagicMock()
        self.pm.grid_manager.handle_deal_callback.return_value = True
        trade = _FakeTrade(
            order_id=940572807,
            stock_code="301560.SZ",
            traded_volume=400,
            traded_price=44.20,
            traded_id="GRID_DEAL_940572807",
            order_type=24,
        )

        old_sim = config.ENABLE_SIMULATION_MODE
        old_grid = config.ENABLE_GRID_TRADING
        try:
            config.ENABLE_SIMULATION_MODE = False
            config.ENABLE_GRID_TRADING = True
            with patch("trading_executor.get_trading_executor", return_value=executor), \
                    patch.object(self.pm, "_request_immediate_position_refresh"):
                self.pm._on_trade_callback(trade)
        finally:
            config.ENABLE_SIMULATION_MODE = old_sim
            config.ENABLE_GRID_TRADING = old_grid

        count = executor.conn.execute("SELECT COUNT(*) FROM trade_records").fetchone()[0]
        self.assertEqual(count, 0)

    def test_h4_confirm_filled_from_query_accepts_dataframe_dict_and_object(self):
        """成交兜底查询应兼容 easy/IPC/RPC DataFrame、list[dict] 和对象记录"""
        cases = [
            pd.DataFrame([{
                "订单编号": 940572802,
                "证券代码": "301560",
                "成交编号": "DF_DEAL",
                "成交均价": 44.10,
                "成交数量": 600,
                "委托类型": 24,
            }]),
            [{
                "订单编号": 940572803,
                "证券代码": "301560",
                "成交编号": "DICT_DEAL",
                "成交均价": 44.11,
                "成交数量": 600,
                "委托类型": 24,
            }],
            [type("TradeObj", (), {
                "order_id": 940572804,
                "stock_code": "301560.SZ",
                "traded_id": "OBJ_DEAL",
                "traded_price": 44.12,
                "traded_volume": 600,
                "order_type": 24,
            })()],
        ]

        for records in cases:
            executor = self._make_live_executor()
            order_id = executor._field_any(records[0] if not hasattr(records, "iloc") else records.iloc[0],
                                           ["订单编号", "order_id"])
            executor.query_stock_trades = MagicMock(return_value=records)
            executor.order_cache[str(order_id)] = {
                "stock_code": "301560",
                "strategy": "auto_full",
                "trade_type": "SELL",
                "price": 44.0,
                "volume": 600,
            }

            self.assertTrue(executor.confirm_live_order_filled(order_id))
            count = executor.conn.execute("SELECT COUNT(*) FROM trade_records").fetchone()[0]
            self.assertEqual(count, 1)

    def test_h5_query_order_status_supports_dataframe_orders_without_xt_trader(self):
        """XtQuantManager/HTTP 客户端无 xt_trader 时，应能从 DataFrame 委托列表查状态"""
        qmt_trader = MagicMock()
        qmt_trader.xt_trader = None
        qmt_trader.query_stock_orders.return_value = pd.DataFrame([{
            "订单编号": 940572805,
            "证券代码": "301560",
            "委托状态": 56,
        }])
        self.pm.qmt_trader = qmt_trader
        self.pm.qmt_connected = True

        status = self.pm._query_order_status("301560", "940572805")
        self.assertEqual(status, 56)

    def test_i1_reconnect_does_not_block_forever_when_old_trader_stop_hangs(self):
        """重连清理旧 xttrader 时应有超时保护，不能被 stop() 永久卡住。"""
        class BlockingOldTrader:
            def stop(self):
                time.sleep(0.3)

        class NewTrader:
            def register_callback(self, callback):
                self.callback = callback

            def start(self):
                pass

            def connect(self):
                return 0

            def subscribe(self, account):
                return 0

        trader = easy_qmt_trader(path="dummy", account="TEST_ACCOUNT")
        trader.xt_trader = BlockingOldTrader()

        old_timeout = getattr(config, "QMT_STOP_TIMEOUT", None)
        try:
            config.QMT_STOP_TIMEOUT = 0.05
            with self._patch_xtquant_trader(NewTrader()):
                start = time.time()
                result = trader.connect()
                elapsed = time.time() - start
        finally:
            if old_timeout is None:
                delattr(config, "QMT_STOP_TIMEOUT")
            else:
                config.QMT_STOP_TIMEOUT = old_timeout

        self.assertIsNotNone(result)
        self.assertLess(elapsed, 0.2, "旧 trader.stop() 卡住时 connect() 也不应长时间阻塞")

    # ── I2~I5: 重连后旧 callback 不得干扰新连接（P0-2 回归） ────────────────
    #
    # 缺陷场景：connect() 每次创建全新 callback，但旧 XtQuantTrader 仍持有旧 callback。
    # 若 stop() 超时（daemon 线程无法被杀），旧 trader 及其 callback 继续存活，
    # 底层延迟触发 on_disconnected → 把新连接刚设好的 qmt_connected 错误置回 False，
    # 并清零重连冷却，引发不必要的 stop/connect 周期（可级联）。

    class _HangingOldTrader:
        """stop() 卡死的旧 trader，用于复现 detach 时机问题。"""
        def __init__(self, callback=None):
            self.callback = callback

        def stop(self):
            time.sleep(0.3)

    class _StubNewTrader:
        def register_callback(self, callback):
            self.callback = callback

        def start(self):
            pass

        def connect(self):
            return 0

        def subscribe(self, account):
            return 0

    def _make_trader_with_old_callback(self):
        """构造一个已有旧 callback 的 easy_qmt_trader，返回 (trader, old_callback)。"""
        trader = easy_qmt_trader(path="dummy", account="TEST_ACCOUNT")
        old_callback = MyXtQuantTraderCallback({})
        old_trader = self._HangingOldTrader(callback=old_callback)
        trader.xt_trader = old_trader
        trader._callback = old_callback
        return trader, old_callback

    def _patch_xtquant_trader(self, stub):
        """
        在 easy_qmt_trader 真实模块的 globals 上替换 XtQuantTrader。

        不能用 patch("easy_qmt_trader.XtQuantTrader")：其他测试
        （test_qmt_ipc_position_manager_integration）会把 sys.modules["easy_qmt_trader"]
        永久替换为 stub 模块，按模块名 patch 会打到那个 stub 上，真实模块仍用真
        XtQuantTrader，导致 connect() 真去连 QMT 并返回 -1（全量跑时偶发失败）。
        通过 connect.__globals__ 拿到真实模块的 globals，可绕过 sys.modules 污染。
        """
        return patch.dict(
            easy_qmt_trader.connect.__globals__,
            {"XtQuantTrader": lambda *a, **kw: stub}
        )

    def test_i2_stale_callback_disconnect_does_not_clobber_new_connection(self):
        """核心回归：旧 callback 延迟触发 on_disconnected 不得影响新连接状态。"""
        trader, old_callback = self._make_trader_with_old_callback()

        # 模拟 PositionManager 在旧连接上注册的断连回调
        state = {"qmt_connected": True, "reconnect_cooldown": 999.0}

        def _on_disconnect():
            state["qmt_connected"] = False
            state["reconnect_cooldown"] = 0.0

        old_callback.disconnect_callbacks.append(_on_disconnect)

        old_timeout = getattr(config, "QMT_STOP_TIMEOUT", None)
        try:
            config.QMT_STOP_TIMEOUT = 0.05  # 强制 stop() 超时，旧 trader 存活
            with self._patch_xtquant_trader(self._StubNewTrader()):
                self.assertIsNotNone(trader.connect(), "重连应成功")
        finally:
            if old_timeout is None:
                delattr(config, "QMT_STOP_TIMEOUT")
            else:
                config.QMT_STOP_TIMEOUT = old_timeout

        # 新连接已建立后，旧 trader 底层延迟触发断连推送
        old_callback.on_disconnected()

        self.assertTrue(
            state["qmt_connected"],
            "旧 callback 的延迟断连推送不得把新连接标记为断连"
        )
        self.assertEqual(
            state["reconnect_cooldown"], 999.0,
            "旧 callback 的延迟断连推送不得清零重连冷却（否则引发重连风暴）"
        )

    def test_i3_detach_marks_callback_and_clears_state_callbacks(self):
        """detach() 应置失效标记并清空「连接状态类」回调列表。"""
        callback = MyXtQuantTraderCallback({})
        callback.disconnect_callbacks.append(lambda: None)
        callback.order_callbacks.append(lambda o: None)
        self.assertFalse(callback.detached, "初始状态不应为 detached")

        callback.detach()

        self.assertTrue(callback.detached)
        self.assertEqual(callback.disconnect_callbacks, [])
        self.assertEqual(callback.order_callbacks, [])

    def test_i4_detached_callback_ignores_disconnect_and_order_pushes(self):
        """即使回调列表被重新填充，detached 标记也必须拦住状态类推送（竞态兜底）。"""
        callback = MyXtQuantTraderCallback({})
        callback.detach()

        # 模拟「detach 与底层推送并发」——列表在 detach 后又被写入
        hits = []
        callback.disconnect_callbacks.append(lambda: hits.append("disconnect"))
        callback.order_callbacks.append(lambda o: hits.append("order"))

        stale_order = _FakeOrder(stock_code="600000.SH", order_status=56, order_id=1)
        stale_order.order_sysid = "SYS_1"  # on_stock_order 日志需要该字段
        callback.on_disconnected()
        callback.on_stock_order(stale_order)

        self.assertEqual(
            hits, [],
            "detached callback 必须忽略断连/委托推送，标记优先于列表内容"
        )

    def test_i5_detached_callback_still_forwards_real_deals(self):
        """刻意保留：成交回报是真实资金变动，迟到仍须转发（落库层按 trade_id 幂等）。"""
        callback = MyXtQuantTraderCallback({})
        received = []
        callback.trade_callbacks.append(lambda t: received.append(t.order_id))

        callback.detach()
        callback.on_stock_trade(_FakeTrade(stock_code="600000.SH", order_id=12345))

        self.assertEqual(
            received, [12345],
            "detach 不应丢弃真实成交回报，否则可能永久丢失一笔成交流水"
        )

    def test_i6_connect_detaches_old_callback_before_stopping_old_trader(self):
        """detach 必须发生在 stop() 之前，否则 stop 卡住期间窗口仍然敞开。"""
        trader = easy_qmt_trader(path="dummy", account="TEST_ACCOUNT")
        old_callback = MyXtQuantTraderCallback({})
        order_of_events = []

        original_detach = old_callback.detach

        def _tracked_detach():
            order_of_events.append("detach")
            original_detach()

        old_callback.detach = _tracked_detach

        class _RecordingOldTrader:
            def __init__(self, callback):
                self.callback = callback

            def stop(self):
                order_of_events.append("stop")

        trader.xt_trader = _RecordingOldTrader(old_callback)
        trader._callback = old_callback

        with self._patch_xtquant_trader(self._StubNewTrader()):
            trader.connect()

        self.assertEqual(
            order_of_events, ["detach", "stop"],
            "必须先 detach 旧 callback 再 stop 旧 trader"
        )


    def test_i7_connect_enables_relaxed_response_order_before_register_callback(self):
        """支持 relaxed response order 的 xtquant 版本，应在注册 callback 前开启该调度。"""
        class _RelaxedStubTrader:
            def __init__(self):
                self.events = []
                self.callback = None

            def set_relaxed_response_order_enabled(self, enabled):
                self.events.append(("relaxed", enabled))

            def register_callback(self, callback):
                self.events.append(("register_callback", True))
                self.callback = callback

            def start(self):
                self.events.append(("start", True))

            def connect(self):
                self.events.append(("connect", True))
                return 0

            def subscribe(self, account):
                self.events.append(("subscribe", True))
                return 0

        stub = _RelaxedStubTrader()
        trader = easy_qmt_trader(path="dummy", account="TEST_ACCOUNT")

        with self._patch_xtquant_trader(stub):
            self.assertIsNotNone(trader.connect())

        self.assertEqual(
            stub.events[:2],
            [("relaxed", True), ("register_callback", True)],
            "relaxed response order 必须早于 callback 注册，避免异步下单响应继续被普通推送队列阻塞"
        )

    def test_i8_ensure_trade_push_ready_enables_relaxed_response_order(self):
        """保留现有连接时，确认主推通道也应顺手开启 relaxed response order。"""
        class _RelaxedPushTrader:
            def __init__(self):
                self.relaxed_calls = []
                self.registered_callback = None

            def set_relaxed_response_order_enabled(self, enabled):
                self.relaxed_calls.append(enabled)

            def register_callback(self, callback):
                self.registered_callback = callback

            def subscribe(self, account):
                return 0

        trader = easy_qmt_trader(path="dummy", account="TEST_ACCOUNT")
        trader.xt_trader = _RelaxedPushTrader()
        trader.acc = object()
        trader._callback = MyXtQuantTraderCallback(trader.order_id_map)

        self.assertTrue(trader.ensure_trade_push_ready())
        self.assertEqual(trader.xt_trader.relaxed_calls, [True])

    def test_i9_relaxed_response_order_failure_does_not_block_connect(self):
        """xtquant 开启 relaxed response order 抛错时，连接流程应继续走默认回调模式。"""
        class _FailingRelaxedStubTrader(self._StubNewTrader):
            def set_relaxed_response_order_enabled(self, enabled):
                raise RuntimeError("mock relaxed failure")

        trader = easy_qmt_trader(path="dummy", account="TEST_ACCOUNT")

        with self._patch_xtquant_trader(_FailingRelaxedStubTrader()):
            self.assertIsNotNone(trader.connect())


if __name__ == "__main__":
    unittest.main(verbosity=2)
