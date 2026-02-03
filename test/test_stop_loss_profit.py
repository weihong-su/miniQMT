"""
Test 5: Stop Loss and Take Profit Test

Tests stop loss and take profit logic and signal generation
"""

import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from test_base import TestBase
from test_utils import calculate_profit_ratio, should_trigger_stop_loss, should_trigger_take_profit
from logger import get_logger

logger = get_logger("test_stop_loss_profit")


class TestStopLossProfit(TestBase):
    """Test stop loss and take profit functionality"""

    def test_01_stop_loss_configuration(self):
        """Test stop loss configuration"""
        logger.info("Testing stop loss configuration")

        stop_loss_ratio = config.STOP_LOSS_RATIO

        self.assertIsInstance(stop_loss_ratio, float, "Stop loss ratio should be float")
        self.assertLess(stop_loss_ratio, 0, "Stop loss ratio should be negative")
        self.assertGreaterEqual(stop_loss_ratio, -1.0, "Stop loss ratio should be >= -1.0")

        logger.info(f"Stop loss ratio: {stop_loss_ratio * 100}%")

    def test_02_take_profit_configuration(self):
        """Test take profit configuration"""
        logger.info("Testing take profit configuration")

        take_profit_ratio = config.INITIAL_TAKE_PROFIT_RATIO
        take_profit_percentage = config.INITIAL_TAKE_PROFIT_RATIO_PERCENTAGE

        self.assertIsInstance(take_profit_ratio, float, "Take profit ratio should be float")
        self.assertGreater(take_profit_ratio, 0, "Take profit ratio should be positive")

        self.assertIsInstance(take_profit_percentage, float, "Percentage should be float")
        self.assertGreater(take_profit_percentage, 0, "Percentage should be positive")
        self.assertLessEqual(take_profit_percentage, 1.0, "Percentage should be <= 1.0")

        logger.info(f"Take profit: {take_profit_ratio * 100}% profit, sell {take_profit_percentage * 100}%")

    def test_03_dynamic_take_profit_configuration(self):
        """Test dynamic take profit configuration"""
        logger.info("Testing dynamic take profit configuration")

        dynamic_config = config.DYNAMIC_TAKE_PROFIT

        self.assertIsInstance(dynamic_config, list, "Should be a list")
        self.assertGreater(len(dynamic_config), 0, "Should not be empty")

        # Validate each level
        for i, (profit_ratio, stop_ratio) in enumerate(dynamic_config):
            self.assertGreater(profit_ratio, 0, f"Level {i}: profit ratio should be positive")
            self.assertGreater(stop_ratio, 0, f"Level {i}: stop ratio should be positive")
            self.assertLess(stop_ratio, 1, f"Level {i}: stop ratio should be < 1")

        logger.info(f"Dynamic take profit: {len(dynamic_config)} levels configured")

    def test_04_calculate_profit_ratio(self):
        """Test profit ratio calculation"""
        logger.info("Testing profit ratio calculation")

        # Test profit scenario
        profit_ratio = calculate_profit_ratio(cost_price=10.0, current_price=11.0)
        self.assertAlmostEqual(profit_ratio, 0.1, places=4, msg="Should be 10% profit")

        # Test loss scenario
        loss_ratio = calculate_profit_ratio(cost_price=10.0, current_price=9.0)
        self.assertAlmostEqual(loss_ratio, -0.1, places=4, msg="Should be 10% loss")

        # Test no change
        no_change = calculate_profit_ratio(cost_price=10.0, current_price=10.0)
        self.assertEqual(no_change, 0.0, "Should be 0% change")

        logger.info("Profit ratio calculation tested")

    def test_05_stop_loss_trigger_exact(self):
        """Test stop loss trigger at exact threshold"""
        logger.info("Testing stop loss trigger at exact threshold")

        cost_price = 10.0
        stop_loss_ratio = config.STOP_LOSS_RATIO  # e.g., -0.075 (-7.5%)

        # Calculate exact stop loss price
        stop_loss_price = cost_price * (1 + stop_loss_ratio)  # 10.0 * 0.925 = 9.25

        # Test at exact threshold
        should_trigger = should_trigger_stop_loss(cost_price, stop_loss_price)

        logger.info(f"Cost: {cost_price}, Stop loss price: {stop_loss_price}, Trigger: {should_trigger}")

        # At exact threshold, should trigger
        self.assertTrue(should_trigger, "Should trigger at exact stop loss threshold")

    def test_06_stop_loss_trigger_below(self):
        """Test stop loss trigger below threshold"""
        logger.info("Testing stop loss trigger below threshold")

        cost_price = 10.0
        current_price = 9.0  # 10% below cost

        should_trigger = should_trigger_stop_loss(cost_price, current_price)

        logger.info(f"Cost: {cost_price}, Current: {current_price}, Trigger: {should_trigger}")

        # Below threshold, should trigger
        self.assertTrue(should_trigger, "Should trigger below stop loss threshold")

    def test_07_stop_loss_no_trigger(self):
        """Test stop loss not triggering above threshold"""
        logger.info("Testing stop loss not triggering")

        cost_price = 10.0
        current_price = 9.5  # Only 5% below cost (threshold is -7.5%)

        should_trigger = should_trigger_stop_loss(cost_price, current_price)

        logger.info(f"Cost: {cost_price}, Current: {current_price}, Trigger: {should_trigger}")

        # Above threshold, should not trigger
        self.assertFalse(should_trigger, "Should not trigger above stop loss threshold")

    def test_08_initial_take_profit_trigger(self):
        """Test initial take profit trigger"""
        logger.info("Testing initial take profit trigger")

        cost_price = 10.0
        take_profit_ratio = config.INITIAL_TAKE_PROFIT_RATIO  # e.g., 0.06 (6%)

        # Calculate exact take profit price
        take_profit_price = cost_price * (1 + take_profit_ratio)  # 10.0 * 1.06 = 10.6

        should_trigger = should_trigger_take_profit(cost_price, take_profit_price, profit_triggered=False)

        logger.info(f"Cost: {cost_price}, Take profit price: {take_profit_price}, Trigger: {should_trigger}")

        # Should trigger initial take profit
        self.assertTrue(should_trigger, "Should trigger initial take profit")

    def test_09_take_profit_no_trigger(self):
        """Test take profit not triggering below threshold"""
        logger.info("Testing take profit not triggering")

        cost_price = 10.0
        current_price = 10.3  # Only 3% profit (threshold is 6%)

        should_trigger = should_trigger_take_profit(cost_price, current_price, profit_triggered=False)

        logger.info(f"Cost: {cost_price}, Current: {current_price}, Trigger: {should_trigger}")

        # Below threshold, should not trigger
        self.assertFalse(should_trigger, "Should not trigger below take profit threshold")

    def test_10_position_with_stop_loss_scenario(self):
        """Test complete stop loss scenario with position"""
        logger.info("Testing complete stop loss scenario")

        conn = self.create_memory_db()
        self._create_positions_table(conn)

        # Create position at cost
        position = self.create_test_position(
            conn,
            stock_code='000001.SZ',
            volume=1000,
            cost_price=10.0,
            current_price=10.0
        )

        # Simulate price drop to stop loss level
        stop_loss_ratio = config.STOP_LOSS_RATIO
        stop_loss_price = 10.0 * (1 + stop_loss_ratio)

        # Update current price
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE positions
            SET current_price=?, market_value=?
            WHERE stock_code=?
        """, (stop_loss_price, stop_loss_price * 1000, '000001.SZ'))
        conn.commit()

        # Check if stop loss should trigger
        should_trigger = should_trigger_stop_loss(10.0, stop_loss_price)

        self.assertTrue(should_trigger, "Stop loss should trigger")

        logger.info(f"Stop loss triggered at price: {stop_loss_price}")

        conn.close()

    def _create_positions_table(self, conn):
        """Helper to create positions table"""
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                stock_code TEXT PRIMARY KEY,
                volume INTEGER,
                available INTEGER,
                cost_price REAL,
                current_price REAL,
                market_value REAL,
                profit_ratio REAL,
                open_date TEXT,
                profit_triggered INTEGER DEFAULT 0,
                highest_price REAL,
                stop_loss_price REAL
            )
        """)
        conn.commit()


def run_tests():
    """Run stop loss/profit tests"""
    suite = unittest.TestLoader().loadTestsFromTestCase(TestStopLossProfit)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
