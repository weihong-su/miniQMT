# 大QMT RPC — Redis 服务安装与配置

大QMT RPC 交易后端（`QmtRpcTrader`）依赖一个 **Redis 服务**作为 miniQMT 客户端与大QMT 策略进程之间的消息中转 broker。本文档讲**如何在 Windows 上安装并配置 Redis**；服务端/客户端的完整联调步骤见 [大QMT-RPC联调checklist.md](../../../qmt-trader/大QMT-RPC联调checklist.md)。

## Redis 在方案中的角色

```text
┌─────────────────────────────────────────────┐
│              Windows 机器                     │
│                                               │
│  miniQMT (python39)      大QMT 策略进程        │
│   QmtRpcTrader ──┐      ┌── redis_rpc 服务端   │
│                  ▼      ▼                      │
│               Redis (Memurai)                 │
│               127.0.0.1:6379  db=5           │
└─────────────────────────────────────────────┘
```

- 两端连**同一个 Redis**，默认库 `db=5`。
- 请求走 `bigqmt:rpc:req:{account_id}`，响应走 `bigqmt:rpc:resp:{account_id}:{request_id}`。
- 成交/委托实时推送走 `bigqmt:order_events:{account_id}` / `bigqmt:trade_events:{account_id}`。

## 安装方案选择

Windows 没有官方原生 Redis，三种可选方案：

| 方案 | 适用 | 说明 |
|------|------|------|
| **Memurai**（推荐） | 生产/长期运行 | Windows 原生 Redis 兼容服务，装成 Windows 服务开机自启，稳定。Developer 版免费 |
| WSL2 跑官方 Redis | 开发验证 | 版本最新、免费，但要处理 WSL 网络转发，同机访问需 `localhost` 端口映射 |
| Docker Desktop | 已用 Docker 的环境 | `docker run` 起官方镜像，隔离干净，但依赖 Docker Desktop 常驻 |

下面以 **Memurai + 同机部署**为主线（跨机见文末补充）。

## 第 1 步：安装 Memurai

1. 下载 **Memurai Developer**（免费版足够联调与开发）：<https://www.memurai.com/get-memurai>
2. 运行 MSI 全默认安装。完成后 Memurai **自动注册为 Windows 服务**并开机自启，默认监听 `127.0.0.1:6379`。
3. 确认服务运行（PowerShell）：

```powershell
Get-Service Memurai   # STATUS 应为 Running
```

关键路径：

- 配置文件：`C:\Program Files\Memurai\memurai.conf`
- CLI 工具：`C:\Program Files\Memurai\memurai-cli.exe`

## 第 2 步：设置密码

QMT 涉及真实资金，即便同机回环也**必须设密码**，防止同机其他进程误连或恶意连接。

1. 生成强密码（PowerShell）：

```powershell
[Convert]::ToBase64String((1..24 | ForEach-Object { Get-Random -Max 256 }))
# 复制输出，下文用 <YOUR_REDIS_PASSWORD> 代替
```

2. 用管理员权限编辑 `C:\Program Files\Memurai\memurai.conf`，确认/追加：

```conf
bind 127.0.0.1
requirepass <YOUR_REDIS_PASSWORD>
```

3. 重启服务生效：

```powershell
Restart-Service Memurai
```

> 密码只写在 `memurai.conf`（本地文件）和环境变量里，**切勿硬编码进项目代码或提交到 git**。

## 第 3 步：验证 Redis（L1 连通性）

```powershell
& "C:\Program Files\Memurai\memurai-cli.exe" -a "<YOUR_REDIS_PASSWORD>" PING
# 期望：PONG

& "C:\Program Files\Memurai\memurai-cli.exe" -a "<YOUR_REDIS_PASSWORD>" -n 5 PING
# 期望：PONG（确认 db 5 可用）
```

出现 `PONG` 说明 Redis 层就绪。

## 第 4 步：配置 miniQMT 客户端环境变量

miniQMT 读取配置的优先级为 **Windows 环境变量 > 项目根 `.env`**（[config.py](../../../config.py) 的 `_load_dotenv_fallback` 在 import 时把 `.env` 补充进未设置的键，已存在的环境变量不覆盖）。因此两种方式都可用：

**方式 A：`setx` 用户级环境变量（推荐，最高优先级）**

```powershell
setx ENABLE_QMT_RPC_FALLBACK true
setx QMT_RPC_TRANSPORT redis
setx QMT_RPC_REDIS_HOST 127.0.0.1
setx QMT_RPC_REDIS_PORT 6379
setx QMT_RPC_REDIS_DB 5
setx QMT_RPC_REDIS_PASSWORD "<YOUR_REDIS_PASSWORD>"
setx QMT_RPC_ALLOW_ORDER false
```

`setx` 只对**之后新开**的终端/进程生效，设完后**新开 PowerShell** 再启动 miniQMT。

**方式 B：项目根 `.env` 文件（fallback，方便集中管理）**

在 `c:\github-repo\miniQMT\.env` 写入（格式 `KEY=value`，`#` 为注释）：

```ini
ENABLE_QMT_RPC_FALLBACK=true
QMT_RPC_TRANSPORT=redis
QMT_RPC_REDIS_HOST=127.0.0.1
QMT_RPC_REDIS_PORT=6379
QMT_RPC_REDIS_DB=5
QMT_RPC_REDIS_PASSWORD=<YOUR_REDIS_PASSWORD>
QMT_RPC_ALLOW_ORDER=false
```

> `.env` 已在 `.gitignore` 中排除，密码写这里不会提交。若某个键**同时**存在于环境变量和 `.env`，**环境变量优先**。

注意：

