"""
Phase 6 测试：client.py（XtQuantClient）

使用 FastAPI TestClient 作为后端，通过 monkeypatch _request() 方法将
XtQuantClient 的 HTTP 调用路由到 TestClient。
"""
import sys
import os
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from fastapi.testclient import TestClient

from xtquant_manager.client import XtQuantClient, ClientConfig
from xtquant_manager.manager import XtQuantManager
from xtquant_manager.account import AccountConfig, XtQuantAccount
from xtquant_manager.server import create_app
from xtquant_manager.security import SecurityConfig

from test.test_xtquant_manager.mocks import MockXtTrader, MockXtData, MockStockAccount

# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------

def make_test_app():
    """创建无认证的测试 app"""
    sec = SecurityConfig(
        api_token="",
        allowed_ips=[],
        local_ips=["127.0.0.1", "::1", "localhost", "testclient", "unknown"],
    )
    return create_app(sec)


def inject_mock_account(manager: XtQuantManager, account_id: str = "55009640"):
    """向 manager 注入已连接的 mock 账号"""
    config = AccountConfig(account_id=account_id, qmt_path="mock")
    account = XtQuantAccount(config)
    mock_trader = MockXtTrader()
    mock_xtdata = MockXtData()
    account._xt_trader = mock_trader
    account._acc = MockStockAccount(account_id)
    account._xtdata = mock_xtdata
    account._connected = True
    account._connected_at = time.time()
    account._last_ping_ok_time = time.time()
    manager._accounts[account_id] = account
    return account, mock_trader, mock_xtdata


def make_client_with_test_backend(account_id: str = "55009640",
                                   test_client: TestClient = None) -> XtQuantClient:
    """
    创建 XtQuantClient，并将其 _request 方法 monkeypatch 到 TestClient。

    XtQuantClient 认为自己在与 http://testserver 通信，
    实际上请求被路由到 FastAPI TestClient（不启动真实服务器）。
    """
    cfg = ClientConfig(
        base_url="http://testserver",
        account_id=account_id,
        api_token="",
        max_retries=0,  # 测试中不重试
    )
    client = XtQuantClient(config=cfg)

    # monkeypatch: 将 _request 路由到 TestClient
    def fake_request(method: str, path: str, **kwargs):
        """拦截 httpx 请求，转发给 FastAPI TestClient"""
        try:
            resp = test_client.request(method, path, **kwargs)
            if resp.status_code >= 500:
                return None
            return resp.json()
        except Exception:
            return None

    client._request = fake_request
    return client


# ---------------------------------------------------------------------------
# 测试基类
# ---------------------------------------------------------------------------

class TestClientBase(unittest.TestCase):
    def setUp(self):
        XtQuantManager.reset_instance()
        self.manager = XtQuantManager.get_instance()
        self.app = make_test_app()
        self.test_client = TestClient(self.app, raise_server_exceptions=True)
        self.client = make_client_with_test_backend("55009640", self.test_client)

    def tearDown(self):
        XtQuantManager.reset_instance()


# ---------------------------------------------------------------------------
# 持仓/资产接口测试
# ---------------------------------------------------------------------------

