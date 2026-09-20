"""
scripts/_launcher.py 部署相关函数的单元测试。

覆盖:
  - check_python_env() 返回字段齐全
  - 首次部署向导的配置生成辅助函数
  - check_account_config() 能识别各种配置异常（文件不存在/JSON 非法/缺字段/
    重复 ID/qmt_path 不存在）以及全 OK 的情况
"""

import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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


class TestLauncherConsoleOutput(unittest.TestCase):
    class AsciiOnlyStream(io.StringIO):
        @property
        def encoding(self):
            return "ascii"

        def write(self, text):
            text.encode("ascii")
            return super().write(text)

    def test_safe_print_replaces_unencodable_text(self):
        stream = self.AsciiOnlyStream()

        with patch.object(_launcher.sys, "stdout", stream):
            _launcher.print("miniQMT 总控制台 ✓")

        output = stream.getvalue()
        self.assertIn("miniQMT", output)
        self.assertIn("?", output)

    def test_safe_input_prompt_replaces_unencodable_text(self):
        stream = self.AsciiOnlyStream()

        with patch.object(_launcher.sys, "stdout", stream), \
             patch("builtins.input", return_value="q"):
            value = _launcher.input("请选择 总控制台 ✓: ")

        self.assertEqual(value, "q")
        self.assertIn("?", stream.getvalue())

    def test_menu_can_render_on_ascii_only_stdout(self):
        stream = self.AsciiOnlyStream()

        with patch.object(_launcher.sys, "stdout", stream), \
             patch.object(_launcher.os, "system", return_value=0), \
             patch.object(_launcher, "_print_xttrader_status"), \
             patch("builtins.input", return_value="q"):
            rc = _launcher.cmd_menu(None)

        self.assertEqual(rc, 0)
        self.assertIn("miniQMT", stream.getvalue())


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
        self.assertNotIn("accounts", xqm_cfg)


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


class TestAccountFlaskPort(unittest.TestCase):
    """账号 Flask 端口推导必须与 config._apply_account_overrides 一致。"""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="launcher_flask_port_"))
        self.env_path = self.tmpdir / ".env"
        self.env_path.write_text("", encoding="utf-8")
        self._orig_env_path = _launcher.ENV_PATH
        _launcher.ENV_PATH = self.env_path
        self.addCleanup(lambda: setattr(_launcher, "ENV_PATH", self._orig_env_path))
        self.addCleanup(lambda: shutil.rmtree(self.tmpdir, ignore_errors=True))
        self.env_patch = patch.dict(os.environ, {"WEB_SERVER_PORT": ""})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_first_account_gets_5000(self):
        self.assertEqual(_launcher._account_flask_port("A", ["A", "B"]), 5000)

    def test_second_account_gets_5001(self):
        self.assertEqual(_launcher._account_flask_port("B", ["A", "B"]), 5001)

    def test_unknown_account_falls_back_to_5000(self):
        self.assertEqual(_launcher._account_flask_port("Z", ["A", "B"]), 5000)

    def test_reads_config_when_ids_not_supplied(self):
        with patch.object(_launcher, "load_accounts",
                          return_value=[{"account_id": "X"}, {"account_id": "Y"}]):
            self.assertEqual(_launcher._account_flask_port("Y"), 5001)

    def test_reads_web_server_port_from_dotenv(self):
        self.env_path.write_text("WEB_SERVER_PORT=5100\n", encoding="utf-8")

        self.assertEqual(_launcher._flask_base_port(), 5100)
        self.assertEqual(_launcher._account_flask_port("B", ["A", "B"]), 5101)

    def test_process_env_web_server_port_overrides_dotenv(self):
        self.env_path.write_text("WEB_SERVER_PORT=5100\n", encoding="utf-8")

        with patch.dict(os.environ, {"WEB_SERVER_PORT": "5200"}):
            self.assertEqual(_launcher._flask_base_port(), 5200)
            self.assertEqual(_launcher._account_flask_port("B", ["A", "B"]), 5201)


