"""
网格交易 MECE 回归测试。

覆盖矩阵:
1. 会话状态机: 提交中停止、撤单失败、终态回调闭环、stopping 状态拒绝新信号。
2. 下单并发: 锁外下单异常清理、提交中买入预算预占、未完成卖单数量预占。
3. 委托回调: 超量成交钳制、部分成交后撤单状态映射、历史委托终态补偿。
4. 真实账本: LIFO 跨 lot 匹配、未匹配卖出、账本事务原子回滚。
5. 重启恢复与退出: 孤儿委托标记、账本查询异常时降级旧 True P&L 口径。
"""

import json
import os
import sys
import threading
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


class GridMeceRegressionBase(unittest.TestCase):
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
        self.orig_counterparty = getattr(config, 'GRID_USE_COUNTERPARTY_PRICE', True)

        config.GRID_SIGNAL_MAX_AGE_SECONDS = 60
        config.GRID_SIGNAL_MAX_PRICE_DRIFT_RATIO = 0.20
        config.GRID_ENABLE_PRICE_LIMIT_GUARD = False
        config.GRID_BUY_COOLDOWN = 0
        config.GRID_SELL_COOLDOWN = 0
        config.GRID_USE_COUNTERPARTY_PRICE = False

    def tearDown(self):
        config.ENABLE_SIMULATION_MODE = self.orig_sim
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = self.orig_confirm
        config.GRID_SIGNAL_MAX_AGE_SECONDS = self.orig_age
        config.GRID_SIGNAL_MAX_PRICE_DRIFT_RATIO = self.orig_drift
        config.GRID_ENABLE_PRICE_LIMIT_GUARD = self.orig_guard
        config.GRID_BUY_COOLDOWN = self.orig_buy_cooldown
        config.GRID_SELL_COOLDOWN = self.orig_sell_cooldown
        config.GRID_USE_COUNTERPARTY_PRICE = self.orig_counterparty
        self.db.close()

    def make_session(self, stock_code='000001.SZ', max_investment=10000,
                     current_investment=0, position_ratio=0.25):
        session = GridSession(
            id=None,
            stock_code=stock_code,
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

    def record_trade(self, session, trade_type, volume, price, trade_id):
        now = datetime.now().isoformat()
        trade_data = {
            'session_id': session.id,
            'stock_code': session.stock_code,
            'trade_type': trade_type,
            'grid_level': price,
            'trigger_price': price,
            'volume': volume,
            'amount': volume * price,
            'trade_id': trade_id,
            'trade_time': now,
            'grid_center_before': session.current_center_price,
            'grid_center_after': price,
        }
        updates = {'trade_count': session.trade_count + 1}
        if trade_type == 'BUY':
            session.buy_count += 1
            session.total_buy_amount += trade_data['amount']
            session.total_buy_volume += volume
            session.current_investment += trade_data['amount']
            updates.update({
                'buy_count': session.buy_count,
                'total_buy_amount': session.total_buy_amount,
                'total_buy_volume': session.total_buy_volume,
                'current_investment': session.current_investment,
            })
        else:
            session.sell_count += 1
            session.total_sell_amount += trade_data['amount']
            session.total_sell_volume += volume
            session.current_investment = max(0, session.current_investment - trade_data['amount'])
            updates.update({
                'sell_count': session.sell_count,
                'total_sell_amount': session.total_sell_amount,
                'total_sell_volume': session.total_sell_volume,
                'current_investment': session.current_investment,
            })
        session.trade_count += 1
        self.db.record_grid_trade_and_update_session(trade_data, updates)


class TestGridSessionStateMachineMece(GridMeceRegressionBase):
    def test_stop_during_submitting_cancels_after_order_is_accepted(self):
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self.make_session()
        submit_started = threading.Event()
        release_submit = threading.Event()

        def slow_buy(**kwargs):
            submit_started.set()
            self.assertTrue(release_submit.wait(timeout=2))
            return {'order_id': 'ORDER_SUBMIT_STOP'}

        self.executor.buy_stock.side_effect = slow_buy
        self.executor.cancel_order.return_value = True

        worker = threading.Thread(
            target=lambda: self.manager.execute_grid_trade(self.buy_signal(session)),
            daemon=True,
        )
        worker.start()
        self.assertTrue(submit_started.wait(timeout=2))
        self.assertEqual(len(self.manager.submitting_grid_orders), 1)

        stats = self.manager.stop_grid_session(session.id, 'manual_stop')

        self.assertEqual(stats['status'], 'stopping')
        self.assertEqual(session.status, 'stopping')
        self.executor.cancel_order.assert_not_called()

        release_submit.set()
        worker.join(timeout=3)

        self.assertFalse(worker.is_alive())
        self.executor.cancel_order.assert_called_once_with('ORDER_SUBMIT_STOP')
        self.assertIn('ORDER_SUBMIT_STOP', self.manager.pending_grid_orders)
        self.assertEqual(self.db.get_grid_order('ORDER_SUBMIT_STOP')['status'], 'cancel_requested')

        self.assertTrue(self.manager.handle_order_callback(FakeOrderInfo('ORDER_SUBMIT_STOP', status=54)))
        self.assertNotIn('ORDER_SUBMIT_STOP', self.manager.pending_grid_orders)
        self.assertEqual(self.db.get_grid_session(session.id)['status'], 'stopped')

    def test_cancel_failure_keeps_session_stopping_until_terminal_callback(self):
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self.make_session()
        self.executor.buy_stock.return_value = {'order_id': 'ORDER_CANCEL_FAIL'}
        self.executor.cancel_order.return_value = False

        self.assertTrue(self.manager.execute_grid_trade(self.buy_signal(session)))
        stats = self.manager.stop_grid_session(session.id, 'manual_stop')

        self.assertEqual(stats['status'], 'stopping')
        self.assertEqual(stats['cancel_failed'], 1)
        self.assertEqual(self.db.get_grid_order('ORDER_CANCEL_FAIL')['status'], 'cancel_failed')
        self.assertIn('ORDER_CANCEL_FAIL', self.manager.pending_grid_orders)

        self.assertTrue(self.manager.handle_order_callback(FakeOrderInfo('ORDER_CANCEL_FAIL', status=57)))
        self.assertNotIn('ORDER_CANCEL_FAIL', self.manager.pending_grid_orders)
        self.assertEqual(self.db.get_grid_session(session.id)['status'], 'stopped')

    def test_stopping_session_rejects_new_signal_without_executor_call(self):
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self.make_session()
        session.status = 'stopping'
        self.db.update_grid_session(session.id, {'status': 'stopping'})

        self.assertFalse(self.manager.execute_grid_trade(self.buy_signal(session)))

        self.executor.buy_stock.assert_not_called()
        self.assertEqual(len(self.manager.submitting_grid_orders), 0)


class TestGridOrderConcurrencyMece(GridMeceRegressionBase):
    def test_executor_exception_cleans_submitting_reservation_and_tracker(self):
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self.make_session()
        tracker = self.manager.trackers[session.id]
        tracker.waiting_callback = True
        tracker.crossed_level = 9.5
        self.executor.buy_stock.side_effect = RuntimeError('券商接口异常')

        self.assertFalse(self.manager.execute_grid_trade(self.buy_signal(session)))

        self.assertEqual(self.manager.submitting_grid_orders, {})
        self.assertEqual(self.manager.pending_grid_orders, {})
        self.assertFalse(tracker.waiting_callback)
        self.assertIsNone(tracker.crossed_level)

    def test_submitting_buy_reservation_blocks_second_buy_before_first_returns(self):
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self.make_session(max_investment=2000, position_ratio=1.0)
        submit_started = threading.Event()
        release_submit = threading.Event()

        def slow_buy(**kwargs):
            submit_started.set()
            self.assertTrue(release_submit.wait(timeout=2))
            return {'order_id': 'ORDER_RESERVED_SUBMITTING'}

        self.executor.buy_stock.side_effect = slow_buy

        worker = threading.Thread(
            target=lambda: self.manager.execute_grid_trade(self.buy_signal(session, price=10.0)),
            daemon=True,
        )
        worker.start()
        self.assertTrue(submit_started.wait(timeout=2))

        self.assertFalse(self.manager.execute_grid_trade(self.buy_signal(session, price=10.0)))

        release_submit.set()
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.executor.buy_stock.call_count, 1)
        self.assertIn('ORDER_RESERVED_SUBMITTING', self.manager.pending_grid_orders)

    def test_pending_sell_reservation_blocks_duplicate_sell_volume(self):
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self.make_session(position_ratio=1.0)
        self.position_manager.get_position.return_value = {
            'stock_code': '000001.SZ',
            'volume': 200,
            'available': 200,
            'cost_price': 10.0,
            'current_price': 10.5,
        }
        self.executor.sell_stock.return_value = {'order_id': 'ORDER_SELL_RESERVED'}

        self.assertTrue(self.manager.execute_grid_trade(self.sell_signal(session, price=10.5)))
        self.executor.sell_stock.reset_mock()
        self.assertFalse(self.manager.execute_grid_trade(self.sell_signal(session, price=10.6)))

        self.executor.sell_stock.assert_not_called()
        self.assertIn('ORDER_SELL_RESERVED', self.manager.pending_grid_orders)


