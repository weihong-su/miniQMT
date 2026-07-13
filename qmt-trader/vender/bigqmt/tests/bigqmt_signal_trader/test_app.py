import datetime
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.app import SignalTradingApp
from bigqmt_signal_trader.models import AssetSnapshot, PositionSnapshot, TradeSignal


def _signal(**kwargs):
    payload = {
        "signal_id": "sig-buy-001",
        "account_id": "test",
        "action": "BUY",
        "stock_code": "000001.SZ",
        "amount": 100,
        "price_type": "AUTO_LIMIT",
        "remark": "web_buy_command",
        "created_at": "2026-06-30 09:31:00",
        "expire_at": "2026-06-30 09:36:00",
        "schema_version": 1,
    }
    payload.update(kwargs)
    return TradeSignal.from_dict(payload)


class FakeSignalSource:
    def __init__(self, items):
        self.items = items
        self.acked = []

    def fetch(self, account_id, limit):
        return self.items[:limit]

    def ack(self, signal):
        self.acked.append(signal.signal_id)


class FakeMarketDataProvider:
    def get_ticks(self, codes):
        return {
            code: {
                "lastPrice": 10.0,
                "askPrice": [10.01, 10.02],
                "bidPrice": [9.99, 9.98],
            }
            for code in codes
        }

    def get_instrument(self, code):
        return {"InstrumentStatus": 0, "UpStopPrice": 11.0, "DownStopPrice": 9.0}


class FakePositionProvider:
    def __init__(self):
        self.positions = {
            "000001.SZ": PositionSnapshot(
                stock_code="000001.SZ",
                volume=1000,
                available=500,
                cost=9.5,
            )
        }

    def get_positions(self, account_id):
        return self.positions

    def get_asset(self, account_id):
        return AssetSnapshot(account_id=account_id, cash=100000.0, total_asset=200000.0)


class FakeOrderGateway:
    def __init__(self):
        self.submitted = []

    def submit(self, request):
        self.submitted.append(request)
        from bigqmt_signal_trader.models import OrderSubmitResult

        return OrderSubmitResult(status="SUBMITTED", user_order_id="bq:sig:1")

    def cancel(self, order_ref):
        return None

    def query_orders(self, account_id, strategy_name):
        return []

    def query_trades(self, account_id, strategy_name):
        return []


class FakePositionSyncSink:
    def __init__(self):
        self.snapshots = []

    def publish(self, snapshot):
        self.snapshots.append(snapshot)


class FakeStateStore:
    def __init__(self, claim_result=True):
        self.claim_result = claim_result
        self.claimed = []
        self.submitted = []
        self.finished = []

    def claim(self, signal, consumer_id):
        self.claimed.append((signal.signal_id, consumer_id))
        return self.claim_result

    def mark_submitted(self, signal_id, result):
        self.submitted.append((signal_id, result.status))

    def mark_finished(self, signal_id, status, message=""):
        self.finished.append((signal_id, status, message))


class SignalTradingAppTest(unittest.TestCase):
    def test_tick_submits_buy_signal_with_replaceable_adapters(self):
        source = FakeSignalSource([_signal()])
        state = FakeStateStore()
        orders = FakeOrderGateway()
        sync = FakePositionSyncSink()
        app = SignalTradingApp(
            account_id="test",
            signal_source=source,
            market_data=FakeMarketDataProvider(),
            position_provider=FakePositionProvider(),
            order_gateway=orders,
            position_sync_sink=sync,
            state_store=state,
            consumer_id="consumer-a",
        )

        app.tick(datetime.datetime(2026, 6, 30, 9, 31))

        self.assertEqual(orders.submitted[0].stock_code, "000001.SZ")
        self.assertEqual(orders.submitted[0].volume, 100)
        self.assertEqual(state.submitted, [("sig-buy-001", "SUBMITTED")])
        self.assertEqual(source.acked, ["sig-buy-001"])
        self.assertEqual(sync.snapshots[0].account_id, "test")

    def test_tick_sells_by_percentage_using_available_position(self):
        source = FakeSignalSource([
            _signal(
                signal_id="sig-sell-001",
                action="SELL",
                amount=None,
                percentage=50,
                remark="web_sell_command",
            )
        ])
        orders = FakeOrderGateway()
        app = SignalTradingApp(
            account_id="test",
            signal_source=source,
            market_data=FakeMarketDataProvider(),
            position_provider=FakePositionProvider(),
            order_gateway=orders,
            position_sync_sink=FakePositionSyncSink(),
            state_store=FakeStateStore(),
            consumer_id="consumer-a",
        )

        app.tick(datetime.datetime(2026, 6, 30, 9, 31))

        self.assertEqual(orders.submitted[0].action, "SELL")
        self.assertEqual(orders.submitted[0].volume, 200)


if __name__ == "__main__":
    unittest.main()
