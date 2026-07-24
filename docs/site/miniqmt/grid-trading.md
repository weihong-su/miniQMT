# 网格交易

## 概述

网格交易在股票价格波动中自动低吸高抛，通过在预设的价格档位之间反复买卖来累积利润。

## 核心概念

### 网格会话（GridSession）

每只股票启动一个独立的网格会话，包含以下参数：

| 参数 | 说明 |
|------|------|
| `center_price` | 网格中心价格 |
| `price_interval` | 价格档位间距（元） |
| `position_ratio` | 每档买入仓位比例 |
| `callback_ratio` | 回调触发比例（0.5%） |
| `max_investment` | 单会话最大投入金额 |
| `max_deviation` | 最大偏离中心价比例（±15%） |
| `target_profit` | 目标盈利比例（10%） |
| `stop_loss` | 网格止损比例（-10%） |

### 价格追踪器（PriceTracker）

实时追踪价格走势，检测网格信号：

- 记录最近峰值/谷值
- 检测是否穿越网格档位
- 判断回调触发条件

### 信号流程

```
价格更新 → PriceTracker.update_price()
  → 检测穿越网格档位 → crossed_level
    → 等待回调确认 → check_callback()
      → 回调达标 → 生成买入/卖出信号
        → execute_grid_trade() → 实际下单
```

---

## 启动条件

创建网格会话前，系统会做最小必要校验：

| 条件 | 当前行为 |
|------|---------|
| 持仓存在 | 必须已有该股票持仓，且持仓数量有效 |
| 首次止盈标记 | 默认不要求 `profit_triggered=True`；`GRID_REQUIRE_PROFIT_TRIGGERED = True` 时才强制要求 |
| 活跃会话 | 同一股票同一时间只能有一个活跃网格会话 |
| 中心价 | 优先使用请求中的 `center_price`，否则使用持仓 `highest_price` 等可用价格 |
| 参数合法性 | `price_interval`、`position_ratio`、`max_investment`、止盈止损等需通过 `grid_validation` 校验 |

!!! note "关于首次止盈限制"
    `GRID_REQUIRE_PROFIT_TRIGGERED` 当前默认值为 `False`，因此持仓个股可以直接启动网格交易。若你希望恢复旧的保守策略，可在 `config.py` 中显式设置为 `True`，也可以通过同名环境变量设置为 `true/1/yes/on`，此时未触发首次止盈的持仓会被拒绝启动网格。

---

## 自动执行开关

网格交易采用三层控制，避免与动态止盈止损互相影响：

| 层级 | 开关 | 作用 |
|------|------|------|
| 总开关 | `ENABLE_AUTO_OPERATION` | 全局自动操作总闸；关闭时所有自动策略不产生新单 |
| 网格分开关 | `ENABLE_GRID_TRADING` | 控制网格模块是否检测并执行网格交易 |
| 个股开关 | `grid_trading_sessions.enabled` | 控制单个网格会话是否继续自动发新网格单 |

Web 中的“自动/暂停”切换对应 `grid_trading_sessions.enabled`。暂停后会话、账本和当前网格参数都会保留，只是不再发出新的网格买卖单；“停止网格”则会结束会话并撤销未完成网格委托。

---

## 退出条件

网格会话在以下条件满足时自动退出：

| 条件 | 说明 |
|------|------|
| 达到目标盈利 | `true_pnl_ratio >= target_profit` |
| 触发止损 | `true_pnl_ratio <= stop_loss` |
| 超出偏离范围 | 有效偏离 `effective_deviation_ratio > max_deviation` |
| 到达结束时间 | `end_time` 到期 |
| 手动停止 | 通过 Web API 或代码调用 |

偏离度分为两个口径：

- **中心漂移偏离**：`drift_deviation = abs(current_center_price - center_price) / center_price`，衡量网格重建后当前中心价相对初始中心价移动了多少。
- **市价偏离**：`market_deviation = abs(current_price - current_center_price) / current_center_price`，衡量当前市价相对当前网格中心价偏离了多少。

自动退出使用 `effective_deviation_ratio = max(drift_deviation, market_deviation)`，任一口径超过 `max_deviation` 都会触发风险退出。Web1.0 悬停卡片中的“中心价偏离”只展示中心漂移偏离，并按带方向的 `center_deviation_ratio = (current_center_price - center_price) / center_price` 标注“上移/下移”；它不是当前市价偏离。

---

## 实盘交易机制 ⭐

