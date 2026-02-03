"""
Test Mock Objects - Mock QMT trader and external dependencies

Provides mock implementations when QMT is not available
"""

import os
import sys
import time
from datetime import datetime
from unittest.mock import MagicMock

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from logger import get_logger

logger = get_logger("test_mocks")


class MockQmtTrader:
    """
    Mock QMT Trader for testing without actual QMT connection

    Simulates key methods of EasyQmtTrader
    """

    def __init__(self):
        self.connected = False
        self.positions = {}
        self.orders = {}
        self.account_balance = 100000.0
        self.order_counter = 1

        logger.info("MockQmtTrader initialized")

    def connect(self):
        """Simulate connection to QMT"""
        self.connected = True
        logger.info("MockQmtTrader connected")
        return True

    def disconnect(self):
        """Simulate disconnection from QMT"""
        self.connected = False
        logger.info("MockQmtTrader disconnected")

    def is_connected(self):
        """Check if connected"""
        return self.connected

    def query_position(self, stock_code=None):
        """
        Simulate position query

        Args:
            stock_code: Optional stock code filter

        Returns:
            list: List of position dicts or single position dict
        """
        if stock_code:
            return self.positions.get(stock_code, {})
        else:
            return list(self.positions.values())

    def query_account(self):
        """
        Simulate account query

        Returns:
            dict: Account info
        """
        return {
            'account_id': 'TEST_ACCOUNT',
            'total_asset': self.account_balance,
            'available_cash': self.account_balance,
            'market_value': sum(p.get('market_value', 0)
                               for p in self.positions.values())
        }

    def order_stock(self, stock_code, order_type, price, volume):
        """
        Simulate stock order

        Args:
            stock_code: Stock code
            order_type: Order type (23=buy, 24=sell)
            price: Order price
            volume: Order volume

        Returns:
            str: Order ID
        """
        if not self.connected:
            raise Exception("Not connected to QMT")

        order_id = f"MOCK_{self.order_counter:06d}"
        self.order_counter += 1

        order = {
            'order_id': order_id,
            'stock_code': stock_code,
            'order_type': order_type,
            'price': price,
            'volume': volume,
            'status': 'filled',
            'filled_volume': volume,
            'filled_price': price,
            'order_time': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        self.orders[order_id] = order

        # Update positions
        if order_type == 23:  # Buy
            if stock_code in self.positions:
                pos = self.positions[stock_code]
                total_cost = pos['cost_price'] * pos['volume'] + price * volume
                new_volume = pos['volume'] + volume
                pos['cost_price'] = total_cost / new_volume
                pos['volume'] = new_volume
                pos['available'] = new_volume
            else:
                self.positions[stock_code] = {
                    'stock_code': stock_code,
                    'volume': volume,
                    'available': volume,
                    'cost_price': price,
                    'current_price': price,
                    'market_value': price * volume
                }

            # Deduct cash
            self.account_balance -= price * volume

        elif order_type == 24:  # Sell
            if stock_code in self.positions:
                pos = self.positions[stock_code]
                pos['volume'] -= volume
                pos['available'] -= volume

                if pos['volume'] <= 0:
                    del self.positions[stock_code]

                # Add cash
                self.account_balance += price * volume

        logger.info(f"Mock order executed: {order_id}")
        return order_id

    def query_order(self, order_id):
        """
        Query order status

        Args:
            order_id: Order ID

        Returns:
            dict: Order info
        """
        return self.orders.get(order_id, {})

    def add_mock_position(self, stock_code, volume, cost_price, current_price=None):
        """
        Add a mock position for testing

        Args:
            stock_code: Stock code
            volume: Volume
            cost_price: Cost price
            current_price: Current price (defaults to cost_price)
        """
        if current_price is None:
            current_price = cost_price

        self.positions[stock_code] = {
            'stock_code': stock_code,
            'volume': volume,
            'available': volume,
            'cost_price': cost_price,
            'current_price': current_price,
            'market_value': current_price * volume
        }

        logger.info(f"Mock position added: {stock_code}")

    def update_mock_price(self, stock_code, new_price):
        """
        Update mock position price

        Args:
            stock_code: Stock code
            new_price: New current price
        """
        if stock_code in self.positions:
            pos = self.positions[stock_code]
            pos['current_price'] = new_price
            pos['market_value'] = new_price * pos['volume']
            logger.debug(f"Mock price updated: {stock_code} -> {new_price}")

    def clear_positions(self):
        """Clear all positions"""
        self.positions.clear()
        logger.info("All mock positions cleared")

    def reset(self):
        """Reset mock trader to initial state"""
        self.positions.clear()
        self.orders.clear()
        self.account_balance = 100000.0
        self.order_counter = 1
        logger.info("MockQmtTrader reset")


def create_mock_qmt_trader():
    """
    Factory function to create MockQmtTrader instance

    Returns:
        MockQmtTrader: Mock trader instance
    """
    return MockQmtTrader()
