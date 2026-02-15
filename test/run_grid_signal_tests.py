"""
网格交易信号检测 - 测试运行器

运行所有信号检测测试并生成报告

使用虚拟环境: C:\\Users\\PC\\Anaconda3\\envs\\python39
"""

import sys
import os
import json
import subprocess
from datetime import datetime

# 测试文件列表
TEST_FILES = [
    'test/test_grid_signal_price_tracker.py',
    'test/test_grid_signal_crossing.py',
    'test/test_grid_signal_callback.py',
    'test/test_grid_signal_integration.py',
]

# Python 解释器路径
PYTHON = r'C:\Users\PC\Anaconda3\envs\python39\python.exe'

def run_test_file(test_file):
    """运行单个测试文件"""
    print(f"\n{'='*80}")
    print(f"运行测试: {test_file}")
    print(f"{'='*80}")

    try:
        result = subprocess.run(
            [PYTHON, test_file],
            capture_output=True,
            text=True,
            timeout=60
        )

        return {
            'file': test_file,
            'returncode': result.returncode,
            'stdout': result.stdout,
            'stderr': result.stderr,
            'success': result.returncode == 0
        }
    except subprocess.TimeoutExpired:
        return {
            'file': test_file,
            'returncode': -1,
            'stdout': '',
            'stderr': 'TIMEOUT: 测试超时(60秒)',
            'success': False
        }
    except Exception as e:
        return {
            'file': test_file,
            'returncode': -1,
            'stdout': '',
            'stderr': f'ERROR: {str(e)}',
            'success': False
        }

def generate_report(results):
    """生成测试报告"""
    report = {
        'timestamp': datetime.now().isoformat(),
        'python_interpreter': PYTHON,
        'total_tests': len(results),
        'passed': sum(1 for r in results if r['success']),
        'failed': sum(1 for r in results if not r['success']),
        'results': results
    }

    # 保存JSON报告
    report_file = 'test/grid_signal_test_report.json'
    with open(report_file, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*80}")
    print("测试报告")
    print(f"{'='*80}")
    print(f"总测试数: {report['total_tests']}")
    print(f"通过: {report['passed']}")
    print(f"失败: {report['failed']}")
    print(f"\n报告已保存到: {report_file}")

    # 打印详细结果
    for r in results:
        status = "✓ PASS" if r['success'] else "✗ FAIL"
        print(f"\n{status}: {r['file']}")
        if not r['success']:
            print(f"  错误: {r['stderr'][:200]}")

    return report

def main():
    """主函数"""
    print("=" * 80)
    print("网格交易信号检测测试套件")
    print("=" * 80)
    print(f"Python 解释器: {PYTHON}")
    print(f"测试文件数: {len(TEST_FILES)}")

    results = []
    for test_file in TEST_FILES:
        result = run_test_file(test_file)
        results.append(result)

        # 实时显示结果
        if result['success']:
            print(f"✓ {test_file} - PASS")
        else:
            print(f"✗ {test_file} - FAIL")

    # 生成报告
    report = generate_report(results)

    # 返回退出码
    sys.exit(0 if report['failed'] == 0 else 1)

if __name__ == '__main__':
    main()
