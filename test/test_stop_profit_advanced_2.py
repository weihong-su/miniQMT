"""
动态止盈止损高级测试 - 多级别触发和全仓止盈
测试 DYNAMIC_TAKE_PROFIT 的 7 个级别触发逻辑

测试场景：
- test_13: 7 个动态止盈级别的止盈价计算正确性验证，以及通过 check_trading_signals 端到端验证信号生成
- test_14: 动态全仓止盈 take_profit_full 信号的端到端生成验证
"""
import unittest
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from test.test_base import TestBase
from position_manager import PositionManager
from logger import get_logger

logger = get_logger("test_stop_profit_advanced_2")


class TestStopProfitAdvanced2(TestBase):
    """测试动态止盈多级别机制"""

    def setUp(self):
        super().setUp()
        from trading_executor import TradingExecutor
        from unittest.mock import MagicMock

        mock_executor = MagicMock(spec=TradingExecutor)
        mock_executor.qmt_trader = MagicMock()

        self.position_manager = PositionManager()
        self.position_manager.qmt_trader = mock_executor.qmt_trader

        # 确保内存表包含 profit_breakout_triggered 字段
        self._ensure_memory_schema()

        # 清理内存持仓，保证用例隔离
        cursor = self.position_manager.memory_conn.cursor()
        cursor.execute("DELETE FROM positions")
        self.position_manager.memory_conn.commit()

        logger.info(f"测试准备完成: {self._testMethodName}")

    def tearDown(self):
        try:
            self.position_manager.stop_sync_thread()
        except Exception:
            pass
        super().tearDown()

    def _ensure_memory_schema(self):
        cursor = self.position_manager.memory_conn.cursor()
        cursor.execute("PRAGMA table_info(positions)")
        cols = {row[1] for row in cursor.fetchall()}
        changed = False
        if "profit_breakout_triggered" not in cols:
            cursor.execute(
                "ALTER TABLE positions ADD COLUMN profit_breakout_triggered BOOLEAN DEFAULT FALSE"
            )
            changed = True
        if "breakout_highest_price" not in cols:
            cursor.execute(
                "ALTER TABLE positions ADD COLUMN breakout_highest_price REAL"
            )
            changed = True
        if changed:
            self.position_manager.memory_conn.commit()

    def _insert_memory_position(self, **kw):
        """直接向内存持仓表插入数据（绕过 SQLite 同步）"""
        stock_code = kw.get("stock_code", "000001.SZ")
        cost_price = kw.get("cost_price", 10.0)
        cursor = self.position_manager.memory_conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO positions
            (stock_code, volume, available, cost_price, current_price,
             open_date, profit_triggered, highest_price, stop_loss_price,
             profit_breakout_triggered, breakout_highest_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            stock_code,
            kw.get("volume", 1000),
            kw.get("available", kw.get("volume", 1000)),
            cost_price,
            kw.get("current_price", cost_price),
            kw.get("open_date", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            kw.get("profit_triggered", 1),
            kw.get("highest_price", cost_price),
            kw.get("stop_loss_price", cost_price * (1 + config.STOP_LOSS_RATIO)),
            kw.get("profit_breakout_triggered", 0),
            kw.get("breakout_highest_price", 0.0),
        ))
        self.position_manager.memory_conn.commit()
        return stock_code

    def test_13_dynamic_take_profit_levels(self):
        """
        测试13：7 个动态止盈级别的触发逻辑端到端验证

        对 DYNAMIC_TAKE_PROFIT 中每个级别:
        1. 验证 calculate_stop_loss_price 返回值 = highest_price * coefficient（价格计算正确性）
        2. 通过 check_trading_signals 验证当 current_price 略低于止盈位时生成 take_profit_full 信号
        """
        logger.info(f"开始测试13：DYNAMIC_TAKE_PROFIT 7个级别端到端验证")

        cost_price = 10.0
        volume = 1000
        dynamic_levels = config.DYNAMIC_TAKE_PROFIT
        logger.info(f"动态止盈配置共 {len(dynamic_levels)} 个级别: {dynamic_levels}")

        for i, (profit_threshold, profit_coefficient) in enumerate(dynamic_levels, 1):
            stock_code = f"00000{i}.SZ"
            logger.info(f"\n=== 级别{i}: 浮盈阈值{profit_threshold*100:.0f}%, "
                        f"止盈系数{profit_coefficient} ===")

            # 最高价恰好达到该浮盈阈值
            highest_price = cost_price * (1 + profit_threshold)
            # 理论止盈位
            expected_trigger_price = highest_price * profit_coefficient
            # 当前价略低于止盈位，触发止盈
            current_price = round(expected_trigger_price - 0.01, 4)

            logger.info(f"  highest_price={highest_price:.2f}, "
                        f"trigger_price={expected_trigger_price:.4f}, "
                        f"current_price={current_price:.4f}")

            # --- 子验证1：calculate_stop_loss_price 计算正确 ---
            computed_price = self.position_manager.calculate_stop_loss_price(
                cost_price, highest_price, True
            )
            self.assertAlmostEqual(
                computed_price, expected_trigger_price, places=4,
                msg=f"级别{i}: calculate_stop_loss_price 应返回 {expected_trigger_price:.4f}"
            )
            logger.info(f"  [子验证1] calculate_stop_loss_price={computed_price:.4f} 正确")

            # --- 子验证2：check_trading_signals 端到端生成 take_profit_full ---
            self._insert_memory_position(
                stock_code=stock_code,
                volume=volume,
                available=volume,
                cost_price=cost_price,
                current_price=current_price,
                profit_triggered=1,
                highest_price=highest_price,
            )

            signal_type, signal_info = self.position_manager.check_trading_signals(
                stock_code, current_price=current_price
            )
            self.assertEqual(
                signal_type, "take_profit_full",
                f"级别{i}: 当前价低于止盈位时应生成 take_profit_full，实际: {signal_type}"
            )
            self.assertIn("dynamic_take_profit_price", signal_info,
                          "signal_info 应含 dynamic_take_profit_price")
            self.assertAlmostEqual(
                signal_info["dynamic_take_profit_price"], computed_price, places=3,
                msg=f"级别{i}: signal_info 中的止盈价应与计算结果一致"
            )
            logger.info(f"  [子验证2] check_trading_signals 返回 take_profit_full, "
                        f"dynamic_take_profit_price={signal_info['dynamic_take_profit_price']:.4f}")

        logger.info(f"\n测试13完成：{len(dynamic_levels)} 个动态止盈级别端到端验证全部通过")

    def test_14_take_profit_full_signal(self):
        """
        测试14：动态全仓止盈 take_profit_full 信号端到端生成验证

        场景：
        - profit_triggered=True，剩余 40% 持仓（首次止盈后）
        - 最高价达到浮盈 10%（级别2，止盈系数 93%）
        - 当前价跌至止盈位以下
        - 验证 check_trading_signals 返回 take_profit_full 且信号字段完整
        """
        logger.info("开始测试14：动态全仓止盈信号端到端生成验证")

        stock_code = "000002.SZ"
        cost_price = 10.0
        initial_volume = 1000
        remaining_volume = int(initial_volume * (1 - config.INITIAL_TAKE_PROFIT_RATIO_PERCENTAGE))
        logger.info(f"剩余持仓: {remaining_volume}股 (首次止盈后 {remaining_volume/initial_volume:.0%})")

        # 使用级别2配置: 浮盈10%, 止盈系数93%
        # 从 DYNAMIC_TAKE_PROFIT 中动态取，避免硬编码
        level2_threshold, level2_coef = config.DYNAMIC_TAKE_PROFIT[1]  # (0.10, 0.93)
        highest_price = cost_price * (1 + level2_threshold)   # 11.0
        trigger_price = highest_price * level2_coef            # 10.23
        current_price = round(trigger_price - 0.01, 4)        # 10.22，触发止盈

        logger.info(f"配置: 级别阈值={level2_threshold:.0%}, 系数={level2_coef}")
        logger.info(f"最高价={highest_price:.2f}, 止盈位={trigger_price:.4f}, 当前价={current_price:.4f}")

        self._insert_memory_position(
            stock_code=stock_code,
            volume=remaining_volume,
            available=remaining_volume,
            cost_price=cost_price,
            current_price=current_price,
            profit_triggered=1,
            highest_price=highest_price,
        )

        signal_type, signal_info = self.position_manager.check_trading_signals(
            stock_code, current_price=current_price
        )

        # 断言1: 信号类型正确
        self.assertEqual(signal_type, "take_profit_full",
                         f"应触发 take_profit_full，实际: {signal_type}")

        # 断言2: signal_info 字段完整
        required_fields = ["current_price", "dynamic_take_profit_price",
                           "highest_price", "matched_level", "volume", "cost_price"]
        for field in required_fields:
            self.assertIn(field, signal_info, f"signal_info 应包含字段: {field}")

        # 断言3: 价格值正确
        self.assertAlmostEqual(signal_info["current_price"], current_price, places=3)
        self.assertAlmostEqual(signal_info["highest_price"], highest_price, places=3)
        self.assertEqual(signal_info["volume"], remaining_volume,
                         "卖出数量应等于剩余持仓量")

        # 断言4: 止盈价格在合理范围
        dtp = signal_info["dynamic_take_profit_price"]
        self.assertGreater(dtp, 0, "止盈价应为正数")
        self.assertLessEqual(dtp, highest_price, "止盈价不应超过最高价")

        logger.info(f"断言通过: signal=take_profit_full, "
                    f"dynamic_take_profit_price={dtp:.4f}, "
                    f"matched_level={signal_info['matched_level']:.0%}, "
                    f"volume={signal_info['volume']}")
        logger.info("测试14完成：动态全仓止盈信号端到端生成验证通过")


if __name__ == '__main__':
    unittest.main()
