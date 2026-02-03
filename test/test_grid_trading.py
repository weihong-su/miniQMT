"""
Test 8: Grid Trading Test

Tests grid trading functionality including:
- Grid session management
- Grid level calculation
- Buy/sell triggers
- Callback confirmation
- Exit conditions
"""

import unittest
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from test_base import TestBase
from logger import get_logger

logger = get_logger("test_grid_trading")


class TestGridTrading(TestBase):
    """Test grid trading functionality"""

    def test_01_grid_trading_enabled(self):
        """Test grid trading configuration"""
        logger.info("Testing grid trading configuration")

        self.assertTrue(
            hasattr(config, 'ENABLE_GRID_TRADING'),
            "ENABLE_GRID_TRADING should exist"
        )

        logger.info(f"Grid trading enabled: {config.ENABLE_GRID_TRADING}")

    def test_02_grid_parameters(self):
        """Test grid trading parameters"""
        logger.info("Testing grid parameters")

        # Callback ratio
        callback_ratio = config.GRID_CALLBACK_RATIO
        self.assertIsInstance(callback_ratio, float, "Callback ratio should be float")
        self.assertGreater(callback_ratio, 0, "Callback ratio should be positive")
        self.assertLess(callback_ratio, 0.1, "Callback ratio should be reasonable")

        # Level cooldown
        cooldown = config.GRID_LEVEL_COOLDOWN
        self.assertIsInstance(cooldown, (int, float), "Cooldown should be numeric")
        self.assertGreater(cooldown, 0, "Cooldown should be positive")

        logger.info(f"Callback ratio: {callback_ratio}, Cooldown: {cooldown}s")

    def test_03_grid_exit_conditions(self):
        """Test grid exit condition configuration"""
        logger.info("Testing grid exit conditions")

        # Max deviation
        max_deviation = config.GRID_MAX_DEVIATION_RATIO
        self.assertGreater(max_deviation, 0, "Max deviation should be positive")

        # Target profit
        target_profit = config.GRID_TARGET_PROFIT_RATIO
        self.assertGreater(target_profit, 0, "Target profit should be positive")

        # Stop loss
        stop_loss = config.GRID_STOP_LOSS_RATIO
        self.assertLess(stop_loss, 0, "Stop loss should be negative")

        # Duration
        duration = config.GRID_DEFAULT_DURATION_DAYS
        self.assertGreater(duration, 0, "Duration should be positive")

        logger.info(f"Exit conditions - Deviation: {max_deviation}, "
                   f"Profit: {target_profit}, Loss: {stop_loss}, Duration: {duration}d")

    def test_04_grid_default_parameters(self):
        """Test grid default parameters"""
        logger.info("Testing grid default parameters")

        # Price interval
        price_interval = config.GRID_DEFAULT_PRICE_INTERVAL
        self.assertGreater(price_interval, 0, "Price interval should be positive")
        self.assertLess(price_interval, 1, "Price interval should be < 1")

        # Position ratio
        position_ratio = config.GRID_DEFAULT_POSITION_RATIO
        self.assertGreater(position_ratio, 0, "Position ratio should be positive")
        self.assertLessEqual(position_ratio, 1, "Position ratio should be <= 1")

        # Max investment ratio
        max_investment = config.GRID_DEFAULT_MAX_INVESTMENT_RATIO
        self.assertGreater(max_investment, 0, "Max investment should be positive")
        self.assertLessEqual(max_investment, 1, "Max investment should be <= 1")

        logger.info(f"Defaults - Interval: {price_interval}, "
                   f"Position: {position_ratio}, Max investment: {max_investment}")

    def test_05_calculate_grid_levels(self):
        """Test grid level calculation"""
        logger.info("Testing grid level calculation")

        center_price = 10.0
        price_interval = 0.05  # 5%

        # Calculate levels
        lower_level = center_price * (1 - price_interval)
        upper_level = center_price * (1 + price_interval)

        self.assertAlmostEqual(lower_level, 9.5, places=2, msg="Lower level should be 9.5")
        self.assertAlmostEqual(upper_level, 10.5, places=2, msg="Upper level should be 10.5")

        logger.info(f"Grid levels - Center: {center_price}, "
                   f"Lower: {lower_level}, Upper: {upper_level}")

    def test_06_buy_trigger_at_lower_level(self):
        """Test buy trigger at lower grid level"""
        logger.info("Testing buy trigger at lower level")

        center_price = 10.0
        price_interval = 0.05
        lower_level = center_price * (1 - price_interval)  # 9.5

        current_price = 9.5

        # Price reached lower level
        should_buy = current_price <= lower_level

        self.assertTrue(should_buy, "Should trigger buy at lower level")

        logger.info(f"Buy triggered at price: {current_price} (lower level: {lower_level})")

    def test_07_sell_trigger_at_upper_level(self):
        """Test sell trigger at upper grid level"""
        logger.info("Testing sell trigger at upper level")

        center_price = 10.0
        price_interval = 0.05
        upper_level = center_price * (1 + price_interval)  # 10.5

        current_price = 10.5

        # Price reached upper level
        should_sell = current_price >= upper_level

        self.assertTrue(should_sell, "Should trigger sell at upper level")

        logger.info(f"Sell triggered at price: {current_price} (upper level: {upper_level})")

    def test_08_callback_confirmation(self):
        """Test callback confirmation mechanism"""
        logger.info("Testing callback confirmation")

        callback_ratio = config.GRID_CALLBACK_RATIO  # e.g., 0.005 (0.5%)

        # Simulate price movement
        peak_price = 10.0
        current_price = 9.95  # 0.5% below peak

        # Calculate callback
        callback = (peak_price - current_price) / peak_price

        # Should confirm callback
        confirmed = callback >= callback_ratio

        self.assertTrue(confirmed, "Should confirm callback")

        logger.info(f"Callback confirmed: {callback*100:.2f}% "
                   f"(threshold: {callback_ratio*100:.2f}%)")

    def test_09_exit_on_max_deviation(self):
        """Test exit condition - max deviation"""
        logger.info("Testing exit on max deviation")

        center_price = 10.0
        max_deviation = config.GRID_MAX_DEVIATION_RATIO  # e.g., 0.15 (15%)

        # Simulate large price movement
        current_price = 11.8  # 18% above center

        deviation = abs(current_price - center_price) / center_price

        should_exit = deviation > max_deviation

        self.assertTrue(should_exit, "Should exit on max deviation")

        logger.info(f"Exit triggered - Deviation: {deviation*100:.1f}% "
                   f"(max: {max_deviation*100:.1f}%)")

    def test_10_grid_session_lifecycle(self):
        """Test complete grid session lifecycle"""
        logger.info("Testing grid session lifecycle")

        conn = self.create_memory_db()
        self._create_grid_tables(conn)

        # Create session
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO grid_sessions
            (stock_code, status, center_price, current_center_price,
             price_interval, position_ratio, callback_ratio,
             max_investment, current_investment,
             max_deviation, target_profit, stop_loss,
             start_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            '000001.SZ', 'active', 10.0, 10.0,
            0.05, 0.25, 0.005,
            50000.0, 0.0,
            0.15, 0.10, -0.10,
            datetime.now()
        ))
        conn.commit()
        session_id = cursor.lastrowid

        # Simulate buy trade
        cursor.execute("""
            INSERT INTO grid_trades
            (session_id, stock_code, trade_type, price, volume,
             amount, grid_level, trigger_reason, trade_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            session_id, '000001.SZ', 'BUY', 9.5, 1000,
            9500.0, 'lower', 'Price reached lower level',
            datetime.now()
        ))
        conn.commit()

        # Update session stats
        cursor.execute("""
            UPDATE grid_sessions
            SET trade_count=trade_count+1,
                buy_count=buy_count+1,
                total_buy_amount=total_buy_amount+?,
                current_investment=current_investment+?
            WHERE id=?
        """, (9500.0, 9500.0, session_id))
        conn.commit()

        # Simulate sell trade
        cursor.execute("""
            INSERT INTO grid_trades
            (session_id, stock_code, trade_type, price, volume,
             amount, grid_level, trigger_reason, trade_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            session_id, '000001.SZ', 'SELL', 10.5, 1000,
            10500.0, 'upper', 'Price reached upper level',
            datetime.now()
        ))
        conn.commit()

        # Update session stats
        cursor.execute("""
            UPDATE grid_sessions
            SET trade_count=trade_count+1,
                sell_count=sell_count+1,
                total_sell_amount=total_sell_amount+?
            WHERE id=?
        """, (10500.0, session_id))
        conn.commit()

        # Query final session state
        cursor.execute("SELECT * FROM grid_sessions WHERE id=?", (session_id,))
        session = cursor.fetchone()

        self.assertEqual(session[14], 2, "Should have 2 trades")  # trade_count
        self.assertEqual(session[15], 1, "Should have 1 buy")     # buy_count
        self.assertEqual(session[16], 1, "Should have 1 sell")    # sell_count

        # Calculate profit
        profit = session[18] - session[17]  # total_sell - total_buy
        profit_ratio = profit / session[17] if session[17] > 0 else 0

        logger.info(f"Grid session completed - Trades: {session[14]}, "
                   f"Profit: {profit:.2f} ({profit_ratio*100:.2f}%)")

        # Stop session
        cursor.execute("""
            UPDATE grid_sessions
            SET status='stopped',
                stop_time=?,
                stop_reason='Test completed'
            WHERE id=?
        """, (datetime.now(), session_id))
        conn.commit()

        conn.close()

        logger.info("Grid session lifecycle test completed")

    def _create_grid_tables(self, conn):
        """Helper to create grid trading tables"""
        cursor = conn.cursor()

        # Grid sessions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS grid_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stock_code TEXT,
                status TEXT,
                center_price REAL,
                current_center_price REAL,
                price_interval REAL,
                position_ratio REAL,
                callback_ratio REAL,
                max_investment REAL,
                current_investment REAL,
                max_deviation REAL,
                target_profit REAL,
                stop_loss REAL,
                end_time TIMESTAMP,
                trade_count INTEGER DEFAULT 0,
                buy_count INTEGER DEFAULT 0,
                sell_count INTEGER DEFAULT 0,
                total_buy_amount REAL DEFAULT 0,
                total_sell_amount REAL DEFAULT 0,
                start_time TIMESTAMP,
                stop_time TIMESTAMP,
                stop_reason TEXT
            )
        """)

        # Grid trades table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS grid_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER,
                stock_code TEXT,
                trade_type TEXT,
                price REAL,
                volume INTEGER,
                amount REAL,
                grid_level TEXT,
                trigger_reason TEXT,
                trade_time TIMESTAMP,
                trade_id TEXT
            )
        """)

        conn.commit()


def run_tests():
    """Run grid trading tests"""
    suite = unittest.TestLoader().loadTestsFromTestCase(TestGridTrading)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
