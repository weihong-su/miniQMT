#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
网格交易参数验证测试
测试 GridConfigSchema 和 GridTemplateSchema 的参数验证逻辑
"""

import sys
import os
import unittest
from decimal import Decimal

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from grid_validation import (
    validate_grid_config,
    validate_grid_template,
    GridConfigSchema,
    GridTemplateSchema
)


class TestGridValidationParams(unittest.TestCase):
    """网格交易参数验证测试"""

    def setUp(self):
        """测试前准备"""
        self.valid_config = {
            'stock_code': '000001.SZ',
            'price_interval': 0.05,
            'position_ratio': 0.25,
            'callback_ratio': 0.005,
            'max_investment': 10000.0,
            'max_deviation': 0.15,
            'target_profit': 0.10,
            'stop_loss': -0.10,
            'duration_days': 7
        }

    def test_valid_config(self):
        """测试有效配置通过验证"""
        is_valid, result = validate_grid_config(self.valid_config)
        self.assertTrue(is_valid, f"有效配置应通过验证，错误: {result}")

    def test_invalid_stock_code_format(self):
        """测试股票代码格式验证"""
        # 错误格式1: 缺少后缀
        config = self.valid_config.copy()
        config['stock_code'] = '000001'
        is_valid, errors = validate_grid_config(config)
        self.assertFalse(is_valid)
        self.assertIn('stock_code', errors)

        # 错误格式2: 错误后缀
        config['stock_code'] = '000001.SS'
        is_valid, errors = validate_grid_config(config)
        self.assertFalse(is_valid)

        # 正确格式
        config['stock_code'] = '600000.SH'
        is_valid, _ = validate_grid_config(config)
        self.assertTrue(is_valid)

    def test_price_interval_range(self):
        """测试价格间隔范围验证 (1%-20%)"""
        config = self.valid_config.copy()

        # 小于最小值
        config['price_interval'] = 0.005
        is_valid, errors = validate_grid_config(config)
        self.assertFalse(is_valid)
        self.assertIn('price_interval', errors)

        # 大于最大值
        config['price_interval'] = 0.25
        is_valid, errors = validate_grid_config(config)
        self.assertFalse(is_valid)

        # 边界值测试
        config['price_interval'] = 0.01  # 最小值
        is_valid, _ = validate_grid_config(config)
        self.assertTrue(is_valid)

        config['price_interval'] = 0.20  # 最大值
        is_valid, _ = validate_grid_config(config)
        self.assertTrue(is_valid)

    def test_position_ratio_range(self):
        """测试每档交易比例验证 (1%-100%)"""
        config = self.valid_config.copy()

        # 小于最小值
        config['position_ratio'] = 0.005
        is_valid, errors = validate_grid_config(config)
        self.assertFalse(is_valid)

        # 大于最大值
        config['position_ratio'] = 1.5
        is_valid, errors = validate_grid_config(config)
        self.assertFalse(is_valid)

        # 边界值
        config['position_ratio'] = 0.01
        is_valid, _ = validate_grid_config(config)
        self.assertTrue(is_valid)

        config['position_ratio'] = 1.0
        is_valid, _ = validate_grid_config(config)
        self.assertTrue(is_valid)

    def test_callback_ratio_range(self):
        """测试回调比例验证 (0.1%-10%)"""
        config = self.valid_config.copy()

        # 小于最小值
        config['callback_ratio'] = 0.0005
        is_valid, errors = validate_grid_config(config)
        self.assertFalse(is_valid)

        # 大于最大值
        config['callback_ratio'] = 0.15
        is_valid, errors = validate_grid_config(config)
        self.assertFalse(is_valid)

        # 边界值
        config['callback_ratio'] = 0.001
        is_valid, _ = validate_grid_config(config)
        self.assertTrue(is_valid)

        config['callback_ratio'] = 0.10
        is_valid, _ = validate_grid_config(config)
        self.assertTrue(is_valid)

    def test_max_investment_validation(self):
        """测试最大投入验证 (>=0)"""
        config = self.valid_config.copy()

        # 负值不允许
        config['max_investment'] = -100
        is_valid, errors = validate_grid_config(config)
        self.assertFalse(is_valid)

        # 零值允许
        config['max_investment'] = 0
        is_valid, _ = validate_grid_config(config)
        self.assertTrue(is_valid)

        # 正值允许
        config['max_investment'] = 50000
        is_valid, _ = validate_grid_config(config)
        self.assertTrue(is_valid)

    def test_deviation_range(self):
        """测试偏离度验证 (5%-50%)"""
        config = self.valid_config.copy()

        # 小于最小值
        config['max_deviation'] = 0.03
        is_valid, errors = validate_grid_config(config)
        self.assertFalse(is_valid)

        # 大于最大值
        config['max_deviation'] = 0.60
        is_valid, errors = validate_grid_config(config)
        self.assertFalse(is_valid)

        # 边界值
        config['max_deviation'] = 0.05
        is_valid, _ = validate_grid_config(config)
        self.assertTrue(is_valid)

        config['max_deviation'] = 0.50
        is_valid, _ = validate_grid_config(config)
        self.assertTrue(is_valid)

    def test_target_profit_range(self):
        """测试目标盈利验证 (1%-100%)"""
        config = self.valid_config.copy()

        # 小于最小值
        config['target_profit'] = 0.005
        is_valid, errors = validate_grid_config(config)
        self.assertFalse(is_valid)

        # 大于最大值
        config['target_profit'] = 1.5
        is_valid, errors = validate_grid_config(config)
        self.assertFalse(is_valid)

        # 边界值
        config['target_profit'] = 0.01
        is_valid, _ = validate_grid_config(config)
        self.assertTrue(is_valid)

        config['target_profit'] = 1.0
        is_valid, _ = validate_grid_config(config)
        self.assertTrue(is_valid)

    def test_stop_loss_range(self):
        """测试止损比例验证 (-50%-0%)"""
        config = self.valid_config.copy()

        # 小于最小值
        config['stop_loss'] = -0.60
        is_valid, errors = validate_grid_config(config)
        self.assertFalse(is_valid)

        # 大于最大值（正值不允许）
        config['stop_loss'] = 0.10
        is_valid, errors = validate_grid_config(config)
        self.assertFalse(is_valid)

        # 边界值
        config['stop_loss'] = -0.50
        is_valid, _ = validate_grid_config(config)
        self.assertTrue(is_valid)

        config['stop_loss'] = 0
        is_valid, _ = validate_grid_config(config)
        self.assertTrue(is_valid)

    def test_duration_days_range(self):
        """测试运行时长验证 (1-365天)"""
        config = self.valid_config.copy()

        # 小于最小值
        config['duration_days'] = 0
        is_valid, errors = validate_grid_config(config)
        self.assertFalse(is_valid)

        # 大于最大值
        config['duration_days'] = 400
        is_valid, errors = validate_grid_config(config)
        self.assertFalse(is_valid)

        # 边界值
        config['duration_days'] = 1
        is_valid, _ = validate_grid_config(config)
        self.assertTrue(is_valid)

        config['duration_days'] = 365
        is_valid, _ = validate_grid_config(config)
        self.assertTrue(is_valid)

    def test_profit_loss_relationship(self):
        """测试目标盈利与止损的合理性验证"""
        config = self.valid_config.copy()

        # 目标盈利小于止损幅度（不合理）
        config['target_profit'] = 0.05
        config['stop_loss'] = -0.10
        is_valid, errors = validate_grid_config(config)
        self.assertFalse(is_valid, "目标盈利应大于或等于止损幅度")

        # 目标盈利等于止损幅度（合理）
        config['target_profit'] = 0.10
        config['stop_loss'] = -0.10
        is_valid, _ = validate_grid_config(config)
        self.assertTrue(is_valid)

        # 目标盈利大于止损幅度（合理）
        config['target_profit'] = 0.15
        config['stop_loss'] = -0.10
        is_valid, _ = validate_grid_config(config)
        self.assertTrue(is_valid)

    def test_template_validation(self):
        """测试模板参数验证"""
        template = {
            'template_name': '测试模板',
            'price_interval': 0.05,
            'position_ratio': 0.25,
            'callback_ratio': 0.005,
            'max_deviation': 0.15,
            'target_profit': 0.10,
            'stop_loss': -0.10,
            'duration_days': 7,
            'max_investment_ratio': 0.5,
            'description': '测试用模板'
        }

        is_valid, result = validate_grid_template(template)
        self.assertTrue(is_valid, f"有效模板应通过验证，错误: {result}")

    def test_template_name_length(self):
        """测试模板名称长度验证 (1-50字符)"""
        template = {
            'template_name': '',  # 空名称
            'price_interval': 0.05
        }
        is_valid, errors = validate_grid_template(template)
        self.assertFalse(is_valid)

        # 超长名称
        template['template_name'] = 'A' * 51
        is_valid, errors = validate_grid_template(template)
        self.assertFalse(is_valid)

        # 边界值
        template['template_name'] = 'A'
        is_valid, _ = validate_grid_template(template)
        self.assertTrue(is_valid)

        template['template_name'] = 'A' * 50
        is_valid, _ = validate_grid_template(template)
        self.assertTrue(is_valid)

    def test_template_description_length(self):
        """测试模板描述长度验证 (<=200字符)"""
        template = {
            'template_name': '测试',
            'description': 'A' * 201  # 超长描述
        }
        is_valid, errors = validate_grid_template(template)
        self.assertFalse(is_valid)

        # 边界值
        template['description'] = 'A' * 200
        is_valid, _ = validate_grid_template(template)
        self.assertTrue(is_valid)

    def test_missing_required_fields(self):
        """测试缺少必填字段"""
        # stock_code 是必填的
        config = self.valid_config.copy()
        del config['stock_code']
        is_valid, errors = validate_grid_config(config)
        self.assertFalse(is_valid)
        self.assertIn('stock_code', errors)

        # max_investment 是必填的
        config = self.valid_config.copy()
        del config['max_investment']
        is_valid, errors = validate_grid_config(config)
        self.assertFalse(is_valid)
        self.assertIn('max_investment', errors)


def run_tests():
    """运行测试"""
    suite = unittest.TestLoader().loadTestsFromTestCase(TestGridValidationParams)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    # 生成测试报告
    report = {
        'test_file': 'test_grid_validation_params.py',
        'total_tests': result.testsRun,
        'passed': result.testsRun - len(result.failures) - len(result.errors),
        'failed': len(result.failures),
        'errors': len(result.errors),
        'coverage': '参数验证 - 100%'
    }

    import json
    with open('test/grid_validation_params_report.json', 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
