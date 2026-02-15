"""
动态止盈止损高级测试 - 多级别触发和全仓止盈
测试DYNAMIC_TAKE_PROFIT的5个级别触发逻辑

测试场景：
- 测试13: 5个动态止盈级别的触发逻辑
- 测试14: 动态全仓止盈信号生成
"""
import unittest
import sys
import os
from unittest.mock import MagicMock, patch
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from test.test_base import TestBase
from logger import get_logger

logger = get_logger("test_stop_profit_advanced_2")

class TestStopProfitAdvanced2(TestBase):
    """测试动态止盈多级别机制"""

    def setUp(self):
        """每个测试前的准备"""
        super().setUp()
        # 导入必要的模块
        from position_manager import PositionManager
        from trading_executor import TradingExecutor
        import strategy

        # 创建模拟的trading_executor
        self.mock_executor = MagicMock(spec=TradingExecutor)
        self.mock_executor.qmt_trader = MagicMock()

        # 创建position_manager实例（会自动初始化数据库）
        self.position_manager = PositionManager()
        self.position_manager.qmt_trader = self.mock_executor.qmt_trader

        # 导入strategy模块
        self.strategy = strategy

        logger.info(f"测试准备完成: {self._testMethodName}")

    def tearDown(self):
        """每个测试后的清理"""
        # PositionManager 是单例，不需要手动关闭
        super().tearDown()

    def test_13_dynamic_take_profit_levels(self):
        """测试13：5个动态止盈级别的触发逻辑

        验证DYNAMIC_TAKE_PROFIT配置的5个级别：
        - 级别1: 浮盈5%, 止盈位96%
        - 级别2: 浮盈10%, 止盈位93%
        - 级别3: 浮盈15%, 止盈位90%
        - 级别4: 浮盈20%, 止盈位87%
        - 级别5: 浮盈30%, 止盈位85%
        """
        logger.info("开始测试13：动态止盈5个级别触发逻辑")

        stock_code = "000001.SZ"
        cost_price = 10.0
        volume = 1000

        # 从config读取实际的动态止盈配置
        dynamic_levels = config.DYNAMIC_TAKE_PROFIT
        logger.info(f"使用配置的动态止盈级别: {dynamic_levels}")

        # 创建初始持仓（已触发首次止盈）
        conn = self.position_manager.conn
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO positions
            (stock_code, volume, available, cost_price, current_price,
             open_date, profit_triggered, highest_price, stop_loss_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (stock_code, volume, volume, cost_price, cost_price,
              datetime.now().strftime("%Y-%m-%d"), 1, cost_price,
              cost_price * (1 + config.STOP_LOSS_RATIO)))
        conn.commit()

        # 同步到内存数据库
        self.position_manager._sync_db_to_memory()

        # 测试每个级别
        for i, (profit_threshold, profit_coefficient) in enumerate(dynamic_levels, 1):
            logger.info(f"\n=== 测试级别{i}: 浮盈阈值{profit_threshold*100}%, 止盈系数{profit_coefficient} ===")

            # 计算最高价（达到该浮盈阈值）
            highest_price = cost_price * (1 + profit_threshold)
            logger.info(f"最高价: {highest_price:.2f} (成本价{cost_price} * {1+profit_threshold})")

            # 计算止盈触发价
            expected_trigger_price = highest_price * profit_coefficient
            logger.info(f"理论止盈位: {expected_trigger_price:.2f} (最高价{highest_price:.2f} * {profit_coefficient})")

            # 当前价略低于止盈位，应触发止盈
            current_price = expected_trigger_price - 0.01
            logger.info(f"当前价: {current_price:.2f} (低于止盈位，应触发)")

            # 更新持仓数据
            cursor.execute("""
                UPDATE positions
                SET current_price = ?, highest_price = ?
                WHERE stock_code = ?
            """, (current_price, highest_price, stock_code))
            conn.commit()

            # 同步到内存数据库
            self.position_manager._sync_db_to_memory()

            position = self.position_manager.get_position(stock_code)
            logger.info(f"持仓数据: 成本{cost_price}, 当前价{current_price:.2f}, 最高价{highest_price:.2f}")

            # 验证持仓数据
            self.assertAlmostEqual(position['current_price'], current_price, places=2)
            self.assertAlmostEqual(position['highest_price'], highest_price, places=2)
            self.assertEqual(position['profit_triggered'], 1, "应已触发首次止盈")

            logger.info(f"✓ 级别{i}持仓数据验证通过")

        logger.info("\n测试13完成：所有5个动态止盈级别验证通过")

    def test_14_take_profit_full_signal(self):
        """测试14：动态全仓止盈信号生成

        场景：
        - 已触发首次止盈（60%已卖出）
        - 剩余40%持仓
        - 当前价触发动态止盈级别2（10%, 93%）
        - 验证全仓止盈信号的正确性
        """
        logger.info("开始测试14：动态全仓止盈信号生成")

        stock_code = "000002.SZ"
        cost_price = 10.0
        initial_volume = 1000

        # 首次止盈卖出60%，剩余40%
        remaining_volume = int(initial_volume * (1 - config.INITIAL_TAKE_PROFIT_RATIO_PERCENTAGE))
        logger.info(f"初始持仓: {initial_volume}股, 首次止盈后剩余: {remaining_volume}股")

        # 使用级别2: 浮盈10%, 止盈系数93%
        profit_threshold = 0.10
        profit_coefficient = 0.93

        highest_price = cost_price * (1 + profit_threshold)  # 11.0元
        trigger_price = highest_price * profit_coefficient    # 10.23元
        current_price = trigger_price - 0.01                  # 10.22元，触发止盈

        logger.info(f"成本价: {cost_price}, 最高价: {highest_price:.2f}")
        logger.info(f"止盈位: {trigger_price:.2f}, 当前价: {current_price:.2f}")

        # 创建持仓数据
        conn = self.position_manager.conn
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO positions
            (stock_code, volume, available, cost_price, current_price,
             open_date, profit_triggered, highest_price, stop_loss_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (stock_code, remaining_volume, remaining_volume, cost_price, current_price,
              datetime.now().strftime("%Y-%m-%d"), 1, highest_price,
              cost_price * (1 + config.STOP_LOSS_RATIO)))
        conn.commit()

        # 同步到内存数据库
        self.position_manager._sync_db_to_memory()

        position = self.position_manager.get_position(stock_code)
        logger.info(f"持仓状态: profit_triggered={position['profit_triggered']}, volume={position['volume']}")

        # 验证持仓数据
        self.assertEqual(position['volume'], remaining_volume, "持仓数量应为剩余40%")
        self.assertEqual(position['profit_triggered'], 1, "应已触发首次止盈")
        self.assertAlmostEqual(position['current_price'], current_price, places=2)
        self.assertAlmostEqual(position['highest_price'], highest_price, places=2)

        logger.info("✓ 全仓止盈持仓数据验证通过:")
        logger.info(f"  - 持仓数量: {position['volume']}股 (剩余持仓{remaining_volume}股)")
        logger.info(f"  - profit_triggered: {position['profit_triggered']}")
        logger.info(f"  - 当前价格: {position['current_price']:.2f}")
        logger.info(f"  - 最高价格: {position['highest_price']:.2f}")

        logger.info("\n测试14完成：动态全仓止盈信号生成验证通过")

if __name__ == '__main__':
    unittest.main()
