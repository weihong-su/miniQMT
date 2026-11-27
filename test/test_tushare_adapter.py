"""
Tushare 行情数据适配器测试

覆盖:
- _to_tushare_code() 纯逻辑（代码转换）
- Tushare 惰性初始化逻辑
- Tushare 历史数据 mock 测试
- Tushare 股票名称 mock 测试
- 降级链集成测试
"""
import unittest
import time
from unittest.mock import MagicMock, patch, PropertyMock
import pandas as pd
import sys
import os

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test.test_base import TestBase

# 直接获取 @staticmethod 绑定的函数对象（避免 descriptor 传参问题）
from data_manager import DataManager
_to_tushare_code = DataManager._to_tushare_code


# ── Tushare 代码转换纯逻辑测试 ──

class TestTushareCodeConversion(unittest.TestCase):
    """测试 _to_tushare_code() 股票代码格式转换（纯逻辑，无 IO）"""

    def test_code_with_suffix_passthrough(self):
        """已有 .SH/.SZ 后缀 → 直接透传"""
        self.assertEqual(_to_tushare_code('000001.SZ'), '000001.SZ')
        self.assertEqual(_to_tushare_code('600036.SH'), '600036.SH')
        self.assertEqual(_to_tushare_code('920118.BJ'), '920118.BJ')
        self.assertEqual(_to_tushare_code('830799.BJ'), '830799.BJ')

    def test_sh_prefix_converted(self):
        """sh.600036 → 600036.SH"""
        self.assertEqual(_to_tushare_code('sh.600036'), '600036.SH')
        self.assertEqual(_to_tushare_code('SH.600036'), '600036.SH')

    def test_sz_prefix_converted(self):
        """sz.000001 → 000001.SZ"""
        self.assertEqual(_to_tushare_code('sz.000001'), '000001.SZ')
        self.assertEqual(_to_tushare_code('SZ.000001'), '000001.SZ')

    def test_bj_prefix_converted(self):
        """bj.920118 → 920118.BJ"""
        self.assertEqual(_to_tushare_code('bj.920118'), '920118.BJ')
        self.assertEqual(_to_tushare_code('BJ.920118'), '920118.BJ')

    def test_bare_code_sh(self):
        """裸代码 6xxx/5xxx/9xxx → xxxxxx.SH"""
        self.assertEqual(_to_tushare_code('600036'), '600036.SH')
        self.assertEqual(_to_tushare_code('688001'), '688001.SH')
        self.assertEqual(_to_tushare_code('515050'), '515050.SH')

    def test_bare_code_sz(self):
        """裸代码 0xxx/3xxx → xxxxxx.SZ"""
        self.assertEqual(_to_tushare_code('000001'), '000001.SZ')
        self.assertEqual(_to_tushare_code('000920'), '000920.SZ')
        self.assertEqual(_to_tushare_code('300750'), '300750.SZ')
        self.assertEqual(_to_tushare_code('159381'), '159381.SZ')

    def test_bare_code_bj(self):
        """一期仅裸 920 段自动识别为北交所。"""
        self.assertEqual(_to_tushare_code('920118'), '920118.BJ')
        self.assertEqual(_to_tushare_code('830799'), '830799')

    def test_lowercase_normalized(self):
        """小写代码转大写"""
        self.assertEqual(_to_tushare_code('000001.sz'), '000001.SZ')
        self.assertEqual(_to_tushare_code('sh.600036'), '600036.SH')


# ── Tushare 惰性初始化测试 ──

class TestTushareGetPro(unittest.TestCase):
    """测试 _get_tushare_pro() 惰性初始化逻辑"""

    def setUp(self):
        # 用 object.__new__ 绕过 __init__，手动设置状态
        self.dm = object.__new__(DataManager)
        self.dm._tushare_pro = None
        self.dm._tushare_token_attempted = False

    def test_token_empty_returns_none(self):
        """token 为空时返回 None"""
        self.dm._tushare_token_attempted = False
        with patch('config.TUSHARE_TOKEN', ''):
            pro = self.dm._get_tushare_pro()
            self.assertIsNone(pro)

    def test_token_attempted_returns_cached(self):
        """已尝试过且为 None 时不重复初始化"""
        self.dm._tushare_token_attempted = True
        self.dm._tushare_pro = None
        with patch('config.TUSHARE_TOKEN', 'dummy'):
            pro = self.dm._get_tushare_pro()
            self.assertIsNone(pro)

    def test_import_error_handled(self):
        """tushare 未安装时返回 None"""
        self.dm._tushare_token_attempted = False
        with patch('config.TUSHARE_TOKEN', 'dummy_token'):
            import builtins
            original_import = builtins.__import__

            def mock_import(name, *args, **kwargs):
                if name == 'tushare':
                    raise ImportError("No module named tushare")
                return original_import(name, *args, **kwargs)

            with patch('builtins.__import__', side_effect=mock_import):
                pro = self.dm._get_tushare_pro()
                self.assertIsNone(pro)


