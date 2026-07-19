# 大QMT RPC 方案 —— 基于 xtquant_big_convert 的交易后端与行情接入计划

> 在现有「大QMT文件IPC方案」之外，新增大QMT RPC 交易后端：复用开源库
> [xtquant_big_convert](https://github.com/litaolemo/xtquant_big_convert) 的 RPC 桥接，
> 把大QMT内置 Python 的交易能力通过 Redis/ZMQ/MySQL 远程暴露给 miniQMT。
>
> 与文件IPC解决同一问题（大QMT 无法用 XtQuantServer/xttrader 直连），但通道从
> 「文件轮询（~1s）」升级为「RPC（Redis ~13ms / ZMQ ~0.7ms，可跨机）」。
>
> **当前实现状态（2026-07-19）**：交易适配器 `QmtRpcTrader` 已落地并接入
> `position_manager._create_qmt_trader()`；行情 RPC 仍是第十一节的独立后续计划，
> 代码中尚未提供 `ENABLE_QMT_RPC_DATA_SOURCE`，`data_manager._create_xtdata()` 也未接入 RPC xtdata。

---

## 一、为什么要这个方案

miniQMT 现有四种交易通道，均通过 [position_manager.py](../position_manager.py) 的
`_create_qmt_trader()` 工厂按开关选择：

| 通道 | 开关 | 通道介质 | 行情能力 |
|------|------|---------|---------|
| xttrader 直连 | 默认 | 本机 xttrader | 实时 xtdata；历史标准模式避开本地 xtdata，走 Tushare/Mootdx |
| XtQuantManager | `ENABLE_XTQUANT_MANAGER` | HTTP 网关 | 通过 `XtDataAdapter` 访问网关 xtdata |
| 文件 IPC | `ENABLE_QMT_IPC_FALLBACK` | 文件系统 `C:\QuantIPC` | ❌ 不覆盖 |
| **RPC（本方案）** | **`ENABLE_QMT_RPC_FALLBACK`** | **Redis/ZMQ/MySQL** | **交易通道已落地；行情 RPC 尚未接入 miniQMT 数据链** |

**本方案的增量价值**：
1. **交易通道更快、可跨机**：Redis 13ms vs 文件轮询 1s；多账号可集中到一台 Redis。
2. **潜在行情能力**：外部库的 `xtdata` RPC 可把 `get_market_data_ex` 等调用隔离在大QMT进程内，
   客户端只收序列化结果，从根上绕开标准模式的 `xtdata` BSON abort 崩溃。
3. **接入缝干净**：上层 PositionManager / TradingExecutor / GridTradingManager **零改动**。

> 本方案分两个正交子项：**交易通道接入**（P1–P6，第二~十节）先行，
> **行情接入（xtdata RPC）**（D1–D4，第十一节）作为独立子项随后启动。
> 两者共用同一大QMT RPC 服务与传输配置，开关独立，可分别或同时开启。

---

## 二、总体架构

```
┌──────────────────────────┐   Redis/ZMQ/MySQL   ┌────────────────────────────┐
│  miniQMT 策略端            │ ◄═════════ RPC ════► │  大QMT内置Python            │
│  PositionManager          │                     │  BIGQMT_REDIS_DRYRUN.py    │
│    └ QmtRpcTrader ────────┼─ order/query 请求 ──┼─► redis_rpc handlers       │
│       (本方案新增适配器)    │                     │    └ passorder/cancel/     │
│       ↑ 契约同 easy_qmt    │ ◄─ 回执/快照/tick ──┼─   get_trade_detail_data  │
└──────────────────────────┘                     └────────────────────────────┘
   vendor/bigqmt/xtquant_compat                     券商大QMT自带 xttrader 授权
   (客户端兼容层，翻译成 RPC)                          (不依赖 miniQMT 权限)
```

**核心逻辑**：外部库把真实 API 调用放在大QMT内部策略进程（`BIGQMT_REDIS_DRYRUN.py`
在 QMT 策略编辑器加载运行），miniQMT 通过 RPC 远程驱动。`QmtRpcTrader` 是薄适配器，
把外部库的 `xt_trader`/`xtdata` 语义翻译成 miniQMT 上层期望的 `easy_qmt_trader` 契约。

---

## 三、契约锚点（QmtRpcTrader 必须实现的接口）

新适配器与现有 [qmt_ipc_trader.py](qmt_ipc_trader.py) 面对**完全相同的上层契约**。以下为硬性要求，
缺一即导致上层崩溃（依赖点已在 `qmt_ipc_trader.py` 注释中标注）：

| 分类 | 方法 / 属性 | 上层依赖点 |
|------|------------|-----------|
| 属性 | `.xt_trader`（含 `query_stock_orders/query_stock_order/cancel_order_stock`）、`.acc`、`.order_id_map` | position_manager 4 处直接访问 |
| 连接 | `connect()→(self,self)/None`、`ping_xttrader()→bool`、`reconnect_xttrader()→bool`、`stop()` | pm 初始化 + thread_monitor 心跳 |
| 回调 | `register_trade_callback` / `register_order_callback` / `register_disconnect_callback` | [position_manager.py:98-102](../position_manager.py#L98-L102) |
| 下单 | `buy` / `sell` / `order_stock` / `order_stock_async` → **纯整数 order_id 或 None** | 策略/网格下单；pm 会 `int(order_id)` |
| 撤单 | `cancel_order_stock` / `cancel_order_stock_async` → 0 / -1 | sell_monitor 撤单 |
| 持仓资产 | `position()` / `query_stock_positions()` → DataFrame（必含 `证券代码/股票余额/可用余额/成本价/市值`）、`balance()` / `query_stock_asset()` | `_sync_real_positions_to_memory` |
| 委托成交 | `query_stock_orders` / `query_stock_trades` / `today_entrusts` / `today_trades` / `get_active_orders_by_stock` / `get_active_order_info_by_stock` | sell_monitor、网格对账 |
| 纯逻辑工具 | `adjust_stock` / `select_data_type` / `select_slippage` / `check_stock_is_av_buy` / `check_stock_is_av_sell` | 直接复用 |

**实现取巧（优雅关键）**：纯逻辑工具、回调轮询框架、`_FakeXtTrader`/`_FakeAccount`/`_FakeXtObject`、
状态码映射 `_IPC_STATUS_TO_QMT` 等**与传输方式无关**。落地时抽出基类 `_QmtTraderBase`，
`QmtIpcTrader` 与 `QmtRpcTrader` 各自只实现「下单落地 / 查询落地」的差异部分，避免两份重复代码。

---

## 四、关键映射（RPC → easy_qmt_trader 契约）

| easy_qmt_trader 契约方法 | 外部库调用 | 备注 |
|--------------------------|-----------|------|
| `buy/sell/order_stock` | `ext.xt_trader.order_stock(acc, code, order_type, vol, price_type, price, remark=)` | 返回值处理见第五节 |
| `position()/query_stock_positions()` | `ext.xt_trader.query_stock_positions(acc)` | 组装必需 5 列 DataFrame（复用 IPC 版组装逻辑） |
| `balance()/query_stock_asset()` | `ext.xt_trader.query_stock_asset(acc)` | 映射 `可用/冻结/市值/总资产` |
| `query_stock_orders()` | `ext.xt_trader.query_stock_orders(acc)` | 转 `_FakeXtObject` → `_orders_to_df`（复用 IPC 版） |
| `query_stock_trades()` | `ext.xt_trader.query_stock_trades(acc)` | 同上 |
| `cancel_order_stock(order_id)` | `ext.xt_trader.cancel_order_stock(acc, order_id)` | 返回 0/-1 |
| `ping_xttrader()` | `ext` RPC `"ping"` 往返成功 | 心跳门禁 |
| 成交/委托回调 | **后台轮询线程** poll `query_orders`/`query_trades` 增量 | 外部库无 push，见第五节 |

---

## 五、⚠️ 三个必须先验证的技术风险（P0 已验证）

> **P0 Spike 结论（2026-07-13，Mock 源码级已定论）**：见各子节 ✅/❌ 标注。
> 结论来自 vendored 源码 [order_bigqmt.py:106-111](vender/bigqmt/src/bigqmt_signal_trader/adapters/order_bigqmt.py#L106-L111)
> 与 [xtquant_compat.py](vender/bigqmt/src/bigqmt_signal_trader/xtquant_compat.py) + 库自带 77 用例。

### 5.1 order_stock 是否同步返回可用 order_id（最高优先级）— ✅ 已证实需合成 id

大QMT 下单走 `passorder`，**确认 `passorder` 不同步返回订单号**：`BigQmtOrderGateway.submit()`
返回 `OrderSubmitResult(order_sys_id=None, user_order_id=<字符串>)`，compat `order_stock` 因而
返回 **字符串 user_order_id**（passorder 场景）或 order_sys_id（其它网关）。

→ **已落地方案**：`QmtRpcTrader._send()` 生成纯整数 `order_id` 返回上层，维护
`int_id ↔ 返回串 ↔ order_sys_id` 三向映射（`_reconcile()`），撤单/回调靠此配对。

### 5.2 无 push 回调，需自建后台轮询线程 — ❌ 假设被推翻，实为双通道

外部库**有 Redis pubsub 推送回调**（`BigQmtXtTrader.register_callback()` + `.start()`，
[xtquant_compat.py:1224-1276](vender/bigqmt/src/bigqmt_signal_trader/xtquant_compat.py#L1224)）。

→ **已落地方案**：`QmtRpcTrader` 同时用 **推送**（`_RpcCallback` 转发）+ **兜底轮询**
（`_poll_loop` 补偿 query_orders），`_seen_orders/_seen_deals` 去重防双触发。

### 5.3 下单开关默认关闭 — ✅ 已加双重门禁

外部库 `rpc_allow_order_methods` 默认 `False`（QMT 端需显式开启）；miniQMT 侧再加
`QMT_RPC_ALLOW_ORDER`（默认 False）：即使 `ENABLE_QMT_RPC_FALLBACK=True`，未开此开关时
`_send()` 拒绝真实下单，为只读安全模式。

---

## 六、需改动 / 新增的文件

| 文件 | 动作 | 内容 |
|------|------|------|
| `qmt-trader/vendor/bigqmt/` | **新增（vendored）** | 拷入外部库 client 侧：`xtquant_compat.py` + `transports/` + `redis_rpc.py` + `adapters/redis_common.py` + `SOURCE.md`（记录来源 commit / 同步方式） |
| `qmt-trader/_qmt_trader_base.py` | **新增** | 抽出 IPC/RPC 共用的回调轮询框架、纯逻辑工具、Fake 对象 |
| `qmt-trader/qmt_rpc_trader.py` | **新增** | `QmtRpcTrader` 适配器，内部持有 vendor 的 `xtquant_compat.xt_trader` |
| [config.py](../config.py#L282-L299) | **改** | 新增 `ENABLE_QMT_RPC_FALLBACK` + `QMT_RPC_*` 配置块（紧跟现有 IPC 块） |
| [position_manager.py:42](../position_manager.py#L42) | **改** | 工厂加 `elif ENABLE_QMT_RPC_FALLBACK` 分支 + 三开关互斥校验 |
| [utils/requirements.txt](../utils/requirements.txt) | **改** | 加 `redis`（ZMQ/MySQL 按需，可选注释） |
| `test/test_qmt_rpc_trader.py` | **新增** | Mock RPC 端单元测试，对齐 IPC 版用例 |
| [test/integration_test_config.json](../test/integration_test_config.json) | **改** | 加 `qmt_rpc` 测试组 |
| 本文档 + [CLAUDE.md](../CLAUDE.md) | **改** | 部署手册 + 架构说明 |

> 大QMT 端脚本（`BIGQMT_REDIS_DRYRUN.py` 等，Python 3.6 / GBK）**不进本仓库主干**，
> 走部署手册单独下发，与现有 `QMT_trade_executor.py` 的部署模式一致。

---

## 七、配置块（config.py 新增，紧跟 IPC 块）

```python
# ======================= 大QMT RPC Fallback（xtquant_big_convert）=======================
# 通过 Redis/ZMQ/MySQL RPC 远程驱动大QMT内置Python执行交易。
# ⚠️ 与 ENABLE_XTQUANT_MANAGER、ENABLE_QMT_IPC_FALLBACK 三者互斥
ENABLE_QMT_RPC_FALLBACK = _env_bool("ENABLE_QMT_RPC_FALLBACK", False)
QMT_RPC_TRANSPORT = os.environ.get("QMT_RPC_TRANSPORT", "redis")   # redis / zmq / mysql
QMT_RPC_REDIS = {                                                 # 密码走环境变量，切勿硬编码
    "host": os.environ.get("QMT_RPC_REDIS_HOST", "127.0.0.1"),
    "port": int(os.environ.get("QMT_RPC_REDIS_PORT", "6379")),
    "db": int(os.environ.get("QMT_RPC_REDIS_DB", "5")),
    "password": os.environ.get("QMT_RPC_REDIS_PASSWORD", ""),
}
QMT_RPC_ORDER_TIMEOUT = 30          # 下单后等待回执最大秒数
QMT_RPC_DEAL_POLL_INTERVAL = 1.0    # 成交回报轮询间隔（秒）
QMT_RPC_ALLOW_ORDER = _env_bool("QMT_RPC_ALLOW_ORDER", False)  # 下单二次确认开关（默认关）
```

**三开关互斥**：工厂现为 if-elif 隐式优先级。加 RPC 后应在
`_create_qmt_trader()` 头部显式校验三者最多只开一个，否则 `logger.error` + 抛异常，防误配。

---

## 八、工厂接入（position_manager.py）

在 [position_manager.py:42](../position_manager.py#L42) 的 `elif ENABLE_QMT_IPC_FALLBACK` 之后加分支：

```python
    elif getattr(config, "ENABLE_QMT_RPC_FALLBACK", False):
        import os as _os, sys as _sys
        _rpc_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "qmt-trader")
        if _rpc_dir not in _sys.path:
            _sys.path.insert(0, _rpc_dir)
        from qmt_rpc_trader import QmtRpcTrader
        account_config = config.get_account_config()
        return QmtRpcTrader(
            path=config.QMT_PATH,
            account=account_config.get("account_id"),
            account_type=account_config.get("account_type", "STOCK"),
        )
```

---

## 九、分阶段实施 + 验证标准

| 阶段 | 内容 | 验证标准 |
|------|------|---------|
| **P0 Spike（先做）** | Mock + 真实两步验证 `order_stock` 返回值 | 见下方细化 |
| P1 依赖引入 | vendored 拷入 client 侧到 `vendor/bigqmt/`，config 加块，requirements 加 redis | `import` 通过；开关默认 False 时现有行为不变 |
| P2 适配器 | 抽 `_QmtTraderBase`，写 `QmtRpcTrader` | 第三节契约方法全实现 |
| P3 工厂接入 | 加 elif 分支 + 三开关互斥校验 | 任两开关同开时报错退出 |
| P4 测试 | Mock RPC 端，照搬 IPC 测试用例 | 新测试组 100% 通过；`--fast` 无回归 |
| P5 联调 | 真实大QMT 跑 `BIGQMT_REDIS_DRYRUN.py`，模拟盘/小额下 1 手 | 下单→回执→持仓刷新闭环 |
| P6 文档 | 更新本文档「已实现」+ CLAUDE.md | — |

### 落地进度（2026-07-13）

| 阶段 | 状态 | 产出 |
|------|------|------|
| P0 | ✅ 完成 | 源码级定论（见第五节）；vendored 库 `test_xtquant_compat` 14 用例通过 |
| P1 | ✅ 完成 | [config.py](../config.py) RPC 配置块；[utils/requirements.txt](../utils/requirements.txt) 加 `redis` |
| P2 | ✅ 完成 | [_qmt_trader_base.py](_qmt_trader_base.py) 共享件 + [qmt_rpc_trader.py](qmt_rpc_trader.py) `QmtRpcTrader` |
| P3 | ✅ 完成 | [position_manager.py](../position_manager.py) 工厂加 RPC 分支 + 三开关互斥抛异常 |
| P4 | ✅ 完成 | [test/test_qmt_rpc_trader.py](../test/test_qmt_rpc_trader.py) 67 用例通过；`qmt_rpc` 测试组；最新 `--all-with-fast` 31 组、107 模块、1933 用例 100% 通过 |
| P5 | ✅ 完成 | Redis Memurai + 大QMT `BIGQMT_REDIS_DRYRUN.py` 完成只读和下单闭环联调，详见 `qmt-trader/大QMT-RPC联调checklist.md` |
| P6 | ✅ 完成 | 本文档、CLAUDE/AGENTS、文档站交易通道说明已补齐；行情 RPC 另列第十一节 |

> **实现说明（与原计划的一处偏差）**：为保持 surgical、不冒回归已过 70+ 测试的
> `qmt_ipc_trader.py` 的风险，`_qmt_trader_base.py` 仅被 `QmtRpcTrader` 复用，
> **未回改 `QmtIpcTrader` 去继承基类**。IPC 与 RPC 间有少量 Fake 对象/列名重复，属可接受权衡。

### P0 Spike 细化（Mock + 真实两步走）

**第一步 · Mock（可离线，我方可直接跑）**
- 跑外部库自带 77 个 Mock 测试，摸清 `xt_trader.order_stock()` 返回结构与 `query_orders` 字段名。
- 写 ~30 行脚本对着 Mock RPC 端跑通 order/query 往返，据此定 `QmtRpcTrader` 接口草稿。

**第二步 · 真实大QMT（需在装大QMT的机器上执行）**
- 在大QMT策略编辑器加载运行 `BIGQMT_REDIS_DRYRUN.py`（QMT 端配置 `rpc_allow_order_methods=True`）。
- 模拟盘或极小额下 1 手，**判读 `order_stock` 是否同步返回真实 order_id**：
  - 返回真实 id → 走「直接透传」路径。
  - 返回 True/无意义 → 走「合成整数 id + order_remark 回查」路径（第 5.1 节）。
- 判读标准：能否用返回值稳定关联到 `query_orders` 里的那一笔委托。

---

## 十、遵守的项目红线

- **模拟优先**：P5 前一律 `ENABLE_SIMULATION_MODE=True` 或模拟盘小额。
- **无硬编码**：Redis 密码、账号全走环境变量（隐私红线）；vendor 目录不含任何 Token/账号。
- **配置集中**：所有参数进 [config.py](../config.py)，无魔法数字。
- **信号验证 / 双层存储 / 线程锁**：适配器只替换交易通道，不碰上层这些机制，天然不受影响。
- **surgical**：不动 `QmtIpcTrader`（仅抽公共基类，行为不变）、不改上层调用方，只加分支。
- **Git**：非用户明确要求不主动提交。

---

## 十一、行情接入（xtdata RPC）—— 独立立项

> 交易通道（P1–P6）稳定后的独立子项，与交易开关**正交**，计划新增单独开关
> `ENABLE_QMT_RPC_DATA_SOURCE`（当前代码尚未实现）。核心价值：用真·QMT 实时行情替代当前标准模式的
> Tushare/Mootdx 降级数据，并从根上绕开 `xtdata` 标准模式的 BSON abort 崩溃
> （`get_market_data_ex` 触发 `u<1000000` 断言，见项目记忆 `xtdata-history-bson-crash`）。

### 11.1 接入缝：`_create_xtdata()` 工厂（与交易侧完全对称）

miniQMT 行情侧已有一个与 `_create_qmt_trader()` 对称的工厂
[data_manager.py:23-42](../data_manager.py#L23-L42)，按开关返回不同的 `self.xt` 对象：

```python
def _create_xtdata():
    if ENABLE_XTQUANT_MANAGER:   return XtDataAdapter(client)   # HTTP 网关适配器
    else:                        return xtquant.xtdata          # 原始模块
```

`self.xt` 在 [data_manager.py](../data_manager.py) 里是**多态使用**的（只要求鸭子类型接口一致）。
行情接入 = 在该工厂加一个分支，返回 vendor 的 `xtquant_compat.xtdata`（外部库已提供的
xtdata 兼容对象）。因 `self.xt` 全程按接口调用，**data_manager 其余代码零改动**。

```python
    elif getattr(config, "ENABLE_QMT_RPC_DATA_SOURCE", False):
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "qmt-trader"))
        from vendor.bigqmt.xtquant_compat import xtdata as rpc_xtdata, configure
        configure()                     # 注入 Redis/传输配置
        return rpc_xtdata               # 鸭子类型兼容 xtdata 模块
```

### 11.2 `self.xt` 必须满足的接口（鸭子类型契约）

RPC xtdata shim 需覆盖 data_manager 实际调用的方法（已在代码中定位）：

| 方法 | data_manager 调用点 | RPC 覆盖情况 |
|------|--------------------|-------------|
| `connect()` | [data_manager.py:317](../data_manager.py#L317) | ✅ 探测 RPC `ping` |
| `get_full_tick(codes)` | [:435](../data_manager.py#L435) `:1883` 实时价 | ✅ 外部库原生 |
| `get_market_data_ex(...)` | [:1200](../data_manager.py#L1200) 历史K线 | ✅ **RPC 隔离，绕开 BSON crash** |
| `download_history_data(...)` | [:1174](../data_manager.py#L1174) | ✅ 外部库原生 |
| `get_instrument_detail(code)` | [:1289](../data_manager.py#L1289) `:1341` 股票名称 | ✅ 外部库 `get_stock_name`/instrument |
| `subscribe_quote(code, count, callback)` | [:358](../data_manager.py#L358) `:417` 实时推送订阅 | ⚠️ **见 11.3 缺口** |
| `reconnect()` | [:471](../data_manager.py#L471) | 可选，缺失时降级用 `connect()` |

### 11.3 ⚠️ 关键缺口：无 push 订阅（与交易侧同源问题）

外部库是 **RPC 拉取式**，无 `xtdata` 那种 tick 推送。而 data_manager 盘中最优路径
（[路径1 `_on_xtdata_tick` 推送缓存](../data_manager.py#L1877-L1881)）依赖 `subscribe_quote`
的 push callback。RPC 模式下该路径不可用，处理策略二选一：

- **方案 A（推荐，简单）**：`subscribe_quote` 实现为**空操作**，盘中直接走
  [路径2 `get_full_tick` 轮询](../data_manager.py#L1883)。RPC 13ms 延迟下，3 秒一轮的持仓监控
  完全够用，牺牲的只是"零线程池开销"这一优化，不影响正确性。
- **方案 B（复杂）**：在 shim 内部起后台线程轮询 `get_full_tick`，主动回灌 `_on_xtdata_tick`
  callback 模拟推送。收益不大，不建议首版做。

首版取**方案 A**：实现成本最低，且 data_manager 的路径2→Mootdx 降级链已经健全。

### 11.4 降级链与健康评分

计划接入后标准模式的数据源优先级：

```
实时：RPC xtdata get_full_tick  →  Mootdx 兜底
历史：RPC xtdata get_market_data_ex/download_history_data  →  Tushare  →  Mootdx
```

miniQMT 已有 `MarketDataHealthTracker`（[data_manager.py:45](../data_manager.py#L45)）健康评分器，
RPC 源接入时按现有 `source="QMT-RPC"` 口径记录 latency/成败，纳入既有降级决策，无需另建机制。

### 11.5 配置块（config.py，与交易块并列）

```python
# 大QMT RPC 行情源（xtquant_big_convert xtdata）——与交易开关正交，可单独开启
ENABLE_QMT_RPC_DATA_SOURCE = _env_bool("ENABLE_QMT_RPC_DATA_SOURCE", False)
# 复用交易侧 QMT_RPC_TRANSPORT / QMT_RPC_REDIS 传输配置（同一大QMT RPC 服务）
```

> 行情与交易共用同一个大QMT RPC 服务（`BIGQMT_REDIS_DRYRUN.py`）和同一套传输配置，
> 因此两个开关可独立开：只开行情（交易仍走 xttrader/文件IPC）、只开交易、或两个都开。

### 11.6 分阶段（交易 P6 之后启动）

| 阶段 | 内容 | 验证标准 |
|------|------|---------|
| D1 | `_create_xtdata()` 加分支 + config 开关；shim 补齐 11.2 接口（`subscribe_quote` 空操作） | 开关默认 False 时行为不变 |
| D2 | Mock RPC 端跑 `get_full_tick`/`get_market_data_ex`/`get_instrument_detail` 往返 | 字段与 xtdata 对齐；组装成 data_manager 期望结构 |
| D3 | 真实大QMT 验证：拉一只股票实时价 + 一段历史K线 + 股票名称 | **确认 get_market_data_ex 不再 BSON crash**；数据与 QMT 官方一致 |
| D4 | 接入 `MarketDataHealthTracker`，跑 `xtdata_data_source` 测试组 | 无回归；降级链正确 |

### 11.7 行情侧红线

- **BSON crash 隔离验证是 D3 的核心验收点**：这是行情接入相对交易接入的**最大增量价值**，
  必须在真实大QMT上确认 `get_market_data_ex` 经 RPC 后不再 abort 进程。
- 首版 `subscribe_quote` 走空操作 + 轮询，不追求 push；不改 data_manager 其余逻辑（surgical）。
- 与交易接入共用 vendor 与传输配置，不重复引入依赖。

---

## 十二、决策记录

| 决策 | 选择 | 日期 |
|------|------|------|
| 外部库引入方式 | **vendored 拷贝**（`vendor/bigqmt/` + `SOURCE.md` 记录来源 commit） | 2026-07-13 |
| P0 验证环境 | **Mock + 真实大QMT 两步走** | 2026-07-13 |

## 十三、方案对比（vs 现有文件 IPC）

| 维度 | 文件 IPC（现有） | RPC（本方案） |
|------|-----------------|--------------|
| 通道 | 文件轮询 `C:\QuantIPC` | Redis 13ms / ZMQ 0.7ms / MySQL |
| 延迟 | ~1-2s | 毫秒级 |
| 跨机 | 需 UNC 网络共享 | 原生支持 |
| 行情 | ❌ | ✅ 117 个只读方法（后续接入） |
| QMT 端入口 | `QMT_trade_executor.py`（模型交易） | `BIGQMT_REDIS_DRYRUN.py`（策略编辑器 RPC 服务） |
| QMT 端依赖 | 无（纯文件） | redis 包（通常内置）/ pyzmq / pymysql |
| 部署复杂度 | 低 | 中（需 Redis） |
| 成熟度 | 已实盘验证 | 待 P0/P5 验证 |

**选型建议**：单机、依赖极简、已跑通 → 继续用文件 IPC；需低延迟/跨机/多账号集中/行情能力 → 用本方案。