class TestDetectRunningWebPort(unittest.TestCase):
    """autobuy 启动时探测主程序真实 Flask 端口。

    cfg 的 base_url 是静态值(默认 :5000)，实际端口由 WEB_SERVER_PORT + 账号索引
    决定，两者常不一致 —— 若不探测，autobuy 会连到不存在的端口。
    """

    def _accounts(self, *ids):
        return [{"account_id": i} for i in ids]

    def test_returns_port_of_running_account(self):
        with patch.object(_launcher, "load_accounts", return_value=self._accounts("A", "B")), \
             patch.object(_launcher, "_resolve_account_process",
                          side_effect=lambda aid, ids=None: (111, "pid") if aid == "A" else (None, "missing")), \
             patch.object(_launcher, "_account_flask_port", return_value=50000), \
             patch.object(_launcher, "_is_port_in_use", return_value=True):
            port, src = _launcher._detect_running_web_port()
        self.assertEqual(port, 50000)
        self.assertIn("A", src)

    def test_skips_stale_pid_and_uses_next_account(self):
        """第一个账号 PID 已失效时应继续探测下一个。"""
        resolved = {"A": (999, "stale"), "B": (222, "pid")}
        ports = {"A": 5000, "B": 5001}
        with patch.object(_launcher, "load_accounts", return_value=self._accounts("A", "B")), \
             patch.object(_launcher, "_resolve_account_process",
                          side_effect=lambda aid, ids=None: resolved[aid]), \
             patch.object(_launcher, "_account_flask_port",
                          side_effect=lambda aid, ids=None: ports[aid]), \
             patch.object(_launcher, "_is_port_in_use", return_value=True):
            port, src = _launcher._detect_running_web_port()
        self.assertEqual(port, 5001)
        self.assertIn("B", src)

    def test_running_pid_but_port_not_listening_is_skipped(self):
        with patch.object(_launcher, "load_accounts", return_value=self._accounts("A")), \
             patch.object(_launcher, "_resolve_account_process", return_value=(111, "pid")), \
             patch.object(_launcher, "_account_flask_port", return_value=5000), \
             patch.object(_launcher, "_is_port_in_use", return_value=False), \
             patch.object(_launcher, "_flask_base_port", return_value=5000):
            port, _ = _launcher._detect_running_web_port()
        self.assertIsNone(port)

    def test_falls_back_to_base_port_when_listening(self):
        """未匹配到具体账号，但基准端口在监听时仍可用。"""
        with patch.object(_launcher, "load_accounts", return_value=self._accounts("A")), \
             patch.object(_launcher, "_resolve_account_process", return_value=(None, "missing")), \
             patch.object(_launcher, "_account_flask_port", return_value=5000), \
             patch.object(_launcher, "_flask_base_port", return_value=5000), \
             patch.object(_launcher, "_is_port_in_use", return_value=True):
            port, src = _launcher._detect_running_web_port()
        self.assertEqual(port, 5000)
        self.assertIn("基准端口", src)

    def test_no_running_program_returns_none(self):
        with patch.object(_launcher, "load_accounts", return_value=self._accounts("A")), \
             patch.object(_launcher, "_resolve_account_process", return_value=(None, "missing")), \
             patch.object(_launcher, "_account_flask_port", return_value=5000), \
             patch.object(_launcher, "_flask_base_port", return_value=5000), \
             patch.object(_launcher, "_is_port_in_use", return_value=False):
            port, src = _launcher._detect_running_web_port()
        self.assertIsNone(port)
        self.assertIn("未检测到", src)

    def test_empty_account_config_returns_none(self):
        with patch.object(_launcher, "load_accounts", return_value=[]):
            port, src = _launcher._detect_running_web_port()
        self.assertIsNone(port)
        self.assertIn("没有账号", src)

    def test_load_accounts_failure_does_not_raise(self):
        with patch.object(_launcher, "load_accounts", side_effect=RuntimeError("boom")):
            port, src = _launcher._detect_running_web_port()
        self.assertIsNone(port)
        self.assertIn("失败", src)