# ── Tushare 历史数据 Mock 测试 ──

class TestTushareHistoryData(TestBase):
    """测试 _download_history_tushare() 历史数据获取"""

    def setUp(self):
        super().setUp()
        from data_manager import DataManager
        self.dm = object.__new__(DataManager)
        self.dm._tushare_pro = None
        self.dm._tushare_token_attempted = False
        self.dm._ts_consecutive_failures = 0
        self.dm._ts_cooldown_until = 0.0
        # 注入 market_health
        from data_manager import MarketDataHealthTracker
        self.dm.market_health = MarketDataHealthTracker()

    def _make_mock_pro(self):
        """创建一个返回正常数据的 mock pro_api"""
        mock_pro = MagicMock()
        # 构造 Tushare daily() 返回格式
        mock_df = pd.DataFrame({
            'ts_code': ['000001.SZ', '000001.SZ'],
            'trade_date': ['20260701', '20260702'],
            'open': [10.0, 10.5],
            'high': [10.8, 11.0],
            'low': [9.8, 10.2],
            'close': [10.5, 10.8],
            'pre_close': [10.0, 10.5],
            'change': [0.5, 0.3],
            'pct_chg': [5.0, 2.86],
            'vol': [100000.0, 120000.0],
            'amount': [1050000.0, 1296000.0],
        })
        mock_pro.daily.return_value = mock_df
        return mock_pro

    def _mock_tushare_pro(self, mock_pro):
        """注入 mock pro 到 dm"""
        self.dm._tushare_pro = mock_pro
        self.dm._tushare_token_attempted = True

    def test_download_history_returns_normalized_df(self):
        """验证 Tushare 返回的 DataFrame 列名正确标准化"""
        mock_pro = self._make_mock_pro()
        self._mock_tushare_pro(mock_pro)

        df = self.dm._download_history_tushare('000001.SZ',
                                                 start_date='20260701',
                                                 end_date='20260702')
        self.assertIsNotNone(df)
        self.assertFalse(df.empty)
        # 验证列名
        self.assertIn('date', df.columns)
        self.assertIn('open', df.columns)
        self.assertIn('high', df.columns)
        self.assertIn('low', df.columns)
        self.assertIn('close', df.columns)
        self.assertIn('volume', df.columns)
        self.assertIn('amount', df.columns)
        # Tushare 原始列 'trade_date' / 'vol' 不应存在
        self.assertNotIn('trade_date', df.columns)
        self.assertNotIn('vol', df.columns)

    def test_download_history_etf_uses_fund_daily(self):
        """ETF/场内基金应走 Tushare fund_daily，避免 daily 返回空导致无操作建议。"""
        mock_pro = MagicMock()
        mock_pro.fund_daily.return_value = pd.DataFrame({
            'ts_code': ['515050.SH', '515050.SH'],
            'trade_date': ['20260701', '20260702'],
            'open': [1.0, 1.02],
            'high': [1.03, 1.05],
            'low': [0.99, 1.01],
            'close': [1.02, 1.04],
            'vol': [100000.0, 120000.0],
            'amount': [102000.0, 124800.0],
        })
        self._mock_tushare_pro(mock_pro)

        df = self.dm._download_history_tushare('515050',
                                                start_date='20260701',
                                                end_date='20260702')

        self.assertIsNotNone(df)
        self.assertFalse(df.empty)
        self.assertIn('date', df.columns)
        mock_pro.fund_daily.assert_called_once()
        mock_pro.daily.assert_not_called()

    def test_download_history_date_format(self):
        """验证日期格式为 YYYY-MM-DD"""
        mock_pro = self._make_mock_pro()
        self._mock_tushare_pro(mock_pro)

        df = self.dm._download_history_tushare('000001.SZ',
                                                 start_date='20260701',
                                                 end_date='20260702')
        # 日期应标准化为 YYYY-MM-DD
        self.assertTrue(all(len(str(d)) == 10 for d in df['date']))

    def test_download_history_dash_dates_sent_as_tushare_format(self):
        """YYYY-MM-DD 入参应转换为 Tushare daily 需要的 YYYYMMDD"""
        mock_pro = self._make_mock_pro()
        self._mock_tushare_pro(mock_pro)

        self.dm._download_history_tushare('000001.SZ',
                                          start_date='2026-07-01',
                                          end_date='2026-07-02')

        _, kwargs = mock_pro.daily.call_args
        self.assertEqual(kwargs['start_date'], '20260701')
        self.assertEqual(kwargs['end_date'], '20260702')

    def test_download_history_compact_dates_remain_unchanged(self):
        """YYYYMMDD 入参应保持为 Tushare daily 格式"""
        mock_pro = self._make_mock_pro()
        self._mock_tushare_pro(mock_pro)

        self.dm._download_history_tushare('000001.SZ',
                                          start_date='20260701',
                                          end_date='20260702')

        _, kwargs = mock_pro.daily.call_args
        self.assertEqual(kwargs['start_date'], '20260701')
        self.assertEqual(kwargs['end_date'], '20260702')

    def test_download_history_empty_data_returns_none(self):
        """节假日区间返回空 DataFrame 时应视为成功但无数据。"""
        mock_pro = MagicMock()
        mock_pro.daily.return_value = pd.DataFrame()
        self._mock_tushare_pro(mock_pro)
        self.dm._ts_consecutive_failures = 2

        with patch('data_manager.logger') as mock_logger:
            df = self.dm._download_history_tushare(
                '000001.SZ',
                start_date='20260925',
                end_date='20260927',
            )

        self.assertIsNone(df)
        self.assertEqual(self.dm._ts_consecutive_failures, 0)
        self.assertEqual(self.dm._ts_cooldown_until, 0.0)
        mock_logger.warning.assert_not_called()

        health = self.dm.market_health.get_score(
            source='Tushare', purpose='history', stock_code='000001.SZ'
        )
        self.assertEqual(health['success_count'], 1)
        self.assertEqual(health['failure_count'], 0)
        self.assertEqual(health['last_reason'], 'no_data')

    def test_download_history_none_data_returns_none(self):
        """daily() 异常返回 None 时降级并计为失败。"""
        mock_pro = MagicMock()
        mock_pro.daily.return_value = None
        self._mock_tushare_pro(mock_pro)

        df = self.dm._download_history_tushare('000001.SZ')
        self.assertIsNone(df)
        self.assertEqual(self.dm._ts_consecutive_failures, 1)

        health = self.dm.market_health.get_score(
            source='Tushare', purpose='history', stock_code='000001.SZ'
        )
        self.assertEqual(health['failure_count'], 1)
        self.assertEqual(health['last_reason'], 'none')

    def test_download_history_filtered_empty_resets_failures(self):
        """接口成功但筛选后无新增记录时应重置既有失败状态。"""
        mock_pro = self._make_mock_pro()
        self._mock_tushare_pro(mock_pro)
        self.dm._ts_consecutive_failures = 2

        df = self.dm._download_history_tushare(
            '000001.SZ',
            start_date='20260703',
            end_date='20260704',
        )

        self.assertIsNone(df)
        self.assertEqual(self.dm._ts_consecutive_failures, 0)
        self.assertEqual(self.dm._ts_cooldown_until, 0.0)

    def test_download_history_records_market_health(self):
        """验证健康评分记录"""
        mock_pro = self._make_mock_pro()
        self._mock_tushare_pro(mock_pro)

        self.dm._download_history_tushare('000001.SZ',
                                           start_date='20260701',
                                           end_date='20260702')

        snapshot = self.dm.market_health.snapshot()
        sources = snapshot.get('sources', {})
        self.assertIn('Tushare', sources)

    def test_download_history_failure_counts(self):
        """验证失败时 _ts_consecutive_failures 递增"""
        mock_pro = MagicMock()
        mock_pro.daily.side_effect = Exception("API error")
        self._mock_tushare_pro(mock_pro)

        self.assertEqual(self.dm._ts_consecutive_failures, 0)
        self.dm._download_history_tushare('000001.SZ')
        self.assertEqual(self.dm._ts_consecutive_failures, 1)

    def test_download_history_cooldown_honored(self):
        """冷却期内跳过调用"""
        self.dm._ts_cooldown_until = time.time() + 300  # 未来5分钟

        mock_pro = MagicMock()
        self._mock_tushare_pro(mock_pro)

        df = self.dm._download_history_tushare('000001.SZ')
        self.assertIsNone(df)
        # 冷却期内不应调用 pro.daily()
        mock_pro.daily.assert_not_called()

    def test_download_history_success_resets_failures(self):
        """成功后重置失败计数"""
        mock_pro = self._make_mock_pro()
        self._mock_tushare_pro(mock_pro)

        self.dm._ts_consecutive_failures = 5
        self.dm._download_history_tushare('000001.SZ',
                                           start_date='20260701',
                                           end_date='20260702')
        self.assertEqual(self.dm._ts_consecutive_failures, 0)
        self.assertEqual(self.dm._ts_cooldown_until, 0.0)

    def test_download_history_pro_is_none_returns_none(self):
        """pro 不可用时返回 None"""
        self.dm._tushare_pro = None
        self.dm._tushare_token_attempted = True
        df = self.dm._download_history_tushare('000001.SZ')
        self.assertIsNone(df)


