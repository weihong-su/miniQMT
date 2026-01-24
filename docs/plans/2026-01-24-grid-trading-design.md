# 网格交易功能设计方案

**设计日期**: 2026-01-24
**设计版本**: v1.0
**状态**: 设计完成,待实施

---

## 1. 功能概述

### 1.1 目标
为已触发半仓止盈(profit_triggered=True)的持仓股票提供网格交易功能,通过手动确认启动,以"买后最高价"为中心进行自动化买卖操作,实现震荡行情中的低买高卖策略。

### 1.2 核心特性
- ✅ **手动触发**: 通过Web界面checkbox启动,用户完全掌控
- ✅ **智能回调**: 价格穿越档位后等待回调(默认0.5%)再交易,避免趋势行情中过早操作
- ✅ **动态网格**: 每次交易后以成交价为新中心重新生成对称网格
- ✅ **多重保护**: 偏离度+盈亏+时间+手动+持仓清空五重退出机制
- ✅ **完整监控**: 实时状态+统计数据+交易明细全面展示
- ✅ **最小侵入**: 新增1个核心模块,修改现有4个文件约250行代码
- ✅ **重启恢复**: 系统重启后自动恢复网格会话,保守策略确保安全

### 1.3 设计原则
- **KISS原则**: 界面复用现有checkbox,配置简洁明了
- **YAGNI原则**: 只实现明确需要的功能,避免过度设计
- **安全第一**: 多重退出保护,重启后保守恢复策略
- **最小侵入**: 尽量复用现有架构,新增代码模块化

---

## 2. 触发机制与工作流程

### 2.1 前置条件
- 持仓股票已触发首次止盈(`profit_triggered=True`)
- 系统网格交易总开关已启用(`ENABLE_GRID_TRADING=True`)
- 持仓数量大于0

### 2.2 启动流程

```
用户在Web界面点击checkbox(未选中→选中)
  ↓
弹出配置面板(Modal对话框)
  ↓
显示默认配置参数(可编辑):
  - 网格价格间隔: 5%
  - 每档交易比例: 25%
  - 回调触发比例: 0.5%
  - 最大追加投入: 当前持仓市值的50%
  - 运行时长限制: 7天
  - 最大偏离度: ±15%
  - 目标盈利: +10%
  - 止损比例: -10%
  ↓
用户确认
  ↓
系统执行:
  1. 读取该股票的 highest_price(买后最高价)
  2. 锁定为 center_price(网格中心,不再变化)
  3. 计算网格区间: [center_price × 0.95, center_price × 1.05]
  4. 生成初始对称档位: 下档、中心、上档
  5. 创建 GridSession 对象
  6. 初始化 PriceTracker 追踪器
  7. 持久化到数据库 grid_trading_sessions 表
  8. checkbox保持选中状态,显示"运行中🟢"
```

### 2.3 运行流程

```
持仓监控线程(每3秒)
  ↓
获取最新价格
  ↓
调用 grid_manager.check_grid_signals(stock_code, current_price)
  ↓
检查退出条件(偏离度/盈亏/时间/持仓) → [触发] → 停止网格会话
  ↓                                      ↓
[未触发]                              记录退出原因
  ↓                                      ↓
更新价格追踪器                          通知用户
  ↓
检查是否穿越档位
  ↓
[穿越上方档位] → 标记direction='rising', 记录peak_price, waiting_callback=True
[穿越下方档位] → 标记direction='falling', 记录valley_price, waiting_callback=True
[未穿越] → 继续监控
  ↓
[等待回调中]
  ↓
追踪峰值/谷值
  ↓
检测回调比例
  ↓
[回调≥0.5%] → 生成网格交易信号 → 添加到 latest_signals 队列
  ↓
策略执行线程
  ↓
从 latest_signals 获取信号
  ↓
调用 grid_manager.execute_grid_trade(signal)
  ↓
执行交易(买入/卖出)
  ↓
记录到 grid_trades 表
  ↓
更新 session 统计数据(累计金额、交易次数)
  ↓
重建网格: current_center_price = 成交价
  ↓
重置 PriceTracker
  ↓
设置档位冷却(60秒)
  ↓
触发数据版本更新 → 前端实时刷新
```

### 2.4 停止流程

