"""
XtQuantManager Flask 兼容端点测试

验证 web2.0 前端通过网关访问时，/api/* 兼容端点返回的数据格式与
Flask web_server 一致（顶层字段 + 英文键），确保持仓/交易数据正确显示。

关键回归点（2026-05-25 修复）:
- 中文字段(证券代码/股票余额/市值...) → 英文字段(stock_code/volume/market_value...)
- 顶层字段对齐: connected/account/settings/ranges/data_version 不嵌套在 data 内
- X-Account-Id 请求头选择目标账号（多账号隔离）
- 委托类型 23→BUY / 24→SELL 映射
- SQLite 持久化字段注入: stock_name/open_date/stop_loss_price/highest_price
"""
import os
import sys
import time
import unittest
import sqlite3
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from xtquant_manager.manager import XtQuantManager
from xtquant_manager.account import AccountConfig, XtQuantAccount
from xtquant_manager.server import create_app
from xtquant_manager.security import SecurityConfig
from test.test_xtquant_manager.mocks import (
    MockXtTrader, MockXtData, MockStockAccount, MockXtTrade,
)

# 用不与真实账号冲突的测试 ID，避免污染真实 SQLite 数据库
ACC1 = "test_flask_a"
ACC2 = "test_flask_b"
_TMP_DIRS = []  # setUp 创建，tearDown 清理


def _inject_account(manager, account_id, positions=None, trades=None):
    cfg = AccountConfig(account_id=account_id, qmt_path="mock")
    acct = XtQuantAccount(cfg)
    trader = MockXtTrader()
    if positions:
        for p in positions:
            trader.add_mock_position(
                stock_code=p["stock_code"],
                volume=p["volume"],
                cost_price=p.get("cost_price", 10.0),
                current_price=p.get("current_price", 10.5),
            )
    if trades:
        for t in trades:
            trader._trades.append(MockXtTrade(
                account_type="STOCK", account_id=account_id,
                stock_code=t["stock_code"], order_type=t["order_type"],
                traded_id=t.get("traded_id", "T1"),
                traded_volume=t.get("traded_volume", 100),
                traded_price=t.get("traded_price", 10.0),
                traded_amount=t.get("traded_amount", 1000.0),
            ))
    acct._xt_trader = trader
    acct._acc = MockStockAccount(account_id)
    acct._xtdata = MockXtData()
    acct._connected = True
    acct._connected_at = time.time()
    acct._last_ping_ok_time = time.time()
    manager._accounts[account_id] = acct
    return acct


