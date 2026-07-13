# 大QMT RPC 联调 Checklist（P5）

> 代码层（P0–P4）已就绪。本清单指导在**装有大QMT的机器**上完成真实下单闭环验证。
> 全程建议用**模拟盘或极小额（1 手）**，逐项打勾。配套方案：`大QMT-RPC方案.md`。

---

## 前置条件

- [ ] 一台可访问的 **Redis** 服务（大QMT端与 miniQMT 端都能连通；同机用 `127.0.0.1` 即可）
- [ ] 大QMT客户端已登录，可正常手动下单
- [ ] miniQMT 端已 `pip install redis`（`utils/requirements.txt` 已含）
- [ ] 确认三个交易开关**当前只会开一个**：`ENABLE_XTQUANT_MANAGER` / `ENABLE_QMT_IPC_FALLBACK` / `ENABLE_QMT_RPC_FALLBACK`（同开会被工厂抛异常拦截）

---

## 一、大QMT 端（服务端）部署

### 1.1 拷贝脚本到大QMT的 python 目录

把 vendored 的 `src/` 下内容拷到大QMT的 `python` 目录（如 `D:\国金证券QMT交易端\python\`）：

- [ ] 整个 `bigqmt_signal_trader/` 包（含 `adapters/`）
- [ ] `bigqmt_signal_trader_redis_rpc_runtime.py`
- [ ] `BIGQMT_REDIS_DRYRUN.py`（★ QMT 编辑器入口，GBK 编码）

> 源路径：`qmt-trader/vender/bigqmt/src/`

### 1.2 创建私有配置（不提交）

在大QMT的 `python` 目录新建 `bigqmt_signal_trader_local_config.py`（参考 `bigqmt_signal_trader_local_config.example.py`）：

```python
# coding: utf-8
BIGQMT_ACCOUNT_ID = "你的资金账号"          # 与 miniQMT account_config.json 的 account_id 一致

