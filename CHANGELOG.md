# Changelog

本文件记录 miniQMT 项目所有重要变更，格式遵循 [Keep a Changelog 1.1.0](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [SemVer 2.0.0](https://semver.org/lang/zh-CN/)。

> 本文件是 **唯一的变更记录源**。文档站 `/changelog/` 页面通过 `include-markdown` 引用本文件，请在此处直接编辑。

## [Unreleased]

## [3.8.2] - 2026-07-24

### Changed
- **网格真实盈亏账本改为 LIFO 最近优先配对**：`grid_lots` 普通卖出优先匹配最近买入批次；`grid_lot_matches` 中先卖后买的底仓回补优先匹配最近未回补卖出，使策略绩效口径贴近“上涨卖出、回落买回”的网格闭环。

### Tests
- 使用 `C:\Users\PC\Anaconda3\envs\python39\python.exe test\run_integration_regression_tests.py --all-with-fast` 完整回归：31 组、107 模块、1963 用例，1963 通过，0 失败，0 错误，成功率 100%。

### Docs
- 更新 README、架构文档和在线网格/数据库文档中的真实账本口径说明，从 FIFO 调整为 LIFO 最近优先。

## [3.8.1] - 2026-07-15

> 本版本聚焦**实盘成交确认闭环与 Web 网格状态口径修正**：动态止盈止损、补仓和网格交易均以成交确认为准写入普通成交流水；web1.0 网格悬停卡片统一真实盈亏和中心价偏离展示口径。

### Changed
- **成交确认后写 `trade_records`**：实盘动态止盈止损、补仓和网格委托不再在委托提交阶段写普通成交流水，统一等成交回报或对账兜底确认后落库，避免 Web 交易记录出现未成交委托。
- **网格悬停卡片口径统一**：web1.0 已启动网格个股的悬停卡片统一读取后端 `pnl_snapshot` 真实盈亏，小数比例由前端统一格式化为百分比，避免重复乘以 100。
- **中心价偏离口径明确化**：`/api/grid/session/<stock_code>` 返回中心漂移偏离、市价偏离和有效偏离三类字段；悬停卡片“中心价偏离”仅展示当前网格中心价相对初始中心价的漂移，并标注上移/下移。

### Fixed
- 修复网格悬停卡片中“盈亏率”可能把后端百分比和小数比例混用导致显示错误的问题。
- 修复“中心价偏离”展示混入当前市价偏离的问题；自动退出仍使用 `max(drift_deviation, market_deviation)` 作为有效偏离。

### Tests
- 新增/扩展 `test/test_web_api_complete.py`，覆盖 `/api/grid/session/<stock_code>` tooltip 字段的小数比例、真实盈亏快照和偏离度字段。
- 新增 `test/test_web1_grid_dialog_static.py`，静态验证 web1.0 悬停卡片不再重复缩放后端比例，且中心价偏离按后端中心漂移字段展示。
- 完整集成回归：`C:\Users\PC\Anaconda3\envs\python39\python.exe test/run_integration_regression_tests.py --all-with-fast`，31 组、107 模块、1933 用例，1933 通过、0 失败、0 错误、0 跳过，成功率 100%，耗时 633.95 秒。

### Docs
- 更新 README、Web 前端文档和 Web API 文档，说明网格悬停卡片的数据来源、比例格式、中心价偏离和成交确认后写流水语义。
- 更新网格交易文档，补充偏离度双口径、有效偏离退出规则，以及三种 xttrader 通道下成交兜底确认后写 `trade_records` 的约束。
- 更新测试框架文档到 v3.8.1 完整回归结果。

## [3.8.0] - 2026-07-13

> 本版本聚焦**大QMT RPC 交易通道 P5 下单联调闭环与配置体验优化**：price_type 调用方可控透传、.env fallback 生产可用、控制台 XtTrader 通道总控菜单（三通道一键切换）。

### Added
- **miniqmt.bat XtTrader 通道总控菜单 `[p]`**：三通道（miniQMT 直连 / 大QMT 文件IPC / 大QMT RPC）可视状态显示与一键切换，支持 RPC Redis 连接配置（host/port/db/password）、下单安全开关切换，同时自动处理通道互斥。
- **.env fallback 机制**：`config.py` 顶部新增 `_load_dotenv_fallback()`，import 时把项目根 `.env` 补进未设置的环境变量。优先级：Windows 用户级/系统环境变量 > .env。零新依赖，自写解析器（KEY=value / # 注释 / 剥引号 / utf-8-sig）。
- **`.env.example` 全面改写**：对齐完整 `.env` 结构（QMT路径/Web API Token/Tushare/IPC/RPC/数据源），所有敏感值替换为占位符，添加分组注释与配置优先级说明。
- **测试隔离开关 `MINIQMT_DISABLE_DOTENV`**：`test/conftest.py` + `test/run_integration_regression_tests.py` 顶部设开关，防止本地 `.env`（含真实 token）污染回归测试基线。
- **大QMT RPC Redis 部署文档** `docs/site/miniqmt/qmt-rpc-redis-setup.md`：Memurai 安装、密码配置、L1/L2 连通性验证、跨机补充，已挂 mkdocs 导航。

### Changed
- **`QmtRpcTrader._send()` price_type 透传**：`_send()` / `buy()` / `sell()` / `order_stock()` 接受调用方显式指定 `price_type` 并透传到 vendored → passorder；不指定时按价格自动推断（price=0 → LATEST_PRICE，否则 → FIX_PRICE）。
- **`_launcher.py` 菜单重构**：原 `[o]` IPC 配置保持不变，新增 `[p]` XtTrader 通道总控（含 `cmd_xttrader_config` + 状态辅助函数），主菜单底部实时打印当前通道简述与 RPC 详情。选择范围从 `a-o` 扩展到 `a-p`。
- **联调 checklist 更新**：`qmt-trader/大QMT-RPC联调checklist.md` 下单闭环全部勾选，新增 strategy_name 匹配和 price_type 透传排障条目。

### Fixed
- 首次启用 .env fallback 后全量回归 54 个 failure（本地 `.env` 含真实 token 导致 web_api 401 + 数据源路由漂移）→ `MINIQMT_DISABLE_DOTENV` 修复，1912/1912 全绿。
- `QmtRpcTrader._send()` 原忽略 `price_type` 参数自行推断 → 改为透传（调用方指定时生效）。

### Tests
- `test/test_qmt_rpc_trader.py`：13 → 67 用例。新增连接生命周期、断连回调、Redis 推送事件模拟、卖单路径、资金校验（check_stock_is_av_buy/sell）、健康诊断、order_id_map 截断、市价 vs 限价 price_type、空持仓、空 available 字段降级。
- `test/test_config_env_overrides.py`：新增 `TestDotenvFallback` 5 用例（补缺/优先级/引用剥离/注释跳过/缺失文件 no-op）。
- 全量集成回归 `--all-with-fast`：31 组、1912 用例、1912 通过、0 失败、0 错误，成功率 100%。
- P5 真实联调验证（Redis Memurai + 大QMT BIGQMT_REDIS_DRYRUN，账号 25105132）：L0 redis 库 → L1 Redis 直连 → L2 RPC ping/rpc_alive → 资产/持仓查询返回真实数据（可用46.6万/持仓300105一只）→ 下单闭环（strategy_name 匹配后查到委托、sysid 回填、撤单成功）。非交易时段废单 status=57（预期），完整链路 passorder→sysid→查询→撤单已验证。联调脚本：`test/live_qmt_rpc_readonly_check.py` / `test/live_qmt_rpc_strategy_check.py`。

### Docs
- 更新 CLAUDE.md：v3.7.0+ 配置开关、三通道概述、.env fallback 机制、QmtRpcTrader 模块职责、qmt-trader/ 子模块说明。
- 更新 `docs/site/miniqmt/configuration.md`：mermaid 图新增 RPC 通道、四通道表格、RPC 参数完整列表（12 项）。
- 更新 `docs/site/miniqmt/index.md`：核心特性新增 xttrader 降级通道和 .env fallback，下一步链接新增 RPC Redis 部署文档。
- 更新 `docs/site/miniqmt/architecture.md`：模块职责新增 qmt-trader/ 子模块说明。
- 更新 `release_version.json` → v3.8.0。

## [3.7.0] - 2026-07-13

> 本版本聚焦**大QMT RPC 交易后端（QmtRpcTrader）P5 只读联调与 price_type 透传修复**。

### Added
- **大QMT RPC 交易后端 `QmtRpcTrader`**：第四种交易通道，基于 vendored xtquant_big_convert，通过 Redis/ZMQ RPC 驱动大QMT策略进程执行交易。`_create_qmt_trader()` 四选一工厂，`ENABLE_QMT_RPC_FALLBACK` / `QMT_RPC_TRANSPORT` / `QMT_RPC_REDIS_*` / `QMT_RPC_ALLOW_ORDER` 等配置项。（v3.6.0 引入代码，v3.7.0 完成 P5 只读联调验证）
- **RPC 只读联调验证**：L0(redis库)/L1(Redis直连)/L2(RPC链路) 三阶自检，ping → query_stock_asset/position/orders/trades 返回真实数据，联调脚本留存 `test/live_qmt_rpc_readonly_check.py`。

### Fixed
- **`_send()` price_type 参数忽略**：`buy/sell/order_stock` 接口接受 `price_type` 参数但 `_send()` 不转发 → 修复为调用方指定时透传，不指定时按价格自动推断（price=0→LATEST_PRICE，否则→FIX_PRICE）。

### Docs
- `qmt-trader/大QMT-RPC联调checklist.md`：新增 strategy_name 匹配排障、price_type 透传说明。

## [3.6.0] - 2026-07-10

> 本版本聚焦**实盘委托生命周期与无人值守生产安全**：首次止盈状态改为成交回报确认后落地，动态止盈止损信号在已有在途委托时阻断，撤单重挂价格增加多级兜底；同时发布大QMT文件IPC交易通道、xtdata tick 推送缓存和行情健康严格门禁。

### Added
- **大QMT文件IPC交易通道**：新增 `qmt-trader/qmt_ipc_trader.py`、`qmt_trade_client.py`、`QMT_trade_executor.py` 和部署手册，支持在 miniQMT xttrader 直连受限时，通过大QMT内置 Python 脚本执行下单/撤单/成交回报轮询；多账号自动隔离到 `{QMT_IPC_ROOT}/{account_id}/`。
- **控制台配置入口**：`miniqmt.bat` / `scripts/_launcher.py` 增加 Tushare Pro 与大QMT IPC Trader 快捷配置、连通性检查和心跳检查。

### Changed
- **首次止盈实盘确认语义**：`take_profit_half` 实盘委托提交成功后不再立即标记 `profit_triggered=True`，改为成交回报确认后更新内存与 SQLite，避免委托未成交时误进入动态止盈阶段。
- **在途委托防重保护**：动态止盈止损入队和最终信号校验都会检查本地 `pending_orders` 与 QMT 活跃委托；同一股票已有在途卖单时阻断新止盈/止损信号，防止重复卖出。
- **撤单重挂价格兜底**：`PENDING_ORDER_REORDER_PRICE_MODE="best"` 下买三价为 `0` 或缺失时，按买一价、最新价、收盘价、原信号价逐级降级；`sell_stock(price=0)` 也会自动改为获取有效买盘/最新价。
- **行情源健康门禁默认启用**：`MARKET_HEALTH_OBSERVE_ONLY` 默认改为 `False`，持仓监控会按健康评分和数据源策略判断行情是否可参与交易信号检测。
- **xtdata tick 推送缓存**：优化实时行情读取路径，减少重复 tick 请求并提升持仓监控循环稳定性。
- **动态止盈止损信号门控**：监控线程仅在 `ENABLE_DYNAMIC_STOP_PROFIT` 与 `ENABLE_AUTO_TRADING` 同时开启时才检测并写入动态止盈止损信号，关闭自动止盈时清理残留动态信号，避免日志刷屏。

### Fixed
- **撤单后重挂跟踪丢失**：修复旧委托撤销成功后先重挂再清理，导致新委托跟踪记录被误删的问题。
- **自动止盈关闭后的日志刷屏**：修复持仓持续满足止盈条件时，监控线程每 3 秒反复入队、策略线程反复清理的循环。

### Security
- **账号信息脱敏**：将 IPC、XtQuantManager 示例文档、测试样例和 Web 占位提示中的真实资金账号统一替换为 `TEST_ACC_1` / `TEST_ACC_2`，避免发布包泄露生产证券账号。

### Tests
- 使用 `C:\Users\PC\Anaconda3\envs\python39\python.exe test/run_integration_regression_tests.py --all` 完成全量集成回归：28 组、70 模块、1039 用例，1039 通过，0 失败，0 错误，成功率 100%。
- 新增/扩展 `test_trader_callback` 与 `test_order_rejection`，覆盖成交后标记首次止盈、在途委托阻断、撤单重挂跟踪保留、买三价为 0 时价格降级、卖出价为 0 时自动兜底。

### Docs
- 更新配置参考、止盈止损策略、Web 前端/Web API、测试框架、README 与开发指南，说明 v3.6.0 的委托确认语义、行情健康默认门禁和最新回归统计。

## [3.5.0] - 2026-07-09

> 本版本聚焦**网格交易实盘落账准确性**：部分成交聚合落账避免QMT拆单导致重复记录，买卖量基数统一确保网格对称运行。同时新增 Tushare 数据源适配。

### Added
- **Tushare 数据源适配**：新增 tushare 股票行情数据接口适配，作为 xtdata/Mootdx/baostock 之外的数据来源扩展（`test/test_tushare_adapter.py` + `test/smoke_tushare.py`）。

### Changed
- **网格部分成交聚合落账**：`handle_deal_callback` 改为部分成交阶段只累积填充量不落账（不写 `grid_trades`/`trade_records`，不重建网格），全部成交后一次性聚合写入（1条 `grid_trades` 加权均价 + 1条 `trade_records` + 1次 `_rebuild_grid`），避免 QMT 拆单（如 1300 股拆成 12 笔）导致重复落账和统计失真。DB失败时回滚 pending 累积量，保留 pending 等待补偿确认重试。
- **网格买卖量基数统一**：有持仓时买入量与卖出量使用同一基数 `current_volume × position_ratio`，确保每档买卖操作量对称；无持仓时回退为基于金额计算；买入量始终受 `max_investment` 硬上限约束。`execute_grid_trade` 中 BUY 信号也预取持仓快照（原仅 SELL）。
- 聚合落账用 `order_id` 作为 `trade_id`，避免多笔部分成交使用无意义的券商 `trade_id`。

### Tests
- `test_grid_live_order_confirmation` — 部分成交聚合语义已同步更新
- `test_grid_bugfix_c1` — DB失败+回滚逻辑更新
- `test_grid_mece_regression` — 部分成交统计预期更新
- `test_grid_trade_buy` / `test_grid_trade_sell` — 聚合 trade_id 更新
- `test_max_investment_strict` — 买入量基于持仓的预期值更新
- 新增 `test_tushare_adapter` / `smoke_tushare` — tushare 适配器单元测试与冒烟
- 集成回归测试新增 tushare 适配器模块到 fast 组

### Docs
- 更新 `docs/site/miniqmt/grid-trading.md` 部分成交聚合与买卖量统一文档

## [3.4.0] - 2026-07-04

> 本版本聚焦**无人值守长稳运行与自动操作开关解耦**：新增数据库维护与日志轮转，将自动交易拆为「总开关 → 策略分开关 → 单只会话开关」三层结构，并把发布版本号收敛到单一来源统一管理。

### Added
- **自动操作三层开关**：新增全局总开关 `ENABLE_AUTO_OPERATION`（默认 `False`，运行时开关、不持久化），与 `ENABLE_AUTO_TRADING`（动态止盈止损分开关）、`ENABLE_GRID_TRADING`（网格分开关）解耦，形成「总开关 → 策略分开关 → 单只网格会话 `grid_trading_sessions.enabled`」结构；关闭总开关时所有自动策略停止产生新单，监控线程仍持续检测信号。web1.0 / web2.0 自动操作控制同步调整。
- **数据库维护任务**（[maintenance.py](https://github.com/weihong-su/miniQMT/blob/main/maintenance.py)，`ENABLE_DB_MAINTENANCE=True`）：独立线程每日非交易时段（`DB_MAINTENANCE_TIME="00:10:00"`）清理过期追加型历史数据，删除行数达阈值（`DB_MAINTENANCE_VACUUM_MIN_DELETED_ROWS=1000`）后执行 `VACUUM` 回收空间；`DB_MAINTENANCE_REQUIRE_NON_TRADE_TIME=True` 确保不影响盘中交易。
- **日志轮转**：XtQuantManager 批处理重定向日志按大小轮转（`XQM_LOG_MAX_SIZE=10MB` × `XQM_LOG_BACKUP_COUNT=5`），随维护任务触发；主日志沿用 `RotatingFileHandler`。
- **发布版本号单一来源**：新增 `release_version.json` 作为唯一版本号出处，web1.0 / web2.0 页面标题、[web_server.py](https://github.com/weihong-su/miniQMT/blob/main/web_server.py)、`web2.0/vite.config.ts` 均通过 `%MINIQMT_RELEASE_VERSION%` 占位符注入，避免版本号分散硬编码。
- **baostock API Key 支持**：新版 baostock(0.9.x) 收紧访问后，登录前经 `set_API_key` 传入 `BAOSTOCK_API_KEY`（环境变量，默认空则匿名访问）。

### Changed
- **baostock 接入规范化**：依赖约束由 `==0.9.1` 放宽为 `>=0.9.1`；新增登录超时 `BAOSTOCK_LOGIN_TIMEOUT=5s`、连续失败冷却 `BAOSTOCK_RETRY_COOLDOWN=300s`、失败阈值 `BAOSTOCK_MAX_CONSECUTIVE_FAILURES=3`；`ENABLE_BAOSTOCK_STOCK_NAME_LOOKUP` / `ENABLE_BAOSTOCK_HISTORY_DATA` 默认关闭，历史行情默认改走 Mootdx，避免无人值守时外部接口反复报错。
- **web1.0 下单日志**：改为定时刷新并优化视觉样式与可读性。
- **Web 页面标题**：统一附带发布版本号（如「交易监控面板 - miniQMT v3.4.0」）。

### Fixed
- 完善行情源健康检测验证逻辑，减少误判。
- 优化止损委托阻断处理，避免异常委托状态阻塞后续止损。
- 网格实盘成交记录延迟到成交回报到达后再登记，避免委托未成交即入账。
- 修复 MkDocs strict 模式构建告警。

### Docs
- 无人值守文档新增「数据库维护与日志轮转」章节；配置参考补充自动操作三层开关、baostock 接入、数据库维护与保留天数、日志轮转参数。

### Database
- 数据库维护任务按保留策略清理追加型历史表：`trade_records`（`TRADE_RECORD_RETENTION_DAYS=1095`，3 年）、`grid_trading_sessions`（`GRID_SESSION_RETENTION_DAYS=365`，仅非 active）、`premarket_sync_history`（365）、`config_history`（365）、autobuy `decision_log`（`AUTOBUY_DECISION_LOG_RETENTION_DAYS=90`）。

## [3.3.0] - 2026-06-27

### Added
- 新增自动买入模块文档：说明 `miniqmt_autobuy` 独立进程、候选池筛选、大盘指数门禁、防重风控、调度与复盘库。
- 新增行情源健康评分文档：说明轻量内存版评分、不落库、观察模式、配置项和 `/api/market/health` 快照接口。

### Changed
- 同步 README、AGENTS、CLAUDE 和在线文档到当前代码：补充 `miniqmt.bat` 自动买入菜单 `[j]`-`[m]`、`--all-with-fast` 回归测试参数、当前测试分组规模、网格真实账本详情接口 `/api/grid/ledger/<session_id>`。
- 更新 Web/API 文档的网关能力边界：`/api/grid/sessions` 在 xtquant_manager 网关模式下支持只读兼容返回，网格写操作和账本详情仍需 Flask 直连。
- 更新配置与架构文档：补充历史数据同步节流/超时参数、自动买入独立配置文件和独立进程定位。
- 同步网格启动条件：`GRID_REQUIRE_PROFIT_TRIGGERED` 当前默认值为 `False`，持仓个股默认不再要求先触发首次止盈即可启动网格；设为 `True` 时仍作为保守安全阀。
- 更新测试统计口径：当前配置 29 个测试组（含 `fast`）、89 个模块引用、64 个唯一测试模块；最近 `--all` 回归为 28 组、65 个模块引用、961 个用例 100% 通过。

## [3.2.0] - 2026-06-13

> 本版本聚焦**网格交易实盘化**：以「成交回报为准」重构订单闭环，新增对手价下单、涨跌停/停牌防护、启动对账与真实盈亏账本，使网格策略可安全用于实盘。

### Added
- **实盘委托成交确认**（`GRID_CONFIRM_LIVE_ORDER_BY_DEAL`，默认 `True`）：实盘下单后先登记待确认委托（`grid_orders` 表），等成交回报 `handle_deal_callback` 到达再落账并重建网格；支持部分成交累计、`trade_id` 幂等去重、单事务落账
- **对手价下单**（`GRID_USE_COUNTERPARTY_PRICE`，默认 `True`）：买取卖三价 / 卖取买三价提高成交概率；`GRID_COUNTERPARTY_BUY_PRICE_BUFFER_RATIO`（2%）按风险价预占资金防止突破 `max_investment`
- **涨跌停 / 停牌防护**（`GRID_ENABLE_PRICE_LIMIT_GUARD`，默认 `True`）：下单前 `_check_tradable` 检查盘口，封板/停牌跳过本次交易，涨跌停价获取失败 fail-open；容差 `GRID_PRICE_LIMIT_EPS`
- **信号执行前复核**：信号有效期（`GRID_SIGNAL_MAX_AGE_SECONDS`，60s）+ 价格漂移（`GRID_SIGNAL_MAX_PRICE_DRIFT_RATIO`，1%）双重校验，丢弃陈旧/失真信号
- **启动对账（startup reconcile）**：系统重启从 `grid_orders` 恢复未完成委托，查询券商当日成交/委托补记差异、关闭终态委托
- **对手方资金/持仓预留**：下单计划扣除待成交委托占用，防止锁外窗口期重复下单超额
- **真实盈亏账本**：新增 `grid_lots`（买入批次）+ `grid_lot_matches`（初版 FIFO 卖出配对，当前已在 Unreleased 改为 LIFO 最近优先）表；`get_pnl_snapshot` 统一盈亏视图按数据可用性分级（`ledger_true_pnl` / `memory_true_pnl` / `cash_flow_legacy` / `fallback_market_value_ratio`），含已实现/未实现盈亏与降级标记
- **网格盈亏前端面板**：web1.0 / web2.0 新增 `GridStatusPanel`，展示利润来源、降级提示，Web API 网格端点返回 `pnl_snapshot`
- **清仓残留持仓告警限频**（`CLEARED_POSITION_WARNING_INTERVAL`，默认 1800s）：券商盘后仍返回已清仓行时降噪，超频降为 DEBUG

### Changed
- `miniqmt.bat` 调整 Python 虚拟环境优先顺序
- 精简部分报错信息（`easy_qmt_trader`）
- 加固股票名称解析（`data_manager` / `position_manager` / `xtquant_manager.client`），提升名称缺失/异常时的健壮性

### Fixed
- 防止陈旧的首次止盈半仓回撤误触发（`guard stale half take-profit pullbacks`）
- 避免盘后已清仓持仓的成本价告警刷屏

### Database
- 新增表：`grid_orders`、`grid_lots`、`grid_lot_matches`
- `grid_trading_sessions` 新增字段：`risk_level`、`template_name`、`total_buy_volume`、`total_sell_volume`（均带自动迁移）
- `grid_orders` 新增字段：`reserved_price`（带自动迁移）

### Docs
- 网格交易文档新增「实盘交易机制」章节；配置参考补充网格实盘参数；数据库文档更正表名 `grid_sessions` → `grid_trading_sessions` 并补全订单/账本表

## [3.1.0] - 2026-05-30

### Added
- **web2.0 启动模式选择**: `miniqmt.bat` 菜单 [7]/[8]/[9] 启动前可选 `web1.0` (Flask :5000 起) 或 `web2.0` (xtquant_manager :8888)，偏好持久化到 `data/.web_mode`
- **xtquant_manager 内嵌 web2.0**: 网关启动后 `http://localhost:8888/` 直接托管 `web2.0/dist/`（静态文件 + SPA fallback），菜单 [g] 打开浏览器
- **Flask 兼容 API 端点**（`xtquant_manager/server.py`）使 web2.0 前端无需改造即可在网关模式下运行：
  - `GET /api/status` `/api/positions` `/api/positions-all` `/api/connection/status` `/api/config` `/api/trade-records`
  - `GET /api/accounts` — 无 Token 公开列出账号 ID，互联网只读用户也能正确发现多账号（无 token 时不再退化为只显示第一个账号）
  - 字段映射对齐 Flask 顶层格式，QMT 实时数据 + SQLite 持久化元数据合并，账号隔离基于 `X-Account-Id` 请求头
- **网关模式动态止盈状态查询**: `/api/v1/stop-profit/status` `/config` `/toggle`，复用 `position_manager` 算法
- **网关模式只读防护**: web2.0 在 `isGatewayMode()` 时禁用自动操作总开关/动态止盈控制/参数保存/模拟买入/初始化按钮，显示「🔒 网关模式 · 只读监控+下单」徽章
- **连接设置面板**: 顶部齿轮 ⚙ 进入，支持「网关模式 / 直连模式」切换、网关地址 + API Token 配置、测试连接（8s 超时 + 非 JSON 检测 + 详细错误）、HTTPS Mixed Content 警告、保存后自动 `discoverAccounts()` 刷新账号下拉
- **iPhone / 移动端适配**: 持仓表格 `overflow-x-auto` 横向滚动 + `min-w-[800px]` 保表头不挤压；HeaderBar 按 `sm:` 断点响应式堆叠；竖向单列布局 + 止盈列改图标
- **Vercel 一键远程部署**: 根目录新增 `vercel.json` 指定 web2.0 构建命令与输出目录，配合 Cloudflare Tunnel 实现「Vercel 前端 + Windows QMT 后端」远程部署
- **绑定地址与客户端地址分离**：`XQM_DEFAULT_HOST=0.0.0.0` (绑定) + `XQM_CLIENT_HOST=127.0.0.1` (客户端目标)；启动菜单同时显示「本机 URL」+「局域网 URL」方便从其他设备访问

### Changed
- **web2.0 交易日志**: 网关模式从「QMT 当日成交/委托」改为优先读 SQLite `trade_records` 表（与 web1.0 同源，含名称/时间/策略/历史买卖），SQLite 无记录时回退 QMT
- **web2.0 持仓字段补齐**: 改用 SQLite 持久化数据替代 xtdata/公式估算，网关模式下持仓名称、建仓日期、止损价能正确显示
- **web2.0 盈亏颜色按 A 股习惯**: 红涨绿跌（与原默认的绿涨红跌相反）
- **web2.0 监控/止盈按钮文案**: 「开始监控/停止监控」「开启动态止盈/禁用动态止盈」（替代 ON/OFF）
- **web2.0 配置面板布局**: 4 列网格 + 标签右对齐 + 紧凑输入框；买入操作整合到 HeaderBar 第 3 行（移除独立 BuyPanel 卡片）
- **web1.0 默认只绑本机**: `WEB_SERVER_HOST=127.0.0.1`，web2.0/xtquant_manager 负责对外（避免 web1.0 误暴露完整写操作 API 到公网）
- **`xtquant_manager` 健康检查日志降噪**: 减少非异常情况下的常规健康检查输出

### Fixed
- **web2.0 网关模式涨跌幅恒为 0**: 持仓裸代码缺少市场后缀（`.SZ`/`.SH`），网关请求 tick 失败，补齐后缀
- **web1.0 持仓不刷新**: SSE `onmessage` 因 `wasSimulationMode` 未定义崩溃，导致后续推送被中断
- **web2.0 连接设置变更后账号下拉未刷新**: 切换网关 URL/Token 后自动调用 `discoverAccounts()` 同步真实账号列表
- **web2.0 互联网用户只能看到第一个账号**: 无 Token 时无法访问 `/api/v1/accounts`，新增公开 `/api/accounts` Flask 兼容端点
- **web2.0 盈亏比例显示错误**: `fmtPercent` 多乘 100（小数→百分比转换），与 web1.0 对齐
- **web2.0 持仓价格精度**: 统一 2 位小数（原 3 位），与 A 股报价精度一致
- **launcher 0.0.0.0 不能作客户端目标**: 健康检查、菜单 UI 打开统一改用 `127.0.0.1`

### Docs
- 新增「Web 前端（web1.0 / web2.0）」章节：双模式架构、网关能力边界、连接设置、启动菜单、Vercel 远程部署 — 见文档站
- `web-api.md` 标注哪些端点在 xtquant_manager 网关模式下可用
- `CLAUDE.md` 同步 Web 双模式架构说明（commit 7035354d）

## [3.0.0] - 2026-05-24

### Added
- **XtQuantManager 动态止盈止损**: 网关模式下独立运行的止盈止损后台监控 (`xtquant_manager/stop_profit.py`)
  - 直接复用 `position_manager.py` 中已验证的止损/首次止盈/动态止盈算法
  - 信号去重（60s 窗口）+ 自动下单（实盘 xttrader 接口）
  - API 端点：`/api/v1/stop-profit/status`、`/config`、`/toggle`
- **web2.0 Vue3 前端**: 全新的持仓管理 Web 界面 (`web2.0/`)
  - Vue3 + Vite + TypeScript + Tailwind CSS + Pinia 状态管理
  - PWA 支持 (vite-plugin-pwa)，可安装到桌面离线使用
  - 双后端兼容：Flask (web1.0 API) + xtquant_manager (v1 API)
  - 多账户切换、连接设置面板、SSE 实时推送 + 智能轮询
  - 止盈止损开关（与 web1.0 `firstProfitSellEnabled` 对齐）
  - Vercel 一键部署支持 (见 `web2.0/VERCEL_DEPLOY.md`)
- **miniqmt.bat 新增 XtQuantManager 菜单**: [d] 启动 [e] 停止 [f] 状态 [g] UI [h] 重启 [i] 日志
- 统一文档体系：MkDocs + mkdocstrings（docstring 自动抽取）+ include-markdown（CHANGELOG 引用）+ 本地热重载 `start_docs.bat`
- 文档构建依赖独立到 `utils/requirements-docs.txt`，不污染运行环境
- GitHub Actions 部署工作流加 `if: false` 守门，未来开启只需删除一行

### Changed
- `docs/site/` 作为唯一 markdown 源，根目录 `CHANGELOG.md` 作为变更日志唯一真源
- web2.0 配置百分比字段统一精度到 2 位小数，金额字段整数显示
- 界面全面视觉升级：渐变背景、毛玻璃顶栏、分层阴影卡片、动画模态框、盈亏色条

### Security
- **隐私安全加固**: `Methods.py` 硬编码 Pushplus Token 改为 `PUSHPLUS_TOKEN` 环境变量
- `web2.0/src/api/accounts.ts` 默认账户去真实 ID，改为空占位符
- `.gitignore` 新增 `web2.0/dist/` 和 `web2.0/node_modules/`
- 文档示例中的真实账号 ID 替换为 `55009640` 等虚构 ID

---

## [2.0.0-Beta] - 2026-03-28

### Added
- 完整回归测试框架：23 组 × 67 模块 × 1170 个测试用例，全部通过（100%）
- 网格交易全区间覆盖测试（114 个用例，A–K 11 个套件）
- XtQuantManager HTTP 网关：多账号注册 + 健康检查 + Fail-Safe 重连
- 非 XtQuantManager 场景的 QMT 重连机制（事件 / 循环 / 主动探测三条路径）
- 盘前 9:25 自动重新初始化 xtquant 接口

### Fixed
- baostock 登录无超时保护导致监控线程阻塞约 168 秒
- 止盈触发标志写入后 positions_cache 未失效导致 10 秒窗口内重复信号
- `qmt_connected` 初始化后永不更新（永久假健康）
- `easy_qmt_trader` 缺少 `reconnect_xttrader()` 方法
- 线程监控未注册 `heartbeat_check`，无法感知 API 断连

### Changed
- 线程注册统一使用 `lambda` 获取最新对象引用，避免重启后引用失效

---

## [1.0.0] - 2026-02-03

### Added
- 首个稳定版本
- 双层存储架构（内存数据库 + SQLite 持久化）
- 信号检测与执行分离设计
- 动态止盈止损策略（最高浮盈 5%/10%/15%/20%/30% 五档）
- 网格交易完整实现
- Web 前端实时监控界面（Flask + SSE）
- 多线程协同 + 线程自愈机制
- 模拟交易模式（无需 QMT 即可验证策略）
- 回归测试框架基础设施

[Unreleased]: https://github.com/weihong-su/miniQMT/compare/v3.6.0...HEAD
[3.6.0]: https://github.com/weihong-su/miniQMT/compare/v3.5.0...v3.6.0
[3.5.0]: https://github.com/weihong-su/miniQMT/compare/v3.4.0...v3.5.0
[3.4.0]: https://github.com/weihong-su/miniQMT/compare/v3.3.0...v3.4.0
[3.3.0]: https://github.com/weihong-su/miniQMT/compare/v3.2.0...v3.3.0
[3.2.0]: https://github.com/weihong-su/miniQMT/compare/v3.1.0...v3.2.0
[3.1.0]: https://github.com/weihong-su/miniQMT/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/weihong-su/miniQMT/compare/V2.0.0-Beta...v3.0.0
[2.0.0-Beta]: https://github.com/weihong-su/miniQMT/compare/V1.0.0...V2.0.0-Beta
[1.0.0]: https://github.com/weihong-su/miniQMT/releases/tag/V1.0.0
