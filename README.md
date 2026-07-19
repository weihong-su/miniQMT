# miniQMT - 无人值守量化交易系统

<div align="center">

[![Python Version](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-BSL%201.1-blue)](LICENSE)
[![QMT API](https://img.shields.io/badge/QMT-API-orange)](https://dict.thinktrader.net)

**基于迅投QMT API的智能量化交易系统**

[功能特性](#功能特性) • [快速开始](#快速开始) • [系统架构](#系统架构) • [文档](#文档) • [常见问题](#常见问题)

</div>

---

## 📖 项目简介

miniQMT 是一个专为A股市场设计的**无人值守量化交易系统**,集成了自动化交易策略执行、智能持仓管理、动态止盈止损等功能。系统采用双层存储架构和线程自愈机制,可7x24小时稳定运行。

### 🎯 核心优势

- **🛡️ 无人值守**: 线程健康监控与自动重启,系统崩溃自动恢复
- **🔄 双层存储**: 内存数据库 + SQLite持久化,高性能与数据安全兼顾
- **🎯 信号分离**: 信号检测与执行分离,策略逻辑清晰可控
- **📈 智能止盈止损**: 动态止盈策略,最大化收益同时控制风险
- **🌐 网格交易**: 智能网格策略,自动低吸高抛
- **🤖 自动买入**: 独立 `miniqmt_autobuy` 进程,从外部候选池筛选并复用 Web 买入 API 下单
- **📡 行情源健康评分**: 内存版行情质量评分,默认启用交易门禁,观测 xtdata/Mootdx 成功率、延迟和新鲜度
- **🧹 数据库维护**: 非交易时段自动清理追加型历史数据并轮转 XtQuantManager 日志
- **⚙️ 配置管理**: Web界面动态配置,无需重启系统
- **🔍 卖出监控**: 委托单超时监控与自动撤单
- **🧪 模拟交易**: 完整的模拟交易功能,策略验证零风险
- **🔗 交易通道四选一**: 默认 xttrader 直连，可选 XtQuantManager、文件 IPC 或 RPC；三个可选后端互斥，RPC 默认只读

---

## ✨ 功能特性

### 交易功能
- ✅ **自动交易执行**: 支持模拟交易和实盘交易
- ✅ **动态止盈止损**: 首次止盈 + 动态止盈双重保护
- ✅ **网格交易**: 智能网格会话管理,自动低吸高抛
- ✅ **网格实盘闭环**: 以成交回报为准的委托确认、对手价下单、涨跌停/停牌防护、启动对账、FIFO 真实盈亏账本
- ✅ **网格账本详情**: Web 前端和 `/api/grid/ledger/<session_id>` 展示买入批次、FIFO 配对、已实现/未实现盈亏
- ✅ **网格状态口径统一**: Web1.0 悬停卡片使用统一 `pnl_snapshot` 真实盈亏，小数比例统一显示；“中心价偏离”按当前网格中心价相对初始中心价漂移计算
- ✅ **成交确认后写流水**: 实盘动态止盈止损、补仓和网格委托均以成交确认为准写入 `trade_records`，避免未成交委托伪装成成交
- ✅ **自动买入模块**: 支持候选池多表并集、大盘指数门禁、惰性条件检查、防重买入和复盘日志
- ✅ **信号验证机制**: 防止重复执行,确保交易安全
- ✅ **委托单管理**: 卖出委托单超时监控与自动撤单

### 系统功能
- ✅ **线程健康监控**: 自动检测线程崩溃并重启
- ✅ **优雅关闭**: 有序关闭各模块,避免数据丢失
- ✅ **超时保护**: API调用超时保护,防止线程阻塞
- ✅ **非交易时段优化**: 自动降低CPU占用(30% → <2%)
- ✅ **内存数据库并发优化**: 16处加锁保护,确保线程安全
- ✅ **配置动态更新**: Web界面修改配置,无需重启系统
- ✅ **行情源健康评分**: 默认启用交易门禁,通过 `/api/market/health` 查看 xtdata/Mootdx 健康状态
- ✅ **数据库维护与日志轮转**: 每日非交易时段清理过期历史表,必要时执行 `VACUUM`,并轮转 `logs/xqm_manager.log`
- ✅ **盘前自动同步**: 每日9:25自动重新初始化xtquant连接
- ✅ **心跳日志**: 每30分钟输出系统健康状态摘要
- ✅ **XtQuantManager**: 可选HTTP网关,支持多账户管理与可观测指标

### 监控功能
- ✅ **Web实时监控**: 账户信息、持仓列表、网格会话实时更新
- ✅ **双版本前端**: web1.0 (Flask 模板) + web2.0 (Vue3 + Vite + TS + PWA)
- ✅ **网关双模式**: web2.0 支持 Flask 直连（完整功能）/ XtQuantManager 网关（多账号只读监控 + 下单）
- ✅ **SSE推送**: Server-Sent Events实时数据推送
- ✅ **配置管理**: Web界面动态修改系统配置
- ✅ **系统诊断**: 内置多个诊断工具,快速定位问题

---

## 🚀 快速开始

### 环境要求

- **Python**: 3.8+ (推荐 3.9)
- **操作系统**: Windows (QMT仅支持Windows)
- **QMT客户端**: 实盘交易需要安装并登录QMT

### 安装步骤

1. **克隆项目**
```bash
git clone https://github.com/your-repo/miniQMT.git
cd miniQMT
```

2. **安装依赖**
```bash
pip install -r utils/requirements.txt

# 验证安装
python utils/check_dependencies.py
```

3. **配置账户** (创建 `account_config.json`)
```json
{
  "account_id": "您的交易账号",
  "account_type": "STOCK",
  "qmt_path": "C:/光大证券金阳光QMT实盘/userdata_mini"
}
```

4. **配置股票池** (可选,创建 `stock_pool.json`)
```json
[
  "000001.SZ",
  "600036.SH",
  "000333.SZ"
]
```

5. **启动系统**
```bash
python main.py
```

6. **访问Web界面**
```
http://localhost:5000          # web1.0 (Flask)，随系统自动启动
```
> web2.0 (Vue3) 需先构建：`cd web2.0 && npm install && npm run build`，可由 Flask / xtquant_manager 托管，或独立部署到 Vercel。

### 一键启动（推荐）

项目提供交互式总控台 `miniqmt.bat`，覆盖环境检查、依赖安装、配置校验、启动/停止、XtQuantManager 网关和自动买入服务管理等：

```bash
miniqmt.bat                       # 打开交互式菜单
python scripts/_launcher.py menu  # 等效命令
```

常用菜单入口：

| 分区 | 选项 | 功能 |
|------|------|------|
| 部署/环境 | `[1]`-`[4]` | 检查环境、安装依赖、校验配置、拉取代码 |
| 交易进程 | `[7]`-`[9]` | 启动所有/指定账号，支持实盘/模拟和 web1.0/web2.0 选择 |
| 停止 | `[a]`-`[c]` | 优雅停止/强制停止账号进程 |
| XtQuantManager | `[d]`-`[i]` | 启动、停止、状态、打开 UI、重启、查看日志 |
| 自动买入 | `[j]`-`[m]` | 启动、停止、查看状态、查看日志 |
| 数据源/通道 | `[n]`-`[p]` | Tushare、文件 IPC、XtTrader 通道总控（直连/IPC/RPC） |

也保留旧的 `launcher.bat`（配合 `launcher.ini`）用于直接拉起 `main.py`：

```ini
[Environment]
ENV_TYPE=conda           # conda 或 uv
CONDA_ENV=python39       # conda环境名
WORK_DIR=c:\github-repo\miniQMT
PYTHON_SCRIPT=main.py
```

### ⚠️ 首次运行建议

**强烈建议先使用模拟模式测试**:

1. 确认 `config.py` 中 `ENABLE_SIMULATION_MODE = True`
2. 运行无人值守功能测试: `python test/test_unattended_operation.py`
3. 观察系统运行稳定后,再切换到实盘模式

---

## 🏗️ 系统架构

### 核心设计

```
┌─────────────────────────────────────────────────────────┐
│                    线程监控器 (60秒)                      │
│              检测线程崩溃 → 自动重启                       │
└─────────────────────────────────────────────────────────┘
                            ↓
┌──────────┬──────────┬──────────┬──────────┬──────────┬────────┐
│数据更新  │持仓监控  │策略执行  │网格交易  │卖出监控  │Web服务 │
│ (60秒)  │ (3秒)   │ (5秒)   │ (5秒)   │ (2秒)   │(持续)  │
└──────────┴──────────┴──────────┴──────────┴──────────┴────────┘
     ↓        ↓        ↓        ↓        ↓        ↓
┌─────────────────────────────────────────────────────────┐
│                    核心架构                              │
├─────────────────────────────────────────────────────────┤
│  配置管理 (config_manager) → 配置持久化数据库             │
│  双层存储 (position_manager) → 内存DB + SQLite          │
│  网格交易 (grid_trading_manager) → 网格会话管理          │
│  卖出监控 (sell_monitor) → 委托单超时撤单                │
└─────────────────────────────────────────────────────────┘
```

### 数据流

**实盘模式**:
```
QMT实盘账户 → 持仓同步(10秒) → 内存数据库 → 定时同步(15秒) → SQLite
```

**模拟模式**:
```
Web界面 → 交易执行器 → 内存数据库 (跳过QMT接口)
```

详细架构说明请查看 [ARCHITECTURE.md](ARCHITECTURE.md)

---

## 📊 核心配置

### 功能开关 (config.py)

```python
# 交易模式
ENABLE_SIMULATION_MODE = True   # True=模拟, False=实盘 ⚠️
ENABLE_AUTO_OPERATION = False   # 全局自动操作总开关：关闭时所有自动策略不产生新单 ⚠️
ENABLE_AUTO_TRADING = False     # 允许自动止盈：动态止盈止损自动执行开关（持久化）

# 策略功能
ENABLE_DYNAMIC_STOP_PROFIT = True  # 止盈止损功能
ENABLE_GRID_TRADING = True         # 允许自动网格：网格模块自动执行开关（持久化）

# 系统功能
ENABLE_THREAD_MONITOR = True    # 线程健康监控（无人值守必需）⭐
ENABLE_SELL_MONITOR = True      # 卖出监控开关
ENABLE_XTQUANT_MANAGER = False  # XtQuantManager HTTP网关（多账户时开启）
DEBUG = False                   # 调试模式
```

交易接口由 `position_manager._create_qmt_trader()` 四选一：默认 `easy_qmt_trader` 直连；也可开启 `ENABLE_XTQUANT_MANAGER`、`ENABLE_QMT_IPC_FALLBACK` 或 `ENABLE_QMT_RPC_FALLBACK`。后三个开关互斥，同时开启会直接抛错；`QMT_RPC_ALLOW_ORDER=False` 时 RPC 通道只读，拒绝真实下单。

### 止盈止损配置

```python
# 止损
STOP_LOSS_RATIO = -0.075  # 成本价下跌7.5%触发止损

# 首次止盈
INITIAL_TAKE_PROFIT_RATIO = 0.06  # 盈利6%触发
INITIAL_TAKE_PROFIT_RATIO_PERCENTAGE = 0.6  # 卖出60%

# 动态止盈
DYNAMIC_TAKE_PROFIT = [
    (0.05, 0.96),  # 最高浮盈5%时,止盈位=最高价*96%
    (0.10, 0.93),  # 最高浮盈10%时,止盈位=最高价*93%
    (0.15, 0.90),
    (0.20, 0.87),
    (0.30, 0.85),
]
```

### 网格交易配置

```python
ENABLE_GRID_TRADING = True                   # 网格交易功能开关（默认已启用）
grid_trading_sessions.enabled = 1            # 个股网格会话自动执行开关（Web“自动/暂停”）
GRID_DEFAULT_PRICE_INTERVAL = 0.05           # 价格间隔5%（下跌触发买入/上涨触发卖出）
GRID_DEFAULT_POSITION_RATIO = 0.25           # 每档交易持仓比例25%
GRID_CALLBACK_RATIO = 0.005                  # 回调触发比例0.5%
GRID_BUY_COOLDOWN = 300                      # 买入冷却时间(秒)
GRID_SELL_COOLDOWN = 300                     # 卖出冷却时间(秒)
GRID_MAX_DEVIATION_RATIO = 0.15              # 最大偏离度15%（触发退出）
GRID_TARGET_PROFIT_RATIO = 0.10              # 目标盈利10%（需买卖配对后触发）
GRID_STOP_LOSS_RATIO = -0.10                 # 止损-10%
GRID_DEFAULT_DURATION_DAYS = 7               # 默认运行7天
GRID_REQUIRE_PROFIT_TRIGGERED = False        # 默认不要求先触发首次止盈即可启动网格

# 网格实盘交易（仅 ENABLE_SIMULATION_MODE = False 生效）
GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True       # 实盘以成交回报为准更新统计
GRID_SIGNAL_MAX_AGE_SECONDS = 60             # 信号最长有效期(秒)
GRID_SIGNAL_MAX_PRICE_DRIFT_RATIO = 0.01     # 执行前最大容忍价格偏离(1%)
GRID_USE_COUNTERPARTY_PRICE = True           # 对手价下单(买取卖三价/卖取买三价)
GRID_COUNTERPARTY_BUY_PRICE_BUFFER_RATIO = 0.02  # 对手价买入资金预占缓冲(2%)
GRID_ENABLE_PRICE_LIMIT_GUARD = True         # 涨跌停/停牌防护
GRID_PRICE_LIMIT_EPS = 0.001                 # 涨跌停判定容差(元)
```

> `GRID_REQUIRE_PROFIT_TRIGGERED` 是保留的安全阀：默认 `False` 时，持仓个股可直接创建网格会话；显式设为 `True` 时，仍要求该持仓已触发首次止盈（`profit_triggered=True`）后才能启动网格。

**网格盈亏口径说明（退出条件使用）**
```text
True P&L = (total_sell_amount - total_buy_amount) + open_grid_volume * current_price
open_grid_volume = total_buy_volume - total_sell_volume
profit_ratio = True P&L / max_investment
```
降级路径（旧会话无 volume 数据）：
```text
若有持仓快照: profit_ratio = (total_sell_amount - total_buy_amount) / (position_volume * current_price)
否则回退:     profit_ratio = (total_sell_amount - total_buy_amount) / max_investment
```

### 委托单管理配置

```python
ENABLE_SELL_MONITOR = True      # 卖出监控开关（默认已启用）
```

### 行情源健康评分配置

第一阶段为轻量内存版，不落库、重启即清空；默认启用严格门禁，按评分与数据源策略判断行情是否可参与交易信号检测。

```python
MARKET_HEALTH_ENABLED = True
MARKET_HEALTH_OBSERVE_ONLY = False            # False=按评分门禁交易信号检测
MARKET_HEALTH_WINDOW_SECONDS = 300            # 统计最近5分钟事件
MARKET_HEALTH_MIN_EVENTS = 3                  # 少于3个样本显示 unknown
MARKET_HEALTH_TRADING_MIN_SCORE = 70          # 严格模式下可交易最低分
MARKET_HEALTH_ALLOW_MOOTDX_FOR_TRADING = False
```

健康快照可通过 `GET /api/market/health` 查看，返回 `overall`、`sources`、`stocks` 和交易阈值配置。

### 自动买入模块

`autobuy/` 是独立进程模块，不嵌入主交易线程。它从外部 SQLite 候选池读取最近 N 个交易日入池股票，先做大盘指数门禁（`999999` / `399001` / `399005` 至少一个 MA5 向上），再做持仓/历史买入防重、技术条件检查，最后复用 web1.0 的 `/api/actions/execute_buy` 下单。

```bash
# 启动前先确保目标账号 web_server 已运行
miniqmt.bat                         # 菜单 [j] 启动、[l] 查看状态、[m] 查看日志
python -m autobuy.app --once        # 单次触发，便于测试配置
```

配置文件为 `autobuy/miniqmt_autobuy.cfg`，运行状态写入 `data/.autobuy_status.json`，日志写入 `logs/miniqmt_autobuy.log`，复盘库为 `data/autobuy.db`。详细说明见 [autobuy/README.md](autobuy/README.md) 和在线文档。

---

## 📚 文档

- **[在线文档站](https://weihong-su.github.io/miniQMT/)** - 完整文档（mkdocs，源码在 `docs/site/`）
- **[CLAUDE.md](CLAUDE.md)** - 开发指南(面向AI助手和开发者)
- **[ARCHITECTURE.md](ARCHITECTURE.md)** - 详细架构说明
- **[QUICK_START.md](QUICK_START.md)** - 快速入门指南
- **[CHANGELOG.md](CHANGELOG.md)** - 版本变更日志
- **[docs/xtquant_manager.md](docs/xtquant_manager.md)** - XtQuantManager 多账户网关说明
- **[autobuy/README.md](autobuy/README.md)** - 自动买入模块说明

> 无人值守运行、网格交易、自动买入、止盈止损、Web 双模式、数据库表结构等详细文档已迁移至[在线文档站](https://weihong-su.github.io/miniQMT/)，本地源码位于 `docs/site/`。

---

## 🧪 测试

### 回归测试框架（推荐）

项目集成了完整的回归测试框架,支持按模块运行、快速验证和失败重试。当前配置包含 31 个测试组（含 `fast` 快速子集）；`--all` 默认排除重复的 `fast` 组，`--all-with-fast` 会连同快速子集一起运行。最近一次使用 Anaconda `python39` 执行 `--all-with-fast` 实测为 31 组、107 个模块、1933 个用例，100% 通过。

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
python test/run_integration_regression_tests.py --group grid_trade          # 网格交易执行
python test/run_integration_regression_tests.py --group grid_true_pnl       # 真实盈亏账本
python test/run_integration_regression_tests.py --group grid_comprehensive  # 网格综合
python test/run_integration_regression_tests.py --group grid_full_range_coverage  # 全区间覆盖（114用例）
python test/run_integration_regression_tests.py --group qmt_ipc_fallback    # 大QMT 文件IPC
python test/run_integration_regression_tests.py --group qmt_rpc             # 大QMT RPC

# 其他选项
python test/run_integration_regression_tests.py --all --retry-failed   # 失败重试
python test/run_integration_regression_tests.py --all --verbose        # 详细输出
```

测试报告自动输出到 `test/integration_test_report.json` 和 `test/integration_test_report.md`。

### 推荐单测顺序

```bash
# 1. 无人值守功能测试（验证线程自愈机制）
python test/test_unattended_operation.py

# 2. 系统集成测试
python -m unittest test.test_system_integration -v

# 3. 止盈止损测试
python test/test_stop_profit.py

# 4. Web数据刷新测试
python test/test_web_data_refresh.py
```

### 系统诊断工具

```bash
# 检查依赖安装
python utils/check_dependencies.py

# 实时查看运行日志
Get-Content logs/qmt_trading.log -Wait   # Windows PowerShell
tail -f logs/qmt_trading.log             # Git Bash
```

> 系统运行状态可直接通过 Web 界面 (`http://localhost:5000`) 或 `miniqmt.bat` 菜单 [6] 查看。

---

## ❓ 常见问题

### Q1: 如何切换到实盘交易?

**A**: 修改 `config.py`:
```python
ENABLE_SIMULATION_MODE = False  # 切换到实盘
ENABLE_AUTO_OPERATION = True    # 启动全局自动操作总闸（Web“开始自动操作”按钮，运行时生效）
ENABLE_AUTO_TRADING = True      # 允许自动止盈（动态止盈止损自动执行）
```
**⚠️ 注意**: 确保QMT客户端已启动并登录!

### Q2: 系统崩溃后会自动恢复吗?

**A**: 会的! 系统内置线程健康监控,每60秒检查一次线程状态,检测到崩溃立即重启。详见[在线文档站 · 无人值守运行](https://weihong-su.github.io/miniQMT/miniqmt/unattended/)

### Q3: 如何查看系统运行日志?

**A**: 日志文件位于 `logs/qmt_trading.log`,可以使用:
```bash
# 实时查看日志
tail -f logs/qmt_trading.log

# 查看最近100行
tail -n 100 logs/qmt_trading.log
```

### Q4: 模拟交易和实盘交易有什么区别?

**A**:
- **模拟交易**: 不调用QMT API,使用虚拟资金,无交易时间限制
- **实盘交易**: 通过QMT API执行真实订单,使用实际账户资金

### Q5: 如何添加新的股票到股票池?

**A**: 编辑 `stock_pool.json` 文件,添加股票代码(格式: `股票代码.交易所`):
```json
[
  "000001.SZ",  // 深圳交易所
  "600036.SH",  // 上海交易所
  "新增股票.SZ"
]
```

### Q6: 如何启用网格交易?

**A**: 网格交易默认已启用（`ENABLE_GRID_TRADING = True`），但仍受全局自动操作总开关 `ENABLE_AUTO_OPERATION` 控制。通过 Web 界面创建网格会话即可；每个个股网格会话还可以在 Web 里用“自动/暂停”开关控制 `grid_trading_sessions.enabled`，暂停后保留会话但不再发新网格单。如需全局禁用网格，修改 `config.py`：
```python
ENABLE_GRID_TRADING = False
```

### Q7: 如何通过Web界面修改配置?

**A**: 访问 `http://localhost:5000/config` 页面，修改配置后点击保存。配置会立即生效，无需重启系统。支持的配置包括:
- 止盈止损比例
- 网格交易参数
- 开始/停止自动操作、允许自动止盈、允许自动网格、个股网格自动/暂停开关
- 线程监控开关

所有配置变更会记录到 `system_config` 和 `config_history` 表中，支持审计和回滚。

更多问题请查看 [CLAUDE.md - 常见问题](CLAUDE.md#常见问题与解决方案)

---

## 🛡️ 风险提示

**⚠️ 重要提醒**:

1. **量化交易有风险,投资需谨慎**
2. **本系统仅供学习研究使用,不构成投资建议**
3. **实盘交易前请充分测试策略,确保理解系统运行逻辑**
4. **建议设置合理的止损比例,控制单日最大亏损**
5. **定期检查系统运行状态,关注异常日志**

---

## 📄 许可证

**Business Source License 1.1 (BSL 1.1)**

- **个人/非商业用途**：免费使用、修改、学习
- **商业用途**（含收费项目、商业产品集成）：需获得作者授权
- **2030-04-13 后**：自动转为 MIT 开源协议

v1.x 及之前的版本仍以 MIT 协议发布，不受影响。详见 [LICENSE](LICENSE) 文件。

如需商业授权，请通过 [GitHub Issue](https://github.com/weihong-su/miniQMT/issues) 联系。

---

## 🤝 贡献

欢迎提交 Issue 和 Pull Request!

---

## 📧 联系方式

如有问题或建议,请通过以下方式联系:

- 提交 [GitHub Issue](https://github.com/weihong-su/miniQMT/issues)
- 邮箱: weihong-su (via GitHub)

---

<div align="center">

**⭐ 如果这个项目对你有帮助,请给个Star支持一下! ⭐**

I would be delighted if you could consider **buying me a coffee** ☕️ to support my work.

<a href="https://buymeacoffee.com/suweihongc">
  <img src="https://www.buymeacoffee.com/assets/img/guidelines/download-assets-sm-2.svg" alt="Buy Me A Coffee" style="height: 41px !important;width: 174px !important;" >
</a>

You can also support via WeChat or Alipay:

<img src="https://get.spdt.work/FreeAI_pay.jpg" alt="Donation QR Code" width="200">

Made with ❤️ by miniQMT Team

</div>
