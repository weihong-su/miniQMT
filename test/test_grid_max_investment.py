#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试网格交易max_investment参数计算功能
验证：max_investment = 当前持仓市值 * 50%
"""

import requests
import json
from datetime import datetime

# 配置
API_BASE_URL = "http://127.0.0.1:5000"
TEST_STOCK_CODES = ["300342.SZ", "300367.SZ", "600509.SH"]

def test_max_investment_calculation():
    """测试max_investment计算逻辑"""
    print("=" * 70)
    print("测试网格交易max_investment参数计算")
    print("=" * 70)

    results = []

    for stock_code in TEST_STOCK_CODES:
        print(f"\n测试股票: {stock_code}")
        print("-" * 70)

        try:
            # 1. 获取持仓信息
            positions_url = f"{API_BASE_URL}/api/positions"
            positions_response = requests.get(positions_url, timeout=5)

            stock_market_value = 0
            if positions_response.status_code == 200:
                positions_data = positions_response.json()
                if positions_data.get('success') and positions_data.get('data'):
                    for pos in positions_data['data']:
                        if pos.get('stock_code') == stock_code:
                            stock_market_value = float(pos.get('market_value', 0))
                            print(f"  当前持仓市值: {stock_market_value:.2f}元")
                            print(f"  持仓数量: {pos.get('volume', 0)}股")
                            print(f"  成本价: {pos.get('cost_price', 0):.2f}元")
                            print(f"  当前价: {pos.get('current_price', 0):.2f}元")
                            break

            if stock_market_value == 0:
                print(f"  [INFO] 该股票无持仓或持仓市值为0")

            # 2. 获取网格交易配置
            session_url = f"{API_BASE_URL}/api/grid/session/{stock_code}"
            session_response = requests.get(session_url, timeout=5)

            if session_response.status_code == 200:
                session_data = session_response.json()
                print(f"\n  网格配置响应:")
                print(f"    has_session: {session_data.get('has_session')}")

                config = session_data.get('config', {})
                max_investment = config.get('max_investment', 0)

                print(f"    max_investment: {max_investment:.2f}元")

                # 3. 验证计算逻辑
                expected_max_investment = stock_market_value * 0.5 if stock_market_value > 0 else 10000

                print(f"\n  验证结果:")
                print(f"    预期值: {expected_max_investment:.2f}元")
                print(f"    实际值: {max_investment:.2f}元")

                # 允许小数点误差
                if abs(max_investment - expected_max_investment) < 0.01:
                    print(f"    [OK] 计算正确")
                    status = "OK"
                else:
                    print(f"    [WARNING] 计算不一致")
                    status = "WARNING"

                results.append({
                    'stock_code': stock_code,
                    'market_value': stock_market_value,
                    'expected_max_investment': expected_max_investment,
                    'actual_max_investment': max_investment,
                    'status': status
                })

            else:
                print(f"  [ERROR] 获取网格配置失败: HTTP {session_response.status_code}")
                results.append({
                    'stock_code': stock_code,
                    'market_value': stock_market_value,
                    'expected_max_investment': stock_market_value * 0.5 if stock_market_value > 0 else 10000,
                    'actual_max_investment': None,
                    'status': 'ERROR'
                })

        except requests.exceptions.Timeout:
            print(f"  [ERROR] 请求超时")
            results.append({
                'stock_code': stock_code,
                'status': 'TIMEOUT'
            })
        except requests.exceptions.ConnectionError:
            print(f"  [ERROR] 连接失败，请确认Web服务器已启动")
            results.append({
                'stock_code': stock_code,
                'status': 'CONNECTION_ERROR'
            })
        except Exception as e:
            print(f"  [ERROR] 异常: {str(e)}")
            results.append({
                'stock_code': stock_code,
                'status': 'ERROR',
                'error': str(e)
            })

    # 打印测试结果汇总
    print("\n" + "=" * 70)
    print("测试结果汇总")
    print("=" * 70)

    print(f"\n{'股票代码':<15} {'持仓市值':<15} {'预期max_inv':<15} {'实际max_inv':<15} {'状态':<10}")
    print("-" * 70)

    for result in results:
        stock = result['stock_code']
        market_value = result.get('market_value', 0)
        expected = result.get('expected_max_investment', 0)
        actual = result.get('actual_max_investment', 0)
        status = result.get('status', 'UNKNOWN')

        print(f"{stock:<15} {market_value:<15.2f} {expected:<15.2f} {actual:<15.2f} {status:<10}")

    # 统计
    ok_count = sum(1 for r in results if r.get('status') == 'OK')
    warning_count = sum(1 for r in results if r.get('status') == 'WARNING')
    error_count = sum(1 for r in results if r.get('status') in ['ERROR', 'TIMEOUT', 'CONNECTION_ERROR'])

    print("\n统计:")
    print(f"  总测试数: {len(results)}")
    print(f"  通过: {ok_count}")
    print(f"  警告: {warning_count}")
    print(f"  错误: {error_count}")

    return results


def main():
    """主测试函数"""
    print(f"\n测试开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    results = test_max_investment_calculation()

    print(f"\n测试结束时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("\n测试完成!")


if __name__ == "__main__":
    main()
