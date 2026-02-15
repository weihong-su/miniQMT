"""
网格交易退出条件综合测试运行器

功能:
1. 自动运行所有退出条件测试脚本
2. 收集各测试的结果
3. 生成综合测试报告
4. 统计覆盖率和通过率

测试脚本列表:
- test_grid_exit_deviation.py (偏离度退出)
- test_grid_exit_profit_loss.py (盈亏退出)
- test_grid_exit_time.py (时间退出)
- test_grid_exit_position_cleared.py (持仓清空退出)
- test_grid_exit_integration.py (集成测试)
"""

import subprocess
import sys
import os
import json
from datetime import datetime

# Python虚拟环境路径
PYTHON_ENV = r"C:\Users\PC\Anaconda3\envs\python39\python.exe"

# 测试脚本列表
TEST_SCRIPTS = [
    {
        'name': '偏离度退出测试',
        'script': 'test_grid_exit_deviation.py',
        'report': 'test_grid_exit_deviation_report.json'
    },
    {
        'name': '盈亏退出测试',
        'script': 'test_grid_exit_profit_loss.py',
        'report': 'test_grid_exit_profit_loss_report.json'
    },
    {
        'name': '时间退出测试',
        'script': 'test_grid_exit_time.py',
        'report': 'test_grid_exit_time_report.json'
    },
    {
        'name': '持仓清空退出测试',
        'script': 'test_grid_exit_position_cleared.py',
        'report': 'test_grid_exit_position_cleared_report.json'
    },
    {
        'name': '退出条件集成测试',
        'script': 'test_grid_exit_integration.py',
        'report': 'test_grid_exit_integration_report.json'
    }
]


def run_test(script_path):
    """运行单个测试脚本"""
    print(f"\n{'='*80}")
    print(f"运行测试: {os.path.basename(script_path)}")
    print(f"{'='*80}")

    try:
        # 使用虚拟环境运行测试
        result = subprocess.run(
            [PYTHON_ENV, script_path],
            cwd=os.path.dirname(script_path) or '.',
            capture_output=True,
            text=True,
            timeout=300  # 5分钟超时
        )

        print(result.stdout)
        if result.stderr:
            print("STDERR:", result.stderr)

        success = result.returncode == 0
        return success, result.stdout, result.stderr

    except subprocess.TimeoutExpired:
        print(f"✗ 测试超时 (>300秒)")
        return False, "", "测试超时"
    except Exception as e:
        print(f"✗ 测试执行失败: {str(e)}")
        return False, "", str(e)


def load_test_report(report_path):
    """加载测试报告"""
    if not os.path.exists(report_path):
        return None

    try:
        with open(report_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"✗ 加载报告失败 ({report_path}): {str(e)}")
        return None


def generate_comprehensive_report(test_results):
    """生成综合测试报告"""
    total_tests = sum(r['total_tests'] for r in test_results.values() if r)
    total_passed = sum(r['passed'] for r in test_results.values() if r)
    total_failed = sum(r['failed'] for r in test_results.values() if r)

    comprehensive_report = {
        'test_suite': '网格交易退出条件综合测试',
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'python_env': PYTHON_ENV,
        'summary': {
            'total_test_modules': len(TEST_SCRIPTS),
            'total_test_cases': total_tests,
            'total_passed': total_passed,
            'total_failed': total_failed,
            'pass_rate': f"{total_passed/total_tests*100:.2f}%" if total_tests > 0 else "0%"
        },
        'test_modules': []
    }

    # 添加各测试模块结果
    for test_info in TEST_SCRIPTS:
        report = test_results.get(test_info['name'])
        if report:
            comprehensive_report['test_modules'].append({
                'name': test_info['name'],
                'total_tests': report['total_tests'],
                'passed': report['passed'],
                'failed': report['failed'],
                'pass_rate': f"{report['passed']/report['total_tests']*100:.2f}%" if report['total_tests'] > 0 else "0%",
                'details': report.get('results', [])
            })
        else:
            comprehensive_report['test_modules'].append({
                'name': test_info['name'],
                'error': '未生成测试报告'
            })

    # 退出条件覆盖率
    comprehensive_report['coverage'] = {
        'exit_conditions': [
            '偏离度退出 (deviation)',
            '止盈退出 (target_profit)',
            '止损退出 (stop_loss)',
            '时间退出 (expired)',
            '持仓清空退出 (position_cleared)'
        ],
        'priority_order': [
            '1. 偏离度检测 (最高优先级)',
            '2. 盈亏检测',
            '3. 时间限制',
            '4. 持仓清空 (最低优先级)'
        ],
        'test_scenarios': [
            '单一条件触发',
            '多条件同时触发',
            '边界值测试',
            '配对操作检查',
            '数据清理验证',
            'stop_reason准确性'
        ]
    }

    return comprehensive_report


def main():
    """主函数"""
    print("="*80)
    print("网格交易退出条件综合测试")
    print("="*80)
    print(f"Python环境: {PYTHON_ENV}")
    print(f"测试脚本数量: {len(TEST_SCRIPTS)}")
    print(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # 运行所有测试
    test_results = {}
    execution_results = {}

    for test_info in TEST_SCRIPTS:
        script_path = os.path.join(os.path.dirname(__file__), test_info['script'])
        success, stdout, stderr = run_test(script_path)

        execution_results[test_info['name']] = {
            'success': success,
            'stdout': stdout,
            'stderr': stderr
        }

        # 加载测试报告
        report_path = os.path.join(os.path.dirname(__file__), test_info['report'])
        report = load_test_report(report_path)
        test_results[test_info['name']] = report

    # 生成综合报告
    comprehensive_report = generate_comprehensive_report(test_results)

    # 保存综合报告
    report_file = os.path.join(os.path.dirname(__file__), 'grid_exit_test_report.json')
    with open(report_file, 'w', encoding='utf-8') as f:
        json.dump(comprehensive_report, f, indent=2, ensure_ascii=False)

    # 打印总结
    print("\n" + "="*80)
    print("测试总结")
    print("="*80)
    print(f"测试模块数: {comprehensive_report['summary']['total_test_modules']}")
    print(f"总测试用例数: {comprehensive_report['summary']['total_test_cases']}")
    print(f"通过: {comprehensive_report['summary']['total_passed']}")
    print(f"失败: {comprehensive_report['summary']['total_failed']}")
    print(f"通过率: {comprehensive_report['summary']['pass_rate']}")
    print(f"\n综合报告已生成: {report_file}")
    print(f"完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*80)

    # 打印各模块详情
    print("\n模块详情:")
    for module in comprehensive_report['test_modules']:
        if 'error' in module:
            print(f"  ✗ {module['name']}: {module['error']}")
        else:
            status = "✓" if module['failed'] == 0 else "✗"
            print(f"  {status} {module['name']}: {module['passed']}/{module['total_tests']} 通过 ({module['pass_rate']})")

    # 检查整体成功率
    if comprehensive_report['summary']['total_failed'] == 0:
        print("\n🎉 所有测试通过!")
        return 0
    else:
        print(f"\n⚠️ 有 {comprehensive_report['summary']['total_failed']} 个测试失败")
        return 1


if __name__ == '__main__':
    exit_code = main()
    sys.exit(exit_code)