**手动停止**:
```
用户点击checkbox(选中→未选中)
  ↓
弹出确认对话框
  ↓
确认 → 调用 stop_grid_session(session_id, reason='manual')
  ↓
更新数据库状态为 'stopped'
  ↓
从内存中移除 session 和 tracker
  ↓
checkbox变为未选中
```

**自动停止**:
```
检测到退出条件触发
  ↓
调用 stop_grid_session(session_id, reason='<具体原因>')
  ↓
更新数据库
  ↓
Toast通知用户: "网格交易已停止: <原因>"
  ↓
checkbox自动取消选中
```

---

## 3. 核心算法设计

### 3.1 智能回调机制

**上升回调逻辑(卖出)**:
```python
# 步骤1: 检测穿越上方档位
if current_price > upper_level and not waiting_callback:
    crossed_level = upper_level
    peak_price = current_price
    direction = 'rising'
    waiting_callback = True

# 步骤2: 追踪峰值
if waiting_callback and direction == 'rising':
    if current_price > peak_price:
        peak_price = current_price  # 更新峰值

# 步骤3: 检测回调
callback_ratio = (peak_price - current_price) / peak_price
if callback_ratio >= 0.005:  # 默认0.5%
    → 触发卖出信号
```

**下降回调逻辑(买入)**:
```python
# 步骤1: 检测穿越下方档位
if current_price < lower_level and not waiting_callback:
    crossed_level = lower_level
    valley_price = current_price
    direction = 'falling'
    waiting_callback = True

# 步骤2: 追踪谷值
if waiting_callback and direction == 'falling':
    if current_price < valley_price:
        valley_price = current_price  # 更新谷值

# 步骤3: 检测回升
rebound_ratio = (current_price - valley_price) / valley_price
if rebound_ratio >= 0.005:  # 默认0.5%
    → 触发买入信号
```

**防重复机制**:
- 同一档位触发后进入60秒冷却期
- 使用字典记录: `{(session_id, level): timestamp}`
- 冷却期内该档位不再触发

### 3.2 动态网格重算

**交易后重建逻辑**:
```python
def rebuild_grid_after_trade(session, trade_price):
    """
    以成交价为新中心,重新生成对称网格
    """
    # 更新当前网格中心(原始center_price不变)
    session.current_center_price = trade_price

    # 生成新的对称档位
    interval = session.price_interval  # 如0.05
    new_levels = {
        'lower': trade_price * (1 - interval),  # 如 trade_price * 0.95
        'center': trade_price,
        'upper': trade_price * (1 + interval)   # 如 trade_price * 1.05
    }

    # 重置价格追踪器
    tracker.reset(trade_price)
    tracker.waiting_callback = False
    tracker.direction = None
    tracker.crossed_level = None

    return new_levels
```

**示例**:
```
初始状态:
  center_price (锁定) = 10.00元
  current_center_price = 10.00元
  档位: 9.50, 10.00, 10.50

第1次交易:
  价格涨到10.50 → 峰值10.55 → 回调到10.52 → 卖出成交
  重建网格:
    current_center_price = 10.52元
    新档位: 9.99, 10.52, 11.05

第2次交易:
  价格跌到9.99 → 谷值9.95 → 回升到9.97 → 买入成交
  重建网格:
    current_center_price = 9.97元
    新档位: 9.47, 9.97, 10.47

注意: center_price始终保持10.00元不变,仅用于偏离度计算
```

### 3.3 混合退出机制

**5种退出条件**(任一触发即停止):

**1. 偏离度退出**
```python
deviation = abs(current_center_price - center_price) / center_price
if deviation > max_deviation:  # 默认0.15 (15%)
    stop_reason = 'deviation'
```

**2. 目标盈利退出**
```python
profit_ratio = (total_sell_amount - total_buy_amount) / total_buy_amount
if profit_ratio >= target_profit:  # 默认0.10 (10%)
    stop_reason = 'target_profit'
```

**3. 止损退出**
```python
if profit_ratio <= stop_loss:  # 默认-0.10 (-10%)
    stop_reason = 'stop_loss'
```

**4. 时间限制退出**
```python
if datetime.now() > end_time:  # 默认7天
    stop_reason = 'expired'
```

**5. 持仓清空退出**
```python
if position.volume == 0:
    stop_reason = 'position_cleared'
```

**6. 手动退出**
```python
# 用户点击checkbox取消选中
stop_reason = 'manual'
```

