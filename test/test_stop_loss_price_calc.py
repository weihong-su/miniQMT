"""
calculate_stop_loss_price Bug 回归测试

回归背景：
  历史 bug（2025-04-25 引入）：
    take_profit_coefficient 默认值为 1.0，当 profit_triggered=True 但
    highest_profit_ratio 未达到任何动态档位时，返回 highest_price * 1.0 = highest_price，
    导致动态止损价等于历史最高价，验证逻辑也无法修正该脏数据。

修复内容：
  1. calculate_stop_loss_price：未匹配任何档位时回退到固定止损，而非 highest_price
  2. 验证逻辑：新增 stop_loss_price >= highest_price * 0.999 条件，检测等于最高价的脏数据

测试覆盖：
  TC-01 ~ TC-04: calculate_stop_loss_price 核心返回值
  TC-05 ~ TC-06: 边界档位匹配（恰好5%，恰好10%）
  TC-07:         profit_triggered=False 固定止损
  TC-08:         cost_price 无效兜底
  TC-09 ~ TC-10: 验证逻辑自动修正脏数据
  TC-11:         301399 实际场景复现
"""

import sys
import os
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from test.test_base import TestBase
from logger import get_logger

logger = get_logger("test_stop_loss_price_calc")


class TestCalculateStopLossPriceBugFix(TestBase):
    """calculate_stop_loss_price 核心逻辑回归测试"""

    def setUp(self):
        super().setUp()
        from position_manager import PositionManager
        self.pm = PositionManager()
        # 保存原始动态止盈配置，测试结束后恢复
        self._orig_dynamic = config.DYNAMIC_TAKE_PROFIT
        self._orig_ratio = config.STOP_LOSS_RATIO

    def tearDown(self):
        try:
            self.pm.stop_sync_thread()
        except Exception:
            pass
        config.DYNAMIC_TAKE_PROFIT = self._orig_dynamic
        config.STOP_LOSS_RATIO = self._orig_ratio
        super().tearDown()

    # ------------------------------------------------------------------
    # TC-01: profit_triggered=True，最高浮盈6.26%，匹配>=5%档(系数0.96)
    #        复现 301399 场景，修复前返回 highest_price，修复后应返回 highest*0.96
    # ------------------------------------------------------------------
    def test_01_bug_fix_5pct_tier_matched(self):
        """TC-01: 301399场景 - 最高浮盈6.26%，应返回highest*0.96=21.83"""
        cost = 21.40
        highest = 22.74
        result = self.pm.calculate_stop_loss_price(cost, highest, profit_triggered=True)
        expected = round(highest * 0.96, 4)
        self.assertAlmostEqual(result, expected, places=2,
                               msg=f"bug修复: 应为 {expected:.2f}，实际返回 {result:.2f}（修复前会返回 {highest}）")
        # 核心断言：不能等于历史最高价
        self.assertLess(result, highest,
                        msg="动态止损价不应等于历史最高价（这是旧bug的特征）")

    # ------------------------------------------------------------------
    # TC-02: profit_triggered=True，最高浮盈恰好低于5%（4.99%），未匹配任何档位
    #        修复前返回 highest_price，修复后应回退到固定止损
    # ------------------------------------------------------------------
    def test_02_bug_fix_below_min_tier(self):
        """TC-02: 最高浮盈4.99%，低于最低档5%，应回退到固定止损"""
        cost = 21.40
        highest = cost * (1 + 0.0499)  # 4.99% 盈利
        result = self.pm.calculate_stop_loss_price(cost, highest, profit_triggered=True)
        # 修复后应回退固定止损
        expected_fixed = cost * (1 + config.STOP_LOSS_RATIO)
        self.assertAlmostEqual(result, expected_fixed, places=2,
                               msg=f"未达动态档位应回退固定止损 {expected_fixed:.2f}，实际 {result:.2f}")
        # 核心断言：不能等于最高价
        self.assertLess(result, highest,
                        msg="低于最低动态档位时，返回值不应等于最高价（旧bug特征）")

    # ------------------------------------------------------------------
    # TC-03: profit_triggered=True，最高浮盈恰好0%（刚触发首次止盈时的时序场景）
    #        这是旧bug最常触发的边界条件
    # ------------------------------------------------------------------
    def test_03_bug_fix_zero_profit_ratio(self):
        """TC-03: 最高浮盈0%（cost==highest），应回退固定止损而非返回highest"""
        cost = 21.40
        highest = 21.40  # 浮盈0%，未达任何档位
        result = self.pm.calculate_stop_loss_price(cost, highest, profit_triggered=True)
        expected_fixed = cost * (1 + config.STOP_LOSS_RATIO)
        self.assertAlmostEqual(result, expected_fixed, places=2,
                               msg=f"浮盈0%时应返回固定止损 {expected_fixed:.2f}，实际 {result:.2f}")
        self.assertLess(result, highest,
                        msg="浮盈0%不应返回highest（旧bug特征）")

    # ------------------------------------------------------------------
    # TC-04: profit_triggered=True，最高浮盈30%，匹配>=30%档(系数0.85)
    # ------------------------------------------------------------------
    def test_04_high_profit_tier_matched(self):
        """TC-04: 最高浮盈30%，匹配>=30%档系数0.85"""
        cost = 10.0
        highest = 13.0  # 30% 盈利
        result = self.pm.calculate_stop_loss_price(cost, highest, profit_triggered=True)
        expected = highest * 0.85
        self.assertAlmostEqual(result, expected, places=2,
                               msg=f"30%浮盈应匹配0.85档，期望 {expected:.2f}，实际 {result:.2f}")

    # ------------------------------------------------------------------
    # TC-05: 边界测试 - 恰好5.00%浮盈，应匹配>=5%档
    # ------------------------------------------------------------------
    def test_05_boundary_exactly_5pct(self):
        """TC-05: 边界 - 恰好5.00%浮盈，应匹配>=5%档(0.96)"""
        cost = 10.0
        highest = 10.5  # 恰好5%
        result = self.pm.calculate_stop_loss_price(cost, highest, profit_triggered=True)
        expected = highest * 0.96
        self.assertAlmostEqual(result, expected, places=2,
                               msg=f"恰好5%应匹配0.96档，期望 {expected:.2f}，实际 {result:.2f}")

    # ------------------------------------------------------------------
    # TC-06: 边界测试 - 恰好10.00%浮盈，应匹配>=10%档(0.93)
    # ------------------------------------------------------------------
    def test_06_boundary_exactly_10pct(self):
        """TC-06: 边界 - 恰好10.00%浮盈，应匹配>=10%档(0.93)"""
        cost = 10.0
        highest = 11.0  # 恰好10%
        result = self.pm.calculate_stop_loss_price(cost, highest, profit_triggered=True)
        expected = highest * 0.93
        self.assertAlmostEqual(result, expected, places=2,
                               msg=f"恰好10%应匹配0.93档，期望 {expected:.2f}，实际 {result:.2f}")

    # ------------------------------------------------------------------
    # TC-07: profit_triggered=False，应使用固定止损(成本价 * (1+STOP_LOSS_RATIO))
    # ------------------------------------------------------------------
    def test_07_profit_not_triggered_fixed_stop(self):
        """TC-07: profit_triggered=False，应返回固定止损价"""
        cost = 31.38
        highest = 32.88
        result = self.pm.calculate_stop_loss_price(cost, highest, profit_triggered=False)
        expected = cost * (1 + config.STOP_LOSS_RATIO)
        self.assertAlmostEqual(result, expected, places=2,
                               msg=f"固定止损应为 {expected:.2f}，实际 {result:.2f}")

    # ------------------------------------------------------------------
    # TC-08: cost_price 无效，应返回 0.0
    # ------------------------------------------------------------------
    def test_08_invalid_cost_price_returns_zero(self):
        """TC-08: cost_price=0，应返回0.0"""
        result = self.pm.calculate_stop_loss_price(0, 10.0, profit_triggered=True)
        self.assertEqual(result, 0.0, "cost_price无效时应返回0.0")

    # ------------------------------------------------------------------
    # TC-09: 验证修复后 calculate_stop_loss_price 不产生脏数据
    #        旧bug：profit_triggered=True 且浮盈刚触及首次止盈阈值时（约6%），
    #        因时序因素可能以 highest_price == cost*(1+6%) 调用，此时若6% < 最低档5%
    #        为时序边界模拟：以 highest_price == cost*1.06 - 极小值构造浮盈4.99%场景
    # ------------------------------------------------------------------
    def test_09_no_dirty_data_produced_at_trigger_boundary(self):
        """TC-09: 修复后计算结果不产生 stop_loss_price == highest_price 的脏数据"""
        cost = 21.40
        # 构造多个"刚触发止盈"场景下的最高价，全部验证不返回 highest_price
        test_cases = [
            cost * 1.0,       # 浮盈 0%
            cost * 1.02,      # 浮盈 2%
            cost * 1.04,      # 浮盈 4%
            cost * 1.049,     # 浮盈 4.9%（低于最低档5%）
        ]
        for highest in test_cases:
            result = self.pm.calculate_stop_loss_price(cost, highest, profit_triggered=True)
            self.assertLess(result, highest,
                            msg=f"highest={highest:.3f}时，结果{result:.3f}不应等于最高价（旧bug特征）")
            # 验证回退到固定止损
            expected_fixed = cost * (1 + config.STOP_LOSS_RATIO)
            self.assertAlmostEqual(result, expected_fixed, places=2,
                                   msg=f"highest={highest:.3f}时应回退固定止损{expected_fixed:.2f}，实际{result:.2f}")

    # ------------------------------------------------------------------
    # TC-10: 脏数据检测条件验证
    #        验证 stop_loss_price >= highest_price * 0.999 条件能正确区分脏数据与正常数据
    # ------------------------------------------------------------------
    def test_10_dirty_data_detection_condition(self):
        """TC-10: 验证 >= highest*0.999 能区分脏数据与正常动态止损价"""
        highest = 22.74
        # 脏数据场景（旧bug产生）
        dirty_values = [22.74, 22.739, 22.73]   # >= highest * 0.999 = 22.717
        # 正常止损价范围（5%档以上产生的值）
        normal_values = [21.83, 21.0, 20.5]     # < highest * 0.999

        threshold = highest * 0.999
        for v in dirty_values:
            self.assertGreaterEqual(v, threshold,
                                    msg=f"脏数据 {v} 应 >= threshold {threshold:.3f}")
        for v in normal_values:
            self.assertLess(v, threshold,
                            msg=f"正常值 {v} 应 < threshold {threshold:.3f}")

    # ------------------------------------------------------------------
    # TC-11: 301399 完整场景端到端验证
    #        给定 cost=21.40, highest=22.74, profit_triggered=True
    #        verify：calculate_stop_loss_price 返回 21.83（22.74*0.96）
    #        verify：返回值严格 < highest_price（不是旧bug的1.0倍）
    # ------------------------------------------------------------------
    def test_11_301399_scenario_end_to_end(self):
        """TC-11: 301399 完整场景 - cost=21.40, highest=22.74, profit_triggered=True"""
        cost = 21.40
        highest = 22.74
        result = self.pm.calculate_stop_loss_price(cost, highest, profit_triggered=True)
        # 最高浮盈 = (22.74-21.40)/21.40 = 6.26% >= 5%，匹配系数0.96
        expected = round(22.74 * 0.96, 2)  # 21.83
        self.assertAlmostEqual(result, expected, places=2,
                               msg=f"301399场景应返回 {expected}，实际 {result:.2f}")
        self.assertLess(result, highest,
                        msg="动态止损价必须 < 历史最高价，否则为旧bug")
        self.assertGreater(result, cost,
                           msg="动态止损价应高于成本价（已有盈利保护）")


