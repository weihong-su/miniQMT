"""
网格交易功能测试 - 完整版
测试所有活跃会话
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
from datetime import datetime
import config

def test_all_sessions():
    """测试所有活跃会话"""
    db_path = config.DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 获取所有活跃会话
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM grid_trading_sessions
        WHERE status='active'
        ORDER BY start_time DESC
    """)
    sessions = [dict(row) for row in cursor.fetchall()]

    print("\n" + "=" * 100)
    print("网格交易功能测试 - 完整报告")
    print("=" * 100)
    print(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"数据库: {db_path}")
    print(f"活跃会话数: {len(sessions)}")
    print("=" * 100)

    for i, session in enumerate(sessions, 1):
        print(f"\n{'#' * 100}")
        print(f"# 会话 {i}/{len(sessions)}: {session['stock_code']}")
        print(f"{'#' * 100}")

        # 基本信息
        print(f"\n[基本信息]")
        print(f"  会话ID: {session['id']}")
        print(f"  股票代码: {session['stock_code']}")
        print(f"  状态: {session['status']}")
        print(f"  创建时间: {session['created_at']}")
        print(f"  更新时间: {session['updated_at']}")

        # 时间范围
        print(f"\n[时间范围]")
        print(f"  开始时间: {session['start_time']}")
        print(f"  结束时间: {session['end_time']}")

        start_dt = datetime.fromisoformat(session['start_time'])
        end_dt = datetime.fromisoformat(session['end_time'])
        duration = (end_dt - start_dt).days
        remaining = (end_dt - datetime.now()).days

        print(f"  持续天数: {duration}天")
        print(f"  剩余天数: {remaining}天")

        # 价格配置
        print(f"\n[价格配置]")
        print(f"  中心价格: {session['center_price']:.2f}")
        if session['current_center_price']:
            print(f"  当前中心价: {session['current_center_price']:.2f}")
            drift = (session['current_center_price'] - session['center_price']) / session['center_price']
            print(f"  中心价漂移: {drift*100:+.2f}%")
        print(f"  价格间隔: {session['price_interval']*100:.1f}%")

        # 交易配置
        print(f"\n[交易配置]")
        print(f"  仓位比例: {session['position_ratio']*100:.1f}%")
        print(f"  回调比例: {session['callback_ratio']*100:.2f}%")

        # 资金配置
        print(f"\n[资金配置]")
        print(f"  最大投入: {session['max_investment']:.2f}")
        print(f"  当前投入: {session['current_investment']:.2f}")
        print(f"  剩余额度: {session['max_investment'] - session['current_investment']:.2f}")
        print(f"  使用率: {session['current_investment']/session['max_investment']*100:.1f}%")

        # 退出配置
        print(f"\n[退出配置]")
        print(f"  最大偏离: {session['max_deviation']*100:.1f}%")
        print(f"  目标盈利: {session['target_profit']*100:.1f}%")
        print(f"  止损比例: {session['stop_loss']*100:.1f}%")

        # 网格档位
        print(f"\n[网格档位]")
        center_price = session['center_price']
        price_interval = session['price_interval']
        max_deviation = session['max_deviation']

        max_levels = int(max_deviation / price_interval)
        upper_bound = center_price * (1 + max_deviation)
        lower_bound = center_price * (1 - max_deviation)

        print(f"  最大档位数: {max_levels}")
        print(f"  价格上界: {upper_bound:.2f}")
        print(f"  价格下界: {lower_bound:.2f}")
        print(f"  档位价差: {center_price * price_interval:.2f}")

        # 交易统计
        print(f"\n[交易统计]")
        print(f"  总交易次数: {session['trade_count']}")
        print(f"  买入次数: {session['buy_count']}")
        print(f"  卖出次数: {session['sell_count']}")
        print(f"  买入总额: {session['total_buy_amount']:.2f}")
        print(f"  卖出总额: {session['total_sell_amount']:.2f}")

        if session['trade_count'] > 0:
            net_amount = session['total_sell_amount'] - session['total_buy_amount']
            print(f"  净收益: {net_amount:+.2f}")

        # 查询最近交易
        cursor.execute("""
            SELECT * FROM grid_trades
            WHERE session_id=?
            ORDER BY trade_time DESC
            LIMIT 5
        """, (session['id'],))
        recent_trades = cursor.fetchall()

        if recent_trades:
            print(f"\n[最近交易] (最多显示5笔)")
            for trade in recent_trades:
                print(f"  {trade['trade_time']}: {trade['trade_type']:4s} "
                      f"档位{trade['grid_level']:+2.0f} "
                      f"价格{trade['trigger_price']:.2f} "
                      f"数量{trade['volume']} "
                      f"金额{trade['amount']:.2f}")

        # 健康检查
        print(f"\n[健康检查]")
        checks = []

        # 检查1: 状态正常
        if session['status'] == 'active':
            checks.append(("状态", "PASS", "active"))
        else:
            checks.append(("状态", "FAIL", f"异常状态: {session['status']}"))

        # 检查2: 时间有效
        now = datetime.now()
        if start_dt <= now <= end_dt:
            checks.append(("时间", "PASS", "在有效期内"))
        else:
            checks.append(("时间", "WARN", "不在有效期内"))

        # 检查3: 参数有效
        if (center_price > 0 and price_interval > 0 and
            0 < session['position_ratio'] <= 1 and
            0 < session['callback_ratio'] < 1):
            checks.append(("参数", "PASS", "配置正确"))
        else:
            checks.append(("参数", "FAIL", "配置异常"))

        # 检查4: 资金安全
        if (session['max_investment'] > 0 and
            session['current_investment'] >= 0 and
            session['current_investment'] <= session['max_investment']):
            checks.append(("资金", "PASS", "额度正常"))
        else:
            checks.append(("资金", "FAIL", "额度异常"))

        # 检查5: 档位合理
        if max_levels > 0:
            checks.append(("档位", "PASS", f"{max_levels}个档位"))
        else:
            checks.append(("档位", "FAIL", "档位计算错误"))

        # 打印检查结果
        for name, status, msg in checks:
            symbol = "+" if status == "PASS" else ("!" if status == "WARN" else "X")
            print(f"  [{symbol}] {name:6s}: {msg}")

        # 总体评分
        pass_count = sum(1 for _, status, _ in checks if status == "PASS")
        total_count = len(checks)
        score = pass_count / total_count * 100

        print(f"\n[总体评分]")
        print(f"  通过: {pass_count}/{total_count}")
        print(f"  得分: {score:.0f}分")

        if score == 100:
            print(f"  评级: 优秀 - 所有检查通过")
        elif score >= 80:
            print(f"  评级: 良好 - 大部分检查通过")
        elif score >= 60:
            print(f"  评级: 合格 - 部分检查通过")
        else:
            print(f"  评级: 不合格 - 存在严重问题")

    # 总结
    print(f"\n{'=' * 100}")
    print(f"测试完成")
    print(f"{'=' * 100}")

    conn.close()

if __name__ == "__main__":
    test_all_sessions()
