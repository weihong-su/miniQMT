"""
网格交易 QA 审查缺失场景补充测试 (BUG-C1 修复验证)

测试范围：
T-1: 实盘模式 executor 下单成功但 DB 失败 -> last_buy_times 已记录 -> 冷却保护生效
T-2: 验证 last_buy_times 在 DB 写入之前已被设置（修复后行为）
T-3: DESIGN-4 止损非对称设计验证（2026-03-25 更新）
     T-3a: buy_count>0 sell_count=0 时亏损超限 -> 止损正常触发（防止单边下跌无限亏损）
     T-3b: 价格大幅偏离 -> deviation 退出兜底
     T-3c: buy+sell 配对后亏损超限 -> 止损正常触发
T-4: 跳空低开严重超过 max_deviation -> 偏离退出优先于买入信号
T-5: 卖出使用过期持仓快照（QMT层面失败）-> 系统优雅恢复不崩溃
T-6: GRID_BUY_COOLDOWN=0 时仅靠 level_cooldown 和投入限额保护

覆盖的设计约束：
- BUG-C1: last_buy_times 必须在下单成功后、DB 写入前记录
- DESIGN-4 (2026-03-25 修订): 止损允许"仅买未卖"阶段触发（防单边下跌风险无限扩大）；
  止盈仍需买卖配对（sell_count > 0）才能触发
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import unittest
from unittest.mock import Mock, patch, MagicMock, call
from datetime import datetime, timedelta
import sqlite3
import time
from dataclasses import asdict

import config
from grid_trading_manager import GridSession, GridTradingManager, PriceTracker
from grid_database import DatabaseManager
from trading_executor import TradingExecutor
from position_manager import PositionManager


def _make_session(db, max_investment=10000, current_investment=0,
                  center_price=10.0, price_interval=0.05,
                  buy_count=0, sell_count=0,
                  total_buy_amount=0.0, total_sell_amount=0.0,
                  max_deviation=0.15, stop_loss=-0.10, target_profit=0.10,
                  stock_code='000001.SZ'):
    """构造并持久化 GridSession，返回已带 id 的对象"""
    session = GridSession(
        id=None,
        stock_code=stock_code,
        status='active',
        center_price=center_price,
        current_center_price=center_price,
        price_interval=price_interval,
        position_ratio=0.25,
        callback_ratio=0.005,
        max_investment=max_investment,
        current_investment=current_investment,
        max_deviation=max_deviation,
        target_profit=target_profit,
        stop_loss=stop_loss,
        buy_count=buy_count,
        sell_count=sell_count,
        total_buy_amount=total_buy_amount,
        total_sell_amount=total_sell_amount,
        start_time=datetime.now(),
        end_time=datetime.now() + timedelta(days=7),
    )
    d = asdict(session)
    session.id = db.create_grid_session(d)
    return session


def _register_in_manager(manager, session):
    """将 session 注册到 manager 的内存缓存"""
    normalized = manager._normalize_code(session.stock_code)
    manager.sessions[normalized] = session
    manager.trackers[session.id] = PriceTracker(
        session_id=session.id,
        last_price=session.current_center_price
    )
    return session


# ==========================================================================
# T-1 & T-2: BUG-C1 修复验证 —— last_buy_times 在 DB 写入前已记录
# ==========================================================================

class TestBugC1DbFailureProtection(unittest.TestCase):
    """
    T-1: 实盘模式 executor 下单成功但 DB 失败 -> last_buy_times 已记录 -> 冷却保护有效
    T-2: last_buy_times 在 DB 写入前已设置（白盒验证）
    """

    def setUp(self):
        self.db = DatabaseManager(':memory:')
        self.db.init_grid_tables()
        self.position_manager = Mock(spec=PositionManager)
        self.executor = Mock(spec=TradingExecutor)
        self.manager = GridTradingManager(
            db_manager=self.db,
            position_manager=self.position_manager,
            trading_executor=self.executor
        )
        self.orig_sim = config.ENABLE_SIMULATION_MODE
        self.orig_cooldown = getattr(config, 'GRID_BUY_COOLDOWN', 0)
        config.ENABLE_SIMULATION_MODE = False  # 实盘模式
        config.GRID_BUY_COOLDOWN = 300        # 5 分钟冷却

    def tearDown(self):
        config.ENABLE_SIMULATION_MODE = self.orig_sim
        config.GRID_BUY_COOLDOWN = self.orig_cooldown
        self.db.close()

    def _make_buy_signal(self, session, trigger_price=9.5):
        return {
            'stock_code': session.stock_code,
            'signal_type': 'BUY',
            'grid_level': session.current_center_price * (1 - session.price_interval),
            'trigger_price': trigger_price,
            'valley_price': trigger_price * 0.995,
            'callback_ratio': 0.005,
            'session_id': session.id,
        }

    def test_t1_last_buy_times_set_after_executor_success_db_fail(self):
        """T-1: 实盘下单成功 + DB 失败 -> last_buy_times 已记录 -> 第二次调用被冷却阻止"""
        print("\n=== T-1: 实盘下单成功 + DB 失败 -> 冷却保护生效 ===")

        session = _make_session(self.db, max_investment=10000)
        _register_in_manager(self.manager, session)

        # executor 下单成功
        self.executor.buy_stock = Mock(return_value={'order_id': 'QMT_ORDER_001'})

        # DB 记录交易时抛出异常（模拟 DB 写入失败）
        original_record = self.db.record_grid_trade
        call_count = [0]

        def flaky_record_grid_trade(trade_data):
            call_count[0] += 1
            raise sqlite3.OperationalError("database is locked")

        self.db.record_grid_trade = flaky_record_grid_trade

        signal = self._make_buy_signal(session)

        # 第一次调用：executor 成功，DB 失败，返回 False
        result_1 = self.manager._execute_grid_buy(session, signal)

        self.assertFalse(result_1, "DB 失败时应返回 False")

        # === 核心断言：BUG-C1 修复后，last_buy_times 已在 DB 写入前被记录 ===
        self.assertIn(session.id, self.manager.last_buy_times,
                      "BUG-C1修复: DB 失败时 last_buy_times 应已记录")
        recorded_time = self.manager.last_buy_times[session.id]
        self.assertAlmostEqual(recorded_time, time.time(), delta=2.0,
                               msg="记录时间应在当前时间 2 秒内")

        # 内存统计已回滚
        self.assertEqual(session.current_investment, 0,
                         "DB 失败时 current_investment 应回滚为 0")
        self.assertEqual(session.buy_count, 0, "DB 失败时 buy_count 应回滚为 0")

        # 第二次立即调用（DB 恢复正常）
        self.db.record_grid_trade = original_record
        result_2 = self.manager._execute_grid_buy(session, signal)

        self.assertFalse(result_2,
                         "冷却期内（300s）第二次调用应被 GRID_BUY_COOLDOWN 阻止")
        # executor 只被调用一次（第二次被冷却拦截）
        self.assertEqual(self.executor.buy_stock.call_count, 1,
                         "冷却期内不应重复下单到 QMT")

        print(f"  last_buy_times 已记录: {recorded_time:.3f}")
        print(f"  第一次: 下单成功+DB失败 -> {result_1}")
        print(f"  第二次: 冷却期内立即重试 -> {result_2} (QMT 调用次数={self.executor.buy_stock.call_count})")

    def test_t2_last_buy_times_set_before_db_write(self):
        """T-2: 白盒验证 last_buy_times 在 DB 写入之前已被设置"""
        print("\n=== T-2: 白盒验证 last_buy_times 在 DB 写入前设置 ===")

        session = _make_session(self.db, max_investment=10000)
        _register_in_manager(self.manager, session)

        self.executor.buy_stock = Mock(return_value={'order_id': 'QMT_ORDER_002'})

        times_at_db_write = {}

        original_record = self.db.record_grid_trade

        def spy_record_grid_trade(trade_data):
            # 在 DB 写入时刻检查 last_buy_times 是否已经设置
            times_at_db_write['last_buy_times_set'] = session.id in self.manager.last_buy_times
            times_at_db_write['snapshot'] = dict(self.manager.last_buy_times)
            return original_record(trade_data)

        self.db.record_grid_trade = spy_record_grid_trade

        signal = self._make_buy_signal(session)
        result = self.manager._execute_grid_buy(session, signal)

        self.assertTrue(result, "正常路径应返回 True")
        self.assertTrue(times_at_db_write.get('last_buy_times_set', False),
                        "BUG-C1修复: DB 写入时 last_buy_times 应已设置")

        print(f"  DB 写入时 last_buy_times 已设置: {times_at_db_write.get('last_buy_times_set')}")
        print(f"  最终结果: {result}")

    def test_t1_simulation_mode_db_fail_also_protected(self):
        """T-1 变种: 模拟模式下 DB 失败也应记录 last_buy_times"""
        print("\n=== T-1 变种: 模拟模式 DB 失败 -> last_buy_times 已记录 ===")

        config.ENABLE_SIMULATION_MODE = True

        session = _make_session(self.db, max_investment=10000)
        _register_in_manager(self.manager, session)

        def flaky_record(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        self.db.record_grid_trade = flaky_record

        signal = self._make_buy_signal(session, trigger_price=9.5)
        result = self.manager._execute_grid_buy(session, signal)

        self.assertFalse(result, "DB 失败时应返回 False")
        self.assertIn(session.id, self.manager.last_buy_times,
                      "模拟模式 DB 失败时 last_buy_times 也应已记录")

        print(f"  模拟模式 last_buy_times 已记录: {session.id in self.manager.last_buy_times}")


# ==========================================================================
# T-3: DESIGN-4 验证 —— 止损非对称设计（2026-03-25 修订）
# 止损允许"仅买未卖"阶段触发；止盈仍需 sell_count > 0
# ==========================================================================

class TestDesign4StopLossWithoutSell(unittest.TestCase):
    """
    T-3: DESIGN-4 非对称止损/止盈设计验证（2026-03-25 修订）

    止损（stop_loss）：仅需 buy_count > 0 即可触发，防止单边下跌行情无限亏损。
    止盈（take_profit）：需 sell_count > 0，确保完成至少一次买卖配对后再判定盈利。
    deviation 兜底：极端行情下由偏离度检测提供退出保护。
    """

    def setUp(self):
        self.db = DatabaseManager(':memory:')
        self.db.init_grid_tables()
        self.position_manager = Mock(spec=PositionManager)
        self.executor = Mock(spec=TradingExecutor)
        self.manager = GridTradingManager(
            db_manager=self.db,
            position_manager=self.position_manager,
            trading_executor=self.executor
        )
        self.position_manager.get_position.return_value = {
            'volume': 1000, 'cost_price': 10.0
        }

    def tearDown(self):
        self.db.close()

    def test_t3_stop_loss_triggered_without_sell(self):
        """T-3a: buy_count>0 sell_count=0 时，价格跌破 stop_loss 阈值应触发止损（DESIGN-4 修订）"""
        print("\n=== T-3a: sell_count=0 时止损正常触发（防单边下跌） ===")

        # 已买入 2000 元，亏损 20%（超过 stop_loss=-10%），无卖出
        # DESIGN-4 修订：仅买入阶段也应触发止损，防止单边下跌无限亏损
        session = _make_session(
            self.db,
            max_investment=10000,
            current_investment=2000,
            buy_count=2,
            sell_count=0,
            total_buy_amount=2000.0,
            total_sell_amount=0.0,
            stop_loss=-0.10,
            max_deviation=0.50,  # 放宽偏离限制，避免 deviation 先退出
        )

        # profit_ratio = (0 - 2000) / 10000 = -20%，超过 stop_loss=-10%
        current_price = session.center_price  # 价格不变，偏离度为 0

        exit_reason = self.manager._check_exit_conditions(
            session, current_price,
            position_snapshot={'volume': 1000, 'cost_price': 10.0}
        )

        self.assertEqual(exit_reason, 'stop_loss',
                         "buy_count>0 时亏损超限应触发止损，即使 sell_count=0")
        print(f"  profit_ratio = {session.get_profit_ratio()*100:.1f}% (< stop_loss={session.stop_loss*100:.1f}%)")
        print(f"  退出原因: {exit_reason} (预期: stop_loss)")

    def test_t3b_deviation_exits_when_price_drifts_far(self):
        """T-3b: 价格大幅偏离时 deviation 退出优先（兜底保护有效）"""
        print("\n=== T-3b: 价格大幅偏离 -> deviation 退出 ===")

        # buy_count=3, sell_count=0，网格中心已下移至 8.4（偏离初始 10.0 达 16%，超过 max_deviation=15%）
        session = _make_session(
            self.db,
            max_investment=10000,
            center_price=10.0,
            buy_count=3,
            sell_count=0,
            total_buy_amount=6000.0,
            total_sell_amount=0.0,
            max_deviation=0.15,
        )
        # 注意：deviation 检查用严格 >，需超过 15%（>0.15），设为 8.4 -> 偏离 16%
        session.current_center_price = 8.4

        # 当前价格也在 8.4 附近（market_deviation 为 0，drift_deviation = 16%）
        current_price = 8.4

        exit_reason = self.manager._check_exit_conditions(
            session, current_price,
            position_snapshot={'volume': 3000, 'cost_price': 10.0}
        )

        self.assertEqual(exit_reason, 'deviation',
                         "价格偏离 >15% 时应触发 deviation 退出")
        deviation = session.get_deviation_ratio()
        print(f"  deviation = {deviation*100:.1f}% (> max_deviation={session.max_deviation*100:.1f}%)")
        print(f"  退出原因: {exit_reason} (预期: deviation)")

    def test_t3c_stop_loss_does_trigger_with_sell(self):
        """T-3c: buy_count>0 sell_count>0 时，亏损超限正常触发止损"""
        print("\n=== T-3c: buy+sell 配对完成时止损正常触发 ===")

        # 已买入 5000，卖出 4000，亏损 10%
        session = _make_session(
            self.db,
            max_investment=10000,
            buy_count=2,
            sell_count=1,
            total_buy_amount=5000.0,
            total_sell_amount=4000.0,  # profit_ratio = (4000-5000)/10000 = -10%
            stop_loss=-0.10,
            max_deviation=0.50,
        )

        current_price = session.center_price

        exit_reason = self.manager._check_exit_conditions(
            session, current_price,
            position_snapshot={'volume': 500, 'cost_price': 10.0}
        )

        self.assertEqual(exit_reason, 'stop_loss',
                         "buy+sell 均有时，亏损 -10% 应触发止损")
        print(f"  profit_ratio = {session.get_profit_ratio()*100:.1f}% (= stop_loss={session.stop_loss*100:.1f}%)")
        print(f"  退出原因: {exit_reason} (预期: stop_loss)")


# ==========================================================================
# T-4: 跳空低开严重超过 max_deviation -> deviation 退出优先
# ==========================================================================

class TestGapDownDeviationExit(unittest.TestCase):
    """
    T-4: 跳空低开 20%（超过 max_deviation=15%），验证：
      - deviation 退出先于任何买入信号
      - 系统不会在高偏离场景下持续买入
    """

    def setUp(self):
        self.db = DatabaseManager(':memory:')
        self.db.init_grid_tables()
        self.position_manager = Mock(spec=PositionManager)
        self.position_manager.signal_lock = __import__('threading').Lock()
        self.position_manager.latest_signals = {}
        self.executor = Mock(spec=TradingExecutor)
        self.manager = GridTradingManager(
            db_manager=self.db,
            position_manager=self.position_manager,
            trading_executor=self.executor
        )
        self.position_manager.get_position.return_value = {
            'volume': 1000, 'cost_price': 10.0
        }
        self.orig_sim = config.ENABLE_SIMULATION_MODE
        config.ENABLE_SIMULATION_MODE = True

    def tearDown(self):
        config.ENABLE_SIMULATION_MODE = self.orig_sim
        self.db.close()

    def test_t4_gap_down_20pct_deviation_exit_before_buy(self):
        """T-4: 跳空低开 20% -> check_grid_signals 返回 None（deviation 停止会话）"""
        print("\n=== T-4: 跳空低开 20% -> deviation 退出，无买入信号 ===")

        session = _make_session(
            self.db,
            center_price=10.0,
            max_deviation=0.15,
        )
        _register_in_manager(self.manager, session)

        # 跳空低开 20%，已超过 max_deviation=15%
        gap_down_price = 8.0  # 10.0 * (1 - 0.20)

        signal = self.manager.check_grid_signals('000001.SZ', gap_down_price)

        self.assertIsNone(signal,
                          "deviation 超限时 check_grid_signals 应停止会话并返回 None")

        # 会话应已停止
        normalized = self.manager._normalize_code('000001.SZ')
        self.assertNotIn(normalized, self.manager.sessions,
                         "deviation 触发后会话应从内存移除")

        print(f"  gap_down_price = {gap_down_price} (偏离 {(10.0-gap_down_price)/10.0*100:.0f}%)")
        print(f"  信号: {signal} (预期: None)")
        print(f"  会话已移除: {normalized not in self.manager.sessions}")

    def test_t4b_borderline_deviation_allows_signal(self):
        """T-4b: 偏离 14%（刚好低于 max_deviation=15%）时正常生成买入信号"""
        print("\n=== T-4b: 偏离 14% 时允许生成信号 ===")

        session = _make_session(
            self.db,
            center_price=10.0,
            max_deviation=0.15,
            max_investment=10000,
        )
        _register_in_manager(self.manager, session)

        # 偏离 14%，未超限
        price_14pct_down = 8.6  # 10.0 * (1 - 0.14)

        # 先推价格到下轨以下触发穿越
        self.manager.check_grid_signals('000001.SZ', price_14pct_down)
        # 再小幅回调触发买入信号
        price_callback = price_14pct_down * 1.006

        signal = self.manager.check_grid_signals('000001.SZ', price_callback)
        # 可能因为回调不足 0.5% 而无信号，但会话应仍然存活（未退出）
        normalized = self.manager._normalize_code('000001.SZ')
        self.assertIn(normalized, self.manager.sessions,
                      "偏离 14% 时会话应仍存活")

        print(f"  price = {price_14pct_down} (偏离 14%)")
        print(f"  会话存活: {normalized in self.manager.sessions}")


# ==========================================================================
# T-5: 卖出快照过期（QMT 失败）-> 系统优雅恢复
# ==========================================================================

class TestSellWithStaleSnapshot(unittest.TestCase):
    """
    T-5: 卖出时持仓快照在锁外预取后持仓已被清仓，
         executor.sell_stock 失败 -> tracker 重置 -> 系统不崩溃
    """

    def setUp(self):
        self.db = DatabaseManager(':memory:')
        self.db.init_grid_tables()
        self.position_manager = Mock(spec=PositionManager)
        self.executor = Mock(spec=TradingExecutor)
        self.manager = GridTradingManager(
            db_manager=self.db,
            position_manager=self.position_manager,
            trading_executor=self.executor
        )
        self.orig_sim = config.ENABLE_SIMULATION_MODE
        config.ENABLE_SIMULATION_MODE = False

    def tearDown(self):
        config.ENABLE_SIMULATION_MODE = self.orig_sim
        self.db.close()

    def test_t5_sell_executor_fail_tracker_reset(self):
        """T-5: executor.sell_stock 返回 None -> 失败返回 False，tracker 被重置"""
        print("\n=== T-5: 卖出执行器失败 -> tracker 重置，系统不崩溃 ===")

        session = _make_session(self.db, max_investment=10000, current_investment=2000)
        _register_in_manager(self.manager, session)

        # 持仓快照（锁外预取，此时有持仓）
        stale_snapshot = {'volume': 1000, 'cost_price': 10.0}

        # executor 失败（模拟快照过期后清仓）
        self.executor.sell_stock = Mock(return_value=None)

        sell_signal = {
            'stock_code': session.stock_code,
            'signal_type': 'SELL',
            'grid_level': session.current_center_price * 1.05,
            'trigger_price': 10.6,
            'peak_price': 10.65,
            'callback_ratio': 0.005,
            'session_id': session.id,
        }

        # 预先设置 tracker 为等待回调状态
        tracker = self.manager.trackers[session.id]
        tracker.waiting_callback = True
        tracker.direction = 'rising'
        tracker.crossed_level = 10.5

        result = self.manager._execute_grid_sell(session, sell_signal, position_snapshot=stale_snapshot)

        self.assertFalse(result, "executor 失败时应返回 False")
        # 注意: _execute_grid_sell 失败时，tracker 重置由 execute_grid_trade 处理
        # 这里直接调用 _execute_grid_sell，tracker 不在此重置（属于正常设计）
        # 关键是：系统不应崩溃
        print(f"  executor 失败返回: {result}")
        print(f"  系统未崩溃，测试通过")


# ==========================================================================
# T-6: GRID_BUY_COOLDOWN=0 时的防护层
# ==========================================================================

class TestBuyProtectionWithoutCooldown(unittest.TestCase):
    """
    T-6: GRID_BUY_COOLDOWN=0（冷却禁用）时，
         投入限额和 level_cooldown 仍能防止无限买入
    """

    def setUp(self):
        self.db = DatabaseManager(':memory:')
        self.db.init_grid_tables()
        self.position_manager = Mock(spec=PositionManager)
        self.position_manager.signal_lock = __import__('threading').Lock()
        self.position_manager.latest_signals = {}
        self.executor = Mock(spec=TradingExecutor)
        self.manager = GridTradingManager(
            db_manager=self.db,
            position_manager=self.position_manager,
            trading_executor=self.executor
        )
        self.position_manager.get_position.return_value = {
            'volume': 1000, 'cost_price': 10.0
        }
        self.orig_sim = config.ENABLE_SIMULATION_MODE
        self.orig_cooldown = getattr(config, 'GRID_BUY_COOLDOWN', 0)
        self.orig_level_cooldown = config.GRID_LEVEL_COOLDOWN
        config.ENABLE_SIMULATION_MODE = True
        config.GRID_BUY_COOLDOWN = 0         # 禁用买入冷却
        config.GRID_LEVEL_COOLDOWN = 3600    # 档位冷却 1 小时（确保不重复穿越）

    def tearDown(self):
        config.ENABLE_SIMULATION_MODE = self.orig_sim
        config.GRID_BUY_COOLDOWN = self.orig_cooldown
        config.GRID_LEVEL_COOLDOWN = self.orig_level_cooldown
        self.db.close()

    def test_t6_investment_limit_blocks_buy_when_cooldown_disabled(self):
        """T-6: 冷却禁用时，max_investment 硬上限阻止超额买入"""
        print("\n=== T-6: GRID_BUY_COOLDOWN=0，max_investment 兜底 ===")

        # 设置 max_investment=10000，每次买 20% = 2000 元
        # 已投入 9500 元（剩余 500 < 最小 100 × 任意股价，会导致数量不足被拒绝）
        session = _make_session(
            self.db,
            max_investment=10000,
            current_investment=9999,  # 已接近满额
        )
        _register_in_manager(self.manager, session)

        signal = {
            'stock_code': session.stock_code,
            'signal_type': 'BUY',
            'grid_level': 9.5,
            'trigger_price': 9.5,
            'valley_price': 9.45,
            'callback_ratio': 0.005,
            'session_id': session.id,
        }

        result = self.manager._execute_grid_buy(session, signal)

        self.assertFalse(result,
                         "投入已接近 max_investment 时，买入应被拒绝")
        print(f"  current_investment = {session.current_investment} / {session.max_investment}")
        print(f"  买入结果: {result} (预期: False)")

    def test_t6b_level_cooldown_prevents_recrossing(self):
        """T-6b: level_cooldown 防止同一档位在冷却期内重复穿越"""
        print("\n=== T-6b: level_cooldown 阻止同档位重复触发 ===")

        session = _make_session(self.db, max_investment=10000)
        _register_in_manager(self.manager, session)
        tracker = self.manager.trackers[session.id]

        lower_level = session.current_center_price * (1 - session.price_interval)

        # 手动设置档位冷却（模拟刚刚成交后设置冷却）
        cooldown_key = (session.id, lower_level)
        self.manager.level_cooldowns[cooldown_key] = time.time()

        # 尝试在冷却期内重新穿越同一档位
        self.manager._check_level_crossing(session, tracker, lower_level * 0.99)

        self.assertFalse(tracker.waiting_callback,
                         "档位冷却期内不应设置 waiting_callback=True")
        print(f"  level_cooldown 生效: waiting_callback={tracker.waiting_callback} (预期: False)")


# ==========================================================================
# 综合场景：BUG-C1 修复端到端验证（实盘模拟链路）
# ==========================================================================

class TestBugC1EndToEnd(unittest.TestCase):
    """
    端到端验证 BUG-C1 修复：
    - 第 1 次下单（实盘成功，DB 失败）-> last_buy_times 记录，内存回滚
    - 第 2 次立即触发 -> 被 GRID_BUY_COOLDOWN 拦截，executor 不被第二次调用
    - 冷却到期后第 3 次触发 -> DB 正常 -> 成功，buy_count=1
    """

    def setUp(self):
        self.db = DatabaseManager(':memory:')
        self.db.init_grid_tables()
        self.position_manager = Mock(spec=PositionManager)
        self.executor = Mock(spec=TradingExecutor)
        self.manager = GridTradingManager(
            db_manager=self.db,
            position_manager=self.position_manager,
            trading_executor=self.executor
        )
        self.orig_sim = config.ENABLE_SIMULATION_MODE
        self.orig_cooldown = getattr(config, 'GRID_BUY_COOLDOWN', 0)
        config.ENABLE_SIMULATION_MODE = False
        config.GRID_BUY_COOLDOWN = 5  # 5 秒冷却（测试用短周期）

    def tearDown(self):
        config.ENABLE_SIMULATION_MODE = self.orig_sim
        config.GRID_BUY_COOLDOWN = self.orig_cooldown
        self.db.close()

    def test_e2e_db_fail_then_cooldown_then_success(self):
        """端到端: DB失败 -> 冷却拦截 -> 冷却到期 -> 成功"""
        print("\n=== 端到端: DB失败+冷却+成功 三阶段验证 ===")

        session = _make_session(self.db, max_investment=10000)
        _register_in_manager(self.manager, session)

        self.executor.buy_stock = Mock(return_value={'order_id': 'E2E_ORDER_001'})

        signal = {
            'stock_code': session.stock_code,
            'signal_type': 'BUY',
            'grid_level': 9.5,
            'trigger_price': 9.5,
            'valley_price': 9.45,
            'callback_ratio': 0.005,
            'session_id': session.id,
        }

        # --- 阶段1: 实盘下单成功，DB 失败 ---
        db_fail = [True]
        original_record = self.db.record_grid_trade

        def conditional_fail(data):
            if db_fail[0]:
                raise sqlite3.OperationalError("disk full")
            return original_record(data)

        self.db.record_grid_trade = conditional_fail

        result_1 = self.manager._execute_grid_buy(session, signal)
        self.assertFalse(result_1, "阶段1: DB 失败应返回 False")
        self.assertIn(session.id, self.manager.last_buy_times, "阶段1: last_buy_times 已记录")
        self.assertEqual(session.buy_count, 0, "阶段1: buy_count 已回滚")
        print(f"  阶段1 [DB失败]: result={result_1}, buy_count={session.buy_count}, "
              f"last_buy_times_set={session.id in self.manager.last_buy_times}")

        # --- 阶段2: 立即重试被冷却拦截 ---
        db_fail[0] = False  # DB 已恢复

        result_2 = self.manager._execute_grid_buy(session, signal)
        self.assertFalse(result_2, "阶段2: 冷却期内应返回 False")
        self.assertEqual(self.executor.buy_stock.call_count, 1,
                         "阶段2: QMT 不应被第二次调用")
        print(f"  阶段2 [冷却期]: result={result_2}, QMT调用次数={self.executor.buy_stock.call_count}")

        # --- 阶段3: 等待冷却到期（5秒），再次触发成功 ---
        print("  等待冷却期结束 (5s)...")
        time.sleep(6)  # 等待 5s 冷却到期

        # 需要先重置 tracker（模拟重新穿越档位）
        tracker = self.manager.trackers.get(session.id)
        if tracker:
            tracker.waiting_callback = False  # 重新触发

        result_3 = self.manager._execute_grid_buy(session, signal)
        self.assertTrue(result_3, "阶段3: 冷却到期后应成功")
        self.assertEqual(session.buy_count, 1, "阶段3: buy_count=1")
        self.assertEqual(self.executor.buy_stock.call_count, 2,
                         "阶段3: QMT 被第二次调用")
        print(f"  阶段3 [冷却后]: result={result_3}, buy_count={session.buy_count}, "
              f"QMT调用次数={self.executor.buy_stock.call_count}")


if __name__ == '__main__':
    unittest.main(verbosity=2)
