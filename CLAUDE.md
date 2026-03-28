# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

miniQMT 是一个基于迅投QMT API的**无人值守量化交易系统**,实现自动化交易策略执行、持仓管理、止盈止损和网格交易。

**核心特性**:
- 🔄 双层存储架构(内存数据库 + SQLite持久化)
- 🎯 信号检测与执行分离设计
- 🧵 多线程协同工作 + 线程自愈机制
- 📈 动态止盈止损策略
- 🌐 Web前端实时监控界面
- 🛡️ 无人值守运行(线程监控、超时保护、优雅关闭)

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
创建 `account_config.json` 和 `stock_pool.json` (参见文档末尾)

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

# 按组运行
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

### 系统诊断工具
```bash
# 检查系统状态
python test/check_system_status.py

# 诊断QMT连接
python test/diagnose_qmt_connection.py

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
策略执行线程 → 检查ENABLE_AUTO_TRADING → 执行/忽略信号
```

**关键点**:
- 监控线程**始终运行**,持续检测信号(即使 `ENABLE_AUTO_TRADING=False`)
- `ENABLE_AUTO_TRADING` 只控制**是否执行**检测到的信号
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
main.py                # 系统启动入口和线程管理
thread_monitor.py      # 线程健康监控与自愈（无人值守核心）⭐
data_manager.py        # 历史数据获取(xtdata接口)
indicator_calculator.py # 技术指标计算
position_manager.py    # 持仓管理核心(内存+SQLite双层)⭐
trading_executor.py    # 交易执行器(xttrader接口)
strategy.py            # 交易策略逻辑⭐
web_server.py          # RESTful API服务(Flask)
easy_qmt_trader.py     # QMT交易API封装
premarket_sync.py      # 盘前同步与初始化(每天9:25重新初始化xtquant)
config_manager.py      # 配置持久化管理
sell_monitor.py        # 卖出委托单超时监控与撤单⭐
grid_trading_manager.py # 网格交易会话管理(独立线程)
grid_database.py       # 网格交易数据持久化(SQLite)
grid_validation.py     # 网格交易参数校验
xtquant_manager/       # XtQuantManager HTTP网关(多账户管理，可选)
```

### 线程架构

| 线程 | 启动位置 | 职责 | 频率 | 关键配置 |
|------|---------|------|------|---------|
| 线程监控 | `thread_monitor.start()` | 检测线程崩溃并自动重启 | 60秒 | `ENABLE_THREAD_MONITOR` |
| 数据更新 | `data_manager.start_data_update_thread()` | 更新股票池行情 | 60秒 | - |
| 持仓监控 | `position_manager.start_position_monitor_thread()` | 同步实盘持仓、更新价格、检测止盈止损 | 3秒 | `MONITOR_LOOP_INTERVAL` |
| 策略执行 | `strategy.start_strategy_thread()` | 获取信号、执行交易 | 5秒 | `ENABLE_AUTO_TRADING` |
| 网格交易 | `grid_trading_manager` 内部线程 | 网格信号检测与买卖执行 | 5秒 | `ENABLE_GRID_TRADING` |
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
ENABLE_AUTO_TRADING = False     # 自动交易执行开关 ⚠️
ENABLE_DYNAMIC_STOP_PROFIT = True  # 止盈止损功能
ENABLE_GRID_TRADING = True      # 网格交易功能
ENABLE_THREAD_MONITOR = True    # 线程健康监控（无人值守必需）⭐
ENABLE_SELL_MONITOR = True      # 卖出委托单超时监控
ENABLE_XTQUANT_MANAGER = False  # XtQuantManager HTTP网关（多账户时开启）
DEBUG = False                   # 调试模式
```

**⚠️ 实盘交易前必须检查**:
1. `ENABLE_SIMULATION_MODE = False` (切换到实盘)
2. `ENABLE_AUTO_TRADING = True` (启用自动交易)
3. QMT客户端已启动并登录
4. `account_config.json` 配置正确

### 无人值守运行配置 ⭐

```python
# 线程监控
ENABLE_THREAD_MONITOR = True      # 启用线程自愈
THREAD_CHECK_INTERVAL = 60        # 检查间隔(秒)
THREAD_RESTART_COOLDOWN = 60      # 重启冷却时间(秒)

# 持仓监控优化
MONITOR_LOOP_INTERVAL = 3         # 监控循环间隔(秒)
MONITOR_CALL_TIMEOUT = 3.0        # API调用超时(秒)
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
```

## 数据库表结构

### positions (持仓表)

**数据来源分类**:

| 字段 | 数据来源 | 更新时机 |
|------|---------|---------|
| `stock_code`, `volume`, `available`, `cost_price` | QMT实盘 `qmt_trader.position()` | 每10秒同步一次 |
| `current_price` | `data_manager.get_latest_data()` | 实时更新 |
| `market_value`, `profit_ratio` | 计算得出 | 价格更新时重新计算 |
| `open_date`, `profit_triggered`, `highest_price`, `stop_loss_price` | 持久化字段 | 策略触发时更新并立即同步到SQLite |