class TestClientPositions(TestClientBase):
    def setUp(self):
        super().setUp()
        self.account, self.mock_trader, self.mock_xtdata = inject_mock_account(
            self.manager, "55009640"
        )

    def test_position_empty(self):
        """无持仓时返回空 DataFrame"""
        import pandas as pd
        df = self.client.position()
        self.assertIsInstance(df, pd.DataFrame)
        self.assertEqual(len(df), 0)

    def test_position_with_data(self):
        """有持仓时返回正确的 DataFrame"""
        import pandas as pd
        self.mock_trader.add_mock_position("000001.SZ", 100, 10.0)
        df = self.client.position()
        self.assertIsInstance(df, pd.DataFrame)
        self.assertGreater(len(df), 0)
        self.assertIn("证券代码", df.columns)
        self.assertIn("股票余额", df.columns)
        self.assertIn("可用余额", df.columns)
        self.assertIn("成本价", df.columns)
        self.assertIn("市值", df.columns)

    def test_position_multiple_stocks(self):
        """多股票持仓"""
        self.mock_trader.add_mock_position("000001.SZ", 100, 10.0)
        self.mock_trader.add_mock_position("600036.SH", 200, 20.0)
        df = self.client.position()
        self.assertEqual(len(df), 2)

    def test_balance_structure(self):
        """balance() 返回含正确列的 DataFrame"""
        import pandas as pd
        df = self.client.balance()
        self.assertIsInstance(df, pd.DataFrame)
        self.assertGreater(len(df), 0)
        self.assertIn("总资产", df.columns)
        self.assertIn("可用金额", df.columns)
        self.assertIn("冻结金额", df.columns)
        self.assertIn("持仓市值", df.columns)

    def test_query_stock_asset_dict(self):
        """query_stock_asset() 返回 dict"""
        asset = self.client.query_stock_asset()
        self.assertIsInstance(asset, dict)
        self.assertIn("总资产", asset)

    def test_position_account_not_registered(self):
        """账号未注册时返回空 DataFrame"""
        import pandas as pd
        client = make_client_with_test_backend("NONEXISTENT", self.test_client)
        df = client.position()
        self.assertIsInstance(df, pd.DataFrame)
        self.assertEqual(len(df), 0)


# ---------------------------------------------------------------------------
# 下单/撤单接口测试
# ---------------------------------------------------------------------------

class TestClientOrders(TestClientBase):
    def setUp(self):
        super().setUp()
        self.account, self.mock_trader, _ = inject_mock_account(self.manager, "55009640")

    def test_order_stock_success(self):
        """order_stock() 成功返回正整数 order_id"""
        order_id = self.client.order_stock(
            stock_code="000001.SZ",
            order_type=23,
            order_volume=100,
            price_type=11,
            price=10.5,
        )
        self.assertGreater(order_id, 0)

    def test_buy_returns_order_id(self):
        """buy() 返回 order_id"""
        order_id = self.client.buy(
            security="000001.SZ",
            order_type=23,
            amount=100,
            price_type=11,
            price=10.5,
        )
        self.assertGreater(order_id, 0)

    def test_sell_returns_order_id(self):
        """sell() 返回 order_id"""
        order_id = self.client.sell(
            security="000001.SZ",
            order_type=24,
            amount=100,
            price_type=11,
            price=10.5,
        )
        self.assertGreater(order_id, 0)

    def test_cancel_order_success(self):
        """cancel_order_stock() 成功返回 0"""
        order_id = self.client.order_stock(
            stock_code="000001.SZ",
            order_type=23,
            order_volume=100,
            price_type=11,
            price=10.5,
        )
        result = self.client.cancel_order_stock(order_id)
        self.assertEqual(result, 0)

    def test_order_account_not_found(self):
        """账号不存在时 order_stock() 返回 -1"""
        client = make_client_with_test_backend("NONEXISTENT", self.test_client)
        result = client.order_stock("000001.SZ", 23, 100, 11, 10.5)
        self.assertEqual(result, -1)

    def test_query_orders_dataframe(self):
        """query_stock_orders() 下单后返回含数据的 DataFrame"""
        import pandas as pd
        self.client.order_stock("000001.SZ", 23, 100, 11, 10.5)
        df = self.client.query_stock_orders()
        self.assertIsInstance(df, pd.DataFrame)
        self.assertGreater(len(df), 0)

    def test_query_trades_dataframe(self):
        """query_stock_trades() 下单后返回含数据的 DataFrame"""
        import pandas as pd
        self.client.order_stock("000001.SZ", 23, 100, 11, 10.5)
        df = self.client.query_stock_trades()
        self.assertIsInstance(df, pd.DataFrame)
        self.assertGreater(len(df), 0)

    def test_query_orders_empty(self):
        """未下单时 query_stock_orders() 返回空 DataFrame"""
        import pandas as pd
        df = self.client.query_stock_orders()
        self.assertIsInstance(df, pd.DataFrame)


# ---------------------------------------------------------------------------
# 行情接口测试
# ---------------------------------------------------------------------------

