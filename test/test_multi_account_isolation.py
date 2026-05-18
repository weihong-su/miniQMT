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

    # -------- 4. 股票池：多账号模式无条件走账号目录,不再回落根目录 --------
    # 历史上是"账号目录文件存在才走账号目录,否则回落根目录"。但 position_manager
    # 每轮持仓监控会把当前持仓覆盖写回该文件,两个账号写同一份会互相覆盖。
    # 现行设计:多账号模式下无条件用 data_<id>/stock_pool.json;文件不存在则由
    # load_stock_pool 兜底为 DEFAULT_STOCK_POOL。position_manager 启动后会很快
    # 把当前持仓写入,从而完成隔离。
    def test_stock_pool_uses_account_dir_even_when_file_missing(self):
        # 根目录写一个全局股票池(应被忽略)
        with open(os.path.join(self.tmpdir, "stock_pool.json"), "w", encoding="utf-8") as f:
            json.dump(["111111.SZ", "222222.SH"], f)

        # BBB 账号目录里没有 stock_pool.json
        r = _run_config_in_subprocess(self.tmpdir, account_id="BBB")
        self.assertEqual(r["stock_pool_file"], os.path.join("data_BBB", "stock_pool.json"))
        # 文件不存在,load_stock_pool 兜底 DEFAULT_STOCK_POOL,不是根目录内容
        self.assertNotEqual(r["stock_pool"], ["111111.SZ", "222222.SH"])
        # 应是 config.DEFAULT_STOCK_POOL 的内容(平安/招行/美的/茅台/五粮液)
        self.assertIn("000001.SZ", r["stock_pool"])

    # -------- 5. 股票池：账号目录有 stock_pool.json 时按账号读取 --------
    def test_stock_pool_per_account_isolation(self):
        # 根目录写一个全局股票池(应被忽略 — 多账号下不应再用)
        with open(os.path.join(self.tmpdir, "stock_pool.json"), "w", encoding="utf-8") as f:
            json.dump(["GLOBAL.SH"], f)

        # BBB 有自己的池子
        bbb_dir = os.path.join(self.tmpdir, "data_BBB")
        os.makedirs(bbb_dir, exist_ok=True)
        with open(os.path.join(bbb_dir, "stock_pool.json"), "w", encoding="utf-8") as f:
            json.dump(["BBB_ONLY.SZ"], f)

        # AAA 也有自己的池子(独立内容)
        aaa_dir = os.path.join(self.tmpdir, "data_AAA")
        os.makedirs(aaa_dir, exist_ok=True)
        with open(os.path.join(aaa_dir, "stock_pool.json"), "w", encoding="utf-8") as f:
            json.dump(["AAA_ONLY.SZ"], f)

        r_b = _run_config_in_subprocess(self.tmpdir, account_id="BBB")
        self.assertEqual(r_b["stock_pool_file"], os.path.join("data_BBB", "stock_pool.json"))
        self.assertEqual(r_b["stock_pool"], ["BBB_ONLY.SZ"])

        r_a = _run_config_in_subprocess(self.tmpdir, account_id="AAA")
        self.assertEqual(r_a["stock_pool_file"], os.path.join("data_AAA", "stock_pool.json"))
        self.assertEqual(r_a["stock_pool"], ["AAA_ONLY.SZ"])
        # 关键:AAA 没读到根目录的 GLOBAL.SH
        self.assertNotIn("GLOBAL.SH", r_a["stock_pool"])

    # -------- 5b. 单账号模式(不设 QMT_ACCOUNT_ID)保持读根目录 — 向后兼容 --------
    def test_stock_pool_single_account_mode_keeps_root(self):
        with open(os.path.join(self.tmpdir, "stock_pool.json"), "w", encoding="utf-8") as f:
            json.dump(["SOLO_ROOT.SZ"], f)

        r = _run_config_in_subprocess(self.tmpdir, account_id=None)
        self.assertEqual(r["stock_pool_file"], "stock_pool.json")
        self.assertEqual(r["stock_pool"], ["SOLO_ROOT.SZ"])

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


