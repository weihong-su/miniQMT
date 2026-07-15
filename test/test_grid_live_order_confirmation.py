"""
网格实盘委托成交确认与执行前复核测试

覆盖：
1. 实盘委托成功后不立即落账，等待成交回调确认
2. 部分成交按成交回报增量落账
3. 旧信号、会话错配、价格漂移过大时拒绝执行
4. 成交回调路径不依赖废弃的 GRID_TRADING_ENABLED
"""

import os
import sys
import json
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import config
from easy_qmt_trader import MyXtQuantTraderCallback, easy_qmt_trader
from grid_database import DatabaseManager
from grid_trading_manager import GridSession, GridTradingManager, PriceTracker
from position_manager import PositionManager
from trading_executor import TradingExecutor, DIRECTION_BUY


class FakeTrade:
    def __init__(self, order_id, stock_code='000001.SZ', volume=100, price=10.0, trade_id='DEAL_1'):
        self.order_id = order_id
        self.stock_code = stock_code
        self.traded_volume = volume
        self.traded_price = price
        self.trade_id = trade_id


class FakeDealInfo:
    m_strInstrumentID = '000001.SZ'
    m_nDirection = DIRECTION_BUY
    m_dPrice = 10.0
    m_nVolume = 100
    m_strTradeID = 'DEAL_OLD_SWITCH'
    m_dComssion = 0.0
    m_strOrderID = 'ORDER_OLD_SWITCH'


class FakeOrderInfo:
    def __init__(self, order_id, stock_code='000001.SZ', status=54):
        self.m_strOrderSysID = order_id
        self.m_strInstrumentID = stock_code
        self.m_nOrderStatus = status
        self.order_sysid = order_id
        self.order_id = order_id
        self.stock_code = stock_code
        self.order_status = status


class FakeBrokerOrder:
    def __init__(self, order_id, status=54, stock_code='000001.SZ', traded_volume=0, traded_price=0):
        self.order_id = order_id
        self.stock_code = stock_code
        self.order_status = status
        self.traded_volume = traded_volume
        self.traded_price = traded_price


