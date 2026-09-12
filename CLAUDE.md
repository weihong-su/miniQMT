# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

miniQMT 是一个基于迅投QMT API的**无人值守量化交易系统**,实现自动化交易策略执行、持仓管理、止盈止损和网格交易。

**核心特性**:
- 🔄 双层存储架构(内存数据库 + SQLite持久化)
- 🎯 信号检测与执行分离设计
- 🧵 多线程协同工作 + 线程自愈机制
- 📈 动态止盈止损策略（含 xtquant_manager 独立运行模式）
- 🤖 自动买入模块（独立进程，复用 Web 买入 API 下单）
- 🌐 Web前端实时监控界面（Flask web1.0 + Vue3 web2.0 双版本）
- 🚪 XtQuantManager HTTP 网关（多账号统一管理、远程 API、PWA 支持）
- 🛡️ 无人值守运行(线程监控、超时保护、优雅关闭)
- 🔀 **交易通道四选一**：默认 miniQMT xttrader 直连，可选 XtQuantManager / 大QMT 文件 IPC / 大QMT RPC(Redis/ZMQ)，控制台可一键切换直连/IPC/RPC
- 📊 **Tushare Pro 数据源**（历史K线优先 + 股票名称补充查询，标准模式 Tushare → Mootdx 降级链）
- 🔧 **.env fallback 配置**（Windows 用户级环境变量 > `.env`，零依赖自解析，测试隔离保护）

**v3.7.0 新增配置开关**:
- `ENABLE_QMT_RPC_FALLBACK` — 大QMT RPC 交易通道开关（Redis/ZMQ RPC，默认 false）
- `QMT_RPC_TRANSPORT` — RPC 传输方式：redis(默认) / zmq(低延迟) / mysql(兜底)
- `QMT_RPC_REDIS_HOST` / `PORT` / `DB` / `PASSWORD` — Redis 连接参数
- `QMT_RPC_ALLOW_ORDER` — RPC 下单二次确认开关（默认 false，只读安全）
- `ENABLE_QMT_IPC_FALLBACK` — 大QMT文件IPC交易通道开关（已有，默认 false）
- `QMT_IPC_ROOT` — IPC 文件目录（已有，默认 `C:\QuantIPC`）
- `.env` 文件现由 `config.py` 自动加载（`_load_dotenv_fallback`），优先级：Windows 环境变量 > .env
- 控制台 `miniqmt.bat` 菜单 `[n]` Tushare / `[o]` IPC / `[p]` XtTrader 通道总控 可直接切换
- **三个可选后端互斥**：`ENABLE_XTQUANT_MANAGER` / `ENABLE_QMT_IPC_FALLBACK` / `ENABLE_QMT_RPC_FALLBACK` 同时最多开一个；都不开时使用默认 xttrader 直连
- 详细配置参考 [docs/site/miniqmt/configuration.md](docs/site/miniqmt/configuration.md) 配置全景图

**隐私安全提醒**:
- ⚠️ **绝不硬编码任何 Token/密码/账号ID** — 一律使用环境变量或配置文件
- `account_config.json` 已在 `.gitignore` 中排除
- Pushplus Token 使用 `PUSHPLUS_TOKEN` 环境变量
- Redis 密码使用 `QMT_RPC_REDIS_PASSWORD` 环境变量（`.env.example` 为占位值）

**环境要求**:
- Python 3.8+ (推荐 3.9)，例如用户目录下的Anaconda3/envs/python39
- 操作系统: Windows (QMT仅支持Windows)
- QMT客户端: 实盘交易需要安装并登录QMT

**依赖安装**:
```bash
pip install -r utils/requirements.txt
```

## ⚠️ 关键约束 - 违反将导致系统故障

**执行任何代码修改前必须遵守**:

1. **配置集中管理**: 所有可配置参数在 [config.py](config.py) 中,严禁硬编码魔法数字
2. **模拟交易优先**: 测试新功能前必须设置 `ENABLE_SIMULATION_MODE = True`
3. **线程安全**: 修改共享数据必须使用 `threading.Lock()` 保护
4. **信号验证**: 交易信号必须经过 `validate_trading_signal()` 验证,防止重复执行
5. **双层存储同步**: 修改内存数据库后必须调用 `_increment_data_version()`
6. **线程注册规范**: 注册线程监控时必须使用 `lambda` 获取线程对象(见下文)
7. **Git操作**: 除非用户明确要求,不要主动执行git提交和分支操作

## 快速开始

### 环境准备(推荐)
```bash
# 创建虚拟环境
python -m venv venv

# 激活虚拟环境
# Windows:
venv\Scripts\activate
# Linux/Mac:
source venv/bin/activate

# 安装依赖
pip install -r utils/requirements.txt

# 验证安装
python utils/check_dependencies.py
```

### 配置文件
创建 `account_config.json` 和 `stock_pool.json` (参见文档末尾)。
配置项可通过 `.env` 文件或 Windows 环境变量覆写，优先级：**环境变量 > .env**。复制 `.env.example` 为 `.env` 并修改即可。
Web 远程/局域网/公网映射访问前必须设置强随机 `QMT_API_TOKEN`；配置后所有 web1.0 `/api/*`（含 GET/SSE）都需要 `X-API-Token`。公网映射建议同时设置 `WEB_PUBLIC_MODE=true`。

### 启动系统
```bash
python main.py
```
**首次运行**: 系统会自动创建 `data/positions.db` 数据库文件

### 运行测试

#### 回归测试框架 (推荐)

项目集成了完整的回归测试框架 ([test/run_integration_regression_tests.py](test/run_integration_regression_tests.py))，支持按模块运行、快速验证和失败重试。测试组配置在 [test/integration_test_config.json](test/integration_test_config.json)。