class TestTradingExecutorSimulationPositions(unittest.TestCase):
    """回归测试: 模拟模式下 trading_executor.get_stock_positions()
    必须从 PositionManager 内存 DB 取数据，而非走 QMT API 返回空。

    Bug 场景: 否则 /api/positions 的主 positions 字段在模拟模式下
    永远是空，多账号 web UI 会显示"两个端口持仓一模一样地为空"。
    """

    def test_simulation_returns_positions_from_position_manager(self):
        import sys, importlib
        from unittest.mock import patch, MagicMock
        import pandas as pd

        sys.path.insert(0, str(PROJECT_ROOT))
        config = importlib.import_module("config")
        trading_executor_mod = importlib.import_module("trading_executor")

        # 构造 PositionManager 桩，返回 2 条模拟持仓
        fake_df = pd.DataFrame([
            {"stock_code": "000001.SZ", "stock_name": "平安银行",
             "volume": 1000, "available": 1000, "cost_price": 10.0,
             "current_price": 10.5, "market_value": 10500, "profit_ratio": 0.05},
            {"stock_code": "600519.SH", "stock_name": "贵州茅台",
             "volume": 100, "available": 100, "cost_price": 1300.0,
             "current_price": 1350.0, "market_value": 135000, "profit_ratio": 0.038},
        ])
        fake_pm = MagicMock()
        fake_pm.get_all_positions_with_all_fields = MagicMock(return_value=fake_df)

        with patch.object(config, "ENABLE_SIMULATION_MODE", True):
            te = trading_executor_mod.TradingExecutor.__new__(
                trading_executor_mod.TradingExecutor
            )
            te.position_manager = fake_pm
            te.trader = None  # 模拟模式下 QMT 没连接

            positions = te.get_stock_positions()

        self.assertEqual(len(positions), 2,
            "模拟模式下 get_stock_positions() 必须返回 PositionManager 内存 DB 的持仓")
        codes = {p["stock_code"] for p in positions}
        self.assertEqual(codes, {"000001.SZ", "600519.SH"})
        # 字段完整性
        p0 = positions[0]
        for key in ("stock_code", "stock_name", "volume", "available",
                    "cost_price", "current_price", "market_value", "profit_ratio"):
            self.assertIn(key, p0, f"持仓字典缺少字段 {key}")
        self.assertIsInstance(p0["volume"], int)
        self.assertIsInstance(p0["cost_price"], float)

    def test_simulation_returns_empty_when_no_positions(self):
        import sys, importlib
        from unittest.mock import patch, MagicMock
        import pandas as pd

        sys.path.insert(0, str(PROJECT_ROOT))
        config = importlib.import_module("config")
        trading_executor_mod = importlib.import_module("trading_executor")

        fake_pm = MagicMock()
        fake_pm.get_all_positions_with_all_fields = MagicMock(return_value=pd.DataFrame())

        with patch.object(config, "ENABLE_SIMULATION_MODE", True):
            te = trading_executor_mod.TradingExecutor.__new__(
                trading_executor_mod.TradingExecutor
            )
            te.position_manager = fake_pm
            te.trader = None

            positions = te.get_stock_positions()

        self.assertEqual(positions, [])

    def test_live_mode_uses_position_manager_not_xtt_module(self):
        """实盘模式下也必须从 PositionManager 取，而非走 xtt.query_position。

        Bug 场景: xtt.create_trader() 不接受 path，多账号下两个进程都
        通过 xtt.query_position(account_id, ...) 可能拿到错账号持仓（曾观测
        :5001 实盘显示 :5000 持仓）。此测试用一个会爆 RuntimeError 的桩
        监控 xtt.query_position 是否被调用，若被调用即视为回归。
        """
        import sys, importlib
        from unittest.mock import patch, MagicMock
        import pandas as pd

        sys.path.insert(0, str(PROJECT_ROOT))
        config = importlib.import_module("config")
        trading_executor_mod = importlib.import_module("trading_executor")

        fake_df = pd.DataFrame([
            {"stock_code": "600519.SH", "stock_name": "贵州茅台",
             "volume": 100, "available": 100, "cost_price": 1300.0,
             "current_price": 1350.0, "market_value": 135000, "profit_ratio": 0.038},
        ])
        fake_pm = MagicMock()
        fake_pm.get_all_positions_with_all_fields = MagicMock(return_value=fake_df)

        # 任何对 xtt.query_position 或 trader.query_position 的调用都让测试失败
        xtt_module = trading_executor_mod.xtt
        guard = MagicMock(side_effect=AssertionError(
            "trading_executor 不应再调用 xtt.query_position；"
            "多账号下该路径会取到错账号持仓"
        ))

        with patch.object(config, "ENABLE_SIMULATION_MODE", False), \
             patch.object(xtt_module, "query_position", guard, create=True):
            te = trading_executor_mod.TradingExecutor.__new__(
                trading_executor_mod.TradingExecutor
            )
            te.position_manager = fake_pm
            # self.trader 设成会爆错的 mock：哪怕走到 trader 分支也会暴露
            te.trader = MagicMock()
            te.trader.query_position = guard

            positions = te.get_stock_positions()

        guard.assert_not_called()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["stock_code"], "600519.SH")


