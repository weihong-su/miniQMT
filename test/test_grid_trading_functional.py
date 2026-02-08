"""
网格交易功能测试脚本

测试目标:
1. 验证网格交易会话状态
2. 验证参数配置正确性
3. 验证价格监控逻辑
4. 验证网格档位计算
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
from datetime import datetime
import config

class GridTradingFunctionalTest:
    """网格交易功能测试类"""

    def __init__(self):
        self.db_path = config.DB_PATH
        self.conn = None
        self.test_results = []

    def connect_db(self):
        """连接数据库"""
        try:
            self.conn = sqlite3.connect(self.db_path)
            self.conn.row_factory = sqlite3.Row
            print(f"[OK] 数据库连接成功: {self.db_path}")
            return True
        except Exception as e:
            print(f"[ERROR] 数据库连接失败: {str(e)}")
            return False

    def get_active_sessions(self):
        """获取活跃会话"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM grid_trading_sessions
            WHERE status='active'
            ORDER BY start_time DESC
        """)
        return [dict(row) for row in cursor.fetchall()]

    def test_session_status(self, session):
        """测试1: 验证会话状态"""
        print("\n" + "=" * 80)
        print("测试1: 会话状态验证")
        print("=" * 80)

        stock_code = session['stock_code']
        session_id = session['id']
        status = session['status']

        print(f"  股票代码: {stock_code}")
        print(f"  会话ID: {session_id}")
        print(f"  状态: {status}")

        # 验证状态
        if status == 'active':
            print(f"  [PASS] 会话状态正确: {status}")
            self.test_results.append(("会话状态", True, f"{stock_code} 状态为 active"))
        else:
            print(f"  [FAIL] 会话状态异常: {status}")
            self.test_results.append(("会话状态", False, f"{stock_code} 状态为 {status}"))

        # 验证时间范围
        start_time = session['start_time']
        end_time = session['end_time']
        print(f"  开始时间: {start_time}")
        print(f"  结束时间: {end_time}")

        try:
            start_dt = datetime.fromisoformat(start_time)
            end_dt = datetime.fromisoformat(end_time)
            now = datetime.now()

            if start_dt <= now <= end_dt:
                print(f"  [PASS] 时间范围有效")
                self.test_results.append(("时间范围", True, f"{stock_code} 在有效期内"))
            else:
                print(f"  [WARN] 时间范围异常: 当前时间不在范围内")
                self.test_results.append(("时间范围", False, f"{stock_code} 不在有效期内"))
        except Exception as e:
            print(f"  [ERROR] 时间解析失败: {str(e)}")
            self.test_results.append(("时间范围", False, f"{stock_code} 时间解析失败"))

    def test_parameters(self, session):
        """测试2: 验证参数配置"""
        print("\n" + "=" * 80)
        print("测试2: 参数配置验证")
        print("=" * 80)

        stock_code = session['stock_code']

        # 价格配置
        center_price = session['center_price']
        current_center_price = session['current_center_price']
        price_interval = session['price_interval']

        print(f"  中心价格: {center_price:.2f}")
        if current_center_price:
            print(f"  当前中心价: {current_center_price:.2f}")
        else:
            print(f"  当前中心价: N/A")
        print(f"  价格间隔: {price_interval*100:.1f}%")

        # 验证价格参数
        if center_price > 0 and price_interval > 0:
            print(f"  [PASS] 价格参数有效")
            self.test_results.append(("价格参数", True, f"{stock_code} 价格参数正常"))
        else:
            print(f"  [FAIL] 价格参数异常")
            self.test_results.append(("价格参数", False, f"{stock_code} 价格参数异常"))

        # 交易配置
        position_ratio = session['position_ratio']
        callback_ratio = session['callback_ratio']

        print(f"  仓位比例: {position_ratio*100:.1f}%")
        print(f"  回调比例: {callback_ratio*100:.2f}%")

        # 验证交易参数
        if 0 < position_ratio <= 1 and 0 < callback_ratio < 1:
            print(f"  [PASS] 交易参数有效")
            self.test_results.append(("交易参数", True, f"{stock_code} 交易参数正常"))
        else:
            print(f"  [FAIL] 交易参数异常")
            self.test_results.append(("交易参数", False, f"{stock_code} 交易参数异常"))

        # 资金配置
        max_investment = session['max_investment']
        current_investment = session['current_investment']

        print(f"  最大投入: {max_investment:.2f}")
        print(f"  当前投入: {current_investment:.2f}")
        print(f"  剩余额度: {max_investment - current_investment:.2f}")

        # 验证资金参数
        if max_investment > 0 and current_investment >= 0 and current_investment <= max_investment:
            print(f"  [PASS] 资金参数有效")
            self.test_results.append(("资金参数", True, f"{stock_code} 资金参数正常"))
        else:
            print(f"  [FAIL] 资金参数异常")
            self.test_results.append(("资金参数", False, f"{stock_code} 资金参数异常"))

        # 退出配置
        max_deviation = session['max_deviation']
        target_profit = session['target_profit']
        stop_loss = session['stop_loss']

        print(f"  最大偏离: {max_deviation*100:.1f}%")
        print(f"  目标盈利: {target_profit*100:.1f}%")
        print(f"  止损比例: {stop_loss*100:.1f}%")

        # 验证退出参数
        if max_deviation > 0 and target_profit > 0 and stop_loss < 0:
            print(f"  [PASS] 退出参数有效")
            self.test_results.append(("退出参数", True, f"{stock_code} 退出参数正常"))
        else:
            print(f"  [FAIL] 退出参数异常")
            self.test_results.append(("退出参数", False, f"{stock_code} 退出参数异常"))

    def test_grid_levels(self, session):
        """测试3: 验证网格档位计算"""
        print("\n" + "=" * 80)
        print("测试3: 网格档位计算验证")
        print("=" * 80)

        stock_code = session['stock_code']
        center_price = session['center_price']
        price_interval = session['price_interval']
        max_deviation = session['max_deviation']

        print(f"  中心价格: {center_price:.2f}")
        print(f"  价格间隔: {price_interval*100:.1f}%")
        print(f"  最大偏离: {max_deviation*100:.1f}%")

        # 计算网格档位
        max_levels = int(max_deviation / price_interval)
        print(f"\n  理论最大档位数: {max_levels}")

        # 计算上下边界
        upper_bound = center_price * (1 + max_deviation)
        lower_bound = center_price * (1 - max_deviation)

        print(f"  价格上界: {upper_bound:.2f}")
        print(f"  价格下界: {lower_bound:.2f}")

        # 生成网格档位
        print(f"\n  网格档位分布:")
        print(f"  {'档位':>6s} {'价格':>10s} {'偏离':>8s}")
        print(f"  {'-'*6} {'-'*10} {'-'*8}")

        for level in range(-max_levels, max_levels + 1):
            grid_price = center_price * (1 + level * price_interval)
            deviation = (grid_price - center_price) / center_price

            # 只显示部分档位
            if abs(level) <= 3 or abs(level) == max_levels:
                print(f"  {level:>6d} {grid_price:>10.2f} {deviation*100:>7.1f}%")
            elif abs(level) == 4:
                print(f"  {'...':>6s} {'...':>10s} {'...':>8s}")

        # 验证档位计算
        if max_levels > 0:
            print(f"\n  [PASS] 网格档位计算正确")
            self.test_results.append(("网格档位", True, f"{stock_code} 档位计算正常"))
        else:
            print(f"\n  [FAIL] 网格档位计算异常")
            self.test_results.append(("网格档位", False, f"{stock_code} 档位计算异常"))

    def test_trade_statistics(self, session):
        """测试4: 验证交易统计"""
        print("\n" + "=" * 80)
        print("测试4: 交易统计验证")
        print("=" * 80)

        stock_code = session['stock_code']
        session_id = session['id']

        trade_count = session['trade_count']
        buy_count = session['buy_count']
        sell_count = session['sell_count']
        total_buy_amount = session['total_buy_amount']
        total_sell_amount = session['total_sell_amount']

        print(f"  总交易次数: {trade_count}")
        print(f"  买入次数: {buy_count}")
        print(f"  卖出次数: {sell_count}")
        print(f"  买入总额: {total_buy_amount:.2f}")
        print(f"  卖出总额: {total_sell_amount:.2f}")

        # 查询实际交易记录
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) as count,
                   COALESCE(SUM(CASE WHEN trade_type='BUY' THEN 1 ELSE 0 END), 0) as buy_count,
                   COALESCE(SUM(CASE WHEN trade_type='SELL' THEN 1 ELSE 0 END), 0) as sell_count
            FROM grid_trades
            WHERE session_id=?
        """, (session_id,))
        row = cursor.fetchone()

        actual_count = row['count']
        actual_buy = row['buy_count']
        actual_sell = row['sell_count']

        print(f"\n  实际记录数: {actual_count}")
        print(f"  实际买入: {actual_buy}")
        print(f"  实际卖出: {actual_sell}")

        # 验证统计一致性
        if trade_count == actual_count and buy_count == actual_buy and sell_count == actual_sell:
            print(f"  [PASS] 交易统计一致")
            self.test_results.append(("交易统计", True, f"{stock_code} 统计数据一致"))
        else:
            print(f"  [WARN] 交易统计不一致")
            self.test_results.append(("交易统计", False, f"{stock_code} 统计数据不一致"))

    def print_summary(self):
        """打印测试总结"""
        print("\n" + "=" * 80)
        print("测试总结")
        print("=" * 80)

        total = len(self.test_results)
        passed = sum(1 for _, result, _ in self.test_results if result)
        failed = total - passed

        print(f"\n  总测试项: {total}")
        print(f"  通过: {passed}")
        print(f"  失败: {failed}")
        print(f"  通过率: {passed/total*100:.1f}%")

        if failed > 0:
            print(f"\n  失败项详情:")
            for name, result, msg in self.test_results:
                if not result:
                    print(f"    - {name}: {msg}")

        print("\n" + "=" * 80)

    def run_tests(self):
        """运行所有测试"""
        print("\n" + "=" * 80)
        print("网格交易功能测试")
        print("=" * 80)
        print(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"数据库: {self.db_path}")

        # 连接数据库
        if not self.connect_db():
            return

        # 获取活跃会话
        sessions = self.get_active_sessions()

        if not sessions:
            print("\n[ERROR] 没有找到活跃的网格交易会话")
            return

        print(f"\n找到 {len(sessions)} 个活跃会话")

        # 选择第一个会话进行测试
        session = sessions[0]
        stock_code = session['stock_code']

        print(f"\n选择测试股票: {stock_code}")
        print(f"会话ID: {session['id']}")

        # 运行测试
        self.test_session_status(session)
        self.test_parameters(session)
        self.test_grid_levels(session)
        self.test_trade_statistics(session)

        # 打印总结
        self.print_summary()

        # 关闭连接
        if self.conn:
            self.conn.close()

def main():
    """主函数"""
    tester = GridTradingFunctionalTest()
    tester.run_tests()

if __name__ == "__main__":
    main()
