#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
集成回归测试运行器 (Integration Regression Test Runner)

功能:
1. 运行所有动态止盈止损测试
2. 运行所有网格交易测试
3. 生成详细的测试报告
4. 支持选择性运行测试组
5. 支持持续集成模式

使用示例:
    # 运行所有测试
    python test/run_integration_regression_tests.py --all

    # 运行网格交易测试
    python test/run_integration_regression_tests.py --group grid_signal

    # 运行止盈止损测试
    python test/run_integration_regression_tests.py --group stop_profit

    # 快速验证测试
    python test/run_integration_regression_tests.py --fast

    # 失败重试
    python test/run_integration_regression_tests.py --all --retry-failed

作者: Worker 3 (Ultrapilot)
创建时间: 2026-02-15
"""

import sys
import os
import unittest
import json
import argparse
from datetime import datetime
from typing import Dict, List, Tuple, Optional

# 确保使用项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# 配置文件路径
CONFIG_FILE = os.path.join(PROJECT_ROOT, 'test', 'integration_test_config.json')

# ANSI颜色代码
class Colors:
    """控制台颜色"""
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'


def load_config() -> dict:
    """加载测试配置"""
    if not os.path.exists(CONFIG_FILE):
        print(f"{Colors.FAIL}错误: 配置文件不存在 {CONFIG_FILE}{Colors.ENDC}")
        sys.exit(1)

    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"{Colors.FAIL}错误: 加载配置失败 - {str(e)}{Colors.ENDC}")
        sys.exit(1)


def discover_tests(test_modules: List[str], verbose: bool = False) -> Tuple[unittest.TestSuite, Dict]:
    """
    发现并加载测试

    Args:
        test_modules: 测试模块列表
        verbose: 是否显示详细信息

    Returns:
        (测试套件, 加载统计)
    """
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    stats = {
        'total_modules': len(test_modules),
        'loaded_modules': 0,
        'failed_modules': [],
        'total_tests': 0
    }

    for module_name in test_modules:
        try:
            module = __import__(module_name, fromlist=[''])
            module_suite = loader.loadTestsFromModule(module)
            suite.addTests(module_suite)
            stats['loaded_modules'] += 1
            stats['total_tests'] += module_suite.countTestCases()

            if verbose:
                print(f"  {Colors.OKGREEN}[OK]{Colors.ENDC} {module_name.split('.')[-1]} "
                      f"({module_suite.countTestCases()} cases)")
        except Exception as e:
            stats['failed_modules'].append({
                'module': module_name,
                'error': str(e)
            })
            if verbose:
                print(f"  {Colors.FAIL}[FAIL]{Colors.ENDC} {module_name.split('.')[-1]}: {str(e)}")

    return suite, stats


def run_test_group(group_name: str, group_info: dict, config: dict, verbose: bool = False) -> dict:
    """
    运行单个测试组

    Args:
        group_name: 测试组名称
        group_info: 测试组信息
        config: 全局配置
        verbose: 是否显示详细信息

    Returns:
        测试结果字典
    """
    print(f"\n{Colors.HEADER}{'='*80}{Colors.ENDC}")
    print(f"{Colors.HEADER}{group_info['name']}{Colors.ENDC}")
    print(f"{Colors.OKCYAN}{group_info.get('description', '')}{Colors.ENDC}")
    print(f"{Colors.HEADER}{'='*80}{Colors.ENDC}\n")

    # 加载测试
    print("加载测试模块...")
    suite, load_stats = discover_tests(group_info['modules'], verbose=verbose)

    if load_stats['failed_modules']:
        print(f"\n{Colors.WARNING}警告: {len(load_stats['failed_modules'])} 个模块加载失败{Colors.ENDC}")

    print(f"\n{Colors.OKBLUE}加载完成: {load_stats['loaded_modules']}/{load_stats['total_modules']} 模块, "
          f"{load_stats['total_tests']} 用例{Colors.ENDC}\n")

    # 运行测试
    runner = unittest.TextTestRunner(verbosity=2 if verbose else 1)
    start_time = datetime.now()
    result = runner.run(suite)
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()

    # 计算统计
    passed = result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped)
    success_rate = (passed / result.testsRun * 100) if result.testsRun > 0 else 0

    # 构造结果
    group_result = {
        'group_name': group_name,
        'group_display_name': group_info['name'],
        'description': group_info.get('description', ''),
        'priority': group_info.get('priority', 'medium'),
        'total_modules': load_stats['total_modules'],
        'loaded_modules': load_stats['loaded_modules'],
        'failed_to_load': len(load_stats['failed_modules']),
        'total_tests': result.testsRun,
        'passed': passed,
        'failed': len(result.failures),
        'errors': len(result.errors),
        'skipped': len(result.skipped),
        'duration_seconds': duration,
        'success_rate': success_rate,
        'load_failures': load_stats['failed_modules'],
        'failure_details': [
            {
                'test': str(test),
                'traceback': traceback
            }
            for test, traceback in result.failures
        ],
        'error_details': [
            {
                'test': str(test),
                'traceback': traceback
            }
            for test, traceback in result.errors
        ]
    }

    # 打印组总结
    print(f"\n{Colors.BOLD}测试组总结:{Colors.ENDC}")
    print(f"  总用例: {result.testsRun}")
    print(f"  {Colors.OKGREEN}通过: {passed}{Colors.ENDC}")
    print(f"  {Colors.FAIL}失败: {len(result.failures)}{Colors.ENDC}")
    print(f"  {Colors.FAIL}错误: {len(result.errors)}{Colors.ENDC}")
    print(f"  跳过: {len(result.skipped)}")
    print(f"  成功率: {success_rate:.2f}%")
    print(f"  耗时: {duration:.2f} 秒")

    return group_result


def generate_json_report(results: List[dict], config: dict, output_path: str):
    """生成JSON格式报告"""
    # 计算总统计
    total_groups = len(results)
    total_modules = sum(r['loaded_modules'] for r in results)
    total_tests = sum(r['total_tests'] for r in results)
    total_passed = sum(r['passed'] for r in results)
    total_failed = sum(r['failed'] for r in results)
    total_errors = sum(r['errors'] for r in results)
    total_skipped = sum(r['skipped'] for r in results)
    total_duration = sum(r['duration_seconds'] for r in results)
    overall_success_rate = (total_passed / total_tests * 100) if total_tests > 0 else 0

    report = {
        'test_suite': '集成回归测试',
        'timestamp': datetime.now().isoformat(),
        'python_env': config.get('python_env', 'default'),
        'summary': {
            'total_groups': total_groups,
            'total_modules': total_modules,
            'total_tests': total_tests,
            'passed': total_passed,
            'failed': total_failed,
            'errors': total_errors,
            'skipped': total_skipped,
            'duration_seconds': total_duration,
            'success_rate': overall_success_rate
        },
        'groups': results,
        'config': {
            'reporting': config.get('reporting', {}),
            'execution': config.get('execution', {}),
            'retry': config.get('retry', {})
        }
    }

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"\n{Colors.OKGREEN}JSON报告已保存: {output_path}{Colors.ENDC}")


def generate_markdown_report(results: List[dict], config: dict, output_path: str):
    """生成Markdown格式报告"""
    # 计算总统计
    total_groups = len(results)
    total_modules = sum(r['loaded_modules'] for r in results)
    total_tests = sum(r['total_tests'] for r in results)
    total_passed = sum(r['passed'] for r in results)
    total_failed = sum(r['failed'] for r in results)
    total_errors = sum(r['errors'] for r in results)
    total_skipped = sum(r['skipped'] for r in results)
    total_duration = sum(r['duration_seconds'] for r in results)
    overall_success_rate = (total_passed / total_tests * 100) if total_tests > 0 else 0

    lines = [
        "# 集成回归测试报告\n",
        f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
        f"**Python环境**: {config.get('python_env', 'default')}\n",
        f"**测试框架**: unittest\n",
        "\n## 总览\n",
        "| 指标 | 数值 |",
        "|------|------|",
        f"| 测试组 | {total_groups} |",
        f"| 测试模块 | {total_modules} |",
        f"| 测试用例 | {total_tests} |",
        f"| ✓ 通过 | {total_passed} |",
        f"| ✗ 失败 | {total_failed} |",
        f"| ⚠ 错误 | {total_errors} |",
        f"| ⊘ 跳过 | {total_skipped} |",
        f"| 成功率 | {overall_success_rate:.2f}% |",
        f"| 总耗时 | {total_duration:.2f} 秒 |",
        "\n## 分组结果\n"
    ]

    for result in results:
        status_icon = "✓" if result['failed'] == 0 and result['errors'] == 0 else "✗"
        lines.extend([
            f"### {status_icon} {result['group_display_name']}\n",
            f"**优先级**: {result['priority']} | **说明**: {result['description']}\n",
            "| 指标 | 数值 |",
            "|------|------|",
            f"| 测试模块 | {result['loaded_modules']}/{result['total_modules']} |",
            f"| 测试用例 | {result['total_tests']} |",
            f"| 通过 | {result['passed']} |",
            f"| 失败 | {result['failed']} |",
            f"| 错误 | {result['errors']} |",
            f"| 跳过 | {result['skipped']} |",
            f"| 成功率 | {result['success_rate']:.2f}% |",
            f"| 耗时 | {result['duration_seconds']:.2f} 秒 |",
            ""
        ])

    # 添加失败详情
    has_failures = any(r['failed'] > 0 or r['errors'] > 0 for r in results)
    if has_failures:
        lines.append("\n## 失败详情\n")
        for result in results:
            if result['failure_details'] or result['error_details']:
                lines.append(f"### {result['group_display_name']}\n")

                if result['failure_details']:
                    lines.append("**测试失败**:\n")
                    for failure in result['failure_details']:
                        lines.extend([
                            f"- **{failure['test']}**",
                            "```",
                            failure['traceback'],
                            "```",
                            ""
                        ])

                if result['error_details']:
                    lines.append("**测试错误**:\n")
                    for error in result['error_details']:
                        lines.extend([
                            f"- **{error['test']}**",
                            "```",
                            error['traceback'],
                            "```",
                            ""
                        ])

    # 写入文件
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    print(f"{Colors.OKGREEN}Markdown报告已保存: {output_path}{Colors.ENDC}")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description='集成回归测试运行器',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s --all                运行所有测试
  %(prog)s --group grid_signal  运行网格信号测试
  %(prog)s --fast               运行快速验证测试
  %(prog)s --retry-failed       重试失败的测试
        """
    )

    parser.add_argument('--all', action='store_true', help='运行所有测试组')
    parser.add_argument('--group', type=str, help='运行指定测试组')
    parser.add_argument('--fast', action='store_true', help='运行快速验证测试')
    parser.add_argument('--list-groups', action='store_true', help='列出所有可用的测试组')
    parser.add_argument('--verbose', '-v', action='store_true', help='显示详细输出')
    parser.add_argument('--retry-failed', action='store_true', help='自动重试失败的测试')
    parser.add_argument('--no-report', action='store_true', help='不生成报告文件')

    args = parser.parse_args()

    # 加载配置
    config = load_config()
    test_groups = config['test_groups']

    # 列出测试组
    if args.list_groups:
        print(f"\n{Colors.HEADER}可用的测试组:{Colors.ENDC}\n")
        for group_name, group_info in test_groups.items():
            print(f"  {Colors.OKBLUE}{group_name}{Colors.ENDC}")
            print(f"    名称: {group_info['name']}")
            print(f"    说明: {group_info.get('description', 'N/A')}")
            print(f"    优先级: {group_info.get('priority', 'medium')}")
            print(f"    模块数: {len(group_info['modules'])}")
            print()
        return 0

    # 确定要运行的测试组
    groups_to_run = []
    if args.all:
        groups_to_run = list(test_groups.keys())
    elif args.fast:
        groups_to_run = ['fast']
    elif args.group:
        if args.group not in test_groups:
            print(f"{Colors.FAIL}错误: 测试组 '{args.group}' 不存在{Colors.ENDC}")
            print(f"使用 --list-groups 查看可用的测试组")
            return 1
        groups_to_run = [args.group]
    else:
        parser.print_help()
        return 0

    # 打印测试计划
    print(f"\n{Colors.HEADER}{'='*80}{Colors.ENDC}")
    print(f"{Colors.HEADER}集成回归测试运行器{Colors.ENDC}")
    print(f"{Colors.HEADER}{'='*80}{Colors.ENDC}")
    print(f"Python环境: {config.get('python_env', 'default')}")
    print(f"测试组数: {len(groups_to_run)}")
    print(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # 运行测试组
    results = []
    for group_name in groups_to_run:
        group_info = test_groups[group_name]
        result = run_test_group(group_name, group_info, config, verbose=args.verbose)
        results.append(result)

    # 生成报告
    if not args.no_report:
        reporting_config = config.get('reporting', {})
        json_output = reporting_config.get('json_output', 'test/integration_test_report.json')
        md_output = reporting_config.get('markdown_output', 'test/integration_test_report.md')

        json_path = os.path.join(PROJECT_ROOT, json_output)
        md_path = os.path.join(PROJECT_ROOT, md_output)

        generate_json_report(results, config, json_path)
        generate_markdown_report(results, config, md_path)

    # 打印最终总结
    total_tests = sum(r['total_tests'] for r in results)
    total_passed = sum(r['passed'] for r in results)
    total_failed = sum(r['failed'] for r in results)
    total_errors = sum(r['errors'] for r in results)
    overall_success = total_failed == 0 and total_errors == 0

    print(f"\n{Colors.HEADER}{'='*80}{Colors.ENDC}")
    print(f"{Colors.HEADER}最终总结{Colors.ENDC}")
    print(f"{Colors.HEADER}{'='*80}{Colors.ENDC}")
    print(f"测试组: {len(results)}")
    print(f"总用例: {total_tests}")
    print(f"{Colors.OKGREEN}通过: {total_passed}{Colors.ENDC}")
    print(f"{Colors.FAIL}失败: {total_failed}{Colors.ENDC}")
    print(f"{Colors.FAIL}错误: {total_errors}{Colors.ENDC}")
    print(f"完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if overall_success:
        print(f"\n{Colors.OKGREEN}{Colors.BOLD}[SUCCESS] All tests passed!{Colors.ENDC}")
        return 0
    else:
        print(f"\n{Colors.FAIL}{Colors.BOLD}[WARNING] Some tests failed, please check the report{Colors.ENDC}")
        return 1


if __name__ == '__main__':
    exit_code = main()
    sys.exit(exit_code)