```bash
# 快速验证（5分钟内完成，检查关键功能）
python test/run_integration_regression_tests.py --fast

# 运行所有回归测试
python test/run_integration_regression_tests.py --all

# 运行所有回归测试，并包含 fast 组
python test/run_integration_regression_tests.py --all-with-fast

# 按组运行
python test/run_integration_regression_tests.py --group autobuy             # 自动买入
python test/run_integration_regression_tests.py --group system_integration  # 系统集成
python test/run_integration_regression_tests.py --group stop_profit         # 止盈止损
python test/run_integration_regression_tests.py --group grid_signal         # 网格信号
python test/run_integration_regression_tests.py --group grid_session        # 网格会话
python test/run_integration_regression_tests.py --group grid_trade          # 网格交易
python test/run_integration_regression_tests.py --group grid_exit           # 网格退出
python test/run_integration_regression_tests.py --group grid_validation     # 网格参数
python test/run_integration_regression_tests.py --group grid_comprehensive  # 网格综合
python test/run_integration_regression_tests.py --group grid_bug_regression # Bug回归验证
python test/run_integration_regression_tests.py --group grid_full_range_coverage  # 全区间覆盖
python test/run_integration_regression_tests.py --group grid_true_pnl       # 真实盈亏账本
python test/run_integration_regression_tests.py --group simulation_trading_e2e  # 模拟交易端到端

# 其他选项
python test/run_integration_regression_tests.py --all --retry-failed   # 失败重试
python test/run_integration_regression_tests.py --all --verbose        # 详细输出
python test/run_integration_regression_tests.py --all --skip-env-prep  # 跳过环境准备
python test/run_integration_regression_tests.py --all --no-backup      # 不备份生产DB
```

测试报告自动输出到 `test/integration_test_report.json` 和 `test/integration_test_report.md`。

#### 单个测试文件

```bash
# 运行单个测试模块
python test/run_single_test.py test.test_unattended_operation

# 直接使用 unittest
python -m unittest test.test_system_integration -v

# 运行全部网格测试
python test/run_all_grid_tests.py
```

### Web前端
浏览器访问: `http://localhost:5000`
- **web1.0**: Flask 模板渲染 (`web1.0/`), 自动运行
- **web2.0**: Vue3 + Vite + TypeScript + PWA (`web2.0/`), 需构建后使用

#### web2.0 开发与构建
```bash
cd web2.0
npm install                          # 安装依赖（仅首次）
npm run dev                          # 开发模式 (http://localhost:5173, 热更新)
npm run build                        # 生产构建 → dist/
```
构建产物 `dist/` 可直接部署到 Vercel 或由 Flask web_server / xtquant_manager 托管。
详见 [web2.0/VERCEL_DEPLOY.md](web2.0/VERCEL_DEPLOY.md)。

#### Web 双模式架构

web2.0 支持两种后端连接模式，通过前端「连接设置」切换：

| 模式 | 后端 | 端口 | 适用场景 |
|------|------|------|---------|
| **Flask 直连** | 每账号独立 Flask (web_server.py) | :5000, :5001... | 完整功能：配置管理/自动操作总开关/模拟买入/初始化持仓 |
| **网关模式** | xtquant_manager 统一入口 | :8888 | 多账号只读监控 + 下单；配置/监控/初始化需 Flask 直连 |

**网关模式能力边界**（截至 2026-08-09）:
- ✅ 多账号持仓查看（字段完整、账号隔离、现价反推）
- ✅ 账户资产/状态、连接状态、交易记录
- ✅ 参数展示（只读）、动态止盈状态、网格会话只读列表
- ✅ 账号自动发现（从 xtquant_manager 同步真实 ID）
- 🔑 **所有数据端点（含只读）均需 `X-API-Token`** — 只读端点会返回持仓成本/盈亏/成交明细，属财务隐私数据；唯一例外是 `/api/v1/health`（存活探针，无 Token 时只返回计数、不返回 `accounts` 明细）
- 🔒 配置保存/自动操作总开关/模拟买入/初始化持仓 — **需 Flask 直连模式**
- 🔒 SSE 实时推送不可用 — 依赖 3s/10s 轮询更新

技术要点:
- 网关兼容端点 (`/api/positions`, `/api/status` 等) 在 [server.py](xtquant_manager/server.py) 中实现
- 网格会话兼容端点 `/api/grid/sessions` 在网关模式下从账号 SQLite 只读返回，盈亏口径为兼容降级快照
- QMT 实时数据 (量/价/市值) + SQLite 持久化元数据 (名称/建仓/止损/止盈) 合并返回
- 前端通过 `X-Account-Id` 请求头切换目标账号，`isGatewayMode()` 检测当前模式
- 网关默认 `trust_proxy=false`，不信任 `X-Forwarded-For`：该头可伪造成 `127.0.0.1` 冒充本机，一次绕过 Token 免验证 / IP 白名单 / 速率限制三处判定。仅在受信任反向代理之后才可在 `xtquant_manager_config.json` 置 `"trust_proxy": true`
- `_launcher.py` 菜单选项 [7]/[9] 启动时记忆 web 模式偏好 (`data/.web_mode`)
- 网关重启 (菜单 [h]) 自动等待端口释放防止旧进程残留

### miniqmt.bat 总控制台
```bash
miniqmt.bat                         # 打开交互式菜单
python scripts/_launcher.py menu    # 等效命令
```

**菜单功能一览**（分页：首页放日常运行 + 三个二级页，一屏装得下）:

| 页 | 选项 | 功能 |
|----|------|------|
| **首页** | [5]-[6] | 查看账号配置、运行状态 |
| | [7]-[9] | 启动所有(实盘)/所有(模拟)/指定账号 |
| | [a]-[c] | 优雅停止全部/指定、强制停止全部 |
| | [1] | → **环境与部署**页 |
| | [2] | → **服务管理**页 |
| | [3] | → **数据与配置**页 |
| 环境与部署 | [0]-[4] | 首次向导、检查环境、装依赖、校验配置、git pull |
| 服务管理 | [d]-[i] | XtQuantManager 网关 启动/停止/状态/UI/重启/日志 |
| | [j]-[m] | 自动买入 启动/停止/状态/日志 |
| 数据与配置 | [n]-[p] | Tushare / 大QMT IPC / XtTrader 通道总控 |
| | [r]-[u] | 交割单：迁移 / 历史回填 / 导入对账单 / 导出 |

