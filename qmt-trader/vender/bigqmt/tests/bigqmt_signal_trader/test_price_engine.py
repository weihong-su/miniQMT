import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.price_engine import build_order_price


class FakeMarketDataProvider:
    def get_ticks(self, codes):
        return {
            "000001.SZ": {
                "lastPrice": 10.0,
                "askPrice": [10.01, 10.02],
                "bidPrice": [9.99, 9.98],
            }
        }

    def get_instrument(self, code):
        return {
            "InstrumentStatus": 0,
            "UpStopPrice": 11.0,
            "DownStopPrice": 9.0,
        }


class PriceEngineTest(unittest.TestCase):
    def test_auto_buy_price_uses_ask2_when_better_than_markup(self):
        price = build_order_price(FakeMarketDataProvider(), "000001.SZ", "BUY")
        self.assertEqual(price, 10.02)

    def test_auto_sell_price_uses_bid2_when_better_than_discount(self):
        price = build_order_price(FakeMarketDataProvider(), "000001.SZ", "SELL")
        self.assertEqual(price, 9.98)


if __name__ == "__main__":
    unittest.main()