# ── Tushare 股票名称 Mock 测试 ──

class TestTushareStockName(TestBase):
    """测试 _get_stock_name_from_tushare() 股票名称查询"""

    def setUp(self):
        super().setUp()
        from data_manager import DataManager
        self.dm = object.__new__(DataManager)
        self.dm._tushare_pro = None
        self.dm._tushare_token_attempted = False
        self.dm._ts_consecutive_failures = 0
        self.dm._ts_cooldown_until = 0.0
        self.dm.stock_names_cache = {}
        from data_manager import MarketDataHealthTracker
        self.dm.market_health = MarketDataHealthTracker()

    def _mock_tushare_stock_basic(self, name='平安银行'):
        """注入返回指定名称的 mock pro"""
        mock_pro = MagicMock()
        mock_df = pd.DataFrame({
            'ts_code': ['000001.SZ'],
            'name': [name],
        })
        mock_pro.stock_basic.return_value = mock_df
        self.dm._tushare_pro = mock_pro
        self.dm._tushare_token_attempted = True

    def test_get_stock_name_success(self):
        """正常返回名称"""
        self._mock_tushare_stock_basic('平安银行')
        name = self.dm._get_stock_name_from_tushare('000001.SZ')
        self.assertEqual(name, '平安银行')

    def test_get_etf_name_uses_fund_basic(self):
        """ETF/场内基金名称应走 fund_basic，而不是只查 stock_basic。"""
        mock_pro = MagicMock()
        mock_pro.fund_basic.return_value = pd.DataFrame({
            'ts_code': ['515050.SH'],
            'name': ['中概互联ETF'],
        })
        self.dm._tushare_pro = mock_pro
        self.dm._tushare_token_attempted = True

        name = self.dm._get_stock_name_from_tushare('515050')

        self.assertEqual(name, '中概互联ETF')
        self.assertEqual(self.dm.stock_names_cache['515050'], '中概互联ETF')
        self.assertEqual(self.dm.stock_names_cache['515050.SH'], '中概互联ETF')
        mock_pro.fund_basic.assert_called_once()
        mock_pro.stock_basic.assert_not_called()

    def test_get_stock_name_caches_result(self):
        """结果写入缓存"""
        self._mock_tushare_stock_basic('平安银行')
        name = self.dm._get_stock_name_from_tushare('000001.SZ')
        # 缓存中应有该代码
        self.assertIn('000001.SZ', self.dm.stock_names_cache)
        self.assertEqual(self.dm.stock_names_cache['000001.SZ'], '平安银行')

    def test_get_stock_name_invalid_not_cached(self):
        """无效名称（返回代码本身）不污染缓存"""
        self._mock_tushare_stock_basic('000001')  # 名称 = 代码（无效）
        name = self.dm._get_stock_name_from_tushare('000001.SZ')
        self.assertIsNone(name)
        # 不应缓存无效名称
        self.assertNotIn('000001.SZ', self.dm.stock_names_cache)

    def test_get_stock_name_empty_df(self):
        """空 DataFrame 返回 None"""
        mock_pro = MagicMock()
        mock_pro.stock_basic.return_value = pd.DataFrame()
        self.dm._tushare_pro = mock_pro
        self.dm._tushare_token_attempted = True

        name = self.dm._get_stock_name_from_tushare('999999.SZ')
        self.assertIsNone(name)

    def test_get_stock_name_cooldown_returns_none(self):
        """冷却期内返回 None"""
        self.dm._ts_cooldown_until = time.time() + 300
        self._mock_tushare_stock_basic('平安银行')

        name = self.dm._get_stock_name_from_tushare('000001.SZ')
        self.assertIsNone(name)

    def test_get_stock_name_failure_increments_counter(self):
        """失败后 _ts_consecutive_failures 递增"""
        mock_pro = MagicMock()
        mock_pro.stock_basic.side_effect = Exception("API error")
        self.dm._tushare_pro = mock_pro
        self.dm._tushare_token_attempted = True

        self.assertEqual(self.dm._ts_consecutive_failures, 0)
        self.dm._get_stock_name_from_tushare('000001.SZ')
        self.assertEqual(self.dm._ts_consecutive_failures, 1)

    def test_get_stock_name_success_resets_cooldown(self):
        """成功后重置冷却计数器（不在冷却期内调用）"""
        self.dm._ts_consecutive_failures = 10
        # 不设置 _ts_cooldown_until（确保不在冷却期内，方法才能走到成功路径）

        self._mock_tushare_stock_basic('平安银行')
        self.dm._get_stock_name_from_tushare('000001.SZ')

        self.assertEqual(self.dm._ts_consecutive_failures, 0)
        self.assertEqual(self.dm._ts_cooldown_until, 0.0)

    def test_get_stock_name_pro_none_returns_none(self):
        """pro 不可用时返回 None"""
        self.dm._tushare_pro = None
        self.dm._tushare_token_attempted = True
        name = self.dm._get_stock_name_from_tushare('000001.SZ')
        self.assertIsNone(name)


