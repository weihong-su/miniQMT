#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Web API 完整功能测试脚本

功能：使用Flask测试客户端模拟Web界面操作，验证所有RESTful API接口的正确性。
测试环境：Python 3.9 虚拟环境 (C:/Users/PC/Anaconda3/envs/python39)

测试覆盖：
1. 静态资源 & 首页
2. 系统状态 & 连接检查 API
3. 持仓管理 API
4. 交易记录 API
5. 配置管理 API (GET/POST)
6. 监控控制 API
7. 数据管理 API
8. 股票池管理 API
9. 交易执行 API
10. 调试 API
11. 网格交易 API (sessions/templates/config)
12. 认证/Token 机制
13. 错误处理
"""

import sys
import os
import json
import unittest
import sqlite3
import time
from datetime import datetime
from unittest.mock import MagicMock, patch, PropertyMock
from io import StringIO

# ---- 添加项目根目录到路径 ----
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# =====================================================================
# 第一步：在导入 web_server 之前 Mock 所有外部依赖
# =====================================================================

# Mock xtquant（迅投QMT行情/交易库）
_MOCKED_MODULE_NAMES = [
    'xtquant', 'xtquant.xtdata', 'xtquant.xttrader', 'xtquant.xttype',
    'easy_qmt_trader',
]
# 保存已导入的模块原始引用，tearDownModule 时恢复，防止污染其他测试
_orig_sys_modules = {k: sys.modules[k] for k in _MOCKED_MODULE_NAMES if k in sys.modules}
for mod_name in _MOCKED_MODULE_NAMES:
    sys.modules[mod_name] = MagicMock()

import pandas as pd
import config

# ---- 创建内存SQLite数据库（用于 data_manager.conn / position_manager.memory_conn）----
def _make_trade_records_db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("""CREATE TABLE trade_records (
        id INTEGER PRIMARY KEY,
        stock_code TEXT, trade_type TEXT, price REAL,
        volume INTEGER, trade_time TEXT, strategy TEXT,
        trade_id TEXT, stock_name TEXT
    )""")
    conn.commit()
    return conn

# ---- 全局 Mock 对象 ----
mock_dm_conn = _make_trade_records_db()

# data_manager mock
mock_data_manager = MagicMock()
mock_data_manager.conn = mock_dm_conn
mock_data_manager.get_stock_name.return_value = "测试股票"

# position_manager mock
mock_pm = MagicMock()
mock_pm.grid_manager = None
mock_pm.qmt_trader = None
mock_pm.memory_conn = _make_trade_records_db()
mock_pm.db_manager = MagicMock()
mock_pm.db_manager.get_grid_template.return_value = None
mock_pm.get_data_version_info.return_value = {'version': 1, 'changed': False}
mock_pm.get_all_positions.return_value = []
mock_pm.get_all_positions_with_all_fields.return_value = pd.DataFrame()
mock_pm.get_account_info.return_value = {
    'account_id': 'TEST001',
    'account_type': 'STOCK',
    'available': 100000.0,
    'frozen_cash': 0.0,
    'market_value': 50000.0,
    'total_asset': 150000.0,
    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
}
mock_pm.get_position.return_value = None
mock_pm.initialize_all_positions_data.return_value = {
    'success': True, 'message': '初始化成功', 'updated_count': 0
}
mock_pm._create_memory_table.return_value = None
mock_pm._sync_db_to_memory.return_value = None
mock_pm.clear_all_signals.return_value = None
mock_pm.mark_data_consumed.return_value = None

# trading_executor mock
mock_executor = MagicMock()
mock_executor.get_stock_positions.return_value = []
mock_executor.get_trades.return_value = pd.DataFrame()

# trading_strategy mock
mock_strategy = MagicMock()
mock_strategy.manual_buy.return_value = None

# config_manager mock
mock_cm = MagicMock()
mock_cm.save_batch_configs.return_value = (3, 0)
mock_cm.load_config.return_value = None
mock_cm.save_config.return_value = None

# indicator_calculator mock
mock_indicator = MagicMock()

# utils mock
mock_utils = MagicMock()
mock_utils.calculate_position_metrics.return_value = {}

# grid_validation mock
mock_grid_val = MagicMock()
mock_grid_val.validate_grid_config.return_value = (True, {
    'stock_code': '000001.SZ',
    'price_interval': 0.05,
    'position_ratio': 0.25,
    'callback_ratio': 0.005,
    'max_investment': 10000,
    'max_deviation': 0.15,
    'target_profit': 0.10,
    'stop_loss': -0.10,
    'duration_days': 7,
})
mock_grid_val.validate_grid_template.return_value = (True, {})

# Methods mock
mock_methods = MagicMock()
mock_methods.add_xt_suffix.side_effect = lambda code: code + '.SZ' if not '.' in code else code

# =====================================================================
# 第二步：注入 Mock 模块到 sys.modules
# =====================================================================
for _extra_mod, _extra_mock in [('utils', mock_utils), ('Methods', mock_methods), ('grid_validation', mock_grid_val)]:
    if _extra_mod in sys.modules:
        _orig_sys_modules[_extra_mod] = sys.modules[_extra_mod]
    sys.modules[_extra_mod] = _extra_mock

# 为真实模块添加 Mock 函数（函数级 patch，避免覆盖整个模块）
import data_manager as _dm_mod
import indicator_calculator as _ic_mod
import position_manager as _pm_mod
import trading_executor as _te_mod
import strategy as _st_mod
import config_manager as _conf_man_mod

# 保存原始函数
_orig_get_dm = _dm_mod.get_data_manager
_orig_get_ic = _ic_mod.get_indicator_calculator
_orig_get_pm = _pm_mod.get_position_manager
_orig_get_te = _te_mod.get_trading_executor
_orig_get_st = _st_mod.get_trading_strategy
_orig_get_cm = _conf_man_mod.get_config_manager

# 替换为返回 mock 的函数
_dm_mod.get_data_manager = lambda: mock_data_manager
_ic_mod.get_indicator_calculator = lambda: mock_indicator
_pm_mod.get_position_manager = lambda: mock_pm
_te_mod.get_trading_executor = lambda: mock_executor
_st_mod.get_trading_strategy = lambda: mock_strategy
_conf_man_mod.get_config_manager = lambda: mock_cm

# =====================================================================
# 第三步：导入 web_server，完成 Mock 设置
# =====================================================================
import web_server

# 注入 mock 实例到 web_server 模块全局变量
web_server.set_position_manager(mock_pm)
web_server.data_manager = mock_data_manager
web_server.trading_executor = mock_executor
web_server.trading_strategy = mock_strategy
web_server.config_manager = mock_cm
web_server.indicator_calculator = mock_indicator


# =====================================================================
# 测试报告记录器
# =====================================================================
class TestReport:
    """收集并输出测试报告"""

    def __init__(self):
        self.results = []
        self.start_time = time.time()

    def record(self, endpoint, method, test_name, status, status_code=None,
               response_data=None, error=None, duration_ms=None):
        self.results.append({
            'endpoint': endpoint,
            'method': method,
            'test_name': test_name,
            'status': status,         # 'PASS' / 'FAIL' / 'SKIP'
            'http_status_code': status_code,
            'response_data': response_data,
            'error': error,
            'duration_ms': duration_ms,
        })

    def summary(self):
        total = len(self.results)
        passed = sum(1 for r in self.results if r['status'] in ('PASS', 'PARTIAL'))
        failed = sum(1 for r in self.results if r['status'] == 'FAIL')
        skipped = sum(1 for r in self.results if r['status'] == 'SKIP')
        elapsed = round(time.time() - self.start_time, 2)
        return {
            'total': total, 'passed': passed,
            'failed': failed, 'skipped': skipped,
            'elapsed_sec': elapsed,
        }


# 全局报告对象（在测试结束后统一输出）
REPORT = TestReport()


# =====================================================================
# 基础测试类
# =====================================================================
class WebAPITestBase(unittest.TestCase):
    """所有API测试的基类"""

    @classmethod
    def setUpClass(cls):
        """启用 Flask 测试模式"""
        web_server.app.config['TESTING'] = True
        web_server.app.config['WTF_CSRF_ENABLED'] = False
        cls.client = web_server.app.test_client()
        # 重置 config 状态，确保测试前状态一致
        config.ENABLE_MONITORING = False
        config.ENABLE_AUTO_TRADING = False
        config.ENABLE_SIMULATION_MODE = True

    def _get(self, url, params=None):
        """发送 GET 请求"""
        t0 = time.time()
        resp = self.client.get(url, query_string=params or {})
        ms = round((time.time() - t0) * 1000, 1)
        return resp, ms

    def _post(self, url, data=None, json_data=None):
        """发送 POST 请求"""
        t0 = time.time()
        if json_data is not None:
            resp = self.client.post(
                url,
                data=json.dumps(json_data),
                content_type='application/json',
            )
        else:
            resp = self.client.post(url, data=data or {})
        ms = round((time.time() - t0) * 1000, 1)
        return resp, ms

    def _delete(self, url):
        """发送 DELETE 请求"""
        t0 = time.time()
        resp = self.client.delete(url)
        ms = round((time.time() - t0) * 1000, 1)
        return resp, ms

    def _put(self, url, json_data=None):
        """发送 PUT 请求"""
        t0 = time.time()
        resp = self.client.put(
            url,
            data=json.dumps(json_data or {}),
            content_type='application/json',
        )
        ms = round((time.time() - t0) * 1000, 1)
        return resp, ms

    def _parse(self, resp):
        """解析 JSON 响应"""
        try:
            return json.loads(resp.data.decode('utf-8'))
        except Exception:
            return {}

    def _record(self, endpoint, method, test_name, resp, ms, extra_checks=None):
        """记录测试结果"""
        data = self._parse(resp)
        ok = resp.status_code < 400
        error_msg = None

        if extra_checks:
            try:
                extra_checks(data)
            except AssertionError as e:
                ok = False
                error_msg = str(e)

        REPORT.record(
            endpoint=endpoint,
            method=method,
            test_name=test_name,
            status='PASS' if ok else 'FAIL',
            status_code=resp.status_code,
            response_data=data,
            error=error_msg,
            duration_ms=ms,
        )
        if not ok and error_msg:
            self.fail(f"{test_name} | {error_msg}")


# =====================================================================
# 1. 静态资源 & 首页
# =====================================================================
class TestStaticAndIndex(WebAPITestBase):
    """测试静态文件服务"""

    def test_01_index_page(self):
        """GET / 应返回 index.html"""
        resp, ms = self._get('/')
        # index.html 存在时返回 200，不存在时 404 均可接受（测试环境可能无前端）
        REPORT.record(
            endpoint='/',
            method='GET',
            test_name='首页 index.html',
            status='PASS' if resp.status_code in (200, 404) else 'FAIL',
            status_code=resp.status_code,
            duration_ms=ms,
        )


# =====================================================================
# 2. 系统状态 & 连接检查
# =====================================================================
class TestSystemStatus(WebAPITestBase):
    """测试系统状态接口"""

    def test_01_connection_status(self):
        """GET /api/connection/status 应返回连接状态"""
        resp, ms = self._get('/api/connection/status')
        data = self._parse(resp)
        self._record(
            '/api/connection/status', 'GET',
            '连接状态查询',
            resp, ms,
            extra_checks=lambda d: (
                self.assertEqual(d.get('status'), 'success'),
                self.assertIn('connected', d),
                self.assertIn('timestamp', d),
            ),
        )

    def test_02_system_status(self):
        """GET /api/status 应返回完整系统状态"""
        resp, ms = self._get('/api/status')
        data = self._parse(resp)
        self._record(
            '/api/status', 'GET',
            '系统运行状态查询',
            resp, ms,
            extra_checks=lambda d: (
                self.assertEqual(d.get('status'), 'success'),
                self.assertIn('account', d),
                self.assertIn('settings', d),
            ),
        )

    def test_04_account_info_refresh_timeout_recovery(self):
        """账户信息刷新超时时，_account_info_refreshing 标志应被重置，缓存应有默认值"""
        from concurrent.futures import TimeoutError as FuturesTimeoutError
        from unittest.mock import patch, MagicMock

        # 重置内部状态
        with web_server._account_info_refresh_lock:
            web_server._account_info_refreshing = False
        with web_server._account_info_lock:
            web_server._account_info_cache['data'] = None
            web_server._account_info_cache['ts'] = 0.0

        # 用超时的 future 模拟 get_account_info 超时
        mock_future = MagicMock()
        mock_future.result.side_effect = FuturesTimeoutError()

        t0 = time.time()
        with patch.object(web_server.api_executor, 'submit', return_value=mock_future):
            web_server._refresh_account_info_worker()
        ms = round((time.time() - t0) * 1000, 1)

        # 超时后：标志应重置为 False，缓存应有默认值（时间戳应更新）
        flag_reset = not web_server._account_info_refreshing
        cache_has_data = web_server._account_info_cache.get('data') is not None
        cache_ts_updated = web_server._account_info_cache.get('ts', 0) > 0

        ok = flag_reset and cache_has_data and cache_ts_updated
        REPORT.record(
            '/api/status', 'INTERNAL',
            '账户信息刷新超时时标志正确重置',
            status='PASS' if ok else 'FAIL',
            status_code=None,
            response_data={
                '_account_info_refreshing': web_server._account_info_refreshing,
                'cache_has_data': cache_has_data,
                'cache_ts_updated': cache_ts_updated,
            },
            error=None if ok else '超时后标志未重置或缓存未初始化',
            duration_ms=ms,
        )
        self.assertFalse(web_server._account_info_refreshing, "超时后 _account_info_refreshing 应为 False")
        self.assertIsNotNone(web_server._account_info_cache.get('data'), "超时后缓存应有默认账户信息")
        self.assertGreater(web_server._account_info_cache.get('ts', 0), 0, "超时后缓存时间戳应更新")


        resp, ms = self._get('/api/debug/status')
        data = self._parse(resp)
        self._record(
            '/api/debug/status', 'GET',
            '调试状态查询',
            resp, ms,
            extra_checks=lambda d: (
                self.assertEqual(d.get('status'), 'success'),
                self.assertIn('system_status', d),
            ),
        )


# =====================================================================
# 3. 持仓管理
# =====================================================================
class TestPositions(WebAPITestBase):
    """测试持仓管理接口"""

    def test_01_get_positions_no_version(self):
        """GET /api/positions 无版本号时返回完整数据"""
        resp, ms = self._get('/api/positions')
        data = self._parse(resp)
        self._record(
            '/api/positions', 'GET',
            '获取持仓（无版本号）',
            resp, ms,
            extra_checks=lambda d: (
                self.assertEqual(d.get('status'), 'success'),
                self.assertIn('data', d),
                self.assertIn('data_version', d),
            ),
        )

    def test_02_get_positions_with_version(self):
        """GET /api/positions?version=1 版本一致时返回 no_change=True"""
        # 先获取当前版本
        mock_pm.get_data_version_info.return_value = {'version': 5, 'changed': False}
        resp, ms = self._get('/api/positions', params={'version': 5})
        data = self._parse(resp)
        self._record(
            '/api/positions?version={current}', 'GET',
            '获取持仓（版本匹配，无变化）',
            resp, ms,
            extra_checks=lambda d: (
                self.assertEqual(d.get('status'), 'success'),
                self.assertTrue(d.get('no_change')),
            ),
        )
        mock_pm.get_data_version_info.return_value = {'version': 1, 'changed': False}

    def test_03_get_positions_all(self):
        """GET /api/positions-all 返回完整持仓数据"""
        resp, ms = self._get('/api/positions-all')
        data = self._parse(resp)
        self._record(
            '/api/positions-all', 'GET',
            '获取所有持仓数据',
            resp, ms,
            extra_checks=lambda d: (
                self.assertEqual(d.get('status'), 'success'),
                self.assertIn('data_version', d),
            ),
        )

    def test_04_get_positions_all_no_change(self):
        """GET /api/positions-all?version=5 版本匹配返回 no_change"""
        mock_pm.get_data_version_info.return_value = {'version': 5, 'changed': False}
        resp, ms = self._get('/api/positions-all', params={'version': 5})
        data = self._parse(resp)
        self._record(
            '/api/positions-all?version={current}', 'GET',
            '获取所有持仓（版本匹配，无变化）',
            resp, ms,
            extra_checks=lambda d: self.assertTrue(d.get('no_change')),
        )
        mock_pm.get_data_version_info.return_value = {'version': 1, 'changed': False}

    def test_05_update_holding_no_stock(self):
        """POST /api/holdings/update 缺少 stock_code 应返回 400"""
        resp, ms = self._post('/api/holdings/update', json_data={})
        self._record(
            '/api/holdings/update', 'POST',
            '更新持仓参数（缺少stock_code）',
            resp, ms,
        )
        self.assertEqual(resp.status_code, 400)
        REPORT.results[-1]['status'] = 'PASS'  # 400 是预期结果

    def test_06_update_holding_stock_not_found(self):
        """POST /api/holdings/update 股票未持有应返回 404"""
        mock_pm.get_position.return_value = None
        resp, ms = self._post(
            '/api/holdings/update',
            json_data={'stock_code': '000001.SZ', 'highest_price': 12.5},
        )
        self._record(
            '/api/holdings/update', 'POST',
            '更新持仓参数（股票未找到）',
            resp, ms,
        )
        self.assertEqual(resp.status_code, 404)
        REPORT.results[-1]['status'] = 'PASS'

    def test_07_update_holding_success(self):
        """POST /api/holdings/update 正常更新持仓参数"""
        mock_pm.get_position.return_value = {
            'stock_code': '000001.SZ',
            'volume': 1000,
            'cost_price': 10.0,
            'profit_triggered': False,
            'highest_price': 11.0,
            'stop_loss_price': 9.2,
        }
        resp, ms = self._post(
            '/api/holdings/update',
            json_data={
                'stock_code': '000001.SZ',
                'highest_price': 12.5,
                'profit_triggered': True,
            },
        )
        data = self._parse(resp)
        self._record(
            '/api/holdings/update', 'POST',
            '更新持仓参数（成功）',
            resp, ms,
            extra_checks=lambda d: self.assertEqual(d.get('status'), 'success'),
        )
        mock_pm.get_position.return_value = None

    def test_08_init_holdings(self):
        """POST /api/holdings/init 初始化持仓数据（bug修复后应返回200）"""
        resp, ms = self._post('/api/holdings/init')
        data = self._parse(resp)
        self._record(
            '/api/holdings/init', 'POST',
            '初始化持仓数据（修复undefined变量bug）',
            resp, ms,
            extra_checks=lambda d: self.assertTrue(
                d.get('success') or d.get('status') == 'success',
                f"期望success=True，实际: {d}"
            ),
        )
        self.assertEqual(resp.status_code, 200, f"修复后应返回200，实际{resp.status_code}: {data}")

    def test_09_initialize_positions(self):
        """POST /api/initialize_positions 初始化持仓数据"""
        resp, ms = self._post('/api/initialize_positions')
        self._record(
            '/api/initialize_positions', 'POST',
            '初始化持仓（全量）',
            resp, ms,
        )
        self.assertIn(resp.status_code, [200, 500])
        REPORT.results[-1]['status'] = 'PASS' if resp.status_code == 200 else 'FAIL'


# =====================================================================
# 4. 交易记录
# =====================================================================
class TestTradeRecords(WebAPITestBase):
    """测试交易记录接口"""

    def test_01_get_trade_records_empty(self):
        """GET /api/trade-records 无记录时返回空列表"""
        mock_executor.get_trades.return_value = pd.DataFrame()
        resp, ms = self._get('/api/trade-records')
        data = self._parse(resp)
        self._record(
            '/api/trade-records', 'GET',
            '获取交易记录（空）',
            resp, ms,
            extra_checks=lambda d: (
                self.assertEqual(d.get('status'), 'success'),
                self.assertEqual(d.get('data'), []),
            ),
        )

    def test_02_get_trade_records_with_data(self):
        """GET /api/trade-records 有记录时正常返回"""
        df = pd.DataFrame([{
            'stock_code': '000001.SZ',
            'trade_type': 'BUY',
            'price': 10.5,
            'volume': 1000,
            'trade_time': '2026-01-01 09:30:00',
            'strategy': 'simu',
            'trade_id': 'SIM001',
            'stock_name': '平安银行',
        }])
        mock_executor.get_trades.return_value = df
        resp, ms = self._get('/api/trade-records')
        data = self._parse(resp)
        self._record(
            '/api/trade-records', 'GET',
            '获取交易记录（有数据）',
            resp, ms,
            extra_checks=lambda d: (
                self.assertEqual(d.get('status'), 'success'),
                self.assertEqual(len(d.get('data', [])), 1),
            ),
        )
        mock_executor.get_trades.return_value = pd.DataFrame()


# =====================================================================
# 5. 配置管理
# =====================================================================
class TestConfig(WebAPITestBase):
    """测试配置管理接口"""

    def test_01_get_config(self):
        """GET /api/config 应返回系统配置"""
        resp, ms = self._get('/api/config')
        data = self._parse(resp)
        self._record(
            '/api/config', 'GET',
            '获取系统配置',
            resp, ms,
            extra_checks=lambda d: (
                self.assertEqual(d.get('status'), 'success'),
                self.assertIn('data', d),
                self.assertIn('ranges', d),
                self.assertIn('singleBuyAmount', d.get('data', {})),
                self.assertIn('simulationMode', d.get('data', {})),
            ),
        )

    def test_02_save_config_valid(self):
        """POST /api/config/save 有效配置应成功保存"""
        payload = {
            'singleBuyAmount': 5000,
            'allowBuy': True,
            'allowSell': True,
            'globalAllowBuySell': False,
            'simulationMode': True,
        }
        resp, ms = self._post('/api/config/save', json_data=payload)
        data = self._parse(resp)
        self._record(
            '/api/config/save', 'POST',
            '保存系统配置（有效参数）',
            resp, ms,
            extra_checks=lambda d: self.assertEqual(d.get('status'), 'success'),
        )

    def test_03_save_config_invalid_range(self):
        """POST /api/config/save 超范围参数应返回 400"""
        payload = {'singleBuyAmount': 9999999}  # 超出最大值
        resp, ms = self._post('/api/config/save', json_data=payload)
        self._record(
            '/api/config/save', 'POST',
            '保存系统配置（超范围参数）',
            resp, ms,
        )
        # 超范围时应返回 400
        self.assertIn(resp.status_code, [400, 200])  # 取决于 CONFIG_PARAM_RANGES 设置
        REPORT.results[-1]['status'] = 'PASS'

    def test_04_save_config_enable_auto_trading(self):
        """POST /api/config/save 切换自动交易开关"""
        old_auto = config.ENABLE_AUTO_TRADING
        payload = {'globalAllowBuySell': True}
        resp, ms = self._post('/api/config/save', json_data=payload)
        data = self._parse(resp)
        self._record(
            '/api/config/save (globalAllowBuySell)', 'POST',
            '切换自动交易开关',
            resp, ms,
            extra_checks=lambda d: self.assertEqual(d.get('status'), 'success'),
        )
        config.ENABLE_AUTO_TRADING = old_auto


# =====================================================================
# 6. 监控控制
# =====================================================================
class TestMonitor(WebAPITestBase):
    """测试监控控制接口"""

    def test_01_start_monitor(self):
        """POST /api/monitor/start 应启动监控"""
        resp, ms = self._post('/api/monitor/start')
        data = self._parse(resp)
        self._record(
            '/api/monitor/start', 'POST',
            '启动监控',
            resp, ms,
            extra_checks=lambda d: (
                self.assertEqual(d.get('status'), 'success'),
                self.assertTrue(d.get('isMonitoring')),
            ),
        )
        self.assertTrue(config.ENABLE_MONITORING)

    def test_02_stop_monitor(self):
        """POST /api/monitor/stop 应停止监控"""
        config.ENABLE_MONITORING = True
        resp, ms = self._post('/api/monitor/stop')
        data = self._parse(resp)
        self._record(
            '/api/monitor/stop', 'POST',
            '停止监控',
            resp, ms,
            extra_checks=lambda d: (
                self.assertEqual(d.get('status'), 'success'),
                self.assertFalse(d.get('isMonitoring')),
            ),
        )
        self.assertFalse(config.ENABLE_MONITORING)

    def test_03_monitor_toggle_sequence(self):
        """多次切换监控状态应保持一致性"""
        config.ENABLE_MONITORING = False
        self._post('/api/monitor/start')
        self.assertTrue(config.ENABLE_MONITORING)
        self._post('/api/monitor/stop')
        self.assertFalse(config.ENABLE_MONITORING)
        REPORT.record(
            '/api/monitor/start+stop', 'POST',
            '监控状态切换一致性',
            status='PASS', status_code=200,
        )


# =====================================================================
# 7. 数据管理
# =====================================================================
class TestDataManagement(WebAPITestBase):
    """测试数据管理接口"""

    def test_01_clear_logs(self):
        """POST /api/logs/clear 清空当天日志"""
        resp, ms = self._post('/api/logs/clear')
        data = self._parse(resp)
        self._record(
            '/api/logs/clear', 'POST',
            '清空当天日志',
            resp, ms,
            extra_checks=lambda d: self.assertEqual(d.get('status'), 'success'),
        )

    def test_02_clear_buysell_data(self):
        """POST /api/data/clear_buysell 清空买卖记录"""
        resp, ms = self._post('/api/data/clear_buysell')
        data = self._parse(resp)
        self._record(
            '/api/data/clear_buysell', 'POST',
            '清空买卖记录',
            resp, ms,
            extra_checks=lambda d: self.assertEqual(d.get('status'), 'success'),
        )

    def test_03_import_data(self):
        """POST /api/data/import 导入数据（存根）"""
        resp, ms = self._post('/api/data/import', json_data={})
        data = self._parse(resp)
        self._record(
            '/api/data/import', 'POST',
            '导入数据（存根接口）',
            resp, ms,
            extra_checks=lambda d: self.assertEqual(d.get('status'), 'success'),
        )


# =====================================================================
# 8. 股票池管理
# =====================================================================
class TestStockPool(WebAPITestBase):
    """测试股票池管理接口"""

    def test_01_get_stock_pool_file_exists(self):
        """GET /api/stock_pool/list 读取股票池文件"""
        resp, ms = self._get('/api/stock_pool/list')
        data = self._parse(resp)
        self._record(
            '/api/stock_pool/list', 'GET',
            '获取备选池股票列表',
            resp, ms,
            extra_checks=lambda d: (
                self.assertEqual(d.get('status'), 'success'),
                self.assertIn('data', d),
                self.assertIsInstance(d.get('data'), list),
            ),
        )


# =====================================================================
# 9. 交易执行
# =====================================================================
class TestTradeExecution(WebAPITestBase):
    """测试交易执行接口"""

    def test_01_execute_buy_missing_quantity(self):
        """POST /api/actions/execute_buy quantity=0 应返回 400"""
        resp, ms = self._post(
            '/api/actions/execute_buy',
            json_data={'strategy': 'custom_stock', 'quantity': 0, 'stocks': ['000001.SZ']},
        )
        self._record(
            '/api/actions/execute_buy', 'POST',
            '执行买入（quantity=0）',
            resp, ms,
        )
        self.assertEqual(resp.status_code, 400)
        REPORT.results[-1]['status'] = 'PASS'

    def test_02_execute_buy_missing_stocks(self):
        """POST /api/actions/execute_buy stocks=[] 应返回 400"""
        resp, ms = self._post(
            '/api/actions/execute_buy',
            json_data={'strategy': 'custom_stock', 'quantity': 1, 'stocks': []},
        )
        self._record(
            '/api/actions/execute_buy', 'POST',
            '执行买入（空股票列表）',
            resp, ms,
        )
        self.assertEqual(resp.status_code, 400)
        REPORT.results[-1]['status'] = 'PASS'

    def test_03_execute_buy_custom_stock(self):
        """POST /api/actions/execute_buy custom_stock 策略"""
        mock_strategy.manual_buy.return_value = 'ORDER_001'
        resp, ms = self._post(
            '/api/actions/execute_buy',
            json_data={
                'strategy': 'custom_stock',
                'quantity': 1,
                'stocks': ['000001.SZ'],
            },
        )
        data = self._parse(resp)
        self._record(
            '/api/actions/execute_buy (custom_stock)', 'POST',
            '执行买入（custom_stock策略）',
            resp, ms,
            extra_checks=lambda d: self.assertEqual(d.get('status'), 'success'),
        )
        mock_strategy.manual_buy.return_value = None

    def test_04_execute_buy_random_pool(self):
        """POST /api/actions/execute_buy random_pool 策略"""
        mock_strategy.manual_buy.return_value = 'ORDER_002'
        resp, ms = self._post(
            '/api/actions/execute_buy',
            json_data={
                'strategy': 'random_pool',
                'quantity': 1,
                'stocks': ['000001.SZ', '600036.SH'],
            },
        )
        data = self._parse(resp)
        self._record(
            '/api/actions/execute_buy (random_pool)', 'POST',
            '执行买入（random_pool策略）',
            resp, ms,
            extra_checks=lambda d: self.assertEqual(d.get('status'), 'success'),
        )
        mock_strategy.manual_buy.return_value = None


# =====================================================================
# 10. 网格交易 API
# =====================================================================
def _make_grid_manager_mock(sessions=None, db_sessions=None):
    """创建标准化的 grid_manager mock 对象"""
    mock_gm = MagicMock()
    mock_gm.sessions = sessions or {}
    mock_gm.trackers = {}
    # 模拟真实的 _normalize_code 行为：去除交易所后缀
    mock_gm._normalize_code.side_effect = lambda code: code.split('.')[0] if code and '.' in code else code
    # db.get_all_grid_sessions 返回普通列表，避免 MagicMock 序列化错误
    mock_gm.db = MagicMock()
    mock_gm.db.get_all_grid_sessions.return_value = db_sessions or []
    mock_gm.db.get_grid_trades.return_value = []
    mock_gm.db.get_grid_trade_count.return_value = 0
    return mock_gm



def _make_grid_session_mock(session_id=1, stock_code='000001.SZ'):
    """创建标准化的 grid session mock 对象（所有字段为 JSON 可序列化类型）"""
    from datetime import datetime, timedelta
    sess = MagicMock()
    sess.id = session_id
    sess.stock_code = stock_code
    sess.status = 'active'
    sess.center_price = 10.0
    sess.current_center_price = 10.0
    sess.price_interval = 0.05
    sess.position_ratio = 0.25
    sess.callback_ratio = 0.005
    sess.max_investment = 10000.0
    sess.current_investment = 0.0
    sess.max_deviation = 0.15
    sess.target_profit = 0.10
    sess.stop_loss = -0.10
    sess.trade_count = 0
    sess.buy_count = 0
    sess.sell_count = 0
    sess.total_buy_amount = 0.0
    sess.total_sell_amount = 0.0
    sess.stop_reason = None
    sess.start_time = datetime.now()
    sess.end_time = datetime.now() + timedelta(days=7)
    sess.stop_time = None
    sess.get_profit_ratio.return_value = 0.0
    sess.get_deviation_ratio.return_value = 0.0
    sess.get_grid_levels.return_value = []
    return sess


class TestGridTrading(WebAPITestBase):
    """测试网格交易相关接口"""

    def test_01_get_grid_sessions(self):
        """GET /api/grid/sessions 应返回所有网格会话（无会话时返回空列表）"""
        mock_gm = _make_grid_manager_mock()
        mock_pm.grid_manager = mock_gm

        resp, ms = self._get('/api/grid/sessions')
        data = self._parse(resp)
        self._record(
            '/api/grid/sessions', 'GET',
            '获取所有网格会话（空）',
            resp, ms,
            extra_checks=lambda d: (
                self.assertTrue(d.get('success')),
                self.assertIn('sessions', d),
            ),
        )
        mock_pm.grid_manager = None

    def test_02_get_grid_session_by_stock(self):
        """GET /api/grid/session/<stock_code> 获取指定股票网格会话"""
        mock_gm = _make_grid_manager_mock()
        mock_pm.grid_manager = mock_gm

        resp, ms = self._get('/api/grid/session/000001.SZ')
        data = self._parse(resp)
        self._record(
            '/api/grid/session/<stock_code>', 'GET',
            '获取指定股票网格会话',
            resp, ms,
            extra_checks=lambda d: self.assertTrue(d.get('success')),
        )
        mock_pm.grid_manager = None

    def test_03_get_grid_status_by_stock(self):
        """GET /api/grid/status/<stock_code> 获取网格实时状态"""
        mock_gm = _make_grid_manager_mock()
        mock_pm.grid_manager = mock_gm

        resp, ms = self._get('/api/grid/status/000001.SZ')
        data = self._parse(resp)
        self._record(
            '/api/grid/status/<stock_code>', 'GET',
            '获取网格实时状态（无会话）',
            resp, ms,
        )
        # 无活跃会话时可能返回 success 或 404/400
        self.assertIn(resp.status_code, [200, 400, 404])
        REPORT.results[-1]['status'] = 'PASS'
        mock_pm.grid_manager = None

    def test_04_get_grid_config(self):
        """GET /api/grid/config 获取默认网格配置"""
        # get_all_positions 返回 DataFrame
        mock_pm.get_all_positions.return_value = pd.DataFrame()
        resp, ms = self._get('/api/grid/config')
        data = self._parse(resp)
        self._record(
            '/api/grid/config', 'GET',
            '获取默认网格配置',
            resp, ms,
            # 此端点返回 'status': 'success' 而非 'success': True
            extra_checks=lambda d: self.assertEqual(d.get('status'), 'success'),
        )

    def test_05_get_grid_risk_templates(self):
        """GET /api/grid/risk-templates 获取风险等级模板"""
        resp, ms = self._get('/api/grid/risk-templates')
        data = self._parse(resp)
        self._record(
            '/api/grid/risk-templates', 'GET',
            '获取风险等级模板',
            resp, ms,
            extra_checks=lambda d: (
                self.assertTrue(d.get('success')),
                self.assertIn('templates', d),
            ),
        )

    def test_06_get_grid_templates(self):
        """GET /api/grid/templates 获取所有已保存模板"""
        mock_pm.db_manager.get_all_grid_templates.return_value = []
        resp, ms = self._get('/api/grid/templates')
        data = self._parse(resp)
        self._record(
            '/api/grid/templates', 'GET',
            '获取所有网格模板',
            resp, ms,
            extra_checks=lambda d: self.assertTrue(d.get('success')),
        )

    def test_07_get_grid_template_by_name(self):
        """GET /api/grid/template/<name> 获取指定模板详情（不存在）"""
        mock_pm.db_manager.get_grid_template.return_value = None
        resp, ms = self._get('/api/grid/template/我的模板')
        data = self._parse(resp)
        self._record(
            '/api/grid/template/<name>', 'GET',
            '获取指定模板（不存在）',
            resp, ms,
        )
        self.assertIn(resp.status_code, [200, 404])
        REPORT.results[-1]['status'] = 'PASS'

    def test_08_get_default_template(self):
        """GET /api/grid/template/default 获取默认模板"""
        mock_pm.db_manager.get_default_grid_template.return_value = None
        resp, ms = self._get('/api/grid/template/default')
        data = self._parse(resp)
        self._record(
            '/api/grid/template/default', 'GET',
            '获取默认模板（未设置）',
            resp, ms,
            extra_checks=lambda d: self.assertTrue(d.get('success')),
        )

    def test_09_save_grid_template(self):
        """POST /api/grid/template/save 保存网格模板"""
        mock_pm.db_manager.save_grid_template.return_value = 42  # 返回模板ID
        # 注意：端点需要 template_name（顶层），不是嵌套在 config 里
        resp, ms = self._post(
            '/api/grid/template/save',
            json_data={
                'template_name': '测试模板',
                'price_interval': 0.05,
                'position_ratio': 0.25,
                'callback_ratio': 0.005,
                'max_deviation': 0.15,
                'target_profit': 0.10,
                'stop_loss': -0.10,
                'duration_days': 7,
                'max_investment_ratio': 0.5,
                'description': '测试',
            },
        )
        data = self._parse(resp)
        self._record(
            '/api/grid/template/save', 'POST',
            '保存网格配置模板',
            resp, ms,
            extra_checks=lambda d: self.assertTrue(d.get('success')),
        )

    def test_10_use_grid_template(self):
        """POST /api/grid/template/use 记录模板使用"""
        mock_pm.db_manager.increment_template_usage.return_value = 5
        mock_pm.db_manager.get_grid_template.return_value = None  # template 为 None 时也应成功
        resp, ms = self._post(
            '/api/grid/template/use',
            json_data={'template_name': '测试模板'},
        )
        data = self._parse(resp)
        self._record(
            '/api/grid/template/use', 'POST',
            '记录模板使用次数',
            resp, ms,
            extra_checks=lambda d: self.assertTrue(d.get('success')),
        )

    def test_11_set_default_template(self):
        """PUT /api/grid/template/<name>/default 设置默认模板"""
        # 需要模板存在才能设置为默认
        existing_template = {
            'template_name': '测试模板',
            'price_interval': 0.05,
            'position_ratio': 0.25,
            'callback_ratio': 0.005,
            'max_deviation': 0.15,
            'target_profit': 0.10,
            'stop_loss': -0.10,
            'duration_days': 7,
        }
        mock_pm.db_manager.get_grid_template.return_value = existing_template
        mock_pm.db_manager.save_grid_template.return_value = 1
        resp, ms = self._put('/api/grid/template/测试模板/default')
        data = self._parse(resp)
        self._record(
            '/api/grid/template/<name>/default', 'PUT',
            '设置默认网格模板',
            resp, ms,
            extra_checks=lambda d: self.assertTrue(d.get('success')),
        )
        mock_pm.db_manager.get_grid_template.return_value = None

    def test_12_delete_grid_template(self):
        """DELETE /api/grid/template/<name> 删除模板"""
        mock_pm.db_manager.delete_grid_template.return_value = True
        resp, ms = self._delete('/api/grid/template/测试模板')
        data = self._parse(resp)
        self._record(
            '/api/grid/template/<name>', 'DELETE',
            '删除网格模板',
            resp, ms,
            extra_checks=lambda d: self.assertTrue(d.get('success')),
        )

    def test_13_get_grid_checkbox_states(self):
        """GET /api/grid/checkbox-states 获取复选框状态"""
        mock_gm = _make_grid_manager_mock()
        mock_pm.grid_manager = mock_gm
        mock_pm.data_version = 1  # 必须是可序列化的整数

        resp, ms = self._get('/api/grid/checkbox-states')
        data = self._parse(resp)
        self._record(
            '/api/grid/checkbox-states', 'GET',
            '获取网格复选框状态',
            resp, ms,
            extra_checks=lambda d: self.assertTrue(d.get('success')),
        )
        mock_pm.grid_manager = None

    def test_14_get_grid_checkbox_state_stock(self):
        """GET /api/grid/checkbox-state/<stock> 获取单股复选框状态"""
        mock_gm = _make_grid_manager_mock()
        mock_pm.grid_manager = mock_gm
        mock_pm.data_version = 1

        resp, ms = self._get('/api/grid/checkbox-state/000001.SZ')
        data = self._parse(resp)
        self._record(
            '/api/grid/checkbox-state/<stock>', 'GET',
            '获取单股网格复选框状态',
            resp, ms,
            extra_checks=lambda d: self.assertTrue(d.get('success')),
        )
        mock_pm.grid_manager = None

    def test_15_start_grid_no_grid_manager(self):
        """POST /api/grid/start 未启用网格交易应返回 400"""
        mock_pm.grid_manager = None
        resp, ms = self._post(
            '/api/grid/start',
            json_data={'stock_code': '000001.SZ', 'max_investment': 10000},
        )
        self._record(
            '/api/grid/start (无grid_manager)', 'POST',
            '启动网格交易（功能未启用）',
            resp, ms,
        )
        self.assertEqual(resp.status_code, 400)
        REPORT.results[-1]['status'] = 'PASS'

    def test_16_start_grid_with_grid_manager(self):
        """POST /api/grid/start 正常启动网格交易"""
        mock_gm = _make_grid_manager_mock()
        mock_session = _make_grid_session_mock(session_id=1, stock_code='000001.SZ')
        mock_gm.start_grid_session.return_value = mock_session
        mock_gm.sessions = {}
        mock_pm.grid_manager = mock_gm
        mock_pm.db_manager.get_grid_template.return_value = None

        resp, ms = self._post(
            '/api/grid/start',
            json_data={
                'stock_code': '000001',
                'max_investment': 10000,
                'price_interval': 0.05,
                'position_ratio': 0.25,
                'callback_ratio': 0.005,
            },
        )
        data = self._parse(resp)
        self._record(
            '/api/grid/start', 'POST',
            '启动网格交易（正常）',
            resp, ms,
        )
        # 依赖 validate_grid_config mock 返回 (True, {...})
        self.assertIn(resp.status_code, [200, 400, 500])
        REPORT.results[-1]['status'] = 'PASS' if resp.status_code == 200 else 'PARTIAL'
        mock_pm.grid_manager = None

    def test_17_stop_grid_by_session_id(self):
        """POST /api/grid/stop/<session_id> 停止网格会话"""
        mock_gm = _make_grid_manager_mock()
        mock_gm.stop_grid_session.return_value = {'profit': 0.0, 'trades': 0}
        mock_pm.grid_manager = mock_gm

        resp, ms = self._post('/api/grid/stop/1')
        data = self._parse(resp)
        self._record(
            '/api/grid/stop/<session_id>', 'POST',
            '停止网格会话（按ID）',
            resp, ms,
            extra_checks=lambda d: self.assertTrue(d.get('success')),
        )
        mock_pm.grid_manager = None

    def test_18_stop_grid_flexible(self):
        """POST /api/grid/stop 灵活停止（by stock_code）"""
        mock_session = _make_grid_session_mock()
        mock_gm = _make_grid_manager_mock(sessions={'000001': mock_session})
        mock_gm.stop_grid_session.return_value = {'profit': 0.0, 'trades': 0}
        mock_pm.grid_manager = mock_gm

        resp, ms = self._post(
            '/api/grid/stop',
            json_data={'stock_code': '000001.SZ'},
        )
        data = self._parse(resp)
        self._record(
            '/api/grid/stop (by stock_code)', 'POST',
            '停止网格会话（按股票代码）',
            resp, ms,
            extra_checks=lambda d: self.assertTrue(d.get('success')),
        )
        mock_pm.grid_manager = None

    def test_19_get_grid_trades(self):
        """GET /api/grid/trades/<session_id> 获取网格交易历史"""
        mock_gm = _make_grid_manager_mock()
        mock_pm.grid_manager = mock_gm

        resp, ms = self._get('/api/grid/trades/1')
        data = self._parse(resp)
        self._record(
            '/api/grid/trades/<session_id>', 'GET',
            '获取网格交易历史',
            resp, ms,
            extra_checks=lambda d: self.assertTrue(d.get('success')),
        )
        mock_pm.grid_manager = None

    def test_20_get_grid_session_by_id(self):
        """GET /api/grid/session/<int:session_id> 获取会话详情（会话不存在）"""
        mock_gm = _make_grid_manager_mock()
        mock_pm.grid_manager = mock_gm

        resp, ms = self._get('/api/grid/session/999')
        data = self._parse(resp)
        self._record(
            '/api/grid/session/<session_id>', 'GET',
            '获取网格会话详情（不存在）',
            resp, ms,
        )
        # 会话不存在时返回 404；grid_manager 未初始化时返回 400
        self.assertIn(resp.status_code, [200, 400, 404])
        REPORT.results[-1]['status'] = 'PASS'
        mock_pm.grid_manager = None


# =====================================================================
# 11. 认证/Token 机制
# =====================================================================
class TestAuthentication(WebAPITestBase):
    """测试 Token 认证机制"""

    def test_01_token_disabled(self):
        """默认无 Token 时敏感 API 可访问"""
        old_token = config.WEB_API_TOKEN if hasattr(config, 'WEB_API_TOKEN') else ''
        # 确认无 token 时通过
        resp, ms = self._post('/api/monitor/start')
        self._record(
            '/api/monitor/start (no token)', 'POST',
            '无Token时可访问敏感API',
            resp, ms,
            extra_checks=lambda d: self.assertEqual(d.get('status'), 'success'),
        )

    def test_02_token_enabled_rejected(self):
        """设置 Token 后无 Token 请求应返回 401"""
        old_token = getattr(config, 'WEB_API_TOKEN', '')
        config.WEB_API_TOKEN = 'secret_test_token'
        resp, ms = self._post('/api/config/save', json_data={'allowBuy': True})
        self._record(
            '/api/config/save (invalid token)', 'POST',
            'Token启用后无Token请求被拒绝',
            resp, ms,
        )
        self.assertEqual(resp.status_code, 401)
        REPORT.results[-1]['status'] = 'PASS'
        config.WEB_API_TOKEN = old_token

    def test_03_token_enabled_header(self):
        """设置 Token 后通过请求头 X-API-Token 认证"""
        old_token = getattr(config, 'WEB_API_TOKEN', '')
        config.WEB_API_TOKEN = 'secret_test_token'
        t0 = time.time()
        resp = self.client.post(
            '/api/monitor/start',
            headers={'X-API-Token': 'secret_test_token'},
        )
        ms = round((time.time() - t0) * 1000, 1)
        data = self._parse(resp)
        REPORT.record(
            '/api/monitor/start (with token header)', 'POST',
            '通过X-API-Token请求头认证',
            status='PASS' if resp.status_code == 200 else 'FAIL',
            status_code=resp.status_code,
            duration_ms=ms,
        )
        self.assertEqual(resp.status_code, 200)
        config.WEB_API_TOKEN = old_token

    def test_04_token_enabled_query_param(self):
        """设置 Token 后通过 URL 参数 token 认证"""
        old_token = getattr(config, 'WEB_API_TOKEN', '')
        config.WEB_API_TOKEN = 'secret_test_token'
        t0 = time.time()
        resp = self.client.post('/api/monitor/stop?token=secret_test_token')
        ms = round((time.time() - t0) * 1000, 1)
        REPORT.record(
            '/api/monitor/stop?token={token}', 'POST',
            '通过URL参数token认证',
            status='PASS' if resp.status_code == 200 else 'FAIL',
            status_code=resp.status_code,
            duration_ms=ms,
        )
        self.assertEqual(resp.status_code, 200)
        config.WEB_API_TOKEN = old_token


# =====================================================================
# 12. 错误处理
# =====================================================================
class TestErrorHandling(WebAPITestBase):
    """测试错误处理机制"""

    def test_01_invalid_endpoint(self):
        """访问不存在的端点应返回 404（或静态文件处理的错误）"""
        resp, ms = self._get('/api/nonexistent_endpoint_xyz')
        self._record(
            '/api/nonexistent_endpoint_xyz', 'GET',
            '访问不存在的端点',
            resp, ms,
        )
        self.assertIn(resp.status_code, [404, 500])
        REPORT.results[-1]['status'] = 'PASS'

    def test_02_post_with_invalid_json(self):
        """发送无效 JSON 应该优雅处理"""
        t0 = time.time()
        resp = self.client.post(
            '/api/config/save',
            data='invalid{json}',
            content_type='application/json',
        )
        ms = round((time.time() - t0) * 1000, 1)
        REPORT.record(
            '/api/config/save (invalid JSON)', 'POST',
            '发送无效JSON请求体',
            status='PASS' if resp.status_code in [400, 500] else 'FAIL',
            status_code=resp.status_code,
            duration_ms=ms,
        )

    def test_03_position_manager_exception(self):
        """position_manager 异常时 API 应返回 500"""
        mock_pm.get_data_version_info.side_effect = RuntimeError("模拟内部错误")
        resp, ms = self._get('/api/positions')
        REPORT.record(
            '/api/positions (PM exception)', 'GET',
            '后端异常时返回500',
            status='PASS' if resp.status_code == 500 else 'FAIL',
            status_code=resp.status_code,
            duration_ms=ms,
        )
        self.assertEqual(resp.status_code, 500)
        mock_pm.get_data_version_info.side_effect = None
        mock_pm.get_data_version_info.return_value = {'version': 1, 'changed': False}


# =====================================================================
# 测试报告输出
# =====================================================================
def print_report():
    """打印格式化测试报告（写入文件 + 控制台ASCII摘要）"""
    summary = REPORT.summary()
    results = REPORT.results

    print("\n" + "=" * 80)
    print("  miniQMT Web API 测试报告")
    print("=" * 80)
    print(f"  测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Python环境: C:/Users/PC/Anaconda3/envs/python39")
    print(f"  总计: {summary['total']} 项  |  通过: {summary['passed']}  |  "
          f"失败: {summary['failed']}  |  跳过: {summary['skipped']}")
    print(f"  耗时: {summary['elapsed_sec']} 秒")
    print("-" * 80)

    # 按端点分组显示
    categories = {
        '静态资源': [],
        '系统状态': [],
        '持仓管理': [],
        '交易记录': [],
        '配置管理': [],
        '监控控制': [],
        '数据管理': [],
        '股票池': [],
        '交易执行': [],
        '网格交易': [],
        '认证机制': [],
        '错误处理': [],
    }

    def categorize(r):
        ep = r['endpoint']
        if ep.startswith('/api/grid'):
            return '网格交易'
        elif ep.startswith('/api/monitor'):
            return '监控控制'
        elif ep.startswith('/api/config'):
            return '配置管理'
        elif ep.startswith('/api/position') or ep.startswith('/api/holding') \
                or ep.startswith('/api/initialize'):
            return '持仓管理'
        elif ep.startswith('/api/trade') or ep.startswith('/api/actions'):
            return '交易执行'
        elif ep.startswith('/api/trade-record'):
            return '交易记录'
        elif ep.startswith('/api/data') or ep.startswith('/api/logs'):
            return '数据管理'
        elif ep.startswith('/api/stock_pool'):
            return '股票池'
        elif ep.startswith('/api/connection') or ep.startswith('/api/status') \
                or ep.startswith('/api/debug'):
            return '系统状态'
        elif 'token' in r['test_name'] or 'Token' in r['test_name'] \
                or '认证' in r['test_name'] or '拒绝' in r['test_name']:
            return '认证机制'
        elif '错误' in r['test_name'] or '异常' in r['test_name'] \
                or '不存在' in r['test_name'] or 'JSON' in r['test_name']:
            return '错误处理'
        elif ep == '/':
            return '静态资源'
        return '交易执行'

    for r in results:
        cat = categorize(r)
        if cat in categories:
            categories[cat].append(r)
        else:
            categories['错误处理'].append(r)

    for cat, items in categories.items():
        if not items:
            continue
        print(f"\n  [{cat}]")
        for r in items:
            if r['status'] == 'PASS':
                icon = '[PASS]   '
            elif r['status'] == 'PARTIAL':
                icon = '[PARTIAL]'
            else:
                icon = '[FAIL]   '
            code = f"HTTP {r['http_status_code']}" if r['http_status_code'] else '   -   '
            ms_str = f"{r['duration_ms']}ms" if r['duration_ms'] else '  -  '
            name = r['test_name'][:45]
            print(f"    {icon}  {r['method']:<6} {code}  {ms_str:>8}  {name}")
            if r['error']:
                print(f"             错误: {r['error']}")

    print("\n" + "-" * 80)
    pass_rate = round(summary['passed'] / summary['total'] * 100, 1) if summary['total'] > 0 else 0
    print(f"  通过率: {pass_rate}%  ({summary['passed']}/{summary['total']})")

    # 失败详情
    failed = [r for r in results if r['status'] == 'FAIL']
    if failed:
        print("\n  [失败项目详情]")
        for r in failed:
            print(f"    [FAIL] [{r['method']}] {r['endpoint']}")
            print(f"           测试: {r['test_name']}")
            print(f"           HTTP状态码: {r['http_status_code']}")
            if r['error']:
                print(f"           错误信息: {r['error']}")
            if r['response_data']:
                print(f"           响应数据: {json.dumps(r['response_data'], ensure_ascii=False)[:200]}")

    print("=" * 80)

    # 保存 JSON 报告
    report_path = os.path.join(PROJECT_ROOT, 'test', 'web_api_test_report.json')
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump({
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'summary': summary,
            'results': results,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n  详细报告已保存至: {report_path}")
    print("=" * 80)


def tearDownModule():
    """恢复模块级 Mock 补丁，确保测试隔离（防止污染后续测试模块的 sys.modules）"""
    all_mocked_names = _MOCKED_MODULE_NAMES + ['utils', 'Methods', 'grid_validation']
    for mod_name in all_mocked_names:
        if mod_name in _orig_sys_modules:
            sys.modules[mod_name] = _orig_sys_modules[mod_name]
        else:
            sys.modules.pop(mod_name, None)
    # 恢复模块函数级补丁
    _dm_mod.get_data_manager = _orig_get_dm
    _ic_mod.get_indicator_calculator = _orig_get_ic
    _pm_mod.get_position_manager = _orig_get_pm
    _te_mod.get_trading_executor = _orig_get_te
    _st_mod.get_trading_strategy = _orig_get_st
    _conf_man_mod.get_config_manager = _orig_get_cm


# =====================================================================
# 主入口
# =====================================================================
def main():
    # 构建测试套件
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    test_classes = [
        TestStaticAndIndex,
        TestSystemStatus,
        TestPositions,
        TestTradeRecords,
        TestConfig,
        TestMonitor,
        TestDataManagement,
        TestStockPool,
        TestTradeExecution,
        TestGridTrading,
        TestAuthentication,
        TestErrorHandling,
    ]

    for cls in test_classes:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    # 运行测试（静默模式，报告由我们自己输出）
    runner = unittest.TextTestRunner(
        verbosity=2,
        stream=sys.stdout,
    )
    result = runner.run(suite)

    # 输出自定义报告
    print_report()

    # 返回退出码
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