> 二级页内 **`[b]` 或直接回车返回主菜单**；`[q]` 在任何页都退出。
> 各页按键不重叠（env 用 0-4 / 首页用 5-9,a-c / services 用 d-m / data 用 n-p,r-u），
> 因此底层分派链是共用的，只在入口按页校验按键。
> 首页底部显示账号运行摘要，执行过可能改变运行状态的操作后会重新采集（有缓存）。
>
> `[r][s][t]` 会改写 `trade_records`，内置**停机守卫**：仍有账号运行时直接拒绝执行，
> 且强制先跑 dry-run 预演、需输入 `yes` 才正式执行。`[u]` 导出是只读，随时可跑。
> 对应 CLI：`python scripts/_launcher.py settlement-{migrate,backfill,import,export}`

### 系统诊断工具
```bash
# 检查系统状态
python -m unittest test.test_system_integration -v

# 诊断QMT连接
python -m unittest test.test_qmt_connection -v

# 查看实时日志
tail -f logs/qmt_trading.log  # Linux/Mac
Get-Content logs/qmt_trading.log -Wait  # Windows PowerShell
```

## 核心架构

### 关键设计原则

**1. 信号检测与执行分离** (最重要!)
```
持仓监控线程(始终运行) → 检测信号 → latest_signals队列
                                        ↓
策略执行线程 → 检查ENABLE_AUTO_OPERATION + ENABLE_AUTO_TRADING → 执行/忽略信号
网格交易线程 → 检查ENABLE_AUTO_OPERATION + ENABLE_GRID_TRADING + grid_trading_sessions.enabled → 执行/暂停新网格单
```

**关键点**:
- 监控线程**始终运行**,持续检测信号
- `ENABLE_AUTO_OPERATION` 是全局自动操作总开关，关闭时所有自动策略不产生新交易动作
- `ENABLE_AUTO_TRADING` 只控制动态止盈止损等非网格自动策略
- **动态止盈止损信号入队门控**：监控线程仅在 `ENABLE_DYNAMIC_STOP_PROFIT` 且 `ENABLE_AUTO_TRADING` 同时开启、**且该股 `positions.stop_profit_enabled` 为真**时才检测并写入 `latest_signals`（`_detect_and_enqueue_dynamic_signal`）。任一关闭时不检测/不入队，避免"检测→策略因自动交易关闭而清除→再检测"的每 3 秒日志刷屏。网格检测走独立分支（`ENABLE_GRID_TRADING`），不受此门控影响
- **个股级动态止盈止损开关**：`positions.stop_profit_enabled`（默认 1=开）与全局开关是 AND 关系，可在 web1.0 持仓列表末列拨动开关单独暂停某只股票；写入走 `PositionManager.set_stop_profit_enabled()`（仿 `set_session_enabled`，只更新单列 + `_increment_data_version()`，不动 `update_position`）。网关模式 `xtquant_manager/stop_profit.py` 同样遵守该开关
- `ENABLE_GRID_TRADING` 控制网格模块，`grid_trading_sessions.enabled` 控制单只股票网格会话“自动/暂停”
- `ENABLE_PAUSE_GRID_AFTER_TAKE_PROFIT_FULL`（默认 True）控制**清仓成交确认后**的网格联动：`take_profit_full` 与 `stop_loss`（两者都卖出 `available` 全量）成交后只暂停同股活跃网格会话（`enabled=False`），不停止/删除会话，也不修改任何网格配置（中心价/档位/投入上限/有效期），便于人工复核后原样恢复
- 每个信号都要经过 `validate_trading_signal()` 验证,防止重复执行

**2. 双层存储架构**
```
实盘模式:
QMT实盘账户 → position_manager.qmt_trader.position() → 内存数据库
内存数据库 → 定时同步(15秒) → SQLite数据库

模拟模式:
Web界面 → trading_executor → position_manager.simulate_buy/sell() → 内存数据库
(跳过QMT接口,资金从SIMULATION_BALANCE扣除/增加)
```

**关键点**:
- 内存数据库存储高频更新数据(价格、市值、盈亏比例)
- SQLite持久化关键状态(开仓日期、止盈触发标记、最高价)
- 修改内存数据后必须调用 `_increment_data_version()` 触发前端更新

### 模块职责

```
config.py              # 集中配置管理(所有魔法数字都在这里)
logger.py              # 统一日志管理
db_migrate.py          # 数据库 schema 迁移(幂等补列/迁移前备份/测试库守卫/账号发现) ⭐
settlement_db.py       # 交割单数据落库(持仓快照/每日净值/运行事件/成交统一写入口) ⭐
main.py                # 系统启动入口和线程管理
thread_monitor.py      # 线程健康监控与自愈（无人值守核心）⭐
data_manager.py        # 历史数据获取(xtdata接口)
indicator_calculator.py # 技术指标计算
position_manager.py    # 持仓管理核心(内存+SQLite双层)⭐
trading_executor.py    # 交易执行器(模拟/实盘下单入口，实盘通道由 position_manager 工厂选择)
strategy.py            # 交易策略逻辑⭐
web_server.py          # RESTful API服务(Flask)
easy_qmt_trader.py     # QMT交易API封装 (xttrader 直连)
qmt-trader/            # 大QMT 降级交易通道
  _qmt_trader_base.py  #   IPC/RPC 共享件 (列名/Fake对象/纯逻辑)
  qmt_rpc_trader.py    #   大QMT RPC 适配器 (Redis/ZMQ RPC 驱动大QMT)  [v3.7.0]
  qmt_ipc_trader.py    #   大QMT 文件IPC 适配器 (JSON文件驱动大QMT)
  qmt_trade_executor.py #   大QMT executor 脚本 (模型交易策略入口)
  qmt_trade_client.py  #   策略端客户端库
premarket_sync.py      # 盘前同步与初始化(每天9:25重新初始化xtquant)
config_manager.py      # 配置持久化管理
sell_monitor.py        # 卖出委托单超时监控与撤单⭐
grid_trading_manager.py # 网格交易会话管理(独立线程)
grid_database.py       # 网格交易数据持久化(SQLite)
grid_validation.py     # 网格交易参数校验
autobuy/               # 自动买入模块(独立进程，候选池筛选→Web买入API)
xtquant_manager/       # XtQuantManager HTTP网关(多账户管理，可选)
xtquant_manager/stop_profit.py  # 网关模式动态止盈止损(后台线程，复用 position_manager 算法)
web2.0/                # Vue3+Vite+TS+PWA 新版Web界面
test/test_xqm_flask_compat.py  # 网关Flask兼容端点测试(21用例,字段映射/账号隔离/SQLite注入)
```