---

## 4. 数据库设计

### 4.1 grid_trading_sessions 表

```sql
CREATE TABLE IF NOT EXISTS grid_trading_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    -- 状态: active, stopped, expired, breakout, target_profit, stop_loss, deviation, position_cleared

    -- 价格配置
    center_price REAL NOT NULL,           -- 原始中心价(锁定的最高价)
    current_center_price REAL,            -- 当前网格中心(动态更新)
    price_interval REAL NOT NULL DEFAULT 0.05,  -- 价格间隔比例

    -- 交易配置
    position_ratio REAL NOT NULL DEFAULT 0.25,  -- 每档交易比例
    callback_ratio REAL NOT NULL DEFAULT 0.005, -- 回调触发比例

    -- 资金配置
    max_investment REAL NOT NULL,         -- 最大追加投入
    current_investment REAL DEFAULT 0,    -- 当前已投入

    -- 退出配置
    max_deviation REAL DEFAULT 0.15,      -- 最大偏离度
    target_profit REAL DEFAULT 0.10,      -- 目标盈利
    stop_loss REAL DEFAULT -0.10,         -- 止损比例

    -- 统计数据
    trade_count INTEGER DEFAULT 0,        -- 总交易次数
    buy_count INTEGER DEFAULT 0,          -- 买入次数
    sell_count INTEGER DEFAULT 0,         -- 卖出次数
    total_buy_amount REAL DEFAULT 0,      -- 累计买入金额
    total_sell_amount REAL DEFAULT 0,     -- 累计卖出金额

    -- 时间戳
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,               -- 预设结束时间
    stop_time TEXT,                       -- 实际停止时间
    stop_reason TEXT,                     -- 停止原因

    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,

    -- 约束: 每个股票只能有一个active会话
    UNIQUE(stock_code, status) ON CONFLICT REPLACE
);

CREATE INDEX idx_grid_sessions_stock ON grid_trading_sessions(stock_code);
CREATE INDEX idx_grid_sessions_status ON grid_trading_sessions(status);
```

### 4.2 grid_trades 表

```sql
CREATE TABLE IF NOT EXISTS grid_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    stock_code TEXT NOT NULL,

    -- 交易信息
    trade_type TEXT NOT NULL,             -- 'BUY', 'SELL'
    grid_level REAL NOT NULL,             -- 档位价格
    trigger_price REAL NOT NULL,          -- 实际成交价
    volume INTEGER NOT NULL,              -- 交易数量
    amount REAL NOT NULL,                 -- 交易金额

    -- 回调信息
    peak_price REAL,                      -- 卖出时的峰值价格
    valley_price REAL,                    -- 买入时的谷值价格
    callback_ratio REAL,                  -- 实际回调比例

    -- 订单信息
    trade_id TEXT,                        -- QMT订单ID或模拟订单ID
    trade_time TEXT NOT NULL,

    -- 网格状态快照
    grid_center_before REAL,              -- 交易前网格中心
    grid_center_after REAL,               -- 交易后网格中心

    created_at TEXT DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (session_id) REFERENCES grid_trading_sessions(id) ON DELETE CASCADE
);

CREATE INDEX idx_grid_trades_session ON grid_trades(session_id);
CREATE INDEX idx_grid_trades_stock ON grid_trades(stock_code);
CREATE INDEX idx_grid_trades_time ON grid_trades(trade_time);
```

---

## 5. 代码架构设计

### 5.1 新增模块

**grid_trading_manager.py** (约600行):
```python
├── GridSession         # 数据模型类(dataclass)
├── PriceTracker        # 价格追踪器类(dataclass)
├── GridTradingManager  # 核心管理器类
│   ├── __init__()                    # 初始化
│   ├── start_grid_session()          # 启动网格会话
│   ├── stop_grid_session()           # 停止网格会话
│   ├── check_grid_signals()          # 检查网格信号(主循环调用)
│   ├── execute_grid_trade()          # 执行网格交易
│   ├── _load_active_sessions()       # 系统启动时恢复会话⭐
│   ├── _check_level_crossing()       # 检查档位穿越
│   ├── _rebuild_grid()               # 重建网格
│   ├── _check_exit_conditions()      # 检查退出条件
│   ├── get_session_stats()           # 获取会话统计
│   └── get_trade_history()           # 获取交易历史
```