**关键字段说明**:
- `profit_triggered`: 是否已触发首次止盈(卖出60%),影响后续动态止盈逻辑
- `highest_price`: 持仓期间最高价,用于计算动态止盈位
- `stop_loss_price`: 止损价格,低于此价格触发止损

### trade_records (交易记录表)

记录所有买卖交易,包含:
- `stock_code`, `trade_type` (BUY/SELL), `price`, `volume`
- `trade_id`: 订单ID (实盘为QMT返回的order_id, 模拟为 `SIM{timestamp}{counter}`)
- `strategy`: 策略标识 (`simu`/`auto_partial`/`stop_loss`/`grid`)

## 无人值守运行 ⭐

系统支持长期持续运行,通过线程健康监控实现自动恢复。详见 [无人值守运行文档](docs/quick_start_unattended.md)

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

持仓监控线程中的API调用有3秒超时保护:

```python
try:
    future.result(timeout=config.MONITOR_CALL_TIMEOUT)  # 默认3秒
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
- 确认 `signal_timestamps` 机制正常工作

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


## Web API端点

### 核心端点

**系统状态**:
- `GET /api/status` - 获取系统运行状态
- `GET /api/connection/status` - 检查QMT连接状态

**持仓管理**:
- `GET /api/positions` - 获取所有持仓
- `GET /api/positions/<stock_code>` - 获取单只股票持仓
- `GET /api/positions/stream` - SSE实时推送持仓数据

**交易操作**:
- `POST /api/actions/execute_buy` - 执行买入 (参数: stock_code, amount, strategy)
- `POST /api/actions/execute_sell` - 执行卖出 (参数: stock_code, volume, strategy)
- `POST /api/actions/execute_trading_signal` - 执行指定交易信号

**配置管理**:
- `GET /api/config` - 获取系统配置
- `POST /api/config/update` - 更新配置参数

**信号查询**:
- `GET /api/signals/pending` - 获取待处理信号列表
- `GET /api/signals/latest/<stock_code>` - 获取指定股票最新信号

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

测试代码位于 [test/](test/) 目录，使用标准 `unittest`，共 65+ 个测试文件，约 21,000 行。

### 测试基础设施

- **[test/test_base.py](test/test_base.py)**: `TestBase` 基类，提供测试DB创建、持仓 fixture、线程断言、条件等待等工具方法
- **[test/test_mocks.py](test/test_mocks.py)**: `MockQmtTrader` 完整模拟 QMT API（连接、持仓查询、下单），无需真实 QMT 环境即可运行测试
- **[test/test_utils.py](test/test_utils.py)**: 通用测试辅助函数

### 测试分组

| 组名 | 优先级 | 内容 |
|------|--------|------|
| `system_integration` | critical | 系统集成、无人值守、线程监控 |
| `stop_profit` | high | 动态止盈止损策略（7个模块） |
| `grid_signal` | high | 网格信号检测与价格追踪 |
| `grid_session` | high | 网格会话生命周期管理 |
| `grid_trade` | high | 网格买卖执行与资金管理 |
| `grid_exit` | high | 网格退出条件检测 |
| `grid_comprehensive` | high | 网格综合端到端场景 |
| `grid_validation` | medium | 参数校验与边界情况 |
| `grid_bugfix_c1` | critical | BUG-C1修复验证（冷却防重单、DESIGN-4设计约束） |
| `grid_bug_regression` | high | 4个已修复Bug的回归验证 |
| `grid_qa_fixes` | high | MECE审查6个修复的验证 |
| `grid_max_investment_safety` | critical | max_investment三重防护验证 |
| `core_metrics` | high | 网格利润计算与风险分级 |
| `trader_callback` | critical | 卖出委托Callback兜底机制 |
| `web_api` | critical | RESTful API功能测试（含Bug修复回归） |
| `db_thread_safety` | critical | 数据库线程安全与Web缓存验证 |
| `dual_layer_storage` | critical | 内存+SQLite双层存储一致性 |
| `xtdata_data_source` | high | xtdata动态订阅与fallback路径 |
| `indicator_calculator` | high | 技术指标计算器全方法验证 |
| `grid_qa_gap_supplement` | critical | QA缺口补充（信号优先级/最小卖出/position_snapshot降级） |
| `grid_full_range_coverage` | critical | 全网格区间覆盖（114个用例，A-K 11个套件） |
| `grid_simulation` | high | 价格模拟测试（30个用例） |
| `fast` | critical | 5分钟快速验证子集（17个模块，419个用例） |

**测试统计（最新）**: 23组 × 67模块 = 1170个测试用例，100%通过

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

### 无人值守运行
- [快速启动指南](docs/quick_start_unattended.md) - 5分钟快速启用无人值守功能
- [核心改进总结](docs/unattended_operation_summary.md) - 线程监控和优化详解
- [优雅关闭优化](docs/graceful_shutdown_optimization.md) - 系统关闭流程说明
- [优雅关闭验证](docs/graceful_shutdown_verification.md) - 验证报告和预期行为

### 代码清理记录
- [cleanup_20260103.md](docs/cleanup_20260103.md) - 最近代码清理记录

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
  "000333.SZ"
]
```

---

**ALWAYS RESPOND IN SIMPLIFIED CHINESE!!!**
