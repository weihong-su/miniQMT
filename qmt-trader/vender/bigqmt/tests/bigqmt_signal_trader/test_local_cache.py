import os
import shutil
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.local_cache import LocalMarketCache


def _has_pyarrow():
    try:
        import pyarrow  # noqa: F401

        return True
    except Exception:
        return False


class LocalMarketCacheTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_write_read_merge_dedupe(self):
        import pandas as pd

        c = LocalMarketCache(self.dir)
        c.write("600000.SH", "1d", pd.DataFrame({"stime": ["20260101", "20260102"], "close": [1.0, 2.0]}))
        # overlapping second write: 20260102 should be replaced (keep last), 20260103 appended
        c.write("600000.SH", "1d", pd.DataFrame({"stime": ["20260102", "20260103"], "close": [2.5, 3.0]}))

        df = c.read("600000.SH", "1d")
        self.assertEqual(list(df["stime"]), ["20260101", "20260102", "20260103"])
        self.assertEqual(df[df["stime"] == "20260102"]["close"].iloc[0], 2.5)

    def test_range_and_count_filters(self):
        import pandas as pd

        c = LocalMarketCache(self.dir)
        c.write("X", "1d", pd.DataFrame({"stime": ["20260101", "20260102", "20260103"], "close": [1, 2, 3]}))

        self.assertEqual(list(c.read("X", "1d", start_time="20260102")["stime"]), ["20260102", "20260103"])
        self.assertEqual(list(c.read("X", "1d", end_time="20260102")["stime"]), ["20260101", "20260102"])
        self.assertEqual(list(c.read("X", "1d", count=1)["stime"]), ["20260103"])
        self.assertIsNone(c.read("MISSING", "1d"))
        self.assertEqual(c.covered("X", "1d"), ("20260101", "20260103", 3))

    def test_drops_zero_fill_placeholder_rows(self):
        import pandas as pd

        c = LocalMarketCache(self.dir)
        df = pd.DataFrame(
            {"stime": ["20200101", "20200102", "20260701"], "close": [0.0, 0.0, 8.65], "open": [0.0, 0.0, 8.58]}
        )
        c.write("X", "1d", df)
        self.assertEqual(list(c.read("X", "1d")["stime"]), ["20260701"])  # 0-fill dropped

        # an all-placeholder write must not create/overwrite a cache file
        self.assertEqual(c.write("Y", "1d", pd.DataFrame({"stime": ["20200101"], "close": [0.0]})), 0)
        self.assertIsNone(c.read("Y", "1d"))

    def test_dividend_type_keeps_separate_caches(self):
        import pandas as pd

        c = LocalMarketCache(self.dir)
        c.write("X", "1d", pd.DataFrame({"stime": ["20260101"], "close": [10.0]}), dividend_type="none")
        c.write("X", "1d", pd.DataFrame({"stime": ["20260101"], "close": [9.0]}), dividend_type="front")

        self.assertEqual(c.read("X", "1d", dividend_type="none")["close"].iloc[0], 10.0)
        self.assertEqual(c.read("X", "1d", dividend_type="front")["close"].iloc[0], 9.0)
        self.assertIsNone(c.read("X", "1d", dividend_type="back"))

    def test_pickle_format_roundtrip(self):
        import pandas as pd

        c = LocalMarketCache(self.dir, fmt="pkl")
        c.write("X", "1d", pd.DataFrame({"stime": ["20260101", "20260102"], "close": [1.0, 2.0]}))
        self.assertTrue(c.path("X", "1d").endswith(".pkl"))
        self.assertEqual(list(c.read("X", "1d")["close"]), [1.0, 2.0])

    @unittest.skipUnless(_has_pyarrow(), "pyarrow not installed")
    def test_parquet_format_roundtrip(self):
        import pandas as pd

        c = LocalMarketCache(self.dir, fmt="parquet")
        c.write("X", "1d", pd.DataFrame({"stime": ["20260101", "20260102"], "close": [1.0, 2.0]}))
        self.assertTrue(c.path("X", "1d").endswith(".parquet"))
        self.assertEqual(list(c.read("X", "1d")["close"]), [1.0, 2.0])

    @unittest.skipUnless(_has_pyarrow(), "pyarrow not installed")
    def test_migrates_pickle_to_parquet(self):
        import pandas as pd

        LocalMarketCache(self.dir, fmt="pkl").write("X", "1d", pd.DataFrame({"stime": ["20260101"], "close": [1.0]}))
        pq = LocalMarketCache(self.dir, fmt="parquet")
        self.assertEqual(list(pq.read("X", "1d")["close"]), [1.0])  # reads the old pkl
        pq.write("X", "1d", pd.DataFrame({"stime": ["20260102"], "close": [2.0]}))
        self.assertTrue(os.path.isfile(pq.path("X", "1d")))  # parquet now exists
        self.assertFalse(os.path.isfile(pq.path("X", "1d")[:-8] + ".pkl"))  # old pkl removed
        self.assertEqual(list(pq.read("X", "1d")["close"]), [1.0, 2.0])  # merged across formats


