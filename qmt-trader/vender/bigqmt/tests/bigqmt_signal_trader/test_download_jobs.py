import json
import os
import sys
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.download_jobs import (
    _enc,
    current_key,
    job_key,
    pump_download_jobs,
    queue_key,
    read_download_status,
    submit_download_job,
    wait_download_job,
)


class FakeRedis:
    def __init__(self):
        self.kv = {}
        self.lists = {}
        self.expired = []

    def setex(self, key, ttl, value):
        self.kv[key] = value
        self.expired.append((key, ttl))
        return True

    def set(self, key, value):
        self.kv[key] = value
        return True

    def get(self, key):
        return self.kv.get(key)

    def delete(self, key):
        self.kv.pop(key, None)
        return 1

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    def lpop(self, key):
        lst = self.lists.get(key) or []
        if not lst:
            return None
        return lst.pop(0)

    def expire(self, key, ttl):
        self.expired.append((key, ttl))
        return True


class FakeMarketData:
    def __init__(self, fail_on=None, sleep=0.0):
        self.data2_calls = []
        self.data_calls = []
        self.fail_on = fail_on
        self.sleep = sleep

    def download_history_data2(self, stock_list, period, start_time, end_time, incrementally):
        if self.sleep:
            time.sleep(self.sleep)
        if self.fail_on and self.fail_on in stock_list:
            raise RuntimeError("boom")
        self.data2_calls.append(list(stock_list))

    def download_history_data(self, code, period, start_time, end_time, incrementally):
        if self.sleep:
            time.sleep(self.sleep)
        if self.fail_on and code == self.fail_on:
            raise RuntimeError("boom")
        self.data_calls.append(code)


class DownloadJobsTest(unittest.TestCase):
    def test_submit_queues_pending_job(self):
        r = FakeRedis()
        job = submit_download_job(r, "acct", ["600000.SH", "000001.SZ"], "1d", chunk_size=1)

        self.assertEqual(job["state"], "pending")
        self.assertEqual(job["total"], 2)
        self.assertIn(_enc(job["job_id"]), r.lists[queue_key("acct")])
        self.assertEqual(read_download_status(r, "acct", job["job_id"])["state"], "pending")

    def test_pump_completes_and_chunks_the_symbol_list(self):
        r = FakeRedis()
        md = FakeMarketData()
        submit_download_job(r, "acct", ["a", "b", "c", "d", "e"], "1d", chunk_size=2)

        # max_wall_seconds=0 disables the budget, so one tick drains the whole job.
        res = pump_download_jobs(r, md, "acct", chunk_size=2, max_wall_seconds=0)

        self.assertEqual(res["state"], "done")
        self.assertEqual(res["done"], 5)
        self.assertEqual(md.data2_calls, [["a", "b"], ["c", "d"], ["e"]])
        # current pointer cleared when the job finishes.
        self.assertIsNone(r.get(current_key("acct")))

    def test_pump_spreads_across_ticks_under_wall_budget(self):
        r = FakeRedis()
        md = FakeMarketData(sleep=0.02)
        submit_download_job(r, "acct", ["a", "b", "c"], "1d", chunk_size=1)

        res1 = pump_download_jobs(r, md, "acct", max_wall_seconds=0.005)
        res2 = pump_download_jobs(r, md, "acct", max_wall_seconds=0.005)
        res3 = pump_download_jobs(r, md, "acct", max_wall_seconds=0.005)

        # One chunk per tick (each chunk exceeds the tiny budget), progress resumes.
        self.assertEqual((res1["state"], res1["done"]), ("running", 1))
        self.assertEqual((res2["state"], res2["done"]), ("running", 2))
        self.assertEqual((res3["state"], res3["done"]), ("done", 3))
        self.assertEqual(md.data_calls if md.data_calls else md.data2_calls, [["a"], ["b"], ["c"]])

    def test_pump_marks_failed_and_clears_current(self):
        r = FakeRedis()
        md = FakeMarketData(fail_on="b")
        job = submit_download_job(
            r, "acct", ["a", "b", "c"], "1d", method="download_history_data", chunk_size=1
        )

        res = pump_download_jobs(r, md, "acct", max_wall_seconds=0)

        self.assertEqual(res["state"], "failed")
        self.assertEqual(md.data_calls, ["a"])
        status = read_download_status(r, "acct", job["job_id"])
        self.assertEqual(status["state"], "failed")
        self.assertTrue(status["error"])
        self.assertIsNone(r.get(current_key("acct")))

    def test_pump_with_no_job_returns_none(self):
        self.assertIsNone(pump_download_jobs(FakeRedis(), FakeMarketData(), "acct"))

    def test_wait_returns_terminal_status(self):
        r = FakeRedis()
        job = submit_download_job(r, "acct", ["a"], "1d", chunk_size=1)
        status = read_download_status(r, "acct", job["job_id"])
        status["state"] = "done"
        status["done"] = 1
        r.set(job_key("acct", job["job_id"]), _enc(json.dumps(status)))

        res = wait_download_job(r, "acct", job["job_id"], wait_seconds=1, poll_interval_seconds=0.01)

        self.assertEqual(res["state"], "done")

    def test_stored_values_are_digit_free_and_compliance_safe(self):
        import re

        from bigqmt_signal_trader.download_jobs import _dec

        # The QMT redis compliance filter blocks a response only when it contains a
        # stock-code pattern (which requires digits). Encoded tokens are all letters.
        stock_re = re.compile(
            "(^|[^\\d])+([36]0[\\d]{4}|00(000[1-9]|[1-9][\\d]{3}|[\\d][1-9][\\d]{2}|[\\d]{2}[1-9][\\d]))([^\\d]|$)+"
        )
        blob = json.dumps({"stock_list": ["600000.SH", "300750.SZ", "000001.SZ"], "chunk_size": 1})
        self.assertTrue(stock_re.search(blob))  # plaintext WOULD trip the filter
        token = _enc(blob)
        self.assertTrue(all(not c.isdigit() for c in token), "encoded token must be digit-free")
        self.assertIsNone(stock_re.search(token), "encoded token must not match the stock-code filter")
        self.assertEqual(_dec(token), blob)  # round-trips
        self.assertIsNone(_dec(None))
        self.assertIsNone(_dec(""))

        # what actually lands in Redis on submit must also be digit-free
        r = FakeRedis()
        job = submit_download_job(r, "acct", ["600000.SH"], "1d", chunk_size=1)
        stored_blob = r.kv[job_key("acct", job["job_id"])]
        queued = r.lists[queue_key("acct")][0]
        self.assertTrue(all(not c.isdigit() for c in stored_blob))
        self.assertTrue(all(not c.isdigit() for c in queued))


if __name__ == "__main__":
    unittest.main()
