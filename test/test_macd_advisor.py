"""macd_advisor.classify 纯逻辑单测。

覆盖决策矩阵四象限 + 边界(DEA 持平、DEA=0、数据无效)。
"""
import unittest

import macd_advisor


class TestMacdClassify(unittest.TestCase):
    # ---- 四象限 ----
    def test_strong_up(self):
        """DEA 向上 + 0 轴以上 → 上升趋势(强)/重仓/启动"""
        r = macd_advisor.classify(dea_prev=0.5, dea_last=0.8, dif_last=1.0)
        self.assertEqual(r["trend"], "上升趋势(强)")
        self.assertEqual(r["base_position"], "重仓")
        self.assertEqual(r["grid"], "启动")

    def test_weak_up_repair(self):
        """DEA 向上 + 0 轴以下 → 上升趋势(弱/修复)/半仓以下/启动"""
        r = macd_advisor.classify(dea_prev=-0.8, dea_last=-0.5, dif_last=-0.4)
        self.assertEqual(r["trend"], "上升趋势(弱/修复)")
        self.assertEqual(r["base_position"], "半仓以下")
        self.assertEqual(r["grid"], "启动")

    def test_weak_down_top(self):
        """DEA 向下 + 0 轴以上 → 下降趋势(弱)/顶部反转/半仓以下/启动"""
        r = macd_advisor.classify(dea_prev=0.8, dea_last=0.5, dif_last=0.4)
        self.assertEqual(r["trend"], "下降趋势(弱)/顶部反转")
        self.assertEqual(r["base_position"], "半仓以下")
        self.assertEqual(r["grid"], "启动")

    def test_strong_down(self):
        """DEA 向下 + 0 轴以下 → 下降趋势(强)/清仓/停用"""
        r = macd_advisor.classify(dea_prev=-0.5, dea_last=-0.8, dif_last=-1.0)
        self.assertEqual(r["trend"], "下降趋势(强)")
        self.assertEqual(r["base_position"], "清仓")
        self.assertEqual(r["grid"], "停用")

    # ---- 边界 ----
    def test_dea_flat_treated_as_up(self):
        """DEA 持平(dea_last == dea_prev) 按向上处理"""
        r = macd_advisor.classify(dea_prev=0.5, dea_last=0.5, dif_last=0.6)
        self.assertEqual(r["base_position"], "重仓")

    def test_dea_zero_is_below_axis(self):
        """DEA == 0 视为 0 轴以下(> 0 才算以上)；向下→清仓"""
        r = macd_advisor.classify(dea_prev=0.2, dea_last=0.0, dif_last=-0.1)
        self.assertEqual(r["base_position"], "清仓")
        self.assertEqual(r["grid"], "停用")

    def test_none_input_returns_none(self):
        self.assertIsNone(macd_advisor.classify(None, 0.5, 0.5))
        self.assertIsNone(macd_advisor.classify(0.5, None, 0.5))

    def test_invalid_input_returns_none(self):
        self.assertIsNone(macd_advisor.classify("x", 0.5, 0.5))

    # ---- 金叉/死叉补充说明 ----
    def test_cross_bullish(self):
        r = macd_advisor.classify(dea_prev=0.5, dea_last=0.8, dif_last=1.0)
        self.assertIn("多头", r["cross"])

    def test_cross_bearish(self):
        r = macd_advisor.classify(dea_prev=0.5, dea_last=0.8, dif_last=0.6)
        self.assertIn("空头", r["cross"])

    def test_cross_empty_when_dif_none(self):
        r = macd_advisor.classify(dea_prev=0.5, dea_last=0.8, dif_last=None)
        self.assertEqual(r["cross"], "")


class TestIsIndexCode(unittest.TestCase):
    def test_shenzhen_index(self):
        self.assertTrue(macd_advisor._is_index_code("399001.SZ"))

    def test_stock_not_index(self):
        self.assertFalse(macd_advisor._is_index_code("002440.SZ"))

    def test_shanghai_index(self):
        self.assertTrue(macd_advisor._is_index_code("000001.SH"))


if __name__ == "__main__":
    unittest.main()
