#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试修复后的max_investment计算
"""

import requests
import json

API_BASE_URL = "http://127.0.0.1:5000"

print("=" * 70)
print("测试修复后的max_investment计算")
print("=" * 70)

# 测试股票
test_stock = "600509.SH"

# 1. 先获取持仓数据
print("\n1. 获取持仓数据:")
print("-" * 70)
positions_response = requests.get(f"{API_BASE_URL}/api/positions")
positions_data = positions_response.json()

if positions_data.get('status') == 'success':
    positions_all = positions_data.get('data', {}).get('positions_all', [])
    print(f"总持仓数: {len(positions_all)}")

    for pos in positions_all:
        if pos.get('stock_code') == test_stock:
            print(f"\n股票: {pos.get('stock_code')}")
            print(f"  持仓数量: {pos.get('volume', 0)}")
            print(f"  当前价格: {pos.get('current_price', 0)}")
            print(f"  持仓市值: {pos.get('market_value', 0)}")
            break
    else:
        print(f"\n未找到 {test_stock} 的持仓")

# 2. 获取网格配置
print("\n2. 获取网格配置:")
print("-" * 70)
session_response = requests.get(f"{API_BASE_URL}/api/grid/session/{test_stock}")
session_data = session_response.json()

print(f"API响应:")
print(json.dumps(session_data, indent=2, ensure_ascii=False))

# 3. 验证计算
if session_data.get('success'):
    has_session = session_data.get('has_session')
    max_investment = session_data.get('config', {}).get('max_investment', 0)

    print(f"\n3. 验证结果:")
    print("-" * 70)
    print(f"has_session: {has_session}")
    print(f"max_investment: {max_investment}")

    if has_session:
        print("  [说明] 该股票有active session，max_investment来自数据库中保存的配置")
    else:
        print("  [说明] 该股票无active session，max_investment基于当前持仓市值计算")

print("\n" + "=" * 70)
print("测试完成")
print("=" * 70)
