"""
scripts/_launcher.py 部署相关函数的单元测试。

覆盖:
  - check_python_env() 返回字段齐全
  - 首次部署向导的配置生成辅助函数
  - check_account_config() 能识别各种配置异常（文件不存在/JSON 非法/缺字段/
    重复 ID/qmt_path 不存在）以及全 OK 的情况
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import _launcher  # noqa: E402


class TestCheckPythonEnv(unittest.TestCase):
    def test_returns_required_fields(self):
        info = _launcher.check_python_env()
        self.assertIn("python", info)
        self.assertIn("executable", info)
        self.assertIn("python_supported", info)
        self.assertIn("python_issue", info)
        self.assertIsInstance(info["missing"], list)
        self.assertIsInstance(info["xqm_missing"], list)
        self.assertIsInstance(info["rpc_missing"], list)
        self.assertIsInstance(info["special_missing"], list)
        self.assertIsInstance(info["python_supported"], bool)
        # 当前测试环境一定能 import 自己（pandas 等是 miniQMT 必需依赖，应已安装）
        # 但不强求，因为某些精简 venv 可能确实缺；只断言结构正确
        self.assertRegex(info["python"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(info["executable"], sys.executable)


class TestSetupWizardHelpers(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="launcher_wizard_"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_discover_qmt_paths_returns_existing_unique_paths(self):
        existing = self.tmpdir / "QMT" / "userdata_mini"
        existing.mkdir(parents=True)
        missing = self.tmpdir / "missing" / "userdata_mini"

        paths = _launcher.discover_qmt_paths([
            str(missing),
            str(existing),
            str(existing),
        ])

        self.assertEqual(paths, [str(existing)])

    def test_build_account_config_trims_values(self):
        cfg = _launcher.build_account_config("  123456  ", "  C:/QMT/userdata_mini  ", " stock ")

        self.assertEqual(cfg["account_id"], "123456")
        self.assertEqual(cfg["account_type"], "STOCK")
        self.assertEqual(cfg["qmt_path"], "C:/QMT/userdata_mini")

    def test_ensure_env_file_creates_and_does_not_overwrite(self):
        env_path = self.tmpdir / ".env"

        created, path = _launcher.ensure_env_file(env_path)
        self.assertTrue(created)
        self.assertEqual(path, env_path)
        self.assertIn("ENABLE_QMT_RPC_FALLBACK=false", env_path.read_text(encoding="utf-8"))

        env_path.write_text("QMT_API_TOKEN=secret\n", encoding="utf-8")
        created, _ = _launcher.ensure_env_file(env_path)
        self.assertFalse(created)
        self.assertEqual(env_path.read_text(encoding="utf-8"), "QMT_API_TOKEN=secret\n")

    def test_ensure_stock_pool_file_creates_empty_pool(self):
        pool_path = self.tmpdir / "stock_pool.json"

        created, path = _launcher.ensure_stock_pool_file(pool_path)

        self.assertTrue(created)
        self.assertEqual(path, pool_path)
        self.assertEqual(json.loads(pool_path.read_text(encoding="utf-8")), [])

    def test_ensure_account_config_file_creates_single_account_config(self):
        cfg_path = self.tmpdir / "account_config.json"

        created, path = _launcher.ensure_account_config_file(
            "ACC001",
            "C:/QMT/userdata_mini",
            path=cfg_path,
        )

        self.assertTrue(created)
        self.assertEqual(path, cfg_path)
        payload = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["account_id"], "ACC001")
        self.assertEqual(payload["account_type"], "STOCK")
        self.assertEqual(payload["qmt_path"], "C:/QMT/userdata_mini")


class TestSetupWizardCommand(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="launcher_wizard_cmd_"))
        self.qmt_path = self.tmpdir / "qmt" / "userdata_mini"
        self.qmt_path.mkdir(parents=True)
        self._orig_cfg = _launcher.CONFIG_PATH
        self._orig_env = _launcher.ENV_PATH
        self._orig_stock_pool = _launcher.STOCK_POOL_PATH
        self._orig_xqm_config = _launcher.XQM_CONFIG_PATH
        _launcher.CONFIG_PATH = self.tmpdir / "account_config.json"
        _launcher.ENV_PATH = self.tmpdir / ".env"
        _launcher.STOCK_POOL_PATH = self.tmpdir / "stock_pool.json"
        _launcher.XQM_CONFIG_PATH = self.tmpdir / "xtquant_manager_config.json"

    def tearDown(self):
        _launcher.CONFIG_PATH = self._orig_cfg
        _launcher.ENV_PATH = self._orig_env
        _launcher.STOCK_POOL_PATH = self._orig_stock_pool
        _launcher.XQM_CONFIG_PATH = self._orig_xqm_config
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cmd_setup_wizard_creates_minimal_safe_files(self):
        env_info = {
            "python": "3.9.18",
            "executable": sys.executable,
            "python_supported": True,
            "python_issue": "",
            "missing": [],
            "xqm_missing": [],
            "rpc_missing": [],
            "special_missing": [],
        }

        with patch.object(_launcher, "check_python_env", return_value=env_info), \
             patch.object(_launcher, "discover_qmt_paths", return_value=[str(self.qmt_path)]), \
             patch("builtins.input", side_effect=["ACC001", "", "", "y"]), \
             patch("sys.stdout", new_callable=io.StringIO):
            rc = _launcher.cmd_setup_wizard(None)

        self.assertEqual(rc, 0)
        self.assertTrue(_launcher.ENV_PATH.exists())
        self.assertTrue(_launcher.STOCK_POOL_PATH.exists())
        self.assertTrue(_launcher.CONFIG_PATH.exists())
        self.assertTrue(_launcher.XQM_CONFIG_PATH.exists())

        account_cfg = json.loads(_launcher.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(account_cfg["account_id"], "ACC001")
        self.assertEqual(account_cfg["qmt_path"], str(self.qmt_path))

        xqm_cfg = json.loads(_launcher.XQM_CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(xqm_cfg["host"], "127.0.0.1")
        self.assertEqual(xqm_cfg["accounts"][0]["account_id"], "ACC001")


class TestCheckAccountConfig(unittest.TestCase):
    def setUp(self):
        # 把 _launcher 的 CONFIG_PATH 指到临时目录，避免污染项目根
        self.tmpdir = Path(tempfile.mkdtemp(prefix="launcher_cfg_"))
        self.cfg_path = self.tmpdir / "account_config.json"
        self._orig_cfg = _launcher.CONFIG_PATH
        _launcher.CONFIG_PATH = self.cfg_path

    def tearDown(self):
        _launcher.CONFIG_PATH = self._orig_cfg
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write(self, payload):
        self.cfg_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_missing_file(self):
        r = _launcher.check_account_config()
        self.assertFalse(r["file_exists"])
        self.assertIn("不存在", r["error"])

    def test_invalid_json(self):
        self.cfg_path.write_text("{not valid json", encoding="utf-8")
        r = _launcher.check_account_config()
        self.assertTrue(r["file_exists"])
        self.assertFalse(r["json_valid"])
        self.assertIn("JSON", r["error"])

    def test_all_valid(self):
        good_path = self.tmpdir / "qmt_A" / "userdata_mini"
        good_path.mkdir(parents=True)
        self._write({
            "account_id": "AAA",
            "accounts": [
                {"account_id": "AAA", "qmt_path": str(good_path)},
            ],
        })
        r = _launcher.check_account_config()
        self.assertTrue(r["json_valid"])
        self.assertEqual(len(r["accounts"]), 1)
        self.assertEqual(r["accounts"][0]["issues"], [])
        self.assertTrue(r["accounts"][0]["qmt_path_exists"])

    def test_qmt_path_missing(self):
        self._write({
            "accounts": [
                {"account_id": "AAA", "qmt_path": "C:/this/path/should/not/exist"},
            ],
        })
        r = _launcher.check_account_config()
        self.assertEqual(len(r["accounts"]), 1)
        self.assertIn("qmt_path 不存在", r["accounts"][0]["issues"])
        self.assertFalse(r["accounts"][0]["qmt_path_exists"])

    def test_missing_account_id_and_qmt_path(self):
        self._write({
            "accounts": [
                {"account_type": "STOCK"},  # 两者都缺
            ],
        })
        r = _launcher.check_account_config()
        issues = r["accounts"][0]["issues"]
        self.assertIn("缺少 account_id", issues)
        self.assertIn("缺少 qmt_path", issues)

    def test_duplicate_account_id(self):
        good_path = self.tmpdir / "qmt_X" / "userdata_mini"
        good_path.mkdir(parents=True)
        self._write({
            "accounts": [
                {"account_id": "DUP", "qmt_path": str(good_path)},
                {"account_id": "DUP", "qmt_path": str(good_path)},
            ],
        })
        r = _launcher.check_account_config()
        self.assertEqual(len(r["accounts"]), 2)
        # 第一个是首次出现，没有重复问题；第二个被标记
        self.assertNotIn("account_id 重复", r["accounts"][0]["issues"])
        self.assertIn("account_id 重复", r["accounts"][1]["issues"])

    def test_single_account_format_compat(self):
        """没有 accounts 数组 → 用顶层字段兜底为单账号。"""
        good_path = self.tmpdir / "qmt_S" / "userdata_mini"
        good_path.mkdir(parents=True)
        self._write({
            "account_id": "SOLO",
            "qmt_path": str(good_path),
        })
        r = _launcher.check_account_config()
        self.assertEqual(len(r["accounts"]), 1)
        self.assertEqual(r["accounts"][0]["account_id"], "SOLO")
        self.assertEqual(r["accounts"][0]["issues"], [])


class TestCmdReturnCodes(unittest.TestCase):
    """端到端验证 cmd_* 命令的退出码语义。"""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="launcher_rc_"))
        self.cfg_path = self.tmpdir / "account_config.json"
        self._orig_cfg = _launcher.CONFIG_PATH
        _launcher.CONFIG_PATH = self.cfg_path

    def tearDown(self):
        _launcher.CONFIG_PATH = self._orig_cfg
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_check_config_returns_0_when_all_ok(self):
        good_path = self.tmpdir / "qmt" / "userdata_mini"
        good_path.mkdir(parents=True)
        self.cfg_path.write_text(json.dumps({
            "accounts": [{"account_id": "OK", "qmt_path": str(good_path)}],
        }), encoding="utf-8")
        self.assertEqual(_launcher.cmd_check_config(None), 0)

    def test_check_config_returns_nonzero_when_path_missing(self):
        self.cfg_path.write_text(json.dumps({
            "accounts": [{"account_id": "X", "qmt_path": "C:/nope"}],
        }), encoding="utf-8")
        self.assertNotEqual(_launcher.cmd_check_config(None), 0)

    def test_check_config_returns_nonzero_when_file_missing(self):
        # 路径已指到不存在的文件
        self.assertNotEqual(_launcher.cmd_check_config(None), 0)


if __name__ == "__main__":
    unittest.main()