### 线程架构

| 线程 | 启动位置 | 职责 | 频率 | 关键配置 |
|------|---------|------|------|---------|
| 线程监控 | `thread_monitor.start()` | 检测线程崩溃并自动重启 | 60秒 | `ENABLE_THREAD_MONITOR` |
| 数据更新 | `data_manager.start_data_update_thread()` | 更新股票池行情 | 60秒 | - |
| 持仓监控 | `position_manager.start_position_monitor_thread()` | 同步实盘持仓、更新价格、检测止盈止损 | 3秒 | `MONITOR_LOOP_INTERVAL` |
| 策略执行 | `strategy.start_strategy_thread()` | 获取非网格信号、执行交易 | 5秒 | `ENABLE_AUTO_OPERATION` + `ENABLE_AUTO_TRADING` |
| 网格交易 | `grid_trading_manager` 内部线程 | 网格信号检测与买卖执行 | 5秒 | `ENABLE_AUTO_OPERATION` + `ENABLE_GRID_TRADING` + `grid_trading_sessions.enabled` |
| 卖出监控 | `sell_monitor` 单例线程 | 委托单超时撤单 | 2秒 | `ENABLE_SELL_MONITOR` |
| 定时同步 | `position_manager.start_sync_thread()` | 内存→SQLite同步 | 15秒 | `POSITION_SYNC_INTERVAL` |
| Web服务 | `web_server.start_web_server()` | RESTful API | 持续 | - |
| 心跳日志 | `start_heartbeat_logger()` | 定期输出系统运行状态 | 1800秒 | `ENABLE_HEARTBEAT_LOG` |
| 盘前同步 | `premarket_sync.start_premarket_sync_scheduler()` | 每天9:25重新初始化xtquant | 每日9:25 | `ENABLE_PREMARKET_XTQUANT_REINIT` |

## 关键配置

### 功能开关 (config.py)

```python
# 核心开关
ENABLE_SIMULATION_MODE = True   # True=模拟, False=实盘 ⚠️
ENABLE_AUTO_OPERATION = False   # 全局自动操作总开关 ⚠️
ENABLE_AUTO_TRADING = False     # 非网格自动策略执行开关
ENABLE_DYNAMIC_STOP_PROFIT = True  # 止盈止损功能
ENABLE_GRID_TRADING = True      # 网格交易功能
ENABLE_PAUSE_GRID_AFTER_TAKE_PROFIT_FULL = True  # 全仓止盈/止损成交后暂停同股网格会话
ENABLE_MACD_SELL = False     # MACD技术指标卖出开关（False=仅记录信号不实盘卖出，2026-08-19修复price_type后新增）
ENABLE_THREAD_MONITOR = True    # 线程健康监控（无人值守必需）⭐
ENABLE_SELL_MONITOR = True      # 卖出委托单超时监控
ENABLE_XTQUANT_MANAGER = False  # XtQuantManager HTTP网关（多账户时开启）
DEBUG = False                   # 调试模式
```

### MACD 技术指标信号的时效语义 ⏱️

`check_buy_signal()` / `check_sell_signal()`（[indicator_calculator.py](indicator_calculator.py)）基于
**最近两根已收盘日线**判定「MACD 金叉/死叉 + 均线多头/空头排列」，存在**固有 T+1 延迟**：

- 当日日线要到 `HISTORY_TODAY_DAILY_AVAILABLE_AFTER`（默认 15:30）之后才入库
  （`data_manager._get_completed_history_end_date()` 保证不用盘中未定型数据）
- 但数据更新线程与策略线程都只在 `is_trade_time()` 内运行（A股 15:00 收盘）
- **结论**：T 日收盘产生的死叉，实际在 **T+1 开盘后**才被检测并执行。这是日线策略的
  正常行为，不是缺陷；但开启 `ENABLE_MACD_SELL=True` 前必须知悉此延迟

**信号去重与开关语义**（2026-08-20 修复）:
- `processed_signals` **仅作用于 MACD 技术指标信号**（`check_buy_signal`/`check_sell_signal`），
  按 `buy_/sell_{code}_{YYYYMMDD}` 去重，每股每日各最多执行一次；由
  `_rollover_signal_cache_if_new_day()` 在**跨交易日时自动清空**（无人值守长跑防内存增长）
- ⚠️ **止盈/止损/补仓/网格不受 `processed_signals` 约束**，它们走
  `position_manager.latest_signals` 队列 + `mark_signal_processed()`（仅出队、不写日级标记），
  基于**实时现价**每 3 秒重新检测。因此同一只股票**同一天内可以先止盈、后止损**——
  [position_manager.py](position_manager.py) 中 `stop_loss_1`（首次止盈后回落触发止损）
  正是为该场景设计的分支
- MACD 之所以按日去重：其信号源是**已收盘日线**柱的符号翻转，盘中不变；不去重会导致
  同一个死叉被每 10 秒重复执行
- `ENABLE_MACD_SELL=False` 或 `ENABLE_AUTO_TRADING=False` 期间，信号只写入独立的
  `macd_sell_notified` 降噪集合，**不写 `processed_signals`** —— 因此盘中把开关改为 True
  后，当日信号**无需重启进程**即可立即生效
- MACD 卖出使用 `position['available']`（可用持仓）下单，与止盈止损口径一致，
  避免 T+1 冻结股份触发 QMT 拒单

**⚠️ 实盘交易前必须检查**:
1. `ENABLE_SIMULATION_MODE = False` (切换到实盘)
2. `ENABLE_AUTO_OPERATION = True` (打开全局自动操作总开关)
3. 按需打开分开关：`ENABLE_AUTO_TRADING=True` 和/或 `ENABLE_GRID_TRADING=True`
4. QMT客户端已启动并登录
5. `account_config.json` 配置正确

### 无人值守运行配置 ⭐

