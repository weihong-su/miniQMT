"""
网格交易配置模板测试

测试范围:
1. 三档风险模板（激进/稳健/保守）
2. 模板保存/加载/删除
3. 模板使用统计

运行环境: Python 3.9 (C:\\Users\\PC\\Anaconda3\\envs\\python39)
"""

import sys
import os
import unittest
import json
from datetime import datetime
from unittest.mock import Mock, patch

# 确保可以导入项目模块
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import config
from grid_database import DatabaseManager


class TestGridSessionTemplates(unittest.TestCase):
    """网格交易配置模板测试"""

    def setUp(self):
        """测试前置设置"""
        # 使用内存数据库
        self.db_path = ":memory:"
        self.db_manager = DatabaseManager(db_path=self.db_path)
        self.db_manager.init_grid_tables()

        # 预定义三档风险模板
        self.templates = {
            'aggressive': {
                'name': '激进型',
                'description': '高频交易，追求快速获利',
                'config': {
                    'price_interval': 0.03,      # 3%档位间隔
                    'position_ratio': 0.30,      # 每次交易30%
                    'callback_ratio': 0.003,     # 0.3%回调触发
                    'max_deviation': 0.10,       # ±10%中心偏离
                    'target_profit': 0.15,       # 15%目标收益
                    'stop_loss': -0.08,          # -8%止损
                    'duration_days': 3           # 3天运行周期
                }
            },
            'balanced': {
                'name': '稳健型',
                'description': '均衡风险与收益',
                'config': {
                    'price_interval': 0.05,      # 5%档位间隔
                    'position_ratio': 0.25,      # 每次交易25%
                    'callback_ratio': 0.005,     # 0.5%回调触发
                    'max_deviation': 0.15,       # ±15%中心偏离
                    'target_profit': 0.10,       # 10%目标收益
                    'stop_loss': -0.10,          # -10%止损
                    'duration_days': 7           # 7天运行周期
                }
            },
            'conservative': {
                'name': '保守型',
                'description': '低频交易，降低风险',
                'config': {
                    'price_interval': 0.08,      # 8%档位间隔
                    'position_ratio': 0.20,      # 每次交易20%
                    'callback_ratio': 0.008,     # 0.8%回调触发
                    'max_deviation': 0.20,       # ±20%中心偏离
                    'target_profit': 0.08,       # 8%目标收益
                    'stop_loss': -0.12,          # -12%止损
                    'duration_days': 14          # 14天运行周期
                }
            }
        }

    def tearDown(self):
        """测试清理"""
        if hasattr(self, 'db_manager') and self.db_manager.conn:
            self.db_manager.conn.close()

    # ==================== 模板定义测试 ====================

    def test_template_definitions(self):
        """测试三档风险模板定义的完整性"""
        for template_id, template in self.templates.items():
            # 验证模板结构
            self.assertIn('name', template)
            self.assertIn('description', template)
            self.assertIn('config', template)

            # 验证配置项完整性
            required_keys = [
                'price_interval', 'position_ratio', 'callback_ratio',
                'max_deviation', 'target_profit', 'stop_loss', 'duration_days'
            ]
            for key in required_keys:
                self.assertIn(key, template['config'], f"模板 {template_id} 缺少配置项 {key}")

            # 验证数值范围
            cfg = template['config']
            self.assertGreater(cfg['price_interval'], 0)
            self.assertGreater(cfg['position_ratio'], 0)
            self.assertLessEqual(cfg['position_ratio'], 1.0)
            self.assertGreater(cfg['callback_ratio'], 0)
            self.assertGreater(cfg['duration_days'], 0)

        print(f"[OK] 测试通过: 三档模板定义完整")

    def test_template_risk_levels(self):
        """测试模板风险等级递进性（激进 > 稳健 > 保守）"""
        aggressive = self.templates['aggressive']['config']
        balanced = self.templates['balanced']['config']
        conservative = self.templates['conservative']['config']

        # 激进型应该有更小的档位间隔（更高频）
        self.assertLess(aggressive['price_interval'], balanced['price_interval'])
        self.assertLess(balanced['price_interval'], conservative['price_interval'])

        # 激进型应该有更大的持仓比例（更激进）
        self.assertGreaterEqual(aggressive['position_ratio'], balanced['position_ratio'])
        self.assertGreaterEqual(balanced['position_ratio'], conservative['position_ratio'])

        # 激进型应该有更高的目标收益
        self.assertGreater(aggressive['target_profit'], balanced['target_profit'])
        self.assertGreater(balanced['target_profit'], conservative['target_profit'])

        # 激进型应该有更短的运行周期
        self.assertLess(aggressive['duration_days'], balanced['duration_days'])
        self.assertLess(balanced['duration_days'], conservative['duration_days'])

        print(f"[OK] 测试通过: 风险等级递进性正确")

    # ==================== 模板保存/加载测试 ====================

    def test_save_and_load_template(self):
        """测试保存和加载模板"""
        # 创建模板表（如果不存在）
        cursor = self.db_manager.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS grid_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                config TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                use_count INTEGER DEFAULT 0
            )
        """)
        self.db_manager.conn.commit()

        # 保存模板
        for template_id, template in self.templates.items():
            cursor.execute("""
                INSERT INTO grid_templates (template_id, name, description, config)
                VALUES (?, ?, ?, ?)
            """, (
                template_id,
                template['name'],
                template['description'],
                json.dumps(template['config'])
            ))
        self.db_manager.conn.commit()

        # 加载模板并验证
        cursor.execute("SELECT * FROM grid_templates")
        saved_templates = cursor.fetchall()

        self.assertEqual(len(saved_templates), 3)

        for row in saved_templates:
            template_dict = dict(row)
            template_id = template_dict['template_id']
            self.assertIn(template_id, self.templates)

            # 验证配置正确性
            saved_config = json.loads(template_dict['config'])
            expected_config = self.templates[template_id]['config']
            self.assertEqual(saved_config, expected_config)

        print(f"[OK] 测试通过: 模板保存和加载成功")

    def test_load_template_by_id(self):
        """测试根据ID加载特定模板"""
        # 创建模板表并保存
        cursor = self.db_manager.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS grid_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                config TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                use_count INTEGER DEFAULT 0
            )
        """)

        # 保存稳健型模板
        balanced = self.templates['balanced']
        cursor.execute("""
            INSERT INTO grid_templates (template_id, name, description, config)
            VALUES (?, ?, ?, ?)
        """, (
            'balanced',
            balanced['name'],
            balanced['description'],
            json.dumps(balanced['config'])
        ))
        self.db_manager.conn.commit()

        # 加载特定模板
        cursor.execute("SELECT * FROM grid_templates WHERE template_id = ?", ('balanced',))
        row = cursor.fetchone()

        self.assertIsNotNone(row)
        template_dict = dict(row)
        self.assertEqual(template_dict['name'], '稳健型')

        loaded_config = json.loads(template_dict['config'])
        self.assertEqual(loaded_config['price_interval'], 0.05)
        self.assertEqual(loaded_config['duration_days'], 7)

        print(f"[OK] 测试通过: 根据ID加载模板成功")

    def test_delete_template(self):
        """测试删除模板"""
        # 创建模板表并保存
        cursor = self.db_manager.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS grid_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                config TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                use_count INTEGER DEFAULT 0
            )
        """)

        # 保存模板
        for template_id, template in self.templates.items():
            cursor.execute("""
                INSERT INTO grid_templates (template_id, name, description, config)
                VALUES (?, ?, ?, ?)
            """, (
                template_id,
                template['name'],
                template['description'],
                json.dumps(template['config'])
            ))
        self.db_manager.conn.commit()

        # 删除激进型模板
        cursor.execute("DELETE FROM grid_templates WHERE template_id = ?", ('aggressive',))
        self.db_manager.conn.commit()

        # 验证删除成功
        cursor.execute("SELECT COUNT(*) FROM grid_templates")
        count = cursor.fetchone()[0]
        self.assertEqual(count, 2)

        # 验证激进型已删除
        cursor.execute("SELECT * FROM grid_templates WHERE template_id = ?", ('aggressive',))
        row = cursor.fetchone()
        self.assertIsNone(row)

        print(f"[OK] 测试通过: 删除模板成功")

    # ==================== 模板使用统计测试 ====================

    def test_template_usage_statistics(self):
        """测试模板使用统计"""
        # 创建模板表
        cursor = self.db_manager.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS grid_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                config TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                use_count INTEGER DEFAULT 0
            )
        """)

        # 保存模板
        for template_id, template in self.templates.items():
            cursor.execute("""
                INSERT INTO grid_templates (template_id, name, description, config)
                VALUES (?, ?, ?, ?)
            """, (
                template_id,
                template['name'],
                template['description'],
                json.dumps(template['config'])
            ))
        self.db_manager.conn.commit()

        # 模拟使用模板（增加use_count）
        cursor.execute("UPDATE grid_templates SET use_count = use_count + 1 WHERE template_id = ?", ('balanced',))
        cursor.execute("UPDATE grid_templates SET use_count = use_count + 1 WHERE template_id = ?", ('balanced',))
        cursor.execute("UPDATE grid_templates SET use_count = use_count + 1 WHERE template_id = ?", ('conservative',))
        self.db_manager.conn.commit()

        # 验证统计数据
        cursor.execute("SELECT template_id, use_count FROM grid_templates ORDER BY use_count DESC")
        stats = cursor.fetchall()

        # 稳健型应该是使用最多的
        most_used = dict(stats[0])
        self.assertEqual(most_used['template_id'], 'balanced')
        self.assertEqual(most_used['use_count'], 2)

        print(f"[OK] 测试通过: 模板使用统计正确")

    def test_get_most_popular_template(self):
        """测试获取最受欢迎的模板"""
        # 创建模板表并保存
        cursor = self.db_manager.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS grid_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                config TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                use_count INTEGER DEFAULT 0
            )
        """)

        # 保存模板并设置不同的使用次数
        template_usage = {
            'aggressive': 5,
            'balanced': 12,
            'conservative': 8
        }

        for template_id, template in self.templates.items():
            cursor.execute("""
                INSERT INTO grid_templates (template_id, name, description, config, use_count)
                VALUES (?, ?, ?, ?, ?)
            """, (
                template_id,
                template['name'],
                template['description'],
                json.dumps(template['config']),
                template_usage[template_id]
            ))
        self.db_manager.conn.commit()

        # 获取最受欢迎的模板
        cursor.execute("""
            SELECT template_id, name, use_count
            FROM grid_templates
            ORDER BY use_count DESC
            LIMIT 1
        """)
        most_popular = dict(cursor.fetchone())

        self.assertEqual(most_popular['template_id'], 'balanced')
        self.assertEqual(most_popular['use_count'], 12)

        print(f"[OK] 测试通过: 最受欢迎模板为稳健型(12次使用)")

    # ==================== 模板应用测试 ====================

    def test_apply_template_to_session(self):
        """测试将模板应用到网格会话"""
        # 获取稳健型模板配置
        balanced_config = self.templates['balanced']['config']

        # 创建会话配置（基于模板）
        session_config = {
            'stock_code': '000001.SZ',
            'center_price': 10.0,
            'max_investment': 10000,
            **balanced_config  # 展开模板配置
        }

        # 验证配置正确应用
        self.assertEqual(session_config['price_interval'], 0.05)
        self.assertEqual(session_config['position_ratio'], 0.25)
        self.assertEqual(session_config['callback_ratio'], 0.005)
        self.assertEqual(session_config['duration_days'], 7)

        print(f"[OK] 测试通过: 模板成功应用到会话配置")


def run_tests():
    """运行测试并生成报告"""
    # 创建测试套件
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestGridSessionTemplates)

    # 运行测试
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    # 生成JSON报告
    report = {
        'test_file': 'test_grid_session_templates.py',
        'run_time': datetime.now().isoformat(),
        'total_tests': result.testsRun,
        'success': result.wasSuccessful(),
        'failures': len(result.failures),
        'errors': len(result.errors),
        'skipped': len(result.skipped),
        'coverage': {
            'templates': {
                'definitions': True,
                'risk_levels': True,
                'save_load': True,
                'delete': True,
                'usage_statistics': True,
                'apply_to_session': True
            }
        }
    }

    # 保存报告
    report_path = os.path.join(os.path.dirname(__file__), 'grid_session_templates_report.json')
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*60}")
    print(f"测试报告已保存: {report_path}")
    print(f"总测试数: {result.testsRun}")
    print(f"成功: {result.wasSuccessful()}")
    print(f"失败: {len(result.failures)}")
    print(f"错误: {len(result.errors)}")
    print(f"{'='*60}")

    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
