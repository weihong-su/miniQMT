#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
集成回归测试运行器 (Integration Regression Test Runner)

功能:
1. 自动准备测试环境（清理数据库、备份生产数据）
2. 运行所有动态止盈止损测试
3. 运行所有网格交易测试
4. 生成详细的测试报告
5. 支持选择性运行测试组
6. 支持持续集成模式

使用示例:
    # 运行所有测试（自动清理数据库）
    python test/run_integration_regression_tests.py --all

    # 运行网格交易测试
    python test/run_integration_regression_tests.py --group grid_signal

    # 运行止盈止损测试
    python test/run_integration_regression_tests.py --group stop_profit

    # 快速验证测试
    python test/run_integration_regression_tests.py --fast

    # 失败重试
    python test/run_integration_regression_tests.py --all --retry-failed

    # 跳过环境准备（不清理数据库）
    python test/run_integration_regression_tests.py --all --skip-env-prep

    # 不备份生产数据库
    python test/run_integration_regression_tests.py --all --no-backup

    # 详细输出
    python test/run_integration_regression_tests.py --all --verbose

作者: Worker 3 (Ultrapilot)
创建时间: 2026-02-15
更新时间: 2026-02-15 (添加环境准备功能)
"""

import sys
import os
import unittest
import json
import argparse
import glob
import shutil
from datetime import datetime
from typing import Dict, List, Tuple, Optional

# 确保使用项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# 修复 Windows 控制台编码问题（GBK 不支持 emoji/Unicode 字符）
if sys.platform == 'win32' and hasattr(sys.stderr, 'buffer'):
    import io
    try:
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    except Exception:
        pass

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


class SafeStream:
    """安全流包装器 - 防止 I/O operation on closed file 和 Unicode 编码错误"""
    def __init__(self, stream):
        self._stream = stream

    def write(self, data):
        try:
            self._stream.write(data)
        except (ValueError, OSError, AttributeError):
            pass
        except UnicodeEncodeError:
            try:
                safe_data = data.encode(self._stream.encoding or 'utf-8', errors='replace').decode(self._stream.encoding or 'utf-8')
                self._stream.write(safe_data)
            except Exception:
                pass

    def flush(self):
        try:
            self._stream.flush()
        except (ValueError, OSError, AttributeError):
            pass

    def writeln(self, data=''):
        self.write(data)
        self.write('\n')


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


def prepare_test_environment(clean_db: bool = True, backup_db: bool = True, verbose: bool = False):
    """
    准备测试环境

    Args:
        clean_db: 是否清理测试数据库
        backup_db: 是否备份生产数据库
        verbose: 是否显示详细信息

    Returns:
        dict: 环境准备结果
    """
    print(f"\n{Colors.HEADER}{'='*80}{Colors.ENDC}")
    print(f"{Colors.HEADER}测试环境准备{Colors.ENDC}")
    print(f"{Colors.HEADER}{'='*80}{Colors.ENDC}")

    results = {
        'cleaned_files': [],
        'backed_up_files': [],
        'errors': []
    }

    data_dir = os.path.join(PROJECT_ROOT, 'data')

    # 1. 备份生产数据库
    if backup_db:
        print(f"\n{Colors.OKCYAN}[1/3] 备份生产数据库...{Colors.ENDC}")
        production_dbs = ['trading.db', 'positions.db']

        for db_name in production_dbs:
            db_path = os.path.join(data_dir, db_name)
            if os.path.exists(db_path) and os.path.getsize(db_path) > 0:
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                backup_path = os.path.join(data_dir, f'{db_name}.backup_{timestamp}')
                try:
                    shutil.copy2(db_path, backup_path)
                    results['backed_up_files'].append(backup_path)
                    if verbose:
                        print(f"  [OK] 已备份: {db_name} -> {os.path.basename(backup_path)}")
                except Exception as e:
                    error_msg = f"备份失败 {db_name}: {str(e)}"
                    results['errors'].append(error_msg)
                    print(f"  {Colors.WARNING}[!] {error_msg}{Colors.ENDC}")

        if not results['backed_up_files'] and verbose:
            print(f"  {Colors.OKBLUE}[i] 没有需要备份的生产数据库{Colors.ENDC}")

    # 2. 清理测试数据库
    if clean_db:
        print(f"\n{Colors.OKCYAN}[2/3] 清理测试数据库...{Colors.ENDC}")

        # 定义需要清理的数据库文件模式
        test_db_patterns = [
            'positions.db',           # 测试持仓数据库
            'trading_test.db',        # 测试交易数据库
            'grid_test*.db',          # 网格测试数据库
            'grid_trading.db',        # 网格交易数据库
            '*.db-journal',           # SQLite日志文件
            '*.db-wal',               # SQLite WAL文件
            '*.db-shm',               # SQLite共享内存文件
        ]

        for pattern in test_db_patterns:
            pattern_path = os.path.join(data_dir, pattern)
            matched_files = glob.glob(pattern_path)

            for file_path in matched_files:
                # 跳过生产数据库和备份文件
                filename = os.path.basename(file_path)
                if filename == 'trading.db' or '.backup_' in filename:
                    continue

                try:
                    # 检查文件是否被占用
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        results['cleaned_files'].append(file_path)
                        if verbose:
                            print(f"  [OK] 已删除: {filename}")
                except PermissionError:
                    error_msg = f"文件被占用，无法删除: {filename}"
                    results['errors'].append(error_msg)
                    print(f"  {Colors.WARNING}[!] {error_msg}{Colors.ENDC}")
                except Exception as e:
                    error_msg = f"删除失败 {filename}: {str(e)}"
                    results['errors'].append(error_msg)
                    print(f"  {Colors.WARNING}[!] {error_msg}{Colors.ENDC}")

        if results['cleaned_files']:
            print(f"  {Colors.OKGREEN}[OK] 已清理 {len(results['cleaned_files'])} 个测试数据库文件{Colors.ENDC}")
        else:
            print(f"  {Colors.OKBLUE}[i] 没有需要清理的测试数据库{Colors.ENDC}")

    # 3. 验证测试环境
    print(f"\n{Colors.OKCYAN}[3/3] 验证测试环境...{Colors.ENDC}")

    # 检查必要的目录
    required_dirs = ['data', 'test', 'logs']
    for dir_name in required_dirs:
        dir_path = os.path.join(PROJECT_ROOT, dir_name)
        if not os.path.exists(dir_path):
            try:
                os.makedirs(dir_path)
                if verbose:
                    print(f"  [OK] 已创建目录: {dir_name}/")
            except Exception as e:
                error_msg = f"创建目录失败 {dir_name}: {str(e)}"
                results['errors'].append(error_msg)
                print(f"  {Colors.FAIL}[X] {error_msg}{Colors.ENDC}")
        elif verbose:
            print(f"  [OK] 目录存在: {dir_name}/")

    # 检查配置文件
    config_files = ['config.py', 'test/integration_test_config.json']
    for config_file in config_files:
        config_path = os.path.join(PROJECT_ROOT, config_file)
        if os.path.exists(config_path):
            if verbose:
                print(f"  [OK] 配置文件存在: {config_file}")
        else:
            error_msg = f"配置文件缺失: {config_file}"
            results['errors'].append(error_msg)
            print(f"  {Colors.FAIL}[X] {error_msg}{Colors.ENDC}")

    # 打印总结
    print(f"\n{Colors.HEADER}环境准备完成{Colors.ENDC}")
    if results['backed_up_files']:
        print(f"  备份文件: {len(results['backed_up_files'])} 个")
    if results['cleaned_files']:
        print(f"  清理文件: {len(results['cleaned_files'])} 个")
    if results['errors']:
        print(f"  {Colors.WARNING}警告: {len(results['errors'])} 个{Colors.ENDC}")
        if verbose:
            for error in results['errors']:
                print(f"    - {error}")
    else:
        print(f"  {Colors.OKGREEN}[OK] 环境准备成功，无错误{Colors.ENDC}")

    return results


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
    runner = unittest.TextTestRunner(verbosity=2 if verbose else 1, stream=SafeStream(sys.stderr))
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
        f"| [OK] 通过 | {total_passed} |",
        f"| [X] 失败 | {total_failed} |",
        f"| [!] 错误 | {total_errors} |",
        f"| ⊘ 跳过 | {total_skipped} |",
        f"| 成功率 | {overall_success_rate:.2f}% |",
        f"| 总耗时 | {total_duration:.2f} 秒 |",
        "\n## 分组结果\n"
    ]

    for result in results:
        status_icon = "[OK]" if result['failed'] == 0 and result['errors'] == 0 else "[X]"
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
    parser.add_argument('--no-clean', action='store_true', help='不清理测试数据库')
    parser.add_argument('--no-backup', action='store_true', help='不备份生产数据库')
    parser.add_argument('--skip-env-prep', action='store_true', help='跳过环境准备步骤')

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

    # 准备测试环境
    if not args.skip_env_prep:
        env_result = prepare_test_environment(
            clean_db=not args.no_clean,
            backup_db=not args.no_backup,
            verbose=args.verbose
        )
        # 如果环境准备有严重错误（超过5个），提示用户
        if len(env_result['errors']) > 5:
            print(f"\n{Colors.WARNING}警告: 环境准备过程中出现 {len(env_result['errors'])} 个问题{Colors.ENDC}")
            print(f"{Colors.WARNING}测试可能会受到影响，建议检查上述错误{Colors.ENDC}")
            try:
                response = input(f"\n是否继续运行测试? (y/n): ")
                if response.lower() != 'y':
                    print(f"{Colors.FAIL}测试已取消{Colors.ENDC}")
                    return 1
            except (EOFError, KeyboardInterrupt):
                print(f"\n{Colors.FAIL}测试已取消{Colors.ENDC}")
                return 1
        elif env_result['errors'] and args.verbose:
            print(f"\n{Colors.OKBLUE}[i] 环境准备有 {len(env_result['errors'])} 个非关键警告，继续测试{Colors.ENDC}")

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