```python
# 线程监控
ENABLE_THREAD_MONITOR = True      # 启用线程自愈
THREAD_CHECK_INTERVAL = 60        # 检查间隔(秒)
THREAD_RESTART_COOLDOWN = 60      # 重启冷却时间(秒)

# 持仓监控优化
MONITOR_LOOP_INTERVAL = 3         # 监控循环间隔(秒)
MONITOR_CALL_TIMEOUT = 8.0        # API调用超时(秒)
MONITOR_NON_TRADE_SLEEP = 60      # 非交易时段休眠(秒)

# 性能优化
QMT_POSITION_QUERY_INTERVAL = 10.0  # QMT持仓查询间隔(秒)
POSITION_SYNC_INTERVAL = 15.0       # SQLite同步间隔(秒)
```

### 止盈止损配置

```python
STOP_LOSS_RATIO = -0.075  # 止损比例: 成本价下跌7.5%
INITIAL_TAKE_PROFIT_RATIO = 0.06  # 首次止盈: 盈利6%
INITIAL_TAKE_PROFIT_RATIO_PERCENTAGE = 0.6  # 首次止盈卖出60%

# 动态止盈 (最高盈利比例, 止盈位系数)
DYNAMIC_TAKE_PROFIT = [
    (0.05, 0.96),  # 最高浮盈5%时,止盈位=最高价*96%
    (0.10, 0.93),
    (0.15, 0.90),
    (0.20, 0.87),
    (0.30, 0.85),
]

# 动态信号保活 (防止瞬时止盈信号在被策略线程消费前丢失)
# 监控线程 3 秒检测一次，策略线程单只股票消费周期约 10+持仓数+股票池数 秒；
# 首次止盈是"跨过即触发"的瞬时信号，价格回踩会让下一轮检测返回 None。
# 开启后已入队未消费的信号在窗口内不被删除；执行前再按 MAX_AGE 做时效兜底，
# 避免以过旧的价格快照下单。
ENABLE_DYNAMIC_SIGNAL_KEEPALIVE = True   # 信号保活开关
DYNAMIC_SIGNAL_KEEPALIVE_SECONDS = 90    # 保活窗口(秒)
DYNAMIC_SIGNAL_MAX_AGE_SECONDS = 120     # 执行前信号最大年龄(秒)，超过判定过期
```

## 数据库表结构

### positions (持仓表)

**数据来源分类**:

| 字段 | 数据来源 | 更新时机 |
|------|---------|---------|
| `stock_code`, `volume`, `available`, `cost_price` | QMT实盘 `qmt_trader.position()` | 每10秒同步一次 |
| `current_price` | `data_manager.get_latest_data()` | 实时更新 |
| `market_value`, `profit_ratio` | 计算得出 | 价格更新时重新计算 |
| `open_date`, `profit_triggered`, `highest_price`, `stop_loss_price`, `stop_profit_enabled` | 持久化字段 | 策略状态变化或成交回报确认后同步到SQLite；`stop_profit_enabled` 由 Web 开关即时写入 |

**关键字段说明**:
- `profit_triggered`: 是否已完成首次止盈(卖出60%)成交确认,影响后续动态止盈逻辑
- `highest_price`: 持仓期间最高价,用于计算动态止盈位
- `stop_loss_price`: 止损价格,低于此价格触发止损

### trade_records (交易记录表)

记录所有买卖交易,包含:
- `stock_code`, `trade_type` (BUY/SELL), `price`, `volume`
- `trade_id`: 订单ID (实盘为QMT返回的order_id, 模拟为 `SIM{timestamp}{counter}`)
- `strategy`: 策略标识 (`simu`/`auto_partial`/`auto_full`/`stop_loss`/`grid`/`manual`/`M_real`/`M_simu`/`manual_real`/`manual_simu`/`external`/`default`/`add_position`/`reorder_take_profit_half`/`reorder_take_profit_full`/`reorder_stop_loss`)
  - Web 显示标签映射需**三处同步**：[web_server.py](web_server.py) `strategy_labels`（服务端下发 `strategy_label`，web2.0 Flask 直连模式优先取此值）、[web1.0/script.js](web1.0/script.js) `LOG_STRATEGY_LABELS`（只认原始 `strategy`）、[web2.0/src/components/OrderLog.vue](web2.0/src/components/OrderLog.vue) `strategyLabels`（网关模式兜底，因网关不下发 `strategy_label`）
  - 手动买卖：`M_real`=手买 / `M_simu`=模买 / `manual_real`=手卖 / `manual_simu`=模卖
  - `reorder_*` 由 `position_manager._reorder_after_cancel()` 以 `f"reorder_{signal_type}"` **动态拼接**产生（委托超时撤单后自动重挂）。新增卖出信号类型时，这三处标签表也要同步补 `reorder_` 前缀的键，否则前端会回退显示英文原始值

## 交割单数据管道 ⭐

归因所需的一切必须在**成交当下**落库，导出退化为纯 SELECT。相关模块见
[db_migrate.py](db_migrate.py) 与 [settlement_db.py](settlement_db.py)。

**新增表**（均为幂等 `CREATE TABLE IF NOT EXISTS`）:
`position_snapshot`（每交易日 09:25 open / 15:05 close 全量持仓）、
`account_equity_daily`（每日净值 + 恒等式校验 + 跳变告警）、
`run_events`（结构化事件）、`trade_records_sim`（模拟成交独立表）。

**trade_records 扩展列**: `account` / `deal_time` / `deal_time_str` / `time_source` /
`order_id` / `fills` / `strategy_label` / `is_simulation` / `commission_source` /
`row_status` 等 17 列。

**核心约定**:
- 成交写入统一走 `settlement_db.record_trade()`，用 `INSERT OR IGNORE` + 唯一索引
  `ux_trade_records_deal` 做**原子**幂等。历史上一笔成交被写两遍，正是因为判重是
  「先 SELECT 再 INSERT」且当时没有唯一索引
- `time_source` 取值：`exchange` / `local_fallback` / `reconcile_backfill` / `broker`。
  **取不到交易所成交时间时如实标 `local_fallback`，禁止用 `now()` 冒充**
- 唯一索引必须写成 `(COALESCE(account,''), trade_id, stock_code, trade_time)`：
  SQLite 唯一索引中 NULL 互不相等，历史行 `account` 全为 NULL 时索引会形同虚设