class TestPortInUse(unittest.TestCase):
    def test_free_port_reports_not_in_use(self):
        self.assertFalse(_launcher._is_port_in_use(59999))

    def test_listening_port_reports_in_use(self):
        import socket
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            self.assertTrue(_launcher._is_port_in_use(port))
        finally:
            srv.close()

    def test_closed_port_after_shutdown(self):
        import socket
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        srv.close()
        self.assertFalse(_launcher._is_port_in_use(port))


class TestAccountProcessResolution(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="launcher_stop_"))
        self.cfg_path = self.tmpdir / "account_config.json"
        self.env_path = self.tmpdir / ".env"
        self.env_path.write_text("", encoding="utf-8")
        self.cfg_path.write_text(json.dumps({
            "accounts": [
                {"account_id": "ACC1", "qmt_path": "C:/qmt1"},
                {"account_id": "ACC2", "qmt_path": "C:/qmt2"},
            ],
        }), encoding="utf-8")
        self._orig_cfg = _launcher.CONFIG_PATH
        self._orig_env = _launcher.ENV_PATH
        _launcher.CONFIG_PATH = self.cfg_path
        _launcher.ENV_PATH = self.env_path

    def tearDown(self):
        _launcher.CONFIG_PATH = self._orig_cfg
        _launcher.ENV_PATH = self._orig_env
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_account_pid_from_port_accepts_current_project_main_process(self):
        cmdline = f'"{sys.executable}" "{_launcher.MAIN_PY}" --account-id ACC1'

        with patch.object(_launcher, "_port_listener_pids",
                          return_value=[4321]), \
             patch.object(_launcher, "pid_alive", return_value=True), \
             patch.object(_launcher, "_process_command_line", return_value=cmdline):
            pid = _launcher._account_pid_from_port("ACC1", ["ACC1", "ACC2"])

        self.assertEqual(pid, 4321)

    def test_account_pid_from_port_ignores_foreign_listener(self):
        with patch.object(_launcher, "_port_listener_pids",
                          return_value=[4321]), \
             patch.object(_launcher, "pid_alive", return_value=True), \
             patch.object(_launcher, "_process_command_line",
                          return_value='"C:/other/app.exe" --port 5000'):
            pid = _launcher._account_pid_from_port("ACC1", ["ACC1", "ACC2"])

        self.assertIsNone(pid)

    def test_cmd_stop_uses_port_fallback_when_pid_file_missing(self):
        pid_path = self.tmpdir / "missing_pid.txt"

        with patch.object(_launcher, "pid_file_for", return_value=pid_path), \
             patch.object(_launcher, "_account_pid_from_port", return_value=4321), \
             patch.object(_launcher, "pid_alive", return_value=True), \
             patch.object(_launcher, "_request_graceful_stop", return_value=True) as graceful, \
             patch.object(_launcher.subprocess, "run") as run, \
             patch.object(_launcher.time, "sleep"), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            rc = _launcher.cmd_stop(
                argparse.Namespace(accounts="ACC1", force=False, timeout=0)
            )

        self.assertEqual(rc, 0)
        graceful.assert_called_once_with("ACC1", 4321)
        run.assert_called_once_with(
            ["taskkill", "/PID", "4321", "/T", "/F"],
            capture_output=True,
        )
        output = stdout.getvalue()
        self.assertIn("通过 Web 端口", output)
        self.assertIn("PID=4321", output)

    def test_graceful_stop_writes_signal_without_ctrl_c(self):
        original_import = __import__

        def fail_on_ctypes(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "ctypes":
                raise AssertionError("停止账号不应再发送 Ctrl+C")
            return original_import(name, globals, locals, fromlist, level)

        with patch.object(_launcher, "PROJECT_ROOT", self.tmpdir), \
             patch.object(_launcher.sys, "platform", "win32"), \
             patch("builtins.__import__", side_effect=fail_on_ctypes):
            ok = _launcher._request_graceful_stop("ACC1", 4321)

        signal_file = self.tmpdir / "data_ACC1" / "stop_signal"
        self.assertTrue(ok)
        self.assertEqual(signal_file.read_text(encoding="ascii"), "4321")

    def test_cmd_status_reports_port_fallback_pid(self):
        with patch.object(_launcher, "_account_pid_from_port",
                          side_effect=lambda acc_id, _ids: 4321 if acc_id == "ACC1" else None), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            rc = _launcher.cmd_status(None)

        self.assertEqual(rc, 0)
        output = stdout.getvalue()
        self.assertIn("ACC1", output)
        self.assertIn("4321", output)
        self.assertIn("运行中(端口)", output)


class TestXtQuantManagerStart(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="launcher_xqm_"))
        self.cfg_path = self.tmpdir / "xtquant_manager_config.json"
        self.env_path = self.tmpdir / ".env"
        self.pid_path = self.tmpdir / ".xqm_manager.pid"
        self.cfg_path.write_text(json.dumps({"accounts": []}), encoding="utf-8")
        self.env_path.write_text("", encoding="utf-8")
        self._orig_xqm_config = _launcher.XQM_CONFIG_PATH
        self._orig_env = _launcher.ENV_PATH
        _launcher.XQM_CONFIG_PATH = self.cfg_path
        _launcher.ENV_PATH = self.env_path

    def tearDown(self):
        _launcher.XQM_CONFIG_PATH = self._orig_xqm_config
        _launcher.ENV_PATH = self._orig_env
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_xqm_port_reads_project_dotenv(self):
        self.env_path.write_text("XQM_PORT=8890\n", encoding="utf-8")

        with patch.dict(os.environ, {"XQM_PORT": ""}):
            self.assertEqual(_launcher._xqm_port(), 8890)

    def test_process_env_xqm_port_overrides_dotenv(self):
        self.env_path.write_text("XQM_PORT=8890\n", encoding="utf-8")

        with patch.dict(os.environ, {"XQM_PORT": "8891"}):
            self.assertEqual(_launcher._xqm_port(), 8891)

    def test_port_conflict_without_healthy_xqm_returns_error_and_does_not_spawn(self):
        with patch.dict(os.environ, {"XQM_PORT": ""}), \
             patch.object(_launcher, "_xqm_is_port_in_use", return_value=True), \
             patch.object(_launcher, "_xqm_health_check", return_value=False), \
             patch.object(_launcher, "_xqm_read_pid", return_value=None), \
             patch.object(_launcher, "_xqm_list_port_listeners", return_value=[{
                 "protocol": "TCP",
                 "local_address": "0.0.0.0:8888",
                 "pid": "1234",
                 "process_name": "python.exe",
             }]), \
             patch.object(_launcher.subprocess, "Popen") as popen, \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            rc = _launcher.cmd_xqm_start(None)

        self.assertEqual(rc, 1)
        popen.assert_not_called()
        output = stdout.getvalue()
        self.assertIn("端口 8888 已被占用", output)
        self.assertIn("PID=1234", output)
        self.assertIn("xtquant_manager 未启动", output)

    def test_start_sets_xqm_log_file_for_child_process(self):
        captured = {}

        class _Proc:
            pid = 4321
            returncode = None

            def poll(self):
                return None

        def _fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env", {})
            captured["stdout_name"] = kwargs.get("stdout").name
            captured["stderr"] = kwargs.get("stderr")
            return _Proc()

        log_path = self.tmpdir / "xqm_test.log"
        with patch.dict(os.environ, {"XQM_PORT": "", "XQM_LOG_FILE": str(log_path)}), \
             patch.object(_launcher, "_xqm_is_port_in_use", return_value=False), \
             patch.object(_launcher, "_xqm_health_check", return_value=True), \
             patch.object(_launcher, "_xqm_pid_file", return_value=self.pid_path), \
             patch.object(_launcher.subprocess, "Popen", side_effect=_fake_popen), \
             patch.object(_launcher.time, "sleep"), \
             patch("sys.stdout", new_callable=io.StringIO):
            rc = _launcher.cmd_xqm_start(None)

        self.assertEqual(rc, 0)
        self.assertIn("-m", captured["cmd"])
        self.assertEqual(captured["env"].get("MINIQMT_LOG_FILE"), str(log_path))
        self.assertEqual(captured["env"].get("XQM_PORT"), "8888")
        self.assertEqual(captured["stdout_name"], str(log_path))
        self.assertEqual(captured["stderr"], _launcher.subprocess.STDOUT)

    def test_start_uses_dotenv_xqm_port(self):
        self.env_path.write_text("XQM_PORT=8890\n", encoding="utf-8")
        captured = {}

        class _Proc:
            pid = 4321
            returncode = None

            def poll(self):
                return None

        def _fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env", {})
            return _Proc()

        with patch.dict(os.environ, {"XQM_PORT": "", "XQM_LOG_FILE": str(self.tmpdir / "xqm_test.log")}), \
             patch.object(_launcher, "_xqm_is_port_in_use", return_value=False), \
             patch.object(_launcher, "_xqm_health_check", return_value=True) as health_check, \
             patch.object(_launcher, "_xqm_pid_file", return_value=self.pid_path), \
             patch.object(_launcher.subprocess, "Popen", side_effect=_fake_popen), \
             patch.object(_launcher.time, "sleep"), \
             patch("sys.stdout", new_callable=io.StringIO):
            rc = _launcher.cmd_xqm_start(None)

        self.assertEqual(rc, 0)
        self.assertIn("8890", captured["cmd"])
        self.assertEqual(captured["env"].get("XQM_PORT"), "8890")
        health_check.assert_called_with(_launcher.XQM_CLIENT_HOST, 8890)

    def test_process_exit_prints_recent_log_tail(self):
        log_path = self.tmpdir / "xqm_test.log"
        log_path.write_text(
            "line 1\n"
            "需要安装 XtQuantManager HTTP 依赖: "
            "pip install -r utils/requirements.txt (缺少 uvicorn)\n",
            encoding="utf-8",
        )

        class _Proc:
            pid = 4321
            returncode = 1

            def poll(self):
                return self.returncode

        with patch.dict(os.environ, {"XQM_PORT": "", "XQM_LOG_FILE": str(log_path)}), \
             patch.object(_launcher, "_xqm_is_port_in_use", return_value=False), \
             patch.object(_launcher, "_xqm_health_check", return_value=False), \
             patch.object(_launcher, "_xqm_pid_file", return_value=self.pid_path), \
             patch.object(_launcher.subprocess, "Popen", return_value=_Proc()), \
             patch.object(_launcher.time, "sleep"), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            rc = _launcher.cmd_xqm_start(None)

        self.assertEqual(rc, 1)
        output = stdout.getvalue()
        self.assertIn("进程已退出", output)
        self.assertIn("最近", output)
        self.assertIn("uvicorn", output)