class TestGridLiveOrderConfirmation(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseManager(':memory:')
        self.db.init_grid_tables()
        self.position_manager = Mock(spec=PositionManager)
        self.position_manager.signal_lock = __import__('threading').Lock()
        self.position_manager.latest_signals = {}
        self.position_manager._increment_data_version = Mock()
        self.position_manager.data_manager = Mock()
        self.position_manager.data_manager.get_latest_data.return_value = {'lastPrice': 10.0}
        # 大部分 BUY 测试不需要持仓快照，Mock 返回 None 表示没有持仓
        # → _build_grid_order_plan 中 BUY 无持仓时走金额回退路径
        self.position_manager.get_position.return_value = None
        self.executor = Mock(spec=TradingExecutor)
        self.executor._save_trade_record.return_value = True
        self.manager = GridTradingManager(self.db, self.position_manager, self.executor)

        self.orig_sim = config.ENABLE_SIMULATION_MODE
        self.orig_confirm = getattr(config, 'GRID_CONFIRM_LIVE_ORDER_BY_DEAL', True)
        self.orig_max_age = getattr(config, 'GRID_SIGNAL_MAX_AGE_SECONDS', 60)
        self.orig_drift = getattr(config, 'GRID_SIGNAL_MAX_PRICE_DRIFT_RATIO', 0.01)
        self.orig_reconcile_interval = getattr(config, 'GRID_ORDER_RECONCILE_INTERVAL', 15)
        self.orig_reconcile_stale = getattr(config, 'GRID_ORDER_RECONCILE_STALE_SECONDS', 5)

    def tearDown(self):
        config.ENABLE_SIMULATION_MODE = self.orig_sim
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = self.orig_confirm
        config.GRID_SIGNAL_MAX_AGE_SECONDS = self.orig_max_age
        config.GRID_SIGNAL_MAX_PRICE_DRIFT_RATIO = self.orig_drift
        config.GRID_ORDER_RECONCILE_INTERVAL = self.orig_reconcile_interval
        config.GRID_ORDER_RECONCILE_STALE_SECONDS = self.orig_reconcile_stale
        self.db.close()

    def _make_session(self, stock_code='000001.SZ', current_investment=0.0):
        session = GridSession(
            id=None,
            stock_code=stock_code,
            status='active',
            center_price=10.0,
            current_center_price=10.0,
            price_interval=0.05,
            position_ratio=0.25,
            callback_ratio=0.005,
            max_investment=10000,
            current_investment=current_investment,
            start_time=datetime.now(),
            end_time=datetime.now() + timedelta(days=7),
        )
        data = asdict(session)
        data['start_time'] = session.start_time.isoformat()
        data['end_time'] = session.end_time.isoformat()
        session.id = self.db.create_grid_session(data)
        self.manager.sessions[self.manager._normalize_code(stock_code)] = session
        self.manager.trackers[session.id] = PriceTracker(session_id=session.id, last_price=10.0)
        return session

    def _buy_signal(self, session, trigger_price=10.0):
        return {
            'stock_code': session.stock_code,
            'strategy': config.GRID_STRATEGY_NAME,
            'signal_type': 'BUY',
            'grid_level': 9.5,
            'trigger_price': trigger_price,
            'session_id': session.id,
            'timestamp': datetime.now().isoformat(),
            'signal_source': 'grid_tracker',
            'require_price_recheck': True,
            'valley_price': 9.9,
            'callback_ratio': 0.005,
        }

    def test_live_order_waits_for_deal_and_partial_fills_incrementally(self):
        """部分成交只更新填充量不落账，全部成交后一次性聚合落账"""
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self._make_session()
        signal = self._buy_signal(session)
        self.executor.buy_stock.return_value = {'order_id': 'ORDER_PARTIAL'}

        result = self.manager.execute_grid_trade(signal)

        self.assertTrue(result)
        self.assertIn('ORDER_PARTIAL', self.manager.pending_grid_orders)
        db_order = self.db.get_grid_order('ORDER_PARTIAL')
        self.assertIsNotNone(db_order)
        self.assertEqual(db_order['status'], 'submitted')
        self.assertEqual(session.buy_count, 0)
        self.assertEqual(len(self.db.get_grid_trades(session.id)), 0)
        self.executor._save_trade_record.assert_not_called()

        # 第1笔部分成交：只累积填充量，不落账
        self.assertTrue(self.manager.handle_deal_callback(
            FakeTrade('ORDER_PARTIAL', volume=100, price=10.0, trade_id='DEAL_1')
        ))
        # 部分成交阶段不应写 trade_records 或 grid_trades
        self.executor._save_trade_record.assert_not_called()
        self.assertEqual(len(self.db.get_grid_trades(session.id, limit=10)), 0)
        self.assertIn('ORDER_PARTIAL', self.manager.pending_grid_orders)
        db_order = self.db.get_grid_order('ORDER_PARTIAL')
        self.assertEqual(db_order['status'], 'partial_filled')
        self.assertEqual(db_order['filled_volume'], 100)
        # session 统计在部分成交阶段不变
        self.assertEqual(session.buy_count, 0)
        self.assertEqual(session.total_buy_volume, 0)
        self.assertAlmostEqual(session.current_investment, 0.0, places=2)

        # 第2笔部分成交：委托量200已满，全部成交 → 一次性聚合落账
        self.assertTrue(self.manager.handle_deal_callback(
            FakeTrade('ORDER_PARTIAL', volume=100, price=10.1, trade_id='DEAL_2')
        ))
        self.assertNotIn('ORDER_PARTIAL', self.manager.pending_grid_orders)
        db_order = self.db.get_grid_order('ORDER_PARTIAL')
        self.assertEqual(db_order['status'], 'filled')
        self.assertEqual(db_order['filled_volume'], 200)
        # 统计只 +1（聚合），总量用加权均价
        self.assertEqual(session.buy_count, 1)
        self.assertEqual(session.total_buy_volume, 200)
        self.assertAlmostEqual(session.current_investment, 200 * 10.05, places=2)  # 加权均价 (100*10.0+100*10.1)/200=10.05
        # grid_trades 只有一条聚合记录
        trades = self.db.get_grid_trades(session.id, limit=10)
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0]['trigger_price'], 10.05, places=4)
        self.assertEqual(trades[0]['volume'], 200)
        # trade_records 只写一条
        self.assertEqual(self.executor._save_trade_record.call_count, 1)
        record = self.executor._save_trade_record.call_args_list[0].kwargs
        self.assertEqual(record['trade_type'], 'BUY')
        self.assertEqual(record['trade_id'], 'ORDER_PARTIAL')
        self.assertEqual(record['volume'], 200)
        self.assertAlmostEqual(record['price'], 10.05, places=4)
        self.assertEqual(record['strategy'], config.GRID_STRATEGY_NAME)

    def test_order_cancel_reject_cleans_pending_and_persists_status(self):
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self._make_session()
        signal = self._buy_signal(session)
        self.executor.buy_stock.return_value = {'order_id': 'ORDER_CANCEL'}

        self.assertTrue(self.manager.execute_grid_trade(signal))
        self.assertIn('ORDER_CANCEL', self.manager.pending_grid_orders)

        handled = self.manager.handle_order_callback(FakeOrderInfo('ORDER_CANCEL', status=54))

        self.assertTrue(handled)
        self.assertNotIn('ORDER_CANCEL', self.manager.pending_grid_orders)
        db_order = self.db.get_grid_order('ORDER_CANCEL')
        self.assertEqual(db_order['status'], 'canceled')
        self.assertEqual(session.buy_count, 0)
        self.assertEqual(len(self.db.get_grid_trades(session.id)), 0)

    def test_open_grid_orders_recovered_on_manager_restart(self):
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self._make_session()
        signal = self._buy_signal(session)
        self.executor.buy_stock.return_value = {'order_id': 'ORDER_RESTART'}

        self.assertTrue(self.manager.execute_grid_trade(signal))

        restarted = GridTradingManager(self.db, self.position_manager, self.executor)
        self.assertIn('ORDER_RESTART', restarted.pending_grid_orders)
        self.assertTrue(restarted.handle_deal_callback(
            FakeTrade('ORDER_RESTART', volume=200, price=10.0, trade_id='DEAL_RESTART')
        ))
        self.assertNotIn('ORDER_RESTART', restarted.pending_grid_orders)
        self.assertEqual(self.db.get_grid_order('ORDER_RESTART')['status'], 'filled')

    def test_startup_reconcile_replays_broker_trade_and_closes_canceled_order(self):
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self._make_session()
        signal = self._buy_signal(session)

        self.db.create_grid_order({
            'order_id': 'ORDER_RECON_FILLED',
            'session_id': session.id,
            'stock_code': session.stock_code,
            'side': 'BUY',
            'status': 'submitted',
            'requested_volume': 200,
            'expected_price': 10.0,
            'reserved_price': 10.2,
            'submitted_at': datetime.now().isoformat(),
            'raw_signal': __import__('json').dumps(signal),
        })
        self.db.create_grid_order({
            'order_id': 'ORDER_RECON_CANCELED',
            'session_id': session.id,
            'stock_code': session.stock_code,
            'side': 'BUY',
            'status': 'submitted',
            'requested_volume': 100,
            'expected_price': 10.0,
            'reserved_price': 10.2,
            'submitted_at': datetime.now().isoformat(),
            'raw_signal': __import__('json').dumps(signal),
        })
        self.executor.query_stock_trades.return_value = [
            FakeTrade('ORDER_RECON_FILLED', volume=200, price=10.1, trade_id='DEAL_RECON_1')
        ]
        self.executor.query_stock_orders.return_value = [
            FakeBrokerOrder('ORDER_RECON_FILLED', status=56, traded_volume=200, traded_price=10.1),
            FakeBrokerOrder('ORDER_RECON_CANCELED', status=54),
        ]

        restarted = GridTradingManager(self.db, self.position_manager, self.executor)

        self.assertNotIn('ORDER_RECON_FILLED', restarted.pending_grid_orders)
        self.assertNotIn('ORDER_RECON_CANCELED', restarted.pending_grid_orders)
        self.assertEqual(self.db.get_grid_order('ORDER_RECON_FILLED')['status'], 'filled')
        self.assertEqual(self.db.get_grid_order('ORDER_RECON_CANCELED')['status'], 'canceled')
        self.assertEqual(len(self.db.get_grid_trades(session.id, limit=10)), 1)

    def test_runtime_reconcile_replays_filled_pending_order_without_restart(self):
        """成交推送漏掉时，运行期对账应补记已成委托，不等重启"""
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self._make_session()
        signal = self._buy_signal(session)
        self.executor.buy_stock.return_value = {'order_id': 'ORDER_RUNTIME_RECON'}

        self.assertTrue(self.manager.execute_grid_trade(signal))
        self.assertIn('ORDER_RUNTIME_RECON', self.manager.pending_grid_orders)
        self.assertEqual(len(self.db.get_grid_trades(session.id, limit=10)), 0)

        self.executor.query_stock_trades.return_value = []
        self.executor.query_stock_orders.return_value = [
            FakeBrokerOrder('ORDER_RUNTIME_RECON', status=56, traded_volume=200, traded_price=10.1)
        ]

        result = self.manager.reconcile_pending_grid_orders_if_due(
            force=True,
            reason='运行期对账测试'
        )

        self.assertEqual(result['checked'], 1)
        self.assertEqual(result['replayed'], 1)
        self.assertNotIn('ORDER_RUNTIME_RECON', self.manager.pending_grid_orders)
        self.assertEqual(self.db.get_grid_order('ORDER_RUNTIME_RECON')['status'], 'filled')
        trades = self.db.get_grid_trades(session.id, limit=10)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]['trade_id'], 'ORDER_RUNTIME_RECON')
        self.assertAlmostEqual(trades[0]['trigger_price'], 10.1, places=4)
        self.assertEqual(session.buy_count, 1)
        self.executor._save_trade_record.assert_called_once()

    def test_buy_signal_suppressed_while_same_side_pending_exists(self):
        """同一会话已有 BUY pending 时，不再生成或执行新的 BUY 网格单"""
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self._make_session()
        self.position_manager.get_position.return_value = {
            'stock_code': session.stock_code,
            'volume': 1000,
            'available': 1000,
            'cost_price': 10.0,
            'current_price': 9.46,
            'market_value': 9460.0,
        }
        signal = self._buy_signal(session)
        self.manager.pending_grid_orders['ORDER_PENDING_BUY'] = {
            'order_id': 'ORDER_PENDING_BUY',
            'session_id': session.id,
            'stock_code': session.stock_code,
            'side': 'BUY',
            'signal': signal,
            'requested_volume': 200,
            'expected_price': 10.0,
            'reserved_price': 10.2,
            'filled_volume': 0,
            'filled_amount': 0.0,
            'confirmed_trade_ids': set(),
            'created_at': datetime.now().isoformat(),
        }
        tracker = self.manager.trackers[session.id]
        tracker.waiting_callback = True
        tracker.direction = 'falling'
        tracker.crossed_level = 9.5
        tracker.valley_price = 9.4

        generated = self.manager.check_grid_signals(session.stock_code, 9.46)

        self.assertIsNone(generated)
        self.assertEqual(session.status, 'active')
        self.assertIn('ORDER_PENDING_BUY', self.manager.pending_grid_orders)

        direct_result = self.manager.execute_grid_trade(signal)
        self.assertFalse(direct_result)
        self.executor.buy_stock.assert_not_called()

    def test_duplicate_deal_ignored_by_db_idempotency(self):
        """重复成交回报（同一 trade_id）在部分成交阶段被忽略"""
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True
        session = self._make_session()
        signal = self._buy_signal(session)
        self.executor.buy_stock.return_value = {'order_id': 'ORDER_DUP'}

        self.assertTrue(self.manager.execute_grid_trade(signal))
        # 第一笔回调：200股全部成交 → 一次性聚合落账
        trade = FakeTrade('ORDER_DUP', volume=200, price=10.0, trade_id='DEAL_DUP')
        self.assertTrue(self.manager.handle_deal_callback(trade))
        # 同一 trade_id 重复回调 → 忽略
        self.assertFalse(self.manager.handle_deal_callback(trade))
        self.assertEqual(len(self.db.get_grid_trades(session.id, limit=10)), 1)

    def test_stale_signal_rejected_before_order(self):
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_SIGNAL_MAX_AGE_SECONDS = 60
        session = self._make_session()
        signal = self._buy_signal(session)
        signal['timestamp'] = (datetime.now() - timedelta(seconds=120)).isoformat()

        result = self.manager.execute_grid_trade(signal)

        self.assertFalse(result)
        self.executor.buy_stock.assert_not_called()

    def test_session_mismatch_rejected_before_order(self):
        config.ENABLE_SIMULATION_MODE = False
        session = self._make_session()
        signal = self._buy_signal(session)
        signal['session_id'] = session.id + 999

        result = self.manager.execute_grid_trade(signal)

        self.assertFalse(result)
        self.executor.buy_stock.assert_not_called()

    def test_price_drift_rejected_before_order(self):
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_SIGNAL_MAX_PRICE_DRIFT_RATIO = 0.01
        session = self._make_session()
        signal = self._buy_signal(session, trigger_price=10.0)
        self.position_manager.data_manager.get_latest_data.return_value = {'lastPrice': 10.25}

        result = self.manager.execute_grid_trade(signal)

        self.assertFalse(result)
        self.executor.buy_stock.assert_not_called()

    def test_trading_executor_deal_callback_without_old_grid_switch(self):
        executor = TradingExecutor.__new__(TradingExecutor)
        executor.position_manager = Mock()
        executor.position_manager.grid_manager = Mock()
        executor.order_cache = {}
        executor.callbacks = {}
        executor._save_trade_record = Mock()
        executor._update_position_after_trade = Mock()

        old_present = hasattr(config, 'GRID_TRADING_ENABLED')
        old_value = getattr(config, 'GRID_TRADING_ENABLED', None)
        if old_present:
            delattr(config, 'GRID_TRADING_ENABLED')
        try:
            self.assertFalse(hasattr(config, 'GRID_TRADING_ENABLED'))
            with patch.object(config, 'ENABLE_SIMULATION_MODE', False), \
                 patch.object(config, 'ENABLE_GRID_TRADING', True):
                executor._on_deal_callback(FakeDealInfo())
        finally:
            if old_present:
                setattr(config, 'GRID_TRADING_ENABLED', old_value)

        executor.position_manager.grid_manager.handle_deal_callback.assert_called_once()

    def test_trading_executor_deal_callback_delegates_grid_recording(self):
        executor = TradingExecutor.__new__(TradingExecutor)
        executor.position_manager = Mock()
        executor.position_manager.grid_manager = Mock()
        executor.position_manager.grid_manager.pending_grid_orders = {'ORDER_OLD_SWITCH': {}}
        executor.position_manager.grid_manager.handle_deal_callback.return_value = True
        executor.order_cache = {
            'ORDER_OLD_SWITCH': {
                'strategy': config.GRID_STRATEGY_NAME,
                'trade_type': 'BUY'
            }
        }
        executor.callbacks = {}
        executor._save_trade_record = Mock()
        executor._update_position_after_trade = Mock()

        with patch.object(config, 'ENABLE_SIMULATION_MODE', False), \
             patch.object(config, 'ENABLE_GRID_TRADING', True), \
             patch.object(config, 'GRID_CONFIRM_LIVE_ORDER_BY_DEAL', True):
            executor._on_deal_callback(FakeDealInfo())

        executor.position_manager.grid_manager.handle_deal_callback.assert_called_once()
        executor._save_trade_record.assert_not_called()
        executor._update_position_after_trade.assert_called_once()

    def test_trading_executor_order_callback_notifies_grid_manager(self):
        executor = TradingExecutor.__new__(TradingExecutor)
        executor.position_manager = Mock()
        executor.position_manager.grid_manager = Mock()
        executor.order_cache = {}
        executor.callbacks = {}

        with patch.object(config, 'ENABLE_GRID_TRADING', True):
            executor._on_order_callback(FakeOrderInfo('ORDER_NOTIFY', status=54))

        executor.position_manager.grid_manager.handle_order_callback.assert_called_once()

    def test_easy_qmt_order_callback_dispatches_external_callbacks(self):
        callback = MyXtQuantTraderCallback({})
        trader = easy_qmt_trader.__new__(easy_qmt_trader)
        trader._callback = callback
        handler = Mock()
        order = FakeOrderInfo('ORDER_QMT_NOTIFY', status=54)

        trader.register_order_callback(handler)
        callback.on_stock_order(order)

        handler.assert_called_once_with(order)

    def test_position_manager_order_callback_notifies_grid_manager(self):
        position_manager = PositionManager.__new__(PositionManager)
        position_manager.grid_manager = Mock()
        order = FakeOrderInfo('ORDER_PM_NOTIFY', status=57)

        with patch.object(config, 'ENABLE_GRID_TRADING', True):
            position_manager._on_order_callback(order)

        position_manager.grid_manager.handle_order_callback.assert_called_once_with(order)


if __name__ == '__main__':
    unittest.main(verbosity=2)
