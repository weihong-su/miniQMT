#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试网格交易checkbox状态更新功能
使用 /api/grid/session/<stock_code> 接口验证
"""

import requests
import json
import time
from datetime import datetime

# 配置
API_BASE_URL = "http://127.0.0.1:5000"
TEST_STOCK_CODES = ["300342.SZ", "300367.SZ"]

def test_grid_session_api():
    """测试单个股票的网格会话状态API"""
    print("=" * 60)
    print("测试网格交易会话状态API")
    print("=" * 60)

    results = []

    for stock_code in TEST_STOCK_CODES:
        print(f"\n测试股票: {stock_code}")
        print("-" * 40)

        try:
            # 调用API获取网格会话状态
            url = f"{API_BASE_URL}/api/grid/session/{stock_code}"
            print(f"请求URL: {url}")

            response = requests.get(url, timeout=5)
            print(f"响应状态码: {response.status_code}")

            if response.status_code == 200:
                data = response.json()
                print(f"响应数据: {json.dumps(data, indent=2, ensure_ascii=False)}")

                # 检查关键字段
                if data.get('success'):
                    has_session = data.get('has_session', False)

                    result = {
                        'stock_code': stock_code,
                        'has_session': has_session,
                        'session_id': data.get('session_id') if has_session else None,
                        'success': True
                    }

                    if has_session:
                        print(f"  [OK] 该股票有活跃的网格交易会话")
                        print(f"  Session ID: {data.get('session_id')}")
                    else:
                        print(f"  [INFO] 该股票无活跃的网格交易会话")

                    results.append(result)
                else:
                    print(f"  [ERROR] API返回失败: {data.get('error')}")
                    results.append({
                        'stock_code': stock_code,
                        'success': False,
                        'error': data.get('error')
                    })
            else:
                print(f"  [ERROR] HTTP请求失败")
                results.append({
                    'stock_code': stock_code,
                    'success': False,
                    'error': f'HTTP {response.status_code}'
                })

        except requests.exceptions.Timeout:
            print(f"  [ERROR] 请求超时")
            results.append({
                'stock_code': stock_code,
                'success': False,
                'error': 'Timeout'
            })
        except requests.exceptions.ConnectionError:
            print(f"  [ERROR] 连接失败，请确认Web服务器已启动")
            results.append({
                'stock_code': stock_code,
                'success': False,
                'error': 'Connection failed'
            })
        except Exception as e:
            print(f"  [ERROR] 异常: {str(e)}")
            results.append({
                'stock_code': stock_code,
                'success': False,
                'error': str(e)
            })

    # 打印测试结果汇总
    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)

    success_count = sum(1 for r in results if r.get('success'))
    total_count = len(results)
    active_session_count = sum(1 for r in results if r.get('has_session'))

    print(f"总测试股票数: {total_count}")
    print(f"API调用成功: {success_count}")
    print(f"活跃session数: {active_session_count}")

    print("\n详细信息:")
    for result in results:
        stock = result['stock_code']
        if result.get('success'):
            has_session = result.get('has_session')
            status = "[有活跃session]" if has_session else "[无活跃session]"
            print(f"  {stock}: {status}")
        else:
            error = result.get('error')
            print(f"  {stock}: [ERROR] {error}")

    return results


def test_all_grid_sessions():
    """测试获取所有网格会话的API"""
    print("\n" + "=" * 60)
    print("测试获取所有网格会话API")
    print("=" * 60)

    try:
        url = f"{API_BASE_URL}/api/grid/sessions"
        print(f"请求URL: {url}")

        response = requests.get(url, timeout=5)
        print(f"响应状态码: {response.status_code}")

        if response.status_code == 200:
            data = response.json()
            print(f"响应数据: {json.dumps(data, indent=2, ensure_ascii=False)}")

            if data.get('success'):
                sessions = data.get('sessions', [])
                print(f"\n当前活跃的网格会话数量: {len(sessions)}")

                for session in sessions:
                    print(f"  - {session.get('stock_code')}: Session ID={session.get('session_id')}, Status={session.get('status')}")

                return sessions
            else:
                print(f"  [ERROR] API返回失败: {data.get('message')}")
                return []
        else:
            print(f"  [ERROR] HTTP请求失败")
            return []

    except Exception as e:
        print(f"  [ERROR] 异常: {str(e)}")
        return []


def main():
    """主测试函数"""
    print(f"\n测试开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    # 测试1: 获取所有网格会话
    all_sessions = test_all_grid_sessions()

    # 测试2: 测试单个股票API
    results = test_grid_session_api()

    # 对比结果
    print("\n" + "=" * 60)
    print("结果验证")
    print("=" * 60)

    if all_sessions:
        active_session_stocks = {s['stock_code'] for s in all_sessions if s['status'] == 'active'}
        print(f"所有会话API返回的活跃股票: {active_session_stocks}")

        for result in results:
            if result.get('success'):
                stock = result['stock_code']
                has_session = result.get('has_session')
                expected = stock in active_session_stocks

                if has_session == expected:
                    print(f"  {stock}: [OK] 状态一致")
                else:
                    print(f"  {stock}: [WARNING] 状态不一致 (has_session={has_session}, expected={expected})")

    print(f"\n测试结束时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("\n测试完成!")


if __name__ == "__main__":
    main()