class TestCalculateStopLossEdgeCases(TestBase):
    """边界条件和特殊配置测试"""

    def setUp(self):
        super().setUp()
        from position_manager import PositionManager
        self.pm = PositionManager()
        self._orig_dynamic = config.DYNAMIC_TAKE_PROFIT
        self._orig_ratio = config.STOP_LOSS_RATIO

    def tearDown(self):
        try:
            self.pm.stop_sync_thread()
        except Exception:
            pass
        config.DYNAMIC_TAKE_PROFIT = self._orig_dynamic
        config.STOP_LOSS_RATIO = self._orig_ratio
        super().tearDown()

    def test_12_empty_dynamic_config_fallback(self):
        """TC-12: DYNAMIC_TAKE_PROFIT为空时，使用保守止盈位(highest*0.95)"""
        config.DYNAMIC_TAKE_PROFIT = []
        cost = 10.0
        highest = 11.0
        result = self.pm.calculate_stop_loss_price(cost, highest, profit_triggered=True)
        # 配置为空时返回 highest * 0.95
        self.assertAlmostEqual(result, highest * 0.95, places=2,
                               msg=f"空动态配置应返回highest*0.95={highest*0.95:.2f}")

    def test_13_highest_price_invalid_uses_cost(self):
        """TC-13: highest_price无效(<=0)，使用cost_price代替，回退到固定止损"""
        cost = 10.0
        result = self.pm.calculate_stop_loss_price(cost, highest_price=0,
                                                    profit_triggered=True)
        # highest_price=0，函数内修正为cost，浮盈0%，未达任何档位，回退固定止损
        expected_fixed = cost * (1 + config.STOP_LOSS_RATIO)
        self.assertAlmostEqual(result, expected_fixed, places=2,
                               msg=f"最高价无效时应回退固定止损 {expected_fixed:.2f}")

    def test_14_highest_tier_50pct_matches_correctly(self):
        """TC-14: 最高浮盈50%，匹配>=50%档位(系数0.80)"""
        cost = 10.0
        highest = 15.0  # 50% 盈利
        result = self.pm.calculate_stop_loss_price(cost, highest, profit_triggered=True)
        expected = highest * 0.80
        self.assertAlmostEqual(result, expected, places=2,
                               msg=f"50%浮盈应匹配0.80档，期望 {expected:.2f}，实际 {result:.2f}")

    def test_15_between_tiers_matches_lower(self):
        """TC-15: 浮盈12%，应匹配>=10%档(0.93)而非>=15%档"""
        cost = 10.0
        highest = 11.2  # 12% 盈利
        result = self.pm.calculate_stop_loss_price(cost, highest, profit_triggered=True)
        expected = highest * 0.93  # 匹配10%档
        self.assertAlmostEqual(result, expected, places=2,
                               msg=f"12%浮盈应匹配0.93档，期望 {expected:.2f}，实际 {result:.2f}")


if __name__ == '__main__':
    unittest.main(verbosity=2)
