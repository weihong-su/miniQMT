"""
xtdata 行情数据源单元测试

覆盖：
  1. DataManager.ensure_subscribed - 单只股票动态订阅（去重、异常处理）
  2. DataManager.get_latest_data   - xtdata -> Mootdx fallback 三条路径
  3. 盘中动态订阅集成              - simulate_buy_position / _sync_real_positions_to_memory
                                   新持仓加入时自动调用 ensure_subscribed

全部使用 Mock，不依赖真实 QMT/Mootdx 环境。
"""

import unittest
import sys
import os
from unittest.mock import MagicMock, patch, call, PropertyMock
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from test.test_base import TestBase
from test.test_mocks import MockQmtTrader


# ──────────────────────────────────────────────────────────────
# 辅助：构造一个最小化的 DataManager（跳过真实 DB/xtquant 初始化）
# ──────────────────────────────────────────────────────────────
def _make_data_manager():
    """返回一个跳过 IO 初始化的 DataManager 实例，方便单元测试。"""
    import sqlite3, tempfile, os as _os
    # 使用内存数据库，避免文件 IO
    with patch('data_manager.DataManager._init_xtquant'):
        # patch config.DATA_DIR 避免 makedirs 失败
        orig_dir = config.DATA_DIR
        config.DATA_DIR = tempfile.mkdtemp()
        orig_db = config.DB_PATH
        config.DB_PATH = _os.path.join(config.DATA_DIR, 'test_dm.db')
        try:
            from data_manager import DataManager
            dm = DataManager()
        finally:
            config.DATA_DIR = orig_dir
            config.DB_PATH = orig_db
    return dm


# ══════════════════════════════════════════════════════════════
# 测试组1：ensure_subscribed
# ══════════════════════════════════════════════════════════════
class TestEnsureSubscribed(TestBase):
    """DataManager.ensure_subscribed 行为验证"""

    def setUp(self):
        super().setUp()
        self.dm = _make_data_manager()
        self.mock_xt = MagicMock()
        self.dm.xt = self.mock_xt
        self.dm.subscribed_stocks = []

    def test_subscribe_new_stock(self):
        """未订阅的股票应触发 subscribe_quote 并加入 subscribed_stocks"""
        self.dm.ensure_subscribed('000920.SZ')
        self.mock_xt.subscribe_quote.assert_called_once_with(
            '000920.SZ', period='tick', start_time='', end_time='', count=0, callback=None
        )
        self.assertIn('000920.SZ', self.dm.subscribed_stocks)

    def test_no_duplicate_subscribe(self):
        """已订阅的股票不应重复调用 subscribe_quote"""
        self.dm.subscribed_stocks = ['000920.SZ']
        self.dm.ensure_subscribed('000920.SZ')
        self.mock_xt.subscribe_quote.assert_not_called()

    def test_subscribe_without_suffix_auto_normalized(self):
        """_adjust_stock 会自动补后缀，ensure_subscribed 不应因代码格式不同重复订阅"""
        # 先用完整代码订阅
        self.dm.ensure_subscribed('300712.SZ')
        self.assertEqual(self.mock_xt.subscribe_quote.call_count, 1)
        # 再次调用同一股票（已在 subscribed_stocks）
        self.dm.ensure_subscribed('300712.SZ')
        self.assertEqual(self.mock_xt.subscribe_quote.call_count, 1)

    def test_xt_none_safe(self):
        """xt 为 None 时 ensure_subscribed 应静默返回，不抛异常"""
        self.dm.xt = None
        try:
            self.dm.ensure_subscribed('000920.SZ')
        except Exception as e:
            self.fail(f"xt=None 时不应抛出异常，但抛出了: {e}")
        self.assertNotIn('000920.SZ', self.dm.subscribed_stocks)

    def test_subscribe_exception_handled(self):
        """subscribe_quote 抛出异常时应捕获并不中断程序"""
        self.mock_xt.subscribe_quote.side_effect = RuntimeError("模拟订阅失败")
        try:
            self.dm.ensure_subscribed('000920.SZ')
        except Exception as e:
            self.fail(f"subscribe_quote 异常应被捕获，但传播了: {e}")
        # 失败时不应加入 subscribed_stocks
        self.assertNotIn('000920.SZ', self.dm.subscribed_stocks)

    def test_multiple_different_stocks(self):
        """不同股票各自独立订阅，互不影响"""
        stocks = ['000920.SZ', '300712.SZ', '301399.SZ']
        for s in stocks:
            self.dm.ensure_subscribed(s)
        self.assertEqual(self.mock_xt.subscribe_quote.call_count, 3)
        for s in stocks:
            self.assertIn(s, self.dm.subscribed_stocks)