### 5.2 修改现有模块

**config.py** (新增约30行):
```python
# 网格交易配置参数
ENABLE_GRID_TRADING = False
GRID_CALLBACK_RATIO = 0.005
GRID_LEVEL_COOLDOWN = 60
GRID_MAX_DEVIATION_RATIO = 0.15
GRID_TARGET_PROFIT_RATIO = 0.10
GRID_STOP_LOSS_RATIO = -0.10
GRID_DEFAULT_DURATION_DAYS = 7
GRID_DEFAULT_PRICE_INTERVAL = 0.05
GRID_DEFAULT_POSITION_RATIO = 0.25
GRID_DEFAULT_MAX_INVESTMENT_RATIO = 0.5

def get_grid_default_config(position_market_value: float) -> dict:
    """获取网格交易默认配置"""
    # ...
```

**position_manager.py** (新增约50行):
```python
class PositionManager:
    def __init__(self):
        # ... 现有代码 ...
        self.grid_manager = None  # 新增

    def init_grid_manager(self, trading_executor):
        """初始化网格管理器"""
        from grid_trading_manager import GridTradingManager
        self.grid_manager = GridTradingManager(
            self.db_manager,
            self,
            trading_executor
        )

    def _update_position_prices(self):
        """更新持仓价格(现有方法,末尾添加)"""
        # ... 现有价格更新逻辑 ...

        # 新增: 网格信号检测(5行)
        if self.grid_manager and config.ENABLE_GRID_TRADING:
            for stock_code, position in self.positions.items():
                signal = self.grid_manager.check_grid_signals(
                    stock_code,
                    position.get('current_price', 0)
                )
                if signal:
                    with self.signal_lock:
                        self.latest_signals[stock_code] = signal
```

**strategy.py** (新增约10行):
```python
def _strategy_loop(self):
    """策略循环(现有方法,信号处理部分添加)"""
    # ... 现有逻辑 ...

    # 处理网格交易信号
    if signal.get('strategy') == 'grid':
        if self.position_manager.grid_manager:
            self.position_manager.grid_manager.execute_grid_trade(signal)
            self.position_manager.mark_signal_processed(stock_code)
```

**web_server.py** (新增约150行):
```python
# 新增API端点
@app.route('/api/grid/start', methods=['POST'])
def start_grid_trading():
    """启动网格交易"""
    # ...

@app.route('/api/grid/stop/<session_id>', methods=['POST'])
def stop_grid_trading(session_id):
    """停止网格交易"""
    # ...

@app.route('/api/grid/sessions', methods=['GET'])
def get_grid_sessions():
    """获取所有网格会话"""
    # ...

@app.route('/api/grid/session/<session_id>', methods=['GET'])
def get_grid_session_detail(session_id):
    """获取网格会话详情"""
    # ...

@app.route('/api/grid/trades/<session_id>', methods=['GET'])
def get_grid_trades(session_id):
    """获取网格交易历史"""
    # ...

@app.route('/api/grid/status/<stock_code>', methods=['GET'])
def get_grid_status(stock_code):
    """获取网格实时状态"""
    # ...

@app.route('/api/grid/stream/<session_id>')
def grid_stream(session_id):
    """SSE实时推送"""
    # ...
```

**database.py** (新增约50行):
```python
class DatabaseManager:
    def init_grid_tables(self):
        """初始化网格交易表"""
        # 创建2个表

    def create_grid_session(self, session_data: dict) -> int:
        """创建网格会话"""
        # ...

    def update_grid_session(self, session_id: int, updates: dict):
        """更新网格会话"""
        # ...

    def stop_grid_session(self, session_id: int, reason: str):
        """停止网格会话"""
        # ...

    def get_active_grid_sessions(self) -> list:
        """获取所有活跃的网格会话"""
        # ...

    def record_grid_trade(self, trade_data: dict) -> int:
        """记录网格交易"""
        # ...

    def get_grid_trades(self, session_id: int, limit=50, offset=0) -> list:
        """获取网格交易历史"""
        # ...
```