class FakeClient:
    def __init__(self, cache_dir, fallback_rpc=False):
        self.account_id = "acct"
        self.calls = []
        self.local_cache_config = {"enabled": True, "dir": cache_dir, "fallback_rpc": fallback_rpc}

    def _redis(self):
        return None

    def call(self, method, params=None, account_id=None, timeout_seconds=None):
        self.calls.append(method)
        if method == "get_market_data_ex":
            import pandas as pd

            codes = (params or {}).get("stock_list") or []
            return {c: pd.DataFrame({"stime": ["20260626", "20260629"], "close": [8.76, 8.73]}) for c in codes}
        raise AssertionError("unexpected rpc: %s" % method)


class LocalCacheClientTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _xt(self, fallback_rpc=False):
        from bigqmt_signal_trader.xtquant_compat import BigQmtXtData

        return BigQmtXtData(FakeClient(self.dir, fallback_rpc=fallback_rpc))

    def test_download_caches_then_get_local_reads_without_rpc(self):
        xt = self._xt()
        progress = []
        res = xt.download_history_data2(["600000.SH", "000001.SZ"], "1d", callback=lambda d: progress.append(d))

        self.assertEqual(res, {"finished": 2, "total": 2})
        self.assertEqual(len(progress), 2)
        self.assertEqual(progress[-1]["stockcode"], "000001.SZ")
        self.assertEqual(progress[-1]["finished"], 2)
        calls_after_download = list(xt.client.calls)

        data = xt.get_local_data(stock_list=["600000.SH", "000001.SZ"], period="1d")

        self.assertIn("600000.SH", data)
        self.assertIn("000001.SZ", data)
        self.assertEqual(list(data["600000.SH"]["close"]), [8.76, 8.73])
        # get_local_data must NOT issue any further RPC — pure local read.
        self.assertEqual(xt.client.calls, calls_after_download)

    def test_get_market_data_ex_caches_through(self):
        xt = self._xt()
        # a plain live read must also populate the cache (cache-through)
        xt.get_market_data_ex(field_list=["close"], stock_list=["600000.SH"], period="1d")
        n = len(xt.client.calls)

        data = xt.get_local_data(stock_list=["600000.SH"], period="1d")
        self.assertIn("600000.SH", data)
        self.assertEqual(len(xt.client.calls), n)  # served from cache, no extra RPC

    def test_get_local_miss_returns_empty_and_no_rpc(self):
        xt = self._xt()
        data = xt.get_local_data(stock_list=["600000.SH"], period="1d")
        self.assertEqual(data, {})
        self.assertEqual(xt.client.calls, [])

    def test_get_local_fallback_rpc_fetches_and_caches(self):
        xt = self._xt(fallback_rpc=True)
        data = xt.get_local_data(stock_list=["600000.SH"], period="1d")
        self.assertIn("600000.SH", data)
        self.assertIn("get_market_data_ex", xt.client.calls)  # fetched on miss
        # second read is served from cache — no new RPC
        n = len(xt.client.calls)
        xt.get_local_data(stock_list=["600000.SH"], period="1d")
        self.assertEqual(len(xt.client.calls), n)


if __name__ == "__main__":
    unittest.main()