模拟模式下网格在内存中直接撮合，落账即时。**实盘模式**则面临委托被拒、部分成交、撤单、滑点、涨跌停封板等真实场景，因此引入了一整套以「**成交回报为准**」的订单闭环机制。下列功能仅在 `ENABLE_SIMULATION_MODE = False` 时生效。

### 委托成交确认（GRID_CONFIRM_LIVE_ORDER_BY_DEAL）

默认 `True`。实盘网格下单成功后**不立即更新网格统计**，而是先把委托登记为「待确认」（`_register_pending_grid_order`，持久化到 `grid_orders` 表），等真正的成交回报 `handle_deal_callback` 到达后才落账并重建网格中心价。

- **幂等保护**：按 `trade_id` 去重，并检查 `grid_trades` 是否已落账，避免重复回报导致重复统计
- **部分成交聚合**：部分成交阶段只累积填充量（`filled_volume`/`filled_amount`/`filled_weighted_price_sum`）并更新 `grid_orders` 状态，**不写** `grid_trades` 和 `trade_records`，不重建网格。待全部成交后一次性聚合落账：1条 `grid_trades`（加权均价）+ 1条 `trade_records` + 1次 `_rebuild_grid`。避免 QMT 拆单（如 1300 股拆成 12 笔）产生重复落账和统计失真
- **事务落账**：`_record_confirmed_grid_trade_with_order` 在单个事务内同时更新 `grid_trades`（成交明细）、`grid_trading_sessions`（会话统计）、`grid_orders`（委托状态）、`grid_lots`/`grid_lot_matches`（账本）
- **普通成交流水延迟写入**：实盘网格委托阶段不写 `trade_records`，只保留 `grid_orders`；收到真实成交回报后再使用券商成交 ID 同步写入 `grid_trades`、LIFO 账本和 `trade_records`，避免 Web 交易记录出现 `ORDER_xxx` 假成交

!!! warning "为何必须以成交回报为准"
    若按「下单即落账」处理，遇到撤单/拒单/部分成交时网格统计会与真实持仓偏离，进而导致重复下单或超额投入。确认机制是实盘网格安全的基石。

### 对手价下单（GRID_USE_COUNTERPARTY_PRICE）

默认 `True`。实盘下单不指定限价，由 executor 取**卖三价（买入）/ 买三价（卖出）**，参考动态止盈的下单逻辑提高成交概率。

- 仅在 `GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True` 时启用（成交以真实回报价落账，统计才准确）
- **资金预占缓冲**（`GRID_COUNTERPARTY_BUY_PRICE_BUFFER_RATIO`，默认 2%）：买入按「风险价」`reserved_price = max(触发价, 现价) × (1 + 缓冲)`（上限不超过涨停价）预占资金校验 `max_investment`，防止成交价高于触发价时突破最大投入。实际落账仍按真实成交价。

### 涨跌停 / 停牌防护（GRID_ENABLE_PRICE_LIMIT_GUARD）

默认 `True`，下单前由 `_check_tradable` 检查标的盘口状态：

| 场景 | 行为 |
|------|------|
| 买入且现价 ≥ 涨停价（封板买不进） | 跳过本次买入 |
| 卖出且现价 ≤ 跌停价（封板卖不出） | 跳过本次卖出 |
| 停牌 / 无有效现价 | 买卖均跳过 |

涨跌停价（`_get_price_limits`）获取失败时 **fail-open**（放行，交由 executor 盘口兜底），仅保留停牌判断。判定容差由 `GRID_PRICE_LIMIT_EPS`（默认 0.001 元）补偿浮点误差。

### 信号执行前复核

信号从检测到执行存在时间差，实盘前由 `_validate_grid_signal_before_execute` 复核：

- **信号有效期**（`GRID_SIGNAL_MAX_AGE_SECONDS`，默认 60 秒）：超龄信号丢弃；时间戳早于现在 5 秒以上（疑似时钟错误）也丢弃
- **价格漂移**（`GRID_SIGNAL_MAX_PRICE_DRIFT_RATIO`，默认 1%）：最新价相对触发价偏离超过阈值时丢弃，避免在已大幅移动的盘口上按陈旧价格成交
- 同时复核会话状态为 `active` 且 `session_id` 匹配

### 启动对账与对手方预留

系统重启后会从 `grid_orders` 表恢复未完成委托（`submitted` / `partial_filled` / `cancel_requested` 等状态），并执行对账补偿（`_reconcile_open_grid_orders`）：

1. 查询券商当日**成交**，对缺失的成交补调 `handle_deal_callback`
2. 查询券商当日**委托**，对已成交但本地未落账的委托合成成交回报补记，对终态委托关闭