class TestXtQuantManagerStartupDiagnostics(unittest.TestCase):
    """XtQuantManager 启动失败诊断的集成入口测试。"""

    def test_server_runner_nonblocking_start_raises_on_early_failure(self):
        from xtquant_manager.server_runner import XtQuantServer, XtQuantServerConfig

        server = XtQuantServer(XtQuantServerConfig(enable_stop_profit=True))
        health_monitor = MagicMock()
        stop_profit_monitor = MagicMock()

        def fake_run_uvicorn(instance):
            instance._startup_error = (
                "需要安装 XtQuantManager HTTP 依赖: "
                "pip install -r utils/requirements.txt (缺少 uvicorn)"
            )
            instance._running = False

        with patch("xtquant_manager.server.create_app", return_value=MagicMock()), \
             patch("xtquant_manager.server_runner.XtQuantManager.get_instance",
                   return_value=MagicMock()), \
             patch("xtquant_manager.server_runner.HealthMonitor",
                   return_value=health_monitor), \
             patch("xtquant_manager.server_runner.StopProfitMonitor",
                   return_value=stop_profit_monitor), \
             patch.object(XtQuantServer, "_run_uvicorn", fake_run_uvicorn):
            with self.assertRaisesRegex(RuntimeError, "uvicorn"):
                server.start(blocking=False)

        health_monitor.stop.assert_called_once()
        stop_profit_monitor.stop.assert_called_once()
        self.assertFalse(server.is_running())

    def test_server_runner_records_missing_uvicorn(self):
        from xtquant_manager.server_runner import XtQuantServer, XtQuantServerConfig

        server = XtQuantServer(XtQuantServerConfig(enable_stop_profit=False))
        server._app = object()
        server._running = True
        original_import = __import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "uvicorn":
                raise ImportError("No module named 'uvicorn'", name="uvicorn")
            return original_import(name, globals, locals, fromlist, level)

        with patch("builtins.__import__", side_effect=fake_import):
            server._run_uvicorn()

        self.assertIn("uvicorn", server._startup_error)
        self.assertIn("pip install -r utils/requirements.txt", server._startup_error)
        self.assertFalse(server._running)

    def test_standalone_config_api_token_prefers_env_over_json(self):
        from xtquant_manager.standalone_config import load_standalone_config

        tmpdir = Path(tempfile.mkdtemp(prefix="xqm_config_token_"))
        old_cwd = os.getcwd()
        try:
            os.chdir(tmpdir)
            cfg_path = tmpdir / "xtquant_manager_config.json"
            cfg_path.write_text(json.dumps({"api_token": "json-token"}), encoding="utf-8")

            with patch.dict(os.environ, {"XQM_API_TOKEN": "", "QMT_API_TOKEN": ""}):
                cfg = load_standalone_config(str(cfg_path))
            self.assertEqual(cfg.api_token, "json-token")

            (tmpdir / ".env").write_text("QMT_API_TOKEN=qmt-token\n", encoding="utf-8")

            with patch.dict(os.environ, {"XQM_API_TOKEN": "", "QMT_API_TOKEN": ""}):
                cfg = load_standalone_config(str(cfg_path))
            self.assertEqual(cfg.api_token, "qmt-token")

            with patch.dict(os.environ, {"XQM_API_TOKEN": "xqm-token", "QMT_API_TOKEN": "qmt-token"}):
                cfg = load_standalone_config(str(cfg_path))
            self.assertEqual(cfg.api_token, "xqm-token")
        finally:
            os.chdir(old_cwd)
            shutil.rmtree(tmpdir, ignore_errors=True)

    @patch("xtquant_manager.standalone.XtQuantServer")
    @patch("xtquant_manager.standalone.XtQuantManager")
    @patch("xtquant_manager.standalone.ServerWatchdog")
    def test_standalone_http_failure_stops_before_watchdog_and_accounts(
        self, MockWatchdog, MockManager, MockServer
    ):
        from xtquant_manager.standalone_config import StandaloneConfig, AccountEntry
        from xtquant_manager.standalone import StandaloneApplication

        mock_server_instance = MagicMock()
        mock_server_instance.start.side_effect = RuntimeError("缺少 uvicorn")
        MockServer.return_value = mock_server_instance
        mock_manager_instance = MagicMock()
        MockManager.get_instance.return_value = mock_manager_instance

        cfg = StandaloneConfig(
            accounts=[AccountEntry(account_id="TEST_ACC_1", qmt_path="C:/mock/path")]
        )
        app = StandaloneApplication(cfg)

        with self.assertRaisesRegex(RuntimeError, "uvicorn"):
            app.run()

        mock_server_instance.stop.assert_called_once()
        MockWatchdog.return_value.start.assert_not_called()
        mock_manager_instance.register_account.assert_not_called()