- 网格路径写入的 `trade_id` 是 `str(order_id)` 而非成交号，`order_id` 跨股跨日复用，
  所以索引**必须带 `stock_code`**
- 模拟成交落 `trade_records_sim`，绝不与实盘同表

**上线顺序（不可颠倒）**:
```bash
# 1. 停止 miniQMT，确认无残留 python 进程
tasklist | findstr python
# 2. 预演（只读，不会写库）
python scripts/migrate_settlement.py --accounts all --dry-run
# 3. 正式迁移（自动备份到 data/backup/migrations/）
python scripts/migrate_settlement.py --accounts all
# 4. 重启，确认日志出现「启动收盘快照线程」
```
⚠️ **迁移未跑就部署新代码**：`record_trade()` 会自动降级为旧列写入（成交不丢，
但归因字段缺失）并在日志告警。反之，**占位流水未清理就建唯一索引会直接 IntegrityError**。

**导出**:
```bash
python scripts/export_settlement.py --start 2026-09-14 --end 2026-10-10 --accounts all --out export/
```
只读、不依赖 `logs/*.log`；禁止在导出时剔除任何股票；`positions_begin` 无快照时
**报错退出，不许用 0 填充**。合并规则 `merge_deals()` 是全项目单点实现（10 秒窗口、不跨日界）。

**历史回填**（把改造前的存量行补齐归因字段）:
```bash
python scripts/backfill_trade_records.py --accounts all --dry-run   # 先看
python scripts/backfill_trade_records.py --accounts all
```
`time_source` 一律标 `local_fallback` —— **绝不伪装 `exchange`**；手续费来源按证据判定
（`amount×0.0003` 可证实是旧版写死的估算会被重算，来源不明的标 `unknown` 且不动）。

**券商对账单导入**（历史成交时间的**唯一**来源 —— xttrader 没有历史成交查询接口）:
```bash
python scripts/import_broker_statement.py --dir "<对账单目录>" --dry-run
python scripts/import_broker_statement.py --dir "<对账单目录>"
```
对账单为 **GBK** 编码。匹配按三级优先级：① 成交编号 == `trade_id`
② 订单编号 == `trade_id` **且代码相同**（order_id 会跨股跨日复用，必须消歧）
③ 代码+方向+价量相同且时间邻近（默认 60 秒）。
回填 `time_source='broker'`；**对账单手续费为 0 时不覆盖本地估算**（0.00 通常表示该字段未导出）。
`broker_deals` / `broker_orders` 留存原始对账数据供审计。

## 无人值守运行 ⭐

系统支持长期持续运行,通过线程健康监控实现自动恢复。详见 [在线文档 · 无人值守运行](docs/site/miniqmt/unattended.md)

### 线程自愈机制

**关键实现** ([thread_monitor.py](thread_monitor.py)):

```python
from thread_monitor import get_thread_monitor

# 在main.py中启动线程监控
if config.ENABLE_THREAD_MONITOR:
    thread_monitor = get_thread_monitor()

    # ⚠️ 必须使用lambda获取最新对象引用
    thread_monitor.register_thread(
        "持仓监控",
        lambda: position_manager.monitor_thread,  # ✅ 正确: lambda
        position_manager.start_position_monitor_thread
    )

    thread_monitor.start()
```

**❌ 常见错误**:
```python
# 错误: 直接传递线程对象,重启后对象引用会变化
monitor.register_thread(
    "持仓监控",
    position_manager.monitor_thread,  # ❌ 错误: 直接传递对象
    restart_func
)
```

**工作原理**:
- 每60秒检查一次线程存活状态
- 检测到崩溃立即重启,60秒冷却时间防止重启风暴
- 完整的重启历史记录

### 优雅关闭流程

系统退出时的正确关闭顺序(在 [main.py](main.py) 的 `cleanup()` 函数中实现):

```
1. Web服务器 → 停止接收新请求
2. 线程监控器 → 停止监控循环,避免误触发重启
3. 业务线程 → 停止数据更新、持仓监控、策略执行
4. 核心模块 → 按依赖顺序关闭(策略→执行器→数据管理器/数据库)
```

**重要**: 每个关闭步骤都有独立的异常处理,确保单个步骤失败不影响其他资源清理。

### 超时保护

持仓监控线程中的API调用有超时保护（当前默认 8 秒）:

```python
try:
    future.result(timeout=config.MONITOR_CALL_TIMEOUT)  # 默认8秒
except TimeoutError:
    logger.warning("API调用超时,跳过本次更新")
    # 继续执行,不阻塞循环
```

### 非交易时段优化

```python
# 非交易时段立即跳过,避免无效API调用
if not config.is_trade_time():
    logger.debug(f"非交易时间(第{loop_count}次循环), 休眠60秒")
    time.sleep(60)
    continue
```

**效果**: 非交易时段CPU占用从~30%降至<2%

## 开发规范

### 1. 配置参数 - 严禁硬编码

```python
# ❌ 错误: 硬编码魔法数字
if profit_ratio > 0.06:
    ...

# ✅ 正确: 使用配置
if profit_ratio > config.INITIAL_TAKE_PROFIT_RATIO:
    ...
```

### 2. 日志级别

- `logger.debug()` - 详细调试信息(变量值、执行路径)
- `logger.info()` - 关键流程节点(系统启动、交易执行)
- `logger.warning()` - 异常但可恢复(数据缺失、连接超时)
- `logger.error()` - 严重错误(模块初始化失败、数据库错误)

### 3. 异常处理 - 所有外部API调用必须包裹

```python
try:
    result = qmt_trader.order_stock(...)
    logger.info(f"下单成功: {result}")
except Exception as e:
    logger.error(f"下单失败: {str(e)}")
    return None
```

### 4. 线程安全 - 使用锁保护共享数据

```python
with self.signal_lock:
    self.latest_signals[stock_code] = signal_info
```

### 5. 数据库操作 - 使用参数化查询

```python
# ✅ 正确: 参数化查询
cursor.execute("SELECT * FROM positions WHERE stock_code=?", (stock_code,))

# ❌ 错误: 字符串拼接(SQL注入风险)
cursor.execute(f"SELECT * FROM positions WHERE stock_code='{stock_code}'")
```