- `ENABLE_QMT_RPC_FALLBACK=true` 与 `ENABLE_XTQUANT_MANAGER` / `ENABLE_QMT_IPC_FALLBACK` **三者互斥**（同开会抛 `ValueError`），确认另两个为 `false`。
- 联调初期保持 `QMT_RPC_ALLOW_ORDER=false`（只读安全），确认查询链路无误后再放开下单。

配置项与 [config.py](../../../config.py) 的对应关系：

| 环境变量 | config 字段 | 默认值 |
|---------|-------------|--------|
| `ENABLE_QMT_RPC_FALLBACK` | `ENABLE_QMT_RPC_FALLBACK` | `false` |
| `QMT_RPC_TRANSPORT` | `QMT_RPC_TRANSPORT` | `redis` |
| `QMT_RPC_REDIS_HOST` | `QMT_RPC_REDIS["host"]` | `127.0.0.1` |
| `QMT_RPC_REDIS_PORT` | `QMT_RPC_REDIS["port"]` | `6379` |
| `QMT_RPC_REDIS_DB` | `QMT_RPC_REDIS["db"]` | `5` |
| `QMT_RPC_REDIS_PASSWORD` | `QMT_RPC_REDIS["password"]` | 空 |
| `QMT_RPC_ALLOW_ORDER` | `QMT_RPC_ALLOW_ORDER` | `false` |

## 第 5 步：配置大QMT 服务端

大QMT 端的私有配置 `bigqmt_signal_trader_local_config.py` 里的 `BIGQMT_REDIS_CONFIG` 必须与客户端**完全一致**（`host`/`port`/`db`/`password`）：

```python
BIGQMT_REDIS_CONFIG = {
    "host": "127.0.0.1",
    "port": 6379,
    "db": 5,                              # 必须与 QMT_RPC_REDIS_DB 一致
    "username": "",
    "password": "<YOUR_REDIS_PASSWORD>",  # 与客户端一致
    "rpc_allow_order_methods": False,     # 联调初期只读
    "exec_events_enabled": True,          # 成交/委托推送
    "rpc_process_in_listener": True,
    "rpc_listener_methods": ("*",),
    "rpc_background_threads": False,
    "schedule_adjust": True,
    "schedule_adjust_interval": "500nMilliSecond",
}
```

完整的脚本拷贝、策略编辑器入口、启动标志见 [大QMT-RPC联调checklist.md](../../../qmt-trader/大QMT-RPC联调checklist.md) 第一节。

## 第 6 步：端到端验证（L2 RPC 链路）

大QMT 策略跑起来后，在 miniQMT 机器上跑只读探活：

```powershell
& "C:\Users\PC\Anaconda3\envs\python39\python.exe" -c @'
import sys; sys.path.insert(0, "qmt-trader")
from qmt_rpc_trader import QmtRpcTrader
t = QmtRpcTrader(account="你的资金账号")
print("ping:", t.ping_xttrader())      # True = RPC 服务端在线
print("health:", t.get_rpc_health())
print("asset:", t.query_stock_asset())
print("positions:\n", t.position())
'@
```

判读：

- `ping: True` → Redis + 大QMT 策略双向通了。
- `asset` / `positions` 返回真实数据 → 只读链路通过。
- `ping: False` → 检查大QMT 策略是否在跑、两端密码/`db` 是否一致。

## 跨机部署补充

若 miniQMT 与大QMT 不在同一台机器，Redis 装在其中一台（或第三台），需额外：

1. **绑定内网 IP**：`memurai.conf` 改 `bind 0.0.0.0`（或指定内网网卡 IP），并**务必**保留 `requirepass`。
2. **关闭保护模式**（设了密码才安全）：`protected-mode no`。
3. **开放防火墙 6379**（仅对内网段）：

```powershell
New-NetFirewallRule -DisplayName "Redis 6379 LAN" -Direction Inbound `
  -Protocol TCP -LocalPort 6379 -RemoteAddress <内网网段/24> -Action Allow
```

4. 客户端与服务端的 `host` 改成 Redis 机器的**内网 IP**。
5. 强烈建议 Redis 只暴露在内网，**不要**绑公网 IP；跨机延迟略高（redis 传输同机 p50 ~12ms，跨机取决于网络）。

## 排障（Redis 层）

| 现象 | 原因 | 排查 |
|------|------|------|
| `memurai-cli PING` 无 `PONG` | 服务没起 | `Get-Service Memurai`；`Restart-Service Memurai` |
| `NOAUTH Authentication required` | 未带密码 | CLI 加 `-a <密码>`；客户端确认 `QMT_RPC_REDIS_PASSWORD` |
| `WRONGPASS` | 密码不一致 | 核对 `memurai.conf`、客户端环境变量、大QMT `local_config` 三处一致 |
| `ping: False`（RPC 层） | 大QMT 策略没跑 / `db` 不一致 | 查大QMT 面板 `[bigqmt_rpc] started`；`memurai-cli -n 5 ping` |
| 跨机连不上 | `bind`/防火墙 | 确认 `bind 0.0.0.0`+`protected-mode no`+防火墙放行；`Test-NetConnection <IP> -Port 6379` |
| miniQMT 读不到环境变量 | `setx` 后没重开终端 | 新开 PowerShell 再启动；或用系统属性确认变量已写入 |

## 关联文档

- [大QMT-RPC联调checklist.md](../../../qmt-trader/大QMT-RPC联调checklist.md) — 完整 P5 联调步骤（服务端部署/下单闭环/回滚）
- [大QMT-RPC方案.md](../../../qmt-trader/大QMT-RPC方案.md) — 方案设计与架构
- [configuration.md](configuration.md) — miniQMT 配置全景图
- [qmt-ipc-fallback.md](qmt-ipc-fallback.md) — 另一种降级通道（文件 IPC）
