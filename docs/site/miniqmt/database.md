# 数据库表结构

## positions（持仓表）

核心持仓信息，双层存储（内存 + SQLite）。

| 字段 | 数据来源 | 更新频率 | 说明 |
|------|---------|---------|------|
| `stock_code` | QMT 实盘 | 首次同步 | 股票代码 |
| `stock_name` | data_manager | 首次建仓 | 股票名称 |
| `volume` | QMT 实盘 | 10 秒 | 持仓数量 |
| `available` | QMT 实盘 | 10 秒 | 可用数量 |
| `cost_price` | QMT 实盘 | 10 秒 | 平均持仓成本 |
| `base_cost_price` | 持久化 | 首次建仓 | 初次建仓成本（补仓摊薄后保持不变）。SQLite 中已有的有效值（> 0）优先，不被内存快照覆盖；旧库迁移时自动用 `cost_price` 回填 |
| `current_price` | data_manager | 实时 | 当前价格 |
| `market_value` | 计算 | 实时 | 市值 |
| `profit_ratio` | 计算 | 实时 | 盈亏比例 |
| `last_update` | 持久化 | 每次更新 | 最后更新时间 |
| `open_date` | 持久化 | 首次买入 | 开仓日期 |
| `profit_triggered` | 持久化 | 首次止盈 | 是否已触发首次止盈 |
| `highest_price` | 持久化 | 价格更新时 | 持仓期间最高价 |
| `stop_loss_price` | 持久化 | 策略触发时 | 止损/止盈价格 |
| `profit_breakout_triggered` | 持久化 | 首次突破 | 是否已突破止盈阈值（首次止盈的回撤监控状态） |
| `breakout_highest_price` | 持久化 | 突破后价格 | 突破止盈阈值后的最高价 |
| `stop_profit_enabled` | 持久化 | 建仓时初始化为 1 | 个股级动态止盈止损开关（`1`=开启，默认；`0`=暂停）。在全局开关 `ENABLE_DYNAMIC_STOP_PROFIT` + `ENABLE_AUTO_TRADING` 开启的前提下，关闭后该股不再检测止盈止损信号。旧库自动迁移补齐首列为 `1` |

### 关键字段说明

- **`profit_triggered`**：影响后续动态止盈逻辑，首次止盈前不启用动态止盈
- **`highest_price`**：用于计算动态止盈位，持续更新
- **`stop_loss_price`**：低于此价格触发全部卖出
- **`base_cost_price`**：只在 SQLite 中的值缺失或无效（`NULL` / `<= 0`）时才写入，写入源依次为内存快照的 `base_cost_price`、`cost_price`。定时同步与 `update_position()` 都遵守该规则，因此补仓摊薄或 QMT 持仓刷新不会抹掉初次建仓成本

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
| `strategy` | 策略标识：`simu`(模拟) / `auto_partial`(浮盈) / `auto_full`(止盈) / `stop_loss`(止损) / `grid`(网格) / `manual`(手动，网关侧) / `M_real`(手买) / `M_simu`(模买) / `manual_real`(手卖) / `manual_simu`(模卖) / `external`(外部) / `default`(默认)。括号内为 Web 界面显示标签 |
| `timestamp` | 交易时间 |

实盘网格在 `GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True` 时，委托阶段不会写入本表；只有收到真实成交回报并完成网格账本落账后，才补写 `strategy = grid` 的普通成交流水。

### 归因扩展字段（v3.9.1 新增 17 列）

v3.9.1 起本表改为「**成交当下写死归因所需的一切**」，导出退化为纯 SELECT。
新增列如下（完整语义见[交割单数据管道](settlement-export.md)）：

