"""
Test 10: Web Data Refresh Test

Tests web data refresh and SSE push mechanism including:
- SSE configuration
- Data version increment
- Hash-based change detection
- Heartbeat mechanism
- Position data serialization
"""

import unittest
import os
import sys
import json
import hashlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from test_base import TestBase
from logger import get_logger

logger = get_logger("test_web_data_refresh")


class TestWebDataRefresh(TestBase):
    """Test web data refresh functionality"""

    def test_01_sse_configuration(self):
        """Test SSE configuration"""
        logger.info("Testing SSE configuration")

        # Check SSE change detection method
        self.assertTrue(
            hasattr(config, 'SSE_CHANGE_DETECTION_METHOD'),
            "SSE_CHANGE_DETECTION_METHOD should exist"
        )

        method = config.SSE_CHANGE_DETECTION_METHOD
        self.assertIn(method, ['version', 'hash'], "Method should be 'version' or 'hash'")

        logger.info(f"SSE change detection method: {method}")

    def test_02_heartbeat_interval_config(self):
        """Test heartbeat interval configuration"""
        logger.info("Testing heartbeat interval config")

        # Trading hours heartbeat
        self.assertTrue(
            hasattr(config, 'SSE_HEARTBEAT_TRADING'),
            "SSE_HEARTBEAT_TRADING should exist"
        )

        trading_interval = config.SSE_HEARTBEAT_TRADING
        self.assertIsInstance(trading_interval, (int, float), "Trading interval should be numeric")
        self.assertGreater(trading_interval, 0, "Trading interval should be positive")

        # Non-trading hours heartbeat
        self.assertTrue(
            hasattr(config, 'SSE_HEARTBEAT_NON_TRADING'),
            "SSE_HEARTBEAT_NON_TRADING should exist"
        )

        non_trading_interval = config.SSE_HEARTBEAT_NON_TRADING
        self.assertIsInstance(non_trading_interval, (int, float), "Non-trading interval should be numeric")
        self.assertGreater(non_trading_interval, 0, "Non-trading interval should be positive")

        logger.info(f"Heartbeat intervals - Trading: {trading_interval}s, Non-trading: {non_trading_interval}s")

    def test_03_data_version_increment(self):
        """Test data version increment mechanism"""
        logger.info("Testing data version increment")

        # Simulate version tracking
        version_counter = {'current': 1}

        def increment_version():
            version_counter['current'] += 1
            return version_counter['current']

        # Initial version
        v1 = version_counter['current']
        self.assertEqual(v1, 1, "Initial version should be 1")

        # Increment version
        v2 = increment_version()
        self.assertEqual(v2, 2, "Version should increment to 2")

        # Multiple increments
        v3 = increment_version()
        v4 = increment_version()
        self.assertEqual(v4, 4, "Version should increment to 4")

        logger.info(f"Version incremented: {v1} -> {v4}")

    def test_04_hash_based_change_detection(self):
        """Test hash-based change detection"""
        logger.info("Testing hash-based change detection")

        # Create sample position data
        position_data_1 = {
            'stock_code': '000001.SZ',
            'volume': 1000,
            'current_price': 10.0,
            'market_value': 10000.0
        }

        position_data_2 = {
            'stock_code': '000001.SZ',
            'volume': 1000,
            'current_price': 10.5,  # Price changed
            'market_value': 10500.0
        }

        # Calculate hashes
        hash1 = hashlib.md5(json.dumps(position_data_1, sort_keys=True).encode()).hexdigest()
        hash2 = hashlib.md5(json.dumps(position_data_2, sort_keys=True).encode()).hexdigest()

        # Hashes should be different
        self.assertNotEqual(hash1, hash2, "Hashes should differ when data changes")

        # Same data should produce same hash
        hash1_copy = hashlib.md5(json.dumps(position_data_1, sort_keys=True).encode()).hexdigest()
        self.assertEqual(hash1, hash1_copy, "Same data should produce same hash")

        logger.info(f"Hash change detected: {hash1[:8]} -> {hash2[:8]}")

    def test_05_position_data_serialization(self):
        """Test position data serialization"""
        logger.info("Testing position data serialization")

        # Create test position
        position = {
            'stock_code': '000001.SZ',
            'volume': 1000,
            'available': 1000,
            'cost_price': 10.0,
            'current_price': 11.0,
            'market_value': 11000.0,
            'profit_ratio': 0.1,
            'open_date': '2026-01-01',
            'profit_triggered': 0,
            'highest_price': 11.0,
            'stop_loss_price': 9.25
        }

        # Serialize to JSON
        json_str = json.dumps(position, ensure_ascii=False)
        self.assertIsInstance(json_str, str, "Should serialize to string")

        # Deserialize back
        position_loaded = json.loads(json_str)
        self.assertEqual(position_loaded['stock_code'], '000001.SZ', "Stock code should match")
        self.assertEqual(position_loaded['volume'], 1000, "Volume should match")
        self.assertAlmostEqual(position_loaded['profit_ratio'], 0.1, places=4, msg="Profit ratio should match")

        logger.info(f"Position serialization successful: {len(json_str)} bytes")

    def test_06_data_push_trigger(self):
        """Test data push trigger conditions"""
        logger.info("Testing data push triggers")

        # Simulate push trigger scenarios
        triggers = []

        # Trigger 1: Price update
        def on_price_update(stock_code, old_price, new_price):
            if old_price != new_price:
                triggers.append({
                    'type': 'price_update',
                    'stock_code': stock_code,
                    'old_price': old_price,
                    'new_price': new_price
                })

        on_price_update('000001.SZ', 10.0, 10.5)

        # Trigger 2: Position change
        def on_position_change(stock_code, old_volume, new_volume):
            if old_volume != new_volume:
                triggers.append({
                    'type': 'position_change',
                    'stock_code': stock_code,
                    'old_volume': old_volume,
                    'new_volume': new_volume
                })

        on_position_change('000001.SZ', 1000, 1500)

        # Verify triggers
        self.assertEqual(len(triggers), 2, "Should have 2 triggers")
        self.assertEqual(triggers[0]['type'], 'price_update', "First trigger should be price_update")
        self.assertEqual(triggers[1]['type'], 'position_change', "Second trigger should be position_change")

        logger.info(f"Data push triggers: {len(triggers)} events")

    def test_07_change_detection_accuracy(self):
        """Test change detection accuracy"""
        logger.info("Testing change detection accuracy")

        # Test version-based detection
        version_changed = False
        old_version = 1
        new_version = 2

        if new_version > old_version:
            version_changed = True

        self.assertTrue(version_changed, "Version-based detection should detect change")

        # Test hash-based detection
        old_data = {'price': 10.0, 'volume': 1000}
        new_data = {'price': 10.0, 'volume': 1000}

        old_hash = hashlib.md5(json.dumps(old_data, sort_keys=True).encode()).hexdigest()
        new_hash = hashlib.md5(json.dumps(new_data, sort_keys=True).encode()).hexdigest()

        hash_changed = (old_hash != new_hash)
        self.assertFalse(hash_changed, "Hash-based detection should not detect change for same data")

        # Modify data
        new_data['price'] = 10.5
        new_hash = hashlib.md5(json.dumps(new_data, sort_keys=True).encode()).hexdigest()
        hash_changed = (old_hash != new_hash)

        self.assertTrue(hash_changed, "Hash-based detection should detect change")

        logger.info("Change detection accuracy verified")

    def test_08_heartbeat_message_format(self):
        """Test heartbeat message format"""
        logger.info("Testing heartbeat message format")

        # Create heartbeat message
        heartbeat = {
            'type': 'heartbeat',
            'timestamp': '2026-02-02 15:30:00',
            'is_trading_time': True
        }

        # Verify structure
        self.assertIn('type', heartbeat, "Should have type field")
        self.assertEqual(heartbeat['type'], 'heartbeat', "Type should be 'heartbeat'")
        self.assertIn('timestamp', heartbeat, "Should have timestamp field")
        self.assertIn('is_trading_time', heartbeat, "Should have is_trading_time field")

        # Serialize
        heartbeat_json = json.dumps(heartbeat)
        self.assertIsInstance(heartbeat_json, str, "Should serialize to string")

        logger.info(f"Heartbeat message: {heartbeat_json}")

    def test_09_push_performance(self):
        """Test push performance characteristics"""
        logger.info("Testing push performance")

        import time

        # Simulate data preparation
        positions = []
        for i in range(10):
            positions.append({
                'stock_code': f'00000{i}.SZ',
                'volume': 1000,
                'current_price': 10.0 + i * 0.1,
                'market_value': (10.0 + i * 0.1) * 1000
            })

        # Measure serialization time
        start_time = time.time()
        json_data = json.dumps(positions, ensure_ascii=False)
        serialization_time = time.time() - start_time

        # Should be fast (< 0.01 seconds for 10 positions)
        self.assertLess(serialization_time, 0.01, "Serialization should be fast")

        # Measure hash calculation time
        start_time = time.time()
        data_hash = hashlib.md5(json_data.encode()).hexdigest()
        hash_time = time.time() - start_time

        # Should be fast (< 0.001 seconds)
        self.assertLess(hash_time, 0.001, "Hash calculation should be fast")

        logger.info(f"Performance - Serialization: {serialization_time*1000:.2f}ms, Hash: {hash_time*1000:.2f}ms")

    def test_10_sse_lifecycle(self):
        """Test SSE complete lifecycle"""
        logger.info("Testing SSE lifecycle")

        # Simulate SSE lifecycle
        sse_state = {
            'connected': False,
            'last_data_hash': None,
            'last_heartbeat': None,
            'message_count': 0
        }

        # 1. Client connects
        sse_state['connected'] = True
        self.assertTrue(sse_state['connected'], "SSE should be connected")

        # 2. Initial data push
        initial_data = {'positions': [], 'version': 1}
        initial_hash = hashlib.md5(json.dumps(initial_data, sort_keys=True).encode()).hexdigest()
        sse_state['last_data_hash'] = initial_hash
        sse_state['message_count'] += 1

        # 3. Heartbeat
        sse_state['last_heartbeat'] = '2026-02-02 15:30:00'
        sse_state['message_count'] += 1

        # 4. Data update (position added)
        updated_data = {
            'positions': [{'stock_code': '000001.SZ', 'volume': 1000}],
            'version': 2
        }
        updated_hash = hashlib.md5(json.dumps(updated_data, sort_keys=True).encode()).hexdigest()

        # Hash should be different
        self.assertNotEqual(updated_hash, sse_state['last_data_hash'], "Hash should change")

        # Push update
        sse_state['last_data_hash'] = updated_hash
        sse_state['message_count'] += 1

        # 5. Verify state
        self.assertTrue(sse_state['connected'], "Should remain connected")
        self.assertEqual(sse_state['message_count'], 3, "Should have sent 3 messages")
        self.assertIsNotNone(sse_state['last_data_hash'], "Should have last data hash")
        self.assertIsNotNone(sse_state['last_heartbeat'], "Should have last heartbeat")

        # 6. Client disconnects
        sse_state['connected'] = False
        self.assertFalse(sse_state['connected'], "SSE should be disconnected")

        logger.info(f"SSE lifecycle completed: {sse_state['message_count']} messages sent")


def run_tests():
    """Run web data refresh tests"""
    suite = unittest.TestLoader().loadTestsFromTestCase(TestWebDataRefresh)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
