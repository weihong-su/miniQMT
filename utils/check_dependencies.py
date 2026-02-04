#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
依赖包检查脚本
验证所有必需的Python包是否已正确安装

使用方式:
  从项目根目录运行: python utils/check_dependencies.py
  或从utils目录运行: python check_dependencies.py
"""

import sys
from typing import List, Tuple

def check_module(module_name: str, package_name: str = None) -> Tuple[bool, str]:
    """
    检查单个模块是否可导入

    Args:
        module_name: 模块名称
        package_name: 包名称(如果与模块名不同)

    Returns:
        (是否成功, 版本号或错误信息)
    """
    try:
        module = __import__(module_name)
        version = getattr(module, '__version__', '未知版本')
        return True, version
    except ImportError as e:
        package = package_name or module_name
        return False, f"未安装(请运行: pip install {package})"

def main():
    """主检查流程"""
    print("=" * 60)
    print("miniQMT 依赖包检查")
    print("=" * 60)
    print()

    # 定义需要检查的依赖
    dependencies = [
        ('pandas', 'pandas'),
        ('numpy', 'numpy'),
        ('flask', 'Flask'),
        ('flask_cors', 'Flask-CORS'),
        ('xtquant', 'xtquant'),
        ('mootdx', 'mootdx'),
        ('baostock', 'baostock'),
        ('marshmallow', 'marshmallow'),
        ('requests', 'requests'),
    ]

    results = []
    all_ok = True

    print("检查核心依赖包:")
    print("-" * 60)

    for module_name, package_name in dependencies:
        success, info = check_module(module_name, package_name)
        status = "✓ OK " if success else "✗ FAIL"
        color = "\033[92m" if success else "\033[91m"  # 绿色/红色
        reset = "\033[0m"

        print(f"{color}{status}{reset} {package_name:20s} {info}")
        results.append((package_name, success, info))

        if not success:
            all_ok = False

    print("-" * 60)
    print()

    # 统计结果
    success_count = sum(1 for _, success, _ in results if success)
    total_count = len(results)

    print(f"检查完成: {success_count}/{total_count} 个包已正确安装")
    print()

    if all_ok:
        print("\033[92m✓ 所有依赖包检查通过!\033[0m")
        print()
        print("下一步:")
        print("1. 配置 account_config.json 文件")
        print("2. 运行 python main.py 启动系统")
        print("3. 访问 http://localhost:5000 查看Web界面")
        return 0
    else:
        print("\033[91m✗ 存在缺失的依赖包\033[0m")
        print()
        print("修复方法:")
        print("1. 安装所有依赖: pip install -r utils/requirements.txt")
        print("2. 或单独安装缺失的包:")
        for package, success, info in results:
            if not success:
                print(f"   pip install {package}")
        return 1

if __name__ == '__main__':
    sys.exit(main())