这也是实盘成交回调缺失时的兜底确认路径。无论底层使用 miniQMT 直连、大QMT 文件 IPC，还是大QMT RPC，网格实盘流水都必须等成交确认后才写 `trade_records`，避免把仅提交成功、后续被拒单或撤单的委托展示成真实成交。

**对手方预留（reserve）**：下单计划生成时会扣除「待成交委托」占用的资金（买）和持仓（卖），防止锁外下单窗口期重复下单导致超额。

### 真实盈亏账本（Ledger）

`grid_lots`（买入批次库存）+ `grid_lot_matches`（LIFO 配对）构成网格账本，按最近优先逐笔匹配卖出与买入，计算真实已实现/未实现盈亏。普通卖出优先匹配最近买入批次；先卖后买的底仓回补优先匹配最近未回补卖出，以贴近网格“上涨卖出、回落买回”的策略闭环。

`get_pnl_snapshot` 提供**统一盈亏视图**，按数据可用性自动降级：

| 方法 | 触发条件 | 说明 |
|------|---------|------|
| `ledger_true_pnl` | 账本可用 | LIFO 最近优先配对的真实已实现 + 未实现盈亏（最准确） |
| `memory_true_pnl` | 有会话买卖量 + 行情价 | 按内存现金流 + 浮动市值估算 |
| `cash_flow_legacy` | 仅有现金流 | 卖出额 − 买入额，标记为降级 |
| `fallback_market_value_ratio` | 仅有持仓市值 | 现金流 / 持仓市值，标记为降级 |

快照含 `realized_pnl` / `unrealized_pnl` / `total_pnl` / `profit_ratio` / `open_volume` / `is_degraded` 等字段，供 Web 前端（GridStatusPanel）展示利润来源与降级提示。

---

## 通过 Web 界面使用

1. 访问 `http://localhost:5000`
2. 在股票列表中选择目标股票
3. 点击「启动网格」
4. 配置参数（或使用模板）
5. 确认启动

## 通过 API 使用

```bash
# 启动网格（使用默认参数）
curl -X POST http://localhost:5000/api/grid/start \
  -H "Content-Type: application/json" \
  -d '{"stock_code": "000001.SZ", "center_price": 10.50}'

# 查看网格状态
curl http://localhost:5000/api/grid/status/000001.SZ

# 查看所有活跃网格
curl http://localhost:5000/api/grid/sessions

# 暂停/恢复单个网格会话自动执行
curl -X POST http://localhost:5000/api/grid/session/1/enabled \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'

# 停止网格
curl -X POST http://localhost:5000/api/grid/stop \
  -H "Content-Type: application/json" \
  -d '{"session_id": 1}'
```

---

## 风险分级模板

系统内置三档风险模板：

| 模板 | 回调比例 | 档位间距 | 止损 | 目标盈利 |
|------|---------|---------|------|---------|
| 保守 | 0.3% | 0.03 | -5% | 5% |
| 标准 | 0.5% | 0.05 | -10% | 10% |
| 激进 | 1.0% | 0.10 | -15% | 15% |

---

## 冷却机制

防止短时间内重复交易：

| 冷却类型 | 默认值 | 说明 |
|---------|--------|------|
| `GRID_LEVEL_COOLDOWN` | 60 秒 | 同一档位两次交易间隔 |
| `GRID_BUY_COOLDOWN` | 300 秒 | 买入成功后全局冷却 |
| `GRID_SELL_COOLDOWN` | 300 秒 | 卖出成功后全局冷却 |

---

## 注意事项

- 默认不要求先触发首次止盈即可启动网格；如需更保守风控，可设置 `GRID_REQUIRE_PROFIT_TRIGGERED = True` 或同名环境变量
- 每只股票同一时间只能有一个活跃网格会话
- 单个网格会话可通过 Web”自动/暂停”开关临时禁止新网格单，不等同于停止会话
- **买卖量基数统一**：买入量与卖出量使用同一基数 `current_volume × position_ratio`（有持仓时），确保每档买卖操作量对称；首次买入（无持仓）回退为基于 `max_investment × position_ratio / price` 计算
- 网格数据持久化在 SQLite `grid_trading_sessions` / `grid_trades` / `grid_orders` / `grid_lots` / `grid_lot_matches` 表中（详见[数据库表结构](database.md)）
- 实盘网格下单以**成交回报**为准（`GRID_CONFIRM_LIVE_ORDER_BY_DEAL`）：委托先进入 `grid_orders`，真实成交后才写 `grid_trades`、账本和普通 `trade_records`，系统重启自动对账恢复未完成委托
- 建议在模拟模式下充分验证策略后再切换实盘
