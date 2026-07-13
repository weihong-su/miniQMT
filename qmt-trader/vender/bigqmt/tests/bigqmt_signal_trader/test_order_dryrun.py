import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapters.order_dryrun import DryRunOrderGateway
from bigqmt_signal_trader.models import OrderRequest


class DryRunOrderGatewayTest(unittest.TestCase):
    def test_submit_records_request_without_real_order(self):
        gateway = DryRunOrderGateway()
        request = OrderRequest(
            signal_id="sig-001",
            account_id="test",
            action="BUY",
            stock_code="000001.SZ",
            volume=100,
            price=10.02,
            price_type="LIMIT",
            strategy_name="bigqmt_signal_trader",
            remark="web_buy_command",
        )

        result = gateway.submit(request)

        self.assertEqual(result.status, "DRY_RUN")
        self.assertEqual(gateway.submitted[0], request)
        self.assertIn("sig-001", result.user_order_id)


if __name__ == "__main__":
    unittest.main()
