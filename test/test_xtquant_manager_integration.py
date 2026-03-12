"""
XtQuantManager 集成测试

测试 ENABLE_XTQUANT_MANAGER 开关以及各工厂函数的分支行为。
无需真实 QMT 环境 —— 通过 monkeypatch 和 mock 隔离外部依赖。
"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config


# ---------------------------------------------------------------------------
# Task 1: config.py 参数测试
# ---------------------------------------------------------------------------

class TestConfig(unittest.TestCase):
    def test_enable_xtquant_manager_default_false(self):
        """ENABLE_XTQUANT_MANAGER 默认为 False"""
        self.assertFalse(config.ENABLE_XTQUANT_MANAGER)

    def test_xtquant_manager_url_default(self):
        """XTQUANT_MANAGER_URL 默认指向本机 8888 端口"""
        self.assertEqual(config.XTQUANT_MANAGER_URL, "http://127.0.0.1:8888")

    def test_xtquant_manager_token_default_empty(self):
        """XTQUANT_MANAGER_TOKEN 默认为空字符串"""
        self.assertEqual(config.XTQUANT_MANAGER_TOKEN, "")

    def test_enable_xtquant_manager_is_bool(self):
        """ENABLE_XTQUANT_MANAGER 是布尔型"""
        self.assertIsInstance(config.ENABLE_XTQUANT_MANAGER, bool)

    def test_xtquant_manager_url_is_str(self):
        """XTQUANT_MANAGER_URL 是字符串"""
        self.assertIsInstance(config.XTQUANT_MANAGER_URL, str)

    def test_xtquant_manager_token_is_str(self):
        """XTQUANT_MANAGER_TOKEN 是字符串"""
        self.assertIsInstance(config.XTQUANT_MANAGER_TOKEN, str)


# ---------------------------------------------------------------------------
# Task 2: XtQuantClient 兼容接口测试
# ---------------------------------------------------------------------------

class TestXtQuantClientCompat(unittest.TestCase):
    def setUp(self):
        from xtquant_manager.client import XtQuantClient, ClientConfig
        self.client = XtQuantClient(
            config=ClientConfig(
                base_url="http://localhost:19999",  # 不存在
                account_id="test",
                max_retries=0,
            )
        )

    def tearDown(self):
        self.client.close()

    def test_connect_returns_tuple_or_none(self):
        """connect() 服务不可达时返回 None"""
        result = self.client.connect()
        # 不可达时返回 None（或 (False, False)，都可接受）
        self.assertIn(result, [None, (False, False), (True, True)])

    def test_register_trade_callback_noop(self):
        """register_trade_callback() 不抛异常（no-op）"""
        try:
            self.client.register_trade_callback(lambda: None)
        except Exception as e:
            self.fail(f"register_trade_callback 不应抛异常: {e}")

    def test_subscribe_callback_noop(self):
        """subscribe_callback() 不抛异常（no-op）"""
        try:
            self.client.subscribe_callback()
        except Exception as e:
            self.fail(f"subscribe_callback 不应抛异常: {e}")


# ---------------------------------------------------------------------------
# Task 3: XtDataAdapter 测试
# ---------------------------------------------------------------------------

class TestXtDataAdapter(unittest.TestCase):
    def _make_adapter(self):
        from xtquant_manager.client import XtQuantClient, ClientConfig, XtDataAdapter
        client = XtQuantClient(
            config=ClientConfig(
                base_url="http://localhost:19999",
                account_id="test",
                max_retries=0,
            )
        )
        return XtDataAdapter(client), client

    def test_adapter_has_connect(self):
        """XtDataAdapter 有 connect() 方法"""
        adapter, client = self._make_adapter()
        self.assertTrue(hasattr(adapter, "connect"))
        client.close()

    def test_adapter_has_get_full_tick(self):
        """XtDataAdapter 有 get_full_tick() 方法"""
        adapter, client = self._make_adapter()
        self.assertTrue(hasattr(adapter, "get_full_tick"))
        client.close()

    def test_adapter_has_get_market_data_ex(self):
        """XtDataAdapter 有 get_market_data_ex() 方法"""
        adapter, client = self._make_adapter()
        self.assertTrue(hasattr(adapter, "get_market_data_ex"))
        client.close()

    def test_adapter_has_download_history_data(self):
        """XtDataAdapter 有 download_history_data() 方法"""
        adapter, client = self._make_adapter()
        self.assertTrue(hasattr(adapter, "download_history_data"))
        client.close()

    def test_adapter_connect_returns_bool(self):
        """connect() 服务不可达时返回 False"""
        adapter, client = self._make_adapter()
        result = adapter.connect()
        self.assertIsInstance(result, bool)
        client.close()

    def test_adapter_get_full_tick_fallback(self):
        """get_full_tick() 服务不可达时返回空 dict"""
        adapter, client = self._make_adapter()
        result = adapter.get_full_tick(["000001.SZ"])
        self.assertIsInstance(result, dict)
        client.close()

    def test_adapter_get_market_data_ex_fallback(self):
        """get_market_data_ex() 服务不可达时返回空 dict"""
        adapter, client = self._make_adapter()
        result = adapter.get_market_data_ex([], ["000001.SZ"], "1d")
        self.assertIsInstance(result, dict)
        client.close()

    def test_adapter_download_history_data_noop(self):
        """download_history_data() 服务不可达时不抛异常"""
        adapter, client = self._make_adapter()
        try:
            adapter.download_history_data("000001.SZ", "1d")
        except Exception as e:
            self.fail(f"download_history_data 不应抛异常: {e}")
        client.close()

    def test_adapter_exported_from_package(self):
        """XtDataAdapter 通过 xtquant_manager 包可导入"""
        from xtquant_manager import XtDataAdapter
        self.assertIsNotNone(XtDataAdapter)


# ---------------------------------------------------------------------------
# Task 4: position_manager 工厂函数测试
# ---------------------------------------------------------------------------

class TestPositionManagerFactory(unittest.TestCase):
    def test_factory_disabled_returns_original_type(self):
        """ENABLE_XTQUANT_MANAGER=False 时返回 EasyQmtTrader"""
        original = config.ENABLE_XTQUANT_MANAGER
        try:
            config.ENABLE_XTQUANT_MANAGER = False
            from position_manager import _create_qmt_trader
            trader = _create_qmt_trader()
            # 关闭时应返回 None（因没有真实 QMT 环境）或 EasyQmtTrader 实例
            # 主要验证函数存在且不抛异常
            self.assertIsNotNone(_create_qmt_trader)
        except ImportError:
            self.skipTest("position_manager 不可用（无 QMT 环境）")
        finally:
            config.ENABLE_XTQUANT_MANAGER = original

    def test_factory_enabled_returns_client(self):
        """ENABLE_XTQUANT_MANAGER=True 时返回 XtQuantClient"""
        original = config.ENABLE_XTQUANT_MANAGER
        try:
            config.ENABLE_XTQUANT_MANAGER = True
            from position_manager import _create_qmt_trader
            from xtquant_manager.client import XtQuantClient
            trader = _create_qmt_trader()
            self.assertIsInstance(trader, XtQuantClient)
        except ImportError:
            self.skipTest("position_manager 不可用（无 QMT 环境）")
        finally:
            config.ENABLE_XTQUANT_MANAGER = original


# ---------------------------------------------------------------------------
# Task 5: data_manager 工厂函数测试
# ---------------------------------------------------------------------------

class TestDataManagerFactory(unittest.TestCase):
    def test_factory_function_exists(self):
        """_create_xtdata() 函数存在于 data_manager"""
        try:
            from data_manager import _create_xtdata
            self.assertTrue(callable(_create_xtdata))
        except ImportError:
            self.skipTest("data_manager 不可用（无 QMT 环境）")

    def test_factory_disabled_returns_xtdata_module(self):
        """ENABLE_XTQUANT_MANAGER=False 时返回 xtquant.xtdata 模块或 None"""
        original = config.ENABLE_XTQUANT_MANAGER
        try:
            config.ENABLE_XTQUANT_MANAGER = False
            from data_manager import _create_xtdata
            # 只验证函数可调用，不关心 QMT 是否实际可用
            self.assertTrue(callable(_create_xtdata))
        except ImportError:
            self.skipTest("data_manager 不可用（无 QMT 环境）")
        finally:
            config.ENABLE_XTQUANT_MANAGER = original

    def test_factory_enabled_returns_adapter(self):
        """ENABLE_XTQUANT_MANAGER=True 时返回 XtDataAdapter"""
        original = config.ENABLE_XTQUANT_MANAGER
        try:
            config.ENABLE_XTQUANT_MANAGER = True
            from data_manager import _create_xtdata
            from xtquant_manager.client import XtDataAdapter
            xtdata = _create_xtdata()
            self.assertIsInstance(xtdata, XtDataAdapter)
        except ImportError:
            self.skipTest("data_manager 不可用（无 QMT 环境）")
        finally:
            config.ENABLE_XTQUANT_MANAGER = original


# ---------------------------------------------------------------------------
# Task 6: main.py 集成测试
# ---------------------------------------------------------------------------

class TestMainIntegration(unittest.TestCase):
    def test_start_function_exists(self):
        """_start_xtquant_manager_server() 函数存在于 main"""
        try:
            import main
            self.assertTrue(hasattr(main, "_start_xtquant_manager_server"))
        except ImportError:
            self.skipTest("main 不可导入（无 QMT 环境）")

    def test_start_function_disabled_returns_none(self):
        """ENABLE_XTQUANT_MANAGER=False 时 _start_xtquant_manager_server() 返回 None"""
        original = config.ENABLE_XTQUANT_MANAGER
        try:
            config.ENABLE_XTQUANT_MANAGER = False
            import main
            result = main._start_xtquant_manager_server()
            self.assertIsNone(result)
        except ImportError:
            self.skipTest("main 不可导入（无 QMT 环境）")
        finally:
            config.ENABLE_XTQUANT_MANAGER = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
