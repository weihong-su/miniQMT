import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.full_tick_cache import (
    full_tick_demand_key,
    full_tick_request_id,
    read_full_tick_cache,
    refresh_full_tick_cache,
    request_full_tick_cache,
)


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.kv = {}
        self.deleted = []
        self.expired = []

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value
        return 1

    def hgetall(self, key):
        return self.hashes.get(key, {})

    def hdel(self, key, field):
        self.deleted.append((key, field))
        self.hashes.setdefault(key, {}).pop(field, None)
        return 1

    def expire(self, key, seconds):
        self.expired.append((key, seconds))
        return True

    def setex(self, key, seconds, value):
        self.kv[key] = value
        self.expired.append((key, seconds))
        return True

    def get(self, key):
        return self.kv.get(key)


class FakeContext:
    def __init__(self):
        self.calls = []

    def get_full_tick(self, codes):
        self.calls.append(list(codes))
        return {codes[0]: {"lastPrice": 10.0, "bidPrice": [9.9], "askPrice": [10.1]}}


class FullTickCacheTest(unittest.TestCase):
    def test_request_then_refresh_writes_fresh_snapshot(self):
        redis_client = FakeRedis()
        context = FakeContext()

        demand = request_full_tick_cache(redis_client, "acct", ["600000"], demand_ttl_seconds=10)
        refreshed = refresh_full_tick_cache(redis_client, context, "acct", cache_ttl_seconds=10)
        ticks = read_full_tick_cache(redis_client, "acct", ["600000.SH"], max_age_seconds=10)

        self.assertEqual(demand["codes"], ["600000.SH"])
        self.assertEqual(refreshed, 1)
        self.assertEqual(context.calls, [["600000.SH"]])
        self.assertEqual(ticks["600000.SH"]["lastPrice"], 10.0)

    def test_expired_demand_is_removed_without_refreshing(self):
        redis_client = FakeRedis()
        context = FakeContext()
        key = full_tick_demand_key("acct")
        request_id = full_tick_request_id(["600000.SH"])
        redis_client.hset(
            key,
            request_id,
            '{"request_id":"%s","codes":["600000.SH"],"requested_at_ts":1,"expires_at_ts":1}' % request_id,
        )

        refreshed = refresh_full_tick_cache(redis_client, context, "acct", cache_ttl_seconds=10)

        self.assertEqual(refreshed, 0)
        self.assertEqual(context.calls, [])
        self.assertIn((key, request_id), redis_client.deleted)

    def test_refresh_kind_symbol_skips_market_demands(self):
        redis_client = FakeRedis()
        context = FakeContext()
        request_full_tick_cache(redis_client, "acct", ["600000"], demand_ttl_seconds=10)
        request_full_tick_cache(redis_client, "acct", ["SH", "SZ"], demand_ttl_seconds=10)

        refreshed = refresh_full_tick_cache(redis_client, context, "acct", cache_ttl_seconds=10, kind="symbol")

        self.assertEqual(refreshed, 1)
        self.assertEqual(context.calls, [["600000.SH"]])

    def test_refresh_kind_market_skips_symbol_demands(self):
        redis_client = FakeRedis()
        context = FakeContext()
        request_full_tick_cache(redis_client, "acct", ["600000"], demand_ttl_seconds=10)
        request_full_tick_cache(redis_client, "acct", ["SH", "SZ"], demand_ttl_seconds=10)

        refreshed = refresh_full_tick_cache(redis_client, context, "acct", cache_ttl_seconds=10, kind="market")

        self.assertEqual(refreshed, 1)
        self.assertEqual(context.calls, [["SH", "SZ"]])


if __name__ == "__main__":
    unittest.main()
