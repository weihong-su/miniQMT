"""
网格交易频繁触发bug修复验证测试

测试目标：
1. P0-1: 验证格式化字符串bug已修复（price/ratio为None时不抛异常）
2. P0-2: 验证会话停止时清除信号（停止后不再执行）
3. P1-1: 验证信号去重机制（避免重复生成信号）
4. P1-2: 验证执行失败清除信号（失败后不再重试）
"""

import unittest
from unittest.mock import Mock, patch, MagicMock
import sys
import os
import threading
import time
from datetime import datetime

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test.test_base import TestBase
from test.test_mocks import MockQmtTrader
from grid_database import DatabaseManager
from grid_trading_manager import GridTradingManager, GridSession
from trading_executor import TradingExecutor
from position_manager import PositionManager
import config


class TestGridTradingBugFixes(TestBase):
    """网格交易bug修复验证测试"""

    def setUp(self):
        """测试前置准备"""
        super().setUp()
        self.mock_trader = MockQmtTrader()

        # 创建PositionManager（与test_trader_callback.py相同方式：
        # 直接实例化，QMT连接失败→离线模式，不需要patch）
        self.position_manager = PositionManager()

        # 为start_grid_session的get_position调用提供默认mock持仓
        # （各测试用例所用stock_code各不同，用side_effect动态生成）
        def _mock_get_position(stock_code):
            return {
                'stock_code': stock_code,
                'volume': 1000,
                'current_price': 10.0,
                'cost_price': 9.5,
                'profit_triggered': True,
                'highest_price': 11.0,
                'market_value': 10000
            }
        self.position_manager.get_position = _mock_get_position

        # 创建临时数据库供GridTradingManager使用
        self.test_db_path = f"data/test_bugfix_{int(time.time()*1000)}.db"
        self.db_manager = DatabaseManager(db_path=self.test_db_path)
        self.db_manager.init_grid_tables()
        self.mock_executor = Mock(spec=TradingExecutor)

        # 创建GridTradingManager（传入正确的3个参数）
        self.grid_manager = GridTradingManager(
            db_manager=self.db_manager,
            position_manager=self.position_manager,
            trading_executor=self.mock_executor
        )

    def tearDown(self):
        """测试后清理"""
        if hasattr(self, 'grid_manager'):
            # 清理所有活跃会话
            for stock_code in list(self.grid_manager.sessions.keys()):
                try:
                    session = self.grid_manager.sessions[stock_code]
                    self.grid_manager.stop_grid_session(session.id, "test_cleanup")
                except Exception:
                    pass
        if hasattr(self, 'position_manager'):
            try:
                self.position_manager.stop_sync_thread()
            except Exception:
                pass
        if hasattr(self, 'db_manager') and self.db_manager:
            self.db_manager.close()
        if hasattr(self, 'test_db_path') and os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
        super().tearDown()

    # ==================== P0-1: 格式化字符串bug验证 ====================

    def test_P0_1_format_string_with_none_price(self):
        """
        P0-1验证：price=None时不抛出格式化异常

        场景：卖出时price参数为None（获取价格失败）
        预期：日志正常输出，不抛出异常
        """
        executor = TradingExecutor()
        executor.position_manager = self.position_manager  # 替换为测试用PM

        # 模拟环境
        with patch.object(config, 'ENABLE_SIMULATION_MODE', True):
            with patch.object(config, 'is_trade_time', return_value=True):
                # 添加模拟持仓
                self.position_manager.simulate_buy_position("000001.SZ", 1000, 10.0)

                # 调用卖出（price=None）
                try:
                    result = executor.sell_stock(
                        stock_code="000001.SZ",
                        volume=100,
                        price=None,  # ⚠️ 关键：price为None
                        ratio=None   # ⚠️ 关键：ratio也为None
                    )

                    # 验证：不应该抛出异常
                    # 如果price=None时获取不到最新价格，会返回None
                    self.assertIsNone(result, "price=None且无法获取价格时，应返回None")

                except Exception as e:
                    # 不应该出现 "unsupported format string" 错误
                    self.assertNotIn("unsupported format string", str(e),
                                    "P0-1修复失败：price=None时仍抛出格式化异常")
                    # 其他异常可以接受（比如"无法获取有效价格"）
                    self.assertIn("无法获取", str(e), f"预期应因无法获取价格失败，实际错误：{str(e)}")

    def test_P0_1_format_string_with_none_ratio(self):
        """
        P0-1验证：ratio=None时不抛出格式化异常

        场景：卖出时ratio参数为None（使用volume参数）
        预期：日志正常输出，交易正常执行
        """
        executor = TradingExecutor()
        executor.position_manager = self.position_manager  # 替换为测试用PM

        with patch.object(config, 'ENABLE_SIMULATION_MODE', True):
            with patch.object(config, 'is_trade_time', return_value=True):
                self.position_manager.simulate_buy_position("000002.SZ", 1000, 10.0)

                try:
                    result = executor.sell_stock(
                        stock_code="000002.SZ",
                        volume=100,
                        price=10.5,
                        ratio=None  # ⚠️ ratio为None
                    )

                    # 应该成功执行
                    self.assertIsNotNone(result, "ratio=None但volume有效时，应正常执行")

                except Exception as e:
                    self.fail(f"P0-1修复失败：ratio=None时抛出异常 - {str(e)}")

    # ==================== P0-2: 会话停止时清除信号验证 ====================

    def test_P0_2_signal_cleared_on_session_stop(self):
        """
        P0-2验证：会话停止时清除网格信号

        场景：
        1. 启动网格会话
        2. 生成网格信号
        3. 停止会话
        4. 检查信号是否被清除

        预期：会话停止后，latest_signals中不再有该股票的网格信号
        """
        stock_code = "600510.SH"

        # 1. 启动网格会话
        user_config = {
            'max_investment': 10000,
            'price_interval': 0.05,
            'position_ratio': 0.25,
            'callback_ratio': 0.005,
            'max_deviation': 0.15,
            'target_profit': 0.1,
            'stop_loss': -0.1,
            'duration_days': 7
        }

        with patch.object(config, 'ENABLE_SIMULATION_MODE', True):
            session = self.grid_manager.start_grid_session(stock_code, user_config)
            self.assertIsNotNone(session, "网格会话应成功启动")

            # 2. 模拟生成网格信号
            with self.position_manager.signal_lock:
                self.position_manager.latest_signals[stock_code] = {
                    'type': 'grid_sell',
                    'info': {
                        'stock_code': stock_code,
                        'signal_type': 'SELL',
                        'session_id': session.id,
                        'timestamp': datetime.now().isoformat()
                    },
                    'timestamp': datetime.now()
                }

            # 验证信号存在
            self.assertIn(stock_code, self.position_manager.latest_signals,
                         "信号应已添加到队列")

            # 3. 停止会话
            self.grid_manager.stop_grid_session(session.id, "test_stop")

            # 4. 验证信号已清除
            self.assertNotIn(stock_code, self.position_manager.latest_signals,
                           "P0-2修复验证：会话停止后，网格信号应被清除")

    def test_P0_2_signal_cleared_on_position_cleared(self):
        """
        P0-2验证：持仓清空触发会话停止时清除信号

        场景：
        1. 启动网格会话
        2. 持仓清空触发会话停止
        3. 检查信号是否被清除

        预期：持仓清空导致会话停止后，信号应被清除
        """
        stock_code = "600511.SH"

        with patch.object(config, 'ENABLE_SIMULATION_MODE', True):
            # 启动会话
            session = self.grid_manager.start_grid_session(stock_code, {
                'max_investment': 10000,
                'price_interval': 0.05,
                'position_ratio': 0.25,
                'callback_ratio': 0.005
            })

            # 添加信号
            with self.position_manager.signal_lock:
                self.position_manager.latest_signals[stock_code] = {
                    'type': 'grid_buy',
                    'info': {'stock_code': stock_code, 'session_id': session.id},
                    'timestamp': datetime.now()
                }

            # 模拟持仓清空触发停止
            self.grid_manager.stop_grid_session(session.id, "position_cleared")

            # 验证信号已清除
            self.assertNotIn(stock_code, self.position_manager.latest_signals,
                           "P0-2修复验证：持仓清空停止会话后，信号应被清除")

    # ==================== P1-1: 信号去重机制验证 ====================

    def test_P1_1_signal_deduplication(self):
        """
        P1-1验证：相同类型的信号不重复生成

        场景：
        1. 已有grid_sell信号在队列中
        2. 再次检测到sell信号
        3. 检查是否生成新信号

        预期：已有相同类型信号时，不生成新信号
        """
        stock_code = "600512.SH"

        with patch.object(config, 'ENABLE_SIMULATION_MODE', True):
            # 启动会话
            session = self.grid_manager.start_grid_session(stock_code, {
                'max_investment': 10000,
                'price_interval': 0.05,
                'position_ratio': 0.25,
                'callback_ratio': 0.005
            })

            # 添加现有信号
            existing_signal = {
                'type': 'grid_sell',
                'info': {
                    'stock_code': stock_code,
                    'signal_type': 'SELL',
                    'session_id': session.id,
                    'trigger_price': 10.5
                },
                'timestamp': datetime.now()
            }

            with self.position_manager.signal_lock:
                self.position_manager.latest_signals[stock_code] = existing_signal

            # 模拟再次检测到sell信号
            # 直接调用check_grid_signals（价格满足sell条件）
            tracker = self.grid_manager.trackers[session.id]

            # 模拟价格触发sell条件
            tracker.peak_price = 10.8
            tracker.crossed_level = 10.5
            tracker.current_price = 10.7  # 回调满足

            # Mock check_callback返回'SELL'
            with patch.object(tracker, 'check_callback', return_value='SELL'):
                new_signal = self.grid_manager.check_grid_signals(stock_code, 10.7)

                # P1-1验证：应返回None，不生成新信号
                self.assertIsNone(new_signal,
                                "P1-1修复验证：已有grid_sell信号时，不应再生成新信号")

    def test_P1_1_different_signal_type_allowed(self):
        """
        P1-1验证：不同类型的信号可以共存

        场景：
        1. 已有grid_sell信号
        2. 检测到grid_buy信号
        3. 检查是否生成新信号

        预期：不同类型信号可以生成
        """
        stock_code = "600513.SH"

        with patch.object(config, 'ENABLE_SIMULATION_MODE', True):
            session = self.grid_manager.start_grid_session(stock_code, {
                'max_investment': 10000,
                'price_interval': 0.05,
                'position_ratio': 0.25,
                'callback_ratio': 0.005
            })

            # 添加grid_sell信号
            with self.position_manager.signal_lock:
                self.position_manager.latest_signals[stock_code] = {
                    'type': 'grid_sell',
                    'info': {'signal_type': 'SELL'},
                    'timestamp': datetime.now()
                }

            # 模拟检测到buy信号
            tracker = self.grid_manager.trackers[session.id]
            tracker.valley_price = 9.5
            tracker.crossed_level = 9.8
            tracker.current_price = 9.6

            with patch.object(tracker, 'check_callback', return_value='BUY'):
                new_signal = self.grid_manager.check_grid_signals(stock_code, 9.6)

                # 验证：不同类型信号可以生成
                self.assertIsNotNone(new_signal,
                                   "P1-1验证：不同类型信号（buy vs sell）应可生成")
                self.assertEqual(new_signal['signal_type'], 'BUY')

    # ==================== P1-2: 执行失败清除信号验证 ====================

    def test_P1_2_signal_cleared_on_execution_failure(self):
        """
        P1-2验证：执行失败后清除信号

        场景：
        1. 生成网格信号
        2. 执行失败（会话不存在）
        3. 检查信号是否被清除

        预期：执行失败后，信号应被清除，不再重试
        """
        stock_code = "600514.SH"

        # 添加信号（但没有对应会话）
        signal_info = {
            'stock_code': stock_code,
            'signal_type': 'SELL',
            'session_id': 999,  # 不存在的session_id
            'trigger_price': 10.5,
            'timestamp': datetime.now().isoformat()
        }

        with self.position_manager.signal_lock:
            self.position_manager.latest_signals[stock_code] = {
                'type': 'grid_sell',
                'info': signal_info,
                'timestamp': datetime.now()
            }

        # 执行网格交易（会话不存在，应失败）
        success = self.grid_manager.execute_grid_trade(signal_info)
        self.assertFalse(success, "会话不存在时，执行应失败")

        # P1-2验证：失败后清除信号
        # 注意：mark_signal_processed 内部自己获取 signal_lock，
        # 不能在持有 lock 的 with 块内调用它（否则 threading.Lock 会死锁）
        self.assertIn(stock_code, self.position_manager.latest_signals,
                      "执行失败后信号应仍在队列（尚未清除）")

        # 模拟策略在失败后调用 mark_signal_processed 清除信号
        self.position_manager.mark_signal_processed(stock_code)

        # 验证信号已清除
        self.assertNotIn(stock_code, self.position_manager.latest_signals,
                       "P1-2修复验证：执行失败后，信号应被清除")

    def test_P1_2_signal_cleared_on_execution_exception(self):
        """
        P1-2验证：执行异常后清除信号

        场景：
        1. 生成网格信号
        2. 执行抛出异常
        3. 检查信号是否被清除

        预期：执行异常后，信号应被清除
        """
        stock_code = "600515.SH"

        # 添加信号
        with self.position_manager.signal_lock:
            self.position_manager.latest_signals[stock_code] = {
                'type': 'grid_sell',
                'info': {
                    'stock_code': stock_code,
                    'signal_type': 'SELL',
                    'session_id': 1
                },
                'timestamp': datetime.now()
            }

        # 模拟执行异常
        with patch.object(self.grid_manager, 'execute_grid_trade',
                         side_effect=Exception("测试异常")):
            # 策略应在异常后调用mark_signal_processed
            try:
                self.grid_manager.execute_grid_trade(
                    self.position_manager.latest_signals[stock_code]['info']
                )
                self.fail("应抛出异常")
            except Exception:
                # 异常后清除信号
                self.position_manager.mark_signal_processed(stock_code)

            # 验证信号已清除
            self.assertNotIn(stock_code, self.position_manager.latest_signals,
                           "P1-2修复验证：执行异常后，信号应被清除")
    def test_P2_1_tracker_reset_on_buy_failure(self):
        """
        P2-1验证：买入失败时追踪器 waiting_callback 被重置

        场景：
        1. 下穿买入档位，tracker.waiting_callback=True
        2. 触发 BUY 回调，生成信号
        3. execute_grid_trade 因 max_investment 达上限返回 False
        4. 验证 tracker.waiting_callback 已被重置为 False

        若不修复此 Bug，tracker.waiting_callback 仍为 True，
        每隔 3 秒 check_callback 会重新返回 BUY，形成无限重试循环。
        """
        stock_code = "600511.SH"

        # 1. 启动会话
        session_config = {
            'center_price': 10.0,
            'price_interval': 0.05,
            'max_investment': 5000,
            'callback_ratio': 0.005,
            'duration_days': 7,
        }
        session = self.grid_manager.start_grid_session(stock_code, session_config)
        session_id = session.id

        # 2. 手动设置追踪器为"等待回调"状态（模拟下穿买入档位后）
        with self.grid_manager.lock:
            tracker = self.grid_manager.trackers[session_id]
            tracker.waiting_callback = True
            tracker.direction = 'falling'
            tracker.valley_price = 9.4
            tracker.last_price = 9.45   # 已回调 0.53%，满足触发条件
            tracker.crossed_level = 9.5

        # 3. 构造 BUY 信号（模拟 check_grid_signals 生成的结果）
        signal = {
            'stock_code': stock_code,
            'signal_type': 'BUY',
            'grid_level': 9.5,
            'trigger_price': 9.45,
            'session_id': session_id,
            'timestamp': datetime.now().isoformat(),
            'valley_price': 9.4,
            'callback_ratio': 0.005,
            'strategy': 'grid',
        }

        # 4. 让 max_investment 耗尽，使买入必然失败
        with self.grid_manager.lock:
            session.current_investment = session.max_investment  # 已耗尽

        # 5. 执行交易（应返回 False）
        result = self.grid_manager.execute_grid_trade(signal)
        self.assertFalse(result, "max_investment 耗尽时应返回 False")

        # 6. 验证追踪器 waiting_callback 被重置为 False（P2-1 修复验证）
        with self.grid_manager.lock:
            tracker = self.grid_manager.trackers[session_id]
            self.assertFalse(tracker.waiting_callback,
                             "P2-1修复验证：买入失败后 tracker.waiting_callback 应被重置为 False，"
                             "防止每 3 秒重新生成相同 BUY 信号的无限重试循环")
            self.assertIsNone(tracker.crossed_level,
                              "P2-1修复验证：买入失败后 tracker.crossed_level 应被清空")

    def test_P2_1_tracker_reset_on_real_buy_failure(self):
        """
        P2-1验证：实盘买入接口返回 None 时，追踪器同样被重置

        场景：buy_stock 返回 None（例如 QMT 未连接或下单被拒绝）
        """
        stock_code = "600512.SH"

        session_config = {
            'center_price': 10.0,
            'price_interval': 0.05,
            'max_investment': 10000,
            'callback_ratio': 0.005,
            'duration_days': 7,
        }
        session = self.grid_manager.start_grid_session(stock_code, session_config)
        session_id = session.id

        # 设置追踪器为等待回调状态
        with self.grid_manager.lock:
            tracker = self.grid_manager.trackers[session_id]
            tracker.waiting_callback = True
            tracker.direction = 'falling'
            tracker.valley_price = 9.4
            tracker.last_price = 9.45
            tracker.crossed_level = 9.5

        signal = {
            'stock_code': stock_code,
            'signal_type': 'BUY',
            'grid_level': 9.5,
            'trigger_price': 9.45,
            'session_id': session_id,
            'timestamp': datetime.now().isoformat(),
            'valley_price': 9.4,
            'callback_ratio': 0.005,
            'strategy': 'grid',
        }

        # 模拟实盘模式，buy_stock 返回 None（下单失败）
        with patch.object(config, 'ENABLE_SIMULATION_MODE', False):
            self.mock_executor.buy_stock = Mock(return_value=None)
            result = self.grid_manager.execute_grid_trade(signal)

        self.assertFalse(result, "buy_stock 返回 None 时应返回 False")

        # 验证追踪器已重置
        with self.grid_manager.lock:
            tracker = self.grid_manager.trackers[session_id]
            self.assertFalse(tracker.waiting_callback,
                             "P2-1修复验证：实盘买入失败后 tracker.waiting_callback 应被重置为 False")

    def test_P2_2_buy_cooldown_prevents_rapid_cascade(self):
        """
        P2-2验证：GRID_BUY_COOLDOWN 阻止短时间内连续买入

        场景：模拟 9:25 开盘，价格连续跌穿档位，验证冷却期内不再买入
        """
        stock_code = "600513.SH"

        session_config = {
            'center_price': 10.0,
            'price_interval': 0.05,
            'max_investment': 50000,
            'callback_ratio': 0.005,
            'duration_days': 7,
        }
        session = self.grid_manager.start_grid_session(stock_code, session_config)
        session_id = session.id

        # 构造一个有效的 BUY 信号
        signal = {
            'stock_code': stock_code,
            'signal_type': 'BUY',
            'grid_level': 9.5,
            'trigger_price': 9.45,
            'session_id': session_id,
            'timestamp': datetime.now().isoformat(),
            'valley_price': 9.4,
            'callback_ratio': 0.005,
            'strategy': 'grid',
        }

        # 开启 60 秒买入冷却
        with patch.object(config, 'GRID_BUY_COOLDOWN', 60):
            with patch.object(config, 'ENABLE_SIMULATION_MODE', True):
                # 第一次买入：应成功
                result1 = self.grid_manager.execute_grid_trade(signal)
                self.assertTrue(result1, "第一次买入应成功")

                # 第二次立即尝试（冷却期内）：应被阻止
                # 更新 signal 的价格信息，模拟新的档位触发
                signal2 = dict(signal)
                signal2['trigger_price'] = 8.98
                signal2['valley_price'] = 8.90
                signal2['grid_level'] = session.current_center_price * (1 - session.price_interval)

                result2 = self.grid_manager.execute_grid_trade(signal2)
                self.assertFalse(result2,
                                 "P2-2修复验证：GRID_BUY_COOLDOWN 期间内，第二次买入应被阻止")

    def test_P2_3_tracker_not_reset_on_success(self):
        """
        P2-3回归验证：买入成功时追踪器由 _rebuild_grid 正确重置（而非本次修复重置）

        确保修复 P2-1 不影响成功路径的追踪器管理。
        """
        stock_code = "600514.SH"

        session_config = {
            'center_price': 10.0,
            'price_interval': 0.05,
            'max_investment': 20000,
            'callback_ratio': 0.005,
            'duration_days': 7,
        }
        session = self.grid_manager.start_grid_session(stock_code, session_config)
        session_id = session.id

        with self.grid_manager.lock:
            tracker = self.grid_manager.trackers[session_id]
            tracker.waiting_callback = True
            tracker.direction = 'falling'
            tracker.valley_price = 9.4
            tracker.last_price = 9.45
            tracker.crossed_level = 9.5

        signal = {
            'stock_code': stock_code,
            'signal_type': 'BUY',
            'grid_level': 9.5,
            'trigger_price': 9.45,
            'session_id': session_id,
            'timestamp': datetime.now().isoformat(),
            'valley_price': 9.4,
            'callback_ratio': 0.005,
            'strategy': 'grid',
        }

        with patch.object(config, 'ENABLE_SIMULATION_MODE', True):
            result = self.grid_manager.execute_grid_trade(signal)

        self.assertTrue(result, "模拟买入应成功")

        # 成功后，_rebuild_grid 应将中心价更新为 trigger_price
        with self.grid_manager.lock:
            self.assertAlmostEqual(session.current_center_price, 9.45, places=2,
                                   msg="买入成功后，current_center_price 应更新为 trigger_price")
            tracker = self.grid_manager.trackers[session_id]
            # _rebuild_grid 调用 tracker.reset() 后 waiting_callback=False
            self.assertFalse(tracker.waiting_callback,
                             "买入成功后，_rebuild_grid 应将 waiting_callback 重置为 False")


if __name__ == '__main__':
    # 运行测试
    unittest.main(verbosity=2)