class TestClientMarket(TestClientBase):
    def setUp(self):
        super().setUp()
        inject_mock_account(self.manager, "55009640")

    def test_get_full_tick(self):
        """get_full_tick() 返回 dict 含 tick 数据"""
        result = self.client.get_full_tick(["000001.SZ"])
        self.assertIsInstance(result, dict)
        self.assertIn("000001.SZ", result)

    def test_get_full_tick_multiple(self):
        """多股票 get_full_tick()"""
        result = self.client.get_full_tick(["000001.SZ", "600036.SH"])
        self.assertIn("000001.SZ", result)
        self.assertIn("600036.SH", result)

    def test_get_market_data_ex(self):
        """get_market_data_ex() 返回 dict"""
        result = self.client.get_market_data_ex(
            fields=[],
            stock_list=["000001.SZ"],
            period="1d",
        )
        self.assertIsInstance(result, dict)

    def test_download_history_data(self):
        """download_history_data() 返回 bool"""
        result = self.client.download_history_data(
            stock_code="000001.SZ",
            period="1d",
        )
        self.assertIsInstance(result, bool)


# ---------------------------------------------------------------------------
# 可观测性接口测试
# ---------------------------------------------------------------------------

class TestClientObservability(TestClientBase):
    def setUp(self):
        super().setUp()
        inject_mock_account(self.manager, "55009640")

    def test_health(self):
        """health() 返回健康状态"""
        result = self.client.health()
        self.assertIsInstance(result, dict)
        self.assertIn("total", result)

    def test_is_connected_true(self):
        """is_connected() 对已注册账号返回 True"""
        self.assertTrue(self.client.is_connected())

    def test_is_connected_false(self):
        """is_connected() 对未注册账号返回 False"""
        client = make_client_with_test_backend("NONEXISTENT", self.test_client)
        self.assertFalse(client.is_connected())

    def test_get_metrics(self):
        """get_metrics() 在触发调用后返回指标数据"""
        self.client.position()  # 触发一次调用
        result = self.client.get_metrics()
        self.assertIsInstance(result, dict)

    def test_get_account_status(self):
        """get_account_status() 返回状态 dict"""
        result = self.client.get_account_status()
        self.assertIsInstance(result, dict)
        self.assertIn("connected", result)
        self.assertTrue(result["connected"])


# ---------------------------------------------------------------------------
# 连接失败回退测试
# ---------------------------------------------------------------------------

class TestClientFallback(unittest.TestCase):
    def test_connection_error_returns_empty_dataframe(self):
        """服务不可达时 position() 返回空 DataFrame"""
        import pandas as pd
        client = XtQuantClient(
            config=ClientConfig(
                base_url="http://localhost:19999",  # 不存在的端口
                account_id="test",
                max_retries=0,
            )
        )
        try:
            result = client.position()
            self.assertIsInstance(result, pd.DataFrame)
            self.assertEqual(len(result), 0)
        finally:
            client.close()

    def test_connection_error_order_returns_minus1(self):
        """服务不可达时 order_stock() 返回 -1"""
        client = XtQuantClient(
            config=ClientConfig(
                base_url="http://localhost:19999",
                account_id="test",
                max_retries=0,
            )
        )
        try:
            result = client.order_stock("000001.SZ", 23, 100, 11, 10.5)
            self.assertEqual(result, -1)
        finally:
            client.close()

    def test_client_context_manager(self):
        """客户端支持 with 语句"""
        with XtQuantClient(
            config=ClientConfig(base_url="http://localhost:19999", account_id="test")
        ) as client:
            self.assertIsNotNone(client)
        # 退出 with 后 session 已关闭
        self.assertIsNone(client._session)

    def test_kwargs_initialization(self):
        """支持 kwargs 快速初始化"""
        client = XtQuantClient(
            base_url="http://localhost:8888",
            account_id="12345",
            api_token="secret",
        )
        self.assertEqual(client.config.base_url, "http://localhost:8888")
        self.assertEqual(client.config.account_id, "12345")
        self.assertEqual(client.config.api_token, "secret")
        self.assertEqual(client._headers.get("X-API-Token"), "secret")


if __name__ == "__main__":
    unittest.main(verbosity=2)