class TestGridOrderCallbackMece(GridMeceRegressionBase):
    def test_overfilled_deal_is_clamped_to_requested_volume(self):
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self.make_session(max_investment=2000, position_ratio=1.0)
        self.executor.buy_stock.return_value = {'order_id': 'ORDER_OVERFILL'}

        self.assertTrue(self.manager.execute_grid_trade(self.buy_signal(session, price=10.0)))
        self.assertTrue(self.manager.handle_deal_callback(
            FakeTrade('ORDER_OVERFILL', volume=500, price=10.0, trade_id='DEAL_OVERFILL')
        ))

        self.assertNotIn('ORDER_OVERFILL', self.manager.pending_grid_orders)
        order = self.db.get_grid_order('ORDER_OVERFILL')
        self.assertEqual(order['status'], 'filled')
        self.assertEqual(order['filled_volume'], 200)
        self.assertEqual(session.total_buy_volume, 200)
        self.assertAlmostEqual(session.current_investment, 2000.0, places=2)

    def test_partial_cancel_after_fill_maps_to_partial_filled_canceled(self):
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self.make_session(max_investment=2000, position_ratio=1.0)
        self.executor.buy_stock.return_value = {'order_id': 'ORDER_PART_CANCEL'}

        self.assertTrue(self.manager.execute_grid_trade(self.buy_signal(session, price=10.0)))
        # 部分成交（100/200），新语义下只累积不落账
        self.assertTrue(self.manager.handle_deal_callback(
            FakeTrade('ORDER_PART_CANCEL', volume=100, price=10.0, trade_id='DEAL_PART_ONLY')
        ))
        self.assertTrue(self.manager.handle_order_callback(FakeOrderInfo('ORDER_PART_CANCEL', status=53)))

        order = self.db.get_grid_order('ORDER_PART_CANCEL')
        self.assertEqual(order['status'], 'partial_filled_canceled')
        self.assertEqual(order['filled_volume'], 100)
        self.assertNotIn('ORDER_PART_CANCEL', self.manager.pending_grid_orders)
        # 新语义：部分成交未落账，buy_count 仍为 0
        self.assertEqual(session.buy_count, 0)

    def test_terminal_callback_updates_historical_order_even_not_in_memory(self):
        session = self.make_session()
        self.db.create_grid_order({
            'order_id': 'ORDER_HISTORY_REJECT',
            'session_id': session.id,
            'stock_code': session.stock_code,
            'side': 'BUY',
            'status': 'submitted',
            'requested_volume': 100,
            'expected_price': 10.0,
            'raw_signal': json.dumps(self.buy_signal(session), ensure_ascii=False),
        })

        self.assertTrue(self.manager.handle_order_callback(FakeOrderInfo('ORDER_HISTORY_REJECT', status=57)))

        order = self.db.get_grid_order('ORDER_HISTORY_REJECT')
        self.assertEqual(order['status'], 'rejected')
        self.assertIn('order terminal status 57', order['last_error'])