**main.py** (新增约20行):
```python
def main():
    # ... 现有初始化 ...

    # 初始化网格交易表
    db_manager.init_grid_tables()

    # 初始化网格交易管理器
    if config.ENABLE_GRID_TRADING:
        position_manager.init_grid_manager(trading_executor)
        logger.info("网格交易管理器初始化完成")

    # ... 启动线程 ...

def cleanup():
    # ... 现有清理 ...

    # 停止所有活跃的网格会话
    if hasattr(position_manager, 'grid_manager') and position_manager.grid_manager:
        try:
            for session in position_manager.grid_manager.sessions.values():
                if session.status == 'active':
                    position_manager.grid_manager.stop_grid_session(
                        session.id,
                        'system_shutdown'
                    )
        except Exception as e:
            logger.error(f"停止网格会话失败: {str(e)}")
```

---

## 6. 系统重启恢复机制 ⭐

### 6.1 恢复策略: 保守模式

**核心思想**: 恢复会话配置,但重置价格追踪器,避免使用过时的峰谷值

**优点**:
- ✅ 安全可靠,不会因过时数据误触发交易
- ✅ 实现简单,逻辑清晰
- ✅ 重启后立即进入正常监控状态

**权衡**:
- ⚠️ 如果重启前正在等待回调,重启后会丢失这个机会
- ⚠️ 需要重新穿越档位才能触发交易

### 6.2 恢复流程

```python
def _load_active_sessions(self):
    """系统启动时从数据库加载活跃会话"""
    logger.info("=" * 60)
    logger.info("系统重启 - 开始恢复网格交易会话")
    logger.info("=" * 60)

    active_sessions = self.db.get_active_grid_sessions()
    recovered_count = 0
    stopped_count = 0

    for session_data in active_sessions:
        stock_code = session_data['stock_code']
        session_id = session_data['id']

        # 1. 检查会话是否已过期
        if datetime.now() > datetime.fromisoformat(session_data['end_time']):
            self.db.stop_grid_session(session_id, 'expired')
            logger.info(f"会话{session_id}({stock_code})已过期,自动停止")
            stopped_count += 1
            continue

        # 2. 检查持仓是否还存在
        position = self.position_manager.get_position(stock_code)
        if not position or position.get('volume', 0) == 0:
            self.db.stop_grid_session(session_id, 'position_cleared')
            logger.info(f"会话{session_id}({stock_code})持仓已清空,自动停止")
            stopped_count += 1
            continue

        # 3. 恢复GridSession对象
        session = GridSession(**session_data)
        self.sessions[stock_code] = session

        # 4. 重置PriceTracker(关键!安全策略)
        current_price = position.get('current_price', session.current_center_price)
        self.trackers[session_id] = PriceTracker(
            session_id=session_id,
            last_price=current_price,
            peak_price=current_price,
            valley_price=current_price,
            direction=None,
            crossed_level=None,
            waiting_callback=False  # 重置为False
        )

        # 5. 清除档位冷却(重启后重新计算)
        cooldown_keys = [k for k in self.level_cooldowns.keys() if k[0] == session_id]
        for key in cooldown_keys:
            del self.level_cooldowns[key]

        # 6. 记录恢复信息
        logger.info(f"恢复会话: {stock_code}")
        logger.info(f"  - 会话ID: {session_id}")
        logger.info(f"  - 原始中心价: {session.center_price:.2f}元(锁定)")
        logger.info(f"  - 当前中心价: {session.current_center_price:.2f}元")
        logger.info(f"  - 当前市价: {current_price:.2f}元")
        logger.info(f"  - 累计交易: {session.trade_count}次(买{session.buy_count}/卖{session.sell_count})")
        logger.info(f"  - 网格盈亏: {session.get_profit_ratio()*100:.2f}%")
        logger.info(f"  - 追踪器状态: 已重置(安全模式)")

        levels = session.get_grid_levels()
        logger.info(f"  - 网格档位: {levels['lower']:.2f} / {levels['center']:.2f} / {levels['upper']:.2f}")
        logger.info(f"  - 剩余时长: {(datetime.fromisoformat(session.end_time) - datetime.now()).days}天")

        recovered_count += 1

    logger.info("=" * 60)
    logger.info(f"网格会话恢复完成: 恢复{recovered_count}个, 自动停止{stopped_count}个")
    logger.info("=" * 60)

    return recovered_count
```

### 6.3 恢复后行为

**正常监控状态**:
- 价格追踪器从当前价格开始工作
- 等待价格穿越档位
- 穿越后等待回调触发交易

