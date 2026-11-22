"""开盘总资产双源核对：使用内存持仓与模拟柜台，禁止连接真实 QMT。"""
import concurrent.futures
import logging
import sqlite3
import threading
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import config
from position_manager import PositionManager


class TestAssetConsistencyMonitor(unittest.TestCase):
    def setUp(self):
        patches = [
            patch.object(config, 'ENABLE_SIMULATION_MODE', True),
            patch.object(config, 'ENABLE_XTQUANT_MANAGER', False),
            patch.object(config, 'ENABLE_QMT_IPC_FALLBACK', False),
            patch.object(config, 'ENABLE_QMT_RPC_FALLBACK', False),
            patch.object(config, 'is_continuous_trade_time', return_value=True),
            patch.object(config, 'is_trade_time', return_value=True),
            patch('position_manager.log_throttled'),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.pm = PositionManager.__new__(PositionManager)
        self.pm._reconnect_lock = threading.Lock()
        self.pm._reconnect_in_progress = False
        self.pm._asset_monitor_last_check = None
        self.pm._asset_monitor_abnormal = False
        self.pm.qmt_connected = True
        self.pm.memory_conn_lock = threading.Lock()
        self.pm.memory_conn = sqlite3.connect(':memory:', check_same_thread=False)
        self.addCleanup(self.pm.memory_conn.close)
        self.pm.memory_conn.execute('CREATE TABLE positions (stock_code TEXT, volume INTEGER)')
        self.pm.memory_conn.execute("INSERT INTO positions VALUES ('603466', 5000)")
        self.pm.qmt_trader = Mock()
        self.pm.data_manager = Mock()
        self.pm.qmt_trader.ping_xttrader.return_value = True
        self.pm.qmt_trader.ensure_trade_push_ready.return_value = True
        self._set_evidence()
        # 仅切换隔离对象的待测分支，所有柜台调用均已模拟。
        config.ENABLE_SIMULATION_MODE = False

    def _set_evidence(self, price=10.0, total=100000.0, cash=50000.0, frozen=0.0):
        import pandas as pd
        self.pm.qmt_trader.balance.return_value = pd.DataFrame([{
            '资金账户': 'TEST', '账号类型': 'STOCK', '可用金额': cash,
            '冻结金额': frozen, '持仓市值': 50000.0, '总资产': total,
        }])
        now = datetime.now()
        self.pm._position_restrictions = {
            '603466': {'position_asof': now.isoformat(), 'quantities': (5000,)}
        }
        self.pm.data_manager.get_cached_quotes.return_value = {
            '603466': {'lastPrice': price, 'time': int(now.timestamp() * 1000)}
        }

    def test_deviation_threshold_in_both_directions(self):
        """等于 3% 不报警，两个方向超过 3% 均标记异常。"""
        for price, abnormal in [(10.0, False), (10.6, False), (9.4, False),
                                (10.62, True), (9.38, True)]:
            with self.subTest(price=price):
                self.pm._asset_monitor_last_check = None
                self.pm._asset_monitor_abnormal = False
                self.pm.qmt_connected = True
                self._set_evidence(price=price)
                self.pm._monitor_total_asset_consistency()
                self.assertEqual(self.pm._asset_monitor_abnormal, abnormal)
                self.assertTrue(self.pm.qmt_connected)

    def test_includes_frozen_cash_in_total_assets(self):
        """未成交挂单冻结的资金仍属于账户总资产。"""
        self._set_evidence(cash=40000, frozen=10000)
        with patch('position_manager.log_throttled') as alert:
            self.pm._monitor_total_asset_consistency()
        self.assertTrue(self.pm.qmt_connected)
        self.assertFalse(self.pm._asset_monitor_abnormal)
        alert.assert_not_called()

    def test_ignores_broker_market_value(self):
        frame = self.pm.qmt_trader.balance.return_value
        frame.loc[0, '持仓市值'] = 999999
        self.pm._monitor_total_asset_consistency()
        self.assertTrue(self.pm.qmt_connected)

    def test_invalid_or_missing_evidence_does_not_mark_disconnected(self):
        """无效数据应提示无法核对，不能按零市值误报断连。"""
        for kind in ('missing_quote', 'stale_quote', 'stale_position', 'zero_price',
                     'nan_price', 'zero_total', 'nan_total', 'missing_account'):
            with self.subTest(kind=kind):
                self.pm._asset_monitor_last_check = None
                self._set_evidence()
                quote = self.pm.data_manager.get_cached_quotes.return_value['603466']
                old = datetime.now() - timedelta(seconds=config.ASSET_VALUATION_MAX_AGE_SECONDS + 10)
                if kind == 'missing_quote':
                    self.pm.data_manager.get_cached_quotes.return_value = {}
                elif kind == 'stale_quote':
                    quote['time'] = int(old.timestamp() * 1000)
                elif kind == 'stale_position':
                    self.pm._position_restrictions['603466']['position_asof'] = old.isoformat()
                elif kind == 'zero_price':
                    quote['lastPrice'] = 0
                elif kind == 'nan_price':
                    quote['lastPrice'] = float('nan')
                elif kind == 'zero_total':
                    self.pm.qmt_trader.balance.return_value.loc[0, '总资产'] = 0
                elif kind == 'nan_total':
                    self.pm.qmt_trader.balance.return_value.loc[0, '总资产'] = float('nan')
                else:
                    self.pm.qmt_trader.balance.return_value = None
                self.pm._monitor_total_asset_consistency()
                self.assertTrue(self.pm.qmt_connected)
                self.assertFalse(self.pm._asset_monitor_abnormal)

    def test_partial_quote_coverage_is_unavailable(self):
        self.pm.memory_conn.execute("INSERT INTO positions VALUES ('300792', 1000)")
        self.pm._position_restrictions['300792'] = {
            'position_asof': datetime.now().isoformat(), 'quantities': (1000,)
        }
        self.pm._monitor_total_asset_consistency()
        self.assertTrue(self.pm.qmt_connected)
        self.assertFalse(self.pm._asset_monitor_abnormal)

    def test_empty_portfolio_includes_frozen_cash(self):
        self.pm.memory_conn.execute('DELETE FROM positions')
        self._set_evidence(cash=90000, frozen=10000)
        self.pm._monitor_total_asset_consistency()
        self.assertTrue(self.pm.qmt_connected)

    def test_skips_simulation_other_backends_and_closed_market(self):
        for name in ('ENABLE_SIMULATION_MODE', 'ENABLE_XTQUANT_MANAGER',
                     'ENABLE_QMT_IPC_FALLBACK', 'ENABLE_QMT_RPC_FALLBACK'):
            with self.subTest(name=name), patch.object(config, name, True):
                self.pm._monitor_total_asset_consistency()
        with patch.object(config, 'is_continuous_trade_time', return_value=False):
            self.pm._monitor_total_asset_consistency()
        self.pm.qmt_trader.balance.assert_not_called()

    def test_checks_at_configured_interval(self):
        with patch('position_manager.time.monotonic', return_value=1000):
            self.pm._monitor_total_asset_consistency()
        with patch('position_manager.time.monotonic',
                   return_value=1000 + config.ASSET_CONSISTENCY_CHECK_INTERVAL_SECONDS - 1):
            self.pm._monitor_total_asset_consistency()
        self.pm.qmt_trader.balance.assert_called_once()
        with patch('position_manager.time.monotonic',
                   return_value=1000 + config.ASSET_CONSISTENCY_CHECK_INTERVAL_SECONDS):
            self.pm._monitor_total_asset_consistency()
        self.assertEqual(self.pm.qmt_trader.balance.call_count, 2)

    def test_asset_warning_does_not_block_connection_probe(self):
        self._set_evidence(price=11.0)
        self.pm._monitor_total_asset_consistency()
        self.assertTrue(self.pm._probe_qmt_recovered())
        self.pm.qmt_trader.ping_xttrader.assert_called_once()
        self.assertTrue(self.pm._asset_monitor_abnormal)

    def test_unknown_evidence_preserves_previous_abnormality(self):
        self._set_evidence(price=11.0)
        self.pm._monitor_total_asset_consistency()
        self.pm._asset_monitor_last_check = None
        self.pm.data_manager.get_cached_quotes.return_value = {}
        self.pm._monitor_total_asset_consistency()
        self.assertTrue(self.pm._asset_monitor_abnormal)
        self.assertTrue(self.pm.qmt_connected)

    def test_rechecks_before_recovery(self):
        self._set_evidence(price=11.0)
        self.pm._monitor_total_asset_consistency()
        self.pm._asset_monitor_last_check = None
        self._set_evidence()
        self.pm._monitor_total_asset_consistency()
        self.assertFalse(self.pm._asset_monitor_abnormal)
        self.assertTrue(self.pm._probe_qmt_recovered())

    def test_alert_contains_both_asset_totals_and_deviation(self):
        self._set_evidence(price=11.0)
        with patch('position_manager.log_throttled') as alert:
            self.pm._monitor_total_asset_consistency()
        self.assertEqual(alert.call_args.args[1], logging.WARNING)
        message = alert.call_args.args[3]
        self.assertIn('总资产核对偏差超限', message)
        self.assertIn('冻结金额=0.00', message)
        self.assertNotIn('已标记断连', message)
        self.assertIn('柜台总资产=100000.00', message)
        self.assertIn('重算总资产=105000.00', message)
        self.assertIn('偏差=5.00%', message)
        self.assertIn('阈值=3.00%', message)

    def test_uses_total_volume_including_unavailable_shares(self):
        self.pm.memory_conn.execute('ALTER TABLE positions ADD COLUMN available INTEGER')
        self.pm.memory_conn.execute('UPDATE positions SET available=1000')
        self.pm._monitor_total_asset_consistency()
        self.assertTrue(self.pm.qmt_connected)
        self.assertFalse(self.pm._asset_monitor_abnormal)

    def test_threshold_is_read_from_config(self):
        self._set_evidence(price=10.8)
        with patch.object(config, 'ASSET_CONSISTENCY_MAX_DEVIATION_RATIO', 0.05):
            self.pm._monitor_total_asset_consistency()
        self.assertTrue(self.pm.qmt_connected)

    def test_check_interval_does_not_change_connection_after_warning(self):
        self._set_evidence(price=11.0)
        with patch('position_manager.time.monotonic', return_value=1000):
            self.pm._monitor_total_asset_consistency()
        self.pm.qmt_connected = True
        with patch('position_manager.time.monotonic', return_value=1001):
            self.pm._monitor_total_asset_consistency()
        self.assertTrue(self.pm.qmt_connected)
        self.assertTrue(self.pm._probe_qmt_recovered())

    def test_reconnect_worker_recovers_despite_asset_warning(self):
        self.pm._asset_monitor_abnormal = True
        self.pm.qmt_connected = False
        self.pm.qmt_trader.reconnect_xttrader.return_value = True
        self.pm._start_qmt_connect_worker()
        self.pm._reconnect_thread.join(timeout=5)
        self.assertFalse(self.pm._reconnect_thread.is_alive())
        self.assertTrue(self.pm.qmt_connected)
        self.assertTrue(self.pm._asset_monitor_abnormal)
        self.assertEqual(self.pm.last_position_update_time, 0)

    def _run_monitor_cycle(self):
        """运行一轮真实监控流程，交易和数据接口全部使用模拟对象。"""
        import pandas as pd
        self.pm.stop_flag = False
        self.pm.last_update_highest_time = float('inf')
        self.pm.update_highest_interval = 60
        self.pm.get_all_positions = Mock(return_value=pd.DataFrame([{
            'stock_code': '603466', 'highest_price': 12.0,
        }]))
        self.pm._maybe_reconcile_missing_trades = Mock()
        self.pm.check_pending_orders_timeout = Mock()
        self.pm._detect_and_enqueue_dynamic_signal = Mock()
        self.pm._has_tracked_pending_order = Mock(return_value=False)
        self.pm.grid_manager = Mock()
        self.pm.grid_manager.check_grid_signals.return_value = None
        self.pm.data_manager.get_latest_data.return_value = {'lastPrice': 11.0}
        self.pm._last_reconnect_time = 0
        self.pm._attempt_qmt_reconnect = Mock()
        with patch('position_manager.run_with_timeout', side_effect=lambda func, timeout: func()), \
             patch.object(config, 'is_global_monitor_enabled', return_value=True), \
             patch('position_manager.time.sleep', side_effect=lambda seconds: setattr(self.pm, 'stop_flag', True)):
            self.pm._position_monitor_loop()

    def test_monitor_loop_continues_signals_and_reconciliation_on_mismatch(self):
        """估值偏差只告警，不跳过成交流水对账或交易信号检测。"""
        self._set_evidence(price=11.0)
        self._run_monitor_cycle()
        self.assertTrue(self.pm.qmt_connected)
        self.pm._attempt_qmt_reconnect.assert_not_called()
        self.pm.qmt_trader.ping_xttrader.assert_not_called()
        self.pm.grid_manager.check_grid_signals.assert_called_once()
        self.pm._maybe_reconcile_missing_trades.assert_called_once()
        self.pm._detect_and_enqueue_dynamic_signal.assert_called_once()

    def test_valuation_errors_and_timeouts_only_warn(self):
        """估值任务超时或异常不能累计为持仓查询失败。"""
        for error in (concurrent.futures.TimeoutError(), RuntimeError('估值失败')):
            with self.subTest(error=type(error).__name__), \
                 patch.object(self.pm, '_monitor_total_asset_consistency', side_effect=error), \
                 patch('position_manager.log_throttled') as alert:
                self._run_monitor_cycle()
                self.assertTrue(self.pm.qmt_connected)
                self.pm._attempt_qmt_reconnect.assert_not_called()
                self.pm._maybe_reconcile_missing_trades.assert_called_once()
                self.pm.grid_manager.check_grid_signals.assert_called_once()
                self.assertEqual(alert.call_args.args[1], logging.WARNING)

    def test_real_disconnection_still_blocks_signals(self):
        self.pm.qmt_connected = False
        self.pm.qmt_trader.ping_xttrader.return_value = False
        self._run_monitor_cycle()
        self.assertFalse(self.pm.qmt_connected)
        self.pm.grid_manager.check_grid_signals.assert_not_called()
        self.pm._maybe_reconcile_missing_trades.assert_not_called()

    def test_warning_does_not_restore_a_disconnected_connection(self):
        self.pm.qmt_connected = False
        self._set_evidence(price=11.0)
        self.pm._monitor_total_asset_consistency()
        self.assertFalse(self.pm.qmt_connected)
        self.assertTrue(self.pm._asset_monitor_abnormal)

    def test_reconnect_in_progress_only_skips_valuation(self):
        self.pm._reconnect_in_progress = True
        self.pm._asset_monitor_abnormal = True
        self.pm._monitor_total_asset_consistency()
        self.assertTrue(self.pm.qmt_connected)
        self.pm.qmt_trader.balance.assert_not_called()

    def test_external_buy_snapshot_gap_only_warns(self):
        """复现今日买入后本地持仓尚未同步的数值。"""
        self._set_evidence(price=132582 / 5000, total=502098.17, cash=326925.17)
        with patch('position_manager.log_throttled') as alert:
            self.pm._monitor_total_asset_consistency()
        self.assertTrue(self.pm.qmt_connected)
        self.assertTrue(self.pm._asset_monitor_abnormal)
        self.assertIn('差额=42591.00', alert.call_args.args[3])
        self.assertEqual(alert.call_args.args[1], logging.WARNING)


if __name__ == '__main__':
    unittest.main()
