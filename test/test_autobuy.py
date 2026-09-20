"""
miniqmt_autobuy 单元测试。

覆盖 config / pool / filter / store / client 五个纯逻辑模块，
mock data_manager 与 requests，使用临时 SQLite，不依赖真实 QMT/web/xtdata。
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd

os.environ.setdefault(
    "MINIQMT_AUTOBUY_LOG_PATH",
    os.path.join(os.path.dirname(__file__), "logs", "miniqmt_autobuy_test.log"),
)

from autobuy.config import AutoBuyConfig, load_config
from autobuy.pool import normalize_code, read_candidates, recent_trading_dates, to_xt_code
from autobuy.filter import (
    MARKET_INDEX_CODES, MarketIndexFilter, BuyConditionFilter,
    _recent_volume_ratios, is_st_name,
)
from autobuy.store import AutoBuyStore
from autobuy.client import WebClient


# ===========================================================================
# config
# ===========================================================================
class TestAutoBuyConfig(unittest.TestCase):
    def _write_cfg(self, text: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".cfg")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        self.addCleanup(lambda: os.remove(path))
        return path

    def test_load_basic(self):
        path = self._write_cfg(
            "[web]\nbase_url = http://127.0.0.1:5001\napi_token = abc\n"
            "[pool]\ntables = stg_chan, zs_pool\ncode_column = code\ndate_column = date\nlatest_n_dates = 3\n"
            "[schedule]\nmode = daily\ndaily_times = 09:35, 14:45\n"
        )
        cfg = load_config(path)
        self.assertEqual(cfg.base_url, "http://127.0.0.1:5001")
        self.assertEqual(cfg.api_token, "abc")
        self.assertEqual(cfg.tables, ["stg_chan", "zs_pool"])
        self.assertEqual(cfg.code_column, "code")
        self.assertEqual(cfg.date_column, "date")
        self.assertEqual(cfg.latest_n_dates, 3)
        self.assertEqual(cfg.mode, "daily")
        self.assertEqual(cfg.daily_times, [(9, 35), (14, 45)])

    def test_default_tables(self):
        path = self._write_cfg("[web]\nbase_url = http://127.0.0.1:5000\n")
        cfg = load_config(path)
        self.assertEqual(cfg.tables, ["stg_chan", "zs_pool"])

    def test_backward_compat_single_table(self):
        # 旧字段名 table / added_time_column / lookback_days 仍可解析
        path = self._write_cfg(
            "[pool]\ntable = my_pool\nadded_time_column = add_ts\nlookback_days = 5\n"
        )
        cfg = load_config(path)
        self.assertEqual(cfg.tables, ["my_pool"])
        self.assertEqual(cfg.date_column, "add_ts")
        self.assertEqual(cfg.latest_n_dates, 5)

    def test_illegal_table_name(self):
        path = self._write_cfg("[pool]\ntables = bad name;drop\n")
        with self.assertRaises(ValueError):
            load_config(path)

    def test_illegal_mode(self):
        path = self._write_cfg("[schedule]\nmode = sometimes\n")
        with self.assertRaises(ValueError):
            load_config(path)

    def test_base_url_env_override(self):
        """miniqmt.bat 探测到真实端口后通过环境变量注入，须覆盖 cfg 的静态值。"""
        path = self._write_cfg("[web]\nbase_url = http://127.0.0.1:5000\n")
        with patch.dict(os.environ, {"MINIQMT_AUTOBUY_BASE_URL": "http://127.0.0.1:50000"}):
            cfg = load_config(path)
        self.assertEqual(cfg.base_url, "http://127.0.0.1:50000")

    def test_base_url_env_override_strips_trailing_slash(self):
        path = self._write_cfg("[web]\nbase_url = http://127.0.0.1:5000\n")
        with patch.dict(os.environ, {"MINIQMT_AUTOBUY_BASE_URL": "http://127.0.0.1:50000/"}):
            cfg = load_config(path)
        self.assertEqual(cfg.base_url, "http://127.0.0.1:50000")

    def test_base_url_env_empty_falls_back_to_cfg(self):
        path = self._write_cfg("[web]\nbase_url = http://127.0.0.1:5001\n")
        with patch.dict(os.environ, {"MINIQMT_AUTOBUY_BASE_URL": "   "}):
            cfg = load_config(path)
        self.assertEqual(cfg.base_url, "http://127.0.0.1:5001")

    def test_api_token_falls_back_to_env(self):
        """cfg 未填 token 时回退 QMT_API_TOKEN，避免 401 导致整轮跳过买入。"""
        path = self._write_cfg("[web]\napi_token =\n")
        with patch.dict(os.environ, {"QMT_API_TOKEN": "env-token-xyz"}):
            cfg = load_config(path)
        self.assertEqual(cfg.api_token, "env-token-xyz")

    def test_cfg_api_token_takes_precedence(self):
        """cfg 显式填了 token 时不被环境变量覆盖。"""
        path = self._write_cfg("[web]\napi_token = cfg-token\n")
        with patch.dict(os.environ, {"QMT_API_TOKEN": "env-token-xyz"}):
            cfg = load_config(path)
        self.assertEqual(cfg.api_token, "cfg-token")

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            load_config("no_such_file_xyz.cfg")

    def test_ma20_and_simulation_defaults(self):
        path = self._write_cfg("[web]\nbase_url = http://127.0.0.1:5000\n")
        cfg = load_config(path)
        self.assertTrue(cfg.enable_ma20_range)
        self.assertAlmostEqual(cfg.min_price_to_ma20_deviation, -0.03)
        self.assertAlmostEqual(cfg.max_price_to_ma20_deviation, 0.05)
        self.assertFalse(cfg.simulation_mode)

    def test_ma20_and_simulation_override(self):
        path = self._write_cfg(
            "[filter]\nenable_ma20_range = false\n"
            "min_price_to_ma20_deviation = -0.05\nmax_price_to_ma20_deviation = 0.08\n"
            "[risk]\nsimulation_mode = true\n"
        )
        cfg = load_config(path)
        self.assertFalse(cfg.enable_ma20_range)
        self.assertAlmostEqual(cfg.min_price_to_ma20_deviation, -0.05)
        self.assertAlmostEqual(cfg.max_price_to_ma20_deviation, 0.08)
        self.assertTrue(cfg.simulation_mode)

    def test_inverted_ma20_range_rejected(self):
        path = self._write_cfg(
            "[filter]\nmin_price_to_ma20_deviation = 0.05\nmax_price_to_ma20_deviation = -0.03\n"
        )
        with self.assertRaises(ValueError):
            load_config(path)

    def test_shipped_cfg_is_loadable(self):
        """仓库内实际部署的 cfg 必须能被解析(防注释/拼写错误)。"""
        from autobuy.config import DEFAULT_CFG_PATH
        cfg = load_config(DEFAULT_CFG_PATH)
        self.assertTrue(cfg.enable_ma20_range)
        self.assertAlmostEqual(cfg.min_price_to_ma20_deviation, -0.03)
        self.assertAlmostEqual(cfg.max_price_to_ma20_deviation, 0.05)
        self.assertFalse(cfg.simulation_mode, "仓库默认配置不应处于模拟模式")
        # 近N日收盘量比取代盘中累计量比
        self.assertTrue(cfg.enable_recent_volume_ratio)
        self.assertFalse(cfg.enable_volume_ratio)
        self.assertEqual(cfg.recent_volume_ratio_days, 2)
        self.assertAlmostEqual(cfg.min_recent_volume_ratio, 1.2)
        self.assertEqual(cfg.volume_ratio_baseline_days, 5)
        self.assertTrue(cfg.skip_st, "仓库默认配置应启用 ST 过滤")

    def test_recent_volume_ratio_override(self):
        path = self._write_cfg(
            "[filter]\nenable_recent_volume_ratio = false\n"
            "recent_volume_ratio_days = 3\nmin_recent_volume_ratio = 1.5\n"
            "volume_ratio_baseline_days = 10\n"
        )
        cfg = load_config(path)
        self.assertFalse(cfg.enable_recent_volume_ratio)
        self.assertEqual(cfg.recent_volume_ratio_days, 3)
        self.assertAlmostEqual(cfg.min_recent_volume_ratio, 1.5)
        self.assertEqual(cfg.volume_ratio_baseline_days, 10)

    def test_invalid_recent_volume_ratio_days_rejected(self):
        for bad in ("recent_volume_ratio_days = 0", "volume_ratio_baseline_days = 0"):
            with self.subTest(bad=bad):
                path = self._write_cfg(f"[filter]\n{bad}\n")
                with self.assertRaises(ValueError):
                    load_config(path)


# ===========================================================================
# pool
# ===========================================================================
class TestPool(unittest.TestCase):
    def setUp(self):
        # 模拟真实 chan.db: 两表 stg_chan/zs_pool，列 code/date，code 格式 'sh.600025'
        # 当前业务口径: 以运行日 2026-06-14(周日) 倒推最近两个交易日，即 06-12 与 06-11。
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE stg_chan (id INTEGER PRIMARY KEY, code TEXT, date TEXT)")
        conn.execute("CREATE TABLE zs_pool  (id INTEGER PRIMARY KEY, code TEXT, date TEXT)")
        conn.executemany("INSERT INTO stg_chan (code, date) VALUES (?, ?)", [
            ("sh.600025", "2026-06-12"),
            ("sz.000001", "2026-06-11"),
            ("sz.300035", "2026-06-11"),  # 与 zs_pool 重复，跨表去重
            ("sh.600999", "2026-06-10"),  # 超出最近2个交易日，应排除
            ("sh.601111", "2026-06-15"),  # 运行日之后的数据不应被选入
        ])
        conn.executemany("INSERT INTO zs_pool (code, date) VALUES (?, ?)", [
            ("sh.603527", "2026-06-12"),
            ("sz.300568", "2026-06-12"),
            ("sz.300035", "2026-06-11"),  # 与 stg_chan 重复
            ("sz.000626", "2026-06-10"),  # 超出最近2个交易日，应排除
            ("sh.600888", "2026-06-13"),  # 周六非交易日，不应被选入
        ])
        conn.commit()
        conn.close()
        self.addCleanup(lambda: os.remove(self.db))

    def _cfg(self, **kw):
        cfg = AutoBuyConfig()
        cfg.db_path = self.db
        cfg.tables = ["stg_chan", "zs_pool"]
        cfg.code_column = "code"
        cfg.date_column = "date"
        cfg.latest_n_dates = 2
        for k, v in kw.items():
            setattr(cfg, k, v)
        return cfg

    def test_recent_trading_dates_skip_weekend(self):
        self.assertEqual(recent_trading_dates(2, "2026-06-14"), ["2026-06-12", "2026-06-11"])
        self.assertEqual(recent_trading_dates(1, "2026-06-15"), ["2026-06-12"])

    def test_recent_trading_dates_union_and_format(self):
        codes = read_candidates(self._cfg(), reference_date="2026-06-14")
        # 代码统一转成系统标准格式
        self.assertIn("600025.SH", codes)   # stg_chan 06-12
        self.assertIn("000001.SZ", codes)   # stg_chan 06-11
        self.assertIn("603527.SH", codes)   # zs_pool 06-12
        self.assertIn("300568.SZ", codes)   # zs_pool 06-12
        self.assertIn("300035.SZ", codes)   # 两表都有(最近交易日内)
        # 超出"运行日前最近2个交易日"的应排除
        self.assertNotIn("600999.SH", codes)  # stg_chan 06-10
        self.assertNotIn("000626.SZ", codes)  # zs_pool 06-10
        self.assertNotIn("601111.SH", codes)  # 运行日之后
        self.assertNotIn("600888.SH", codes)  # 周六非交易日
        # 跨表去重: 300035 只出现一次
        self.assertEqual(sum(1 for c in codes if normalize_code(c) == "300035"), 1)

    def test_latest_n_dates_one(self):
        # 只取运行日前最近 1 个交易日
        codes = read_candidates(self._cfg(latest_n_dates=1), reference_date="2026-06-14")
        self.assertIn("600025.SH", codes)   # stg_chan 06-12
        self.assertNotIn("000001.SZ", codes)  # 06-11 被排除
        self.assertIn("603527.SH", codes)   # zs_pool 06-12
        self.assertNotIn("300035.SZ", codes)  # 06-11 被排除

    def test_missing_table_degrades(self):
        codes = read_candidates(self._cfg(tables=["stg_chan", "no_such_table"]), reference_date="2026-06-14")
        self.assertIn("600025.SH", codes)
        self.assertNotIn("603527.SH", codes)  # 来自缺失表途径，未命中

    def test_missing_db(self):
        cfg = self._cfg(db_path="data/__no_such__.db")
        self.assertEqual(read_candidates(cfg), [])

    def test_normalize_code(self):
        self.assertEqual(normalize_code("sh.600025"), "600025")
        self.assertEqual(normalize_code("600000.SH"), "600000")
        self.assertEqual(normalize_code(" sz.000001 "), "000001")

    def test_to_xt_code(self):
        self.assertEqual(to_xt_code("sh.600025"), "600025.SH")
        self.assertEqual(to_xt_code("sz.000626"), "000626.SZ")
        self.assertEqual(to_xt_code("600025.SH"), "600025.SH")  # 已标准
        self.assertEqual(to_xt_code("000001"), "000001.SZ")     # 纯数字按前缀
        self.assertEqual(to_xt_code("600000"), "600000.SH")
        self.assertEqual(to_xt_code("688981"), "688981.SH")     # 科创板


# ===========================================================================
# filter
# ===========================================================================
def _make_dm(quote=None, df=None, detail=None):
    """构造一个满足 BuyConditionFilter 取数接口的 mock data_manager。"""
    dm = MagicMock()
    dm.get_latest_data.return_value = quote
    dm.download_history_data.return_value = df
    dm._adjust_stock.side_effect = lambda c: c if "." in c else c + ".SH"
    dm.xt.get_instrument_detail.return_value = detail or {}
    return dm


class TestFilter(unittest.TestCase):
    def setUp(self):
        self.cfg = AutoBuyConfig()  # 默认: 换手率/近N日量比/MA8方向/价格相对MA8/MA20区间 启用
        # 上升的收盘价序列 (24 根，满足 MA20 所需的 20 根)，ma8 向上，
        # 且末价相对 MA20 的偏离落在默认 [-3%,+5%] 区间内(现价 10.5 → 偏离约 +2.7%)
        self.close_up = [
            9.86, 9.90, 9.94, 9.98, 10.02, 10.06, 10.10, 10.14,
            10.18, 10.22, 10.26, 10.30, 10.34, 10.38, 10.42, 10.46,
            10.50, 10.54, 10.58, 10.62, 10.66, 10.70, 10.74, 10.78,
        ]
        # 末两个交易日放量，使默认的"近2日收盘量比 >= 1.2"成立
        self.volume_up = [100] * 22 + [150, 200]
        self.df_up = pd.DataFrame({"close": self.close_up, "volume": self.volume_up})

    def _quote(self, price=10.5, last_close=10.0, volume=1000):
        return {"lastPrice": price, "lastClose": last_close, "volume": volume}

    def _detail(self, float_vol=1_000_000, up=11.0):
        return {"FloatVolume": float_vol, "UpStopPrice": up}

    def test_all_pass(self):
        dm = _make_dm(self._quote(), self.df_up, self._detail())
        ok, reason = BuyConditionFilter(self.cfg, dm).check("600000")
        self.assertTrue(ok, reason)
        self.assertAlmostEqual(reason["turnover_rate"], 0.1, places=3)  # 1000*100/1e6
        # 默认启用的是近N日收盘量比，不是盘中累计量比
        self.assertNotIn("volume_ratio", reason)
        self.assertEqual(len(reason["recent_volume_ratios"]), 2)
        self.assertTrue(all(r >= 1.2 for r in reason["recent_volume_ratios"]))
        self.assertTrue(reason["ma8_uptrend"])

    def test_turnover_too_low(self):
        dm = _make_dm(self._quote(), self.df_up, self._detail(float_vol=10_000_000_000))
        ok, reason = BuyConditionFilter(self.cfg, dm).check("600000")
        self.assertFalse(ok)
        self.assertTrue(any("换手率" in r for r in reason["failed"]))

    def test_volume_ratio_too_low(self):
        # 显式验证旧的盘中累计量比: today=1000 / avg5=1000 = 1.0 < 2.0
        self.cfg.enable_volume_ratio = True
        self.cfg.enable_recent_volume_ratio = False
        n = len(self.close_up)
        df = pd.DataFrame({"close": self.close_up, "volume": [1000] * n})
        dm = _make_dm(self._quote(volume=1000), df, self._detail())
        ok, reason = BuyConditionFilter(self.cfg, dm).check("600000")
        self.assertFalse(ok)
        self.assertTrue(any("量比" in r for r in reason["failed"]))

    def test_ma8_downtrend(self):
        df = pd.DataFrame({"close": list(reversed(self.close_up)), "volume": self.volume_up})
        dm = _make_dm(self._quote(), df, self._detail())
        ok, reason = BuyConditionFilter(self.cfg, dm).check("600000")
        self.assertFalse(ok)
        self.assertTrue(any("MA8方向" in r for r in reason["failed"]))

    def test_price_far_above_ma8(self):
        dm = _make_dm(self._quote(price=20.0), self.df_up, self._detail(up=25.0))
        ok, reason = BuyConditionFilter(self.cfg, dm).check("600000")
        self.assertFalse(ok)
        self.assertTrue(any("MA8" in r for r in reason["failed"]))

    def test_limit_up_skip(self):
        dm = _make_dm(self._quote(price=11.0), self.df_up, self._detail(up=11.0))
        ok, reason = BuyConditionFilter(self.cfg, dm).check("600000")
        self.assertFalse(ok)
        self.assertIn("已涨停", reason["failed"])

    def test_no_quote(self):
        dm = _make_dm(None, self.df_up, self._detail())
        ok, reason = BuyConditionFilter(self.cfg, dm).check("600000")
        self.assertFalse(ok)


# ===========================================================================
# filter: 近 N 日收盘量比
# ===========================================================================
class TestRecentVolumeRatios(unittest.TestCase):
    """_recent_volume_ratios 算法本身: 每日独立回看、顺序无关、边界。"""

    def _df(self, vols, ascending=True):
        n = len(vols)
        dates = [f"2026-06-{i + 1:02d}" for i in range(n)]
        df = pd.DataFrame({"date": dates, "close": [10.0] * n, "volume": vols})
        return df if ascending else df.iloc[::-1].reset_index(drop=True)

    def test_each_day_looks_back_independently(self):
        # 基准5日均量=100。前天(idx=-2)量=150 → 1.5; 昨天(idx=-1)量比的基准
        # 是它【之前】5根 = [100,100,100,100,150] 均值110 → 220/110 = 2.0
        df = self._df([100, 100, 100, 100, 100, 150, 220])
        r = _recent_volume_ratios(df, days=2, baseline=5)
        self.assertEqual(len(r), 2)
        self.assertAlmostEqual(r[0], 1.5, places=6)   # 由远及近: 前天
        self.assertAlmostEqual(r[1], 2.0, places=6)   # 昨天

    def test_descending_input_gives_same_result(self):
        """data_manager 返回最新在前(降序)，结果必须与升序一致。"""
        vols = [100, 100, 100, 100, 100, 150, 220]
        asc = _recent_volume_ratios(self._df(vols, ascending=True), 2, 5)
        desc = _recent_volume_ratios(self._df(vols, ascending=False), 2, 5)
        self.assertEqual([round(x, 6) for x in asc], [round(x, 6) for x in desc])

    def test_returns_oldest_first(self):
        df = self._df([100] * 5 + [300, 100])   # 前天放量、昨天缩量
        r = _recent_volume_ratios(df, days=2, baseline=5)
        self.assertGreater(r[0], r[1], "应按由远及近返回: r[0]=前天, r[1]=昨天")

    def test_single_day(self):
        df = self._df([100] * 5 + [250])
        r = _recent_volume_ratios(df, days=1, baseline=5)
        self.assertEqual(len(r), 1)
        self.assertAlmostEqual(r[0], 2.5, places=6)

    def test_exact_minimum_length(self):
        # days+baseline = 7 根，恰好够
        df = self._df([100] * 7)
        self.assertIsNotNone(_recent_volume_ratios(df, days=2, baseline=5))
        # 少一根即不足
        self.assertIsNone(_recent_volume_ratios(self._df([100] * 6), days=2, baseline=5))

    def test_zero_baseline_returns_none(self):
        df = self._df([0, 0, 0, 0, 0, 100, 100])
        self.assertIsNone(_recent_volume_ratios(df, days=2, baseline=5))

    def test_missing_volume_column(self):
        df = pd.DataFrame({"date": ["2026-06-01"], "close": [10.0]})
        self.assertIsNone(_recent_volume_ratios(df, days=2, baseline=5))

    def test_none_or_empty(self):
        self.assertIsNone(_recent_volume_ratios(None, 2, 5))
        self.assertIsNone(_recent_volume_ratios(pd.DataFrame(), 2, 5))

    def test_works_without_date_column(self):
        """无 date 列时按传入顺序处理(回测路径已自行排序)。"""
        df = pd.DataFrame({"close": [10.0] * 7, "volume": [100] * 5 + [150, 220]})
        r = _recent_volume_ratios(df, days=2, baseline=5)
        self.assertAlmostEqual(r[0], 1.5, places=6)


class TestRecentVolumeRatioCondition(unittest.TestCase):
    """条件集成: 近 N 日量比须【全部】达标。"""

    def setUp(self):
        self.cfg = AutoBuyConfig()
        # 隔离出本条件
        self.cfg.enable_turnover_rate = False
        self.cfg.enable_volume_ratio = False
        self.cfg.enable_pct_change = False
        self.cfg.enable_ma8_uptrend = False
        self.cfg.enable_price_below_ma8_ratio = False
        self.cfg.enable_ma20_range = False
        self.cfg.skip_limit_up = False
        self.cfg.enable_recent_volume_ratio = True

    def _check(self, vols, **kw):
        for k, v in kw.items():
            setattr(self.cfg, k, v)
        n = len(vols)
        df = pd.DataFrame({
            "date": [f"2026-06-{i + 1:02d}" for i in range(n)],
            "close": [10.0] * n, "volume": vols,
        })
        dm = _make_dm({"lastPrice": 10.0, "lastClose": 10.0, "volume": 999}, df, {})
        return BuyConditionFilter(self.cfg, dm).check("600000")

    def test_defaults(self):
        self.assertTrue(self.cfg.enable_recent_volume_ratio)
        self.assertEqual(self.cfg.recent_volume_ratio_days, 2)
        self.assertAlmostEqual(self.cfg.min_recent_volume_ratio, 1.2)
        self.assertEqual(self.cfg.volume_ratio_baseline_days, 5)
        self.assertFalse(self.cfg.enable_volume_ratio, "盘中累计量比默认应关闭")

    def test_both_days_above_threshold_passes(self):
        # 前天 1.5、昨天 220/110=2.0，均 >= 1.2
        ok, reason = self._check([100, 100, 100, 100, 100, 150, 220])
        self.assertTrue(ok, reason)
        self.assertEqual(len(reason["recent_volume_ratios"]), 2)

    def test_one_day_below_threshold_fails(self):
        # 前天 1.5 达标，昨天 55/110=0.5 不达标 → 整体不通过
        ok, reason = self._check([100, 100, 100, 100, 100, 150, 55])
        self.assertFalse(ok)
        self.assertTrue(any("量比" in r for r in reason["failed"]), reason["failed"])

    def test_first_day_below_threshold_fails(self):
        # 前天 0.5 不达标(即使昨天放量) → 整体不通过
        ok, reason = self._check([100, 100, 100, 100, 100, 50, 300])
        self.assertFalse(ok)
        self.assertTrue(any("量比" in r for r in reason["failed"]))

    def test_boundary_exactly_at_threshold_passes(self):
        # 前天恰好 1.2；昨天基准=(100*4+120)/5=104, 需 >=124.8 → 用 130
        ok, reason = self._check([100, 100, 100, 100, 100, 120, 130])
        self.assertTrue(ok, reason)
        self.assertAlmostEqual(reason["recent_volume_ratios"][0], 1.2, places=3)

    def test_custom_days_and_threshold(self):
        # 3 日全部 >= 1.0
        ok, reason = self._check(
            [100] * 5 + [110, 120, 130],
            recent_volume_ratio_days=3, min_recent_volume_ratio=1.0,
        )
        self.assertTrue(ok, reason)
        self.assertEqual(len(reason["recent_volume_ratios"]), 3)

    def test_insufficient_history_fails(self):
        # 6 根 < days(2)+baseline(5) → 不通过，不静默放行
        ok, reason = self._check([100] * 6)
        self.assertFalse(ok)
        self.assertIsNone(reason["recent_volume_ratios"])
        self.assertTrue(any("无法计算" in r for r in reason["failed"]))

    def test_disabled_skips_check(self):
        ok, reason = self._check([100] * 6, enable_recent_volume_ratio=False)
        self.assertTrue(ok, reason)
        self.assertNotIn("recent_volume_ratios", reason)

    def test_reason_records_actual_ratios(self):
        _, reason = self._check([100, 100, 100, 100, 100, 150, 220])
        self.assertAlmostEqual(reason["recent_volume_ratios"][0], 1.5, places=2)
        self.assertAlmostEqual(reason["recent_volume_ratios"][1], 2.0, places=2)

    def test_independent_from_intraday_volume(self):
        """本条件只看历史收盘量，与盘中累计量(quote.volume)无关。"""
        vols = [100, 100, 100, 100, 100, 150, 220]
        n = len(vols)
        df = pd.DataFrame({
            "date": [f"2026-06-{i + 1:02d}" for i in range(n)],
            "close": [10.0] * n, "volume": vols,
        })
        results = []
        for intraday in (1, 999999):
            dm = _make_dm({"lastPrice": 10.0, "lastClose": 10.0, "volume": intraday}, df, {})
            ok, reason = BuyConditionFilter(self.cfg, dm).check("600000")
            results.append((ok, reason["recent_volume_ratios"]))
        self.assertEqual(results[0], results[1], "盘中成交量不应影响近N日收盘量比")



# ===========================================================================
# filter: ST 过滤
# ===========================================================================
class TestIsStName(unittest.TestCase):
    """ST 判定按证券名称前缀。

    不能用 InstrumentStatus: 实测 *ST美丽/*ST皇庭/ST海王/ST晨鸣 该字段均为 0，
    与正常股无区别。
    """

    def test_st_variants(self):
        for name in ("ST海王", "*ST元道", "*ST康佳A", "SST华新", "S*ST前锋", "ST晨鸣"):
            with self.subTest(name=name):
                self.assertTrue(is_st_name(name), name)

    def test_delisting(self):
        for name in ("退市博元", "京天利退", "长油5退"):
            with self.subTest(name=name):
                self.assertTrue(is_st_name(name), name)

    def test_normal_names(self):
        for name in ("平安银行", "华能水电", "金力永磁", "福昕软件", "中国宝安"):
            with self.subTest(name=name):
                self.assertFalse(is_st_name(name), name)

    def test_name_with_spaces_and_fullwidth(self):
        """xtdata 返回的名称可能含空格/全角字符，如 '万 科Ａ'。

        关键用例是空格出现在【前缀内部】(如 'S T海王'、'* ST元道')——
        此时不做空白归一化，startswith 会漏判。
        """
        self.assertFalse(is_st_name("万 科Ａ"))
        self.assertFalse(is_st_name("深振业Ａ"))
        self.assertTrue(is_st_name("*ST 元道"))
        self.assertTrue(is_st_name("ST　海王"))     # 前缀后全角空格
        self.assertTrue(is_st_name("S T海王"))      # 前缀内部半角空格
        self.assertTrue(is_st_name("* ST元道"))     # 前缀内部半角空格
        self.assertTrue(is_st_name("S　T晨鸣"))     # 前缀内部全角空格

    def test_empty_and_none(self):
        for val in (None, "", "   "):
            with self.subTest(val=repr(val)):
                self.assertFalse(is_st_name(val))

    def test_does_not_match_st_in_middle(self):
        """名称中间含 ST 的正常股不应被误判(仅前缀匹配)。"""
        self.assertFalse(is_st_name("海STAR科技"))


class TestStFilterInCheck(unittest.TestCase):
    """ST 过滤接入 check(): 一票否决，且应在取历史K线前短路。"""

    def setUp(self):
        self.cfg = AutoBuyConfig()
        self.df = pd.DataFrame({
            "date": [f"2026-06-{i + 1:02d}" for i in range(24)],
            "close": [10.0] * 24, "volume": [100] * 24,
        })

    def _check(self, inst_name, **kw):
        for k, v in kw.items():
            setattr(self.cfg, k, v)
        dm = _make_dm(
            {"lastPrice": 10.0, "lastClose": 10.0, "volume": 1000},
            self.df, {"InstrumentName": inst_name, "FloatVolume": 1e6},
        )
        ok, reason = BuyConditionFilter(self.cfg, dm).check("301139.SZ")
        return ok, reason, dm

    def test_default_enabled(self):
        self.assertTrue(self.cfg.skip_st)

    def test_st_stock_rejected(self):
        ok, reason, _ = self._check("*ST元道")
        self.assertFalse(ok)
        self.assertTrue(any("ST股" in r for r in reason["failed"]), reason["failed"])
        self.assertEqual(reason["instrument_name"], "*ST元道")

    def test_st_check_short_circuits_before_history(self):
        """ST 判定不依赖行情，应在取K线前返回，避免无谓开销。"""
        _, _, dm = self._check("*ST元道")
        dm.download_history_data.assert_not_called()

    def test_normal_stock_not_rejected_by_st(self):
        ok, reason, _ = self._check("华能水电")
        self.assertFalse(any("ST股" in r for r in reason["failed"]), reason["failed"])

    def test_disabled_allows_st(self):
        _, reason, _ = self._check("*ST元道", skip_st=False)
        self.assertFalse(any("ST股" in r for r in reason["failed"]))
        self.assertNotIn("instrument_name", reason)

    def test_missing_name_does_not_reject(self):
        """detail 取不到名称时不应误杀(保持原有行为)。"""
        dm = _make_dm(
            {"lastPrice": 10.0, "lastClose": 10.0, "volume": 1000},
            self.df, {"FloatVolume": 1e6},
        )
        _, reason = BuyConditionFilter(self.cfg, dm).check("600000.SH")
        self.assertFalse(any("ST股" in r for r in reason["failed"]))


# ===========================================================================
# filter: MA20 区间条件
# ===========================================================================
class TestMA20Range(unittest.TestCase):
    """现价相对 MA20 的偏离度须落在 [min, max] 区间内。

    构造 20 根恒定收盘价 10.0 的 K 线 → MA20 恰为 10.0，
    于是"现价"即可直接表达偏离度，边界判定清晰可读。
    """

    def setUp(self):
        self.cfg = AutoBuyConfig()
        # 只留 MA20 一项条件，隔离其它条件的干扰
        self.cfg.enable_turnover_rate = False
        self.cfg.enable_volume_ratio = False
        self.cfg.enable_recent_volume_ratio = False
        self.cfg.enable_pct_change = False
        self.cfg.enable_ma8_uptrend = False
        self.cfg.enable_price_below_ma8_ratio = False
        self.cfg.skip_limit_up = False
        self.cfg.enable_ma20_range = True
        # MA20 == 10.0 (20 根恒定价)
        self.df = pd.DataFrame({"close": [10.0] * 20, "volume": [100] * 20})

    def _check(self, price, **cfg_kw):
        for k, v in cfg_kw.items():
            setattr(self.cfg, k, v)
        dm = _make_dm({"lastPrice": price, "lastClose": 10.0, "volume": 1000}, self.df, {})
        return BuyConditionFilter(self.cfg, dm).check("600000")

    def test_default_range_is_minus3_to_plus5(self):
        self.assertAlmostEqual(self.cfg.min_price_to_ma20_deviation, -0.03)
        self.assertAlmostEqual(self.cfg.max_price_to_ma20_deviation, 0.05)

    def test_within_range_passes(self):
        for price in (9.80, 10.0, 10.30, 10.49):
            with self.subTest(price=price):
                ok, reason = self._check(price)
                self.assertTrue(ok, reason)
                self.assertAlmostEqual(reason["ma20"], 10.0)

    def test_lower_boundary_inclusive(self):
        # -3% 恰好在界上 → 通过(闭区间)
        ok, reason = self._check(9.70)
        self.assertTrue(ok, reason)
        self.assertAlmostEqual(reason["price_to_ma20_deviation"], -0.03, places=4)

    def test_upper_boundary_inclusive(self):
        # +5% 恰好在界上 → 通过(闭区间)
        ok, reason = self._check(10.50)
        self.assertTrue(ok, reason)
        self.assertAlmostEqual(reason["price_to_ma20_deviation"], 0.05, places=4)

    def test_below_lower_boundary_fails(self):
        # -3.1% → 跌离均线太远，拦截
        ok, reason = self._check(9.69)
        self.assertFalse(ok)
        self.assertTrue(any("MA20" in r for r in reason["failed"]), reason["failed"])

    def test_above_upper_boundary_fails(self):
        # +5.1% → 追高偏离太多，拦截
        ok, reason = self._check(10.51)
        self.assertFalse(ok)
        self.assertTrue(any("MA20" in r for r in reason["failed"]), reason["failed"])

    def test_disabled_skips_check(self):
        # 关闭后即使严重偏离也不因 MA20 被拦
        ok, reason = self._check(20.0, enable_ma20_range=False)
        self.assertTrue(ok, reason)
        self.assertNotIn("ma20", reason)

    def test_insufficient_history_fails(self):
        # 不足 20 根 → MA20 无法计算，判定不通过(不静默放行)
        dm = _make_dm(
            {"lastPrice": 10.0, "lastClose": 10.0, "volume": 1000},
            pd.DataFrame({"close": [10.0] * 19, "volume": [100] * 19}),
            {},
        )
        ok, reason = BuyConditionFilter(self.cfg, dm).check("600000")
        self.assertFalse(ok)
        self.assertIsNone(reason["ma20"])
        self.assertTrue(any("MA20无法计算" in r for r in reason["failed"]))

    def test_custom_range_is_respected(self):
        ok, _ = self._check(
            10.80, min_price_to_ma20_deviation=-0.10, max_price_to_ma20_deviation=0.10
        )
        self.assertTrue(ok)

    def test_reason_records_actual_deviation(self):
        # 决策日志须能复盘实际偏离度
        _, reason = self._check(10.20)
        self.assertAlmostEqual(reason["price_to_ma20_deviation"], 0.02, places=4)

    def test_inverted_range_rejected_by_validate(self):
        cfg = AutoBuyConfig()
        cfg.min_price_to_ma20_deviation = 0.05
        cfg.max_price_to_ma20_deviation = -0.03
        with self.assertRaises(ValueError):
            cfg.validate()

    def test_ma20_combines_with_other_conditions(self):
        """MA20 与既有条件是 AND 关系: MA20 合格但量比不合格仍应拦截。"""
        cfg = AutoBuyConfig()
        cfg.enable_turnover_rate = False
        cfg.enable_ma8_uptrend = False
        cfg.enable_price_below_ma8_ratio = False
        cfg.skip_limit_up = False
        cfg.enable_recent_volume_ratio = False   # 隔离: 只验证旧的盘中累计量比
        cfg.enable_volume_ratio = True
        cfg.min_volume_ratio = 2.0
        # today_volume == avg5 → 量比 1.0 < 2.0
        df = pd.DataFrame({"close": [10.0] * 20, "volume": [1000] * 20})
        dm = _make_dm({"lastPrice": 10.0, "lastClose": 10.0, "volume": 1000}, df, {})
        ok, reason = BuyConditionFilter(cfg, dm).check("600000")
        self.assertFalse(ok)
        self.assertTrue(any("量比" in r for r in reason["failed"]))
        # MA20 项本身是合格的(偏离 0%)，证明两者独立评估
        self.assertAlmostEqual(reason["price_to_ma20_deviation"], 0.0, places=4)


# ===========================================================================
# market filter
# ===========================================================================
class TestMarketIndexFilter(unittest.TestCase):
    def _df(self, closes):
        return pd.DataFrame({"close": closes})

    def test_any_index_ma5_up_passes(self):
        dm = MagicMock()
        down = self._df([10, 9, 8, 7, 6, 5])
        up = self._df([1, 2, 3, 4, 5, 6])
        dm.download_history_data.side_effect = [down, up]

        ok, reason = MarketIndexFilter(dm).check()

        self.assertTrue(ok, reason)
        self.assertEqual(reason["passed_index"], "399001")
        self.assertEqual(dm.download_history_data.call_args_list[0].args[0], "000001.SH")
        self.assertEqual(dm.download_history_data.call_args_list[1].args[0], "399001.SZ")

    def test_index_alias_fallback(self):
        dm = MagicMock()
        up = self._df([1, 2, 3, 4, 5, 6])
        dm.download_history_data.side_effect = [None, up]

        ok, reason = MarketIndexFilter(dm, index_codes=("999999",)).check()

        self.assertTrue(ok, reason)
        self.assertEqual(reason["details"]["999999"]["code"], "999999.SH")
        self.assertEqual(dm.download_history_data.call_args_list[0].args[0], "000001.SH")
        self.assertEqual(dm.download_history_data.call_args_list[1].args[0], "999999.SH")

    def test_all_index_ma5_down_blocks(self):
        dm = MagicMock()
        dm.download_history_data.return_value = self._df([10, 9, 8, 7, 6, 5])

        ok, reason = MarketIndexFilter(dm).check()

        self.assertFalse(ok)
        self.assertEqual(len(reason["details"]), len(MARKET_INDEX_CODES))
        self.assertTrue(all(not item.get("ma5_up", True) for item in reason["details"].values()))


# ===========================================================================
# store
# ===========================================================================
class TestStore(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = AutoBuyStore(self.db)
        self.addCleanup(lambda: (self.store.close(), os.remove(self.db)))

    def test_dedup_window(self):
        self.store.record_buy("600000", "test", success=True)
        self.store.record_buy("000001", "test", success=False)  # 失败不计入
        # 当天窗口
        self.assertEqual(self.store.recently_bought_codes(0), {"600000"})
        # 永久窗口
        self.assertEqual(self.store.recently_bought_codes(-1), {"600000"})

    def test_dedup_returns_normalized_codes(self):
        """回归: 库里存 '600000.SH'，防重查询须归一成 '600000'。

        原 bug: record_buy 写入的是 read_candidates→to_xt_code 产出的
        '600000.SH'，而 app._dedup_filter 用 normalize_code(code)='600000'
        去比较，集合永不命中 → dedup_window_days 完全失效。
        """
        self.store.record_buy("600000.SH", "test", success=True)
        self.store.record_buy("000001.SZ", "test", success=True)
        self.assertEqual(self.store.recently_bought_codes(0), {"600000", "000001"})
        self.assertEqual(self.store.recently_bought_codes(-1), {"600000", "000001"})

    def test_simulated_buy_excluded_from_dedup(self):
        """模拟买入只记录、不参与防重，避免试跑挡住当天真实买入。"""
        self.store.record_buy("600000.SH", "test", success=True, is_simulation=True)
        self.store.record_buy("600001.SH", "test", success=True, is_simulation=False)
        self.assertEqual(self.store.recently_bought_codes(0), {"600001"})
        self.assertEqual(self.store.recently_bought_codes(-1), {"600001"})
        # 但模拟记录确实落库了(复盘可见)
        rows = self.store.conn.execute(
            "SELECT stock_code FROM buy_history WHERE is_simulation = 1"
        ).fetchall()
        self.assertEqual([r["stock_code"] for r in rows], ["600000.SH"])

    def test_is_simulation_backfilled_on_legacy_db(self):
        """老库(无 is_simulation 列)重新打开应幂等补列，存量行视为实盘。"""
        import sqlite3 as _sq
        fd, legacy = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.remove(legacy))
        conn = _sq.connect(legacy)
        conn.execute(
            "CREATE TABLE buy_history (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "stock_code TEXT NOT NULL, buy_time TEXT NOT NULL, run_trigger TEXT, "
            "success INTEGER NOT NULL DEFAULT 0, http_status INTEGER, "
            "order_result TEXT, amount REAL)"
        )
        conn.execute(
            "INSERT INTO buy_history (stock_code, buy_time, success) VALUES "
            "('600000.SH', ?, 1)", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),)
        )
        conn.commit()
        conn.close()

        store = AutoBuyStore(legacy)
        self.addCleanup(store.close)
        cols = {r[1] for r in store.conn.execute("PRAGMA table_info(buy_history)")}
        self.assertIn("is_simulation", cols)
        # 存量行默认 0(实盘)，因此仍应参与防重
        self.assertEqual(store.recently_bought_codes(0), {"600000"})

    def test_decision_log(self):
        self.store.record_decision("2026-06-14 10:00:00", "600000", True, {"failed": []})
        rows = self.store.conn.execute("SELECT * FROM decision_log").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["passed"], 1)


# ===========================================================================
# client
# ===========================================================================
class TestClient(unittest.TestCase):
    def setUp(self):
        self.cfg = AutoBuyConfig()
        self.cfg.base_url = "http://127.0.0.1:5000"
        self.cfg.api_token = "tok"
        self.client = WebClient(self.cfg)

    @patch("autobuy.client.requests")
    def test_buy_body_and_token(self, mock_req):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"status": "success", "success_count": 1}
        mock_req.post.return_value = resp
        mock_req.RequestException = Exception

        ok, status, data = self.client.buy("600000")
        self.assertTrue(ok)
        self.assertEqual(status, 200)
        _, kwargs = mock_req.post.call_args
        self.assertEqual(kwargs["json"], {"strategy": "custom_stock", "quantity": 1, "stocks": ["600000"]})
        self.assertEqual(kwargs["headers"].get("X-API-Token"), "tok")

    @patch("autobuy.client.requests")
    def test_buy_failure(self, mock_req):
        resp = MagicMock(status_code=500)
        resp.json.return_value = {"status": "error"}
        mock_req.post.return_value = resp
        mock_req.RequestException = Exception
        ok, status, _ = self.client.buy("600000")
        self.assertFalse(ok)

    @patch("autobuy.client.requests")
    def test_held_codes_parse(self, mock_req):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"data": {"positions": [
            {"stock_code": "600000.SH"}, {"stock_code": "000001.SZ"}
        ]}}
        mock_req.get.return_value = resp
        mock_req.RequestException = Exception
        held = self.client.get_held_codes()
        self.assertEqual(held, {"600000", "000001"})

    @patch("autobuy.client.requests")
    def test_held_codes_failure_returns_none(self, mock_req):
        mock_req.RequestException = Exception
        mock_req.get.side_effect = Exception("conn refused")
        self.assertIsNone(self.client.get_held_codes())

    @patch("autobuy.client.requests")
    def test_held_codes_empty_portfolio_is_not_failure(self, mock_req):
        """真实空仓必须返回空集合(而非 None)，否则空仓账户永远买不进。"""
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"status": "success", "data": {"positions": []}}
        mock_req.get.return_value = resp
        mock_req.RequestException = Exception
        self.assertEqual(self.client.get_held_codes(), set())

    @patch("autobuy.client.requests")
    def test_held_codes_http_error_returns_none(self, mock_req):
        """回归: 401/500 曾被解析成"空仓"→防重 fail-open→可能重复买入。

        非 200 必须返回 None 触发 fail-safe(本轮不下单)。
        """
        mock_req.RequestException = Exception
        for code, body in (
            (401, {"success": False, "status": "error", "error": "invalid token"}),
            (500, {"status": "error", "message": "获取持仓信息时出错"}),
            (404, {"detail": "not found"}),
        ):
            with self.subTest(http_status=code):
                resp = MagicMock(status_code=code)
                resp.json.return_value = body
                mock_req.get.return_value = resp
                self.assertIsNone(self.client.get_held_codes())

    @patch("autobuy.client.requests")
    def test_held_codes_business_error_with_200_returns_none(self, mock_req):
        """服务端以 HTTP 200 返回 status=error 时同样不可当作空仓。"""
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"status": "error", "message": "position_manager 未就绪"}
        mock_req.get.return_value = resp
        mock_req.RequestException = Exception
        self.assertIsNone(self.client.get_held_codes())

    @patch("autobuy.client.requests")
    def test_held_codes_non_json_returns_none(self, mock_req):
        """200 但响应体不是 JSON(如反代返回 HTML 错误页)也须 fail-safe。"""
        resp = MagicMock(status_code=200)
        resp.json.side_effect = ValueError("no json")
        mock_req.get.return_value = resp
        mock_req.RequestException = Exception
        self.assertIsNone(self.client.get_held_codes())


# ===========================================================================
# run_once 惰性求值
# ===========================================================================
class _AppFixtureMixin:
    """构造绕过重量级 __init__ 的 AutoBuyApp，注入 mock 依赖。"""

    def _app(self, candidates, check_result=True, held=None, max_buys=1):
        from autobuy.app import AutoBuyApp
        app = AutoBuyApp.__new__(AutoBuyApp)
        app.cfg = AutoBuyConfig()
        app.cfg.max_buys_per_run = max_buys
        app.cfg.dedup_by_position = True
        app.cfg.dedup_window_days = 1
        # store: 真实临时库
        fd, dbp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        app.store = AutoBuyStore(dbp)
        self.addCleanup(lambda: (app.store.close(), os.remove(dbp)))
        # client: mock
        app.client = MagicMock()
        app.client.get_held_codes.return_value = set(held or [])
        app.client.buy.return_value = (True, 200, {"status": "success"})
        # filter: 计数 check 调用
        app.filter = MagicMock()
        if callable(check_result):
            app.filter.check.side_effect = lambda c: (check_result(c), {"code": c, "failed": []})
        else:
            app.filter.check.side_effect = lambda c: (check_result, {"code": c, "failed": []})
        app.market_filter = MagicMock()
        app.market_filter.check.return_value = (True, {"passed": True, "passed_index": "999999"})
        app._write_status = lambda status: None
        self._candidates = candidates
        return app


class TestRunOnceLazy(_AppFixtureMixin, unittest.TestCase):
    @patch("autobuy.app.read_candidates")
    def test_lazy_stops_after_enough_pass(self, mock_read):
        # 100 只候选全部能通过，max_buys=1 → 只应检查 1 只即停
        codes = [f"{600000 + i}.SH" for i in range(100)]
        mock_read.return_value = codes
        app = self._app(codes, check_result=True, max_buys=1)
        app.run_once("test")
        self.assertEqual(app.filter.check.call_count, 1)
        self.assertEqual(app.client.buy.call_count, 1)

    @patch("autobuy.app.read_candidates")
    def test_dedup_before_check(self, mock_read):
        # 已持仓的不应被检查; held 用规范化 6 位数字
        codes = ["600000.SH", "600001.SH"]
        mock_read.return_value = codes
        app = self._app(codes, check_result=True, held={"600000", "600001"}, max_buys=1)
        app.run_once("test")
        self.assertEqual(app.filter.check.call_count, 0)  # 全被防重，未做检查
        self.assertEqual(app.client.buy.call_count, 0)

    @patch("autobuy.app.read_candidates")
    def test_held_query_fail_no_buy(self, mock_read):
        mock_read.return_value = ["600000.SH"]
        app = self._app(["600000.SH"], check_result=True, max_buys=1)
        app.client.get_held_codes.return_value = None  # 持仓查询失败
        app.run_once("test")
        self.assertEqual(app.client.buy.call_count, 0)  # 安全优先，不下单

    @patch("autobuy.app.read_candidates")
    def test_market_filter_blocks_before_stock_check(self, mock_read):
        mock_read.return_value = ["600000.SH", "600001.SH"]
        app = self._app(["600000.SH", "600001.SH"], check_result=True, max_buys=1)
        app.market_filter.check.return_value = (False, {"passed": False})
        app.run_once("test")
        self.assertEqual(app.client.get_held_codes.call_count, 0)
        self.assertEqual(app.filter.check.call_count, 0)
        self.assertEqual(app.client.buy.call_count, 0)

    @patch("autobuy.app.read_candidates")
    def test_dedup_window_blocks_second_run_end_to_end(self, mock_read):
        """回归(Bug1): 同一只股票在防重窗口内不得被第二轮再次买入。

        走完整生产路径: read_candidates 产出 '600000.SH' → 下单 → record_buy
        写入 '600000.SH' → 次轮 _dedup_filter 用 '600000' 比较。
        修复前两轮都会下单(防重恒不命中)。
        """
        mock_read.return_value = ["600000.SH"]
        app = self._app(["600000.SH"], check_result=True, max_buys=1)
        app.cfg.dedup_by_position = False  # 隔离出 dedup_window 这一条防线

        app.run_once("round-1")
        self.assertEqual(app.client.buy.call_count, 1)

        app.run_once("round-2")
        self.assertEqual(app.client.buy.call_count, 1, "第二轮不应重复下单")
        self.assertEqual(app.filter.check.call_count, 1, "已买过的标的不应再做条件检查")


# ===========================================================================
# 模拟运行模式
# ===========================================================================
class TestSimulationMode(_AppFixtureMixin, unittest.TestCase):
    """模拟运行: 除不发真实买入请求外，其余逻辑与实盘完全一致。"""

    @patch("autobuy.app.read_candidates")
    def test_no_real_order_but_full_pipeline(self, mock_read):
        codes = ["600000.SH", "600001.SH"]
        mock_read.return_value = codes
        app = self._app(codes, check_result=True, max_buys=1)
        app.cfg.simulation_mode = True
        app.run_once("sim")

        # 未发真实下单请求
        self.assertEqual(app.client.buy.call_count, 0)
        # 但门禁/防重/条件检查/决策日志全部照常执行
        self.assertEqual(app.market_filter.check.call_count, 1)
        self.assertEqual(app.client.get_held_codes.call_count, 1)
        self.assertEqual(app.filter.check.call_count, 1)
        decisions = app.store.conn.execute("SELECT COUNT(*) c FROM decision_log").fetchone()["c"]
        self.assertEqual(decisions, 1)

    @patch("autobuy.app.read_candidates")
    def test_simulated_buy_recorded_and_flagged(self, mock_read):
        mock_read.return_value = ["600000.SH"]
        app = self._app(["600000.SH"], check_result=True, max_buys=1)
        app.cfg.simulation_mode = True
        app.run_once("sim")

        rows = app.store.conn.execute(
            "SELECT stock_code, success, is_simulation, http_status FROM buy_history"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stock_code"], "600000.SH")
        self.assertEqual(rows[0]["success"], 1)
        self.assertEqual(rows[0]["is_simulation"], 1)
        self.assertIsNone(rows[0]["http_status"])

    @patch("autobuy.app.read_candidates")
    def test_simulated_run_does_not_block_later_real_buy(self, mock_read):
        """模拟买入不参与防重: 试跑后切回实盘，同一只仍可买入。"""
        mock_read.return_value = ["600000.SH"]
        app = self._app(["600000.SH"], check_result=True, max_buys=1)
        app.cfg.dedup_by_position = False

        app.cfg.simulation_mode = True
        app.run_once("sim")
        self.assertEqual(app.client.buy.call_count, 0)

        app.cfg.simulation_mode = False
        app.run_once("real")
        self.assertEqual(app.client.buy.call_count, 1, "模拟记录不应挡住真实买入")

    @patch("autobuy.app.read_candidates")
    def test_status_file_marks_simulation(self, mock_read):
        mock_read.return_value = ["600000.SH"]
        app = self._app(["600000.SH"], check_result=True, max_buys=1)
        app.cfg.simulation_mode = True
        captured = {}
        app._write_status = lambda s: captured.update(s)
        app.run_once("sim")
        self.assertTrue(captured["simulation_mode"])
        self.assertEqual(captured["bought"], ["600000.SH"])

    @patch("autobuy.app.read_candidates")
    def test_simulation_still_respects_market_gate(self, mock_read):
        """模拟模式不绕过任何门禁: 大盘不通过时同样不产生买入记录。"""
        mock_read.return_value = ["600000.SH"]
        app = self._app(["600000.SH"], check_result=True, max_buys=1)
        app.cfg.simulation_mode = True
        app.market_filter.check.return_value = (False, {"passed": False})
        app.run_once("sim")
        rows = app.store.conn.execute("SELECT COUNT(*) c FROM buy_history").fetchone()["c"]
        self.assertEqual(rows, 0)

    @patch("autobuy.app.read_candidates")
    def test_simulation_still_respects_held_query_failure(self, mock_read):
        """模拟模式同样遵守 fail-safe: 持仓查询失败则本轮不买。"""
        mock_read.return_value = ["600000.SH"]
        app = self._app(["600000.SH"], check_result=True, max_buys=1)
        app.cfg.simulation_mode = True
        app.client.get_held_codes.return_value = None
        app.run_once("sim")
        rows = app.store.conn.execute("SELECT COUNT(*) c FROM buy_history").fetchone()["c"]
        self.assertEqual(rows, 0)


class TestScheduleTradeTime(unittest.TestCase):
    """调度时段门禁: 非交易时段必须停止定时筛选。

    回归点: 修复前 autobuy 误用 config.is_trade_time()，该函数在模拟模式下恒为
    True，导致非交易时段(如 19:00)仍触发完整筛选。改用 config.is_market_hours()
    后按真实市场时钟判断。
    """

    def _app(self, only_trade_time=True):
        from datetime import datetime, timedelta
        from autobuy.app import AutoBuyApp
        app = AutoBuyApp.__new__(AutoBuyApp)
        app.cfg = AutoBuyConfig()
        app.cfg.mode = "interval"
        app.cfg.only_trade_time = only_trade_time
        app.cfg.interval_minutes = 30
        app._fired_daily = set()
        app._fired_daily_date = None
        app._last_interval_run = datetime.now() - timedelta(hours=1)  # 确保 interval 已到点
        app._safe_run = MagicMock()
        return app

    @patch("autobuy.app.config.is_market_hours", return_value=False)
    def test_interval_skipped_outside_market_hours(self, _m):
        app = self._app()
        before = app._last_interval_run
        app._tick()
        app._safe_run.assert_not_called()
        self.assertEqual(app._last_interval_run, before)  # 计时器未被消费 → 开盘后可立即触发

    @patch("autobuy.app.config.is_market_hours", return_value=True)
    def test_interval_runs_in_market_hours(self, _m):
        app = self._app()
        app._tick()
        app._safe_run.assert_called_once()

    @patch("autobuy.app.config.is_trade_time", return_value=True)
    @patch("autobuy.app.config.is_market_hours", return_value=False)
    def test_simulation_bypass_does_not_resume_scheduling(self, _mh, _tt):
        # 即使 is_trade_time()=True(模拟旁路), 非交易时段仍须停止 —— 锁定原 bug
        app = self._app()
        app._tick()
        app._safe_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