BIGQMT_REDIS_CONFIG = {
    "host": "127.0.0.1",                   # miniQMT 端能连到的 Redis 地址
    "port": 6379,
    "db": 5,                               # ★ 必须与 miniQMT 端 QMT_RPC_REDIS_DB 一致
    "username": "",
    "password": "你的Redis密码",            # 无密码留空

    "rpc_allow_order_methods": True,       # ★★ 联调下单必须改 True（默认 False 拒绝下单）
    "exec_events_enabled": True,           # ★ 启用成交/委托 Redis 推送（miniQMT 端回调依赖）

    "rpc_process_in_listener": True,
    "rpc_listener_methods": ("*",),
    "rpc_background_threads": False,        # redis 传输保持 False；切 zmq/mysql 才改 True
    "schedule_adjust": True,
    "schedule_adjust_interval": "500nMilliSecond",
}
```

- [ ] `BIGQMT_ACCOUNT_ID` 与 miniQMT `account_config.json` 的 `account_id` **完全一致**
- [ ] `db` 与 miniQMT `QMT_RPC_REDIS_DB` **一致**
- [ ] `rpc_allow_order_methods = True`
- [ ] `exec_events_enabled = True`

### 1.3 在大QMT策略编辑器运行

- [ ] 模型交易/模型研究 → 新建 Python 策略 → **只加载 `BIGQMT_REDIS_DRYRUN.py`**
- [ ] 运行模式选"实盘"，编译保存运行
- [ ] 输出面板出现以下**启动成功标志**：

```
[bigqmt_shell] reload entry paths=['D:\\...\\python']
[bigqmt_shell] local redis config loaded keys=['db','exec_events_enabled','host',...]
[bigqmt_shell] local account config loaded=True
[bigqmt_rpc] started channel=bigqmt:rpc:req:你的账号 queue=...
```

---

## 二、miniQMT 端（客户端）配置

### 2.1 环境变量清单

在 miniQMT 启动前设置（`.env` 或启动脚本；密码等敏感项**只走环境变量**）：

| 环境变量 | 联调值 | 说明 |
|---------|--------|------|
| `ENABLE_QMT_RPC_FALLBACK` | `true` | 启用 RPC 交易后端 |
| `QMT_RPC_ALLOW_ORDER` | `true` | ★ 放开真实下单（默认 false 为只读安全） |
| `QMT_RPC_TRANSPORT` | `redis` | 传输方式（联调用默认 redis） |
| `QMT_RPC_REDIS_HOST` | `127.0.0.1` | Redis 地址（与大QMT端一致） |
| `QMT_RPC_REDIS_PORT` | `6379` | Redis 端口 |
| `QMT_RPC_REDIS_DB` | `5` | ★ 与大QMT端 `db` 一致 |
| `QMT_RPC_REDIS_PASSWORD` | `你的Redis密码` | 无密码留空 |
| `ENABLE_SIMULATION_MODE` | `false` | ★ 必须关模拟模式，否则 pm 跳过 QMT 连接 |

Windows CMD 示例：
```bat
set ENABLE_QMT_RPC_FALLBACK=true
set QMT_RPC_ALLOW_ORDER=true
set QMT_RPC_REDIS_HOST=127.0.0.1
set QMT_RPC_REDIS_DB=5
set ENABLE_SIMULATION_MODE=false
```

- [ ] `account_config.json` 的 `account_id` 与大QMT端 `BIGQMT_ACCOUNT_ID` 一致
- [ ] 确认 `ENABLE_XTQUANT_MANAGER=false` 且 `ENABLE_QMT_IPC_FALLBACK=false`

### 2.2 连通性快速自检（不下单，先跑）

在 miniQMT 项目根目录跑（无需启动整个系统）：

```bash
"/c/Users/PC/Anaconda3/envs/python39/python.exe" -c "
import sys, os
sys.path.insert(0, 'qmt-trader')
os.environ['ENABLE_QMT_RPC_FALLBACK']='true'
from qmt_rpc_trader import QmtRpcTrader
t = QmtRpcTrader(account='你的账号')
print('ping:', t.ping_xttrader())        # 期望 True
print('health:', t.get_rpc_health())
print('positions:')
print(t.position())                      # 期望返回真实持仓 DataFrame
print('asset:', t.query_stock_asset())   # 期望返回真实资产
"
```

- [ ] `ping: True`
- [ ] `position()` 返回真实持仓（证券代码/股票余额/可用余额/成本价/市值 五列齐全）
- [ ] `query_stock_asset()` 总资产/可用金额与大QMT一致

---

## 三、下单闭环验证（模拟盘/1 手）

- [ ] 启动 miniQMT：`python main.py`
- [ ] 日志出现 `QmtRpcTrader 连接成功（大QMT RPC 在线）` 和 `[OK] QMT已连接`
- [ ] 通过 Web 模拟买入接口或直接调用下 **1 手**限价单
- [ ] 观察闭环：
  - [ ] miniQMT 日志：`RPC下单已提交: buy ... order_id=<纯整数>, 返回=bq:...`
  - [ ] 大QMT输出面板出现 passorder 记录，大QMT委托列表出现该笔委托
  - [ ] 成交后 miniQMT 日志出现成交回调（pending_orders 移除该 order_id）
  - [ ] `position()` 刷新出现新持仓
- [ ] 测试撤单：下一笔挂单后撤单，确认 `RPC撤单指令已提交` 且大QMT委托变"已撤"

---

## 四、排障要点

| 现象 | 可能原因 | 排查 |
|------|---------|------|
| `ping: False` / 连接失败 | 大QMT脚本没跑 / Redis 不通 / db 不一致 | 查大QMT输出面板有无 `[bigqmt_rpc] started channel`；确认两端 host/port/**db** 一致；`redis-cli -n 5 ping` |
| `RPC下单被拒: QMT_RPC_ALLOW_ORDER=False` | miniQMT 端没开下单开关 | 设 `QMT_RPC_ALLOW_ORDER=true` 重启 |
| 下单无反应 / passorder 未执行 | 大QMT端 `rpc_allow_order_methods=False` | 改 local_config 为 `True`，重新运行策略 |
| 下单成功但无成交回调 | 大QMT端 `exec_events_enabled` 未开 | 确认 local_config `exec_events_enabled=True`；回调失效时兜底轮询仍会在 ~1s 内补触发 |
| 账号 account_id 为空 / 持仓空 | 两端账号不一致 | `BIGQMT_ACCOUNT_ID` == `account_config.json.account_id` == miniQMT 进程账号 |
| 工厂启动即抛 `交易接口开关互斥` | 同时开了多个后端开关 | 只保留 `ENABLE_QMT_RPC_FALLBACK=true`，关掉另两个 |
| `import xtquant` 行为异常 | vendored `src/xtquant` shim 遮蔽真实包 | 适配器已 append 到 sys.path 末尾规避；勿手动把 vendored src 插到 path 前面 |
| order_id 无法撤单 (`未找到 order_sys_id`) | 委托回报尚未到达，sysid 未回填 | 稍等 1–2s（轮询回填）后再撤；已终态委托本就无法撤 |
| 撤单/下单卡顿 | Redis 跨机网络抖动 | 优先同机 Redis；`QMT_RPC_TIMEOUT_SECONDS` 可适当调大 |

---

## 五、回滚

联调不通或需回退，**关掉一个环境变量即可**恢复原行为，无需改代码：

```bat
set ENABLE_QMT_RPC_FALLBACK=false
```

→ 工厂回落到默认 `easy_qmt_trader`（xttrader 直连）或你原用的后端。
