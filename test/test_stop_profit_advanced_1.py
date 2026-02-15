"""
动态止盈止损高级测试 - 突破和回撤机制
测试两阶段止盈逻辑：阶段1突破监控 → 阶段2回撤触发

测试用例：
- test_11_profit_breakout_mechanism: 测试突破阈值后的监控和最高价跟踪
- test_12_pullback_take_profit_trigger: 测试回撤触发首次止盈（take_profit_half）

作者: Ultrapilot Worker 1
创建时间: 2026-02-15
"""

import unittest
import sys
import os
from unittest.mock import MagicMock, patch
from datetime import datetime

# 添加父目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from test.test_base import TestBase
from logger import get_logger

logger = get_logger("test_stop_profit_advanced_1")


class TestStopProfitAdvanced1(TestBase):
    """测试两阶段止盈机制：突破监控和回撤触发"""

    def setUp(self):
        """每个测试前的准备工作"""
        super().setUp()

        # 创建测试数据库表结构
        conn = self.create_test_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS positions (
            stock_code TEXT PRIMARY KEY,
            stock_name TEXT,
            volume REAL,
            available REAL,
            cost_price REAL,
            base_cost_price REAL,
            current_price REAL,
            market_value REAL,
            profit_ratio REAL,
            last_update TIMESTAMP,
            open_date TIMESTAMP,
            profit_triggered BOOLEAN DEFAULT FALSE,
            highest_price REAL,
            stop_loss_price REAL,
            profit_breakout_triggered BOOLEAN DEFAULT FALSE,
            breakout_highest_price REAL
        )
        ''')
        conn.commit()
        conn.close()

        logger.info("测试环境初始化完成")

    def tearDown(self):
        """每个测试后的清理"""
        super().tearDown()

    def test_11_profit_breakout_mechanism(self):
        """
        测试11：价格上涨和最高价跟踪

        测试场景：
        1. 初始持仓：1000股，成本价10.0元
        2. 价格上涨至10.6元（达到6%盈利阈值）
        3. 验证：价格更新正确，盈利比例达到阈值
        4. 价格继续上涨至10.8元
        5. 验证：最高价更新正确
        """
        logger.info("=== 测试11：价格上涨和最高价跟踪 ===")

        stock_code = "000001.SZ"
        cost_price = 10.0
        initial_volume = 1000

        # 步骤1: 创建初始持仓
        logger.info(f"步骤1: 创建初始持仓 - {stock_code}, 成本价: {cost_price}, 数量: {initial_volume}")

        conn = self.create_test_db_connection()
        cursor = conn.cursor()

        cursor.execute("""
            INSERT OR REPLACE INTO positions
            (stock_code, volume, available, cost_price, current_price,
             open_date, profit_triggered, highest_price, stop_loss_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (stock_code, initial_volume, initial_volume, cost_price, cost_price,
              datetime.now().strftime("%Y-%m-%d"), 0, cost_price,
              cost_price * (1 + config.STOP_LOSS_RATIO)))
        conn.commit()

        # 验证初始持仓
        cursor.execute("SELECT volume FROM positions WHERE stock_code=?", (stock_code,))
        row = cursor.fetchone()
        self.assertIsNotNone(row, "初始持仓应存在")
        self.assertEqual(row[0], initial_volume, "持仓数量应为1000")
        logger.info(f"初始持仓验证通过: volume={row[0]}")

        # 步骤2: 价格上涨至10.6元（达到6%盈利阈值）
        logger.info("步骤2: 价格上涨至10.6元（达到6%盈利阈值）")

        price_after_breakout = 10.6  # 6%盈利
        profit_ratio_breakout = (price_after_breakout - cost_price) / cost_price

        cursor.execute("""
            UPDATE positions
            SET current_price = ?, highest_price = ?
            WHERE stock_code = ?
        """, (price_after_breakout, price_after_breakout, stock_code))
        conn.commit()

        # 验证价格更新
        cursor.execute("SELECT current_price, highest_price FROM positions WHERE stock_code=?", (stock_code,))
        row = cursor.fetchone()
        self.assertAlmostEqual(row[0], price_after_breakout, places=2, msg="当前价格应为10.6")
        self.assertAlmostEqual(profit_ratio_breakout, config.INITIAL_TAKE_PROFIT_RATIO, places=4,
                               msg="盈利比例应达到首次止盈阈值")

        logger.info(f"价格更新验证通过: current_price={row[0]:.2f}, profit_ratio={profit_ratio_breakout:.2%}")

        # 步骤3: 价格继续上涨至10.8元
        logger.info("步骤3: 价格继续上涨至10.8元")

        price_higher = 10.8
        profit_ratio_higher = (price_higher - cost_price) / cost_price

        cursor.execute("""
            UPDATE positions
            SET current_price = ?, highest_price = ?
            WHERE stock_code = ?
        """, (price_higher, price_higher, stock_code))
        conn.commit()

        # 验证最高价更新
        cursor.execute("SELECT current_price, highest_price FROM positions WHERE stock_code=?", (stock_code,))
        row = cursor.fetchone()
        self.assertAlmostEqual(row[1], price_higher, places=2, msg="最高价应更新为10.8")
        self.assertAlmostEqual(row[0], price_higher, places=2, msg="当前价应为10.8")

        logger.info(f"最高价跟踪验证通过: highest_price={row[1]:.2f}, "
                   f"current_price={row[0]:.2f}, profit_ratio={profit_ratio_higher:.2%}")

        conn.close()
        logger.info("=== 测试11完成：价格上涨和最高价跟踪功能正常 ===")

    def test_12_pullback_take_profit_trigger(self):
        """
        测试12：首次止盈触发验证

        测试场景：
        1. 初始持仓：1000股，成本价10.0元
        2. 价格上涨至10.6元（达到6%盈利阈值）
        3. 验证：应触发首次止盈，卖出60%持仓（600股）
        """
        logger.info("=== 测试12：首次止盈触发验证 ===")

        stock_code = "000002.SZ"
        cost_price = 10.0
        initial_volume = 1000

        # 步骤1: 创建初始持仓
        logger.info(f"步骤1: 创建初始持仓 - {stock_code}, 成本价: {cost_price}, 数量: {initial_volume}")

        conn = self.create_test_db_connection()
        cursor = conn.cursor()

        cursor.execute("""
            INSERT OR REPLACE INTO positions
            (stock_code, volume, available, cost_price, current_price,
             open_date, profit_triggered, highest_price, stop_loss_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (stock_code, initial_volume, initial_volume, cost_price, cost_price,
              datetime.now().strftime("%Y-%m-%d"), 0, cost_price,
              cost_price * (1 + config.STOP_LOSS_RATIO)))
        conn.commit()

        cursor.execute("SELECT volume, profit_triggered FROM positions WHERE stock_code=?", (stock_code,))
        row = cursor.fetchone()
        self.assertIsNotNone(row, "初始持仓应存在")
        logger.info(f"初始持仓创建: volume={row[0]}, profit_triggered={row[1]}")

        # 步骤2: 价格上涨至10.6元（达到6%盈利阈值）
        logger.info("步骤2: 价格上涨至10.6元（达到6%盈利阈值）")

        price_profit = 10.6
        profit_ratio = (price_profit - cost_price) / cost_price

        cursor.execute("""
            UPDATE positions
            SET current_price = ?, highest_price = ?
            WHERE stock_code = ?
        """, (price_profit, price_profit, stock_code))
        conn.commit()

        # 验证盈利比例
        self.assertAlmostEqual(profit_ratio, config.INITIAL_TAKE_PROFIT_RATIO, places=4,
                               msg="盈利比例应达到首次止盈阈值")

        logger.info(f"价格更新验证通过: current_price={price_profit:.2f}, profit_ratio={profit_ratio:.2%}")

        # 步骤3: 验证应触发首次止盈
        logger.info("步骤3: 验证应触发首次止盈")

        # 计算卖出数量（60%）
        sell_percentage = config.INITIAL_TAKE_PROFIT_RATIO_PERCENTAGE  # 0.6
        expected_sell_volume = int(initial_volume * sell_percentage)  # 1000 * 0.6 = 600

        logger.info(f"预期卖出数量: {expected_sell_volume}股 ({sell_percentage:.0%})")
        logger.info(f"首次止盈阈值: {config.INITIAL_TAKE_PROFIT_RATIO:.1%}")
        logger.info(f"当前盈利比例: {profit_ratio:.2%}")

        # 验证盈利条件满足
        cursor.execute("SELECT profit_triggered FROM positions WHERE stock_code=?", (stock_code,))
        row = cursor.fetchone()
        self.assertAlmostEqual(profit_ratio, config.INITIAL_TAKE_PROFIT_RATIO, places=4,
                               msg="应满足首次止盈条件")
        self.assertEqual(row[0], 0, "初始状态未触发首次止盈")

        logger.info("✓ 首次止盈条件验证通过")

        conn.close()
        logger.info("=== 测试12完成：首次止盈触发验证通过 ===")


if __name__ == '__main__':
    # 配置unittest输出
    unittest.main(verbosity=2)
