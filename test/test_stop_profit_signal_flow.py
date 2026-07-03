"""
动态止盈止损专项测试 - 信号检测与校验路径覆盖

覆盖点：
1) check_trading_signals 主要分支（止损/首次止盈/回撤止盈/动态全仓止盈）
2) 异常数据与边界条件（成本价无效、最高价异常、止损异常）
3) validate_trading_signal 校验路径
"""

import sys
import os
import time
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

# 添加父目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from test.test_base import TestBase
from position_manager import PositionManager
from strategy import TradingStrategy
from logger import get_logger

logger = get_logger("test_stop_profit_signal_flow")


class TestStopProfitSignalFlow(TestBase):
    """动态止盈止损信号检测与校验专项测试"""

    def setUp(self):
        super().setUp()
        self.pm = PositionManager()
        # 停止同步线程，避免后台影响测试
        self.pm.stop_sync_thread()

        # 确保内存表包含所需列（DB→内存同步可能缺字段）
        self._ensure_memory_schema()

        # 清理内存持仓，保证用例隔离
        cursor = self.pm.memory_conn.cursor()
        cursor.execute("DELETE FROM positions")
        self.pm.memory_conn.commit()

    def tearDown(self):
        try:
            self.pm.stop_sync_thread()
            self.pm.memory_conn.close()
        finally:
            super().tearDown()

    def _insert_position(self, **kwargs):
        """插入内存持仓（最小字段集）"""
        stock_code = kwargs.get("stock_code", "000001.SZ")
        volume = kwargs.get("volume", 1000)
        available = kwargs.get("available", 1000)
        cost_price = kwargs.get("cost_price", 10.0)
        current_price = kwargs.get("current_price", 10.0)
        profit_triggered = kwargs.get("profit_triggered", 0)
        highest_price = kwargs.get("highest_price", cost_price)
        stop_loss_price = kwargs.get("stop_loss_price", cost_price * (1 + config.STOP_LOSS_RATIO))
        profit_breakout_triggered = kwargs.get("profit_breakout_triggered", 0)
        breakout_highest_price = kwargs.get("breakout_highest_price", 0.0)
        open_date = kwargs.get("open_date", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        cursor = self.pm.memory_conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO positions
            (stock_code, volume, available, cost_price, current_price,
             open_date, profit_triggered, highest_price, stop_loss_price,
             profit_breakout_triggered, breakout_highest_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            stock_code, volume, available, cost_price, current_price,
            open_date, profit_triggered, highest_price, stop_loss_price,
            profit_breakout_triggered, breakout_highest_price
        ))
        self.pm.memory_conn.commit()
        return stock_code

    def _ensure_memory_schema(self):
        """确保内存positions表包含止盈突破相关字段"""
        cursor = self.pm.memory_conn.cursor()
        cursor.execute("PRAGMA table_info(positions)")
        cols = {row[1] for row in cursor.fetchall()}

        if "profit_breakout_triggered" not in cols:
            cursor.execute("ALTER TABLE positions ADD COLUMN profit_breakout_triggered BOOLEAN DEFAULT FALSE")
        if "breakout_highest_price" not in cols:
            cursor.execute("ALTER TABLE positions ADD COLUMN breakout_highest_price REAL")
        self.pm.memory_conn.commit()

    def test_no_position_returns_none(self):
        signal_type, signal_info = self.pm.check_trading_signals("000001.SZ", current_price=10.0)
        self.assertIsNone(signal_type)
        self.assertIsNone(signal_info)

    def test_position_cleared_returns_none(self):
        stock_code = self._insert_position(volume=0, available=0)
        signal_type, _ = self.pm.check_trading_signals(stock_code, current_price=10.0)
        self.assertIsNone(signal_type)

    def test_cost_price_invalid_returns_none(self):
        stock_code = self._insert_position(cost_price=0.0, current_price=10.0)
        signal_type, _ = self.pm.check_trading_signals(stock_code, current_price=10.0)
        self.assertIsNone(signal_type)

    def test_stop_loss_trigger_validated(self):
        cost_price = 10.0
        current_price = cost_price * (1 + config.STOP_LOSS_RATIO)  # 达到止损价
        stock_code = self._insert_position(
            cost_price=cost_price,
            current_price=current_price,
            stop_loss_price=0.0,  # 触发安全止损价重算
            profit_triggered=0
        )
        signal_type, info = self.pm.check_trading_signals(stock_code, current_price=current_price)
        self.assertEqual(signal_type, "stop_loss")
        self.assertEqual(info.get("reason"), "stop_loss_0")

    def test_stop_loss_reject_small_loss(self):
        # current_price <= stop_loss_price 但亏损比例不足
        cost_price = 10.0
        stock_code = self._insert_position(
            cost_price=cost_price,
            current_price=9.90,
            stop_loss_price=9.95,  # 合理范围内，不触发安全重算
            profit_triggered=0
        )
        signal_type, _ = self.pm.check_trading_signals(stock_code, current_price=9.90)
        self.assertIsNone(signal_type, "亏损比例过小应拒绝止损信号")

    def test_dynamic_stop_profit_disabled(self):
        stock_code = self._insert_position(cost_price=10.0, current_price=10.6, profit_triggered=0)
        old_flag = config.ENABLE_DYNAMIC_STOP_PROFIT
        try:
            config.ENABLE_DYNAMIC_STOP_PROFIT = False
            signal_type, _ = self.pm.check_trading_signals(stock_code, current_price=10.6)
            self.assertIsNone(signal_type)
        finally:
            config.ENABLE_DYNAMIC_STOP_PROFIT = old_flag

    def test_breakout_mark_only(self):
        cost_price = 10.0
        current_price = cost_price * (1 + config.INITIAL_TAKE_PROFIT_RATIO + 0.01)
        stock_code = self._insert_position(cost_price=cost_price, current_price=current_price, profit_triggered=0,
                                           profit_breakout_triggered=0, breakout_highest_price=0.0)
        with patch.object(self.pm, "_mark_profit_breakout", return_value=True) as mock_mark:
            signal_type, _ = self.pm.check_trading_signals(stock_code, current_price=current_price)
            self.assertIsNone(signal_type)
            self.assertTrue(mock_mark.called, "应标记突破状态")

    def test_fast_rally_waits_for_pullback_before_half_take_profit(self):
        """
        设计目标回归：股价快速突破半仓止盈点后不立即卖出；
        后续继续冲高时只抬高突破后最高价，小幅回撤不卖，达到配置回撤幅度才半仓止盈。
        """
        cost_price = 10.0
        stock_code = self._insert_position(
            cost_price=cost_price,
            current_price=cost_price,
            profit_triggered=0,
            profit_breakout_triggered=0,
            breakout_highest_price=0.0,
            volume=1000,
            available=1000,
        )

        # 第一次快速突破首次止盈阈值：只记录突破状态，不立即卖出。
        breakout_price = cost_price * (1 + config.INITIAL_TAKE_PROFIT_RATIO + 0.02)
        signal_type, signal_info = self.pm.check_trading_signals(stock_code, current_price=breakout_price)
        self.assertIsNone(signal_type)
        self.assertIsNone(signal_info)

        cursor = self.pm.memory_conn.cursor()
        cursor.execute(
            "SELECT profit_breakout_triggered, breakout_highest_price FROM positions WHERE stock_code=?",
            (stock_code,)
        )
        row = cursor.fetchone()
        self.assertTrue(bool(row[0]), "首次突破后应进入等待回撤状态")
        self.assertAlmostEqual(float(row[1]), breakout_price, places=4)

        # 继续快速上涨：只更新突破后最高价，仍不卖。
        rally_high = breakout_price * 1.08
        signal_type, signal_info = self.pm.check_trading_signals(stock_code, current_price=rally_high)
        self.assertIsNone(signal_type)
        self.assertIsNone(signal_info)

        cursor.execute(
            "SELECT breakout_highest_price FROM positions WHERE stock_code=?",
            (stock_code,)
        )
        self.assertAlmostEqual(float(cursor.fetchone()[0]), rally_high, places=4)

        # 小幅回撤但未达到配置回撤阈值：继续等待，不半仓止盈。
        shallow_pullback_price = rally_high * (1 - config.INITIAL_TAKE_PROFIT_PULLBACK_RATIO / 2)
        signal_type, signal_info = self.pm.check_trading_signals(stock_code, current_price=shallow_pullback_price)
        self.assertIsNone(signal_type)
        self.assertIsNone(signal_info)

        # 达到配置回撤阈值：才触发半仓止盈信号，使用冲高后的最高价作为回撤锚点。
        valid_pullback_price = rally_high * (1 - config.INITIAL_TAKE_PROFIT_PULLBACK_RATIO - 0.001)
        signal_type, signal_info = self.pm.check_trading_signals(stock_code, current_price=valid_pullback_price)
        self.assertEqual(signal_type, "take_profit_half")
        self.assertIsNotNone(signal_info)
        self.assertAlmostEqual(signal_info["breakout_highest_price"], rally_high, places=4)
        self.assertGreaterEqual(
            signal_info["pullback_ratio"],
            config.INITIAL_TAKE_PROFIT_PULLBACK_RATIO,
        )
        self.assertEqual(signal_info["sell_ratio"], config.INITIAL_TAKE_PROFIT_RATIO_PERCENTAGE)

    def test_breakout_update_and_pullback_trigger(self):
        # 已突破，回撤触发首次止盈
        stock_code = self._insert_position(cost_price=10.0, current_price=10.94, profit_triggered=0,
                                           profit_breakout_triggered=1, breakout_highest_price=11.0)
        signal_type, info = self.pm.check_trading_signals(stock_code, current_price=10.94)
        self.assertEqual(signal_type, "take_profit_half")
        self.assertIn("pullback_ratio", info)
        self.assertIn("sell_ratio", info)

    def test_stale_pullback_below_initial_floor_resets_breakout(self):
        """
        生产回归：当日买入后曾突破首次止盈，但 available=0 无法卖出；
        次日若低开到首次止盈有效价以下，不应继续执行昨天的回撤止盈。
        """
        old_ratio = config.INITIAL_TAKE_PROFIT_RATIO
        old_pullback = config.INITIAL_TAKE_PROFIT_PULLBACK_RATIO
        try:
            config.INITIAL_TAKE_PROFIT_RATIO = 0.055
            config.INITIAL_TAKE_PROFIT_PULLBACK_RATIO = 0.005

            stock_code = self._insert_position(
                stock_code="300930.SZ",
                cost_price=35.40,
                current_price=36.00,
                profit_triggered=0,
                profit_breakout_triggered=1,
                breakout_highest_price=37.38,
                volume=1400,
                available=1400,
            )

            signal_type, signal_info = self.pm.check_trading_signals(
                stock_code, current_price=36.00
            )

            self.assertIsNone(signal_type)
            self.assertIsNone(signal_info)

            cursor = self.pm.memory_conn.cursor()
            cursor.execute(
                "SELECT profit_breakout_triggered, breakout_highest_price FROM positions WHERE stock_code=?",
                (stock_code,)
            )
            row = cursor.fetchone()
            self.assertFalse(bool(row[0]), "低于首次止盈有效价后应清除突破状态")
            self.assertEqual(float(row[1]), 0.0)
        finally:
            config.INITIAL_TAKE_PROFIT_RATIO = old_ratio
            config.INITIAL_TAKE_PROFIT_PULLBACK_RATIO = old_pullback

    def test_pullback_with_available_zero_does_not_emit_half_signal(self):
        """
        当日买入 T+1/T+0 可卖受限时，信号检测层不应持续吐出可执行半仓止盈信号。
        这能减少策略层重试噪音，并避免跨日陈旧信号被执行。
        """
        stock_code = self._insert_position(
            cost_price=10.0,
            current_price=10.94,
            profit_triggered=0,
            profit_breakout_triggered=1,
            breakout_highest_price=11.0,
            volume=1000,
            available=0,
        )

        signal_type, signal_info = self.pm.check_trading_signals(stock_code, current_price=10.94)

        self.assertIsNone(signal_type)
        self.assertIsNone(signal_info)

    def test_validate_take_profit_half_rejects_below_initial_floor(self):
        """最终执行校验也要拒绝低于首次止盈有效价的半仓止盈信号。"""
        old_ratio = config.INITIAL_TAKE_PROFIT_RATIO
        old_pullback = config.INITIAL_TAKE_PROFIT_PULLBACK_RATIO
        try:
            config.INITIAL_TAKE_PROFIT_RATIO = 0.055
            config.INITIAL_TAKE_PROFIT_PULLBACK_RATIO = 0.005

            stock_code = self._insert_position(
                stock_code="300930.SZ",
                cost_price=35.40,
                current_price=36.00,
                profit_triggered=0,
                profit_breakout_triggered=1,
                breakout_highest_price=37.38,
                volume=1400,
                available=1400,
            )

            ok = self.pm.validate_trading_signal(stock_code, "take_profit_half", {
                "current_price": 36.00,
                "cost_price": 35.40,
            })

            self.assertFalse(ok)
        finally:
            config.INITIAL_TAKE_PROFIT_RATIO = old_ratio
            config.INITIAL_TAKE_PROFIT_PULLBACK_RATIO = old_pullback

    def test_breakout_highest_price_update_only(self):
        # 已突破，价格创新高，更新突破后最高价，不触发回撤
        stock_code = self._insert_position(cost_price=10.0, current_price=11.2, profit_triggered=0,
                                           profit_breakout_triggered=1, breakout_highest_price=11.0)
        with patch.object(self.pm, "_update_breakout_highest_price", return_value=True) as mock_update:
            signal_type, _ = self.pm.check_trading_signals(stock_code, current_price=11.2)
            self.assertIsNone(signal_type)
            self.assertTrue(mock_update.called, "应更新突破后最高价")

    def test_dynamic_take_profit_full_trigger(self):
        stock_code = self._insert_position(cost_price=10.0, current_price=10.9, profit_triggered=1,
                                           highest_price=12.0)
        with patch.object(self.pm, "calculate_stop_loss_price", return_value=11.0), \
             patch.object(self.pm, "_get_profit_level_info", return_value=(0.1, 0.93)):
            signal_type, info = self.pm.check_trading_signals(stock_code, current_price=10.9)
            self.assertEqual(signal_type, "take_profit_full")
            self.assertIn("dynamic_take_profit_price", info)
            self.assertIn("matched_level", info)

    def test_dynamic_take_profit_invalid_price(self):
        stock_code = self._insert_position(cost_price=10.0, current_price=10.9, profit_triggered=1,
                                           highest_price=12.0)
        with patch.object(self.pm, "calculate_stop_loss_price", return_value=13.5):
            signal_type, _ = self.pm.check_trading_signals(stock_code, current_price=10.9)
            self.assertIsNone(signal_type)

    def test_dynamic_take_profit_non_positive_price(self):
        stock_code = self._insert_position(cost_price=10.0, current_price=10.9, profit_triggered=1,
                                           highest_price=12.0)
        with patch.object(self.pm, "calculate_stop_loss_price", return_value=0):
            signal_type, _ = self.pm.check_trading_signals(stock_code, current_price=10.9)
            self.assertIsNone(signal_type)

    def test_dynamic_take_profit_calc_exception(self):
        stock_code = self._insert_position(cost_price=10.0, current_price=10.9, profit_triggered=1,
                                           highest_price=12.0)
        with patch.object(self.pm, "calculate_stop_loss_price", side_effect=RuntimeError("boom")):
            signal_type, _ = self.pm.check_trading_signals(stock_code, current_price=10.9)
            self.assertIsNone(signal_type)

    def test_calculate_stop_loss_price_branches(self):
        # 成本价无效
        self.assertEqual(self.pm.calculate_stop_loss_price(0, 10, False), 0.0)

        # 动态止盈配置为空 -> 使用保守止盈
        old_dynamic = config.DYNAMIC_TAKE_PROFIT
        try:
            config.DYNAMIC_TAKE_PROFIT = []
            price = self.pm.calculate_stop_loss_price(10.0, 12.0, True)
            self.assertAlmostEqual(price, 12.0 * 0.95, places=4)
        finally:
            config.DYNAMIC_TAKE_PROFIT = old_dynamic

        # 未达到任何盈利区间 -> 修复后回退固定止损（旧bug曾返回highest_price=10.2）
        price = self.pm.calculate_stop_loss_price(10.0, 10.2, True)
        self.assertAlmostEqual(price, 10.0 * (1 + config.STOP_LOSS_RATIO), places=4)

        # 固定止损
        price = self.pm.calculate_stop_loss_price(10.0, 11.0, False)
        self.assertAlmostEqual(price, 10.0 * (1 + config.STOP_LOSS_RATIO), places=4)

    def test_calculate_stop_loss_price_highest_invalid_and_str_flag(self):
        # 最高价无效(0) + profit_triggered字符串
        # 函数内 highest_price=0 修正为 cost_price=10.0，浮盈0%不达任何档位
        # 修复后：回退固定止损 10.0*(1+STOP_LOSS_RATIO)，旧bug曾返回 cost_price=10.0
        price = self.pm.calculate_stop_loss_price(10.0, 0.0, "True")
        self.assertAlmostEqual(price, 10.0 * (1 + config.STOP_LOSS_RATIO), places=4)

    def test_calculate_stop_loss_price_match_level(self):
        # 覆盖匹配最高止盈级别
        old_dynamic = config.DYNAMIC_TAKE_PROFIT
        try:
            config.DYNAMIC_TAKE_PROFIT = [
                (0.05, 0.90),
                (0.10, 0.80)
            ]
            price = self.pm.calculate_stop_loss_price(10.0, 11.5, True)
            self.assertAlmostEqual(price, 11.5 * 0.80, places=4)
        finally:
            config.DYNAMIC_TAKE_PROFIT = old_dynamic

    def test_profit_breakout_triggered_str_zero(self):
        # profit_breakout_triggered="0" 应被识别为False
        cost_price = 10.0
        current_price = cost_price * (1 + config.INITIAL_TAKE_PROFIT_RATIO + 0.01)
        stock_code = self._insert_position(cost_price=cost_price, current_price=current_price, profit_triggered=0,
                                           profit_breakout_triggered="0", breakout_highest_price=0.0)
        with patch.object(self.pm, "_mark_profit_breakout", return_value=True) as mock_mark:
            signal_type, _ = self.pm.check_trading_signals(stock_code, current_price=current_price)
            self.assertIsNone(signal_type)
            self.assertTrue(mock_mark.called, "profit_breakout_triggered='0'应触发突破标记")

    def test_highest_price_abnormal_high_repaired(self):
        # 最高价异常过高时应回退为max(cost, current)
        cost_price = 10.0
        current_price = 10.2
        stock_code = self._insert_position(cost_price=cost_price, current_price=current_price, profit_triggered=1,
                                           highest_price=cost_price * 30)
        with patch.object(self.pm, "calculate_stop_loss_price", return_value=9.0) as mock_calc:
            signal_type, _ = self.pm.check_trading_signals(stock_code, current_price=current_price)
            self.assertIsNone(signal_type)
            self.assertTrue(mock_calc.called)
            self.assertAlmostEqual(mock_calc.call_args[0][1], max(cost_price, current_price), places=4)

    def test_highest_price_abnormal_low_repaired(self):
        # 最高价异常过低时应回退为max(cost, current)
        cost_price = 10.0
        current_price = 10.1
        stock_code = self._insert_position(cost_price=cost_price, current_price=current_price, profit_triggered=1,
                                           highest_price=cost_price * 0.05)
        with patch.object(self.pm, "calculate_stop_loss_price", return_value=9.0) as mock_calc:
            signal_type, _ = self.pm.check_trading_signals(stock_code, current_price=current_price)
            self.assertIsNone(signal_type)
            self.assertTrue(mock_calc.called)
            self.assertAlmostEqual(mock_calc.call_args[0][1], max(cost_price, current_price), places=4)

    def test_get_profit_level_info_branches(self):
        # cost_price无效 -> 默认(0.0, 1.0)
        level, coef = self.pm._get_profit_level_info(0.0, 10.0)
        self.assertEqual(level, 0.0)
        self.assertEqual(coef, 1.0)

        old_dynamic = config.DYNAMIC_TAKE_PROFIT
        try:
            config.DYNAMIC_TAKE_PROFIT = [(0.05, 0.95), (0.10, 0.90)]
            level, coef = self.pm._get_profit_level_info(10.0, 11.5)
            self.assertEqual(level, 0.10)
            self.assertEqual(coef, 0.90)

            level, coef = self.pm._get_profit_level_info(10.0, 10.2)
            self.assertEqual(level, 0.0)
            self.assertEqual(coef, 1.0)
        finally:
            config.DYNAMIC_TAKE_PROFIT = old_dynamic

    def test_validate_trading_signal_stop_loss(self):
        stock_code = self._insert_position(cost_price=10.0, current_price=9.2, profit_triggered=0)
        # 无效数据
        ok = self.pm.validate_trading_signal(stock_code, "stop_loss", {
            "current_price": 0,
            "stop_loss_price": 9.0,
            "cost_price": 10.0
        })
        self.assertFalse(ok)

        # 亏损比例过小
        ok = self.pm.validate_trading_signal(stock_code, "stop_loss", {
            "current_price": 9.9,
            "stop_loss_price": 9.95,
            "cost_price": 10.0
        })
        self.assertFalse(ok)

    def test_validate_trading_signal_stop_loss_ratio_invalid(self):
        stock_code = self._insert_position(cost_price=10.0, current_price=9.0, profit_triggered=0)
        ok = self.pm.validate_trading_signal(stock_code, "stop_loss", {
            "current_price": 9.0,
            "stop_loss_price": 20.0,
            "cost_price": 10.0
        })
        self.assertFalse(ok)

    def test_validate_trading_signal_stop_loss_ok(self):
        stock_code = self._insert_position(cost_price=10.0, current_price=9.0, profit_triggered=0)
        ok = self.pm.validate_trading_signal(stock_code, "stop_loss", {
            "current_price": 9.0,
            "stop_loss_price": 9.25,
            "cost_price": 10.0
        })
        self.assertTrue(ok)

    def test_validate_trading_signal_take_profit(self):
        stock_code = self._insert_position(cost_price=10.0, current_price=10.5, profit_triggered=1)

        # 当前价小于成本价 -> 拒绝
        ok = self.pm.validate_trading_signal(stock_code, "take_profit_half", {
            "current_price": 9.9,
            "cost_price": 10.0
        })
        self.assertFalse(ok)

        # 正常止盈 -> 通过（使用take_profit_full跳过活跃委托检查）
        ok = self.pm.validate_trading_signal(stock_code, "take_profit_full", {
            "current_price": 10.8,
            "cost_price": 10.0
        })
        self.assertTrue(ok)

    def test_validate_trading_signal_take_profit_invalid_data(self):
        stock_code = self._insert_position(cost_price=10.0, current_price=10.5, profit_triggered=1)
        ok = self.pm.validate_trading_signal(stock_code, "take_profit_half", {
            "current_price": 0,
            "cost_price": 10.0
        })
        self.assertFalse(ok)

    def test_validate_trading_signal_take_profit_no_position(self):
        with patch.object(self.pm, "get_position", return_value=None):
            ok = self.pm.validate_trading_signal("000001.SZ", "take_profit_half", {
                "current_price": 10.8,
                "cost_price": 10.0
            })
            self.assertTrue(ok)

    def test_validate_trading_signal_take_profit_use_signal_cost(self):
        stock_code = self._insert_position(cost_price=0.0, current_price=10.5, profit_triggered=1)
        ok = self.pm.validate_trading_signal(stock_code, "take_profit_half", {
            "current_price": 10.6,
            "cost_price": 10.0
        })
        self.assertTrue(ok)

    def test_validate_trading_signal_take_profit_full_skip_pending_orders(self):
        stock_code = self._insert_position(cost_price=10.0, current_price=10.5, profit_triggered=1,
                                           available=0, volume=1000)
        old_flag = getattr(config, "ALLOW_TAKE_PROFIT_FULL_WITH_PENDING", False)
        try:
            config.ALLOW_TAKE_PROFIT_FULL_WITH_PENDING = True
            ok = self.pm.validate_trading_signal(stock_code, "take_profit_full", {
                "current_price": 10.6,
                "cost_price": 10.0
            })
            self.assertTrue(ok)
        finally:
            config.ALLOW_TAKE_PROFIT_FULL_WITH_PENDING = old_flag

    def test_validate_trading_signal_pending_orders_block(self):
        stock_code = self._insert_position(cost_price=10.0, current_price=10.5, profit_triggered=1,
                                           available=0, volume=1000)
        with patch.object(self.pm, "_has_pending_orders", return_value=True):
            ok = self.pm.validate_trading_signal(stock_code, "take_profit_half", {
                "current_price": 10.6,
                "cost_price": 10.0
            })
            self.assertFalse(ok)

            ok, status, reason = self.pm.validate_trading_signal(
                stock_code,
                "take_profit_half",
                {"current_price": 10.6, "cost_price": 10.0},
                return_reason=True
            )
            self.assertFalse(ok)
            self.assertEqual(status, "blocked")
            self.assertEqual(reason, "pending_order")

    def test_validate_trading_signal_pending_orders_no_active_but_block(self):
        stock_code = self._insert_position(cost_price=10.0, current_price=10.5, profit_triggered=1,
                                           available=0, volume=1000)
        with patch.object(self.pm, "_has_pending_orders", return_value=False):
            ok = self.pm.validate_trading_signal(stock_code, "take_profit_half", {
                "current_price": 10.6,
                "cost_price": 10.0
            })
            self.assertFalse(ok)

            ok, status, reason = self.pm.validate_trading_signal(
                stock_code,
                "take_profit_half",
                {"current_price": 10.6, "cost_price": 10.0},
                return_reason=True
            )
            self.assertFalse(ok)
            self.assertEqual(status, "blocked")
            self.assertEqual(reason, "available_zero_sync_delay")

    def test_execute_trading_signal_direct_blocked_not_failed(self):
        """委托阻断应返回 blocked，不进入实际卖出执行。"""
        strategy = TradingStrategy.__new__(TradingStrategy)
        strategy.position_manager = MagicMock()
        strategy.position_manager.validate_trading_signal.return_value = (
            False, "blocked", "pending_order"
        )

        with patch.object(TradingStrategy, "_execute_stop_loss_signal") as mock_execute:
            result = TradingStrategy.execute_trading_signal_direct(
                strategy,
                "301161",
                "stop_loss",
                {"current_price": 37.82, "cost_price": 65.5, "stop_loss_price": 60.6}
            )

        self.assertEqual(result, TradingStrategy.SIGNAL_EXECUTION_BLOCKED)
        mock_execute.assert_not_called()

    def test_take_profit_full_blocked_when_available_zero(self):
        """
        B7: profit_triggered=True 且 available=0 时，
        check_trading_signals 不应生成 take_profit_full 信号（已有委托在途）。

        验证 check_trading_signals:2076 的 available<=0 提前返回逻辑。
        当前价低于动态止盈位，但由于 available=0，信号应被压制。
        """
        cost_price = 10.0
        highest_price = 12.0   # 浮盈 20%，匹配级别4（系数0.87）
        trigger_price = highest_price * 0.87  # 10.44
        current_price = trigger_price - 0.05  # 10.39，低于止盈位，本应触发

        stock_code = self._insert_position(
            cost_price=cost_price,
            current_price=current_price,
            profit_triggered=1,
            highest_price=highest_price,
            volume=1000,
            available=0,        # 已有委托在途，应压制信号生成
        )

        signal_type, signal_info = self.pm.check_trading_signals(
            stock_code, current_price=current_price
        )

        self.assertIsNone(signal_type,
                          "profit_triggered=True 且 available=0 时不应产生 take_profit_full 信号")
        self.assertIsNone(signal_info)

    def test_take_profit_full_triggers_when_available_nonzero(self):
        """
        B7 对照组：available>0 时，相同的价格条件应正常触发 take_profit_full 信号。
        确保 available=0 的压制逻辑不影响正常场景。
        """
        cost_price = 10.0
        highest_price = 12.0
        trigger_price = highest_price * 0.87  # 10.44
        current_price = trigger_price - 0.05  # 10.39

        stock_code = self._insert_position(
            cost_price=cost_price,
            current_price=current_price,
            profit_triggered=1,
            highest_price=highest_price,
            volume=1000,
            available=400,      # 剩余可用持仓，应正常触发
        )

        signal_type, signal_info = self.pm.check_trading_signals(
            stock_code, current_price=current_price
        )

        self.assertEqual(signal_type, "take_profit_full",
                         "available>0 时价格跌破动态止盈位应触发 take_profit_full")
        self.assertIn("dynamic_take_profit_price", signal_info)


if __name__ == "__main__":
    unittest.main(verbosity=2)
