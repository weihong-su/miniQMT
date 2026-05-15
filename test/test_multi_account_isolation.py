"""
多账号隔离测试 - 验证 QMT_ACCOUNT_ID 环境变量能让 config.py 的关键全局
变量按账号正确分离，且不破坏单账号（向后兼容）行为。

策略：每个测试用例启动一个独立的 python 子进程，传入不同的
QMT_ACCOUNT_ID 与临时 account_config.json，子进程 import config 后把
关键字段以 JSON 打印到 stdout，父进程解析并断言。

这样能避免 importlib.reload 污染主测试进程，也最接近真实启动场景。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from typing import Optional


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_config_in_subprocess(tmpdir: str, account_id: Optional[str],
                              extra_env: Optional[dict] = None) -> dict:
    """在临时工作目录中启动子进程，import config，打印关键全局变量。

    Args:
        tmpdir:        子进程的工作目录（也是 account_config.json 所在地）
        account_id:    子进程的 QMT_ACCOUNT_ID 环境变量；None 表示不设置
        extra_env:     追加的环境变量

    Returns:
        子进程 import config 后导出的字段字典
    """
    snippet = (
        "import json, config; "
        "print('<<JSON>>' + json.dumps({"
        "  'account_id': config.ACCOUNT_CONFIG.get('account_id', ''),"
        "  'qmt_path': config.QMT_PATH,"
        "  'data_dir': config.DATA_DIR,"
        "  'db_path': config.DB_PATH,"
        "  'stock_pool_file': config.STOCK_POOL_FILE,"
        "  'stock_pool': config.STOCK_POOL,"
        "  'log_file': config.LOG_FILE,"
        "  'web_port': config.WEB_SERVER_PORT,"
        "  'all_accounts': config.get_all_accounts_config(),"
        "}) + '<<END>>')"
    )

    env = os.environ.copy()
    env.pop("QMT_ACCOUNT_ID", None)
    env.pop("QMT_PATH", None)
    if account_id is not None:
        env["QMT_ACCOUNT_ID"] = account_id
    if extra_env:
        env.update(extra_env)
    # 子进程需要 import 项目根的 config.py
    env["PYTHONPATH"] = PROJECT_ROOT + os.pathsep + env.get("PYTHONPATH", "")

    result = subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=tmpdir, env=env, capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"子进程退出码 {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    stdout = result.stdout
    start = stdout.index("<<JSON>>") + len("<<JSON>>")
    end   = stdout.index("<<END>>")
    return json.loads(stdout[start:end])


class TestMultiAccountIsolation(unittest.TestCase):
    """验证多账号配置隔离的核心断言。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="miniqmt_isolate_")
        # 模拟两个真实存在的 QMT 路径（让 get_qmt_path 的 os.path.exists 检查通过）
        self.qmt_a = os.path.join(self.tmpdir, "qmt_A", "userdata_mini")
        self.qmt_b = os.path.join(self.tmpdir, "qmt_B", "userdata_mini")
        self.qmt_c = os.path.join(self.tmpdir, "qmt_C", "userdata_mini")
        for p in (self.qmt_a, self.qmt_b, self.qmt_c):
            os.makedirs(p, exist_ok=True)

        self.cfg = {
            "account_id": "AAA",
            "account_type": "STOCK",
            "qmt_path": self.qmt_a,
            "accounts": [
                {"account_id": "AAA", "account_type": "STOCK", "qmt_path": self.qmt_a},
                {"account_id": "BBB", "account_type": "STOCK", "qmt_path": self.qmt_b},
                {"account_id": "CCC", "account_type": "STOCK", "qmt_path": self.qmt_c},
            ],
        }
        with open(os.path.join(self.tmpdir, "account_config.json"), "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, ensure_ascii=False, indent=2)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -------- 1. 向后兼容：不设 QMT_ACCOUNT_ID 时行为不变 --------
    def test_backward_compat_no_env_returns_top_level(self):
        result = _run_config_in_subprocess(self.tmpdir, account_id=None)
        self.assertEqual(result["account_id"], "AAA")
        self.assertEqual(result["qmt_path"], self.qmt_a)
        # 单账号模式下 DATA_DIR/PORT 保持默认值
        self.assertEqual(result["data_dir"], "data")
        self.assertEqual(result["web_port"], 5000)
        self.assertEqual(result["log_file"], "qmt_trading.log")

    # -------- 2. 切换到不同账号时关键字段同步切换 --------
    def test_account_a_picks_top_settings(self):
        r = _run_config_in_subprocess(self.tmpdir, account_id="AAA")
        self.assertEqual(r["account_id"], "AAA")
        self.assertEqual(r["qmt_path"], self.qmt_a)
        self.assertEqual(r["data_dir"], "data_AAA")
        self.assertEqual(r["db_path"], os.path.join("data_AAA", "trading.db"))
        self.assertEqual(r["log_file"], "account_AAA.log")
        self.assertEqual(r["web_port"], 5000)  # 索引 0

    def test_account_b_isolated(self):
        r = _run_config_in_subprocess(self.tmpdir, account_id="BBB")
        self.assertEqual(r["account_id"], "BBB")
        self.assertEqual(r["qmt_path"], self.qmt_b)
        self.assertEqual(r["data_dir"], "data_BBB")
        self.assertEqual(r["db_path"], os.path.join("data_BBB", "trading.db"))
        self.assertEqual(r["log_file"], "account_BBB.log")
        self.assertEqual(r["web_port"], 5001)  # 索引 1

    def test_account_c_isolated(self):
        """第 3 个账号验证扩展性 —— 新增账号无需改代码"""
        r = _run_config_in_subprocess(self.tmpdir, account_id="CCC")
        self.assertEqual(r["account_id"], "CCC")
        self.assertEqual(r["qmt_path"], self.qmt_c)
        self.assertEqual(r["data_dir"], "data_CCC")
        self.assertEqual(r["web_port"], 5002)  # 索引 2

    # -------- 3. 未知账号 ID 回落到顶层 --------
    def test_unknown_account_falls_back(self):
        r = _run_config_in_subprocess(self.tmpdir, account_id="ZZZ_NOT_EXIST")
        # 警告打印但仍能启动 —— 用顶层值
        self.assertEqual(r["account_id"], "AAA")
        self.assertEqual(r["qmt_path"], self.qmt_a)
        self.assertEqual(r["data_dir"], "data")  # 未触发覆写
        self.assertEqual(r["web_port"], 5000)

    # -------- 4. 股票池：默认回落到根目录 stock_pool.json --------
    def test_stock_pool_falls_back_when_no_per_account_file(self):
        # 根目录写一个全局股票池
        with open(os.path.join(self.tmpdir, "stock_pool.json"), "w", encoding="utf-8") as f:
            json.dump(["111111.SZ", "222222.SH"], f)

        r = _run_config_in_subprocess(self.tmpdir, account_id="BBB")
        self.assertEqual(r["stock_pool_file"], "stock_pool.json")
        self.assertEqual(r["stock_pool"], ["111111.SZ", "222222.SH"])

    # -------- 5. 股票池：账号目录有 stock_pool.json 时优先使用 --------
    def test_stock_pool_per_account_overrides_global(self):
        with open(os.path.join(self.tmpdir, "stock_pool.json"), "w", encoding="utf-8") as f:
            json.dump(["GLOBAL.SH"], f)
        bbb_dir = os.path.join(self.tmpdir, "data_BBB")
        os.makedirs(bbb_dir, exist_ok=True)
        with open(os.path.join(bbb_dir, "stock_pool.json"), "w", encoding="utf-8") as f:
            json.dump(["BBB_ONLY.SZ"], f)

        r_b = _run_config_in_subprocess(self.tmpdir, account_id="BBB")
        self.assertEqual(r_b["stock_pool_file"], os.path.join("data_BBB", "stock_pool.json"))
        self.assertEqual(r_b["stock_pool"], ["BBB_ONLY.SZ"])

        # A 没自己的池，仍然用全局
        r_a = _run_config_in_subprocess(self.tmpdir, account_id="AAA")
        self.assertEqual(r_a["stock_pool_file"], "stock_pool.json")
        self.assertEqual(r_a["stock_pool"], ["GLOBAL.SH"])

    # -------- 6. get_all_accounts_config 始终返回全列表，不受 QMT_ACCOUNT_ID 影响 --------
    def test_all_accounts_list_invariant(self):
        r_none = _run_config_in_subprocess(self.tmpdir, account_id=None)
        r_a    = _run_config_in_subprocess(self.tmpdir, account_id="AAA")
        r_b    = _run_config_in_subprocess(self.tmpdir, account_id="BBB")
        ids_none = [a["account_id"] for a in r_none["all_accounts"]]
        ids_a    = [a["account_id"] for a in r_a["all_accounts"]]
        ids_b    = [a["account_id"] for a in r_b["all_accounts"]]
        self.assertEqual(ids_none, ["AAA", "BBB", "CCC"])
        self.assertEqual(ids_a,    ["AAA", "BBB", "CCC"])
        self.assertEqual(ids_b,    ["AAA", "BBB", "CCC"])

    # -------- 7. 单账号配置格式（无 accounts 数组）的向后兼容 --------
    def test_single_account_format_compat(self):
        # 覆写为单账号格式
        with open(os.path.join(self.tmpdir, "account_config.json"), "w", encoding="utf-8") as f:
            json.dump({
                "account_id": "SOLO",
                "account_type": "STOCK",
                "qmt_path": self.qmt_a,
            }, f)

        # 不设环境变量 → 顶层
        r1 = _run_config_in_subprocess(self.tmpdir, account_id=None)
        self.assertEqual(r1["account_id"], "SOLO")
        self.assertEqual(r1["data_dir"], "data")
        self.assertEqual(len(r1["all_accounts"]), 1)
        self.assertEqual(r1["all_accounts"][0]["account_id"], "SOLO")

        # 用户主动设 QMT_ACCOUNT_ID=SOLO：即便是单账号格式，
        # get_all_accounts_config 也会以单账号兜底成 [SOLO]，命中 idx=0，
        # 触发目录覆写。这是合理特性 —— 让单账号用户也能用一键脚本得到 data_SOLO/。
        r2 = _run_config_in_subprocess(self.tmpdir, account_id="SOLO")
        self.assertEqual(r2["account_id"], "SOLO")
        self.assertEqual(r2["data_dir"], "data_SOLO")
        self.assertEqual(r2["log_file"], "account_SOLO.log")
        self.assertEqual(r2["web_port"], 5000)

        # 但若设的 ID 与单账号顶层不匹配，则回落到顶层默认
        r3 = _run_config_in_subprocess(self.tmpdir, account_id="OTHER")
        self.assertEqual(r3["account_id"], "SOLO")
        self.assertEqual(r3["data_dir"], "data")


if __name__ == "__main__":
    unittest.main()