class TestFlaskCompatEndpoints(unittest.TestCase):
    def setUp(self):
        # 保存现有单例，避免污染其他在导入期建立状态的测试模块
        self._prev_instance = getattr(XtQuantManager, "_instance", None)
        XtQuantManager.reset_instance()
        self.manager = XtQuantManager.get_instance()
        _inject_account(self.manager, ACC1, positions=[
            {"stock_code": "000001.SZ", "volume": 1000, "cost_price": 10.0, "current_price": 10.5},
        ], trades=[
            {"stock_code": "000001.SZ", "order_type": 24, "traded_volume": 500, "traded_price": 10.5},
        ])
        _inject_account(self.manager, ACC2, positions=[
            {"stock_code": "600036.SH", "volume": 500, "cost_price": 35.0, "current_price": 36.0},
        ], trades=[
            {"stock_code": "600036.SH", "order_type": 23, "traded_volume": 200, "traded_price": 35.0},
        ])

        # 创建临时 SQLite 数据库模拟 position_manager 持久化数据
        self._tmp_dirs = []
        self._create_test_db(ACC1, {
            "000001": {"stock_name": "平安银行", "open_date": "2026-03-15 10:30:00",
                        "stop_loss_price": 9.25, "profit_triggered": 0, "highest_price": 11.2},
        }, grid_sessions=[
            {"stock_code": "000001.SZ", "status": "active"},
        ])
        self._create_test_db(ACC2, {
            "600036": {"stock_name": "招商银行", "open_date": "2026-04-20 14:00:00",
                        "stop_loss_price": 32.38, "profit_triggered": 1, "highest_price": 38.5},
        })

        # TestClient host 为 "testclient"，需加入 local_ips 才能通过安全校验
        sec = SecurityConfig(api_token="", local_ips=["127.0.0.1", "::1", "localhost", "testclient", "unknown"])
        self.app = create_app(sec)
        self.client = TestClient(self.app)

    def _create_test_db(self, aid: str, positions: dict, grid_sessions: list = None):
        """在项目根目录创建 data_<aid>/trading.db 临时 SQLite 文件。"""
        import os as _os
        tmp_dir = _os.path.join(_os.path.dirname(__file__), "..", f"data_{aid}")
        tmp_dir = _os.path.normpath(tmp_dir)
        _os.makedirs(tmp_dir, exist_ok=True)
        self._tmp_dirs.append(tmp_dir)
        db_path = _os.path.join(tmp_dir, "trading.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""CREATE TABLE IF NOT EXISTS positions (
            stock_code TEXT PRIMARY KEY, stock_name TEXT, volume REAL,
            cost_price REAL, current_price REAL, market_value REAL,
            open_date TIMESTAMP, profit_triggered BOOLEAN DEFAULT FALSE,
            highest_price REAL, stop_loss_price REAL)""")
        for code, fields in positions.items():
            conn.execute(
                "INSERT OR REPLACE INTO positions (stock_code, stock_name, open_date, stop_loss_price, profit_triggered, highest_price) VALUES (?,?,?,?,?,?)",
                (code, fields["stock_name"], fields["open_date"],
                 fields["stop_loss_price"], fields["profit_triggered"], fields["highest_price"]))
        conn.execute("""CREATE TABLE IF NOT EXISTS grid_trading_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_code TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            center_price REAL NOT NULL DEFAULT 10,
            current_center_price REAL DEFAULT 10,
            price_interval REAL NOT NULL DEFAULT 0.05,
            position_ratio REAL NOT NULL DEFAULT 0.25,
            callback_ratio REAL NOT NULL DEFAULT 0.005,
            max_investment REAL NOT NULL DEFAULT 10000,
            current_investment REAL DEFAULT 0,
            max_deviation REAL DEFAULT 0.15,
            target_profit REAL DEFAULT 0.10,
            stop_loss REAL DEFAULT -0.10,
            trade_count INTEGER DEFAULT 0,
            buy_count INTEGER DEFAULT 0,
            sell_count INTEGER DEFAULT 0,
            total_buy_amount REAL DEFAULT 0,
            total_sell_amount REAL DEFAULT 0,
            start_time TEXT NOT NULL DEFAULT '2026-03-15T10:30:00',
            end_time TEXT NOT NULL DEFAULT '2026-03-22T10:30:00',
            stop_time TEXT,
            stop_reason TEXT
        )""")
        for s in grid_sessions or []:
            conn.execute(
                "INSERT INTO grid_trading_sessions "
                "(stock_code, status, center_price, current_center_price, max_investment, current_investment, start_time, end_time) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    s["stock_code"], s.get("status", "active"),
                    s.get("center_price", 10.0), s.get("current_center_price", 10.5),
                    s.get("max_investment", 10000), s.get("current_investment", 2500),
                    s.get("start_time", "2026-03-15T10:30:00"),
                    s.get("end_time", "2026-03-22T10:30:00"),
                )
            )
        conn.commit()
        conn.close()

    def tearDown(self):
        # 清理临时 SQLite 数据库目录
        for d in self._tmp_dirs:
            try:
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
        # 还原测试前的单例，保证跨模块运行时不破坏其他测试的状态
        XtQuantManager._instance = self._prev_instance

    # ---- /api/positions: 字段映射 + 顶层格式 ----

    def test_positions_status_at_top_level(self):
        r = self.client.get("/api/positions")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "success")
        self.assertIn("data", body)
        self.assertIn("data_version", body)  # 顶层字段
        self.assertIn("no_change", body)

    def test_positions_english_keys(self):
        r = self.client.get("/api/positions", headers={"X-Account-Id": ACC1})
        positions = r.json()["data"]["positions"]
        self.assertEqual(len(positions), 1)
        p = positions[0]
        for key in ("stock_code", "stock_name", "volume", "available", "cost_price",
                    "current_price", "market_value", "profit_ratio",
                    "stop_loss_price", "open_date"):
            self.assertIn(key, p, f"缺少字段 {key}")
        self.assertEqual(p["stock_code"], "000001")
        self.assertEqual(p["volume"], 1000)
        self.assertEqual(p["cost_price"], 10.0)

    def test_positions_stock_name_from_sqlite(self):
        """股票名称从 SQLite 持久化字段获取（position_manager 写入）"""
        r = self.client.get("/api/positions", headers={"X-Account-Id": ACC1})
        p = r.json()["data"]["positions"][0]
        self.assertEqual(p["stock_name"], "平安银行")

    def test_positions_stock_name_fallback_to_code(self):
        """无 SQLite 数据的股票回退为代码作为名称"""
        r = self.client.get("/api/positions", headers={"X-Account-Id": ACC2})
        p = r.json()["data"]["positions"][0]
        self.assertEqual(p["stock_name"], "招商银行")

    def test_positions_open_date_from_sqlite(self):
        """建仓日期从 SQLite 读取"""
        r = self.client.get("/api/positions", headers={"X-Account-Id": ACC1})
        p = r.json()["data"]["positions"][0]
        self.assertEqual(p["open_date"], "2026-03-15")

    def test_positions_stop_loss_from_sqlite(self):
        """止损价优先使用 SQLite 策略计算值"""
        r1 = self.client.get("/api/positions", headers={"X-Account-Id": ACC1})
        p1 = r1.json()["data"]["positions"][0]
        self.assertEqual(p1["stop_loss_price"], 9.25)  # SQLite 精确值
        r2 = self.client.get("/api/positions", headers={"X-Account-Id": ACC2})
        p2 = r2.json()["data"]["positions"][0]
        self.assertEqual(p2["stop_loss_price"], 32.38)

    def test_positions_profit_triggered_from_sqlite(self):
        """止盈触发标记从 SQLite 读取"""
        r1 = self.client.get("/api/positions", headers={"X-Account-Id": ACC1})
        self.assertFalse(r1.json()["data"]["positions"][0]["profit_triggered"])
        r2 = self.client.get("/api/positions", headers={"X-Account-Id": ACC2})
        self.assertTrue(r2.json()["data"]["positions"][0]["profit_triggered"])

    def test_positions_highest_price_from_sqlite(self):
        """最高价从 SQLite 读取（非 QMT 现价）"""
        r1 = self.client.get("/api/positions", headers={"X-Account-Id": ACC1})
        self.assertEqual(r1.json()["data"]["positions"][0]["highest_price"], 11.2)
        r2 = self.client.get("/api/positions", headers={"X-Account-Id": ACC2})
        self.assertEqual(r2.json()["data"]["positions"][0]["highest_price"], 38.5)

    def test_positions_grid_session_active_from_sqlite(self):
        """网关兼容持仓应从 SQLite 网格会话识别活跃状态，兼容裸代码/带后缀。"""
        r1 = self.client.get("/api/positions", headers={"X-Account-Id": ACC1})
        self.assertTrue(r1.json()["data"]["positions"][0]["grid_session_active"])
        r2 = self.client.get("/api/positions", headers={"X-Account-Id": ACC2})
        self.assertFalse(r2.json()["data"]["positions"][0]["grid_session_active"])

    def test_grid_sessions_compat_endpoint_from_sqlite(self):
        """网关模式 /api/grid/sessions 应返回 SQLite 中的网格会话。"""
        r = self.client.get("/api/grid/sessions", headers={"X-Account-Id": ACC1})
        body = r.json()
        self.assertEqual(body["status"], "success")
        self.assertTrue(body["success"])
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["sessions"][0]["stock_code"], "000001.SZ")
        self.assertEqual(body["sessions"][0]["status"], "active")

    def test_positions_current_price_computed(self):
        """市价为 None 时应从 市值/股票余额 估算 = 10.5"""
        r = self.client.get("/api/positions", headers={"X-Account-Id": ACC1})
        p = r.json()["data"]["positions"][0]
        # market_value = 10.5 * 1000 = 10500; current = 10500/1000 = 10.5
        self.assertAlmostEqual(p["current_price"], 10.5, places=2)
        # profit_ratio = 100 * (10.5-10)/10 = 5.0 (百分比，与 Flask 对齐)
        self.assertAlmostEqual(p["profit_ratio"], 5.0, places=2)

    def test_positions_metrics_computed(self):
        r = self.client.get("/api/positions", headers={"X-Account-Id": ACC1})
        metrics = r.json()["data"]["metrics"]
        self.assertEqual(metrics["position_count"], 1)
        self.assertAlmostEqual(metrics["total_market_value"], 10500.0, places=1)

    # ---- X-Account-Id 账号隔离 ----

    def test_account_isolation_via_header(self):
        r1 = self.client.get("/api/positions", headers={"X-Account-Id": ACC1})
        r2 = self.client.get("/api/positions", headers={"X-Account-Id": ACC2})
        p1 = r1.json()["data"]["positions"][0]
        p2 = r2.json()["data"]["positions"][0]
        self.assertEqual(p1["stock_code"], "000001")
        self.assertEqual(p2["stock_code"], "600036")
        self.assertNotEqual(p1["stock_code"], p2["stock_code"])

    def test_no_header_falls_back_to_first_account(self):
        r = self.client.get("/api/positions")
        p = r.json()["data"]["positions"][0]
        # 无 header → 第一个注册账号 ACC1
        self.assertEqual(p["stock_code"], "000001")

    def test_invalid_account_id_falls_back(self):
        r = self.client.get("/api/positions", headers={"X-Account-Id": "9999999"})
        # 不存在的账号 → fallback 到第一个
        self.assertEqual(r.json()["status"], "success")
        self.assertEqual(len(r.json()["data"]["positions"]), 1)

    # ---- /api/status ----

    def test_status_top_level_fields(self):
        r = self.client.get("/api/status", headers={"X-Account-Id": ACC2})
        body = r.json()
        self.assertEqual(body["status"], "success")
        self.assertIn("account", body)        # 顶层
        self.assertIn("settings", body)        # 顶层
        self.assertIn("isMonitoring", body)
        self.assertEqual(body["account"]["id"], ACC2)
        self.assertIn("availableBalance", body["account"])

    # ---- /api/connection/status ----

    def test_connection_status_connected_top_level(self):
        r = self.client.get("/api/connection/status", headers={"X-Account-Id": ACC1})
        body = r.json()
        self.assertEqual(body["status"], "success")
        self.assertIn("connected", body)       # 顶层，不嵌套在 data
        self.assertTrue(body["connected"])

    # ---- /api/config ----

    def test_config_data_and_ranges_top_level(self):
        r = self.client.get("/api/config")
        body = r.json()
        self.assertEqual(body["status"], "success")
        self.assertIn("data", body)
        self.assertIn("ranges", body)          # 顶层
        self.assertIn("singleBuyAmount", body["data"])

    # ---- /api/trade-records: 数组 + BUY/SELL 映射 ----

    def test_trade_records_data_is_array(self):
        r = self.client.get("/api/trade-records", headers={"X-Account-Id": ACC1})
        body = r.json()
        self.assertEqual(body["status"], "success")
        self.assertIsInstance(body["data"], list)
        self.assertEqual(len(body["data"]), 1)

    def test_trade_records_sell_mapping(self):
        r = self.client.get("/api/trade-records", headers={"X-Account-Id": ACC1})
        t = r.json()["data"][0]
        self.assertEqual(t["trade_type"], "SELL")  # order_type 24 → SELL
        self.assertEqual(t["stock_code"], "000001")
        self.assertEqual(t["price"], 10.5)

    def test_trade_records_buy_mapping(self):
        r = self.client.get("/api/trade-records", headers={"X-Account-Id": ACC2})
        t = r.json()["data"][0]
        self.assertEqual(t["trade_type"], "BUY")   # order_type 23 → BUY
        self.assertEqual(t["stock_code"], "600036")

    def test_trade_records_english_keys(self):
        r = self.client.get("/api/trade-records", headers={"X-Account-Id": ACC1})
        t = r.json()["data"][0]
        for key in ("stock_code", "trade_type", "price", "volume", "trade_id"):
            self.assertIn(key, t, f"缺少字段 {key}")

    # ---- /api/positions-all ----

    def test_positions_all_data_is_array(self):
        r = self.client.get("/api/positions-all", headers={"X-Account-Id": ACC1})
        body = r.json()
        self.assertEqual(body["status"], "success")
        self.assertIsInstance(body["data"], list)
        self.assertEqual(body["data"][0]["stock_code"], "000001")


if __name__ == "__main__":
    unittest.main(verbosity=2)