**示例场景**:
```
重启前状态:
  current_center_price = 10.50元
  档位: 9.975 / 10.50 / 11.025
  tracker: waiting_callback=True, direction='rising', peak_price=10.80

重启后恢复:
  current_center_price = 10.50元(保持不变)
  档位: 9.975 / 10.50 / 11.025(保持不变)
  tracker: 重置为 waiting_callback=False, 从当前价格10.60开始监控

行为:
  - 如果价格继续上涨到11.025,会重新进入等待回调状态
  - 重启前的那次等待回调机会已丢失(安全权衡)
```

---

## 7. Web界面设计

### 7.1 UI集成方案(最小侵入)

**复用现有checkbox**:
- 持仓列表中每个股票前的checkbox保持原有样式
- 为已触发止盈(`profit_triggered=True`)的股票启用checkbox
- 未触发止盈的股票checkbox置灰禁用,鼠标悬停提示"需先触发止盈"

**checkbox状态映射**:
```
☐ 未选中 + 可点击 → 可启动网格交易
☑ 选中 + 绿色边框 → 网格运行中
☑ 选中 + 黄色边框 → 即将触发退出条件(警告)
☐ 自动取消选中 + Toast通知 → 网格已自动停止
```

### 7.2 配置面板

点击checkbox弹出Modal:
```
┌─────────────────────────────────────────────┐
│  启动网格交易 - 000001.SZ                    │
├─────────────────────────────────────────────┤
│  基础参数                                    │
│  ├ 网格价格间隔: [5.0]% (1-20%)             │
│  ├ 每档交易比例: [25]% (10-50%)             │
│  └ 回调触发比例: [0.5]% (0.1-2%)            │
├─────────────────────────────────────────────┤
│  资金控制                                    │
│  └ 最大追加投入: [12,500]元                 │
│     (建议: 当前持仓市值25,000元 × 50%)      │
├─────────────────────────────────────────────┤
│  退出条件                                    │
│  ├ 运行时长: [7]天                          │
│  ├ 最大偏离: ±[15]%                         │
│  ├ 目标盈利: +[10]%                         │
│  └ 止损比例: -[10]%                         │
├─────────────────────────────────────────────┤
│  网格预览                                    │
│  ├ 锁定中心价: 10.00元 (买后最高价)         │
│  ├ 网格区间: 9.50 ~ 10.50元                 │
│  └ 初始档位: 9.50 / 10.00 / 10.50           │
├─────────────────────────────────────────────┤
│  ⚠️ 风险提示:                                │
│  网格交易适合震荡行情,单边趋势可能触发止损    │
├─────────────────────────────────────────────┤
│  [取消]                     [确认启动]       │
└─────────────────────────────────────────────┘
```

### 7.3 监控面板

点击"运行中🟢"展开详情:
```
┌─────────────────────────────────────────────┐
│  网格交易详情 - 000001.SZ                    │
├─────────────────────────────────────────────┤
│  基础信息                                    │
│  ├ 原始中心价: 10.00元(锁定)                │
│  ├ 当前中心价: 10.52元                      │
│  ├ 网格区间: 9.99 ~ 11.05元                 │
│  ├ 运行时长: 2天5小时 / 7天                 │
│  └ 状态: 运行中 🟢                          │
├─────────────────────────────────────────────┤
│  实时监控                                    │
│  ├ 当前价格: 10.52元                        │
│  ├ 价格追踪: 上升中,峰值10.55元             │
│  ├ 等待回调: 是(已穿越10.50档位)            │
│  ├ 下个买入档位: 9.99元                     │
│  └ 下个卖出档位: 11.05元                    │
├─────────────────────────────────────────────┤
│  统计数据                                    │
│  ├ 交易次数: 8次 (买4 / 卖4)                │
│  ├ 累计买入: 12,000元                       │
│  ├ 累计卖出: 13,200元                       │
│  ├ 网格盈亏: +1,200元 (+10.0%) 🎉          │
│  ├ 已用额度: 2,000元 / 5,000元 (40%)        │
│  └ 剩余持仓: 450股                          │
├─────────────────────────────────────────────┤
│  退出条件监控                                │
│  ├ 偏离度: 5.2% / 15% ✅                    │
│  ├ 盈亏率: +10.0% / +10% ⚠️ 即将触发        │
│  ├ 时间: 2天 / 7天 ✅                       │
│  └ 持仓: 450股 ✅                           │
├─────────────────────────────────────────────┤
│  交易明细 (最近10条)          [查看全部>>]   │
│  ┌──────┬──────┬───────┬──────┬──────┐      │
│  │时间  │类型  │档位价 │成交价│数量  │      │
│  ├──────┼──────┼───────┼──────┼──────┤      │
│  │14:35 │卖出🔴│10.50  │10.48 │112股 │      │
│  │14:12 │买入🟢│10.00  │10.02 │120股 │      │
│  │13:45 │卖出🔴│10.50  │10.49 │115股 │      │
│  │...   │...   │...    │...   │...   │      │
│  └──────┴──────┴───────┴──────┴──────┘      │
├─────────────────────────────────────────────┤
│  [停止网格交易] [导出明细]                   │
└─────────────────────────────────────────────┘
```

