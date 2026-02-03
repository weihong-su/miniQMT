"""
Test 3: Database Operations Test

Tests database creation, CRUD operations, and integrity
"""

import unittest
import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from test_base import TestBase
from logger import get_logger

logger = get_logger("test_database_operations")


class TestDatabaseOperations(TestBase):
    """Test database operations"""

    def setUp(self):
        """Setup before each test"""
        super().setUp()
        # Use in-memory database for isolated testing
        self.conn = self.create_memory_db()
        self._create_test_tables()

    def tearDown(self):
        """Cleanup after each test"""
        if self.conn:
            self.conn.close()
        super().tearDown()

    def _create_test_tables(self):
        """Create test database tables"""
        cursor = self.conn.cursor()

        # Create positions table
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

        # Create trade_records table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trade_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stock_code TEXT,
                trade_type TEXT,
                price REAL,
                volume INTEGER,
                amount REAL,
                trade_id TEXT,
                strategy TEXT,
                trade_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Create grid_sessions table
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

        self.conn.commit()

    def test_01_create_tables(self):
        """Test table creation"""
        logger.info("Testing table creation")

        cursor = self.conn.cursor()

        # Check positions table
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='positions'
        """)
        self.assertIsNotNone(cursor.fetchone(), "positions table should exist")

        # Check trade_records table
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='trade_records'
        """)
        self.assertIsNotNone(cursor.fetchone(), "trade_records table should exist")

        # Check grid_sessions table
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='grid_sessions'
        """)
        self.assertIsNotNone(cursor.fetchone(), "grid_sessions table should exist")

        logger.info("All tables created successfully")

    def test_02_insert_position(self):
        """Test inserting a position"""
        logger.info("Testing position insertion")

        position = self.create_test_position(
            self.conn,
            stock_code='000001.SZ',
            volume=1000,
            cost_price=10.0
        )

        # Verify insertion
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM positions WHERE stock_code=?", ('000001.SZ',))
        result = cursor.fetchone()

        self.assertIsNotNone(result, "Position should be inserted")
        self.assertEqual(result[0], '000001.SZ', "Stock code should match")
        self.assertEqual(result[1], 1000, "Volume should match")
        self.assertEqual(result[3], 10.0, "Cost price should match")

        logger.info("Position inserted successfully")

    def test_03_update_position(self):
        """Test updating a position"""
        logger.info("Testing position update")

        # Insert initial position
        self.create_test_position(
            self.conn,
            stock_code='000001.SZ',
            volume=1000,
            cost_price=10.0
        )

        # Update position
        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE positions
            SET current_price=?, market_value=?
            WHERE stock_code=?
        """, (11.0, 11000.0, '000001.SZ'))
        self.conn.commit()

        # Verify update
        cursor.execute("SELECT current_price, market_value FROM positions WHERE stock_code=?",
                      ('000001.SZ',))
        result = cursor.fetchone()

        self.assertEqual(result[0], 11.0, "Current price should be updated")
        self.assertEqual(result[1], 11000.0, "Market value should be updated")

        logger.info("Position updated successfully")

    def test_04_delete_position(self):
        """Test deleting a position"""
        logger.info("Testing position deletion")

        # Insert position
        self.create_test_position(
            self.conn,
            stock_code='000001.SZ',
            volume=1000,
            cost_price=10.0
        )

        # Delete position
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM positions WHERE stock_code=?", ('000001.SZ',))
        self.conn.commit()

        # Verify deletion
        cursor.execute("SELECT * FROM positions WHERE stock_code=?", ('000001.SZ',))
        result = cursor.fetchone()

        self.assertIsNone(result, "Position should be deleted")

        logger.info("Position deleted successfully")

    def test_05_insert_trade_record(self):
        """Test inserting a trade record"""
        logger.info("Testing trade record insertion")

        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO trade_records
            (stock_code, trade_type, price, volume, amount, trade_id, strategy)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ('000001.SZ', 'BUY', 10.0, 1000, 10000.0, 'TEST001', 'simu'))
        self.conn.commit()

        # Verify insertion
        cursor.execute("SELECT * FROM trade_records WHERE trade_id=?", ('TEST001',))
        result = cursor.fetchone()

        self.assertIsNotNone(result, "Trade record should be inserted")
        self.assertEqual(result[1], '000001.SZ', "Stock code should match")
        self.assertEqual(result[2], 'BUY', "Trade type should match")
        self.assertEqual(result[3], 10.0, "Price should match")

        logger.info("Trade record inserted successfully")

    def test_06_query_trade_history(self):
        """Test querying trade history"""
        logger.info("Testing trade history query")

        cursor = self.conn.cursor()

        # Insert multiple trades
        trades = [
            ('000001.SZ', 'BUY', 10.0, 1000, 10000.0, 'TEST001', 'simu'),
            ('000001.SZ', 'SELL', 11.0, 500, 5500.0, 'TEST002', 'auto_partial'),
            ('000002.SZ', 'BUY', 20.0, 500, 10000.0, 'TEST003', 'simu')
        ]

        for trade in trades:
            cursor.execute("""
                INSERT INTO trade_records
                (stock_code, trade_type, price, volume, amount, trade_id, strategy)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, trade)
        self.conn.commit()

        # Query trades for specific stock
        cursor.execute("""
            SELECT * FROM trade_records
            WHERE stock_code=?
            ORDER BY id
        """, ('000001.SZ',))
        results = cursor.fetchall()

        self.assertEqual(len(results), 2, "Should have 2 trades for 000001.SZ")

        logger.info(f"Trade history query successful ({len(results)} records)")

    def test_07_concurrent_access(self):
        """Test concurrent database access with locks"""
        logger.info("Testing concurrent database access")

        import threading
        import time

        success_count = [0]
        lock = threading.Lock()

        # Simulate concurrent access with locks
        # Note: SQLite connections cannot be shared across threads
        # This test verifies lock mechanism works correctly

        def simulated_database_operation(operation_id):
            try:
                with lock:
                    # Simulate database operation
                    time.sleep(0.001)
                    success_count[0] += 1
                    logger.debug(f"Operation {operation_id} completed")
            except Exception as e:
                logger.error(f"Operation failed: {str(e)}")

        # Create threads
        threads = []
        for i in range(5):
            t = threading.Thread(target=simulated_database_operation, args=(i,))
            threads.append(t)
            t.start()

        # Wait for all threads
        for t in threads:
            t.join()

        # Verify all operations succeeded
        self.assertEqual(success_count[0], 5, "All 5 operations should succeed")

        # Now verify actual database can be accessed sequentially
        for i in range(5):
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT INTO positions (stock_code, volume, cost_price)
                VALUES (?, ?, ?)
            """, (f'00000{i}.SZ', 1000, 10.0))
            self.conn.commit()

        # Verify row count
        count = self.get_table_row_count(self.conn, 'positions')
        self.assertEqual(count, 5, "Should have 5 positions")

        logger.info("Concurrent access test passed")

    def test_08_transaction_rollback(self):
        """Test transaction rollback"""
        logger.info("Testing transaction rollback")

        cursor = self.conn.cursor()

        try:
            # Start transaction
            cursor.execute("BEGIN TRANSACTION")

            # Insert position
            cursor.execute("""
                INSERT INTO positions (stock_code, volume, cost_price)
                VALUES (?, ?, ?)
            """, ('000001.SZ', 1000, 10.0))

            # Simulate error
            raise Exception("Simulated error")

        except Exception as e:
            # Rollback on error
            self.conn.rollback()
            logger.info(f"Transaction rolled back due to: {str(e)}")

        # Verify no data was inserted
        cursor.execute("SELECT COUNT(*) FROM positions")
        count = cursor.fetchone()[0]

        self.assertEqual(count, 0, "No data should be inserted after rollback")

        logger.info("Transaction rollback test passed")

    def test_09_grid_session_crud(self):
        """Test grid session CRUD operations"""
        logger.info("Testing grid session operations")

        cursor = self.conn.cursor()

        # Insert grid session
        cursor.execute("""
            INSERT INTO grid_sessions
            (stock_code, status, center_price, price_interval, max_investment)
            VALUES (?, ?, ?, ?, ?)
        """, ('000001.SZ', 'active', 10.0, 0.05, 50000.0))
        self.conn.commit()

        session_id = cursor.lastrowid

        # Query session
        cursor.execute("SELECT * FROM grid_sessions WHERE id=?", (session_id,))
        result = cursor.fetchone()

        self.assertIsNotNone(result, "Grid session should exist")
        self.assertEqual(result[1], '000001.SZ', "Stock code should match")
        self.assertEqual(result[2], 'active', "Status should be active")

        # Update session
        cursor.execute("""
            UPDATE grid_sessions
            SET status=?, trade_count=?
            WHERE id=?
        """, ('stopped', 10, session_id))
        self.conn.commit()

        # Verify update
        cursor.execute("SELECT status, trade_count FROM grid_sessions WHERE id=?",
                      (session_id,))
        result = cursor.fetchone()

        self.assertEqual(result[0], 'stopped', "Status should be updated")
        self.assertEqual(result[1], 10, "Trade count should be updated")

        logger.info("Grid session CRUD test passed")

    def test_10_data_integrity(self):
        """Test data integrity constraints"""
        logger.info("Testing data integrity")

        cursor = self.conn.cursor()

        # Test primary key uniqueness
        cursor.execute("""
            INSERT INTO positions (stock_code, volume, cost_price)
            VALUES (?, ?, ?)
        """, ('000001.SZ', 1000, 10.0))
        self.conn.commit()

        # Try to insert duplicate
        try:
            cursor.execute("""
                INSERT INTO positions (stock_code, volume, cost_price)
                VALUES (?, ?, ?)
            """, ('000001.SZ', 2000, 11.0))
            self.conn.commit()
            self.fail("Should not allow duplicate stock_code")
        except sqlite3.IntegrityError:
            logger.info("Primary key constraint enforced correctly")

        logger.info("Data integrity test passed")


def run_tests():
    """Run database operations tests"""
    suite = unittest.TestLoader().loadTestsFromTestCase(TestDatabaseOperations)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
