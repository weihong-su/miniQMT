# 数据库表结构

## positions（持仓表）

核心持仓信息，双层存储（内存 + SQLite）。

| 字段 | 数据来源 | 更新频率 | 说明 |
|------|---------|---------|------|
| `stock_code` | QMT 实盘 | 首次同步 | 股票代码 |
| `volume` | QMT 实盘 | 10 秒 | 持仓数量 |
| `available` | QMT 实盘 | 10 秒 | 可用数量 |
| `cost_price` | QMT 实盘 | 10 秒 | 成本价 |
| `current_price` | data_manager | 实时 | 当前价格 |
| `market_value` | 计算 | 实时 | 市值 |
| `profit_ratio` | 计算 | 实时 | 盈亏比例 |
| `open_date` | 持久化 | 首次买入 | 开仓日期 |
| `profit_triggered` | 持久化 | 首次止盈 | 是否已触发首次止盈 |
| `highest_price` | 持久化 | 价格更新时 | 持仓期间最高价 |
| `stop_loss_price` | 持久化 | 策略触发时 | 止损价格 |

### 关键字段说明

- **`profit_triggered`**：影响后续动态止盈逻辑，首次止盈前不启用动态止盈
- **`highest_price`**：用于计算动态止盈位，持续更新
- **`stop_loss_price`**：低于此价格触发全部卖出

---

## trade_records（交易记录表）

记录所有买卖交易。

| 字段 | 说明 |
|------|------|
| `stock_code` | 股票代码 |
| `trade_type` | `BUY` / `SELL` |
| `price` | 成交价格 |
| `volume` | 成交数量 |
| `trade_id` | 成交/流水 ID；模拟为 `SIM{timestamp}{counter}`，普通实盘流水可使用订单 ID，实盘网格确认模式下为券商成交回报 ID |
| `strategy` | 策略标识（`simu` / `auto_partial` / `stop_loss` / `grid`） |
| `timestamp` | 交易时间 |

实盘网格在 `GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True` 时，委托阶段不会写入本表；只有收到真实成交回报并完成网格账本落账后，才补写 `strategy = grid` 的普通成交流水。

---

## 网格交易表

### grid_trading_sessions（网格会话表）

| 字段 | 说明 |
|------|------|
| `id` | 会话 ID |
| `stock_code` | 股票代码 |
| `status` | `active` / `stopped` / `completed` |
| `center_price` | 网格中心价格 |
| `current_center_price` | 当前中心价格（成交后调整） |
| `price_interval` | 档位间距 |
| `position_ratio` | 每档仓位比例 |
| `callback_ratio` | 回调触发比例 |
| `max_investment` | 最大投入金额 |
| `current_investment` | 当前已投入金额 |
| `max_deviation` | 最大偏离比例 |
| `target_profit` | 目标盈利比例 |
| `stop_loss` | 止损比例 |
| `trade_count` / `buy_count` / `sell_count` | 成交次数统计 |
| `total_buy_amount` / `total_sell_amount` | 累计买入/卖出金额 |
| `total_buy_volume` / `total_sell_volume` | 累计买入/卖出股数（真实盈亏计算用） |
| `start_time` / `end_time` / `stop_time` / `stop_reason` | 会话时间与停止原因 |
| `risk_level` | 风险等级 `conservative` / `moderate` / `aggressive` |
| `template_name` | 关联的配置模板名称 |

### grid_trades（网格成交明细表）

| 字段 | 说明 |
|------|------|
| `session_id` | 关联的网格会话 ID |
| `stock_code` | 股票代码 |
| `trade_type` | `BUY` / `SELL` |
| `grid_level` | 触发的网格档位 |
| `trigger_price` | 触发价格 |
| `volume` / `amount` | 成交数量 / 金额 |
| `peak_price` / `valley_price` / `callback_ratio` | 信号回调追踪上下文 |
| `trade_id` | 成交 ID（实盘为券商回报 ID） |
| `grid_center_before` / `grid_center_after` | 成交前后的网格中心价 |
| `trade_time` | 交易时间 |

---

## 网格实盘订单与账本表 ⭐

实盘模式以**成交回报**为准的订单闭环依赖以下三张表（详见[网格交易 · 实盘交易机制](grid-trading.md)）。网格委托阶段只更新 `grid_orders`，真实成交确认后才写 `grid_trades`、`grid_lots` / `grid_lot_matches` 和普通 `trade_records`。

### grid_orders（网格委托表）

登记每笔实盘委托，待成交回报到达后更新状态。它是已报未成交网格单的唯一本地落点，用于重启恢复、拒单/撤单状态闭环和成交补偿对账。

| 字段 | 说明 |
|------|------|
| `order_id` | 委托 ID（主键，券商返回） |
| `session_id` | 关联会话 |
| `stock_code` | 股票代码 |
| `side` | `BUY` / `SELL` |
| `status` | `submitted` / `partial_filled` / `filled` / `canceled` / `rejected` / `cancel_requested` 等 |
| `requested_volume` | 委托数量 |
| `expected_price` | 期望价格 |
| `reserved_price` | 对手价资金预占风险价 |
| `filled_volume` / `filled_amount` | 已成交数量 / 金额 |
| `last_error` | 最近错误信息 |
| `submitted_at` / `updated_at` | 提交 / 更新时间 |
| `raw_signal` | 触发信号（JSON） |

### grid_lots（网格买入批次表）

记录每笔网格买入形成的库存批次，供 LIFO（最近优先）策略配对。

| 字段 | 说明 |
|------|------|
| `id` | 批次 ID |
| `session_id` / `stock_code` | 关联会话 / 股票代码 |
| `buy_trade_id` / `buy_order_id` | 买入成交 / 委托 ID |
| `buy_price` / `buy_amount` | 买入价 / 买入总额 |
| `original_volume` | 买入数量 |
| `remaining_volume` | 剩余未卖数量 |
| `realized_volume` | 已卖出数量 |
| `status` | `open` / `closed` |
| `opened_at` / `updated_at` | 建仓 / 更新时间 |

### grid_lot_matches（LIFO 配对表）

记录卖出与买入批次的 LIFO（最近优先）配对，计算真实已实现盈亏。普通卖出优先匹配最近买入批次；先卖后买的底仓回补优先匹配最近未回补卖出。

| 字段 | 说明 |
|------|------|
| `id` | 配对 ID |
| `session_id` / `stock_code` | 关联会话 / 股票代码 |
| `buy_lot_id` | 对应买入批次（`NULL` 表示底仓卖出） |
| `sell_trade_id` / `sell_order_id` | 卖出成交 / 委托 ID |
| `match_type` | `matched`（配对买单）/ `unmatched`（底仓/余量） |
| `volume` | 配对数量 |
| `buy_price` / `sell_price` | 买入价 / 卖出价 |
| `buy_amount` / `sell_amount` | 买入额 / 卖出额 |
| `realized_pnl` | 已实现盈亏 `(sell_price − buy_price) × volume` |
| `matched_at` | 配对时间 |

> 此外还有 `grid_config_templates`（网格配置模板表）持久化用户保存的参数模板。

---

## 同步机制

```
内存数据库 ←→ SQLite

每 15 秒（POSITION_SYNC_INTERVAL）:
  内存 → SQLite: 持久化所有 positions 表的持久化字段
  SQLite → 内存: 恢复 open_date、profit_triggered、highest_price 等字段
```

- 内存数据库存储高频更新数据（价格、市值、盈亏比例）
- SQLite 持久化关键状态，系统重启后自动恢复
- 修改内存数据后必须调用 `_increment_data_version()` 触发前端更新