### 7.4 API端点

```
POST /api/grid/start
  参数: {stock_code, price_interval, position_ratio, ...}
  返回: {success, session_id, config}

POST /api/grid/stop/<session_id>
  返回: {success, stop_reason, final_stats}

GET /api/grid/sessions
  返回: [{session_id, stock_code, status, stats}, ...]

GET /api/grid/session/<session_id>
  返回: {详细配置, 实时状态, 统计数据}

GET /api/grid/trades/<session_id>?limit=50&offset=0
  返回: {trades: [...], total_count, pagination}

GET /api/grid/status/<stock_code>
  返回: {is_active, current_center_price, grid_levels, tracker}

GET /api/grid/stream/<session_id>
  SSE推送: 价格更新, 交易执行, 退出事件
```

---

## 8. 配置参数说明

### 8.1 config.py 新增参数

```python
# ======================= 网格交易高级配置 =======================

# 总开关
ENABLE_GRID_TRADING = False  # 必须启用才能使用网格交易

# 回调触发机制
GRID_CALLBACK_RATIO = 0.005  # 回调0.5%触发交易

# 档位冷却时间
GRID_LEVEL_COOLDOWN = 60  # 同一档位60秒内不重复触发

# 混合退出机制 - 默认值
GRID_MAX_DEVIATION_RATIO = 0.15    # 网格中心最大偏离±15%
GRID_TARGET_PROFIT_RATIO = 0.10    # 目标盈利10%
GRID_STOP_LOSS_RATIO = -0.10       # 止损-10%
GRID_DEFAULT_DURATION_DAYS = 7     # 默认运行7天

# Web界面默认值
GRID_DEFAULT_PRICE_INTERVAL = 0.05           # 默认价格间隔5%
GRID_DEFAULT_POSITION_RATIO = 0.25           # 默认每档交易25%
GRID_DEFAULT_MAX_INVESTMENT_RATIO = 0.5      # 默认最大投入为持仓市值50%

# 日志级别
GRID_LOG_LEVEL = "INFO"  # DEBUG时输出详细价格追踪
```

### 8.2 可配置项总结

| 配置项 | 默认值 | 可调范围 | 说明 |
|--------|--------|----------|------|
| 价格间隔 | 5% | 1-20% | 网格档位间距 |
| 交易比例 | 25% | 10-50% | 每档交易的持仓比例 |
| 回调比例 | 0.5% | 0.1-2% | 触发交易的回调幅度 |
| 最大投入 | 持仓市值50% | 自定义 | 网格追加投入上限 |
| 运行时长 | 7天 | 1-30天 | 自动到期时间 |
| 最大偏离 | ±15% | 5-30% | 网格中心偏离限制 |
| 目标盈利 | +10% | 1-50% | 盈利自动退出 |
| 止损比例 | -10% | -5 ~ -20% | 亏损自动退出 |
| 档位冷却 | 60秒 | 30-300秒 | 防重复触发间隔 |

---

## 9. 实施计划

### 9.1 开发阶段

**阶段1: 核心模块开发** (预计2-3天)
- [ ] 创建 grid_trading_manager.py
  - [ ] GridSession 数据模型
  - [ ] PriceTracker 状态机
  - [ ] GridTradingManager 核心类
- [ ] 数据库表创建和操作封装
- [ ] 配置参数定义

