"""
Main Test Runner - Execute all regression tests

Runs all test modules in sequence and generates comprehensive report
"""

import unittest
import sys
import os
import time
from datetime import datetime
import json

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from logger import get_logger

logger = get_logger("test_runner")


# Test execution order (by dependency)
TEST_MODULES = [
    ('Config Management', 'test_config_management'),
    ('QMT Connection', 'test_qmt_connection'),
    # Additional modules will be loaded dynamically
]


def discover_test_modules():
    """
    Discover all test modules in test directory

    Returns:
        list: List of (test_name, module_name) tuples
    """
    test_dir = os.path.dirname(os.path.abspath(__file__))
    modules = []

    for filename in os.listdir(test_dir):
        if filename.startswith('test_') and filename.endswith('.py'):
            if filename not in ['test_base.py', 'test_utils.py',
                              'test_mocks.py', 'test_config.py']:
                module_name = filename[:-3]  # Remove .py
                # Convert to readable name
                test_name = module_name.replace('test_', '').replace('_', ' ').title()
                modules.append((test_name, module_name))

    return sorted(modules, key=lambda x: x[1])


def run_single_test(module_name):
    """
    Run a single test module

    Args:
        module_name: Name of the test module

    Returns:
        dict: Test result
    """
    logger.info(f"Running test module: {module_name}")
    start_time = time.time()

    try:
        # Import module dynamically
        module = __import__(module_name)

        # Load tests
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromModule(module)

        # Run tests
        runner = unittest.TextTestRunner(verbosity=1, stream=open(os.devnull, 'w'))
        result = runner.run(suite)

        duration = time.time() - start_time

        # Collect results
        test_result = {
            'module': module_name,
            'status': 'PASS' if result.wasSuccessful() else 'FAIL',
            'duration': round(duration, 2),
            'total': result.testsRun,
            'passed': result.testsRun - len(result.failures) - len(result.errors),
            'failed': len(result.failures),
            'errors': len(result.errors),
            'skipped': len(result.skipped),
            'failures': [str(f[1]) for f in result.failures],
            'error_details': [str(e[1]) for e in result.errors]
        }

        return test_result

    except Exception as e:
        duration = time.time() - start_time
        logger.error(f"Failed to run {module_name}: {str(e)}")

        return {
            'module': module_name,
            'status': 'ERROR',
            'duration': round(duration, 2),
            'total': 0,
            'passed': 0,
            'failed': 0,
            'errors': 1,
            'skipped': 0,
            'failures': [],
            'error_details': [str(e)]
        }


def print_test_header():
    """Print test suite header"""
    print("=" * 70)
    print(" " * 15 + "miniQMT Regression Test Suite")
    print("=" * 70)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Python: {sys.version.split()[0]}")
    print("=" * 70)
    print()


def print_test_progress(test_num, total_tests, test_name, status, duration):
    """
    Print test progress

    Args:
        test_num: Current test number
        total_tests: Total number of tests
        test_name: Name of the test
        status: Test status (PASS/FAIL/SKIP/ERROR)
        duration: Test duration in seconds
    """
    status_symbols = {
        'PASS': '[PASS]',
        'FAIL': '[FAIL]',
        'SKIP': '[SKIP]',
        'ERROR': '[ERROR]'
    }

    symbol = status_symbols.get(status, '[UNKNOWN]')
    padding = '.' * (50 - len(test_name))

    print(f"[{test_num:2d}/{total_tests:2d}] {test_name} {padding} {symbol} ({duration:.1f}s)")


def print_summary(results, total_duration):
    """
    Print test summary

    Args:
        results: List of test results
        total_duration: Total execution time
    """
    print()
    print("=" * 70)
    print(" " * 25 + "TEST SUMMARY")
    print("=" * 70)

    total_tests = len(results)
    passed = sum(1 for r in results if r['status'] == 'PASS')
    failed = sum(1 for r in results if r['status'] == 'FAIL')
    errors = sum(1 for r in results if r['status'] == 'ERROR')
    skipped = sum(1 for r in results if r['status'] == 'SKIP')

    print(f"Total Tests: {total_tests}")
    print(f"  PASSED:  {passed}")
    print(f"  FAILED:  {failed}")
    print(f"  ERRORS:  {errors}")
    print(f"  SKIPPED: {skipped}")
    print()
    print(f"Total Duration: {total_duration:.2f}s")
    print("=" * 70)

    # Print failures if any
    if failed > 0 or errors > 0:
        print()
        print("FAILURES AND ERRORS:")
        print("-" * 70)

        for result in results:
            if result['status'] in ['FAIL', 'ERROR']:
                print(f"\n{result['module']}:")
                if result['failures']:
                    for failure in result['failures']:
                        print(f"  FAILURE: {failure[:200]}")
                if result['error_details']:
                    for error in result['error_details']:
                        print(f"  ERROR: {error[:200]}")

        print("-" * 70)


def save_json_report(results, total_duration):
    """
    Save test results as JSON report

    Args:
        results: List of test results
        total_duration: Total execution time

    Returns:
        str: Path to saved report
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_dir = os.path.join(os.path.dirname(__file__), 'reports')

    # Ensure reports directory exists
    os.makedirs(report_dir, exist_ok=True)

    report_path = os.path.join(report_dir, f"test_report_{timestamp}.json")

    report = {
        'test_run_id': timestamp,
        'start_time': datetime.now().isoformat(),
        'python_version': sys.version,
        'python_env': sys.executable,
        'total_duration': round(total_duration, 2),
        'summary': {
            'total': len(results),
            'passed': sum(1 for r in results if r['status'] == 'PASS'),
            'failed': sum(1 for r in results if r['status'] == 'FAIL'),
            'errors': sum(1 for r in results if r['status'] == 'ERROR'),
            'skipped': sum(1 for r in results if r['status'] == 'SKIP')
        },
        'tests': results
    }

    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    logger.info(f"JSON report saved: {report_path}")
    return report_path


def main():
    """Main test execution"""
    print_test_header()

    # Discover test modules
    test_modules = discover_test_modules()
    total_tests = len(test_modules)

    logger.info(f"Discovered {total_tests} test modules")

    # Run tests
    results = []
    start_time = time.time()

    for i, (test_name, module_name) in enumerate(test_modules, 1):
        result = run_single_test(module_name)
        results.append({
            'name': test_name,
            **result
        })

        print_test_progress(
            i, total_tests,
            test_name,
            result['status'],
            result['duration']
        )

    total_duration = time.time() - start_time

    # Print summary
    print_summary(results, total_duration)

    # Save JSON report
    report_path = save_json_report(results, total_duration)
    print()
    print(f"Report saved: {report_path}")
    print()

    # Determine exit code
    all_passed = all(r['status'] in ['PASS', 'SKIP'] for r in results)
    return 0 if all_passed else 1


if __name__ == '__main__':
    exit_code = main()
    sys.exit(exit_code)