class TestGridLedgerMece(GridMeceRegressionBase):
    def test_lifo_ledger_matches_across_multiple_lots_and_marks_open_remainder(self):
        session = self.make_session(max_investment=10000)

        self.record_trade(session, 'BUY', 100, 10.0, 'LIFO_BUY_1')
        self.record_trade(session, 'BUY', 100, 9.0, 'LIFO_BUY_2')
        self.record_trade(session, 'SELL', 150, 11.0, 'LIFO_SELL_1')

        lots = self.db.get_grid_lots(session.id)
        matches = self.db.get_grid_lot_matches(session.id)
        summary = self.db.get_grid_ledger_summary(session.id, current_price=12.0)

        self.assertEqual([lot['remaining_volume'] for lot in lots], [50, 0])
        self.assertEqual([lot['status'] for lot in lots], ['open', 'closed'])
        self.assertEqual([match['volume'] for match in matches], [100, 50])
        self.assertAlmostEqual(matches[0]['buy_price'], 9.0, places=2)
        self.assertAlmostEqual(matches[1]['buy_price'], 10.0, places=2)
        self.assertAlmostEqual(matches[0]['realized_pnl'], 200.0, places=2)
        self.assertAlmostEqual(matches[1]['realized_pnl'], 50.0, places=2)
        self.assertEqual(summary['open_volume'], 50)
        self.assertAlmostEqual(summary['open_cost'], 500.0, places=2)
        self.assertAlmostEqual(summary['unrealized_pnl'], 100.0, places=2)
        self.assertAlmostEqual(summary['true_pnl'], 350.0, places=2)

    def test_unmatched_sell_is_recorded_without_fake_profit(self):
        session = self.make_session(max_investment=10000)

        self.record_trade(session, 'SELL', 100, 12.0, 'UNMATCHED_SELL_1')

        matches = self.db.get_grid_lot_matches(session.id)
        summary = self.db.get_grid_ledger_summary(session.id, current_price=12.0)

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['match_type'], 'unmatched')
        self.assertIsNone(matches[0]['buy_lot_id'])
        self.assertEqual(summary['unmatched_volume'], 100)
        self.assertAlmostEqual(summary['realized_pnl'], 0.0, places=2)
        self.assertAlmostEqual(summary['true_pnl'], 0.0, places=2)

    def test_sell_first_then_buy_back_is_realized_profit(self):
        session = self.make_session(stock_code='003025.SZ', max_investment=25000, position_ratio=0.2)

        self.assertTrue(self.manager._record_confirmed_grid_trade(
            session=session,
            signal=self.sell_signal(session, price=17.93),
            side='SELL',
            price=17.93,
            volume=200,
            trade_id='PROD_SELL_FIRST'
        ))
        self.assertTrue(self.manager._record_confirmed_grid_trade(
            session=session,
            signal=self.buy_signal(session, price=17.10),
            side='BUY',
            price=17.10,
            volume=200,
            trade_id='PROD_BUY_BACK'
        ))

        lots = self.db.get_grid_lots(session.id)
        matches = self.db.get_grid_lot_matches(session.id)
        summary = self.db.get_grid_ledger_summary(session.id, current_price=17.10)
        db_session = self.db.get_grid_session(session.id)

        self.assertEqual(len(lots), 1)
        self.assertEqual(lots[0]['original_volume'], 200)
        self.assertEqual(lots[0]['remaining_volume'], 0)
        self.assertEqual(lots[0]['realized_volume'], 200)
        self.assertEqual(lots[0]['status'], 'closed')
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['match_type'], 'matched')
        self.assertEqual(matches[0]['volume'], 200)
        self.assertAlmostEqual(matches[0]['sell_price'], 17.93, places=2)
        self.assertAlmostEqual(matches[0]['buy_price'], 17.10, places=2)
        self.assertAlmostEqual(matches[0]['realized_pnl'], 166.0, places=2)
        self.assertEqual(summary['matched_volume'], 200)
        self.assertEqual(summary['unmatched_volume'], 0)
        self.assertEqual(summary['open_volume'], 0)
        self.assertAlmostEqual(summary['realized_pnl'], 166.0, places=2)
        self.assertAlmostEqual(summary['true_pnl'], 166.0, places=2)
        self.assertAlmostEqual(session.current_investment, 0.0, places=2)
        self.assertAlmostEqual(db_session['current_investment'], 0.0, places=2)

    def test_sell_first_buy_back_matches_latest_unmatched_sell(self):
        session = self.make_session(stock_code='600509.SH', max_investment=20000, position_ratio=0.2)

        self.record_trade(session, 'SELL', 700, 7.60, 'SELL_LOW_FIRST')
        self.record_trade(session, 'SELL', 600, 8.006666666666666, 'SELL_HIGH_LATEST')
        self.record_trade(session, 'BUY', 400, 7.66, 'BUY_BACK_LATEST')

        matches = self.db.get_grid_lot_matches(session.id)
        summary = self.db.get_grid_ledger_summary(session.id, current_price=7.66)

        matched = [m for m in matches if m['match_type'] == 'matched']
        unmatched = [m for m in matches if m['match_type'] == 'unmatched']

        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]['sell_trade_id'], 'SELL_HIGH_LATEST')
        self.assertEqual(matched[0]['volume'], 400)
        self.assertAlmostEqual(matched[0]['sell_price'], 8.006666666666666, places=6)
        self.assertAlmostEqual(matched[0]['buy_price'], 7.66, places=2)
        self.assertAlmostEqual(matched[0]['realized_pnl'], 138.66666666666632, places=2)
        self.assertEqual(sum(m['volume'] for m in unmatched), 900)
        self.assertEqual(summary['matched_volume'], 400)
        self.assertEqual(summary['unmatched_volume'], 900)
        self.assertAlmostEqual(summary['realized_pnl'], 138.66666666666632, places=2)
        self.assertAlmostEqual(summary['true_pnl'], 138.66666666666632, places=2)

    def test_rebuild_ledger_repairs_historical_sell_first_open_buy_lot(self):
        session = self.make_session(stock_code='003025.SZ', max_investment=25000, position_ratio=0.2)
        sell_time = '2026-06-16T09:34:33.589241'
        buy_time = '2026-06-17T09:32:04.390301'
        sell_trade = {
            'session_id': session.id,
            'stock_code': session.stock_code,
            'trade_type': 'SELL',
            'grid_level': 17.7765,
            'trigger_price': 17.93,
            'volume': 200,
            'amount': 3586.0,
            'trade_id': '70600105000007919492',
            'trade_time': sell_time,
            'grid_center_before': 16.93,
            'grid_center_after': 17.93,
        }
        buy_trade = {
            'session_id': session.id,
            'stock_code': session.stock_code,
            'trade_type': 'BUY',
            'grid_level': 17.0335,
            'trigger_price': 17.10,
            'volume': 200,
            'amount': 3420.0,
            'trade_id': '70730101000005117310',
            'trade_time': buy_time,
            'grid_center_before': 17.93,
            'grid_center_after': 17.10,
        }
        self.db.record_grid_trade(sell_trade)
        self.db.record_grid_trade(buy_trade)
        self.db.update_grid_session(session.id, {
            'trade_count': 2,
            'buy_count': 1,
            'sell_count': 1,
            'total_buy_amount': 3420.0,
            'total_sell_amount': 3586.0,
            'total_buy_volume': 200,
            'total_sell_volume': 200,
            'current_investment': 3420.0,
            'current_center_price': 17.10,
        })
        with self.db.lock:
            cursor = self.db.conn.cursor()
            cursor.execute("""
                INSERT INTO grid_lot_matches
                (session_id, stock_code, buy_lot_id, sell_trade_id, sell_order_id,
                 match_type, volume, buy_price, sell_price, buy_amount, sell_amount,
                 realized_pnl, matched_at)
                VALUES (?, ?, NULL, ?, ?, 'unmatched', 200, NULL, 17.93, 0, 3586, 0, ?)
            """, (session.id, session.stock_code, sell_trade['trade_id'], '135266305', sell_time))
            cursor.execute("""
                INSERT INTO grid_lots
                (session_id, stock_code, buy_trade_id, buy_order_id, buy_price,
                 original_volume, remaining_volume, realized_volume, buy_amount,
                 opened_at, status)
                VALUES (?, ?, ?, ?, 17.10, 200, 200, 0, 3420, ?, 'open')
            """, (session.id, session.stock_code, buy_trade['trade_id'], '403701761', buy_time))
            self.db.conn.commit()

        stale_summary = self.db.get_grid_ledger_summary(session.id, current_price=17.10)
        self.assertEqual(stale_summary['open_volume'], 200)
        self.assertEqual(stale_summary['unmatched_volume'], 200)
        self.assertAlmostEqual(stale_summary['true_pnl'], 0.0, places=2)

        rebuild_summary = self.db.rebuild_grid_ledger_for_session(session.id)
        fixed_summary = self.db.get_grid_ledger_summary(session.id, current_price=17.10)
        fixed_session = self.db.get_grid_session(session.id)

        self.assertEqual(rebuild_summary['trade_count'], 2)
        self.assertEqual(fixed_summary['open_volume'], 0)
        self.assertEqual(fixed_summary['unmatched_volume'], 0)
        self.assertEqual(fixed_summary['matched_volume'], 200)
        self.assertAlmostEqual(fixed_summary['realized_pnl'], 166.0, places=2)
        self.assertAlmostEqual(fixed_summary['true_pnl'], 166.0, places=2)
        self.assertAlmostEqual(fixed_session['current_investment'], 0.0, places=2)

    def test_rebuild_ledger_noops_when_session_has_no_trades(self):
        session = self.make_session(stock_code='700022.SH', max_investment=5000, current_investment=3000)
        self.db.update_grid_session(session.id, {'current_investment': 3000.0})
        opened_at = '2026-06-18T09:30:00'
        with self.db.lock:
            cursor = self.db.conn.cursor()
            cursor.execute("""
                INSERT INTO grid_lots
                (session_id, stock_code, buy_trade_id, buy_order_id, buy_price,
                 original_volume, remaining_volume, realized_volume, buy_amount,
                 opened_at, status)
                VALUES (?, ?, 'LEGACY_BUY', 'LEGACY_ORDER',
                        10.00, 300, 300, 0, 3000, ?, 'open')
            """, (session.id, session.stock_code, opened_at))
            self.db.conn.commit()

        before = self.db.get_grid_ledger_summary(session.id, current_price=10.0)
        rebuild_summary = self.db.rebuild_grid_ledger_for_session(session.id)
        after = self.db.get_grid_ledger_summary(session.id, current_price=10.0)
        fixed_session = self.db.get_grid_session(session.id)

        self.assertEqual(rebuild_summary['trade_count'], 0)
        self.assertEqual(before['open_volume'], 300)
        self.assertEqual(after['open_volume'], 300)
        self.assertEqual(after['lot_count'], before['lot_count'])
        self.assertAlmostEqual(after['open_cost'], 3000.0, places=2)
        self.assertAlmostEqual(rebuild_summary['current_investment'], 3000.0, places=2)
        self.assertAlmostEqual(fixed_session['current_investment'], 3000.0, places=2)

    def test_ledger_failure_rolls_back_trade_session_and_order_updates(self):
        session = self.make_session(max_investment=10000)
        self.db.create_grid_order({
            'order_id': 'ORDER_ATOMIC',
            'session_id': session.id,
            'stock_code': session.stock_code,
            'side': 'BUY',
            'status': 'submitted',
            'requested_volume': 100,
            'expected_price': 10.0,
            'raw_signal': json.dumps(self.buy_signal(session), ensure_ascii=False),
        })
        original_apply = self.db._apply_grid_ledger
        self.db._apply_grid_ledger = Mock(side_effect=RuntimeError('账本写入失败'))
        try:
            with self.assertRaises(RuntimeError):
                self.db.record_grid_trade_and_update_session(
                    {
                        'session_id': session.id,
                        'stock_code': session.stock_code,
                        'trade_type': 'BUY',
                        'grid_level': 9.5,
                        'trigger_price': 10.0,
                        'volume': 100,
                        'amount': 1000.0,
                        'trade_id': 'ATOMIC_BUY',
                        'trade_time': datetime.now().isoformat(),
                        'grid_center_before': 10.0,
                        'grid_center_after': 10.0,
                    },
                    {'trade_count': 1, 'buy_count': 1, 'current_investment': 1000.0},
                    order_id='ORDER_ATOMIC',
                    order_updates={'status': 'filled', 'filled_volume': 100, 'filled_amount': 1000.0},
                )
        finally:
            self.db._apply_grid_ledger = original_apply

        self.assertEqual(self.db.get_grid_trades(session.id), [])
        self.assertEqual(self.db.get_grid_session(session.id)['trade_count'], 0)
        order = self.db.get_grid_order('ORDER_ATOMIC')
        self.assertEqual(order['status'], 'submitted')
        self.assertEqual(order['filled_volume'], 0)


