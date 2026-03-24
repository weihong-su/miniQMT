"""
动态止盈止损高级测试 - 止损补仓功能
测试 ENABLE_STOP_LOSS_BUY 配置和止损补仓逻辑

Worker 3/3 - 止损补仓测试
"""
import unittest
import sys
import os
from unittest.mock import MagicMock, patch
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from test.test_base import TestBase
from position_manager import PositionManager
from logger import get_logger

logger = get_logger("test_stop_profit_advanced_3")


class TestStopProfitAdvanced3(TestBase):
    """测试止损补仓功能"""

    def setUp(self):
        super().setUp()
        from trading_executor import TradingExecutor

        mock_executor = MagicMock(spec=TradingExecutor)
        mock_executor.qmt_trader = MagicMock()

        self.position_manager = PositionManager()
        self.position_manager.qmt_trader = mock_executor.qmt_trader

        logger.info(f"测试准备完成: {self._testMethodName}")

    def tearDown(self):
        try:
            self.position_manager.stop_sync_thread()
        except Exception:
            pass
        super().tearDown()

    def test_16_stop_loss_buy_position_limit(self):
        """
        测试16：止损补仓的持仓限制

        测试场景：
        1. 持仓接近上限时，补仓金额应自动调整
        2. 持仓达到上限时，不应生成补仓信号
        """
        logger.info("=" * 60)
        logger.info("测试16：止损补仓持仓限制")
        logger.info("=" * 60)

        if not hasattr(config, 'ENABLE_STOP_LOSS_BUY'):
            self.skipTest("ENABLE_STOP_LOSS_BUY功能未实现")

        stock_code = "000002.SZ"

        # ========== 场景1：持仓接近上限 ==========
        logger.info("\n--- 场景1：持仓接近上限 ---")

        cost_price = 10.0
        current_price = 9.20
        volume = 6500  # 6500股 * 10元 = 65000元

        conn = self.position_manager.conn
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO positions
            (stock_code, volume, available, cost_price, current_price,
             open_date, profit_triggered, highest_price, stop_loss_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (stock_code, volume, volume, cost_price, current_price,
              datetime.now().strftime("%Y-%m-%d"), 0, cost_price, 9.25))
        conn.commit()

        current_market_value = volume * current_price
        remaining_space = config.MAX_POSITION_VALUE - current_market_value

        logger.info(f"当前市值: {current_market_value:.2f}元")
        logger.info(f"最大持仓: {config.MAX_POSITION_VALUE}元")
        logger.info(f"剩余空间: {remaining_space:.2f}元")

        with patch.object(config, 'ENABLE_STOP_LOSS_BUY', True):
            signal_type, signal_info = self.position_manager.check_add_position_signal(stock_code)

            if signal_type == 'add_position' and signal_info:
                add_amount = signal_info['add_amount']
                logger.info(f"补仓金额: {add_amount}元")
                self.assertLessEqual(add_amount, remaining_space + 1,
                                     "补仓金额应不超过剩余持仓空间")
                logger.info("补仓金额自动调整验证通过")
            else:
                logger.info("未生成补仓信号（可能剩余空间不足）")

        # ========== 场景2：持仓达到上限 ==========
        logger.info("\n--- 场景2：持仓达到上限 ---")

        volume_at_limit = int(config.MAX_POSITION_VALUE / current_price)
        cursor.execute("""
            UPDATE positions SET volume = ?, available = ? WHERE stock_code = ?
        """, (volume_at_limit, volume_at_limit, stock_code))
        conn.commit()

        market_value_at_limit = volume_at_limit * current_price
        logger.info(f"持仓市值: {market_value_at_limit:.2f}元（已达上限）")

        with patch.object(config, 'ENABLE_STOP_LOSS_BUY', True):
            signal_type, signal_info = self.position_manager.check_add_position_signal(stock_code)
            self.assertIsNone(signal_type, "持仓达到上限时，不应生成补仓信号")
            logger.info("持仓上限验证通过：未生成补仓信号")

        logger.info("=" * 60)
        logger.info("测试16完成：止损补仓持仓限制验证通过")
        logger.info("=" * 60)

    def test_16b_scenario_b_stop_loss_first_no_add_position(self):
        """
        测试16b：补仓场景B——止损优先，永不补仓

        当 add_position_threshold >= stop_loss_threshold 时（场景B），
        即使价格下跌达到止损阈值附近，check_add_position_signal 也应返回 None。

        验证点：
        1. 价格下跌超过 stop_loss_threshold (7.5%) 时返回 None（止损拦截）
        2. 价格下跌超过 add_position_threshold (10%) 时返回 None（止损优先策略拒绝）
        3. 价格下跌不足 stop_loss_threshold 时返回 None（未达触发条件）
        """
        logger.info("=" * 60)
        logger.info("测试16b：场景B——止损优先策略不触发补仓")
        logger.info("=" * 60)

        if not hasattr(config, 'ENABLE_STOP_LOSS_BUY'):
            self.skipTest("ENABLE_STOP_LOSS_BUY功能未实现")

        stock_code = "000003.SZ"
        cost_price = 10.0
        volume = 3000

        # 场景B条件：BUY_GRID_LEVELS[1] <= 0.925
        # add_threshold = 1 - 0.90 = 0.10 >= stop_threshold = 0.075 → 场景B
        scenario_b_grid_levels = [1.0, 0.90, 0.80]
        add_threshold_b = 1 - scenario_b_grid_levels[1]  # 0.10
        stop_threshold = abs(config.STOP_LOSS_RATIO)      # 0.075
        self.assertGreaterEqual(add_threshold_b, stop_threshold,
                                "测试前置条件：add_threshold 应 >= stop_threshold（场景B）")
        logger.info(f"场景B参数: add_threshold={add_threshold_b:.0%}, "
                    f"stop_threshold={stop_threshold:.0%}")

        with patch.object(config, 'BUY_GRID_LEVELS', scenario_b_grid_levels), \
             patch.object(config, 'ENABLE_STOP_LOSS_BUY', True):

            # 验证动态优先级判断确实为场景B
            priority = config.determine_stop_loss_add_position_priority()
            self.assertEqual(priority['priority'], 'stop_loss_first',
                             "patch后应判定为场景B（stop_loss_first）")
            logger.info(f"动态优先级确认: {priority['priority']} (场景{priority['scenario']})")

            # ---- sub-case 1：下跌 8%（介于 stop_threshold 7.5% 和 add_threshold 10% 之间）----
            drop_8pct = round(cost_price * (1 - 0.08), 2)
            logger.info(f"\n--- Sub-case 1: 下跌 8% 到 {drop_8pct}（>stop阈值 {stop_threshold:.0%}）---")

            conn = self.position_manager.conn
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO positions
                (stock_code, volume, available, cost_price, current_price,
                 open_date, profit_triggered, highest_price, stop_loss_price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (stock_code, volume, volume, cost_price, drop_8pct,
                  datetime.now().strftime("%Y-%m-%d"), 0, cost_price,
                  cost_price * (1 + config.STOP_LOSS_RATIO)))
            conn.commit()

            signal_type, _ = self.position_manager.check_add_position_signal(stock_code)
            self.assertIsNone(signal_type,
                              f"场景B: 下跌8%时止损优先策略应拒绝补仓，实际={signal_type}")
            logger.info("断言通过: 下跌8% 时无补仓信号（止损拦截）")

            # ---- sub-case 2：下跌 11%（超过 add_threshold 10%）----
            drop_11pct = round(cost_price * (1 - 0.11), 2)
            logger.info(f"\n--- Sub-case 2: 下跌 11% 到 {drop_11pct}（>add阈值 {add_threshold_b:.0%}）---")

            cursor.execute("""
                UPDATE positions SET current_price = ? WHERE stock_code = ?
            """, (drop_11pct, stock_code))
            conn.commit()

            signal_type, _ = self.position_manager.check_add_position_signal(stock_code)
            self.assertIsNone(signal_type,
                              f"场景B: 下跌11%时止损优先策略应拒绝补仓，实际={signal_type}")
            logger.info("断言通过: 下跌11% 时无补仓信号（止损优先拒绝）")

            # ---- sub-case 3：下跌 4%（未达任何阈值）----
            drop_4pct = round(cost_price * (1 - 0.04), 2)
            logger.info(f"\n--- Sub-case 3: 下跌 4% 到 {drop_4pct}（未达任何阈值）---")

            cursor.execute("""
                UPDATE positions SET current_price = ? WHERE stock_code = ?
            """, (drop_4pct, stock_code))
            conn.commit()

            signal_type, _ = self.position_manager.check_add_position_signal(stock_code)
            self.assertIsNone(signal_type,
                              "下跌不足阈值时不应生成补仓信号")
            logger.info("断言通过: 下跌4% 时无补仓信号（未达阈值）")

        logger.info("=" * 60)
        logger.info("测试16b完成：场景B止损优先策略验证通过（共3个子场景）")
        logger.info("=" * 60)


if __name__ == '__main__':
    unittest.main(verbosity=2)