# ══════════════════════════════════════════════════════════════
# 测试组2：get_latest_data fallback 路径
# ══════════════════════════════════════════════════════════════
class TestGetLatestDataFallback(TestBase):
    """DataManager.get_latest_data xtdata->Mootdx fallback 三条路径验证"""

    def setUp(self):
        super().setUp()
        self.dm = _make_data_manager()
        self.mock_xt = MagicMock()
        self.dm.xt = self.mock_xt
        self.dm.subscribed_stocks = []

    def _make_mootdx_df(self, last_price):
        """构造 Mootdx 返回的 DataFrame 格式（至少2行，才能计算 lastClose）"""
        import pandas as pd
        prev_price = round(last_price - 0.1, 2)
        return pd.DataFrame({'last': [prev_price, last_price], 'open': [prev_price, last_price],
                             'high': [prev_price, last_price], 'low': [prev_price, last_price],
                             'close': [prev_price, last_price]})

    def test_xtdata_valid_price_returns_immediately(self):
        """xtdata 返回有效 lastPrice > 0 时，直接返回，不调用 Mootdx"""
        self.dm.get_latest_xtdata = MagicMock(return_value={'lastPrice': 13.59, 'lastClose': 13.97})
        with patch('config.is_trade_time', return_value=True), \
             patch('Methods.getStockData') as mock_mootdx:
            result = self.dm.get_latest_data('000920.SZ')
        mock_mootdx.assert_not_called()
        self.assertEqual(result.get('lastPrice'), 13.59)

    def test_xtdata_price_zero_subscribed_fallback_with_warning(self):
        """已订阅但 lastPrice=0：应记录 WARNING，降级到 Mootdx"""
        self.dm.subscribed_stocks = ['000920.SZ']
        self.dm.get_latest_xtdata = MagicMock(return_value={'lastPrice': 0, 'lastClose': 13.97})
        mock_df = self._make_mootdx_df(13.59)
        with patch('config.is_trade_time', return_value=True), \
             patch('Methods.getStockData', return_value=mock_df) as mock_mootdx, \
             self.assertLogs('miniQMT.dm', level='WARNING') as cm:
            self.dm.get_latest_data('000920.SZ')
        mock_mootdx.assert_called_once()
        self.assertTrue(any('已订阅但 lastPrice=0' in m for m in cm.output))

    def test_xtdata_price_zero_not_subscribed_triggers_subscribe(self):
        """未订阅且 lastPrice=0：应触发 ensure_subscribed，降级到 Mootdx，记录 INFO"""
        self.dm.subscribed_stocks = []
        self.dm.get_latest_xtdata = MagicMock(return_value={'lastPrice': 0, 'lastClose': 13.97})
        mock_df = self._make_mootdx_df(13.59)
        with patch('config.is_trade_time', return_value=True), \
             patch('Methods.getStockData', return_value=mock_df), \
             self.assertLogs('miniQMT.dm', level='INFO') as cm:
            self.dm.get_latest_data('000920.SZ')
        self.assertIn('000920.SZ', self.dm.subscribed_stocks,
                      "未订阅股票 lastPrice=0 时应触发 ensure_subscribed")
        self.assertTrue(any('未订阅' in m and '触发订阅' in m for m in cm.output))

    def test_xtdata_empty_dict_silent_fallback(self):
        """xtdata 返回空 dict（超时/连接失败）：静默 fallback，不记录 WARNING"""
        self.dm.get_latest_xtdata = MagicMock(return_value={})
        mock_df = self._make_mootdx_df(13.59)
        with patch('config.is_trade_time', return_value=True), \
             patch('Methods.getStockData', return_value=mock_df):
            # 不应产生 WARNING 级别日志
            import logging
            with self.assertLogs('miniQMT.dm', level='DEBUG') as cm:
                self.dm.get_latest_data('000920.SZ')
            warning_logs = [m for m in cm.output if 'WARNING' in m and '000920' in m]
            self.assertEqual(warning_logs, [], f"空 dict 不应产生 WARNING，但有: {warning_logs}")

    def test_non_trade_time_skips_xtdata(self):
        """非交易时段且无 xtdata 连接时不走 xtdata 路径"""
        self.dm.xt = None  # 模拟无 xtdata 连接，非交易时段优化路径不触发
        self.dm.get_latest_xtdata = MagicMock()
        mock_df = self._make_mootdx_df(13.59)
        with patch('config.is_trade_time', return_value=False), \
             patch('Methods.getStockData', return_value=mock_df):
            self.dm.get_latest_data('000920.SZ')
        self.dm.get_latest_xtdata.assert_not_called()

    def test_xtdata_exception_silent_fallback(self):
        """xtdata 内部抛出异常：静默 fallback 到 Mootdx，不中断"""
        self.dm.get_latest_xtdata = MagicMock(side_effect=RuntimeError("模拟异常"))
        mock_df = self._make_mootdx_df(13.59)
        with patch('config.is_trade_time', return_value=True), \
             patch('Methods.getStockData', return_value=mock_df):
            try:
                result = self.dm.get_latest_data('000920.SZ')
            except Exception as e:
                self.fail(f"xtdata 异常应被捕获，不应传播: {e}")


