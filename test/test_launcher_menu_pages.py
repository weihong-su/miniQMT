# -*- coding: utf-8 -*-
"""
总控制台分页菜单测试。

菜单是交互式的，这里用管道喂输入跑真实子进程，验证页面渲染与导航路由 ——
比 mock 掉 print 更能反映用户实际看到的东西。
"""
import os
import subprocess
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(PROJECT_ROOT, 'scripts', '_launcher.py')


def run_menu(keys):
    """按顺序喂入按键，返回累积输出。"""
    proc = subprocess.run(
        [sys.executable, LAUNCHER, 'menu'],
        input="\n".join(keys) + "\n",
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        cwd=PROJECT_ROOT, timeout=90)
    return proc.stdout or ''


class TestMainPage(unittest.TestCase):
    """首页只放日常运行 + 三个二级入口，必须一屏装得下。"""

    @classmethod
    def setUpClass(cls):
        cls.out = run_menu(['q'])

    def test_shows_daily_operations(self):
        for token in ('[5] 账号配置', '[6] 运行状态',
                      '[7] 全部(实盘)', '[8] 全部(模拟)', '[9] 指定账号',
                      '[a] 全部(优雅)', '[b] 指定账号', '[c] 强制全部'):
            self.assertIn(token, self.out, f"首页应包含日常运行项 {token}")

    def test_shows_three_submenu_entries(self):
        for token in ('[1] 环境与部署', '[2] 服务管理', '[3] 数据与配置'):
            self.assertIn(token, self.out)

    def test_folded_items_not_on_main_page(self):
        """折叠进二级页的项不应再出现在首页。"""
        for token in ('[0] 首次部署向导', '[d] 启动 xtquant_manager',
                      '[j] 启动自动买入', '[n] Tushare', '[r] 数据库迁移'):
            self.assertNotIn(token, self.out, f"{token} 应已折叠到二级页")

    def test_main_page_fits_one_screen(self):
        """一屏约 30 行，首页必须装得下（含底部的运行状态摘要）。"""
        page = self.out.split('[警告]')[0]
        self.assertLessEqual(len(page.splitlines()), 30,
                             "首页超过 30 行会滚屏，与'一屏装下'的目标不符")

    def test_shows_runtime_status_summary(self):
        self.assertIn('账号状态:', self.out)
        self.assertIn('XtTrader 通道:', self.out)


class TestSubPageNavigation(unittest.TestCase):
    def test_env_page(self):
        out = run_menu(['1', 'b', 'q'])
        self.assertIn('总控制台 · 环境与部署', out)
        for token in ('[0] 首次部署向导', '[1] 检查 Python 环境', '[2] 安装/更新',
                      '[3] 检查配置文件', '[4] 拉取最新代码'):
            self.assertIn(token, out)

    def test_services_page(self):
        out = run_menu(['2', 'b', 'q'])
        self.assertIn('总控制台 · 服务管理', out)
        for token in ('[d] 启动 xtquant_manager', '[i] 查看 xtquant_manager 实时日志',
                      '[j] 启动自动买入服务', '[m] 查看自动买入日志'):
            self.assertIn(token, out)

    def test_data_page(self):
        out = run_menu(['3', 'b', 'q'])
        self.assertIn('总控制台 · 数据与配置', out)
        for token in ('[n] Tushare Pro', '[o] 大QMT IPC', '[p] XtTrader 通道总控',
                      '[r] 数据库迁移', '[s] 历史回填', '[t] 导入券商对账单',
                      '[u] 导出标准交割单'):
            self.assertIn(token, out)

    def test_b_returns_to_main_page(self):
        out = run_menu(['1', 'b', 'q'])
        # 返回后应再次看到首页的日常运行项
        self.assertGreaterEqual(out.count('[5] 账号配置'), 2,
                                "按 b 后应回到首页")

    def test_empty_input_returns_to_main_page(self):
        """回车也应返回，避免误按卡在二级页。"""
        out = run_menu(['1', '', 'q'])
        self.assertGreaterEqual(out.count('[5] 账号配置'), 2)

    def test_q_quits_from_sub_page(self):
        out = run_menu(['1', 'q'])
        self.assertIn('再见', out)

    def test_sub_page_shows_back_hint(self):
        out = run_menu(['1', 'b', 'q'])
        self.assertIn('[b] 返回主菜单', out)

    def test_sub_page_prompt_mentions_enter(self):
        out = run_menu(['1', 'b', 'q'])
        self.assertIn('回车=返回主菜单', out)


class TestInvalidKeys(unittest.TestCase):
    def test_invalid_key_on_main_page_warns_and_stays(self):
        out = run_menu(['z', 'q'])
        self.assertIn('无效选择', out)
        self.assertGreaterEqual(out.count('[5] 账号配置'), 2,
                                "无效按键后应仍停留在首页")

    def test_main_page_key_not_valid_on_sub_page(self):
        """首页的 [5] 在二级页无效，应提示而不是误执行。"""
        out = run_menu(['1', '5', 'b', 'q'])
        self.assertIn('无效选择', out)

    def test_quit_from_main_page(self):
        self.assertIn('再见', run_menu(['q']))


class TestExistingDispatchStillWorks(unittest.TestCase):
    """分页改造后，原有命令分派必须照常工作。"""

    def test_list_from_main_page(self):
        out = run_menu(['5', '', 'q'])
        self.assertIn('账号 ID', out)
        self.assertIn('QMT 路径', out)

    def test_status_from_main_page(self):
        out = run_menu(['6', '', 'q'])
        self.assertIn('账号 ID', out)
        self.assertIn('状态', out)


if __name__ == '__main__':
    unittest.main(verbosity=2)
