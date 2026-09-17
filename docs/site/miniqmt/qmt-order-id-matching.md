# QMT order_id 匹配调试与加固

本文记录 2026-08-11 对 QMT 异步下单 `seq -> order_id` 行为的实盘验证结论，以及主程序中的加固原则。文档仅保留可复用经验，不记录真实账号、确认串或可触发实盘交易的 Web 调试入口。

## 背景

miniQMT 默认使用 `order_stock_async()` 下单。该接口同步返回的是请求序号 `seq`，真实券商委托号 `order_id` 需要等待 QMT 异步回调 `on_order_stock_async_response()` 到达后才能建立映射。

> **日志术语**：自 2026-09-17 起，日志中 `seq` 统一写作 `请求序号=`、`order_id` 统一写作 `委托号=`、`order_sysid` 写作 `柜台编号=`、`traded_id` 写作 `成交编号=`、`trade_records.trade_id` 写作 `流水号=`。完整术语表见 [CLAUDE.md](https://github.com/Su-M10/miniQMT/blob/main/CLAUDE.md) 开发规范一节。查日志时注意 `委托号` 与 `柜台编号` 是两个不同的值。

2026-08-11 09:30 前后，自动止盈止损开启后暴露的核心风险是：卖出委托已经提交到 QMT，但主程序未能可靠把 `seq` 匹配为真实 `order_id`。如果程序把“已提交但未确认 order_id”的订单当作失败继续重试，就可能造成同股同方向重复卖出委托。

## 已确认的 QMT 行为

本次通过主程序连接、callback、订阅和账号绑定链路完成验证，结论如下：

| 场景 | 实盘行为 |
|------|----------|
| 高价限价卖出后撤单 | `order_stock_async()` 返回正 `seq`，随后异步回调给出真实 `order_id`；委托先进入已报状态，撤单后终态为已撤，成交量为 0 |
| 开盘后真实成交 | `seq` 可通过异步回调映射到真实 `order_id`；成交回报中的 `order_id` 与委托列表一致；成交后持仓可用数量同步减少 |
| 主程序重启后再验证 | 重启后 callback 映射、委托查询、撤单终态仍可形成闭环 |

由此确认：

- `order_stock_async()` 的返回值是 `seq`，不能当作撤单或成交确认使用的真实 `order_id`。
- `on_order_stock_async_response()` 是最快、最准确的 `seq -> order_id` 来源。
- `query_stock_orders()` 能看到当日委托，包括已报、已撤、已成等终态。
- `query_stock_trades()` 能看到真实成交，成交回报中的 `order_id` 与委托查询一致。
- QMT 可能截断或改写 `order_remark`，因此 `order_remark` 不能作为硬过滤条件。
- 对 `price_type=5` 等非固定价委托，QMT 委托列表中的价格可能落成实际委托价，不能用提交前价格硬过滤反查。

## 主程序加固原则

### 映射优先级

`_get_real_order_id()` 的解析顺序必须是：

1. 先等待 `order_id_map` 中的 `seq -> order_id` callback 映射。
2. callback 超时后，再按股票、方向、数量、策略、时间窗口查询当日委托和成交反查。
3. 若唯一命中，回填 `order_id_map`，后续撤单、成交确认和日志都使用真实 `order_id`。
4. 若多候选或查询失败，保守返回 `None`，并由执行器进入 unknown 冷却，禁止重复发单。

### 字段匹配口径

- 股票代码统一按 6 位裸代码匹配，兼容带市场后缀和不带后缀的写法。
- 方向字段兼容 `23/24`、`23.0/24.0`、中文买卖方向。
- 数量优先匹配委托数量；只有成交列表没有委托数量时，才用成交数量做部分成交兜底。
- `order_remark` 不作为硬过滤条件，只记录不一致原因。
- `strategy_name` 精确匹配优先；若 QMT 截断较长策略名，允许长前缀匹配；多候选仍保守失败。
- 仅 `price_type=11` 固定价委托用价格辅助过滤；非固定价委托不使用提交前价格过滤。
- 报单时间兼容 Unix 秒、`HHMMSS`、`YYYYMMDDHHMMSS`、字符串时间；匹配窗口由配置控制。

### unknown 委托保护

当 QMT 返回正 `seq`，但主程序未能及时拿到真实 `order_id` 时：

- 该订单视为“券商侧可能已接收”，不能继续重试下单。
- 按同股票、同方向进入 `ASYNC_ORDER_UNKNOWN_COOLDOWN_SECONDS` 冷却。
- 如果迟到 callback 后续补上 `seq -> order_id`，应解除 unknown，并补齐最小 `order_cache`；若有策略信号上下文，也补入 `pending_orders`，让成交/撤单回调继续闭环。

## 相关配置

| 配置 | 默认值 | 说明 |
|------|--------|------|
| `USE_SYNC_ORDER_API` | `False` | 默认使用异步下单，返回 `seq` |
| `ASYNC_ORDER_ID_WAIT_TIMEOUT_SECONDS` | `2.0` | 等待 callback 映射的时间 |
| `ASYNC_ORDER_QUERY_FALLBACK_TIMEOUT_SECONDS` | `30.0` | callback 未到时，查询委托/成交反查的最长时间 |
| `ASYNC_ORDER_QUERY_MATCH_PRE_WINDOW_SECONDS` | `5.0` | 允许匹配下单前小窗口，兼容本机与 QMT 时间差 |
| `ASYNC_ORDER_QUERY_MATCH_POST_WINDOW_SECONDS` | `30.0` | 允许匹配下单后小窗口，避免误配历史委托 |
| `ASYNC_ORDER_UNKNOWN_COOLDOWN_SECONDS` | `300` | 正 `seq` 未确认时，同股同方向暂停重复提交 |
| `QMT_ORDER_ID_MAP_TTL_SECONDS` | `86400` | `seq -> order_id` 映射保留时间 |
| `QMT_ORDER_ID_MAP_MAX_ENTRIES` | `4096` | `order_id_map` 最大键数量，`int/str` 双键计入 |

## 安全要求

早期实盘验证曾临时使用 Web 窄接口记录 QMT 行为。该类接口已经移除，原因是：

- 它固定绑定真实账号、股票和数量，存在敏感信息暴露风险。
- 它具备实盘下单能力，即使有 token 和确认串，也不适合作为长期 Web API 保留。
- 后续如需实盘诊断，应使用主程序内部日志、QMT 委托/成交查询、最小化本地脚本，并且不得把真实账号或可下单调试入口提交到仓库。

## 9:30 问题场景的覆盖结论

本次加固覆盖 2026-08-11 09:30 前后暴露的核心问题：卖出委托已经提交到 QMT，但主程序无法可靠把 `seq` 匹配为真实 `order_id`，进而产生告警或重复下单风险。

当前闭环是：

1. 下单后优先等待 callback 映射。
2. callback 未到时主动查当日委托和成交反查。
3. 仍不确定时停止重试，标记 unknown 冷却。
4. 迟到映射到达后解除 unknown，并补齐订单缓存。
5. 成交以 `on_stock_trade` / `query_stock_trades` 为准，撤单以委托终态和撤单回报为准。

因此，针对“自动止盈止损触发卖出后拿不到真实 `order_id`”这一类问题，当前代码已具备防重复、可反查、可撤单、可成交确认的保护链路。