class TestGridRecoveryAndExitMece(GridMeceRegressionBase):
    def test_restart_marks_open_order_without_active_session_as_orphaned(self):
        session = self.make_session()
        self.db.create_grid_order({
            'order_id': 'ORDER_ORPHAN',
            'session_id': session.id,
            'stock_code': session.stock_code,
            'side': 'BUY',
            'status': 'submitted',
            'requested_volume': 100,
            'expected_price': 10.0,
            'raw_signal': json.dumps(self.buy_signal(session), ensure_ascii=False),
        })
        self.db.stop_grid_session(session.id, 'manual_stop')

        restarted = GridTradingManager(self.db, self.position_manager, self.executor)

        self.assertEqual(restarted.pending_grid_orders, {})
        order = self.db.get_grid_order('ORDER_ORPHAN')
        self.assertEqual(order['status'], 'orphaned')
        self.assertIn('active session not found', order['last_error'])

    def test_restart_rebuilds_sell_first_ledger_and_repairs_investment(self):
        session = self.make_session(stock_code='003025.SZ', max_investment=25000, position_ratio=0.2)
        sell_time = '2026-06-16T09:34:33.589241'
        buy_time = '2026-06-17T09:32:04.390301'
        self.db.record_grid_trade({
            'session_id': session.id,
            'stock_code': session.stock_code,
            'trade_type': 'SELL',
            'grid_level': 17.7765,
            'trigger_price': 17.93,
            'volume': 200,
            'amount': 3586.0,
            'trade_id': 'PROD_RESTART_SELL',
            'trade_time': sell_time,
            'grid_center_before': 16.93,
            'grid_center_after': 17.93,
        })
        self.db.record_grid_trade({
            'session_id': session.id,
            'stock_code': session.stock_code,
            'trade_type': 'BUY',
            'grid_level': 17.0335,
            'trigger_price': 17.10,
            'volume': 200,
            'amount': 3420.0,
            'trade_id': 'PROD_RESTART_BUY',
            'trade_time': buy_time,
            'grid_center_before': 17.93,
            'grid_center_after': 17.10,
        })
        self.db.update_grid_session(session.id, {
            'trade_count': 2,
            'buy_count': 1,
            'sell_count': 1,
            'total_buy_amount': 3420.0,
            'total_sell_amount': 3586.0,
            'total_buy_volume': 200,
            'total_sell_volume': 200,
            'current_investment': 3420.0,
            'current_center_price': 17.10,
        })
        with self.db.lock:
            cursor = self.db.conn.cursor()
            cursor.execute("""
                INSERT INTO grid_lot_matches
                (session_id, stock_code, buy_lot_id, sell_trade_id, sell_order_id,
                 match_type, volume, buy_price, sell_price, buy_amount, sell_amount,
                 realized_pnl, matched_at)
                VALUES (?, ?, NULL, 'PROD_RESTART_SELL', '135266305',
                        'unmatched', 200, NULL, 17.93, 0, 3586, 0, ?)
            """, (session.id, session.stock_code, sell_time))
            cursor.execute("""
                INSERT INTO grid_lots
                (session_id, stock_code, buy_trade_id, buy_order_id, buy_price,
                 original_volume, remaining_volume, realized_volume, buy_amount,
                 opened_at, status)
                VALUES (?, ?, 'PROD_RESTART_BUY', '403701761',
                        17.10, 200, 200, 0, 3420, ?, 'open')
            """, (session.id, session.stock_code, buy_time))
            self.db.conn.commit()

        restarted = GridTradingManager(self.db, self.position_manager, self.executor)
        restored = restarted.sessions[restarted._normalize_code(session.stock_code)]
        summary = self.db.get_grid_ledger_summary(session.id, current_price=17.10)

        self.assertAlmostEqual(restored.current_investment, 0.0, places=2)
        self.assertEqual(summary['open_volume'], 0)
        self.assertEqual(summary['unmatched_volume'], 0)
        self.assertAlmostEqual(summary['true_pnl'], 166.0, places=2)

    def test_exit_conditions_fall_back_to_legacy_true_pnl_when_ledger_query_fails(self):
        session = self.make_session(max_investment=1000)
        session.buy_count = 1
        session.sell_count = 1
        session.total_buy_amount = 1000.0
        session.total_sell_amount = 1100.0
        session.total_buy_volume = 100
        session.total_sell_volume = 100
        session.target_profit = 0.05
        session.stop_loss = -0.10
        self.db.get_grid_ledger_summary = Mock(side_effect=RuntimeError('账本查询失败'))

        reason = self.manager._check_exit_conditions(
            session,
            current_price=11.0,
            position_snapshot={'volume': 1000}
        )

        self.assertEqual(reason, 'target_profit')


if __name__ == '__main__':
    unittest.main(verbosity=2)
