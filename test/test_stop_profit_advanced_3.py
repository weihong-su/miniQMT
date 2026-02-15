"""
动态止盈止损高级测试 - 止损补仓功能
测试ENABLE_STOP_LOSS_BUY配置和止损补仓逻辑

Worker 3/3 - 止损补仓测试
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

logger = get_logger("test_stop_profit_advanced_3")


class TestStopProfitAdvanced3(TestBase):
    """测试止损补仓功能"""

    def setUp(self):
        """每个测试前的准备"""
        super().setUp()
        # 导入必要的模块
        from position_manager import PositionManager
        from trading_executor import TradingExecutor

        # 创建模拟的trading_executor
        self.mock_executor = MagicMock(spec=TradingExecutor)
        self.mock_executor.qmt_trader = MagicMock()

        # 创建position_manager实例（单例模式，会自动初始化数据库）
        self.position_manager = PositionManager()
        self.position_manager.qmt_trader = self.mock_executor.qmt_trader

        logger.info(f"测试准备完成: {self._testMethodName}")

    def tearDown(self):
        """每个测试后的清理"""
        # PositionManager 是单例，不需要手动关闭
        super().tearDown()

    def test_16_stop_loss_buy_position_limit(self):
        """
        测试16：止损补仓的持仓限制

        测试场景：
        1. 持仓接近上限时，补仓金额应自动调整
        2. 持仓达到上限时，不应生成补仓信号
        """
        logger.info("=" * 60)
        logger.info("测试16：止损补仓的持仓限制")
        logger.info("=" * 60)

        if not hasattr(config, 'ENABLE_STOP_LOSS_BUY'):
            self.skipTest("ENABLE_STOP_LOSS_BUY功能未实现")

        stock_code = "000002.SZ"

        # ========== 场景1：持仓接近上限 ==========
        logger.info("\n--- 场景1：持仓接近上限 ---")

        # 创建接近上限的持仓
        # MAX_POSITION_VALUE = 70000, 当前市值 = 65000
        cost_price = 10.0
        current_price = 9.20
        volume = 6500  # 6500股 * 10元 = 65000元

        conn = self.position_manager.conn
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO positions
            (stock_code, volume, available, cost_price, current_price,
             open_date, profit_triggered, highest_price, stop_loss_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (stock_code, volume, volume, cost_price, current_price,
              datetime.now().strftime("%Y-%m-%d"), 0, cost_price, 9.25))
        conn.commit()

        current_market_value = volume * current_price
        remaining_space = config.MAX_POSITION_VALUE - current_market_value

        logger.info(f"当前市值: {current_market_value:.2f}元")
        logger.info(f"最大持仓: {config.MAX_POSITION_VALUE}元")
        logger.info(f"剩余空间: {remaining_space:.2f}元")

        with patch.object(config, 'ENABLE_STOP_LOSS_BUY', True):
            signal_type, signal_info = self.position_manager.check_add_position_signal(stock_code)

            if signal_type == 'add_position' and signal_info:
                add_amount = signal_info['add_amount']
                logger.info(f"补仓金额: {add_amount}元")

                # 验证：补仓金额应小于等于剩余空间
                self.assertLessEqual(add_amount, remaining_space + 1,  # +1容差
                                   "补仓金额应不超过剩余持仓空间")
                logger.info(f"✓ 补仓金额自动调整验证通过")
            else:
                logger.info("未生成补仓信号（可能剩余空间不足）")

        # ========== 场景2：持仓达到上限 ==========
        logger.info("\n--- 场景2：持仓达到上限 ---")

        # 更新持仓到上限
        volume_at_limit = int(config.MAX_POSITION_VALUE / current_price)
        cursor.execute("""
            UPDATE positions
            SET volume = ?, available = ?
            WHERE stock_code = ?
        """, (volume_at_limit, volume_at_limit, stock_code))
        conn.commit()

        market_value_at_limit = volume_at_limit * current_price
        logger.info(f"持仓市值: {market_value_at_limit:.2f}元（已达上限）")

        with patch.object(config, 'ENABLE_STOP_LOSS_BUY', True):
            signal_type, signal_info = self.position_manager.check_add_position_signal(stock_code)

            logger.info(f"补仓信号检测结果: signal_type={signal_type}")

            # 验证：不应生成补仓信号
            self.assertIsNone(signal_type,
                            "持仓达到上限时，不应生成补仓信号")
            logger.info("✓ 持仓上限验证通过：未生成补仓信号")

        logger.info("\n" + "=" * 60)
        logger.info("测试16完成：止损补仓持仓限制验证通过")
        logger.info("=" * 60)


if __name__ == '__main__':
    # 配置unittest输出
    unittest.main(verbosity=2)
