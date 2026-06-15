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

from autobuy.config import AutoBuyConfig, load_config
from autobuy.pool import normalize_code, read_candidates, recent_trading_dates, to_xt_code
from autobuy.filter import MARKET_INDEX_CODES, MarketIndexFilter, BuyConditionFilter
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

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            load_config("no_such_file_xyz.cfg")


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
        self.cfg = AutoBuyConfig()  # 默认: 换手率/量比/MA8方向/价格相对MA8 启用, 涨幅关
        # 上升的收盘价序列 (12 根)，ma8 向上
        self.close_up = [9.7, 9.75, 9.8, 9.85, 9.9, 9.95, 10.0, 10.05, 10.1, 10.2, 10.3, 10.4]
        self.df_up = pd.DataFrame({"close": self.close_up, "volume": [100] * 12})

    def _quote(self, price=10.5, last_close=10.0, volume=1000):
        return {"lastPrice": price, "lastClose": last_close, "volume": volume}

    def _detail(self, float_vol=1_000_000, up=11.0):
        return {"FloatVolume": float_vol, "UpStopPrice": up}

    def test_all_pass(self):
        dm = _make_dm(self._quote(), self.df_up, self._detail())
        ok, reason = BuyConditionFilter(self.cfg, dm).check("600000")
        self.assertTrue(ok, reason)
        self.assertAlmostEqual(reason["turnover_rate"], 0.1, places=3)  # 1000*100/1e6
        self.assertGreaterEqual(reason["volume_ratio"], 2.0)
        self.assertTrue(reason["ma8_uptrend"])

    def test_turnover_too_low(self):
        dm = _make_dm(self._quote(), self.df_up, self._detail(float_vol=10_000_000_000))
        ok, reason = BuyConditionFilter(self.cfg, dm).check("600000")
        self.assertFalse(ok)
        self.assertTrue(any("换手率" in r for r in reason["failed"]))

    def test_volume_ratio_too_low(self):
        df = pd.DataFrame({"close": self.close_up, "volume": [1000] * 12})  # today=1000 / avg5=1000 = 1
        dm = _make_dm(self._quote(volume=1000), df, self._detail())
        ok, reason = BuyConditionFilter(self.cfg, dm).check("600000")
        self.assertFalse(ok)
        self.assertTrue(any("量比" in r for r in reason["failed"]))

    def test_ma8_downtrend(self):
        df = pd.DataFrame({"close": list(reversed(self.close_up)), "volume": [100] * 12})
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
        self.assertEqual(dm.download_history_data.call_args_list[0].args[0], "999999.SH")
        self.assertEqual(dm.download_history_data.call_args_list[1].args[0], "399001.SZ")

    def test_index_alias_fallback(self):
        dm = MagicMock()
        up = self._df([1, 2, 3, 4, 5, 6])
        dm.download_history_data.side_effect = [None, up]

        ok, reason = MarketIndexFilter(dm, index_codes=("999999",)).check()

        self.assertTrue(ok, reason)
        self.assertEqual(reason["details"]["999999"]["code"], "sh.000001")
        self.assertEqual(dm.download_history_data.call_args_list[0].args[0], "999999.SH")
        self.assertEqual(dm.download_history_data.call_args_list[1].args[0], "sh.000001")

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
        resp = MagicMock()
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


# ===========================================================================
# run_once 惰性求值
# ===========================================================================
class TestRunOnceLazy(unittest.TestCase):
    def _app(self, candidates, check_result=True, held=None, max_buys=1):
        """构造一个绕过重量级 __init__ 的 AutoBuyApp，注入 mock 依赖。"""
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


if __name__ == "__main__":
    unittest.main()
