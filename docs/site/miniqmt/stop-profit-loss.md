# 止盈止损策略

## 策略概述

miniQMT 采用**动态止盈止损**策略，包含两个阶段：

1. **首次止盈**：盈利达到阈值后，卖出部分持仓（60%）
2. **动态止盈**：根据持仓期间最高价动态调整止盈位

---

## 首次止盈

**触发条件**：盈利比例 >= `INITIAL_TAKE_PROFIT_RATIO`（6%）

**执行动作**：卖出 `INITIAL_TAKE_PROFIT_RATIO_PERCENTAGE`（60%）持仓

**回撤触发**：达到首次止盈阈值后，从高点回落 `INITIAL_TAKE_PROFIT_PULLBACK_RATIO`（0.5%）时执行卖出

```
盈利 6% → 标记 profit_triggered = True → 等待回撤 0.5% → 卖出 60%
```

!!! note "标记持久化"
    `profit_triggered` 标记保存在 SQLite 中，系统重启后不丢失。

---

## 动态止盈

首次止盈后，系统持续跟踪持仓期间的最高价，按档位动态调整止盈位。

**档位表**：

| 最高浮盈比例 | 止盈位系数 | 示例（成本 10 元） |
|:----------:|:---------:|:----------------:|
| 5% | × 96% | 最高 10.50 → 止盈 10.08 |
| 10% | × 93% | 最高 11.00 → 止盈 10.23 |
| 15% | × 90% | 最高 11.50 → 止盈 10.35 |
| 20% | × 87% | 最高 12.00 → 止盈 10.44 |
| 30% | × 85% | 最高 13.00 → 止盈 11.05 |

**工作流程**：

```
每 3 秒持仓监控循环:
  1. 更新 current_price → 如果 > highest_price，更新 highest_price
  2. 计算当前浮盈比例 = (highest_price - cost_price) / cost_price
  3. 查找对应档位的止盈位系数
  4. 计算止盈价格 = highest_price × 系数
  5. 如果 current_price < 止盈价格 → 触发卖出信号
```

---

## 信号检测与执行的开关门控

止盈止损遵循"检测与执行分离"设计，但**信号入队本身也受开关门控**，避免关闭自动交易时仍反复检测刷屏：

| 开关 | 作用 |
|------|------|
| `ENABLE_AUTO_OPERATION` | 全局自动操作总开关，关闭时所有自动策略不产生新交易动作 |
| `ENABLE_DYNAMIC_STOP_PROFIT` | 动态止盈止损**检测**开关 |
| `ENABLE_AUTO_TRADING` | 动态止盈止损**执行**开关 |

持仓监控线程仅在 **`ENABLE_DYNAMIC_STOP_PROFIT` 且 `ENABLE_AUTO_TRADING` 同时开启**时，才检测动态止盈止损信号并写入 `latest_signals` 队列（`_detect_and_enqueue_dynamic_signal`）。

!!! warning "为何检测也要门控"
    若仅按总开关检测、按执行开关执行，则当"允许自动止盈"(`ENABLE_AUTO_TRADING`)关闭而持仓持续满足止盈条件时，会形成"监控检测 → 策略因自动交易关闭而清除 → 监控再检测"的每 3 秒死循环，日志刷屏（曾出现单账户 `take_profit_full` 一天刷屏近 2 万行）。因此关闭执行开关时直接跳过检测，并清理残留动态信号（保留 `grid_` 网格信号）。网格交易走独立分支（`ENABLE_GRID_TRADING`），不受此门控影响。

---

## 止损

**触发条件**：盈利比例 <= `STOP_LOSS_RATIO`（-7.5%）

**执行动作**：全部卖出

止损与止盈独立判断，优先级更高。止损信号一经检测立即执行，不受 `profit_triggered` 标记影响。

---

## 关键数据库字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `profit_triggered` | bool | 是否已触发首次止盈 |
| `highest_price` | float | 持仓期间最高价 |
| `stop_loss_price` | float | 止损价格 |
| `open_date` | str | 开仓日期 |

这些字段保存在 SQLite `positions` 表中，系统重启后自动恢复。

---

## 配置示例

```python
# config.py — 激进型配置
STOP_LOSS_RATIO = -0.05           # 止损收紧到 -5%
INITIAL_TAKE_PROFIT_RATIO = 0.04  # 首次止盈提前到 4%
INITIAL_TAKE_PROFIT_RATIO_PERCENTAGE = 0.5  # 首次卖出 50%

# config.py — 保守型配置
STOP_LOSS_RATIO = -0.10           # 止损放宽到 -10%
INITIAL_TAKE_PROFIT_RATIO = 0.08  # 首次止盈延后到 8%
INITIAL_TAKE_PROFIT_RATIO_PERCENTAGE = 0.7  # 首次卖出 70%
```
