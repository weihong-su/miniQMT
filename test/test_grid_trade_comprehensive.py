"""
网格交易执行测试报告生成器

运行所有网格交易执行测试并生成JSON报告
"""

import sys
import os
import json
import unittest
from datetime import datetime
from io import StringIO

# 添加项目路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# 导入测试模块
from test_grid_trade_buy import TestGridTradeBuy
from test_grid_trade_sell import TestGridTradeSell
from test_grid_trade_fund_management import TestGridTradeFundManagement
from test_grid_trade_statistics import TestGridTradeStatistics
from test_grid_trade_rebuild import TestGridTradeRebuild


def run_all_tests():
    """运行所有测试并收集结果"""
    print("=" * 80)
    print("网格交易执行测试套件")
    print("=" * 80)
    print()

    test_classes = [
        ('买入执行测试', TestGridTradeBuy),
        ('卖出执行测试', TestGridTradeSell),
        ('资金管理测试', TestGridTradeFundManagement),
        ('统计更新测试', TestGridTradeStatistics),
        ('网格重建测试', TestGridTradeRebuild),
    ]

    all_results = {}
    total_tests = 0
    total_passed = 0
    total_failed = 0
    total_errors = 0

    for test_name, test_class in test_classes:
        print(f"\n{'=' * 80}")
        print(f"运行: {test_name}")
        print('=' * 80)

        # 创建测试套件
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromTestCase(test_class)

        # 运行测试
        runner = unittest.TextTestRunner(verbosity=2)
        result = runner.run(suite)

        # 收集结果
        test_count = result.testsRun
        passed = test_count - len(result.failures) - len(result.errors)
        failed = len(result.failures)
        errors = len(result.errors)

        all_results[test_name] = {
            'total': test_count,
            'passed': passed,
            'failed': failed,
            'errors': errors,
            'success_rate': f"{(passed/test_count*100):.1f}%" if test_count > 0 else "0%",
            'failures': [
                {
                    'test': str(failure[0]),
                    'traceback': failure[1]
                } for failure in result.failures
            ],
            'errors': [
                {
                    'test': str(error[0]),
                    'traceback': error[1]
                } for error in result.errors
            ]
        }

        total_tests += test_count
        total_passed += passed
        total_failed += failed
        total_errors += errors

    # 生成总结
    summary = {
        'test_date': datetime.now().isoformat(),
        'total_test_cases': total_tests,
        'total_passed': total_passed,
        'total_failed': total_failed,
        'total_errors': total_errors,
        'overall_success_rate': f"{(total_passed/total_tests*100):.1f}%" if total_tests > 0 else "0%",
        'test_suites': all_results
    }

    return summary


def generate_report(summary):
    """生成测试报告"""
    print("\n\n" + "=" * 80)
    print("测试总结")
    print("=" * 80)

    print(f"\n测试时间: {summary['test_date']}")
    print(f"\n总测试数: {summary['total_test_cases']}")
    print(f"成功: {summary['total_passed']} ({summary['overall_success_rate']})")
    print(f"失败: {summary['total_failed']}")
    print(f"错误: {summary['total_errors']}")

    print("\n" + "-" * 80)
    print("各测试套件详情:")
    print("-" * 80)

    for suite_name, result in summary['test_suites'].items():
        status = "[OK] 通过" if result['failed'] == 0 and result['errors'] == 0 else "[FAIL] 失败"
        print(f"\n{suite_name}: {status}")
        print(f"  总数: {result['total']}, 通过: {result['passed']}, 失败: {result['failed']}, 错误: {result['errors']}")
        print(f"  成功率: {result['success_rate']}")

        # 显示失败详情
        if result['failures']:
            print(f"\n  失败测试:")
            for failure in result['failures']:
                print(f"    - {failure['test']}")

        if result['errors']:
            print(f"\n  错误测试:")
            for error in result['errors']:
                print(f"    - {error['test']}")

    print("\n" + "=" * 80)

    # 保存JSON报告
    report_path = os.path.join(os.path.dirname(__file__), 'grid_trade_test_report.json')
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"\nJSON报告已保存: {report_path}")

    return report_path


def main():
    """主函数"""
    try:
        # 运行所有测试
        summary = run_all_tests()

        # 生成报告
        report_path = generate_report(summary)

        # 返回退出码
        if summary['total_failed'] > 0 or summary['total_errors'] > 0:
            print("\n[WARN]  部分测试失败，请检查报告")
            return 1
        else:
            print("\n[OK] 所有测试通过")
            return 0

    except Exception as e:
        print(f"\n错误: 测试运行失败")
        print(f"异常信息: {str(e)}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    exit_code = main()
    sys.exit(exit_code)