### 6. 数据版本更新 - 修改内存数据后必须调用

```python
def simulate_buy_position(self, ...):
    # ... 执行模拟买入逻辑 ...
    self._increment_data_version()  # ⚠️ 必须调用,否则前端不更新
```

## 常见问题与解决方案

### 1. 止盈止损信号重复执行

**原因**: 信号验证失败或未正确标记为已处理

**解决**:
- 检查 `validate_trading_signal()` 和 `mark_signal_processed()` 调用链
- 查看日志中的信号验证详情
- 确认信号有效期过滤正常工作：信号入队时写入 `latest_signals[stock_code]['timestamp']`，`get_pending_signals()` 会过滤超过 300 秒的过期信号

### 2. 模拟交易持仓不更新

**原因**: 未触发数据版本号更新

**解决**:
```python
def simulate_buy_position(self, ...):
    # ... 执行模拟买入逻辑 ...
    self._increment_data_version()  # 必须调用
```

### 3. QMT连接断开

**检查**:
```python
# 检查连接状态
position_manager.qmt_trader.xt_trader.is_connected()

# 重新连接
position_manager.qmt_trader.connect()

# 检查路径配置
# config.py中的QMT_PATH是否正确
```

### 4. 持仓监控线程未运行

**排查**:
```python
# 1. 检查配置
config.ENABLE_POSITION_MONITOR  # 应为True

# 2. 检查线程状态
import threading
print(threading.enumerate())

# 3. 查看日志
# 搜索 "启动持仓监控线程" 或 "持仓监控线程异常"
```

### 5. 线程监控器未自动重启线程

**原因**: 使用了错误的线程注册方式

**正确做法**:
```python
# ❌ 错误: 直接传递线程对象
monitor.register_thread(
    "持仓监控",
    position_manager.monitor_thread,  # 重启后对象引用会变化
    restart_func
)

# ✅ 正确: 使用lambda获取最新对象
monitor.register_thread(
    "持仓监控",
    lambda: position_manager.monitor_thread,  # 每次获取最新引用
    restart_func
)
```

### 6. 系统退出时出现数据库错误

**原因**: 关闭顺序不正确,Web服务器在数据库关闭后仍在处理请求

**解决**: 确保 [main.py](main.py) 中的 `cleanup()` 函数按正确顺序关闭

**验证**: 退出系统时查看日志,应该看到有序的关闭步骤,无ERROR日志

### 7. 外部成交后系统卡死 / 重连线程僵住

**原因**: 在 QMT 回调线程内反向调用 QMT 同步接口(如 `qmt_trader.position()`),与回调线程互等死锁

**约束**（修改成交回调链路时必须遵守）:
- 外部成交（QMT 客户端手工下单等本机未发的委托）走 `_record_external_trade_after_callback()`，**只置零 `last_position_update_time`** 让下轮监控自己同步，绝不在回调里请求持仓快刷
- `_confirm_filled_order()` 仅在 `matched_key or order_info` 为真（确实匹配到本机委托）时才调 `_request_immediate_position_refresh()`
- 回调链路上写流水时用 `data_manager.get_stock_name(stock_code, allow_qmt_lookup=False)` 关闭 QMT 持仓回查

**相关保护**: `easy_qmt_trader._stop_trader_with_timeout()` 把 `xt_trader.stop()` 放进 daemon 线程并按 `config.QMT_STOP_TIMEOUT`(默认5秒)超时放弃，避免底层 stop 卡死拖垮重连


## Web API端点

### 核心端点

**系统状态**:
- `GET /api/status` - 获取系统运行状态
- `GET /api/connection/status` - 检查QMT连接状态

**持仓管理**:
- `GET /api/positions` - 获取所有持仓
- `GET /api/positions-all` - 获取全部持仓详情
- `GET /api/sse` - SSE实时推送数据

**交易操作**:
- `POST /api/actions/execute_buy` - 执行买入 (参数: strategy, quantity, stocks；自动买入模块也复用该路径)
- `POST /api/holdings/update` - 更新持仓参数（止盈标记/最高价/止损价）
- `POST /api/holdings/stop_profit` - 设置个股「动态止盈止损」开关（参数: stock_code, enabled）

**网格交易**:
- `POST /api/grid/start` - 启动网格会话
- `POST /api/grid/stop/<session_id>` - 停止指定网格会话
- `GET /api/grid/sessions` - 获取所有网格会话（网关模式只读兼容）
- `GET /api/grid/trades/<session_id>` - 获取网格成交记录
- `GET /api/grid/ledger/<session_id>` - 获取网格真实账本详情（批次、LIFO配对、盈亏汇总）
- `GET /api/grid/status/<stock_code>` - 获取指定股票网格状态

**配置管理**:
- `GET /api/config` - 获取系统配置
- `POST /api/config/save` - 保存配置参数

## QMT API集成

### xtdata (行情接口)

```python
import xtquant.xtdata as xt

# 连接行情服务
xt.connect()

# 获取历史数据
xt.get_market_data(
    field_list=['open', 'high', 'low', 'close', 'volume'],
    stock_list=['000001.SZ'],
    period='1d',
    start_time='20230101',
    end_time='20231231'
)

# 获取实时Tick
xt.get_full_tick(['000001.SZ'])
```

### xttrader (交易接口)

```python
from xtquant.xttrader import XtQuantTrader
from xtquant.xttype import StockAccount

# 创建交易对象
xt_trader = XtQuantTrader(path, session_id)
xt_trader.start()

# 连接账户
acc = StockAccount(account_id, account_type)
xt_trader.connect()

# 下单
xt_trader.order_stock(
    acc,
    stock_code,
    order_type,  # 23=限价买入, 24=限价卖出
    order_volume,
    order_price
)

# 查询持仓
xt_trader.query_stock_positions(acc)

# 查询资产
xt_trader.query_stock_asset(acc)
```

## 调试技巧

### 启用详细日志
```python
# config.py
DEBUG = True
LOG_LEVEL = "DEBUG"
```

### 测试模拟交易
```python
# config.py
ENABLE_SIMULATION_MODE = True
DEBUG_SIMU_STOCK_DATA = True  # 绕过交易时间限制
```

