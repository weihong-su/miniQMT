#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
网格交易完整测试套件运行器
一键运行所有21个测试文件，生成综合报告
"""

import sys
import os
import unittest
import json
from datetime import datetime

# 确保使用项目根目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 测试文件列表（按Worker分组）
TEST_FILES = {
    'Worker 1 - 会话管理': [
        'test.test_grid_session_lifecycle',
        'test.test_grid_session_recovery',
        'test.test_grid_session_templates',
    ],
    'Worker 2 - 信号检测': [
        'test.test_grid_signal_price_tracker',
        'test.test_grid_signal_crossing',
        'test.test_grid_signal_callback',
        'test.test_grid_signal_integration',
    ],
    'Worker 3 - 交易执行': [
        'test.test_grid_trade_buy',
        'test.test_grid_trade_sell',
        'test.test_grid_trade_fund_management',
        'test.test_grid_trade_statistics',
        'test.test_grid_trade_rebuild',
    ],
    'Worker 4 - 退出条件': [
        'test.test_grid_exit_deviation',
        'test.test_grid_exit_profit_loss',
        'test.test_grid_exit_time',
        'test.test_grid_exit_position_cleared',
        'test.test_grid_exit_integration',
    ],
    'Worker 5 - 验证边界': [
        'test.test_grid_validation_params',
        'test.test_grid_validation_edge_cases',
        'test.test_grid_validation_exceptions',
    ]
}


def run_all_tests(verbose=True):
    """运行所有网格交易测试"""
    print("=" * 80)
    print("网格交易完整测试套件")
    print("=" * 80)
    print(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # 创建测试套件
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    # 统计信息
    total_workers = len(TEST_FILES)
    total_files = sum(len(files) for files in TEST_FILES.values())
    loaded_files = 0
    failed_to_load = []

    # 按Worker加载测试
    for worker_name, test_modules in TEST_FILES.items():
        print(f"\n加载 {worker_name}...")
        for module_name in test_modules:
            try:
                module = __import__(module_name, fromlist=[''])
                suite.addTests(loader.loadTestsFromModule(module))
                loaded_files += 1
                print(f"  [OK] {module_name.split('.')[-1]}")
            except Exception as e:
                failed_to_load.append((module_name, str(e)))
                print(f"  [FAIL] {module_name.split('.')[-1]}: {str(e)}")

    print()
    print("-" * 80)
    print(f"测试文件加载: {loaded_files}/{total_files}")
    if failed_to_load:
        print(f"失败: {len(failed_to_load)} 个")
        for module_name, error in failed_to_load:
            print(f"  - {module_name}: {error}")
    print("-" * 80)
    print()

    # 运行测试
    runner = unittest.TextTestRunner(verbosity=2 if verbose else 1)
    start_time = datetime.now()
    result = runner.run(suite)
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()

    # 生成报告
    print()
    print("=" * 80)
    print("测试总结")
    print("=" * 80)
    print(f"总测试用例: {result.testsRun}")
    print(f"通过: {result.testsRun - len(result.failures) - len(result.errors)}")
    print(f"失败: {len(result.failures)}")
    print(f"错误: {len(result.errors)}")
    print(f"执行时长: {duration:.2f} 秒")
    print()

    # 保存JSON报告
    report = {
        'timestamp': datetime.now().isoformat(),
        'summary': {
            'total_workers': total_workers,
            'total_files': total_files,
            'loaded_files': loaded_files,
            'failed_to_load': len(failed_to_load),
            'total_tests': result.testsRun,
            'passed': result.testsRun - len(result.failures) - len(result.errors),
            'failed': len(result.failures),
            'errors': len(result.errors),
            'duration_seconds': duration,
            'success_rate': (result.testsRun - len(result.failures) - len(result.errors)) / result.testsRun * 100 if result.testsRun > 0 else 0
        },
        'failures': [
            {'test': str(test), 'traceback': traceback}
            for test, traceback in result.failures
        ],
        'errors': [
            {'test': str(test), 'traceback': traceback}
            for test, traceback in result.errors
        ],
        'load_failures': [
            {'module': module, 'error': error}
            for module, error in failed_to_load
        ]
    }

    report_path = 'test/grid_comprehensive_test_report.json'
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"详细报告已保存: {report_path}")
    print()

    # 返回状态
    return result.wasSuccessful()


if __name__ == '__main__':
    # 支持命令行参数
    verbose = '--verbose' in sys.argv or '-v' in sys.argv
    success = run_all_tests(verbose=verbose)
    sys.exit(0 if success else 1)
