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
- v3.8.9 起，`take_profit_full` 全仓止盈委托成交确认后，`ENABLE_PAUSE_GRID_AFTER_TAKE_PROFIT_FULL` 默认会把同股活跃网格会话切到暂停（保留会话与账本）
- v3.9.0 起，`stop_loss` 止损清仓成交确认后同样触发该暂停；开关复用同一个 `ENABLE_PAUSE_GRID_AFTER_TAKE_PROFIT_FULL`
- 每个信号经过 `validate_trading_signal()` 验证，防止重复执行
- **信号保活与时效兜底**（v3.8.6）：`latest_signals` 是覆盖式队列，监控线程 3 秒一轮、策略线程单股消费周期约 `10 + 持仓数 + 股票池数` 秒。首次止盈这类「跨过即触发」的瞬时信号，价格回踩一次就会被原逻辑删除而永久丢失。现由 `ENABLE_DYNAMIC_SIGNAL_KEEPALIVE` 在窗口内保留未消费信号，并由 `validate_trading_signal()` 的年龄检查（`DYNAMIC_SIGNAL_MAX_AGE_SECONDS`）兜底，防止以过旧的价格快照下单。详见[止盈止损 · 信号保活与时效兜底](stop-profit-loss.md)

### IPC 启动与回调生命周期

`PositionManager` 先建立内存库、缓存、版本、信号、委托、刷新、网格和重连状态，再连接交易通道和注册回调，最后启动同步线程。`get_position_manager()` 与 `get_trading_executor()` 使用可重入锁串行发布实例，后台回报在构造阶段请求全局实例时等待完整对象。

IPC 的回调列表在锁内复制、锁外执行；未注册消费者时保留回执，委托投递抛出异常时允许下轮重试。避免“先连接再建锁”造成缺少 `pending_orders_lock` / `callback_refresh_lock`，也避免注册空窗吞掉历史回执。回归见 `test/test_ipc_startup_race.py`，部署与诊断见 [IPC 指南](qmt-ipc-fallback.md)。

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

### 资产、历史与自动买入约束

- 总资产核对包含可用资金、冻结资金和持仓市值；资产 / 估值偏差只告警，不触发断连或反推现金。
- IPC 的实时行情来自文件适配器；历史日线沿用 Tushare → Mootdx，指数走各源的指数接口，实时和历史健康按目的分别记录。
- 实盘内部自动策略（含网格）按单股全持仓、在途买入及未同步成交数量核对 `MAX_POSITION_VALUE`。未知买入结果保留预占，不重发；网格另保留 lot 成本预算。Web 手动买入路径的边界见 [Web API](web-api.md)。

### 外部成交补账

在 QMT 客户端手工下单、或其他程序用同一账号交易时，本机会收到成交回报但匹配不到本机 `pending_orders`。此时走 `_record_external_trade_after_callback()`：以 `strategy='external'` 补写 `trade_records`，并把 `last_position_update_time` 归零，强制下一轮持仓监控立刻重新拉取 QMT 持仓（而不是等 `QMT_POSITION_QUERY_INTERVAL` 到期）。

!!! warning "回调线程内不得回查 QMT"
    补账走在 QMT 回调线程上，此时**不能**再反向调用 `qmt_trader.position()` 之类的同步接口——底层会与回调线程互等而死锁。因此：

    - `_confirm_filled_order()` 只在确实匹配到本机委托（`matched_key or order_info`）时才请求持仓快刷，外部成交不走这条路径，改用置零时间戳让监控线程自己去同步。
    - 补写流水时 `data_manager.get_stock_name(allow_qmt_lookup=False)` 关闭 QMT 持仓回查，名称改由缓存/xtdata/Tushare 等非阻塞源提供。

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
data_manager.py        # 行情获取（实时标准模式 xtdata→Mootdx，网关模式 xtdata→lastClose/None；历史标准模式 Tushare→Mootdx，网关模式 xtdata→Tushare→Mootdx；行情健康评分；.SH/.SZ/.BJ 代码归一）
indicator_calculator.py # 技术指标计算
position_manager.py    # 持仓管理核心（内存 + SQLite 双层）
trading_executor.py    # 交易执行器（共用实盘下单入口、单股自动买入预占及各交易通道）
strategy.py            # 交易策略逻辑
web_server.py          # RESTful API 服务（Flask；QMT_API_TOKEN 配置后统一保护所有 /api/*，含 GET/SSE）
easy_qmt_trader.py     # QMT 交易 API 封装（xttrader 直连）
qmt-trader/            # 大QMT 降级交易通道  [v3.7.0+]
  _qmt_trader_base.py  #   IPC/RPC 共享件（列名、Fake对象、纯逻辑）
  qmt_rpc_trader.py    #   大QMT RPC 适配器（Redis/ZMQ RPC 驱动大QMT）
  qmt_ipc_trader.py    #   大QMT 文件IPC 适配器（JSON文件驱动大QMT）
  QMT_trade_executor.py #   大QMT 模型交易策略入口（保持 ASCII 源码，兼容内置编辑器）
  qmt_trade_client.py  #   策略端客户端库
premarket_sync.py      # 盘前同步与初始化
config_manager.py      # 配置持久化管理
sell_monitor.py        # 卖出委托单超时监控与撤单
grid_trading_manager.py # 网格交易会话管理
grid_database.py       # 网格交易数据持久化（SQLite）
grid_validation.py     # 网格交易参数校验
autobuy/               # 自动买入独立进程：候选池筛选、防重、HTTP 下单
macd_advisor.py         # MACD 操盘建议（决策矩阵 + 迷你全景图）
maintenance.py          # 数据库维护后台线程（清理历史数据、轮转日志）
xtquant_manager/       # XtQuantManager HTTP 网关（可选）
```

交易接口由 `position_manager._create_qmt_trader()` 四选一：默认 `easy_qmt_trader` 直连；`ENABLE_XTQUANT_MANAGER`、`ENABLE_QMT_IPC_FALLBACK`、`ENABLE_QMT_RPC_FALLBACK` 分别切换到 HTTP 网关、文件 IPC、RPC 后端，三个可选后端互斥。行情侧的 RPC xtdata 数据源当前未接入 `data_manager._create_xtdata()`，RPC 只作为交易后端使用。

股票代码在主程序、Web、网关兼容端点和 IPC/RPC 交易后端统一归一：`000001` / `SZ.000001` → `000001.SZ`，`600036` / `SH.600036` → `600036.SH`，`920118` / `BJ.920118` → `920118.BJ`。北交所 `.BJ` 当前不走 Mootdx 兜底，行情源不支持时保守返回空/跳过。

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