class TestWeb2FlaskAutoStart(unittest.TestCase):
    """web2.0 模式下的 Flask 处置

    网关需要反向调用 Flask 才能读到 ENABLE_AUTO_OPERATION 等
    只存在于主进程内存、不持久化的开关。因此 web2.0 模式不再一律
    设 QMT_NO_FLASK=1，而是按端口占用情况决定：
      - 端口空闲 → 启动 Flask（不设该变量）
      - 端口已占 → 跳过（设 QMT_NO_FLASK=1），避免端口冲突
    """

    def setUp(self):
        self.accounts = [
            {"account_id": "ACC1", "qmt_path": "C:/qmt1"},
            {"account_id": "ACC2", "qmt_path": "C:/qmt2"},
        ]
        self.captured = []
        self.captured_cmds = []
        self.tmpdir = Path(tempfile.mkdtemp(prefix="launcher_web2_flask_"))
        self.env_path = self.tmpdir / ".env"
        self.env_path.write_text("", encoding="utf-8")
        self._orig_env_path = _launcher.ENV_PATH
        _launcher.ENV_PATH = self.env_path

        class _Proc:
            pid = 4242

        def _fake_popen(cmd, **kw):
            self.captured_cmds.append(cmd)
            self.captured.append(kw.get("env", {}))
            return _Proc()

        self.p_popen = patch.object(_launcher.subprocess, "Popen",
                                    side_effect=_fake_popen)
        self.p_accounts = patch.object(_launcher, "load_accounts",
                                       return_value=self.accounts)
        self.p_pid = patch.object(_launcher, "read_pid", return_value=None)
        self.p_sleep = patch.object(_launcher.time, "sleep")
        self.p_env = patch.dict(os.environ, {"WEB_SERVER_PORT": ""})
        for p in (self.p_popen, self.p_accounts, self.p_pid,
                  self.p_sleep, self.p_env):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: setattr(_launcher, "ENV_PATH", self._orig_env_path))
        self.addCleanup(lambda: shutil.rmtree(self.tmpdir, ignore_errors=True))

    def _args(self, web2, accounts=None):
        class _A:
            pass
        a = _A()
        a.accounts = accounts
        a.simulation = False
        a.web2 = web2
        return a

    def _run(self, web2, port_in_use):
        with patch.object(_launcher, "_is_port_in_use", return_value=port_in_use), \
             patch.object(_launcher.Path, "mkdir"), \
             patch.object(_launcher, "pid_file_for") as pf:
            pf.return_value = Path(tempfile.gettempdir()) / "dummy_pid.txt"
            with patch("sys.stdout", new=io.StringIO()):
                _launcher.cmd_start(self._args(web2))
        return self.captured

    def test_web2_starts_flask_when_port_free(self):
        envs = self._run(web2=True, port_in_use=False)
        self.assertTrue(envs)
        for env in envs:
            self.assertNotIn("QMT_NO_FLASK", env,
                             "端口空闲时应启动 Flask 供网关探测")

    def test_web2_skips_flask_when_port_occupied(self):
        envs = self._run(web2=True, port_in_use=True)
        self.assertTrue(envs)
        for env in envs:
            self.assertEqual(env.get("QMT_NO_FLASK"), "1",
                             "端口已占用时应跳过 Flask 避免冲突")

    def test_web1_never_sets_no_flask(self):
        envs = self._run(web2=False, port_in_use=False)
        self.assertTrue(envs)
        for env in envs:
            self.assertNotIn("QMT_NO_FLASK", env)

    def test_web1_ignores_port_state(self):
        """web1.0 模式不做端口检查，QMT_NO_FLASK 始终不设"""
        envs = self._run(web2=False, port_in_use=True)
        for env in envs:
            self.assertNotIn("QMT_NO_FLASK", env)

    def test_account_env_still_set(self):
        envs = self._run(web2=True, port_in_use=False)
        self.assertEqual(envs[0]["QMT_ACCOUNT_ID"], "ACC1")
        self.assertEqual(envs[1]["QMT_ACCOUNT_ID"], "ACC2")

    def test_start_passes_account_id_argument(self):
        self._run(web2=False, port_in_use=False)

        self.assertEqual(self.captured_cmds[0][-2:], ["--account-id", "ACC1"])
        self.assertEqual(self.captured_cmds[1][-2:], ["--account-id", "ACC2"])

    def test_configured_flask_base_port_is_used_and_passed_to_child(self):
        self.env_path.write_text("WEB_SERVER_PORT=5100\n", encoding="utf-8")

        with patch.object(_launcher, "_is_port_in_use", return_value=False) as port_check, \
             patch.object(_launcher.Path, "mkdir"), \
             patch.object(_launcher, "pid_file_for") as pf:
            pf.return_value = Path(tempfile.gettempdir()) / "dummy_pid.txt"
            with patch("sys.stdout", new=io.StringIO()):
                _launcher.cmd_start(self._args(web2=True))

        self.assertEqual([c.args[0] for c in port_check.call_args_list], [5100, 5101])
        self.assertEqual([env["WEB_SERVER_PORT"] for env in self.captured], ["5100", "5100"])


if __name__ == "__main__":
    unittest.main()
