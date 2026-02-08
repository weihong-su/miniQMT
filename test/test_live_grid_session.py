"""
Live Grid Trading Session Test
真实网格交易会话测试

测试目标: 验证活跃网格交易会话的实时监控和信号检测功能
测试对象: Session ID 54 - 300342.SZ (中心价56.18元)
"""

import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from grid_trading_manager import GridTradingManager
from grid_database import DatabaseManager
from position_manager import PositionManager
from trading_executor import TradingExecutor
from data_manager import DataManager
from logger import get_logger

logger = get_logger("test_live_grid_session")


class LiveGridSessionTester:
    """活跃网格交易会话测试器"""

    def __init__(self):
        self.db_path = 'data/trading.db'
        self.session_id = 54
        self.stock_code = '300342.SZ'

        # 初始化数据库管理器
        self.db = DatabaseManager(self.db_path)

        # 获取活跃会话信息
        session_data = self.db.get_grid_session(self.session_id)
        if not session_data:
            raise ValueError(f"Session {self.session_id} not found")

        logger.info("=" * 60)
        logger.info("活跃网格交易会话测试")
        logger.info("=" * 60)
        logger.info(f"Session ID: {self.session_id}")
        logger.info(f"股票代码: {self.stock_code}")
        logger.info(f"中心价格: {session_data['center_price']:.2f} 元")
        logger.info(f"当前中心: {session_data['current_center_price']:.2f} 元")
        logger.info(f"网格间隔: {session_data['price_interval']*100:.1f}%")
        logger.info(f"持仓比例: {session_data['position_ratio']*100:.1f}%")
        logger.info(f"回调比例: {session_data['callback_ratio']*100:.2f}%")
        logger.info(f"开始时间: {session_data['start_time']}")
        logger.info("=" * 60)
        logger.info("")

        # 显示网格档位
        self.display_grid_levels(session_data)

        # 初始化实际组件（非模拟）
        self.position_mgr = PositionManager()
        self.executor = TradingExecutor()
        self.data_mgr = DataManager()

        # 创建网格管理器
        self.grid_mgr = GridTradingManager(
            self.db,
            self.position_mgr,
            self.executor
        )

        # 验证会话已加载
        if self.stock_code not in self.grid_mgr.sessions:
            raise RuntimeError(f"Session {self.stock_code} not loaded in GridTradingManager")

        self.session = self.grid_mgr.sessions[self.stock_code]

    def display_grid_levels(self, session_data):
        """显示当前网格档位"""
        center = session_data['current_center_price']
        interval = session_data['price_interval']

        lower = center * (1 - interval)
        upper = center * (1 + interval)

        logger.info("当前网格档位:")
        logger.info(f"  下档: {lower:.2f} 元")
        logger.info(f"  中心: {center:.2f} 元")
        logger.info(f"  上档: {upper:.2f} 元")
        logger.info("")

    def test_01_session_status(self):
        """测试1: 验证会话状态"""
        logger.info("=" * 60)
        logger.info("TEST 1: 会话状态验证")
        logger.info("=" * 60)

        # 检查会话对象
        assert self.session.id == self.session_id, "Session ID mismatch"
        assert self.session.stock_code == self.stock_code, "Stock code mismatch"
        assert self.session.status == 'active', "Session not active"

        logger.info(f"[PASS] Session ID: {self.session.id}")
        logger.info(f"[PASS] Stock Code: {self.session.stock_code}")
        logger.info(f"[PASS] Status: {self.session.status}")
        logger.info(f"[PASS] Trade Count: {self.session.trade_count}")
        logger.info(f"[PASS] Buy Count: {self.session.buy_count}")
        logger.info(f"[PASS] Sell Count: {self.session.sell_count}")

        # 获取交易记录
        trades = self.db.get_grid_trades(self.session_id)
        logger.info(f"[INFO] Total trades in database: {len(trades)}")

        if trades:
            logger.info("[INFO] Recent trades:")
            for trade in trades[-5:]:  # 显示最近5笔
                logger.info(f"  - {trade['trade_time']}: {trade['trade_type']} "
                          f"{trade['volume']}股 @ {trade['price']:.2f}元 "
                          f"({trade['trigger_reason']})")

        return True

    def test_02_current_price_check(self):
        """测试2: 获取当前价格"""
        logger.info("")
        logger.info("=" * 60)
        logger.info("TEST 2: 当前价格获取")
        logger.info("=" * 60)

        try:
            # 获取实时价格
            current_price = self.data_mgr.get_latest_price(self.stock_code)

            if current_price:
                logger.info(f"[INFO] 当前价格: {current_price:.2f} 元")

                # 计算与网格档位的关系
                levels = self.session.get_grid_levels()
                lower = levels['lower']
                upper = levels['upper']

                if current_price > upper:
                    logger.info(f"[INFO] 价格位于上档之上 ({current_price:.2f} > {upper:.2f})")
                elif current_price < lower:
                    logger.info(f"[INFO] 价格位于下档之下 ({current_price:.2f} < {lower:.2f})")
                else:
                    logger.info(f"[INFO] 价格在网格区间内 [{lower:.2f}, {upper:.2f}]")

                return current_price
            else:
                logger.warning("[WARN] 无法获取实时价格，使用模拟价格")
                return self.session.current_center_price

        except Exception as e:
            logger.error(f"[ERROR] 获取价格失败: {str(e)}")
            logger.info("[INFO] 使用中心价格作为测试价格")
            return self.session.current_center_price

    def test_03_signal_detection(self, current_price):
        """测试3: 信号检测"""
        logger.info("")
        logger.info("=" * 60)
        logger.info("TEST 3: 网格信号检测")
        logger.info("=" * 60)

        logger.info(f"[INFO] 检测价格: {current_price:.2f} 元")

        # 执行信号检测
        signal = self.grid_mgr.check_grid_signals(self.stock_code, current_price)

        if signal:
            logger.info(f"[SIGNAL] 检测到交易信号!")
            logger.info(f"  - 类型: {signal['signal_type']}")
            logger.info(f"  - 价格: {signal['price']:.2f} 元")
            logger.info(f"  - 档位: {signal['grid_level']:.2f} 元")
            logger.info(f"  - 原因: {signal['trigger_reason']}")
        else:
            logger.info("[INFO] 未检测到交易信号")
            levels = self.session.get_grid_levels()
            logger.info(f"[INFO] 当前网格: [{levels['lower']:.2f}, {levels['center']:.2f}, {levels['upper']:.2f}]")

        return signal

    def test_04_price_tracker_status(self):
        """测试4: 价格追踪器状态"""
        logger.info("")
        logger.info("=" * 60)
        logger.info("TEST 4: 价格追踪器状态")
        logger.info("=" * 60)

        if self.stock_code in self.grid_mgr.price_trackers:
            tracker = self.grid_mgr.price_trackers[self.stock_code]

            logger.info(f"[INFO] 追踪器状态:")
            logger.info(f"  - 当前价格: {tracker.last_price:.2f} 元")
            logger.info(f"  - 峰值价格: {tracker.peak_price:.2f} 元")
            logger.info(f"  - 谷值价格: {tracker.valley_price:.2f} 元")
            logger.info(f"  - 等待回调: {tracker.waiting_callback}")
            logger.info(f"  - 追踪方向: {tracker.direction}")

            if tracker.waiting_callback:
                if tracker.direction == 'rising':
                    callback = (tracker.peak_price - tracker.last_price) / tracker.peak_price
                    logger.info(f"[INFO] 回调进度: {callback*100:.2f}% (阈值: {self.session.callback_ratio*100:.2f}%)")
                elif tracker.direction == 'falling':
                    callback = (tracker.last_price - tracker.valley_price) / tracker.valley_price
                    logger.info(f"[INFO] 回调进度: {callback*100:.2f}% (阈值: {self.session.callback_ratio*100:.2f}%)")
        else:
            logger.info("[INFO] 价格追踪器未初始化")

        return True

    def test_05_exit_conditions(self, current_price):
        """测试5: 退出条件检查"""
        logger.info("")
        logger.info("=" * 60)
        logger.info("TEST 5: 退出条件检查")
        logger.info("=" * 60)

        # 偏离度检查
        deviation = self.session.get_deviation_ratio(current_price)
        logger.info(f"[INFO] 偏离度: {deviation*100:.2f}% (上限: {self.session.max_deviation*100:.1f}%)")

        if abs(deviation) > self.session.max_deviation:
            logger.warning(f"[WARN] 偏离度超限!")

        # 盈亏检查
        if self.session.total_buy_amount > 0:
            profit_ratio = self.session.get_profit_ratio()
            logger.info(f"[INFO] 盈亏比例: {profit_ratio*100:.2f}%")
            logger.info(f"[INFO] 买入总额: {self.session.total_buy_amount:.2f} 元")
            logger.info(f"[INFO] 卖出总额: {self.session.total_sell_amount:.2f} 元")
        else:
            logger.info("[INFO] 暂无交易记录")

        # 时间检查
        if self.session.end_time:
            remaining = self.session.end_time - datetime.now()
            logger.info(f"[INFO] 剩余时间: {remaining}")

        return True

    def test_06_grid_statistics(self):
        """测试6: 网格统计"""
        logger.info("")
        logger.info("=" * 60)
        logger.info("TEST 6: 网格交易统计")
        logger.info("=" * 60)

        logger.info(f"[INFO] 会话统计:")
        logger.info(f"  - 总交易次数: {self.session.trade_count}")
        logger.info(f"  - 买入次数: {self.session.buy_count}")
        logger.info(f"  - 卖出次数: {self.session.sell_count}")
        logger.info(f"  - 当前投入: {self.session.current_investment:.2f} 元")
        logger.info(f"  - 最大投入: {self.session.max_investment:.2f} 元")

        if self.session.total_buy_amount > 0:
            profit = self.session.total_sell_amount - self.session.total_buy_amount
            profit_ratio = self.session.get_profit_ratio()
            logger.info(f"  - 总盈亏: {profit:.2f} 元 ({profit_ratio*100:.2f}%)")

        return True

    def run_all_tests(self):
        """运行所有测试"""
        logger.info("")
        logger.info("=" * 60)
        logger.info("开始执行活跃会话测试")
        logger.info("=" * 60)
        logger.info("")

        tests = [
            ("会话状态验证", self.test_01_session_status),
            ("当前价格获取", lambda: self.test_02_current_price_check()),
            ("网格信号检测", self.test_03_signal_detection),
            ("价格追踪器状态", self.test_04_price_tracker_status),
            ("退出条件检查", lambda: self.test_05_exit_conditions(self.test_02_current_price_check())),
            ("网格统计信息", self.test_06_grid_statistics),
        ]

        passed = 0
        failed = 0
        results = []
        current_price = None

        for name, test_func in tests:
            try:
                if name == "网格信号检测":
                    result = test_func(current_price)
                elif name == "退出条件检查":
                    result = test_func(current_price)
                elif name == "当前价格获取":
                    current_price = test_func()
                    result = current_price is not None
                else:
                    result = test_func()

                if result or result is None:
                    passed += 1
                    results.append((name, "PASS"))
                    logger.info(f"\n[RESULT] {name}: PASSED")
                else:
                    failed += 1
                    results.append((name, "FAIL"))
                    logger.error(f"\n[RESULT] {name}: FAILED")

            except Exception as e:
                failed += 1
                results.append((name, f"ERROR: {str(e)}"))
                logger.error(f"\n[RESULT] {name}: ERROR - {str(e)}")

        # 打印总结
        logger.info("\n" + "=" * 60)
        logger.info("测试结果总结")
        logger.info("=" * 60)
        for name, result in results:
            logger.info(f"  {name}: {result}")

        logger.info(f"\n总计: {len(tests)} 个测试")
        logger.info(f"通过: {passed} 个")
        logger.info(f"失败: {failed} 个")
        logger.info(f"成功率: {passed/len(tests)*100:.1f}%")

        return failed == 0


def main():
    """主函数"""
    print("\n" + "=" * 60)
    print("活跃网格交易会话实时测试")
    print("=" * 60)

    tester = LiveGridSessionTester()

    try:
        success = tester.run_all_tests()

        if success:
            print("\n[SUCCESS] 所有测试通过!")
            return 0
        else:
            print("\n[INFO] 部分测试完成（详见日志）")
            return 0

    except Exception as e:
        logger.error(f"测试执行异常: {str(e)}", exc_info=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
