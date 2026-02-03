"""
Test 6: System Integration Test

Tests end-to-end integration and complete workflows
"""

import unittest
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from test_base import TestBase
from test_mocks import create_mock_qmt_trader
from logger import get_logger

logger = get_logger("test_system_integration")


class TestSystemIntegration(TestBase):
    """Test system integration and complete workflows"""

    def setUp(self):
        """Setup before each test"""
        super().setUp()
        self.mock_trader = create_mock_qmt_trader()
        self.mock_trader.connect()

    def tearDown(self):
        """Cleanup after each test"""
        if self.mock_trader:
            self.mock_trader.disconnect()
        super().tearDown()

    def test_01_system_configuration_integration(self):
        """Test that all system components can access configuration"""
        logger.info("Testing system configuration integration")

        # Verify critical configurations are accessible
        self.assertTrue(hasattr(config, 'ENABLE_SIMULATION_MODE'))
        self.assertTrue(hasattr(config, 'ENABLE_AUTO_TRADING'))
        self.assertTrue(hasattr(config, 'DB_PATH'))

        # Verify test mode is properly set
        self.assertTrue(config.ENABLE_SIMULATION_MODE, "Should be in simulation mode")
        self.assertFalse(config.ENABLE_AUTO_TRADING, "Auto trading should be disabled in tests")

        logger.info("System configuration integration verified")

    def test_02_database_and_config_integration(self):
        """Test database configuration integration"""
        logger.info("Testing database and config integration")

        db_path = config.DB_PATH
        self.assertIsNotNone(db_path, "DB path should be configured")

        # Verify it's test database
        self.assertIn('test', db_path.lower(), "Should use test database")

        logger.info(f"Database path: {db_path}")

    def test_03_mock_trader_integration(self):
        """Test mock trader integration"""
        logger.info("Testing mock trader integration")

        # Test connection
        self.assertTrue(self.mock_trader.is_connected(), "Mock trader should be connected")

        # Test account query
        account_info = self.mock_trader.query_account()
        self.assertIsInstance(account_info, dict, "Account info should be a dict")
        self.assertIn('total_asset', account_info, "Should have total_asset")

        logger.info(f"Mock trader account: {account_info}")

    def test_04_simulated_buy_workflow(self):
        """Test complete simulated buy workflow"""
        logger.info("Testing simulated buy workflow")

        # Add initial balance
        initial_balance = self.mock_trader.account_balance
        logger.info(f"Initial balance: {initial_balance}")

        # Execute buy order
        stock_code = '000001.SZ'
        price = 10.0
        volume = 1000

        order_id = self.mock_trader.order_stock(
            stock_code=stock_code,
            order_type=23,  # Buy
            price=price,
            volume=volume
        )

        self.assertIsNotNone(order_id, "Order should return ID")
        logger.info(f"Buy order executed: {order_id}")

        # Verify position created
        position = self.mock_trader.query_position(stock_code)
        self.assertEqual(position['volume'], volume, "Position volume should match")
        self.assertEqual(position['cost_price'], price, "Cost price should match")

        # Verify balance deducted
        new_balance = self.mock_trader.account_balance
        expected_balance = initial_balance - (price * volume)
        self.assertAlmostEqual(new_balance, expected_balance, places=2, msg="Balance should be deducted")

        logger.info(f"New balance: {new_balance}")

    def test_05_simulated_sell_workflow(self):
        """Test complete simulated sell workflow"""
        logger.info("Testing simulated sell workflow")

        # First buy
        stock_code = '000001.SZ'
        buy_price = 10.0
        volume = 1000

        self.mock_trader.order_stock(
            stock_code=stock_code,
            order_type=23,  # Buy
            price=buy_price,
            volume=volume
        )

        balance_after_buy = self.mock_trader.account_balance

        # Then sell
        sell_price = 11.0
        sell_volume = 500

        order_id = self.mock_trader.order_stock(
            stock_code=stock_code,
            order_type=24,  # Sell
            price=sell_price,
            volume=sell_volume
        )

        self.assertIsNotNone(order_id, "Sell order should return ID")
        logger.info(f"Sell order executed: {order_id}")

        # Verify position updated
        position = self.mock_trader.query_position(stock_code)
        expected_remaining = volume - sell_volume
        self.assertEqual(position['volume'], expected_remaining, "Remaining volume should be correct")

        # Verify balance increased
        new_balance = self.mock_trader.account_balance
        expected_balance = balance_after_buy + (sell_price * sell_volume)
        self.assertAlmostEqual(new_balance, expected_balance, places=2, msg="Balance should increase")

        logger.info(f"Balance after sell: {new_balance}")

    def test_06_price_update_workflow(self):
        """Test price update and profit calculation workflow"""
        logger.info("Testing price update workflow")

        # Create position
        stock_code = '000001.SZ'
        cost_price = 10.0
        volume = 1000

        self.mock_trader.add_mock_position(stock_code, volume, cost_price, cost_price)

        # Update price
        new_price = 11.0
        self.mock_trader.update_mock_price(stock_code, new_price)

        # Verify price updated
        position = self.mock_trader.query_position(stock_code)
        self.assertEqual(position['current_price'], new_price, "Price should be updated")

        # Calculate expected market value
        expected_market_value = new_price * volume
        self.assertEqual(position['market_value'], expected_market_value, "Market value should be updated")

        logger.info(f"Price updated: {cost_price} -> {new_price}")

    def test_07_multiple_positions_workflow(self):
        """Test managing multiple positions"""
        logger.info("Testing multiple positions workflow")

        # Create multiple positions
        positions_to_create = [
            ('000001.SZ', 1000, 10.0),
            ('000002.SZ', 500, 20.0),
            ('600036.SH', 2000, 5.0)
        ]

        for stock_code, volume, price in positions_to_create:
            self.mock_trader.add_mock_position(stock_code, volume, price, price)

        # Query all positions
        all_positions = self.mock_trader.query_position()

        self.assertEqual(len(all_positions), 3, "Should have 3 positions")

        # Verify each position
        for stock_code, volume, price in positions_to_create:
            position = self.mock_trader.query_position(stock_code)
            self.assertEqual(position['volume'], volume)
            self.assertEqual(position['cost_price'], price)

        logger.info(f"Multiple positions managed: {len(all_positions)}")

    def test_08_order_history_tracking(self):
        """Test order history tracking"""
        logger.info("Testing order history tracking")

        # Execute multiple orders
        orders = []

        # Buy order 1
        order_id = self.mock_trader.order_stock('000001.SZ', 23, 10.0, 1000)
        orders.append(order_id)

        # Buy order 2
        order_id = self.mock_trader.order_stock('000002.SZ', 23, 20.0, 500)
        orders.append(order_id)

        # Sell order
        order_id = self.mock_trader.order_stock('000001.SZ', 24, 11.0, 500)
        orders.append(order_id)

        # Verify all orders are tracked
        for order_id in orders:
            order_info = self.mock_trader.query_order(order_id)
            self.assertIsNotNone(order_info, f"Order {order_id} should be tracked")
            self.assertEqual(order_info.get('status'), 'filled', "Order should be filled")

        logger.info(f"Order history tracked: {len(orders)} orders")

    def test_09_account_state_consistency(self):
        """Test account state consistency across operations"""
        logger.info("Testing account state consistency")

        initial_balance = self.mock_trader.account_balance

        # Execute a series of operations
        operations = [
            ('000001.SZ', 23, 10.0, 1000),  # Buy
            ('000002.SZ', 23, 20.0, 500),   # Buy
            ('000001.SZ', 24, 11.0, 500),   # Sell
        ]

        expected_balance = initial_balance

        for stock_code, order_type, price, volume in operations:
            self.mock_trader.order_stock(stock_code, order_type, price, volume)

            if order_type == 23:  # Buy
                expected_balance -= price * volume
            else:  # Sell
                expected_balance += price * volume

        # Verify final balance
        final_balance = self.mock_trader.account_balance
        self.assertAlmostEqual(
            final_balance,
            expected_balance,
            places=2,
            msg="Account balance should be consistent"
        )

        logger.info(f"Account state consistent: {initial_balance} -> {final_balance}")

    def test_10_system_cleanup(self):
        """Test system cleanup and reset"""
        logger.info("Testing system cleanup")

        # Create some positions
        self.mock_trader.add_mock_position('000001.SZ', 1000, 10.0, 10.0)
        self.mock_trader.add_mock_position('000002.SZ', 500, 20.0, 20.0)

        # Verify positions exist
        positions = self.mock_trader.query_position()
        self.assertGreater(len(positions), 0, "Should have positions")

        # Clear positions
        self.mock_trader.clear_positions()

        # Verify positions cleared
        positions_after = self.mock_trader.query_position()
        self.assertEqual(len(positions_after), 0, "Positions should be cleared")

        # Reset trader
        self.mock_trader.reset()

        # Verify reset
        balance = self.mock_trader.account_balance
        self.assertEqual(balance, 100000.0, "Balance should be reset to initial")

        logger.info("System cleanup and reset successful")


def run_tests():
    """Run system integration tests"""
    suite = unittest.TestLoader().loadTestsFromTestCase(TestSystemIntegration)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
