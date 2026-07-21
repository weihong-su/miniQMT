"""
miniQMT 总控制台后端（被 miniqmt.bat 调用）。

子命令:
  menu                       交互式菜单（默认入口）
  list                       列出 account_config.json 中所有账号
  status                     查看每个账号的进程运行状态
  start [--accounts a,b] [--simulation]   启动账号（默认全部）
  stop  [--accounts a,b] [--force] [--timeout N]   停止账号
  setup-wizard              首次部署向导：检查环境并生成本机配置骨架
  check-env                  检查 Python 版本与核心依赖
  install-deps               运行 pip install -r utils/requirements.txt
  check-config               校验 account_config.json 的合法性与 qmt_path 存在性
  git-pull                   拉取最新代码
  xqm-start                  启动 xtquant_manager 网关
  xqm-stop                   停止 xtquant_manager 网关
  xqm-status                 查看 xtquant_manager 运行状态
  xqm-ui                     在浏览器打开 web2.0/1.0 界面
  autobuy-start              启动自动买入服务
  autobuy-stop               停止自动买入服务
  autobuy-status             查看自动买入服务状态
  autobuy-log                查看自动买入服务日志

进程跟踪:
  main.py 启动时自己把 PID 写到 data_<account_id>/pid.txt；
  本脚本通过该文件定位进程，可发 Ctrl+C 走优雅关闭，超时则 taskkill。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# 强制使用 UTF-8 输出，避免 chcp 65001 下中文乱码
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH  = PROJECT_ROOT / "account_config.json"
ENV_PATH     = PROJECT_ROOT / ".env"
STOCK_POOL_PATH = PROJECT_ROOT / "stock_pool.json"
XQM_CONFIG_PATH = PROJECT_ROOT / "xtquant_manager_config.json"
MAIN_PY      = PROJECT_ROOT / "main.py"
WEB_MODE_PREF = PROJECT_ROOT / "data" / ".web_mode"

SUPPORTED_PYTHON_MIN = (3, 8)
SUPPORTED_PYTHON_MAX = (3, 11)  # 仓库内 xtquant 二进制当前覆盖到 cp311


# ---------------------------------------------------------------------------
# Web 模式偏好（记忆用户最后选择的 web1.0/2.0）
# ---------------------------------------------------------------------------
def _load_web_mode_pref() -> str:
    """读取用户上次选择的 Web 模式，返回 "1" 或 "2"，默认 "1"。"""
    try:
        if WEB_MODE_PREF.exists():
            val = WEB_MODE_PREF.read_text(encoding="ascii").strip()
            if val in ("1", "2"):
                return val
    except Exception:
        pass
    return "1"


def _save_web_mode_pref(web2: bool) -> None:
    """保存用户 Web 模式选择。"""
    data_dir = WEB_MODE_PREF.parent
    if not data_dir.exists():
        data_dir.mkdir(parents=True, exist_ok=True)
    WEB_MODE_PREF.write_text("2" if web2 else "1", encoding="ascii")


# ---------------------------------------------------------------------------
# Account / process helpers
# ---------------------------------------------------------------------------
def load_accounts() -> list[dict]:
    if not CONFIG_PATH.exists():
        print(f"[错误] 找不到配置文件: {CONFIG_PATH}")
        sys.exit(2)
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    accounts = cfg.get("accounts") or []
    if not accounts:
        accounts = [{
            "account_id":   cfg.get("account_id", ""),
            "account_type": cfg.get("account_type", "STOCK"),
            "qmt_path":     cfg.get("qmt_path", ""),
        }]
    return [a for a in accounts if a.get("account_id")]


def pid_file_for(account_id: str) -> Path:
    return PROJECT_ROOT / f"data_{account_id}" / "pid.txt"


def read_pid(account_id: str) -> int | None:
    p = pid_file_for(account_id)
    if not p.exists():
        return None
    try:
        return int(p.read_text(encoding="ascii").strip())
    except (ValueError, OSError):
        return None


def pid_alive(pid: int) -> bool:
    """Windows 上用 tasklist 判断 PID 是否存活。"""
    if not pid:
        return False
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
        return str(pid) in r.stdout
    except Exception:
        return False


def filter_accounts(accounts: list[dict], selection: str | None) -> list[dict]:
    if not selection:
        return accounts
    wanted = {x.strip() for x in selection.split(",") if x.strip()}
    picked = [a for a in accounts if a["account_id"] in wanted]
    missing = wanted - {a["account_id"] for a in picked}
    if missing:
        print(f"[警告] 未找到账号: {', '.join(missing)}")
    return picked


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_list(_args) -> int:
    accounts = load_accounts()
    if not accounts:
        print("(account_config.json 中没有有效账号)")
        return 0
    print(f"{'#':<3} {'账号 ID':<14} {'Web 端口':<8} {'QMT 路径'}")
    print("-" * 70)
    for i, a in enumerate(accounts):
        print(f"{i+1:<3} {a['account_id']:<14} :{5000 + i:<7} {a.get('qmt_path','')}")
    return 0


def cmd_start(args, web2: bool = False) -> int:
    accounts = filter_accounts(load_accounts(), args.accounts)
    if not accounts:
        print("[错误] 没有可启动的账号")
        return 1

    python_exe = sys.executable
    if not MAIN_PY.exists():
        print(f"[错误] 找不到 main.py: {MAIN_PY}")
        return 1

    started = 0
    for acc in accounts:
        acc_id   = acc["account_id"]
        qmt_path = acc.get("qmt_path", "")

        # 已运行检查
        existing = read_pid(acc_id)
        if existing and pid_alive(existing):
            print(f"  ⊘ 账号 {acc_id} 已在运行 (PID={existing})，跳过")
            continue

        # 准备 data 目录、删旧 pid 文件
        data_dir = PROJECT_ROOT / f"data_{acc_id}"
        data_dir.mkdir(exist_ok=True)
        pf = pid_file_for(acc_id)
        if pf.exists():
            try:
                pf.unlink()
            except OSError:
                pass

        # 用 CREATE_NEW_CONSOLE 在新窗口启动 python.exe；
        # Popen.pid 就是 python 真实 PID（main.py 启动后会用同样的 PID 改写 pid.txt）
        env = os.environ.copy()
        env["QMT_ACCOUNT_ID"] = acc_id
        env["QMT_PATH"]       = qmt_path
        # 显式锁定模式：菜单 [7] 启动 = 实盘 (false)，菜单 [8] 启动 = 模拟 (true)。
        # 避免 config.py 默认值在两个进程"不一致"——以前曾出现 5000 实盘 / 5001
        # 模拟混搭的局面（用户在 5000 web UI 切换了实盘，5001 没切）。
        env["ENABLE_SIMULATION_MODE"] = "true" if args.simulation else "false"
        if getattr(args, "web2", False):
            env["QMT_NO_FLASK"] = "1"

        creationflags = 0x00000010  # CREATE_NEW_CONSOLE
        try:
            proc = subprocess.Popen(
                [python_exe, str(MAIN_PY)],
                cwd=str(PROJECT_ROOT),
                env=env,
                creationflags=creationflags,
                close_fds=True,
            )
        except OSError as e:
            print(f"  ✗ 启动账号 {acc_id} 失败: {e}")
            continue

        # 兜底写入 PID（main.py 启动后会自己再写一次，确保即使早期崩溃也能跟踪）
        try:
            pf.write_text(str(proc.pid), encoding="ascii")
        except OSError:
            pass

        mode = "模拟" if args.simulation else "实盘"
        print(f"  ✓ 已启动 {acc_id}  PID={proc.pid}  模式={mode}  QMT={qmt_path}")
        started += 1
        time.sleep(0.8)  # 错开启动，避免 QMT 客户端初始化竞争

    print(f"\n共启动 {started}/{len(accounts)} 个账号。Web 端口从 5000 开始按账号顺序分配（绑定 127.0.0.1，仅本机访问）。")
    return 0


def _request_graceful_stop(acc_id: str, pid: int) -> bool:
    """请求账号进程优雅退出。写停止信号文件 + 尝试 Ctrl+C。

    Windows 下 CREATE_NEW_CONSOLE 创建的进程有独立控制台，
    GenerateConsoleCtrlEvent 无法可靠送达。因此主路径改为：
    1) 写 data_<id>/stop_signal 文件 → main.py 主循环 1 秒内检测到并退出
    2) 额外尝试 Ctrl+C 作为补充（对非 CREATE_NEW_CONSOLE 场景仍有效）

    Returns:
        True 表示至少写入了信号文件（可靠路径）
    """
    # 主路径：写信号文件
    signal_file = PROJECT_ROOT / f"data_{acc_id}" / "stop_signal"
    try:
        signal_file.write_text(str(pid), encoding="ascii")
    except OSError:
        return False

    # 补充路径：尝试 Ctrl+C（对同控制台进程有效）
    if sys.platform == "win32":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.FreeConsole()
            if k.AttachConsole(pid):
                k.SetConsoleCtrlHandler(None, True)
                k.GenerateConsoleCtrlEvent(0, 0)
                k.FreeConsole()
                k.SetConsoleCtrlHandler(None, False)
        except OSError:
            pass

    return True


def cmd_stop(args) -> int:
    accounts = filter_accounts(load_accounts(), args.accounts)

    # ── 阶段 1：收集存活进程 ──
    targets: list[tuple[str, int]] = []       # (account_id, pid)
    for acc in accounts:
        acc_id = acc["account_id"]
        pid = read_pid(acc_id)
        if pid is None:
            print(f"[{acc_id}] 无 pid.txt，跳过（可能未启动）")
            continue
        if not pid_alive(pid):
            print(f"[{acc_id}] PID={pid} 已不存在，清理 pid.txt")
            pid_file_for(acc_id).unlink(missing_ok=True)
            continue
        targets.append((acc_id, pid))

    if not targets:
        print("没有需要停止的账号进程。")
        return 0

    # ── 阶段 2：向所有进程发 Ctrl+C（批量，不等待） ──
    # 必须先全部发完再等待退出。原因是：
    #   1) 顺序阻塞版"发 Ctrl+C → 等 30s → 发下一个"会让后面的账号
    #      在前一个退出前完全没被通知，用户看到"只停了一个"。
    #   2) Windows 的 GenerateConsoleCtrlEvent 发给整个控制台进程组；
    #      CREATE_NEW_CONSOLE 创建的每个进程有独立控制台，必须逐个
    #      AttachConsole → 发送。先发的那个退出后，FreeConsole +
    #      恢复 handler 的时序可能影响后续 AttachConsole 成功率。
    #   3) 批量发送更高效：所有进程同时开始优雅关闭。
    ctrl_c_sent: list[tuple[str, int]] = []   # 成功发送 Ctrl+C 的
    for acc_id, pid in targets:
        print(f"[{acc_id}] 准备停止 PID={pid}")
        if not args.force:
            if _request_graceful_stop(acc_id, pid):
                print(f"  ✓ 已发送停止信号")
                ctrl_c_sent.append((acc_id, pid))
            else:
                print(f"  ⚠ 停止信号发送失败，稍后强制结束")
        else:
            print(f"  跳过 Ctrl+C（--force 模式）")

    # ── 阶段 3：等待所有 Ctrl+C 进程退出 ──
    deadline = time.time() + args.timeout
    exited: set[int] = set()
    while time.time() < deadline and len(exited) < len(ctrl_c_sent):
        for acc_id, pid in ctrl_c_sent:
            if pid in exited:
                continue
            if not pid_alive(pid):
                exited.add(pid)
                print(f"  ✓ [{acc_id}] PID={pid} 已退出")
        if len(exited) < len(ctrl_c_sent):
            time.sleep(1)

    # ── 阶段 4：对未退出的进程强制结束 ──
    stopped = 0
    for acc_id, pid in targets:
        if pid_alive(pid):
            print(f"  → [{acc_id}] PID={pid} 强制结束（taskkill /T /F）")
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True)
            time.sleep(0.3)
        pid_file_for(acc_id).unlink(missing_ok=True)
        stopped += 1

    print(f"\n共停止 {stopped} 个账号进程。")
    return 0


# ---------------------------------------------------------------------------
# Deployment / environment helpers
# ---------------------------------------------------------------------------
REQUIREMENTS_FILE = PROJECT_ROOT / "utils" / "requirements.txt"

# 核心 PyPI 依赖：pip 能装的（xtquant 是 QMT 客户端附带，单独标注）
CORE_DEPS = [
    "pandas", "numpy", "flask", "flask_cors", "mootdx",
    "marshmallow", "requests", "colorama",
]
XQM_DEPS = [
    "fastapi", "uvicorn", "pydantic",
]
RPC_DEPS = ["redis"]
# 需要特殊处理的依赖：通过 QMT 客户端自带的 SDK 安装
SPECIAL_DEPS = ["xtquant"]

QMT_PATH_CANDIDATES = [
    r"C:/QMT/userdata_mini",
    r"C:/QMT1/userdata_mini",
    r"C:/QMT2/userdata_mini",
    r"C:/光大证券金阳光QMT实盘/userdata_mini",
    r"D:/QMT/userdata_mini",
    r"D:/QMT1/userdata_mini",
    r"D:/QMT2/userdata_mini",
    r"D:/光大证券金阳光QMT实盘/userdata_mini",
]

DEFAULT_ENV_TEXT = """# miniQMT 本机环境配置
# 说明：空值表示未启用或不校验；不要把真实 token 提交到仓库。
QMT_API_TOKEN=
TUSHARE_TOKEN=
ENABLE_TUSHARE_DATA_SOURCE=false
ENABLE_QMT_IPC_FALLBACK=false
ENABLE_QMT_RPC_FALLBACK=false
QMT_RPC_TRANSPORT=redis
QMT_RPC_REDIS_HOST=127.0.0.1
QMT_RPC_REDIS_PORT=6379
QMT_RPC_REDIS_DB=5
QMT_RPC_REDIS_PASSWORD=
QMT_RPC_ALLOW_ORDER=false
"""


def check_python_env() -> dict:
    """返回 Python 版本与核心依赖检查结果。

    Returns:
        {
          "python": "3.9.x",
          "executable": "...",
          "python_supported": True,
          "python_issue": "",
          "missing": ["pandas", ...],          # pip 可装的缺失模块
          "xqm_missing": ["fastapi", ...],      # web2.0 网关依赖
          "rpc_missing": ["redis"],             # 大QMT RPC 依赖
          "special_missing": ["xtquant", ...], # 需特殊安装的缺失模块
        }
    """
    import importlib
    major_minor = sys.version_info[:2]
    python_supported = SUPPORTED_PYTHON_MIN <= major_minor <= SUPPORTED_PYTHON_MAX
    python_issue = ""
    if major_minor < SUPPORTED_PYTHON_MIN:
        python_issue = (
            f"当前 Python {major_minor[0]}.{major_minor[1]} 低于最低要求 "
            f"{SUPPORTED_PYTHON_MIN[0]}.{SUPPORTED_PYTHON_MIN[1]}"
        )
    elif major_minor > SUPPORTED_PYTHON_MAX:
        python_issue = (
            f"当前 Python {major_minor[0]}.{major_minor[1]} 高于已验证范围 "
            f"{SUPPORTED_PYTHON_MAX[0]}.{SUPPORTED_PYTHON_MAX[1]}"
        )
    info = {
        "python": ".".join(map(str, sys.version_info[:3])),
        "executable": sys.executable,
        "python_supported": python_supported,
        "python_issue": python_issue,
        "missing": [],
        "xqm_missing": [],
        "rpc_missing": [],
        "special_missing": [],
    }
    for mod in CORE_DEPS:
        try:
            importlib.import_module(mod)
        except Exception:
            info["missing"].append(mod)
    for mod in XQM_DEPS:
        try:
            importlib.import_module(mod)
        except Exception:
            info["xqm_missing"].append(mod)
    for mod in RPC_DEPS:
        try:
            importlib.import_module(mod)
        except Exception:
            info["rpc_missing"].append(mod)
    for mod in SPECIAL_DEPS:
        try:
            importlib.import_module(mod)
        except Exception:
            info["special_missing"].append(mod)
    return info


def discover_qmt_paths(candidates: list[str] | None = None) -> list[str]:
    """在常见安装位置里寻找 QMT 的 userdata_mini 目录。"""
    found: list[str] = []
    seen: set[str] = set()
    for raw_path in candidates or QMT_PATH_CANDIDATES:
        path = Path(os.path.expandvars(raw_path)).expanduser()
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        if path.exists() and path.is_dir():
            found.append(str(path))
    return found


def build_account_config(
    account_id: str,
    qmt_path: str,
    account_type: str = "STOCK",
) -> dict:
    """构造单账号 account_config.json，保持与项目现有示例兼容。"""
    return {
        "account_id": account_id.strip(),
        "account_type": (account_type.strip() or "STOCK").upper(),
        "qmt_path": qmt_path.strip(),
    }


def ensure_env_file(path: Path | None = None) -> tuple[bool, Path]:
    """确保 .env 存在；存在时不覆盖，避免误改用户 token。"""
    target = path or ENV_PATH
    if target.exists():
        return False, target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(DEFAULT_ENV_TEXT, encoding="utf-8")
    return True, target


def ensure_stock_pool_file(path: Path | None = None) -> tuple[bool, Path]:
    """确保 stock_pool.json 存在；默认创建空股票池。"""
    target = path or STOCK_POOL_PATH
    if target.exists():
        return False, target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("[]\n", encoding="utf-8")
    return True, target


def ensure_account_config_file(
    account_id: str,
    qmt_path: str,
    account_type: str = "STOCK",
    path: Path | None = None,
) -> tuple[bool, Path]:
    """确保 account_config.json 存在；存在时不覆盖。"""
    target = path or CONFIG_PATH
    if target.exists():
        return False, target
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = build_account_config(account_id, qmt_path, account_type)
    target.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return True, target


def check_account_config() -> dict:
    """校验 account_config.json：是否存在、JSON 合法、字段齐全、qmt_path 存在。

    Returns:
        {
          "file_exists": bool,
          "json_valid": bool,
          "error": "..." or None,
          "accounts": [
              {"account_id": "xxx", "qmt_path": "...",
               "qmt_path_exists": bool, "issues": [...]},
              ...
          ],
        }
    """
    result = {
        "file_exists": CONFIG_PATH.exists(),
        "json_valid": False,
        "error": None,
        "accounts": [],
    }
    if not result["file_exists"]:
        result["error"] = f"配置文件不存在: {CONFIG_PATH}"
        return result

    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        result["json_valid"] = True
    except Exception as e:
        result["error"] = f"JSON 解析失败: {e}"
        return result

    accounts = cfg.get("accounts") or []
    if not accounts:
        # 单账号兼容
        accounts = [{
            "account_id":   cfg.get("account_id", ""),
            "account_type": cfg.get("account_type", "STOCK"),
            "qmt_path":     cfg.get("qmt_path", ""),
        }]

    seen_ids = set()
    for acc in accounts:
        issues = []
        acc_id   = acc.get("account_id", "")
        qmt_path = acc.get("qmt_path", "")
        if not acc_id:
            issues.append("缺少 account_id")
        elif acc_id in seen_ids:
            issues.append("account_id 重复")
        else:
            seen_ids.add(acc_id)
        if not qmt_path:
            issues.append("缺少 qmt_path")
        path_exists = bool(qmt_path) and os.path.exists(qmt_path)
        if qmt_path and not path_exists:
            issues.append("qmt_path 不存在")
        result["accounts"].append({
            "account_id":      acc_id,
            "qmt_path":        qmt_path,
            "qmt_path_exists": path_exists,
            "issues":          issues,
        })
    return result


def cmd_check_env(_args) -> int:
    info = check_python_env()
    print(f"Python 版本 : {info['python']}")
    print(f"解释器路径  : {info['executable']}")
    if info["python_supported"]:
        print(
            f"版本范围    : ✓ 已验证范围 "
            f"{SUPPORTED_PYTHON_MIN[0]}.{SUPPORTED_PYTHON_MIN[1]}-"
            f"{SUPPORTED_PYTHON_MAX[0]}.{SUPPORTED_PYTHON_MAX[1]}"
        )
    else:
        print(f"版本范围    : ✗ {info['python_issue']}")
    print()
    if not info["missing"]:
        print("✓ PyPI 核心依赖全部已安装")
    else:
        print(f"✗ 缺失 {len(info['missing'])} 个 PyPI 依赖:")
        for m in info["missing"]:
            print(f"    - {m}")
        print(f"\n  执行菜单 [2] 自动安装，或手动运行:")
        print(f"    pip install -r utils/requirements.txt")
    print()
    if not info["special_missing"]:
        print("✓ QMT SDK (xtquant) 已安装")
    else:
        print("✗ 缺失 QMT SDK (需手动从 QMT 客户端目录安装):")
        for m in info["special_missing"]:
            print(f"    - {m}")
        print("    具体方法见 utils/INSTALL.md")
    print()
    if not info["xqm_missing"]:
        print("✓ web2.0 / xtquant_manager 依赖已安装")
    else:
        print("ℹ web2.0 / xtquant_manager 可选依赖未装:")
        for m in info["xqm_missing"]:
            print(f"    - {m}")
    if not info["rpc_missing"]:
        print("✓ 大QMT RPC 可选依赖已安装")
    else:
        print("ℹ 大QMT RPC 可选依赖未装:")
        for m in info["rpc_missing"]:
            print(f"    - {m}")
    return 0 if info["python_supported"] and not info["missing"] and not info["special_missing"] else 1


def cmd_setup_wizard(_args) -> int:
    """首次部署向导：只生成安全默认配置，不自动安装依赖或开启实盘自动交易。"""
    blockers: list[str] = []

    def ask(prompt: str) -> str:
        try:
            return input(prompt).strip()
        except (KeyboardInterrupt, EOFError):
            return ""

    print("=" * 64)
    print("                  miniQMT 首次部署向导")
    print("=" * 64)
    print("本向导只做环境体检和配置骨架生成，不会自动安装依赖、不会启动实盘、不会打开自动交易总开关。")
    print()

    print("—— 1/5 Python 与依赖体检 ——")
    info = check_python_env()
    print(f"Python 版本 : {info['python']}")
    print(f"解释器路径  : {info['executable']}")
    if info["python_supported"]:
        print("版本检查    : ✓ 通过")
    else:
        print(f"版本检查    : ✗ {info['python_issue']}")
        blockers.append(info["python_issue"])

    if info["missing"]:
        print(f"核心依赖    : ✗ 缺失 {', '.join(info['missing'])}")
        blockers.append("安装核心依赖: pip install -r utils/requirements.txt")
    else:
        print("核心依赖    : ✓ 通过")

    if info["special_missing"]:
        print(f"QMT SDK     : ✗ 缺失 {', '.join(info['special_missing'])}")
        blockers.append("安装 xtquant：请按 utils/INSTALL.md 从 QMT 客户端目录配置")
    else:
        print("QMT SDK     : ✓ 通过")

    if info["xqm_missing"]:
        print(f"web2.0 网关 : ℹ 可选依赖未装 {', '.join(info['xqm_missing'])}")
    else:
        print("web2.0 网关 : ✓ 可用")
    if info["rpc_missing"]:
        print(f"大QMT RPC   : ℹ 可选依赖未装 {', '.join(info['rpc_missing'])}")
    else:
        print("大QMT RPC   : ✓ 可用")
    print()

    print("—— 2/5 本机 .env 安全默认值 ——")
    created, env_path = ensure_env_file()
    if created:
        print(f"✓ 已创建 {env_path}")
    else:
        print(f"✓ 已存在 {env_path}，未覆盖")
    print("  默认关闭 Tushare、IPC、RPC 与 RPC 下单权限；真实 token 后续按需填写。")
    print()

    print("—— 3/5 交易账号配置 ——")
    if CONFIG_PATH.exists():
        print(f"✓ 已存在 {CONFIG_PATH}，未覆盖")
        cfg_status = check_account_config()
        if not cfg_status["json_valid"]:
            blockers.append(f"修正 account_config.json: {cfg_status['error']}")
        else:
            bad_accounts = [
                acc for acc in cfg_status["accounts"]
                if acc["issues"]
            ]
            if bad_accounts:
                blockers.append("修正 account_config.json 中的账号 ID 或 qmt_path")
                for acc in bad_accounts:
                    print(f"  ✗ {acc['account_id'] or '<空账号>'}: {'; '.join(acc['issues'])}")
            else:
                print(f"  ✓ 检测到 {len(cfg_status['accounts'])} 个有效账号")
    else:
        found_paths = discover_qmt_paths()
        if found_paths:
            print("检测到可能的 QMT userdata_mini 目录：")
            for i, path in enumerate(found_paths, 1):
                print(f"  [{i}] {path}")
        else:
            print("未在常见位置自动发现 QMT userdata_mini 目录。")

        account_id = ask("请输入交易账号 ID（留空=暂不创建 account_config.json）: ")
        if account_id:
            account_type = ask("账号类型（默认 STOCK）: ") or "STOCK"
            default_qmt_path = found_paths[0] if found_paths else ""
            if default_qmt_path:
                qmt_path = ask(f"QMT userdata_mini 路径（留空使用 {default_qmt_path}）: ") or default_qmt_path
            else:
                qmt_path = ask("QMT userdata_mini 路径（例如 C:/QMT/userdata_mini，留空=暂不创建）: ")

            if qmt_path:
                ensure_account_config_file(account_id, qmt_path, account_type)
                print(f"✓ 已创建 {CONFIG_PATH}")
                if not Path(qmt_path).exists():
                    print("  ⚠ 当前路径不存在，后续请确认 QMT 安装目录。")
                    blockers.append("确认 account_config.json 中的 qmt_path 是否为真实 userdata_mini 目录")
            else:
                blockers.append("创建 account_config.json：需要交易账号 ID 和 QMT userdata_mini 路径")
                print("已跳过 account_config.json 创建。")
        else:
            blockers.append("创建 account_config.json：需要交易账号 ID 和 QMT userdata_mini 路径")
            print("已跳过 account_config.json 创建。")
    print()

    print("—— 4/5 股票池文件 ——")
    created, stock_pool_path = ensure_stock_pool_file()
    if created:
        print(f"✓ 已创建空股票池 {stock_pool_path}")
    else:
        print(f"✓ 已存在 {stock_pool_path}，未覆盖")
    print("  首次建议先保持空股票池，进入 Web 后再逐步添加。")
    print()

    print("—— 5/5 web2.0 网关配置 ——")
    if XQM_CONFIG_PATH.exists():
        print(f"✓ 已存在 {XQM_CONFIG_PATH}，未覆盖")
    elif CONFIG_PATH.exists():
        confirm = ask("是否生成 xtquant_manager_config.json（默认仅绑定 127.0.0.1）? [Y/n]: ")
        if confirm.lower() != "n":
            out_path = _ensure_xqm_config(host="127.0.0.1")
            if out_path:
                print("  ✓ 已生成本机访问配置；如需局域网访问，请设置 token 后再改 host。")
            else:
                print("  ⚠ 生成失败，请先检查 account_config.json。")
        else:
            print("已跳过 xtquant_manager_config.json 创建。")
    else:
        print("跳过：缺少 account_config.json。")
    print()

    print("=" * 64)
    print("下一步建议")
    print("=" * 64)
    if info["missing"]:
        print("1. 先安装核心依赖：菜单 [2] 或命令 python scripts/_launcher.py install-deps")
    else:
        print("1. 核心依赖已就绪，可跳过安装步骤。")
    print("2. 运行配置检查：菜单 [3] 或命令 python scripts/_launcher.py check-config")
    print("3. 首次启动建议使用模拟模式：菜单 [8]")
    print("4. web1.0 默认访问：http://127.0.0.1:5000")
    print("5. 确认 QMT 登录和模拟流程无误后，再考虑实盘与自动交易开关。")

    if blockers:
        print()
        print("仍需处理：")
        for item in blockers:
            print(f"  - {item}")
        return 1

    print()
    print("✓ 首次部署基础文件已就绪。")
    return 0


def cmd_install_deps(_args) -> int:
    if not REQUIREMENTS_FILE.exists():
        print(f"[错误] 找不到依赖文件: {REQUIREMENTS_FILE}")
        return 1
    print(f"将运行: pip install -r {REQUIREMENTS_FILE}")
    print("(xtquant 可能失败，需手动从 QMT 客户端安装；其他依赖应正常)")
    print()
    cmd = [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS_FILE)]
    try:
        rc = subprocess.call(cmd, cwd=str(PROJECT_ROOT))
    except OSError as e:
        print(f"[错误] 启动 pip 失败: {e}")
        return 1
    print()
    if rc == 0:
        print("✓ 安装完成")
    else:
        print(f"⚠ pip 退出码 = {rc}（xtquant 失败属正常，其余请检查日志）")
    return rc


def cmd_check_config(_args) -> int:
    r = check_account_config()
    if not r["file_exists"]:
        print(f"✗ {r['error']}")
        return 1
    if not r["json_valid"]:
        print(f"✗ {r['error']}")
        return 1

    print(f"配置文件: {CONFIG_PATH}")
    print(f"账号数量: {len(r['accounts'])}")
    print()
    print(f"{'#':<3} {'账号 ID':<14} {'QMT 路径':<32} {'状态'}")
    print("-" * 72)
    all_ok = True
    for i, acc in enumerate(r["accounts"], 1):
        path_disp = (acc["qmt_path"] or "(空)")[:32]
        if acc["issues"]:
            status = "✗ " + "; ".join(acc["issues"])
            all_ok = False
        else:
            status = "✓ OK"
        print(f"{i:<3} {acc['account_id']:<14} {path_disp:<32} {status}")
    print()
    return 0 if all_ok else 1


def cmd_git_pull(_args) -> int:
    git_dir = PROJECT_ROOT / ".git"
    if not git_dir.exists():
        print(f"[错误] {PROJECT_ROOT} 不是 git 仓库")
        return 1
    try:
        st = subprocess.run(
            ["git", "status", "-sb"], cwd=str(PROJECT_ROOT),
            capture_output=True, text=True, timeout=10,
        )
        print("当前状态:")
        print(st.stdout or "(无)")
    except FileNotFoundError:
        print("[错误] 找不到 git 命令，请确认已安装 Git for Windows")
        return 1
    print("\n执行: git pull")
    print("-" * 60)
    rc = subprocess.call(["git", "pull"], cwd=str(PROJECT_ROOT))
    print("-" * 60)
    if rc == 0:
        print("✓ 更新完成")
    else:
        print(f"✗ git pull 退出码 = {rc}")
    return rc


# ============================================================================
# XtQuantManager 网关管理
# ============================================================================
XQM_DEFAULT_HOST = "0.0.0.0"        # 绑定地址：全部网卡（含 WAN/LAN/本机）
XQM_CLIENT_HOST  = "127.0.0.1"      # 客户端访问地址：本机健康检查/UI 打开（0.0.0.0 不能作客户端目标）
XQM_DEFAULT_PORT = 8888
XQM_MODULE = "xtquant_manager"


def _get_lan_ip() -> str:
    """获取本机局域网 IP（用于菜单显示，方便用户从其他设备访问）。
    无网卡或失败时返回空字符串，调用方应做空值兼容。"""
    import socket
    try:
        # 用 UDP 连接外部地址触发路由选择（不真正发包）以获得首选出口 IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return ""


def _xqm_config_path() -> Path:
    local = XQM_CONFIG_PATH
    if local.exists():
        return local
    return PROJECT_ROOT / "xtquant_manager" / "standalone_config.py"


def _ensure_xqm_config(host: str = "0.0.0.0") -> Path | None:
    """从 account_config.json 自动生成 xtquant_manager_config.json。
    账号列表从 account_config.json 读取，其他参数使用默认值。
    """
    config_path = CONFIG_PATH  # account_config.json
    if not config_path.exists():
        return None

    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    accounts = cfg.get("accounts") or []
    if not accounts:
        # 单账号兼容
        acc_id = cfg.get("account_id", "")
        if acc_id:
            accounts = [{
                "account_id": acc_id,
                "account_type": cfg.get("account_type", "STOCK"),
                "qmt_path": cfg.get("qmt_path", ""),
            }]

    if not accounts:
        return None

    # 构建 xtquant_manager 配置
    xqm_cfg = {
        "host": host,
        "port": 8888,
        "api_token": "",
        "rate_limit": 600,
        "enable_stop_profit": True,
        "accounts": [
            {
                "account_id": a["account_id"],
                "qmt_path": a.get("qmt_path", ""),
                "account_type": a.get("account_type", "STOCK"),
            }
            for a in accounts
            if a.get("account_id")
        ],
    }

    out_path = XQM_CONFIG_PATH
    out_path.write_text(json.dumps(xqm_cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  ✓ 已从 account_config.json 自动生成配置: {out_path}")
    print(f"    共 {len(xqm_cfg['accounts'])} 个账号")
    return out_path


def _xqm_pid_file() -> Path:
    return PROJECT_ROOT / "data" / ".xqm_manager.pid"


def _xqm_read_pid() -> int | None:
    p = _xqm_pid_file()
    if not p.exists():
        return None
    try:
        return int(p.read_text(encoding="ascii").strip())
    except (ValueError, OSError):
        return None


def _xqm_is_port_in_use(port: int = XQM_DEFAULT_PORT) -> bool:
    try:
        r = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=5
        )
        return any(f":{port} " in line and "LISTENING" in line for line in r.stdout.splitlines())
    except Exception:
        return False


def _xqm_health_check(host: str = XQM_CLIENT_HOST, port: int = XQM_DEFAULT_PORT) -> bool:
    """对运行中的服务做健康检查。
    注意：host 必须是可作客户端目标的地址（127.0.0.1/本机 IP）；
    0.0.0.0 是绑定地址、不能作客户端目标——若传入 0.0.0.0 自动改用 127.0.0.1。"""
    if host in ("0.0.0.0", "::", ""):
        host = XQM_CLIENT_HOST
    import urllib.request
    try:
        req = urllib.request.Request(f"http://{host}:{port}/api/v1/health", method="GET")
        resp = urllib.request.urlopen(req, timeout=3)
        return resp.status == 200
    except Exception:
        return False


def _xqm_access_urls(bind_host: str, port: int) -> str:
    """构造访问地址显示文本。
    bind_host=0.0.0.0 时同时列出本机和局域网两个 URL，方便用户复制；
    否则按绑定地址显示。"""
    if bind_host in ("0.0.0.0", "::", ""):
        lan_ip = _get_lan_ip()
        local = f"http://{XQM_CLIENT_HOST}:{port}"
        if lan_ip and lan_ip != XQM_CLIENT_HOST:
            return f"{local}  (本机)  |  http://{lan_ip}:{port}  (局域网)"
        return f"{local}  (绑定 0.0.0.0：全部网卡可达)"
    return f"http://{bind_host}:{port}"


def cmd_xqm_start(_args) -> int:
    host = os.environ.get("XQM_HOST", XQM_DEFAULT_HOST)
    port = int(os.environ.get("XQM_PORT", str(XQM_DEFAULT_PORT)))

    if _xqm_is_port_in_use(port):
        if _xqm_health_check(XQM_CLIENT_HOST, port):
            print(f"  ✓ xtquant_manager 已在运行: {_xqm_access_urls(host, port)}")
            return 0
        else:
            print(f"  ⚠ 端口 {port} 被占用但健康检查失败，尝试清理...")
            pid = _xqm_read_pid()
            if pid and pid_alive(pid):
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
                time.sleep(1)

    config_path = _xqm_config_path()

    # 如果配置文件不存在，从 account_config.json 自动生成
    if not config_path.exists() or config_path.name != "xtquant_manager_config.json":
        config_path = _ensure_xqm_config()
        if config_path is None:
            print("  ✗ 无法生成 xtquant_manager 配置: account_config.json 不存在或无有效账号")
            return 1

    config_arg = f"--config {config_path}"

    data_dir = PROJECT_ROOT / "data"
    data_dir.mkdir(exist_ok=True)

    creationflags = 0x00000010
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", XQM_MODULE, "--host", host, "--port", str(port)]
            + (["--config", str(config_path)] if config_arg else []),
            cwd=str(PROJECT_ROOT),
            creationflags=creationflags,
            close_fds=True,
        )
    except OSError as e:
        print(f"  ✗ 启动 xtquant_manager 失败: {e}")
        return 1

    try:
        _xqm_pid_file().write_text(str(proc.pid), encoding="ascii")
    except OSError:
        pass

    print(f"  启动中 (PID={proc.pid})，等待服务就绪...")
    for _ in range(15):
        time.sleep(1)
        if _xqm_health_check(XQM_CLIENT_HOST, port):
            print(f"  ✓ xtquant_manager 已启动: {_xqm_access_urls(host, port)}")
            return 0
    print(f"  ⚠ 服务已启动但健康检查超时，稍后请访问 http://{XQM_CLIENT_HOST}:{port}/api/v1/health 确认")
    return 0


def cmd_xqm_stop(_args) -> int:
    host = os.environ.get("XQM_HOST", XQM_DEFAULT_HOST)
    port = int(os.environ.get("XQM_PORT", str(XQM_DEFAULT_PORT)))

    pid = _xqm_read_pid()
    if pid and pid_alive(pid):
        print(f"  停止 xtquant_manager (PID={pid})...")
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        time.sleep(0.5)

    if _xqm_is_port_in_use(port):
        print(f"  清理端口 {port} 上的残留进程...")
        r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True)
        for line in r.stdout.splitlines():
            if f":{port} " in line and "LISTENING" in line:
                parts = line.split()
                if len(parts) >= 5:
                    subprocess.run(["taskkill", "/PID", parts[-1], "/F"], capture_output=True)

    _xqm_pid_file().unlink(missing_ok=True)

    if not _xqm_is_port_in_use(port):
        print("  ✓ xtquant_manager 已停止")
    else:
        print("  ⚠ 停止失败，请手动检查")
    return 0


def cmd_xqm_status(_args) -> int:
    host = os.environ.get("XQM_HOST", XQM_DEFAULT_HOST)
    port = int(os.environ.get("XQM_PORT", str(XQM_DEFAULT_PORT)))

    print("=" * 48)
    print("  XtQuantManager 状态")
    print("=" * 48)
    print()

    pid = _xqm_read_pid()
    port_used = _xqm_is_port_in_use(port)
    healthy = _xqm_health_check(XQM_CLIENT_HOST, port)

    if pid:
        print(f"  PID      : {pid} {'(存活)' if pid_alive(pid) else '(已失效)'}")
    else:
        print("  PID      : (未记录)")
    print(f"  端口     : {port} {'(监听中)' if port_used else '(空闲)'}")
    print(f"  健康检查 : {'✓ 通过' if healthy else ('✗ 失败' if port_used else '—')}")
    print(f"  绑定     : {host}")
    print(f"  访问地址 : {_xqm_access_urls(host, port)}")

    if healthy:
        import urllib.request, json
        try:
            req = urllib.request.Request(f"http://{XQM_CLIENT_HOST}:{port}/api/v1/health")
            data = json.loads(urllib.request.urlopen(req, timeout=3).read())
            acc_info = data.get("data", {})
            total = acc_info.get("total", 0)
            h_count = acc_info.get("healthy", 0)
            print(f"  账号     : {total} 个注册, {h_count} 个健康")
        except Exception:
            pass

    config_path = _xqm_config_path()
    print(f"  配置文件 : {config_path} {'(存在)' if config_path.exists() else '(缺失)'}")
    return 0


def cmd_xqm_ui(_args) -> int:
    import webbrowser

    host = os.environ.get("XQM_HOST", XQM_DEFAULT_HOST)
    port = int(os.environ.get("XQM_PORT", str(XQM_DEFAULT_PORT)))

    # 浏览器无法访问 0.0.0.0；统一用 127.0.0.1 打开（本机浏览器场景）
    if _xqm_health_check(XQM_CLIENT_HOST, port):
        url = f"http://{XQM_CLIENT_HOST}:{port}/"
        print(f"  打开 web2.0: {url}")
        lan_ip = _get_lan_ip()
        if host in ("0.0.0.0", "::") and lan_ip and lan_ip != XQM_CLIENT_HOST:
            print(f"  局域网访问: http://{lan_ip}:{port}/")
        webbrowser.open(url)
        return 0

    # 网关未运行，检查本地文件
    web2_dist = PROJECT_ROOT / "web2.0" / "dist" / "index.html"
    web1_index = PROJECT_ROOT / "web1.0" / "index.html"

    if web2_dist.exists():
        print(f"  xtquant_manager 未运行，打开本地 web2.0 文件")
        print(f"  注意: 本地文件模式下无法调用 API，请先启动 xtquant_manager")
        webbrowser.open(web2_dist.as_uri())
    elif web1_index.exists():
        webbrowser.open(web1_index.as_uri())
    else:
        print("  未找到 web 界面文件。请先: cd web2.0 && npm run build")

    print(f"  提示: 用 miniqmt.bat 菜单 [d] 启动 xtquant_manager 后，访问 http://{XQM_CLIENT_HOST}:{port}/")
    return 0


def cmd_xqm_logs(_args) -> int:
    """查看 xtquant_manager 最近日志（读取 logs/xqm_manager.log 尾部）。"""
    log_file = PROJECT_ROOT / "logs" / "xqm_manager.log"
    if not log_file.exists():
        print("  日志文件不存在: logs/xqm_manager.log")
        print("  启动 xtquant_manager 服务后会自动生成日志")
        return 0
    try:
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = lines[-40:]  # 最后 40 行
        print(f"  日志文件: {log_file}")
        print(f"  共 {len(lines)} 行, 显示最后 {len(tail)} 行")
        print("=" * 64)
        for line in tail:
            print(f"  {line}")
    except Exception as e:
        print(f"  读取日志失败: {e}")
    return 0


# ---------------------------------------------------------------------------
# 自动买入服务 (miniqmt_autobuy) 管理
# ---------------------------------------------------------------------------
AUTOBUY_APP    = PROJECT_ROOT / "autobuy" / "app.py"
AUTOBUY_CFG    = PROJECT_ROOT / "autobuy" / "miniqmt_autobuy.cfg"
AUTOBUY_LOG    = PROJECT_ROOT / "logs" / "miniqmt_autobuy.log"
AUTOBUY_STATUS = PROJECT_ROOT / "data" / ".autobuy_status.json"


def _autobuy_pid_file() -> Path:
    return PROJECT_ROOT / "data" / ".autobuy.pid"


def _autobuy_read_pid() -> int | None:
    p = _autobuy_pid_file()
    if not p.exists():
        return None
    try:
        return int(p.read_text(encoding="ascii").strip())
    except (ValueError, OSError):
        return None


def cmd_autobuy_start(_args) -> int:
    pid = _autobuy_read_pid()
    if pid and pid_alive(pid):
        print(f"  ✓ 自动买入服务已在运行 (PID={pid})")
        return 0
    if not AUTOBUY_APP.exists():
        print(f"  ✗ 未找到 {AUTOBUY_APP}")
        return 1
    if not AUTOBUY_CFG.exists():
        print("  ✗ 未找到配置文件 autobuy/miniqmt_autobuy.cfg，请先创建/检查")
        return 1

    (PROJECT_ROOT / "data").mkdir(exist_ok=True)
    creationflags = 0x00000010  # CREATE_NEW_CONSOLE，独立控制台
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "autobuy.app"],
            cwd=str(PROJECT_ROOT),
            creationflags=creationflags,
            close_fds=True,
        )
    except OSError as e:
        print(f"  ✗ 启动自动买入服务失败: {e}")
        return 1

    try:
        _autobuy_pid_file().write_text(str(proc.pid), encoding="ascii")
    except OSError:
        pass
    print(f"  ✓ 自动买入服务已启动 (PID={proc.pid})")
    print(f"    日志: {AUTOBUY_LOG}")
    print("    ⚠ 需保证目标 web_server 已运行 (见 autobuy/miniqmt_autobuy.cfg [web] base_url)")
    return 0


def cmd_autobuy_stop(_args) -> int:
    pid = _autobuy_read_pid()
    if pid and pid_alive(pid):
        print(f"  停止自动买入服务 (PID={pid})...")
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        time.sleep(0.5)
    else:
        print("  自动买入服务未在运行")
    _autobuy_pid_file().unlink(missing_ok=True)
    try:
        AUTOBUY_STATUS.unlink(missing_ok=True)
    except OSError:
        pass
    print("  ✓ 已停止")
    return 0


def cmd_autobuy_status(_args) -> int:
    print("=" * 48)
    print("  自动买入服务 (miniqmt_autobuy) 状态")
    print("=" * 48)
    pid = _autobuy_read_pid()
    if pid and pid_alive(pid):
        print(f"  运行中 (PID={pid})")
    else:
        print("  未运行")
    if AUTOBUY_STATUS.exists():
        try:
            st = json.loads(AUTOBUY_STATUS.read_text(encoding="utf-8"))
            print(f"  最近触发 : {st.get('last_run')} [{st.get('trigger')}]")
            print(f"  候选/通过 : {st.get('candidates')} / {st.get('passed')}")
            print(f"  本轮买入 : {st.get('bought')}")
            print(f"  更新时间 : {st.get('updated_at')}")
        except Exception as e:
            print(f"  读取状态文件失败: {e}")
    else:
        print("  暂无运行记录 (状态文件不存在)")
    return 0


def cmd_autobuy_logs(_args) -> int:
    if not AUTOBUY_LOG.exists():
        print("  日志文件不存在: logs/miniqmt_autobuy.log")
        print("  启动自动买入服务后会自动生成日志")
        return 0
    try:
        lines = AUTOBUY_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = lines[-40:]
        print(f"  日志文件: {AUTOBUY_LOG}")
        print(f"  共 {len(lines)} 行, 显示最后 {len(tail)} 行")
        print("=" * 64)
        for line in tail:
            print(f"  {line}")
    except Exception as e:
        print(f"  读取日志失败: {e}")
    return 0


# ---------------------------------------------------------------------------
# Tushare 数据源配置
# ---------------------------------------------------------------------------
def _read_env_key(key: str) -> str:
    """从 .env 文件读取指定 key 的值。"""
    if not ENV_PATH.exists():
        return ""
    for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip()
    return ""

def _write_env_key(key: str, value: str) -> None:
    """在 .env 中设置或更新 KEY=value。"""
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    else:
        lines = []
    found = False
    for i, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, _ = line.partition("=")
        if k.strip() == key:
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

def _mask_token(token: str) -> str:
    """Token 脱敏显示: 空=未配置, 只显示后8位。"""
    if not token:
        return "未配置"
    return "***" + token[-8:]

def cmd_tushare_config(_args) -> int:
    """Tushare 数据源配置子菜单。"""
    import webbrowser

    def _status():
        token = _read_env_key("TUSHARE_TOKEN")
        enabled = _read_env_key("ENABLE_TUSHARE_DATA_SOURCE").lower() in ("true", "1", "yes", "on")
        print("\n" + "=" * 48)
        print("  Tushare Pro 数据源 — 当前状态")
        print("=" * 48)
        print(f"  Token   : {_mask_token(token)}")
        print(f"  总开关  : {'✓ 已启用' if enabled else '✗ 已禁用'}")
        print(f"  说明    : 启用后在标准模式下优先于 Mootdx 获取历史K线和股票名称")
        print("-" * 48)

    while True:
        _status()
        print("  [1] 开启 / 关闭 Tushare 数据源")
        print("  [2] 修改 Tushare Token")
        print("  [3] 测试 Tushare 连通性")
        print("  [4] 打开 Tushare Pro 官网 (注册/获取 Token)")
        print("  [0] 返回主菜单")
        print()
        c = input("请选择 [0-4]: ").strip()

        if c == "0":
            return 0
        elif c == "1":
            current = _read_env_key("ENABLE_TUSHARE_DATA_SOURCE").lower() in ("true", "1", "yes", "on")
            new_val = "false" if current else "true"
            _write_env_key("ENABLE_TUSHARE_DATA_SOURCE", new_val)
            print(f"\n  ✓ ENABLE_TUSHARE_DATA_SOURCE → {new_val}")
            print("  ⚠ 重启 miniQMT 后生效")
            input("\n按回车键继续...")
        elif c == "2":
            print("\n请输入新的 Tushare Token (留空=取消):")
            new_token = input("> ").strip()
            if new_token:
                _write_env_key("TUSHARE_TOKEN", new_token)
                print(f"\n  ✓ TUSHARE_TOKEN → {_mask_token(new_token)}")
                print("  ⚠ 重启 miniQMT 后生效")
            else:
                print("  已取消")
            input("\n按回车键继续...")
        elif c == "3":
            print("\n正在测试 Tushare 连通性...")
            token = _read_env_key("TUSHARE_TOKEN")
            if not token:
                print("  ✗ TUSHARE_TOKEN 未配置，请先设置 Token")
                input("\n按回车键继续...")
                continue
            try:
                import tushare as ts
                ts.set_token(token)
                pro = ts.pro_api()
                df = pro.stock_basic(ts_code='000001.SZ', fields='name')
                if not df.empty:
                    print(f"  ✓ 连通正常！示例: 000001.SZ = {df.iloc[0]['name']}")
                else:
                    print("  ✗ Token 可能无效（返回空数据）")
            except ImportError:
                print("  ✗ tushare 包未安装 (pip install tushare)")
            except Exception as e:
                print(f"  ✗ 连通性测试失败: {e}")
            input("\n按回车键继续...")
        elif c == "4":
            webbrowser.open("https://tushare.pro")
            print("\n  已打开浏览器...")
            time.sleep(1)
        else:
            print(f"\n  无效选择: {c!r}")
            time.sleep(1)


# ---------------------------------------------------------------------------
# 大QMT IPC Trader 配置
# ---------------------------------------------------------------------------
def cmd_qmt_ipc_config(_args) -> int:
    """大QMT文件IPC Trader 配置子菜单。"""
    import webbrowser

    def _heartbeat_status():
        """扫描 IPC_ROOT 下所有账号子目录的心跳文件。"""
        ipc_root = _read_env_key("QMT_IPC_ROOT") or r"C:\QuantIPC"
        if not os.path.isdir(ipc_root):
            return []
        results = []
        for name in sorted(os.listdir(ipc_root)):
            sub = os.path.join(ipc_root, name)
            if not os.path.isdir(sub):
                continue
            hb = os.path.join(sub, "status", "heartbeat.json")
            if os.path.exists(hb):
                try:
                    age = time.time() - os.path.getmtime(hb)
                    alive = age < 10
                    with open(hb, encoding="utf-8") as f:
                        data = json.load(f)
                    acc = data.get("account_id", name)
                except Exception:
                    acc = name
                    alive = False
                    age = -1
                results.append((acc, alive, age))
        return results

    def _status():
        enabled = _read_env_key("ENABLE_QMT_IPC_FALLBACK").lower() in ("true", "1", "yes", "on")
        ipc_root = _read_env_key("QMT_IPC_ROOT") or "C:\\QuantIPC"
        print("\n" + "=" * 48)
        print("  大QMT文件IPC Trader — 当前状态")
        print("=" * 48)
        print(f"  总开关  : {'✓ 已启用' if enabled else '✗ 已禁用 (使用 xttrader 直连)'}")
        print(f"  IPC目录 : {ipc_root}")
        hb_list = _heartbeat_status()
        if hb_list:
            print(f"  大QMT心跳:")
            for acc, alive, age in hb_list:
                tag = f"在线 ({age:.0f}秒前)" if alive else f"离线 (上次 {age:.0f}秒前)" if age > 0 else "离线"
                print(f"    {acc}: {tag}")
        else:
            print(f"  大QMT心跳: 未检测到 (等待 executor 启动)")
        if enabled:
            print(f"  提示    : 下单后 1-2 秒成交, 适合中低频策略")
        print("-" * 48)

    while True:
        _status()
        print("  [1] 开启 / 关闭 大QMT IPC Fallback")
        print("  [2] 修改 IPC 文件目录")
        print("  [3] 打开部署操作手册")
        print("  [0] 返回主菜单")
        print()
        c = input("请选择 [0-3]: ").strip()

        if c == "0":
            return 0
        elif c == "1":
            current = _read_env_key("ENABLE_QMT_IPC_FALLBACK").lower() in ("true", "1", "yes", "on")
            new_val = "false" if current else "true"
            _write_env_key("ENABLE_QMT_IPC_FALLBACK", new_val)
            print(f"\n  ✓ ENABLE_QMT_IPC_FALLBACK → {new_val}")
            if new_val == "true":
                print("  ℹ 大QMT模式: 所有交易通过文件IPC路由到 executor 执行")
            else:
                print("  ℹ xttrader 直连模式: 恢复默认行为")
            print("  ⚠ 重启 miniQMT 后生效")
            input("\n按回车键继续...")
        elif c == "2":
            current = _read_env_key("QMT_IPC_ROOT") or "C:\\QuantIPC"
            print(f"\n当前 IPC 目录: {current}")
            print("请输入新路径 (留空=取消):")
            new_path = input("> ").strip()
            if new_path:
                _write_env_key("QMT_IPC_ROOT", new_path)
                print(f"\n  ✓ QMT_IPC_ROOT → {new_path}")
                print("  ⚠ 两端路径必须一致，重启后生效")
            else:
                print("  已取消")
            input("\n按回车键继续...")
        elif c == "3":
            doc_path = PROJECT_ROOT / "qmt-trader" / "部署手册.md"
            if doc_path.exists():
                webbrowser.open(str(doc_path))
                print("\n  已打开部署手册...")
            else:
                print("\n  ✗ 部署手册未找到")
            time.sleep(1)
        else:
            print(f"\n  无效选择: {c!r}")
            time.sleep(1)


def _read_xttrader_status():
    """读取当前 xttrader 通道状态：miniQMT/IPC/RPC 三选一。"""
    ipc_enabled = _read_env_key("ENABLE_QMT_IPC_FALLBACK").lower() in ("true", "1", "yes", "on")
    rpc_enabled = _read_env_key("ENABLE_QMT_RPC_FALLBACK").lower() in ("true", "1", "yes", "on")
    if rpc_enabled:
        return "rpc"
    if ipc_enabled:
        return "ipc"
    return "miniqmt"


def _get_rpc_display_info():
    """获取 RPC 通道的只读显示信息（不下单、不ping）。"""
    transport = _read_env_key("QMT_RPC_TRANSPORT") or "redis"
    host = _read_env_key("QMT_RPC_REDIS_HOST") or "127.0.0.1"
    port = _read_env_key("QMT_RPC_REDIS_PORT") or "6379"
    db = _read_env_key("QMT_RPC_REDIS_DB") or "5"
    pwd = _read_env_key("QMT_RPC_REDIS_PASSWORD")
    allow_order = _read_env_key("QMT_RPC_ALLOW_ORDER").lower() in ("true", "1", "yes", "on")
    return transport, host, port, db, pwd, allow_order


def _print_xttrader_status():
    """在主菜单底部打印当前 xttrader 通道简述。"""
    mode = _read_xttrader_status()
    label = {"miniqmt": "✓ miniQMT (xttrader 直连)", "ipc": "✓ 大QMT 文件IPC", "rpc": "✓ 大QMT RPC"}[mode]
    print(f"  XtTrader 通道: {label}")
    if mode == "rpc":
        transport, host, port, db, pwd, allow_order = _get_rpc_display_info()
        masked_pwd = ("<%d字符>" % len(pwd)) if pwd else "<空>"
        order_tag = "+下单" if allow_order else "只读"
        print(f"    RPC: {transport}://{host}:{port}/db{db}  密码={masked_pwd}  {order_tag}")
    elif mode == "ipc":
        ipc_root = _read_env_key("QMT_IPC_ROOT") or "C:\\QuantIPC"
        print(f"    IPC: {ipc_root}")


def cmd_xttrader_config(_args) -> int:
    """XtTrader 通道总控子菜单：miniQMT / IPC-Trader / RPC-Trader 三选一统一管理。"""
    import webbrowser

    def _status():
        mode = _read_xttrader_status()
        print("\n" + "=" * 56)
        print("  XtTrader 交易通道 — 当前配置")
        print("=" * 56)
        for key, desc, mode_key in [
            ("miniqmt", "miniQMT xttrader 直连（默认）", "miniqmt"),
            ("ipc", "大QMT 文件 IPC Trader", "ipc"),
            ("rpc", "大QMT RPC Trader（Redis/ZMQ）", "rpc"),
        ]:
            tag = " <<< 当前使用" if mode == mode_key else ""
            print(f"  {'✓' if mode == mode_key else ' '} {desc}{tag}")
        print("-" * 56)
        if mode == "rpc":
            transport, host, port, db, pwd, allow_order = _get_rpc_display_info()
            masked_pwd = ("<%d字符>" % len(pwd)) if pwd else "<空>"
            order_tag = "+下单" if allow_order else "只读"
            print(f"  RPC 详情: {transport}://{host}:{port}/db{db}")
            print(f"    密码: {masked_pwd}  |  下单: {order_tag}")
            print(f"    部署文档: docs/site/miniqmt/qmt-rpc-redis-setup.md")
        elif mode == "ipc":
            ipc_root = _read_env_key("QMT_IPC_ROOT") or "C:\\QuantIPC"
            print(f"  IPC 目录: {ipc_root}")
            print(f"    部署手册: qmt-trader/部署手册.md")
        else:
            print(f"  miniQMT xttrader 直连 (在 xtquant 可用时无需额外配置)")
        print("-" * 56)

    while True:
        _status()
        print("  [1] 切换到 miniQMT xttrader 直连（关闭 IPC 和 RPC）")
        print("  [2] 切换到大QMT 文件 IPC Trader")
        print("  [3] 切换到大QMT RPC Trader")
        print("  [4] RPC 连接配置 (host/port/db/password)")
        print("  [5] RPC 下单开关 (当前只读安全模式, 联调完成后再开)")
        print("  [6] 打开 RPC Redis 部署文档")
        print("  [0] 返回主菜单")
        print()
        c = input("请选择 [0-6]: ").strip()

        if c == "0":
            return 0

        elif c == "1":
            _write_env_key("ENABLE_QMT_IPC_FALLBACK", "false")
            _write_env_key("ENABLE_QMT_RPC_FALLBACK", "false")
            print("\n  ✓ 已切换到 miniQMT xttrader 直连")
            print("  ⚠ 重启 miniQMT 后生效")
            input("\n按回车键继续...")

        elif c == "2":
            # 先关掉 RPC（互斥）
            _write_env_key("ENABLE_QMT_RPC_FALLBACK", "false")
            _write_env_key("ENABLE_QMT_IPC_FALLBACK", "true")
            print("\n  ✓ 已切换到大QMT 文件 IPC Trader")
            print("  ℹ 订单将通过文件 IPC 路由到大QMT executor 执行")
            ipc_root = _read_env_key("QMT_IPC_ROOT") or "C:\\QuantIPC"
            print(f"  ℹ IPC 目录: {ipc_root}")
            print("  ⚠ 重启 miniQMT 后生效")
            input("\n按回车键继续...")

        elif c == "3":
            # 先关掉 IPC（互斥）
            _write_env_key("ENABLE_QMT_IPC_FALLBACK", "false")
            _write_env_key("ENABLE_QMT_RPC_FALLBACK", "true")
            print("\n  ✓ 已切换到大QMT RPC Trader")
            print("  ℹ 订单将通过 Redis RPC 路由到大QMT执行")
            print("  ℹ 默认连接: redis://127.0.0.1:6379/db5")
            print("  ℹ 下单默认关闭 (只读安全), 用菜单 [5] 可开启")
            print("  ⚠ 重启 miniQMT 后生效；无需 xtquant 连接")
            input("\n按回车键继续...")

        elif c == "4":
            print("\n—— RPC Redis 连接配置 ——\n")
            for key, label, default in [
                ("QMT_RPC_REDIS_HOST", "Redis 主机地址", "127.0.0.1"),
                ("QMT_RPC_REDIS_PORT", "Redis 端口", "6379"),
                ("QMT_RPC_REDIS_DB", "Redis 库号", "5"),
            ]:
                current = _read_env_key(key) or default
                val = input(f"  {label} (当前={current}, 留空=不变): ").strip()
                if val:
                    _write_env_key(key, val)
            print("\n  —— Redis 密码 ——")
            current_pwd = _read_env_key("QMT_RPC_REDIS_PASSWORD")
            print(f"  当前密码: {'<空>' if not current_pwd else ('<' + str(len(current_pwd)) + '字符>')}")
            val = input("  新密码 (留空=不变, 输入 - 清空): ").strip()
            if val == "-":
                _write_env_key("QMT_RPC_REDIS_PASSWORD", "")
            elif val:
                _write_env_key("QMT_RPC_REDIS_PASSWORD", val)
            print("\n  ✓ Redis 连接配置已更新")
            print("  ⚠ 重启 miniQMT 后生效")
            input("\n按回车键继续...")

        elif c == "5":
            current = _read_env_key("QMT_RPC_ALLOW_ORDER").lower() in ("true", "1", "yes", "on")
            new_state = not current
            _write_env_key("QMT_RPC_ALLOW_ORDER", "true" if new_state else "false")
            print(f"\n  ✓ QMT_RPC_ALLOW_ORDER → {'true (允许下单)' if new_state else 'false (只读安全)'}")
            if new_state:
                print("  ⚠ 安全提醒:")
                print("     - 确保大QMT端 bigqmt_signal_trader_local_config.py 的")
                print("       rpc_allow_order_methods 也已同步改为 True")
                print("     - 建议先用模拟盘小额验证下单闭环再投入实盘")
            print("  ⚠ 重启 miniQMT 后生效")
            input("\n按回车键继续...")

        elif c == "6":
            doc_path = PROJECT_ROOT / "docs" / "site" / "miniqmt" / "qmt-rpc-redis-setup.md"
            if doc_path.exists():
                webbrowser.open(str(doc_path))
                print("\n  已打开 RPC Redis 部署文档...")
            else:
                print("\n  ✗ 文档未找到")
            time.sleep(1)

        else:
            print(f"\n  无效选择: {c!r}")
            time.sleep(1)


# ---------------------------------------------------------------------------
# 主菜单
# ---------------------------------------------------------------------------
def cmd_menu(_args) -> int:
    """交互式中文菜单循环（由 miniqmt.bat 启动）。

    所有中文 UI 在 Python 里渲染，console 已经被 miniqmt.bat 切到 UTF-8（chcp 65001）。
    Python 的 sys.stdout 也 reconfigure 为 utf-8，避免乱码。
    """
    SEPARATOR = "=" * 64
    DASH      = "-" * 64

    def pause_return():
        try:
            input("\n按回车键返回菜单...")
        except (KeyboardInterrupt, EOFError):
            pass

    def ask(prompt: str) -> str:
        try:
            return input(prompt).strip()
        except (KeyboardInterrupt, EOFError):
            return ""

    def _start_with_web_mode(simulation: bool, all_accounts: bool):
        """启动账号，先询问 Web 模式（记忆上次选择作为默认）。"""
        mode_label = "实盘" if not simulation else "模拟"
        scope_label = "所有账号" if all_accounts else "指定账号"
        pref = _load_web_mode_pref()
        mode_desc = "web1.0 (Flask :5000起, 仅本机)" if pref == "1" else "web2.0 (xtquant_manager :8888, 全网卡)"
        print(f"\n[启动{scope_label} - {mode_label}模式]\n")
        web2 = ask(f"Web 界面 [1=web1.0, 2=web2.0] (默认 {pref}={mode_desc}): ")
        if web2 == "":
            web2 = pref  # 使用记忆偏好
        else:
            web2 = "2" if web2 == "2" else "1"  # 非 2 一律当 1
        _save_web_mode_pref(web2 == "2")
        web2 = web2 == "2"
        print()
        _do_start(None, simulation=simulation, web2=web2)

    def _do_start(accounts_str: str | None, simulation: bool, web2: bool):
        """实际启动逻辑。web2 模式下先启动 xtquant_manager。"""
        if web2:
            print("—— 步骤 1/2: 启动 xtquant_manager (web2.0 界面) ——")
            # 如果已经在运行就跳过启动
            if not _xqm_is_port_in_use(XQM_DEFAULT_PORT):
                cmd_xqm_start(argparse.Namespace())
            else:
                if _xqm_health_check(XQM_CLIENT_HOST, XQM_DEFAULT_PORT):
                    print("  ✓ xtquant_manager 已在运行")
                else:
                    print("  ⚠ xtquant_manager 端口被占用但健康检查失败，尝试重启...")
                    cmd_xqm_stop(argparse.Namespace())
                    time.sleep(1)
                    cmd_xqm_start(argparse.Namespace())
            print()
            print("—— 步骤 2/2: 启动交易策略 (main.py) ——")

        cmd_start(argparse.Namespace(accounts=accounts_str, simulation=simulation, web2=web2))

        if web2:
            print()
            print("=" * 48)
            print(f"  web2.0 访问地址: {_xqm_access_urls(XQM_DEFAULT_HOST, XQM_DEFAULT_PORT)}")
            print(f"  API 文档:        http://{XQM_CLIENT_HOST}:{XQM_DEFAULT_PORT}/docs")
            print("=" * 48)

        pause_return()

    while True:
        os.system("cls" if sys.platform == "win32" else "clear")
        print(SEPARATOR)
        print("                  miniQMT 总控制台")
        print(SEPARATOR)
        print(f"  工作目录 : {PROJECT_ROOT}")
        print(f"  Python   : {sys.executable}")
        print(DASH)
        print("  [首次部署 / 环境]")
        print("   [0] 首次部署向导（新电脑推荐先运行）")
        print("   [1] 检查 Python 环境与核心依赖")
        print("   [2] 安装/更新 Python 依赖 (pip install -r utils/requirements.txt)")
        print("   [3] 检查配置文件 (account_config.json, qmt_path)")
        print("   [4] 拉取最新代码 (git pull)")
        print()
        print("  [日常运行 - 查看]")
        print("   [5] 查看所有账号配置")
        print("   [6] 查看运行状态")
        print()
        print("  [日常运行 - 启动]")
        print("   [7] 启动所有账号 (实盘，启动时选择 web1.0/web2.0)")
        print("   [8] 启动所有账号 (模拟，启动时选择 web1.0/web2.0)")
        print("   [9] 启动指定账号 (选择实盘/模拟 + web1.0/web2.0)")
        print("        web1.0 = Flask :5000 起, 仅本机访问 (配置/监控用)")
        print("        web2.0 = xtquant_manager :8888, 全网卡 (只读查询/对外)")
        print()
        print("  [日常运行 - 停止]")
        print("   [a] 停止所有账号 (优雅, 30s 超时)")
        print("   [b] 停止指定账号 (优雅)")
        print("   [c] 强制停止所有账号 (立即 taskkill)")
        print()
        print("  [XtQuantManager 网关]")
        print("   [d] 启动 xtquant_manager 服务")
        print("   [e] 停止 xtquant_manager 服务")
        print("   [f] 查看 xtquant_manager 状态")
        print("   [g] 打开 web2.0 UI")
        print("   [h] 重启 xtquant_manager 服务")
        print("   [i] 查看 xtquant_manager 实时日志")
        print()
        print("  [自动买入服务 miniqmt_autobuy]")
        print("   [j] 启动自动买入服务")
        print("   [k] 停止自动买入服务")
        print("   [l] 查看自动买入状态")
        print("   [m] 查看自动买入日志")
        print()
        print("  [数据源 & 交易通道配置]")
        print("   [n] Tushare Pro 数据源配置")
        print("   [o] 大QMT IPC Trader 配置")
        print("   [p] XtTrader 通道总控 (miniQMT \ IPC-Trader \ RPC-Trader)")
        print()
        _print_xttrader_status()
        print(DASH)
        print("   [q] 退出")
        print(SEPARATOR)

        choice = ask("请选择 [0-9, a-q]: ").lower()

        if choice == "q":
            print("\n再见!")
            return 0

        # ---- 部署 / 环境 ----
        elif choice == "0":
            print()
            cmd_setup_wizard(None)
            pause_return()

        elif choice == "1":
            print()
            cmd_check_env(None)
            pause_return()

        elif choice == "2":
            print()
            cmd_install_deps(None)
            pause_return()

        elif choice == "3":
            print()
            cmd_check_config(None)
            pause_return()

        elif choice == "4":
            print()
            cmd_git_pull(None)
            pause_return()

        # ---- 查看 ----
        elif choice == "5":
            print()
            cmd_list(None)
            pause_return()

        elif choice == "6":
            print()
            cmd_status(None)
            pause_return()

        # ---- 启动 ----
        elif choice == "7":
            print()
            _start_with_web_mode(simulation=False, all_accounts=True)

        elif choice == "8":
            print()
            _start_with_web_mode(simulation=True, all_accounts=True)

        elif choice == "9":
            print()
            cmd_list(None)
            print()
            acc = ask("请输入要启动的账号 ID (多个用英文逗号分隔): ")
            if not acc:
                continue
            sim = ask("模式 [1=实盘, 2=模拟] (默认 1): ") == "2"
            pref = _load_web_mode_pref()
            mode_desc = "web1.0 (Flask)" if pref == "1" else "web2.0 (xtquant_manager)"
            web2_input = ask(f"Web 界面 [1=web1.0, 2=web2.0] (默认 {pref}={mode_desc}): ")
            web2 = (web2_input if web2_input else pref) == "2"
            _save_web_mode_pref(web2)
            print()
            _do_start(acc, simulation=sim, web2=web2)

        # ---- 停止 ----
        elif choice == "a":
            print()
            cmd_stop(argparse.Namespace(accounts=None, force=False, timeout=30))
            pause_return()

        elif choice == "b":
            print()
            cmd_status(None)
            print()
            acc = ask("请输入要停止的账号 ID (多个用英文逗号分隔): ")
            if not acc:
                continue
            print()
            cmd_stop(argparse.Namespace(accounts=acc, force=False, timeout=30))
            pause_return()

        elif choice == "c":
            print("\n[警告] 将强制结束所有账号进程 (含 xtquant_manager 网关)，未保存数据可能丢失！")
            confirm = ask("确认强制停止? [y/N]: ")
            if confirm.lower() == "y":
                print()
                # 先停交易策略 (main.py)，再停网关
                cmd_stop(argparse.Namespace(accounts=None, force=True, timeout=0))
                print()
                if _xqm_is_port_in_use(XQM_DEFAULT_PORT):
                    print("停止 xtquant_manager 网关...")
                    cmd_xqm_stop(argparse.Namespace())
            else:
                print("已取消。")
            pause_return()

        # ---- XtQuantManager 网关 ----
        elif choice == "d":
            print()
            cmd_xqm_start(None)
            pause_return()

        elif choice == "e":
            print()
            cmd_xqm_stop(None)
            pause_return()

        elif choice == "f":
            print()
            cmd_xqm_status(None)
            pause_return()

        elif choice == "g":
            print()
            cmd_xqm_ui(None)
            pause_return()

        elif choice == "h":
            print("\n[重启 xtquant_manager]\n")
            cmd_xqm_stop(None)
            # 等待端口完全释放（最多等 5 秒），确保新进程能绑定
            for _ in range(10):
                if not _xqm_is_port_in_use(XQM_DEFAULT_PORT):
                    break
                time.sleep(0.5)
            print()
            cmd_xqm_start(None)
            pause_return()

        elif choice == "i":
            print()
            cmd_xqm_logs(None)
            pause_return()

        # ---- 自动买入服务 ----
        elif choice == "j":
            print()
            cmd_autobuy_start(None)
            pause_return()

        elif choice == "k":
            print()
            cmd_autobuy_stop(None)
            pause_return()

        elif choice == "l":
            print()
            cmd_autobuy_status(None)
            pause_return()

        elif choice == "m":
            print()
            cmd_autobuy_logs(None)
            pause_return()

        # ---- 数据源 & 交易通道配置 ----
        elif choice == "n":
            print()
            cmd_tushare_config(None)
            pause_return()

        elif choice == "o":
            print()
            cmd_qmt_ipc_config(None)
            pause_return()

        elif choice == "p":
            print()
            cmd_xttrader_config(None)
            pause_return()

        else:
            print(f"\n[警告] 无效选择: {choice!r}")
            time.sleep(1)


def cmd_status(_args) -> int:
    accounts = load_accounts()
    print(f"{'账号 ID':<14} {'PID':<8} {'状态':<10} {'Web':<8} {'QMT 路径'}")
    print("-" * 78)
    for i, acc in enumerate(accounts):
        acc_id = acc["account_id"]
        pid = read_pid(acc_id)
        if pid is None:
            status, pid_str = "未运行", "-"
        elif pid_alive(pid):
            status, pid_str = "运行中", str(pid)
        else:
            status, pid_str = "PID失效", str(pid)
        print(f"{acc_id:<14} {pid_str:<8} {status:<10} :{5000+i:<6} {acc.get('qmt_path','')}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(prog="_launcher.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list")

    sp = sub.add_parser("start")
    sp.add_argument("--accounts", help="逗号分隔的账号 ID 列表，不指定则全部")
    sp.add_argument("--simulation", action="store_true",
                    help="强制以模拟模式启动")

    sp = sub.add_parser("stop")
    sp.add_argument("--accounts", help="逗号分隔的账号 ID 列表，不指定则全部")
    sp.add_argument("--force", action="store_true",
                    help="跳过优雅关闭，直接 taskkill")
    sp.add_argument("--timeout", type=int, default=30,
                    help="优雅关闭等待秒数（默认 30）")

    sub.add_parser("status")
    sub.add_parser("menu")
    sub.add_parser("setup-wizard")
    sub.add_parser("check-env")
    sub.add_parser("install-deps")
    sub.add_parser("check-config")
    sub.add_parser("git-pull")
    sub.add_parser("xqm-start")
    sub.add_parser("xqm-stop")
    sub.add_parser("xqm-status")
    sub.add_parser("xqm-ui")
    sub.add_parser("xqm-log")
    sub.add_parser("autobuy-start")
    sub.add_parser("autobuy-stop")
    sub.add_parser("autobuy-status")
    sub.add_parser("autobuy-log")
    sub.add_parser("autobuy-logs")

    args = parser.parse_args()
    return {
        "list":          cmd_list,
        "start":         cmd_start,
        "stop":          cmd_stop,
        "status":        cmd_status,
        "menu":          cmd_menu,
        "setup-wizard":  cmd_setup_wizard,
        "check-env":     cmd_check_env,
        "install-deps":  cmd_install_deps,
        "check-config":  cmd_check_config,
        "git-pull":      cmd_git_pull,
        "xqm-start":     cmd_xqm_start,
        "xqm-stop":      cmd_xqm_stop,
        "xqm-status":    cmd_xqm_status,
        "xqm-ui":        cmd_xqm_ui,
        "xqm-log":       cmd_xqm_logs,
        "autobuy-start":  cmd_autobuy_start,
        "autobuy-stop":   cmd_autobuy_stop,
        "autobuy-status": cmd_autobuy_status,
        "autobuy-log":    cmd_autobuy_logs,
        "autobuy-logs":   cmd_autobuy_logs,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main() or 0)
