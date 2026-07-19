# 架构说明

## 核心设计原则

### 信号检测与执行分离（最重要）

```
持仓监控线程（始终运行） → 检测非网格信号 → latest_signals 队列
                                      ↓
策略执行线程 → 检查 ENABLE_AUTO_OPERATION + ENABLE_AUTO_TRADING → 执行 / 忽略信号

网格交易线程 → 检查 ENABLE_AUTO_OPERATION + ENABLE_GRID_TRADING + grid_trading_sessions.enabled → 执行 / 暂停新网格单
```

- 持仓监控线程和网格线程可以持续运行，但自动下单受开关体系控制
- `ENABLE_AUTO_OPERATION` 是全局自动操作总开关，关闭时所有自动策略不产生新交易动作
- `ENABLE_AUTO_TRADING` 只控制动态止盈止损等非网格自动策略
- **动态止盈止损信号入队门控**：持仓监控仅在 `ENABLE_DYNAMIC_STOP_PROFIT` 且 `ENABLE_AUTO_TRADING` 同时开启时才检测并写入 `latest_signals`（`_detect_and_enqueue_dynamic_signal`）。任一关闭时不检测、不入队，避免"检测 → 策略因自动交易关闭而清除 → 再检测"的每 3 秒日志刷屏；关闭时会清理残留动态信号（保留 `grid_` 网格信号）。网格检测走独立分支（`ENABLE_GRID_TRADING`），不受此门控影响
- `ENABLE_GRID_TRADING` 控制网格模块，`grid_trading_sessions.enabled` 控制单只股票网格会话“自动/暂停”
- 每个信号经过 `validate_trading_signal()` 验证，防止重复执行

### 双层存储架构

```
实盘模式:
  QMT 实盘账户 → qmt_trader.position() → 内存数据库
  内存数据库 → 定时同步（15 秒） → SQLite 数据库

模拟模式:
  Web 界面 → trading_executor → simulate_buy/sell() → 内存数据库
```

- **内存数据库**：高频更新数据（价格、市值、盈亏比例）
- **SQLite**：持久化关键状态（开仓日期、止盈标记、最高价）
- 修改内存数据后必须调用 `_increment_data_version()` 触发前端更新

---

## 线程架构

| 线程 | 职责 | 频率 | 关键配置 |
|------|------|------|---------|
| 线程监控 | 检测线程崩溃并自动重启 | 60 秒 | `THREAD_CHECK_INTERVAL` |
| 数据更新 | 更新股票池行情数据 | 60 秒 | — |
| 持仓监控 | 同步实盘持仓、更新价格、检测信号 | 3 秒 | `MONITOR_LOOP_INTERVAL` |
| 策略执行 | 获取非网格信号、执行交易 | 5 秒 | `ENABLE_AUTO_OPERATION` + `ENABLE_AUTO_TRADING` |
| 网格交易 | 网格信号检测与买卖执行 | 5 秒 | `ENABLE_AUTO_OPERATION` + `ENABLE_GRID_TRADING` + `grid_trading_sessions.enabled` |
| 卖出监控 | 委托单超时撤单 | 2 秒 | `ENABLE_SELL_MONITOR` |
| 定时同步 | 内存 → SQLite 同步 | 15 秒 | `POSITION_SYNC_INTERVAL` |
| Web 服务 | RESTful API | 持续 | — |
| 心跳日志 | 定期输出系统运行状态 | 30 分钟 | `ENABLE_HEARTBEAT_LOG` |
| 盘前同步 | 重新初始化 xtquant | 每日 9:25 | `ENABLE_PREMARKET_XTQUANT_REINIT` |
| 自动买入 | 候选池筛选与 Web API 买入 | 独立进程定时触发 | `autobuy/miniqmt_autobuy.cfg` |

---

## 模块职责

```
config.py              # 集中配置管理
logger.py              # 统一日志管理
main.py                # 系统启动入口和线程管理
thread_monitor.py      # 线程健康监控与自愈
data_manager.py        # 行情获取（实时 xtdata→Mootdx；历史标准模式 Tushare→Mootdx，网关模式 xtdata→Tushare→Mootdx；行情健康评分）
indicator_calculator.py # 技术指标计算
position_manager.py    # 持仓管理核心（内存 + SQLite 双层）
trading_executor.py    # 交易执行器（xttrader 接口）
strategy.py            # 交易策略逻辑
web_server.py          # RESTful API 服务（Flask）
easy_qmt_trader.py     # QMT 交易 API 封装（xttrader 直连）
qmt-trader/            # 大QMT 降级交易通道  [v3.7.0+]
  _qmt_trader_base.py  #   IPC/RPC 共享件（列名、Fake对象、纯逻辑）
  qmt_rpc_trader.py    #   大QMT RPC 适配器（Redis/ZMQ RPC 驱动大QMT）
  qmt_ipc_trader.py    #   大QMT 文件IPC 适配器（JSON文件驱动大QMT）
  qmt_trade_executor.py #   大QMT 模型交易策略入口脚本（GBK编码）
  qmt_trade_client.py  #   策略端客户端库
premarket_sync.py      # 盘前同步与初始化
config_manager.py      # 配置持久化管理
sell_monitor.py        # 卖出委托单超时监控与撤单
grid_trading_manager.py # 网格交易会话管理
grid_database.py       # 网格交易数据持久化（SQLite）
grid_validation.py     # 网格交易参数校验
autobuy/               # 自动买入独立进程：候选池筛选、防重、HTTP 下单
xtquant_manager/       # XtQuantManager HTTP 网关（可选）
```

交易接口由 `position_manager._create_qmt_trader()` 四选一：默认 `easy_qmt_trader` 直连；`ENABLE_XTQUANT_MANAGER`、`ENABLE_QMT_IPC_FALLBACK`、`ENABLE_QMT_RPC_FALLBACK` 分别切换到 HTTP 网关、文件 IPC、RPC 后端，三个可选后端互斥。行情侧的 RPC xtdata 数据源当前未接入 `data_manager._create_xtdata()`，RPC 只作为交易后端使用。

---

## 行情源健康评分

`data_manager.py` 内置 `MarketDataHealthTracker`，对 xtdata/Mootdx 的实时行情请求做轻量内存评分：

- 记录成功/失败、原因、延迟、数据质量和最近成功时间
- 按 5 分钟窗口计算 `healthy` / `degraded` / `unstable` / `down`
- 快照通过 Flask `GET /api/market/health` 暴露
- 不落库，系统重启后样本清空
- 默认 `MARKET_HEALTH_OBSERVE_ONLY = False`，由 `data_manager.is_quote_tradable()` 参与持仓监控信号检测；如需只观察不拦截，可显式改为 `True`

---

## 优雅关闭流程

系统退出时按以下顺序关闭（`main.py` 的 `cleanup()` 函数）：

```
1. Web 服务器 → 停止接收新请求
2. 线程监控器 → 停止监控循环
3. 业务线程 → 停止数据更新、持仓监控、策略执行
4. 核心模块 → 按依赖顺序关闭
```

每个步骤都有独立的异常处理，确保单个步骤失败不影响其他资源清理。