| 字段 | 说明 |
|------|------|
| `account` | 账号标识，迁移时按库路径回填 |
| `deal_time` / `deal_time_str` | **交易所成交时间**（Unix 秒 + 可读串）。取不到时留空 |
| `time_source` | 成交时间来源：`exchange` / `local_fallback` / `reconcile_backfill` / `broker` |
| `recorded_at` | 落库时刻（**不是成交时刻**，不要拿它当成交时间） |
| `order_id` | 委托编号。网格路径写的 `trade_id` 本就是它 |
| `fill_ids` / `fills` | 原始成交编号（分号分隔）/ 合并笔数；库里存原始 deal 粒度，`fills` 恒为 1 |
| `strategy_code` / `strategy_label` | 策略内部标识 / 中文枚举标签（落库时写死） |
| `is_simulation` | 是否模拟成交 |
| `commission_source` / `commission_rate` | 手续费来源（`broker`/`estimated`/`unknown`）与实际费率 |
| `side_source` | 买卖方向来源 |
| `row_status` / `duplicate_of` | `active` / `superseded` + 指向保留行的 id |
| `trade_id_source` | `traded_id`(20 位成交编号) / `order_id`(9-10 位) / `placeholder` |

!!! warning "trade_id 不是全局唯一成交编号"
    本表 `trade_id` 混存三种语义：`ORDER_` 前缀（占位流水，已归档）、
    9-10 位短数字（**网格路径写入的 `str(order_id)`**，跨标的复用）、
    20 位长数字（真实成交编号）。因此唯一键必须包含 `stock_code`/`trade_type`/价量，
    不能只按 `(account, trade_id)`。

---

## 交割单相关表 ⭐（v3.9.1 新增）

| 表 | 主键 | 用途 |
|----|------|------|
| `position_snapshot` | `(account, snapshot_date, code, snapshot_type)` | 每交易日 09:25(`open`) / 15:05(`close`) 全量持仓快照 |
| `account_equity_daily` | `(account, date, snapshot_type)` | 每日净值 + 资产恒等式校验 + 跳变标记 |
| `run_events` | 自增 | 结构化运行事件（对账失配、落库失败、快照缺失等） |
| `trade_records_sim` | 自增 | **模拟成交独立表**，与实盘物理隔离 |
| `broker_deals` / `broker_orders` | 自增 | 券商对账单原始成交 / 委托 |

!!! warning "positions 表不能当历史用"
    `positions` 是「**当前**持仓」，会被持续覆盖写。对账基准只能取 `position_snapshot`。

### account_equity_daily 字段

| 字段 | 说明 |
|------|------|
| `total_asset` / `market_value` / `cash` / `frozen_cash` | QMT `XtAsset` 的原始字段。**没有 `available` 列**——它与 `cash` 是同一个数 |
| `deposit` / `withdraw` / `cum_deposit` / `deposit_source` | 出入金。QMT **无任何出入金接口**，默认 NULL，只能由对账单导入或人工填报 |
| `daily_pnl` / `unexplained_delta` | 派生：`close − open`（未扣出入金）；超过阈值时标 `unexplained_delta` 供人工判断 |
| `source` | `qmt_api` / `simulation`（如实填写，不抹来源痕迹） |

**写入前校验**：`total_asset <= 0` 或四项全零一律拒写并落
`run_events(asset_write_failed, reason=invalid_asset_reading)`。
QMT 未连接时 `balance()` 返回整行 0，而**资产恒等式拦不住它**（`0 == 0+0+0` 恒成立）。

### run_events.event_type 枚举

`startup` / `shutdown` / `qmt_disconnect` / `qmt_reconnect` / `deal_received` /
`deal_persist_failed` / `deal_key_collision` / `reconcile_backfill` /
`position_mismatch` / `position_snapshot_written` / `snapshot_write_failed` /
`snapshot_missing_streak` / `asset_write_failed` / `asset_identity_mismatch` /
`asset_jump` / `stop_loss_triggered` / `grid_order_skipped`

---

## 网格交易表

### grid_trading_sessions（网格会话表）

| 字段 | 说明 |
|------|------|
| `id` | 会话 ID |
| `stock_code` | 股票代码 |
| `status` | `active` / `stopped` / `completed` |
| `enabled` | 自动执行开关（`1`=自动，`0`=暂停）；暂停后保留会话数据，不发新网格单 |
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