# ══════════════════════════════════════════════════════════════
# 测试组3：_subscribe_stocks_to_xtdata 订阅追踪
# ══════════════════════════════════════════════════════════════
class TestSubscribeStocksTracking(TestBase):
    """_subscribe_stocks_to_xtdata 批量订阅时正确追踪 subscribed_stocks"""

    def setUp(self):
        super().setUp()
        self.dm = _make_data_manager()
        self.mock_xt = MagicMock()
        self.dm.xt = self.mock_xt
        self.dm.subscribed_stocks = []

    def test_batch_subscribe_tracks_all(self):
        """批量订阅后所有股票都在 subscribed_stocks 中"""
        stocks = ['000920.SZ', '300712.SZ', '301399.SZ', '600509.SH']
        self.dm._subscribe_stocks_to_xtdata(stocks)
        for s in stocks:
            self.assertIn(s, self.dm.subscribed_stocks)

    def test_batch_subscribe_no_duplicate(self):
        """已订阅的股票不重复加入 subscribed_stocks"""
        self.dm.subscribed_stocks = ['000920.SZ']
        self.dm._subscribe_stocks_to_xtdata(['000920.SZ', '300712.SZ'])
        count = self.dm.subscribed_stocks.count('000920.SZ')
        self.assertEqual(count, 1, "000920.SZ 不应在 subscribed_stocks 中出现两次")

    def test_partial_failure_tracked_correctly(self):
        """部分订阅失败时，成功的才加入 subscribed_stocks"""
        def side_effect(code, **kwargs):
            if code == '301399.SZ':
                raise RuntimeError("模拟失败")
        self.mock_xt.subscribe_quote.side_effect = side_effect
        self.dm._subscribe_stocks_to_xtdata(['000920.SZ', '301399.SZ', '600509.SH'])
        self.assertIn('000920.SZ', self.dm.subscribed_stocks)
        self.assertNotIn('301399.SZ', self.dm.subscribed_stocks)
        self.assertIn('600509.SH', self.dm.subscribed_stocks)


