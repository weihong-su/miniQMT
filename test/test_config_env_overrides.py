import importlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestConfigEnvOverrides(unittest.TestCase):
    def setUp(self):
        self._env_keys = [
            "GRID_REQUIRE_PROFIT_TRIGGERED",
            "ENABLE_BAOSTOCK_STOCK_NAME_LOOKUP",
            "ENABLE_BAOSTOCK_HISTORY_DATA",
        ]
        self._orig_env = {key: os.environ.get(key) for key in self._env_keys}

    def tearDown(self):
        for key, value in self._orig_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

        if "config" in sys.modules:
            importlib.reload(sys.modules["config"])

    def _reload_config(self):
        if "config" in sys.modules:
            return importlib.reload(sys.modules["config"])

        import config
        return config

    def test_grid_require_profit_triggered_reads_env(self):
        os.environ["GRID_REQUIRE_PROFIT_TRIGGERED"] = "true"
        config = self._reload_config()
        self.assertTrue(config.GRID_REQUIRE_PROFIT_TRIGGERED)

        os.environ["GRID_REQUIRE_PROFIT_TRIGGERED"] = "0"
        config = self._reload_config()
        self.assertFalse(config.GRID_REQUIRE_PROFIT_TRIGGERED)

    def test_baostock_switches_default_to_disabled(self):
        os.environ.pop("ENABLE_BAOSTOCK_STOCK_NAME_LOOKUP", None)
        os.environ.pop("ENABLE_BAOSTOCK_HISTORY_DATA", None)

        config = self._reload_config()

        self.assertFalse(config.ENABLE_BAOSTOCK_STOCK_NAME_LOOKUP)
        self.assertFalse(config.ENABLE_BAOSTOCK_HISTORY_DATA)


class TestDotenvFallback(unittest.TestCase):
    """验证 _load_dotenv_fallback：环境变量为主，.env 仅补充未设置的键。"""

    def _load(self):
        import config
        return config._load_dotenv_fallback

    def _write_env(self, text):
        fd, path = tempfile.mkstemp(suffix=".env")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_env_missing_key_is_filled_from_dotenv(self):
        load = self._load()
        key = "TEST_DOTENV_FILL_KEY"
        os.environ.pop(key, None)
        self.addCleanup(lambda: os.environ.pop(key, None))
        path = self._write_env("%s=from_dotenv\n" % key)
        load(path)
        self.assertEqual(os.environ[key], "from_dotenv")

    def test_existing_env_var_is_not_overridden(self):
        load = self._load()
        key = "TEST_DOTENV_PRIORITY_KEY"
        os.environ[key] = "from_env"
        self.addCleanup(lambda: os.environ.pop(key, None))
        path = self._write_env("%s=from_dotenv\n" % key)
        load(path)
        # 已存在的环境变量优先，.env 不覆盖
        self.assertEqual(os.environ[key], "from_env")

    def test_comments_and_blank_lines_skipped(self):
        load = self._load()
        key = "TEST_DOTENV_COMMENT_KEY"
        os.environ.pop(key, None)
        self.addCleanup(lambda: os.environ.pop(key, None))
        path = self._write_env("# comment line\n\n%s=value1\n" % key)
        load(path)
        self.assertEqual(os.environ[key], "value1")

    def test_quotes_are_stripped(self):
        load = self._load()
        key = "TEST_DOTENV_QUOTE_KEY"
        os.environ.pop(key, None)
        self.addCleanup(lambda: os.environ.pop(key, None))
        path = self._write_env('%s="quoted value"\n' % key)
        load(path)
        self.assertEqual(os.environ[key], "quoted value")

    def test_missing_file_is_noop(self):
        load = self._load()
        # 不存在的路径不应抛异常
        load(os.path.join(tempfile.gettempdir(), "definitely_missing_xyz.env"))


if __name__ == "__main__":
    unittest.main()