**阶段2: 现有代码集成** (预计1-2天)
- [ ] position_manager.py 集成
- [ ] strategy.py 信号处理
- [ ] main.py 初始化和清理
- [ ] config.py 参数管理

**阶段3: Web API开发** (预计2-3天)
- [ ] RESTful API端点
- [ ] SSE实时推送
- [ ] 前端UI集成
- [ ] 配置面板开发

**阶段4: 重启恢复机制** (预计1天)
- [ ] 会话恢复逻辑
- [ ] 状态验证和清理
- [ ] 日志记录优化

### 9.2 测试阶段

**单元测试**:
- [ ] PriceTracker 状态转换测试
- [ ] 网格重建算法测试
- [ ] 退出条件触发测试
- [ ] 数据库操作测试

**集成测试**:
- [ ] 完整交易流程测试(模拟模式)
- [ ] 系统重启恢复测试
- [ ] 多股票并发网格测试
- [ ] 异常情况处理测试

**压力测试**:
- [ ] 长时间运行稳定性测试
- [ ] 高频价格更新测试
- [ ] 内存泄漏检测

### 9.3 部署上线

**准备工作**:
- [ ] 备份现有数据库
- [ ] 更新配置文档
- [ ] 准备回滚方案

**分阶段上线**:
1. 模拟环境验证(ENABLE_SIMULATION_MODE=True)
2. 单只股票小额测试
3. 多只股票正常运行
4. 全量开放使用

---

## 10. 风险评估与应对

### 10.1 技术风险

| 风险 | 影响 | 概率 | 应对措施 |
|------|------|------|----------|
| 价格追踪器状态混乱 | 高 | 中 | 严格的状态机设计+单元测试 |
| 档位重复触发 | 中 | 中 | 冷却机制+信号验证 |
| 数据库锁竞争 | 低 | 低 | 线程锁保护+事务隔离 |
| 系统重启数据丢失 | 高 | 低 | 保守恢复策略+日志记录 |

### 10.2 业务风险

| 风险 | 影响 | 概率 | 应对措施 |
|------|------|------|----------|
| 单边趋势行情亏损 | 高 | 中 | 止损机制+偏离度退出 |
| 过度交易手续费 | 中 | 中 | 回调机制+档位冷却 |
| 资金占用过多 | 中 | 低 | 最大投入限制 |
| 用户误操作 | 低 | 中 | 确认对话框+参数验证 |

### 10.3 监控告警

**关键指标监控**:
- 网格会话数量
- 平均交易频率
- 累计盈亏比例
- 异常退出次数

**告警条件**:
- 单个会话交易频率 > 20次/小时
- 网格盈亏 < -5%
- 系统重启恢复失败
- 档位冷却失效(重复触发)

---

## 11. 后续优化方向

### 11.1 功能增强(v2.0)
- [ ] 支持多档位网格(3/5/7档)
- [ ] 非对称网格(上下档位密度不同)
- [ ] 网格组合策略(多股票联动)
- [ ] 智能档位调整(根据波动率)

### 11.2 性能优化
- [ ] 价格追踪器状态持久化(可选)
- [ ] 数据库批量操作优化
- [ ] 前端图表可视化
- [ ] 交易明细导出功能

### 11.3 用户体验
- [ ] 网格模板配置(保存常用参数组合)
- [ ] 一键复制配置到其他股票
- [ ] 历史会话回放分析
- [ ] 盈亏归因分析

---

## 12. 总结

本设计方案遵循**最小侵入原则**,在现有miniQMT系统基础上,以**新增1个核心模块 + 修改4个文件约250行代码**的方式,实现了功能完整、安全可靠的网格交易系统。

**核心亮点**:
1. **智能回调机制**: 避免趋势行情中过早交易
2. **动态网格重建**: 以成交价为中心持续跟踪价格
3. **多重退出保护**: 5种退出条件全面保障安全
4. **保守恢复策略**: 系统重启后安全恢复,无人值守可靠
5. **完整监控体系**: 实时状态+统计数据+交易明细

**设计原则坚持**:
- ✅ KISS: 复用checkbox UI,配置简洁
- ✅ YAGNI: 只实现明确需求,无过度设计
- ✅ DRY: 复用现有架构(双层存储、信号队列、线程模型)
- ✅ 安全第一: 多重保护+保守恢复

本方案已完成详细设计,可直接进入开发实施阶段。