### 监控关键数据
```python
# 查看内存持仓
position_manager.get_all_positions()

# 查看待执行信号
position_manager.get_pending_signals()

# 检查账户信息
position_manager.get_account_info()

# 查看信号队列
position_manager.latest_signals

# 查看线程监控状态
thread_monitor.get_status()
```

## 测试框架架构

测试代码位于 [test/](test/) 目录，使用标准 `unittest`。当前回归配置见 [test/integration_test_config.json](test/integration_test_config.json)，包含 32 个测试组（含 `fast` 快速子集）。

### 测试基础设施

- **[test/test_base.py](test/test_base.py)**: `TestBase` 基类，提供测试DB创建、持仓 fixture、线程断言、条件等待等工具方法
- **[test/test_mocks.py](test/test_mocks.py)**: `MockQmtTrader` 完整模拟 QMT API（连接、持仓查询、下单），无需真实 QMT 环境即可运行测试
- **[test/test_utils.py](test/test_utils.py)**: 通用测试辅助函数

### 测试分组

| 组名 | 优先级 | 内容 |
|------|--------|------|
| `autobuy` | high | 自动买入候选池/条件检查/防重/HTTP下单 |
| `system_integration` | critical | 系统集成、无人值守、线程监控 |
| `stop_profit` | high | 动态止盈止损策略（7个模块） |
| `grid_signal` | high | 网格信号检测与价格追踪 |
| `grid_session` | high | 网格会话生命周期管理 |
| `grid_trade` | high | 网格买卖执行与资金管理 |
| `grid_mece_regression` | critical | 网格状态机、并发预占、委托回调、真实账本、重启恢复边界 |
| `grid_exit` | high | 网格退出条件检测 |
| `grid_comprehensive` | high | 网格综合端到端场景 |
| `grid_validation` | medium | 参数校验与边界情况 |
| `grid_bugfix_c1` | critical | BUG-C1修复验证（冷却防重单、DESIGN-4设计约束） |
| `grid_bug_regression` | high | 4个已修复Bug的回归验证 |
| `order_rejection` | critical | QMT拒单保护与卖出冷却缩短 |
| `grid_qa_fixes` | high | MECE审查6个修复的验证 |
| `grid_max_investment_safety` | critical | max_investment三重防护验证 |
| `core_metrics` | high | 网格利润计算与风险分级 |
| `trader_callback` | critical | 卖出委托Callback兜底机制 |
| `web_api` | critical | RESTful API功能测试（含Bug修复回归） |
| `multi_account_isolation` | critical | 多账号配置/数据目录/端口隔离 |
| `launcher_deployment` | high | 总控制台环境检查/配置校验 |
| `db_thread_safety` | critical | 数据库线程安全与Web缓存验证 |
| `dual_layer_storage` | critical | 内存+SQLite双层存储一致性 |
| `xtdata_data_source` | high | xtdata动态订阅与fallback路径 |
| `indicator_calculator` | high | 技术指标计算器全方法验证 |
| `grid_qa_gap_supplement` | critical | QA缺口补充（信号优先级/最小卖出/position_snapshot降级） |
| `grid_full_range_coverage` | critical | 全网格区间覆盖（114个用例，A-K 11个套件） |
| `grid_true_pnl` | critical | 网格 True P&L / LIFO 真实盈亏验证 |
| `grid_simulation` | high | 价格模拟测试（30个用例） |
| `qmt_ipc_fallback` | high | 大QMT文件IPC降级通道（客户端/执行器/集成） |
| `qmt_rpc` | high | 大QMT RPC 交易后端（契约兼容、只读门禁、回调/委托映射） |
| `simulation_trading_e2e` | critical | 模拟交易模式端到端（核心链路/Web下单/策略四分支/模式切换） |
| `p1_fixes` | high | 重连缓存刷新/QMT自恢复探测/信号保活与时效兜底/超时泄漏可观测 |
| `fast` | critical | 快速验证子集（当前配置 43 个模块、1037 个用例） |

**测试统计（当前配置）**: 36组（含 `fast`）。`--all` 默认排除重复的 `fast` 组；最近一次（2026-09-12, v3.9.1）使用 Anaconda `python39` 执行 `--all-with-fast` 实测为 36组、146个模块、3126个用例，100% 通过；具体以本地运行报告为准。

### 编写新测试的规范

```python
from test.test_base import TestBase
from test.test_mocks import MockQmtTrader

class TestMyFeature(TestBase):
    def setUp(self):
        super().setUp()
        self.mock_trader = MockQmtTrader()
        self.mock_trader.add_mock_position("000001.SZ", volume=1000, cost_price=10.0)

    def test_something(self):
        # 测试代码...
        self.wait_for_condition(lambda: condition_met, timeout=5)
```

测试运行时自动备份生产DB，测试完成后恢复。使用 `--skip-env-prep` 跳过备份（仅限开发调试）。

## 相关文档

### 在线文档
- [无人值守运行](docs/site/miniqmt/unattended.md) - 线程监控、超时保护、非交易时段优化
- [Web 前端](docs/site/miniqmt/web-frontend.md) - web1.0 / web2.0 双模式
- [自动买入](docs/site/miniqmt/autobuy.md) - 候选池筛选、指数门禁、防重买入
- [网格交易](docs/site/miniqmt/grid-trading.md) - 网格实盘闭环与真实盈亏账本（含固定金额/固定股数交易份额模式，按 MACD DEA 趋势推荐默认）
- [QMT order_id 匹配](docs/site/miniqmt/qmt-order-id-matching.md) - 异步下单 `seq -> order_id` 的实盘行为结论、匹配优先级与 unknown 委托防重复下单

### 配置文件

#### account_config.json (必需)
```json
{
  "account_id": "您的交易账号",
  "account_type": "STOCK",
  "qmt_path": "C:/光大证券金阳光QMT实盘/userdata_mini"
}
```

#### stock_pool.json (可选)
```json
[
  "000001.SZ",
  "600036.SH",
  "920118.BJ"
]
```

股票池支持 `.SH` / `.SZ` / `.BJ` 后缀，也支持裸代码自动补全；发布配置建议显式写后缀。

---

**ALWAYS RESPOND IN SIMPLIFIED CHINESE!!!**
