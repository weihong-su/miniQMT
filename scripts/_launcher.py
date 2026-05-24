"""
miniQMT 总控制台后端（被 miniqmt.bat 调用）。

子命令:
  menu                       交互式菜单（默认入口）
  list                       列出 account_config.json 中所有账号
  status                     查看每个账号的进程运行状态
  start [--accounts a,b] [--simulation]   启动账号（默认全部）
  stop  [--accounts a,b] [--force] [--timeout N]   停止账号
  check-env                  检查 Python 版本与核心依赖
  install-deps               运行 pip install -r utils/requirements.txt
  check-config               校验 account_config.json 的合法性与 qmt_path 存在性
  git-pull                   拉取最新代码
  xqm-start                  启动 xtquant_manager 网关
  xqm-stop                   停止 xtquant_manager 网关
  xqm-status                 查看 xtquant_manager 运行状态
  xqm-ui                     在浏览器打开 web2.0/1.0 界面

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
MAIN_PY      = PROJECT_ROOT / "main.py"


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

    print(f"\n共启动 {started}/{len(accounts)} 个账号。Web 端口从 5000 开始按账号顺序分配。")
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
    "baostock", "marshmallow", "requests", "colorama",
]
# 需要特殊处理的依赖：通过 QMT 客户端自带的 SDK 安装
SPECIAL_DEPS = ["xtquant"]


def check_python_env() -> dict:
    """返回 Python 版本与核心依赖检查结果。

    Returns:
        {
          "python": "3.9.x",
          "executable": "...",
          "missing": ["pandas", ...],          # pip 可装的缺失模块
          "special_missing": ["xtquant", ...], # 需特殊安装的缺失模块
        }
    """
    import importlib
    info = {
        "python": ".".join(map(str, sys.version_info[:3])),
        "executable": sys.executable,
        "missing": [],
        "special_missing": [],
    }
    for mod in CORE_DEPS:
        try:
            importlib.import_module(mod)
        except Exception:
            info["missing"].append(mod)
    for mod in SPECIAL_DEPS:
        try:
            importlib.import_module(mod)
        except Exception:
            info["special_missing"].append(mod)
    return info


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
    return 0 if not info["missing"] and not info["special_missing"] else 1


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
XQM_DEFAULT_HOST = "127.0.0.1"
XQM_DEFAULT_PORT = 8888
XQM_MODULE = "xtquant_manager"


def _xqm_config_path() -> Path:
    local = PROJECT_ROOT / "xtquant_manager_config.json"
    if local.exists():
        return local
    return PROJECT_ROOT / "xtquant_manager" / "standalone_config.py"


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


def _xqm_health_check(host: str = XQM_DEFAULT_HOST, port: int = XQM_DEFAULT_PORT) -> bool:
    import urllib.request
    try:
        req = urllib.request.Request(f"http://{host}:{port}/api/v1/health", method="GET")
        resp = urllib.request.urlopen(req, timeout=3)
        return resp.status == 200
    except Exception:
        return False


def cmd_xqm_start(_args) -> int:
    host = os.environ.get("XQM_HOST", XQM_DEFAULT_HOST)
    port = int(os.environ.get("XQM_PORT", str(XQM_DEFAULT_PORT)))

    if _xqm_is_port_in_use(port):
        if _xqm_health_check(host, port):
            print(f"  ✓ xtquant_manager 已在运行: http://{host}:{port}")
            return 0
        else:
            print(f"  ⚠ 端口 {port} 被占用但健康检查失败，尝试清理...")
            pid = _xqm_read_pid()
            if pid and pid_alive(pid):
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
                time.sleep(1)

    config_path = _xqm_config_path()
    config_arg = f"--config {config_path}" if config_path.name == "xtquant_manager_config.json" else ""

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
        if _xqm_health_check(host, port):
            print(f"  ✓ xtquant_manager 已启动: http://{host}:{port}")
            return 0
    print(f"  ⚠ 服务已启动但健康检查超时，稍后请访问 http://{host}:{port}/api/v1/health 确认")
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
    healthy = _xqm_health_check(host, port)

    if pid:
        print(f"  PID      : {pid} {'(存活)' if pid_alive(pid) else '(已失效)'}")
    else:
        print("  PID      : (未记录)")
    print(f"  端口     : {port} {'(监听中)' if port_used else '(空闲)'}")
    print(f"  健康检查 : {'✓ 通过' if healthy else ('✗ 失败' if port_used else '—')}")
    print(f"  地址     : http://{host}:{port}")

    if healthy:
        import urllib.request, json
        try:
            req = urllib.request.Request(f"http://{host}:{port}/api/v1/health")
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

    # 优先用 xtquant_manager 托管的 HTTP 服务打开
    if _xqm_health_check(host, port):
        url = f"http://{host}:{port}/"
        print(f"  打开 web2.0: {url}")
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

    print(f"  提示: 用 miniqmt.bat 菜单 [d] 启动 xtquant_manager 后，访问 http://{host}:{port}/")
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
        """启动账号，先询问 Web 模式。"""
        mode_label = "实盘" if not simulation else "模拟"
        scope_label = "所有账号" if all_accounts else "指定账号"
        print(f"\n[启动{scope_label} - {mode_label}模式]\n")
        web2 = ask("Web 界面 [1=web1.0 (Flask :5000起), 2=web2.0 (xtquant_manager :8888)] (默认 1): ") == "2"
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
                if _xqm_health_check(XQM_DEFAULT_HOST, XQM_DEFAULT_PORT):
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
            print(f"  web2.0 访问地址: http://{XQM_DEFAULT_HOST}:{XQM_DEFAULT_PORT}")
            print(f"  API 文档:        http://{XQM_DEFAULT_HOST}:{XQM_DEFAULT_PORT}/docs")
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
        print(DASH)
        print("   [0] 退出")
        print(SEPARATOR)

        choice = ask("请选择 [0-9, a-i]: ").lower()

        if choice == "0":
            print("\n再见!")
            return 0

        # ---- 部署 / 环境 ----
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
            web2 = ask("Web 界面 [1=web1.0 (Flask), 2=web2.0 (xtquant_manager)] (默认 1): ") == "2"
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
            print("\n[警告] 将强制结束所有账号进程，未保存数据可能丢失！")
            confirm = ask("确认强制停止? [y/N]: ")
            if confirm.lower() == "y":
                print()
                cmd_stop(argparse.Namespace(accounts=None, force=True, timeout=0))
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
            time.sleep(2)
            cmd_xqm_start(None)
            pause_return()

        elif choice == "i":
            print()
            cmd_xqm_logs(None)
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
    sub.add_parser("check-env")
    sub.add_parser("install-deps")
    sub.add_parser("check-config")
    sub.add_parser("git-pull")
    sub.add_parser("xqm-start")
    sub.add_parser("xqm-stop")
    sub.add_parser("xqm-status")
    sub.add_parser("xqm-ui")
    sub.add_parser("xqm-log")

    args = parser.parse_args()
    return {
        "list":          cmd_list,
        "start":         cmd_start,
        "stop":          cmd_stop,
        "status":        cmd_status,
        "menu":          cmd_menu,
        "check-env":     cmd_check_env,
        "install-deps":  cmd_install_deps,
        "check-config":  cmd_check_config,
        "git-pull":      cmd_git_pull,
        "xqm-start":     cmd_xqm_start,
        "xqm-stop":      cmd_xqm_stop,
        "xqm-status":    cmd_xqm_status,
        "xqm-ui":        cmd_xqm_ui,
        "xqm-log":       cmd_xqm_logs,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main() or 0)