class TestSimulationModeViaEnv(unittest.TestCase):
    """回归测试: ENABLE_SIMULATION_MODE 必须能被环境变量覆写。

    Bug 场景: 之前 _launcher.py 菜单 [7] 启动"实盘"时不设环境变量，
    config.py 默认 ENABLE_SIMULATION_MODE=True，结果两个进程都是模拟，
    需用户在每个 web UI 单独切换。一旦只切了 5000 没切 5001，就出现
    "5000 实盘 / 5001 模拟混搭"的状态混乱。
    """

    def _run_in_subprocess(self, env_value):
        """子进程里 import config，回报 ENABLE_SIMULATION_MODE 实际值。"""
        env = os.environ.copy()
        env.pop("ENABLE_SIMULATION_MODE", None)
        if env_value is not None:
            env["ENABLE_SIMULATION_MODE"] = env_value
        env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

        # 切到一个临时空目录避免污染当前 cwd
        with tempfile.TemporaryDirectory() as tmp:
            # 临时账户配置（任意有效内容即可）
            with open(os.path.join(tmp, "account_config.json"), "w", encoding="utf-8") as f:
                json.dump({"account_id": "TEST", "qmt_path": tmp}, f)
            result = subprocess.run(
                [sys.executable, "-c",
                 "import config; print('VAL=' + str(config.ENABLE_SIMULATION_MODE))"],
                cwd=tmp, env=env, capture_output=True, text=True, timeout=20,
            )
        for line in result.stdout.splitlines():
            if line.startswith("VAL="):
                return line[4:].strip()
        raise RuntimeError(f"无法解析 ENABLE_SIMULATION_MODE: {result.stdout!r}\n{result.stderr}")

    def test_env_false_disables_simulation(self):
        """env=false → 实盘"""
        self.assertEqual(self._run_in_subprocess("false"), "False")
        self.assertEqual(self._run_in_subprocess("FALSE"), "False")
        self.assertEqual(self._run_in_subprocess("0"),     "False")
        self.assertEqual(self._run_in_subprocess(""),      "True")  # 空字符串视为未设置 → 默认 True

    def test_env_true_enables_simulation(self):
        """env=true → 模拟"""
        self.assertEqual(self._run_in_subprocess("true"), "True")
        self.assertEqual(self._run_in_subprocess("1"),    "True")
        self.assertEqual(self._run_in_subprocess("Yes"),  "True")

    def test_no_env_keeps_default(self):
        """未设置环境变量 → 保持 config.py 源码默认值 True（安全）"""
        self.assertEqual(self._run_in_subprocess(None), "True")