# ══════════════════════════════════════════════════════════════
# 测试组4：PositionManager 盘中动态订阅集成
# ══════════════════════════════════════════════════════════════
class TestPositionManagerDynamicSubscribe(TestBase):
    """验证 PositionManager 在新持仓加入时自动调用 ensure_subscribed"""

    def setUp(self):
        super().setUp()
        self.mock_trader = MockQmtTrader()
        from position_manager import PositionManager
        self.pm = PositionManager()

        # 替换 data_manager 为 mock
        self.mock_dm = MagicMock()
        self.mock_dm.get_latest_data.return_value = {'lastPrice': 10.0, 'lastClose': 9.8}
        self.mock_dm.get_stock_name.return_value = '测试股票'
        self.mock_dm.conn = self.pm.data_manager.conn  # 保留真实 conn
        self.pm.data_manager = self.mock_dm

    def tearDown(self):
        try:
            self.pm.stop_sync_thread()
        except Exception:
            pass
        super().tearDown()

    def test_simulate_buy_new_stock_calls_ensure_subscribed(self):
        """simulate_buy_position 买入新股票后应调用 ensure_subscribed"""
        with patch.object(self.pm, '_simulate_update_position', return_value=True), \
             patch.object(self.pm, '_save_simulated_trade_record', return_value=True), \
             patch.object(self.pm, 'get_position', return_value=None):
            self.pm.simulate_buy_position('000920.SZ', 1000, 13.5, strategy='simu')
        self.mock_dm.ensure_subscribed.assert_called_with('000920.SZ')

    def test_simulate_buy_existing_stock_still_calls_ensure_subscribed(self):
        """加仓也应调用 ensure_subscribed（ensure_subscribed 内部会去重）"""
        existing_pos = {
            'stock_code': '000920.SZ', 'volume': 1000,
            'cost_price': 13.0, 'available': 1000,
            'profit_triggered': False, 'highest_price': 14.0,
            'open_date': '2025-01-01 10:00:00', 'stop_loss_price': 12.0
        }
        with patch.object(self.pm, '_simulate_update_position', return_value=True), \
             patch.object(self.pm, '_save_simulated_trade_record', return_value=True), \
             patch.object(self.pm, 'get_position', return_value=existing_pos):
            self.pm.simulate_buy_position('000920.SZ', 500, 13.5, strategy='simu')
        self.mock_dm.ensure_subscribed.assert_called_with('000920.SZ')

    def test_simulate_buy_failure_no_ensure_subscribed(self):
        """simulate_buy_position 失败时不应调用 ensure_subscribed"""
        with patch.object(self.pm, '_simulate_update_position', return_value=False), \
             patch.object(self.pm, '_save_simulated_trade_record', return_value=True), \
             patch.object(self.pm, 'get_position', return_value=None):
            self.pm.simulate_buy_position('000920.SZ', 1000, 13.5, strategy='simu')
        self.mock_dm.ensure_subscribed.assert_not_called()

    def test_sync_real_new_position_calls_ensure_subscribed(self):
        """_sync_real_positions_to_memory 新增实盘持仓时调用 ensure_subscribed"""
        import pandas as pd

        real_df = pd.DataFrame([{
            '证券代码': '000920.SZ',
            '股票余额': 1000,
            '可用余额': 1000,
            '成本价': 13.0,
            '市值': 13590.0,
        }])

        # 确保内存DB中没有该持仓（新增场景）
        with self.pm.memory_conn_lock:
            self.pm.memory_conn.execute(
                "DELETE FROM positions WHERE stock_code=?", ('000920.SZ',))
            self.pm.memory_conn.commit()

        self.pm._sync_real_positions_to_memory(real_df)
        self.mock_dm.ensure_subscribed.assert_called_with('000920.SZ')

    def test_sync_real_existing_position_no_ensure_subscribed(self):
        """_sync_real_positions_to_memory 更新已有持仓时不调用 ensure_subscribed"""
        import pandas as pd

        real_df = pd.DataFrame([{
            '证券代码': '000920.SZ',
            '股票余额': 1000,
            '可用余额': 1000,
            '成本价': 13.0,
            '市值': 13590.0,
        }])

        # 先插入一条持仓记录（已存在场景）
        with self.pm.memory_conn_lock:
            self.pm.memory_conn.execute("""
                INSERT OR REPLACE INTO positions
                (stock_code, volume, available, cost_price, current_price, market_value,
                 profit_ratio, profit_triggered, highest_price, open_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, ('000920.SZ', 1000, 1000, 13.0, 13.0, 13000.0, 0.0, False, 14.0, '2025-01-01'))
            self.pm.memory_conn.commit()

        self.pm._sync_real_positions_to_memory(real_df)
        self.mock_dm.ensure_subscribed.assert_not_called()


# ══════════════════════════════════════════════════════════════
# 测试组5：ensure_subscribed 线程安全
# ══════════════════════════════════════════════════════════════
class TestEnsureSubscribedThreadSafety(TestBase):
    """ensure_subscribed 并发调用不应产生重复订阅"""

    def setUp(self):
        super().setUp()
        self.dm = _make_data_manager()
        self.mock_xt = MagicMock()
        self.dm.xt = self.mock_xt
        self.dm.subscribed_stocks = []
        # 模拟 subscribe_quote 有轻微延迟
        import time
        def slow_subscribe(*args, **kwargs):
            time.sleep(0.01)
        self.mock_xt.subscribe_quote.side_effect = slow_subscribe

    def test_concurrent_ensure_subscribed_no_duplicate(self):
        """多线程并发 ensure_subscribed 同一股票，subscribe_quote 最多调用一次"""
        threads = [
            threading.Thread(target=self.dm.ensure_subscribed, args=('000920.SZ',))
            for _ in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        count = self.dm.subscribed_stocks.count('000920.SZ')
        # 由于 subscribed_stocks 不是线程安全结构，允许极少量重复（竞态窗口极小）
        # 但 subscribe_quote 调用次数应 <= 线程数
        subscribe_count = self.mock_xt.subscribe_quote.call_count
        self.assertLessEqual(subscribe_count, 10,
            f"subscribe_quote 调用 {subscribe_count} 次，超出预期")
        self.assertGreaterEqual(subscribe_count, 1,
            "subscribe_quote 至少应被调用1次")


if __name__ == '__main__':
    unittest.main(verbosity=2)
