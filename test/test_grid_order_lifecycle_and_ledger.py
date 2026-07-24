"""
网格交易订单生命周期与真实账本测试

覆盖:
1. 停止策略时对未完成网格委托发起撤单，并等待终态后再清理会话
2. execute_grid_trade 在锁外调用交易执行器，避免 QMT 卡顿阻塞网格全局锁
3. 真实网格账本按 LIFO lot 匹配，退出盈亏优先使用账本口径
"""

import os
import sys
import threading
import time
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta
from unittest.mock import Mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import config
from grid_database import DatabaseManager
from grid_trading_manager import GridSession, GridTradingManager, PriceTracker


class FakeTrade:
    def __init__(self, order_id, stock_code='000001.SZ', volume=100, price=10.0, trade_id='DEAL_1'):
        self.order_id = order_id
        self.stock_code = stock_code
        self.traded_volume = volume
        self.traded_price = price
        self.trade_id = trade_id


class FakeOrderInfo:
    def __init__(self, order_id, status=54):
        self.order_id = order_id
        self.order_sysid = order_id
        self.m_strOrderSysID = order_id
        self.order_status = status
        self.m_nOrderStatus = status


class GridLifecycleLedgerBase(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseManager(':memory:')
        self.db.init_grid_tables()
        self.position_manager = Mock()
        self.position_manager.signal_lock = threading.Lock()
        self.position_manager.latest_signals = {}
        self.position_manager._increment_data_version = Mock()
        self.position_manager.data_manager = Mock()
        self.position_manager.data_manager.get_latest_data.return_value = {'lastPrice': 10.0}
        self.position_manager.get_position.return_value = {
            'stock_code': '000001.SZ',
            'volume': 1000,
            'available': 1000,
            'cost_price': 10.0,
            'current_price': 10.0,
            'profit_triggered': True,
        }
        self.executor = Mock()
        self.manager = GridTradingManager(self.db, self.position_manager, self.executor)

        self.orig_sim = config.ENABLE_SIMULATION_MODE
        self.orig_confirm = getattr(config, 'GRID_CONFIRM_LIVE_ORDER_BY_DEAL', True)
        self.orig_age = getattr(config, 'GRID_SIGNAL_MAX_AGE_SECONDS', 60)
        self.orig_drift = getattr(config, 'GRID_SIGNAL_MAX_PRICE_DRIFT_RATIO', 0.01)
        self.orig_guard = getattr(config, 'GRID_ENABLE_PRICE_LIMIT_GUARD', True)
        self.orig_buy_cooldown = getattr(config, 'GRID_BUY_COOLDOWN', 300)
        self.orig_sell_cooldown = getattr(config, 'GRID_SELL_COOLDOWN', 300)
        config.GRID_SIGNAL_MAX_AGE_SECONDS = 60
        config.GRID_SIGNAL_MAX_PRICE_DRIFT_RATIO = 0.20
        config.GRID_ENABLE_PRICE_LIMIT_GUARD = False
        config.GRID_BUY_COOLDOWN = 0
        config.GRID_SELL_COOLDOWN = 0

    def tearDown(self):
        config.ENABLE_SIMULATION_MODE = self.orig_sim
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = self.orig_confirm
        config.GRID_SIGNAL_MAX_AGE_SECONDS = self.orig_age
        config.GRID_SIGNAL_MAX_PRICE_DRIFT_RATIO = self.orig_drift
        config.GRID_ENABLE_PRICE_LIMIT_GUARD = self.orig_guard
        config.GRID_BUY_COOLDOWN = self.orig_buy_cooldown
        config.GRID_SELL_COOLDOWN = self.orig_sell_cooldown
        self.db.close()

    def make_session(self, max_investment=10000, current_investment=0, position_ratio=0.25):
        session = GridSession(
            id=None,
            stock_code='000001.SZ',
            status='active',
            center_price=10.0,
            current_center_price=10.0,
            price_interval=0.05,
            position_ratio=position_ratio,
            callback_ratio=0.005,
            max_investment=max_investment,
            current_investment=current_investment,
            start_time=datetime.now(),
            end_time=datetime.now() + timedelta(days=7),
        )
        data = asdict(session)
        data['start_time'] = session.start_time.isoformat()
        data['end_time'] = session.end_time.isoformat()
        session.id = self.db.create_grid_session(data)
        self.manager.sessions[self.manager._normalize_code(session.stock_code)] = session
        self.manager.trackers[session.id] = PriceTracker(session_id=session.id, last_price=10.0)
        return session

    def buy_signal(self, session, price=10.0):
        return {
            'stock_code': session.stock_code,
            'strategy': config.GRID_STRATEGY_NAME,
            'signal_type': 'BUY',
            'grid_level': 9.5,
            'trigger_price': price,
            'session_id': session.id,
            'timestamp': datetime.now().isoformat(),
            'signal_source': 'grid_tracker',
            'require_price_recheck': True,
            'valley_price': price * 0.99,
            'callback_ratio': 0.005,
        }

    def sell_signal(self, session, price=10.5):
        return {
            'stock_code': session.stock_code,
            'strategy': config.GRID_STRATEGY_NAME,
            'signal_type': 'SELL',
            'grid_level': 10.5,
            'trigger_price': price,
            'session_id': session.id,
            'timestamp': datetime.now().isoformat(),
            'signal_source': 'grid_tracker',
            'require_price_recheck': True,
            'peak_price': price * 1.01,
            'callback_ratio': 0.005,
        }


class TestGridStopCancelClosure(GridLifecycleLedgerBase):
    def test_stop_requests_cancel_and_completes_after_order_terminal(self):
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self.make_session()
        self.executor.buy_stock.return_value = {'order_id': 'ORDER_STOP_1'}
        self.executor.cancel_order.return_value = True

        self.assertTrue(self.manager.execute_grid_trade(self.buy_signal(session)))
        self.assertIn('ORDER_STOP_1', self.manager.pending_grid_orders)

        stats = self.manager.stop_grid_session(session.id, 'manual')

        self.assertEqual(stats['status'], 'stopping')
        self.executor.cancel_order.assert_called_once_with('ORDER_STOP_1')
        self.assertEqual(session.status, 'stopping')
        self.assertIn('ORDER_STOP_1', self.manager.pending_grid_orders)
        self.assertEqual(self.db.get_grid_order('ORDER_STOP_1')['status'], 'cancel_requested')

        self.assertTrue(self.manager.handle_order_callback(FakeOrderInfo('ORDER_STOP_1', status=54)))

        self.assertNotIn('ORDER_STOP_1', self.manager.pending_grid_orders)
        self.assertNotIn(self.manager._normalize_code(session.stock_code), self.manager.sessions)
        self.assertEqual(self.db.get_grid_session(session.id)['status'], 'stopped')
        self.assertEqual(self.db.get_grid_order('ORDER_STOP_1')['status'], 'canceled')

    def test_stopping_response_returns_unified_pnl_snapshot(self):
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self.make_session(max_investment=1000)
        session.buy_count = 1
        session.sell_count = 1
        session.total_buy_amount = 1000.0
        session.total_sell_amount = 1000.0
        session.total_buy_volume = 100
        session.total_sell_volume = 100
        session.current_center_price = 11.0
        self.executor.cancel_order.return_value = True

        now = datetime.now().isoformat()
        self.db.record_grid_trade_and_update_session(
            {
                'session_id': session.id,
                'stock_code': session.stock_code,
                'trade_type': 'BUY',
                'grid_level': 9.5,
                'trigger_price': 10.0,
                'volume': 100,
                'amount': 1000.0,
                'trade_id': 'STOPPING_BUY',
                'trade_time': now,
                'grid_center_before': 10.0,
                'grid_center_after': 10.0,
            },
            {'trade_count': 1, 'buy_count': 1, 'total_buy_amount': 1000.0, 'total_buy_volume': 100, 'current_investment': 1000.0}
        )
        self.db.record_grid_trade_and_update_session(
            {
                'session_id': session.id,
                'stock_code': session.stock_code,
                'trade_type': 'SELL',
                'grid_level': 10.5,
                'trigger_price': 11.0,
                'volume': 100,
                'amount': 1100.0,
                'trade_id': 'STOPPING_SELL',
                'trade_time': now,
                'grid_center_before': 10.0,
                'grid_center_after': 11.0,
            },
            {'trade_count': 2, 'sell_count': 1, 'total_sell_amount': 1100.0, 'total_sell_volume': 100, 'current_investment': 0.0}
        )
        self.manager.pending_grid_orders['ORDER_STOPPING'] = {
            'session_id': session.id,
            'stock_code': session.stock_code,
            'side': 'BUY',
            'volume': 100,
            'expected_price': 10.0,
            'submitted_at': now,
        }

        result = self.manager.stop_grid_session(session.id, 'manual')

        self.assertEqual(result['status'], 'stopping')
        self.assertAlmostEqual(result['profit_ratio'], 0.10, places=6)
        self.assertAlmostEqual(result['grid_profit'], 100.0, places=2)
        self.assertEqual(result['pnl_snapshot']['method'], 'ledger_true_pnl')
        self.assertAlmostEqual(result['pnl_snapshot']['profit_ratio'], result['profit_ratio'], places=6)
        self.assertAlmostEqual(result['pnl_snapshot']['total_pnl'], result['grid_profit'], places=6)


class TestGridOrderOutsideLock(GridLifecycleLedgerBase):
    def test_executor_called_after_grid_lock_released(self):
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self.make_session()
        lock_was_free_during_executor = []

        def slow_buy(**kwargs):
            acquired = self.manager.lock.acquire(blocking=False)
            lock_was_free_during_executor.append(acquired)
            if acquired:
                self.manager.lock.release()
            time.sleep(0.02)
            return {'order_id': 'ORDER_LOCK_FREE'}

        self.executor.buy_stock.side_effect = slow_buy

        self.assertTrue(self.manager.execute_grid_trade(self.buy_signal(session)))

        self.assertEqual(lock_was_free_during_executor, [True])
        self.assertIn('ORDER_LOCK_FREE', self.manager.pending_grid_orders)

    def test_reserved_pending_buy_prevents_over_budget_second_order(self):
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self.make_session(max_investment=2000, current_investment=0, position_ratio=1.0)
        self.executor.buy_stock.return_value = {'order_id': 'ORDER_RESERVED'}

        self.assertTrue(self.manager.execute_grid_trade(self.buy_signal(session, price=10.0)))
        self.executor.buy_stock.reset_mock()

        self.assertFalse(self.manager.execute_grid_trade(self.buy_signal(session, price=10.0)))
        self.executor.buy_stock.assert_not_called()


class TestGridRealLedger(GridLifecycleLedgerBase):
    def test_simulation_mode_confirms_immediately_even_when_confirm_switch_on(self):
        config.ENABLE_SIMULATION_MODE = True
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self.make_session(max_investment=10000)

        self.assertTrue(self.manager.execute_grid_trade(self.buy_signal(session, price=10.0)))

        self.assertEqual(session.buy_count, 1)
        self.assertEqual(len(self.manager.pending_grid_orders), 0)
        self.assertEqual(len(self.db.get_grid_lots(session.id)), 1)

    def test_buy_creates_lot_and_sell_lifo_matches_realized_pnl(self):
        config.ENABLE_SIMULATION_MODE = True
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = False
        session = self.make_session(max_investment=10000)

        self.assertTrue(self.manager.execute_grid_trade(self.buy_signal(session, price=10.0)))
        self.assertTrue(self.manager.execute_grid_trade(self.sell_signal(session, price=10.5)))

        lots = self.db.get_grid_lots(session.id)
        matches = self.db.get_grid_lot_matches(session.id)
        summary = self.db.get_grid_ledger_summary(session.id, current_price=10.5)

        self.assertEqual(len(lots), 1)
        self.assertEqual(lots[0]['original_volume'], 200)
        self.assertEqual(lots[0]['remaining_volume'], 0)
        self.assertEqual(lots[0]['status'], 'closed')
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['match_type'], 'matched')
        self.assertAlmostEqual(matches[0]['realized_pnl'], 100.0, places=2)
        self.assertAlmostEqual(summary['realized_pnl'], 100.0, places=2)
        self.assertAlmostEqual(summary['true_pnl'], 100.0, places=2)

    def test_exit_conditions_use_ledger_true_pnl_before_legacy_totals(self):
        session = self.make_session(max_investment=1000)
        session.buy_count = 1
        session.sell_count = 1
        session.target_profit = 0.09
        session.stop_loss = -0.10
        session.total_buy_amount = 1000
        session.total_sell_amount = 1000
        session.total_buy_volume = 100
        session.total_sell_volume = 100

        now = datetime.now().isoformat()
        self.db.record_grid_trade_and_update_session(
            {
                'session_id': session.id,
                'stock_code': session.stock_code,
                'trade_type': 'BUY',
                'grid_level': 9.5,
                'trigger_price': 10.0,
                'volume': 100,
                'amount': 1000.0,
                'trade_id': 'LEDGER_BUY',
                'trade_time': now,
                'grid_center_before': 10.0,
                'grid_center_after': 10.0,
            },
            {'trade_count': 1, 'buy_count': 1, 'total_buy_amount': 1000.0, 'total_buy_volume': 100, 'current_investment': 1000.0}
        )
        self.db.record_grid_trade_and_update_session(
            {
                'session_id': session.id,
                'stock_code': session.stock_code,
                'trade_type': 'SELL',
                'grid_level': 10.5,
                'trigger_price': 11.0,
                'volume': 100,
                'amount': 1100.0,
                'trade_id': 'LEDGER_SELL',
                'trade_time': now,
                'grid_center_before': 10.0,
                'grid_center_after': 11.0,
            },
            {'trade_count': 2, 'sell_count': 1, 'total_sell_amount': 1100.0, 'total_sell_volume': 100, 'current_investment': 0.0}
        )

        reason = self.manager._check_exit_conditions(
            session,
            current_price=11.0,
            position_snapshot={'volume': 1000}
        )

        self.assertEqual(reason, 'target_profit')

    def test_pnl_snapshot_stats_and_exit_share_same_ledger_ratio(self):
        session = self.make_session(max_investment=1000)
        session.buy_count = 1
        session.sell_count = 1
        session.target_profit = 0.09
        session.stop_loss = -0.10
        session.total_buy_amount = 1000.0
        session.total_sell_amount = 1000.0
        session.total_buy_volume = 100
        session.total_sell_volume = 100

        now = datetime.now().isoformat()
        self.db.record_grid_trade_and_update_session(
            {
                'session_id': session.id,
                'stock_code': session.stock_code,
                'trade_type': 'BUY',
                'grid_level': 9.5,
                'trigger_price': 10.0,
                'volume': 100,
                'amount': 1000.0,
                'trade_id': 'SNAPSHOT_BUY',
                'trade_time': now,
                'grid_center_before': 10.0,
                'grid_center_after': 10.0,
            },
            {'trade_count': 1, 'buy_count': 1, 'total_buy_amount': 1000.0, 'total_buy_volume': 100, 'current_investment': 1000.0}
        )
        self.db.record_grid_trade_and_update_session(
            {
                'session_id': session.id,
                'stock_code': session.stock_code,
                'trade_type': 'SELL',
                'grid_level': 10.5,
                'trigger_price': 11.0,
                'volume': 100,
                'amount': 1100.0,
                'trade_id': 'SNAPSHOT_SELL',
                'trade_time': now,
                'grid_center_before': 10.0,
                'grid_center_after': 11.0,
            },
            {'trade_count': 2, 'sell_count': 1, 'total_sell_amount': 1100.0, 'total_sell_volume': 100, 'current_investment': 0.0}
        )

        snapshot = self.manager.get_pnl_snapshot(session, current_price=11.0)
        stats = self.manager.get_session_stats(session.id)
        reason = self.manager._check_exit_conditions(
            session,
            current_price=11.0,
            position_snapshot={'volume': 1000}
        )

        self.assertEqual(snapshot['method'], 'ledger_true_pnl')
        self.assertAlmostEqual(snapshot['profit_ratio'], 0.10, places=6)
        self.assertAlmostEqual(stats['profit_ratio'], snapshot['profit_ratio'], places=6)
        self.assertAlmostEqual(stats['grid_profit'], snapshot['total_pnl'], places=6)
        self.assertEqual(stats['pnl_snapshot']['method'], 'ledger_true_pnl')
        self.assertEqual(reason, 'target_profit')

    def test_stop_session_returns_unified_pnl_snapshot(self):
        session = self.make_session(max_investment=1000)
        session.buy_count = 1
        session.sell_count = 1
        session.total_buy_amount = 1000.0
        session.total_sell_amount = 1000.0
        session.total_buy_volume = 100
        session.total_sell_volume = 100
        session.current_center_price = 11.0

        now = datetime.now().isoformat()
        self.db.record_grid_trade_and_update_session(
            {
                'session_id': session.id,
                'stock_code': session.stock_code,
                'trade_type': 'BUY',
                'grid_level': 9.5,
                'trigger_price': 10.0,
                'volume': 100,
                'amount': 1000.0,
                'trade_id': 'STOP_BUY',
                'trade_time': now,
                'grid_center_before': 10.0,
                'grid_center_after': 10.0,
            },
            {'trade_count': 1, 'buy_count': 1, 'total_buy_amount': 1000.0, 'total_buy_volume': 100, 'current_investment': 1000.0}
        )
        self.db.record_grid_trade_and_update_session(
            {
                'session_id': session.id,
                'stock_code': session.stock_code,
                'trade_type': 'SELL',
                'grid_level': 10.5,
                'trigger_price': 11.0,
                'volume': 100,
                'amount': 1100.0,
                'trade_id': 'STOP_SELL',
                'trade_time': now,
                'grid_center_before': 10.0,
                'grid_center_after': 11.0,
            },
            {'trade_count': 2, 'sell_count': 1, 'total_sell_amount': 1100.0, 'total_sell_volume': 100, 'current_investment': 0.0}
        )

        result = self.manager.stop_grid_session(session.id, 'manual')

        self.assertAlmostEqual(result['profit_ratio'], 0.10, places=6)
        self.assertEqual(result['pnl_snapshot']['method'], 'ledger_true_pnl')
        self.assertAlmostEqual(result['pnl_snapshot']['total_pnl'], 100.0, places=2)


if __name__ == '__main__':
    unittest.main()
