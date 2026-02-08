#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
网格交易BUG检测脚本
使用静态代码分析方法检测已知BUG是否存在

BUG 1: position_manager.py:3039 - latest_quote变量未定义
BUG 2: position_manager.py:1336 - 网格检测被0.3%阈值限制
"""

import os
import re
import sys

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)


def detect_bug_1(file_path):
    """
    检测BUG 1: latest_quote变量未定义

    检测逻辑:
    1. 在网格交易信号检测代码块中查找 'if latest_quote:' 的使用
    2. 向上查找是否有 latest_quote 的定义或赋值
    3. 如果在使用前没有定义，则BUG存在
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    # 查找关键代码行
    target_line = None
    for i, line in enumerate(lines):
        if 'if latest_quote:' in line and i > 3030:  # 大约在3039行附近
            target_line = i + 1  # 转换为1-based行号
            break

    if not target_line:
        return False, "未找到目标代码行"

    # 向上查找latest_quote的定义
    # 查找范围: 从函数开始到目标行
    search_start = max(0, target_line - 200)  # 向上查找200行
    search_lines = lines[search_start:target_line-1]

    # 查找latest_quote的赋值语句
    latest_quote_defined = False
    for line in search_lines:
        if re.search(r'latest_quote\s*=', line):
            latest_quote_defined = True
            break

    if latest_quote_defined:
        return False, f"已修复 - latest_quote在第{target_line}行使用前已定义"
    else:
        return True, f"存在 - 第{target_line}行使用latest_quote但未定义"


def detect_bug_2(file_path):
    """
    检测BUG 2: 网格检测被0.3%阈值限制

    检测逻辑:
    1. 找到第1336行的if语句及其缩进级别
    2. 找到check_grid_signals调用的位置及其缩进级别
    3. 如果check_grid_signals的缩进级别 <= 第1336行if的缩进级别，则BUG已修复
    4. 否则BUG仍存在
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    # 查找关键代码行: 0.003阈值检查
    threshold_line_idx = None
    threshold_indent = None
    for i, line in enumerate(lines):
        if '> 0.003' in line and i > 1330 and i < 1340:  # 大约在1336行附近
            threshold_line_idx = i
            # 计算缩进级别（空格数）
            threshold_indent = len(line) - len(line.lstrip())
            break

    if threshold_line_idx is None:
        return False, "未找到阈值检查代码"

    # 查找check_grid_signals调用
    grid_check_line_idx = None
    grid_check_indent = None
    for i in range(threshold_line_idx, min(threshold_line_idx + 50, len(lines))):
        line = lines[i]
        if 'check_grid_signals' in line or 'ENABLE_GRID_TRADING' in line:
            grid_check_line_idx = i
            grid_check_indent = len(line) - len(line.lstrip())
            break

    if grid_check_line_idx is None:
        return False, "未找到网格检测代码"

    # 比较缩进级别
    # 如果网格检测的缩进 > 阈值if的缩进，说明在if块内部（BUG存在）
    # 如果网格检测的缩进 <= 阈值if的缩进，说明在if块外部（BUG已修复）
    if grid_check_indent > threshold_indent:
        return True, f"存在 - 第{threshold_line_idx+1}行的0.3%阈值限制了网格检测（网格检测在if块内部，缩进{grid_check_indent}>{threshold_indent}）"
    else:
        return False, f"已修复 - 第{threshold_line_idx+1}行的阈值检查不影响网格检测（网格检测在if块外部，缩进{grid_check_indent}<={threshold_indent}）"


def main():
    """主函数"""
    # 设置Windows控制台编码为UTF-8
    if sys.platform == 'win32':
        import codecs
        sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')

    print("=" * 60)
    print("网格交易BUG检测报告")
    print("=" * 60)
    print()

    # 定位position_manager.py文件
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    target_file = os.path.join(project_root, 'position_manager.py')

    if not os.path.exists(target_file):
        print(f"错误: 找不到文件 {target_file}")
        return 1

    print(f"分析文件: {target_file}")
    print()

    # 检测BUG 1
    print("BUG 1: latest_quote未定义")
    print("-" * 60)
    print("位置: position_manager.py:3039")
    bug1_exists, bug1_msg = detect_bug_1(target_file)
    print(f"状态: {'[存在]' if bug1_exists else '[已修复]'}")
    print(f"详情: {bug1_msg}")
    print()

    # 检测BUG 2
    print("BUG 2: 网格检测被阈值限制")
    print("-" * 60)
    print("位置: position_manager.py:1336")
    bug2_exists, bug2_msg = detect_bug_2(target_file)
    print(f"状态: {'[存在]' if bug2_exists else '[已修复]'}")
    print(f"详情: {bug2_msg}")
    print()

    # 总结
    print("=" * 60)
    print("检测总结")
    print("=" * 60)
    total_bugs = sum([bug1_exists, bug2_exists])
    print(f"发现BUG数量: {total_bugs}/2")

    if total_bugs == 0:
        print("结论: 所有已知BUG已修复")
        return 0
    else:
        print("结论: 仍有BUG需要修复")
        return 1


if __name__ == '__main__':
    sys.exit(main())
