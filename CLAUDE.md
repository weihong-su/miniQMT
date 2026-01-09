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

### 启动系统
```bash
python main.py
```

### 运行测试(推荐顺序)
```bash
# 1. 无人值守功能测试（推荐首先运行,验证线程自愈机制）
python test/test_unattended_operation.py

# 2. 系统综合测试
python test/comprehensive_test.py

# 3. 止盈止损测试
python test/test_stop_loss_buy_param.py

# 4. Web数据刷新测试
python test/test_web_data_refresh.py

# 5. 盘前同步测试
python test/test_premarket_sync_performance.py
```

### 前端访问
```
http://localhost:5000
```

### 关键配置检查
```bash
# 检查系统状态
python test/check_system_status.py

# 诊断QMT连接
python test/diagnose_qmt_connection.py

# 诊断系统问题
python test/diagnose_system_issues.py
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
premarket_sync.py      # 盘前同步与初始化
config_manager.py      # 配置持久化管理
```

### 线程架构

| 线程 | 启动位置 | 职责 | 频率 | 关键配置 |
|------|---------|------|------|---------|
| 线程监控 | `thread_monitor.start()` | 检测线程崩溃并自动重启 | 60秒 | `ENABLE_THREAD_MONITOR` |
| 数据更新 | `data_manager.start_data_update_thread()` | 更新股票池行情 | 60秒 | - |
| 持仓监控 | `position_manager.start_position_monitor_thread()` | 同步实盘持仓、更新价格、检测止盈止损 | 3秒 | `MONITOR_LOOP_INTERVAL` |
| 策略执行 | `strategy.start_strategy_thread()` | 获取信号、执行交易、网格检查 | 5秒 | `ENABLE_AUTO_TRADING` |
| 定时同步 | `position_manager.start_sync_thread()` | 内存→SQLite同步 | 15秒 | `POSITION_SYNC_INTERVAL` |
| Web服务 | `web_server.start_web_server()` | RESTful API | 持续 | - |

## 关键配置

### 功能开关 (config.py)

```python
# 核心开关
ENABLE_SIMULATION_MODE = True   # True=模拟, False=实盘 ⚠️
ENABLE_AUTO_TRADING = False     # 自动交易执行开关 ⚠️
ENABLE_DYNAMIC_STOP_PROFIT = True  # 止盈止损功能
ENABLE_GRID_TRADING = False     # 网格交易功能
ENABLE_THREAD_MONITOR = True    # 线程健康监控（无人值守必需）⭐
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

## 环境要求

- Python 3.8+ (推荐 3.9)
- 依赖包: `pandas`, `numpy`, `flask`, `flask-cors`, `xtquant`, `mootdx`, `sqlite3`
- QMT客户端 (实盘交易时需要)


