import datetime
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.models import SignalAction, SignalStatus, TradeSignal


def _base_payload(**kwargs):
    payload = {
        "signal_id": "sig-001",
        "account_id": "test",
        "action": "BUY",
        "stock_code": "000001.SZ",
        "amount": 100,
        "price_type": "AUTO_LIMIT",
        "created_at": "2026-06-30 09:31:00",
        "expire_at": "2026-06-30 09:36:00",
        "schema_version": 1,
    }
    payload.update(kwargs)
    return payload


class TradeSignalModelTest(unittest.TestCase):
    def test_trade_signal_requires_signal_id(self):
        payload = _base_payload()
        payload.pop("signal_id")

        with self.assertRaisesRegex(ValueError, "signal_id"):
            TradeSignal.from_dict(payload)

    def test_trade_signal_parses_buy_payload(self):
        signal = TradeSignal.from_dict(_base_payload())

        self.assertEqual(signal.signal_id, "sig-001")
        self.assertEqual(signal.action, SignalAction.BUY)
        self.assertEqual(signal.status, SignalStatus.PENDING)
        self.assertEqual(signal.amount, 100)

    def test_sell_signal_requires_amount_or_percentage(self):
        payload = _base_payload(action="SELL", amount=None, percentage=None)

        with self.assertRaisesRegex(ValueError, "amount or percentage"):
            TradeSignal.from_dict(payload)

    def test_expired_signal_is_detected(self):
        signal = TradeSignal.from_dict(_base_payload(expire_at="2026-06-30 09:32:00"))
        now = datetime.datetime(2026, 6, 30, 9, 32, 1)

        self.assertTrue(signal.is_expired(now))


if __name__ == "__main__":
    unittest.main()