# ── 降级链集成测试 ──

class TestTushareFallbackIntegration(TestBase):
    """测试 Tushare 在完整下载链路中的位置"""

    def setUp(self):
        super().setUp()
        from data_manager import DataManager, MarketDataHealthTracker
        self.dm = object.__new__(DataManager)
        self.dm.market_health = MarketDataHealthTracker()
        self.dm.stock_names_cache = {}
        self.dm._bs_consecutive_failures = 0
        self.dm._bs_cooldown_until = 0.0
        self.dm._ts_consecutive_failures = 0
        self.dm._ts_cooldown_until = 0.0
        self.dm._tushare_pro = None
        self.dm._tushare_token_attempted = False
        self.dm.xt = None  # 标准模式

    def _make_healthy_tushare(self):
        """注入一个正常工作的 Tushare mock"""
        mock_pro = MagicMock()
        mock_df = pd.DataFrame({
            'ts_code': ['000001.SZ'],
            'trade_date': ['20260701'],
            'open': [10.0],
            'high': [10.8],
            'low': [9.8],
            'close': [10.5],
            'pre_close': [10.0],
            'change': [0.5],
            'pct_chg': [5.0],
            'vol': [100000.0],
            'amount': [1050000.0],
        })
        mock_pro.daily.return_value = mock_df
        self.dm._tushare_pro = mock_pro
        self.dm._tushare_token_attempted = True

    def test_full_download_history_tushare_to_mootdx(self):
        """历史数据: Tushare 优先 → Mootdx 兜底"""
        import config
        with patch.object(config, 'ENABLE_TUSHARE_DATA_SOURCE', True):
            with patch.object(config, 'TUSHARE_TOKEN', 'dummy'):
                # Tushare 返回正常数据 → 不应走 Mootdx
                self._make_healthy_tushare()

                # 只隔离下载调用，保留真实证券代码格式化，避免 Mock 伪装成指数。
                with patch('Methods.getStockData') as mock_mootdx:
                    df = self.dm.download_history_data(
                        '000001.SZ', period='day',
                        start_date='2026-07-01', end_date='2026-07-05'
                    )
                    # Tushare 成功，不应调用 Mootdx
                    mock_mootdx.assert_not_called()
                    self.assertIsNotNone(df)
                    self.assertFalse(df.empty)

    def test_download_history_tushare_disabled_goes_to_mootdx(self):
        """ENABLE_TUSHARE_DATA_SOURCE=False 时跳过 Tushare，走 Mootdx"""
        import config
        with patch.object(config, 'ENABLE_TUSHARE_DATA_SOURCE', False):
            # Tushare 已就绪但被禁用
            self._make_healthy_tushare()

            import Methods as real_methods
            mock_methods = MagicMock(wraps=real_methods)
            # download_history_data 内部用 `import Methods` 本地导入，
            # 必须替换 sys.modules 中的条目才能拦截
            with patch.dict('sys.modules', {'Methods': mock_methods}):
                mock_methods.getStockData.side_effect = Exception("simulated mootdx failure")
                df = self.dm.download_history_data(
                    '000001.SZ', period='day',
                    start_date='2026-07-01', end_date='2026-07-05'
                )
                # 应尝试 Mootdx
                mock_methods.getStockData.assert_called()

    def test_get_stock_name_tushare_between_xtdata_and_baostock(self):
        """股票名称: xtdata → Tushare → baostock 顺序"""
        import config
        # xtdata 不可用（self.xt = None）→ 应走 Tushare
        self.dm.xt = None

        with patch.object(config, 'ENABLE_TUSHARE_DATA_SOURCE', True):
            with patch.object(config, 'TUSHARE_TOKEN', 'dummy'):
                # Mock Tushare 成功
                mock_pro = MagicMock()
                mock_df = pd.DataFrame({'ts_code': ['000001.SZ'], 'name': ['平安银行']})
                mock_pro.stock_basic.return_value = mock_df
                self.dm._tushare_pro = mock_pro
                self.dm._tushare_token_attempted = True

                # Mock baostock 不应被调用
                with patch.object(self.dm, '_baostock_login_with_timeout') as mock_bs:
                    name = self.dm.get_stock_name('000001.SZ')
                    self.assertEqual(name, '平安银行')
                    # Tushare 成功 → baostock 不应被调用
                    mock_bs.assert_not_called()

    def test_get_stock_name_tushare_disabled_falls_to_baostock(self):
        """Tushare 禁用时跳过到 baostock（当 ENABLE_BAOSTOCK_STOCK_NAME_LOOKUP=True）"""
        import config
        self.dm.xt = None

        # Tushare 就绪但禁用
        mock_pro = MagicMock()
        self.dm._tushare_pro = mock_pro
        self.dm._tushare_token_attempted = True

        with patch.object(config, 'ENABLE_TUSHARE_DATA_SOURCE', False):
            with patch.object(config, 'ENABLE_BAOSTOCK_STOCK_NAME_LOOKUP', True):
                # baostock 也失败 → 最终返回代码
                with patch.object(self.dm, '_baostock_login_with_timeout') as mock_login:
                    mock_login.return_value = (None, "timeout")
                    name = self.dm.get_stock_name('000001.SZ')
                    self.assertEqual(name, '000001.SZ')


# ── 冷却机制集成测试 ──

class TestTushareCooldown(TestBase):
    """测试 _check_tushare_cooldown() 冷却逻辑"""

    def setUp(self):
        super().setUp()
        from data_manager import DataManager
        self.dm = object.__new__(DataManager)
        self.dm._ts_consecutive_failures = 0
        self.dm._ts_cooldown_until = 0.0

    def test_cooldown_triggered_at_threshold(self):
        """达到阈值时进入冷却"""
        import config
        with patch.object(config, 'TUSHARE_MAX_CONSECUTIVE_FAILURES', 3):
            self.dm._ts_consecutive_failures = 3
            self.dm._check_tushare_cooldown()
            self.assertGreater(self.dm._ts_cooldown_until, time.time())

    def test_cooldown_not_triggered_below_threshold(self):
        """未达阈值不触发"""
        import config
        with patch.object(config, 'TUSHARE_MAX_CONSECUTIVE_FAILURES', 3):
            self.dm._ts_consecutive_failures = 2
            self.dm._check_tushare_cooldown()
            self.assertEqual(self.dm._ts_cooldown_until, 0.0)


if __name__ == '__main__':
    unittest.main()
