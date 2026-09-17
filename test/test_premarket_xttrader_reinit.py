"""
盘前 xttrader 重连逻辑测试 (ping-first 行为)

修复背景:
  旧逻辑: reinit_xtquant_trader() 无条件调用 connect()
          connect() 先 stop() 现有连接，再立即重连
          QMT 进程来不及处理新连接请求 → connect() 返回 -1

  新逻辑: 先用 ping_xttrader() 探测现有连接
          - 若连通 (ping=True)  → 跳过 connect()，保留现有连接
          - 若断连 (ping=False) → 调用 connect() 重建连接

测试场景:
  TC1: QMT 正在运行，xttrader 已连通 → 跳过 connect()，确认主推订阅，qmt_connected=True
  TC2: QMT 断连 → 调用 connect()，connect 成功 → qmt_connected=True，回调重注册
  TC3: QMT 断连 → 调用 connect()，connect 失败 → qmt_connected=False，返回 False
  TC4: ping 抛异常 → 当作断连处理，继续尝试 connect()
  TC5: connect() 抛异常 → qmt_connected=False，返回 False
  TC6: qmt_trader 未初始化 → 跳过，返回 True（兜底）
  TC7: position_manager 无 _on_trade_callback → 跳过 trade_callback 注册，不崩溃
  TC8: position_manager 无 _on_qmt_disconnect → 跳过 disconnect_callback 注册，不崩溃
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config


class TestReinitXtquantTrader(unittest.TestCase):
    """reinit_xtquant_trader() 新 ping-first 行为的单元测试"""

    def _make_pm(self, ping_result=True, connect_result='tuple',
                 has_trade_cb=True, has_order_cb=True, has_disconnect_cb=True,
                 ensure_push_result=True):
        """
        构造 mock position_manager + qmt_trader 对象。

        参数:
            ping_result:      ping_xttrader() 的返回值 (bool 或 Exception)
            connect_result:   'tuple'=连接成功, None=连接失败
            has_trade_cb:     是否有 _on_trade_callback 属性
            has_order_cb:     是否有 _on_order_callback 属性
            has_disconnect_cb: 是否有 _on_qmt_disconnect 属性
        """
        pm = MagicMock()
        pm.qmt_connected = True  # 初始状态

        qmt_trader = MagicMock()

        if isinstance(ping_result, Exception):
            qmt_trader.ping_xttrader.side_effect = ping_result
        else:
            qmt_trader.ping_xttrader.return_value = ping_result

        if connect_result == 'tuple':
            qmt_trader.connect.return_value = (MagicMock(), MagicMock())  # 成功返回 (xt_trader, acc)
        else:
            qmt_trader.connect.return_value = connect_result  # None = 失败
        qmt_trader.ensure_trade_push_ready.return_value = ensure_push_result

        pm.qmt_trader = qmt_trader

        if has_trade_cb:
            pm._on_trade_callback = MagicMock()
        else:
            # 确保 hasattr 返回 False
            del pm._on_trade_callback

        if has_order_cb:
            pm._on_order_callback = MagicMock()
        else:
            del pm._on_order_callback

        if has_disconnect_cb:
            pm._on_qmt_disconnect = MagicMock()
        else:
            del pm._on_qmt_disconnect

        return pm, qmt_trader

    def _run_reinit(self, pm):
        """调用 reinit_xtquant_trader()，注入 mock position_manager"""
        # 注意: reinit_xtquant_trader() 内部用局部 import 导入 get_position_manager
        # 因此需要 patch 源模块 position_manager.get_position_manager
        with patch('position_manager.get_position_manager', return_value=pm):
            from premarket_sync import reinit_xtquant_trader
            return reinit_xtquant_trader()

    # ------------------------------------------------------------------
    # TC1: 连接正常时跳过 connect()
    # ------------------------------------------------------------------
    def test_tc1_ping_ok_skips_connect(self):
        """TC1: xttrader 已连通 → 不调用 connect()，但必须确认主推订阅和回调"""
        pm, qmt_trader = self._make_pm(ping_result=True)

        result = self._run_reinit(pm)

        self.assertTrue(result, "连接正常时应返回 True")
        qmt_trader.connect.assert_not_called()
        qmt_trader.ensure_trade_push_ready.assert_called_once()
        qmt_trader.register_trade_callback.assert_called_once()
        qmt_trader.register_order_callback.assert_called_once()
        qmt_trader.register_disconnect_callback.assert_called_once()
        self.assertTrue(pm.qmt_connected, "qmt_connected 应为 True")

    def test_tc1b_ping_ok_but_push_not_ready_returns_false(self):
        """TC1b: ping 成功但交易主推不可用 → 不应假健康。"""
        pm, qmt_trader = self._make_pm(ping_result=True, ensure_push_result=False)

        result = self._run_reinit(pm)

        self.assertFalse(result, "主推订阅失败时应返回 False")
        qmt_trader.connect.assert_not_called()
        qmt_trader.ensure_trade_push_ready.assert_called_once()
        qmt_trader.register_trade_callback.assert_not_called()
        self.assertFalse(pm.qmt_connected, "主推未恢复时 qmt_connected 必须为 False")

    # ------------------------------------------------------------------
    # TC2: 连接断开时调用 connect()，重连成功
    # ------------------------------------------------------------------
    def test_tc2_ping_fail_connect_success(self):
        """TC2: xttrader 断连 + connect() 成功 → qmt_connected=True，回调被重新注册"""
        pm, qmt_trader = self._make_pm(ping_result=False, connect_result='tuple')
        pm.qmt_connected = False  # 初始断连

        result = self._run_reinit(pm)

        self.assertTrue(result, "重连成功时应返回 True")
        qmt_trader.connect.assert_called_once()
        self.assertTrue(pm.qmt_connected, "重连成功后 qmt_connected 应为 True")
        # 验证回调被重新注册
        qmt_trader.register_trade_callback.assert_called_once()
        qmt_trader.register_order_callback.assert_called_once()
        qmt_trader.register_disconnect_callback.assert_called_once()

    # ------------------------------------------------------------------
    # TC3: 连接断开时调用 connect()，重连失败
    # ------------------------------------------------------------------
    def test_tc3_ping_fail_connect_fail(self):
        """TC3: xttrader 断连 + connect() 失败(返回None) → qmt_connected=False，返回 False"""
        pm, qmt_trader = self._make_pm(ping_result=False, connect_result=None)
        pm.qmt_connected = True  # 初始标记为 True（旧 bug 场景）

        result = self._run_reinit(pm)

        self.assertFalse(result, "重连失败时应返回 False")
        qmt_trader.connect.assert_called_once()
        self.assertFalse(pm.qmt_connected, "重连失败后 qmt_connected 必须被置为 False")
        # 连接失败时不应注册回调
        qmt_trader.register_trade_callback.assert_not_called()
        qmt_trader.register_disconnect_callback.assert_not_called()

    # ------------------------------------------------------------------
    # TC4: ping 抛异常 → 当作断连处理，继续尝试 connect()
    # ------------------------------------------------------------------
    def test_tc4_ping_exception_treated_as_disconnect(self):
        """TC4: ping_xttrader 抛异常 → 按断连处理，尝试 connect()"""
        pm, qmt_trader = self._make_pm(
            ping_result=Exception("ping 内部异常"),
            connect_result='tuple'
        )

        result = self._run_reinit(pm)

        self.assertTrue(result, "ping 异常后 connect 成功应返回 True")
        qmt_trader.connect.assert_called_once()
        self.assertTrue(pm.qmt_connected, "qmt_connected 应为 True")

    # ------------------------------------------------------------------
    # TC5: connect() 抛异常 → qmt_connected=False，返回 False
    # ------------------------------------------------------------------
    def test_tc5_connect_exception(self):
        """TC5: connect() 抛异常 → qmt_connected=False，返回 False"""
        pm, qmt_trader = self._make_pm(ping_result=False)
        qmt_trader.connect.side_effect = RuntimeError("QMT 内部错误")
        pm.qmt_connected = True  # 初始标记

        result = self._run_reinit(pm)

        self.assertFalse(result, "connect 抛异常时应返回 False")
        self.assertFalse(pm.qmt_connected, "connect 抛异常后 qmt_connected 必须为 False")

    # ------------------------------------------------------------------
    # TC6: qmt_trader 未初始化（None）→ 跳过，返回 True
    # ------------------------------------------------------------------
    def test_tc6_no_qmt_trader(self):
        """TC6: qmt_trader 为 None → 跳过初始化，返回 True"""
        pm = MagicMock()
        pm.qmt_trader = None

        with patch('position_manager.get_position_manager', return_value=pm):
            from premarket_sync import reinit_xtquant_trader
            result = reinit_xtquant_trader()

        self.assertTrue(result, "qmt_trader 未初始化时应返回 True（兜底）")

    # ------------------------------------------------------------------
    # TC7: 没有 _on_trade_callback → 跳过注册，不崩溃
    # ------------------------------------------------------------------
    def test_tc7_no_trade_callback_no_crash(self):
        """TC7: pm 无 _on_trade_callback → 跳过 trade_callback 注册，不崩溃"""
        pm, qmt_trader = self._make_pm(
            ping_result=False,
            connect_result='tuple',
            has_trade_cb=False,
            has_order_cb=True,
            has_disconnect_cb=True
        )

        result = self._run_reinit(pm)

        self.assertTrue(result, "无 trade_callback 时应正常返回 True")
        qmt_trader.register_trade_callback.assert_not_called()
        qmt_trader.register_order_callback.assert_called_once()
        # disconnect_callback 仍应注册
        qmt_trader.register_disconnect_callback.assert_called_once()

    # ------------------------------------------------------------------
    # TC8: 没有 _on_qmt_disconnect → 跳过注册，不崩溃
    # ------------------------------------------------------------------
    def test_tc8_no_disconnect_callback_no_crash(self):
        """TC8: pm 无 _on_qmt_disconnect → 跳过 disconnect_callback 注册，不崩溃"""
        pm, qmt_trader = self._make_pm(
            ping_result=False,
            connect_result='tuple',
            has_trade_cb=True,
            has_order_cb=True,
            has_disconnect_cb=False
        )

        result = self._run_reinit(pm)

        self.assertTrue(result, "无 disconnect_callback 时应正常返回 True")
        qmt_trader.register_trade_callback.assert_called_once()
        qmt_trader.register_order_callback.assert_called_once()
        qmt_trader.register_disconnect_callback.assert_not_called()

    # ------------------------------------------------------------------
    # TC9: 关键回归 - 原 bug 复现场景
    #      QMT 持续运行 → 旧逻辑调用 connect() 导致连接断开
    #      新逻辑: ping=True → 跳过 connect() → 连接保持
    # ------------------------------------------------------------------
    def test_tc9_regression_qmt_running_no_connect_called(self):
        """
        TC9 (回归): QMT 正常运行时，盘前初始化不应调用 connect()
        这是修复前出现 connect()=-1 的根本原因复现
        """
        pm, qmt_trader = self._make_pm(ping_result=True)

        result = self._run_reinit(pm)

        self.assertTrue(result)
        # 核心断言：connect 绝不能被调用（否则会终止正常连接）
        qmt_trader.connect.assert_not_called()
        # stop() 也不应被调用
        qmt_trader.xt_trader.stop.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