class TestEasyQmtTraderAccountGuard(unittest.TestCase):
    """回归测试: easy_qmt_trader.position() / balance() 必须按 self.account
    过滤返回的条目。多账号下 xtquant 共享/串账号时，这是兜底防线。
    """

    def _make_trader_stub(self, account):
        """构造一个不走真实 xtquant 的 easy_qmt_trader 实例。"""
        import sys, importlib
        from unittest.mock import MagicMock
        sys.path.insert(0, str(PROJECT_ROOT))
        eqt = importlib.import_module("easy_qmt_trader")

        t = eqt.easy_qmt_trader.__new__(eqt.easy_qmt_trader)
        t.account     = account
        t.account_type = "STOCK"
        t.path        = "C:/QMT/userdata_mini"
        t._connecting = False
        t.xt_trader   = MagicMock()
        t.acc         = MagicMock()
        return t, eqt

    def _make_pos(self, account_id, stock_code, volume=100):
        from unittest.mock import MagicMock
        m = MagicMock()
        m.account_id     = account_id
        m.account_type   = "STOCK"
        m.stock_code     = stock_code
        m.volume         = volume
        m.can_use_volume = volume
        m.open_price     = 10.0
        m.market_value   = volume * 10
        return m

    def test_position_filters_wrong_account(self):
        """query_stock_positions 返回了 A、B 两个账号的持仓时，position()
        只能返回 self.account 对应的那一个账号的条目。"""
        t, _ = self._make_trader_stub("25106531")
        t.xt_trader.query_stock_positions.return_value = [
            self._make_pos("25105132", "000001.SZ"),  # 错账号 - 应被丢弃
            self._make_pos("25106531", "600519.SH"),  # 本账号 - 保留
            self._make_pos("25105132", "600036.SH"),  # 错账号 - 应被丢弃
        ]
        df = t.position()
        self.assertEqual(len(df), 1, f"错账号持仓未被过滤: {df}")
        self.assertEqual(df.iloc[0]['资金账号'], "25106531")
        self.assertEqual(df.iloc[0]['证券代码'], "600519")

    def test_position_keeps_all_when_account_id_missing(self):
        """若 pos.account_id 为空（早期 QMT/兼容），不应误杀，全部保留。"""
        t, _ = self._make_trader_stub("25106531")
        t.xt_trader.query_stock_positions.return_value = [
            self._make_pos("", "000001.SZ"),
            self._make_pos("25106531", "600519.SH"),
        ]
        df = t.position()
        self.assertEqual(len(df), 2)

    def test_balance_rejects_wrong_account(self):
        """query_stock_asset 返回了错账号资产时，balance() 必须返回空 DataFrame。"""
        from unittest.mock import MagicMock
        t, _ = self._make_trader_stub("25106531")
        asset = MagicMock()
        asset.account_id   = "25105132"     # 错账号
        asset.account_type = "STOCK"
        asset.cash         = 1000000
        asset.frozen_cash  = 0
        asset.market_value = 200000
        asset.total_asset  = 1200000
        t.xt_trader.query_stock_asset.return_value = asset

        df = t.balance()
        self.assertTrue(df.empty, "错账号资产应被丢弃，返回空 DataFrame")

    def test_balance_accepts_matching_account(self):
        from unittest.mock import MagicMock
        t, _ = self._make_trader_stub("25106531")
        asset = MagicMock()
        asset.account_id   = "25106531"
        asset.account_type = "STOCK"
        asset.cash         = 1000000
        asset.frozen_cash  = 0
        asset.market_value = 200000
        asset.total_asset  = 1200000
        t.xt_trader.query_stock_asset.return_value = asset

        df = t.balance()
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]['资金账户'], "25106531")


class TestSimulationAccountIdInWebStatus(unittest.TestCase):
    """回归测试: 模拟模式下 /api/status 必须返回真实的 account_id，
    而非硬编码 'SIMULATION'，否则多账号 Web UI 无法区分两个窗口。
    """

    def test_simulation_returns_real_account_id_from_config(self):
        # 用 mock 直接调用 PositionManager.get_account_info()
        # 不走完整初始化，避免触发 QMT/数据库依赖
        import sys
        sys.path.insert(0, str(PROJECT_ROOT))
        # 重新导入 config 模块（保证后续 monkeypatch 不污染）
        from unittest.mock import patch, MagicMock
        import importlib
        config = importlib.import_module("config")
        position_manager_mod = importlib.import_module("position_manager")

        # 临时启用模拟模式 + 设置 ACCOUNT_CONFIG 真实账号
        with patch.object(config, "ENABLE_SIMULATION_MODE", True), \
             patch.object(config, "ACCOUNT_CONFIG",
                          {"account_id": "12345678", "account_type": "STOCK"}), \
             patch.object(config, "SIMULATION_BALANCE", 500000):

            # 构造一个最小 mock 实例，跳过 __init__
            pm = position_manager_mod.PositionManager.__new__(
                position_manager_mod.PositionManager
            )
            # get_all_positions 必须返回空 DataFrame
            import pandas as pd
            pm.get_all_positions = MagicMock(return_value=pd.DataFrame())

            info = pm.get_account_info()

        self.assertEqual(info["account_id"], "12345678",
                         "模拟模式下应返回真实 account_id，不能硬编码 'SIMULATION'")
        self.assertEqual(info["account_type"], "STOCK")
        self.assertEqual(info["available"], 500000.0)

    def test_simulation_falls_back_when_account_id_missing(self):
        """ACCOUNT_CONFIG 没有 account_id 时仍能正常工作（兜底为 SIMULATION）"""
        import sys
        sys.path.insert(0, str(PROJECT_ROOT))
        from unittest.mock import patch, MagicMock
        import importlib
        config = importlib.import_module("config")
        position_manager_mod = importlib.import_module("position_manager")

        with patch.object(config, "ENABLE_SIMULATION_MODE", True), \
             patch.object(config, "ACCOUNT_CONFIG", {}), \
             patch.object(config, "SIMULATION_BALANCE", 100000):

            pm = position_manager_mod.PositionManager.__new__(
                position_manager_mod.PositionManager
            )
            import pandas as pd
            pm.get_all_positions = MagicMock(return_value=pd.DataFrame())

            info = pm.get_account_info()

        self.assertEqual(info["account_id"], "SIMULATION")


if __name__ == "__main__":
    unittest.main()
