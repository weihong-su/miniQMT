# Web API

miniQMT 提供 RESTful API。Flask 直连模式暴露完整 web1.0 API；xtquant_manager 网关模式暴露一部分 Flask 兼容端点，并额外提供 `/api/v1/*` 多账号 API。

| 后端 | 默认地址 | 适用 |
|------|---------|-----|
| Flask 直连 (`web_server.py`) | `http://127.0.0.1:5000`（每账号一个端口） | web1.0、单机完整功能 |
| xtquant_manager 网关 | `http://127.0.0.1:8888` | web2.0、多账号、远程访问 |

**认证**：Token 通过 `QMT_API_TOKEN` 环境变量（Flask）或 XtQuantManager 服务端 Token（优先级 `XQM_API_TOKEN > QMT_API_TOKEN > xtquant_manager_config.json/api_token`）设置，请求统一使用 `X-API-Token` 头。

- **Flask 直连模式**：v3.8.9 起统一保护所有 `/api/*`，包括 GET 查询和 `/api/sse` 实时流；设置 `QMT_API_TOKEN` 后必须携带请求头 `X-API-Token: <token>`，不再建议把 Token 放进 URL 查询参数。未设置 `QMT_API_TOKEN` 且 `WEB_PUBLIC_MODE=false` 时放行，适合默认只绑 `127.0.0.1` 的本机调试。
- **Flask 公网模式**：设置 `WEB_PUBLIC_MODE=true` 后，即使 `QMT_API_TOKEN` 为空也拒绝所有 `/api/*`，用于宝塔面板、内网穿透、反向代理等公网暴露场景的 fail-closed 保护。
- **网关模式**：**所有数据端点（含只读）均需 `X-API-Token`**，只读端点同样会返回持仓成本、盈亏与成交明细，属财务隐私数据。唯一例外是 `/api/v1/health`（存活探针）——免 Token 可达，但无 Token 时只返回 `total`/`healthy` 计数，不返回 `accounts` 明细。`api_token` 为空时仅本机可访问，非本机一律拒绝。
- 网关默认**不信任** `X-Forwarded-For`（`trust_proxy: false`），避免伪造该头冒充本机绕过认证；仅在受信任反向代理之后才可开启。详见 [XQM 安全配置](../xqm/guides/security.md)。

**多账号路由（网关）**：通过 `X-Account-Id` 请求头切换目标账号；未指定时回退到第一个已注册账号。

!!! note "表格里的 Token 标注"
    历史文档在写接口旁标注 **🔑 需 Token**。v3.8.9 起，Flask 直连只要配置了 `QMT_API_TOKEN`，所有 `/api/*` 都需要 Token；这些标注仅用于提醒“该端点一定是敏感写操作”，不是鉴权范围的全部。

## 股票代码入参

股票代码参数支持后缀格式、前缀格式和 6 位裸代码，后端会统一归一为 `XXXXXX.SH` / `XXXXXX.SZ` / `XXXXXX.BJ`。

| 输入 | 归一结果 | 说明 |
|------|----------|------|
| `000001` / `SZ.000001` | `000001.SZ` | 深市 |
| `600036` / `SH.600036` | `600036.SH` | 沪市 |
| `920118` / `BJ.920118` | `920118.BJ` | 北交所 |

北交所 `.BJ` 当前不走 Mootdx 行情兜底；行情源不支持时会保守返回空或跳过。

下列表格中 **🌐 网关** 列含义：

- ✅ 完整 — 网关模式可用，行为与 Flask 一致
- 🔒 只读 — 网关模式仅返回数据，不接受写操作
- ❌ 不可用 — 网关未实现，需 Flask 直连模式

---

## 系统状态

| 方法 | 路径 | 说明 | 🌐 网关 |
|------|------|------|--------|
| GET | `/api/connection/status` | QMT 连接状态 | ✅ 完整 |
| GET | `/api/status` | 系统运行状态总览 | ✅ 完整 |
| GET | `/api/market/health` | 行情源健康评分内存快照（xtdata/Mootdx 成功率、延迟、新鲜度、数据质量） | ❌ |
| GET | `/api/macd/advice` | MACD 操盘建议（底仓/网格方向 + 迷你全景图） | ✅ 完整 |
| GET | `/api/debug/status` | 详细调试状态 | ❌ |
| GET | `/api/accounts` | 列出已注册账号（供前端账号发现）🔑 需 Token ——**仅网关提供**，Flask 直连无此端点 | ✅ 完整 |

---

## 行情源健康 {#market-health}

`GET /api/market/health` 返回内存中的行情源健康快照，不触发行情请求、不落库，重启后样本清空。当前默认 `MARKET_HEALTH_OBSERVE_ONLY = False`，持仓监控会按健康评分与数据源策略判断行情是否可参与交易信号检测；如需只观察不拦截，可显式改为 `True`。

典型返回：

```json
{
  "status": "success",
  "data": {
    "enabled": true,
    "observe_only": false,
    "overall": {
      "score": 92,
      "status": "healthy"
    },
    "sources": {
      "xtdata": {
        "score": 95,
        "status": "healthy",
        "success_count": 18,
        "failure_count": 0
      }
    },
    "trading": {
      "min_score": 70,
      "allow_mootdx": false
    }
  },
  "timestamp": "2026-06-27 10:30:00"
}
```

!!! note "网关能力边界"
    该接口目前由 Flask 直连模式提供；xtquant_manager 网关的 `/api/v1/health` 是网关账号连接健康，不等同于 miniQMT 的行情源健康评分。

!!! info "Tushare `no_data` 语义  [2026-09-29]"
    历史查询成功但区间内没有交易日时，Tushare 健康事件返回 `ok=true`、`reason=no_data`，不会累计连续失败或触发冷却。日期范围过滤后没有新增记录使用 `reason=no_new_data`。只有返回 `None`、超时或异常才计入 `failure_count`；前端或监控程序不应把 `no_data` 解释为数据源离线。

---

## 持仓与交易记录

| 方法 | 路径 | 说明 | 🌐 网关 |
|------|------|------|--------|
| GET | `/api/positions` | 当前持仓列表（含 SQLite 持久化字段：名称/建仓日/止损价；`change_percentage` 由 tick 计算，裸代码自动补交易所后缀） | ✅ 完整 |
| GET | `/api/positions-all` | 全部持仓详情 | ✅ 完整 |
| GET | `/api/trade-records` | 交易记录（优先读 SQLite `trade_records`，`trade_time` 统一格式化为 `YYYY-MM-DD HH:MM:SS`） | ✅ 完整 |
| GET | `/api/orders` | **当日委托列表**（含在途未成交），在途委托排在前面 | ✅ 完整 |
| POST | `/api/initialize_positions` | 初始化持仓数据 🔑 需 Token | ❌ |
| POST | `/api/holdings/init` | 初始化持股配置（返回体含 `status`，供 web1.0 按钮判定成败） 🔑 需 Token | ❌ |

### 持仓受限字段（v3.9.6）

`/api/positions` 与 `/api/positions-all` 中的持仓记录可包含以下字段，Web 两端用于“可用”列的悬浮提示：

| 字段 | 含义 |
|------|------|
| `available` / `volume` | 当前可卖数量 / 总持仓数量；缺失可卖量时不能退回总量 |
| `unavailable_volume` | 不可卖数量，缺乏有效数量时不伪造为 0 |
| `restriction_reason` | `t1` / `sell_order` / `mixed` / `unknown` |
| `restriction_detail` | 依据成交、委托和持仓快照形成的中文说明 |
| `position_asof` | 持仓核对时间，用于识别过期证据 |

网关兼容路径使用同一口径；未知或过期状态不表示已解冻。

### `/api/orders` 字段

交易记录只含**已成交**，看不到挂单中的止盈卖单——这是监控视图的盲区，`/api/orders` 用于补齐。

| 字段 | 说明 |
|------|------|
| `order_id` | 委托编号 |
| `stock_code` / `stock_name` | 代码 / 名称（名称来自账号 SQLite） |
| `trade_type` | `BUY`(23) / `SELL`(24) |
| `price` / `volume` / `traded_volume` | 委托价 / 委托量 / 已成交量 |
| `status` / `status_desc` | 状态码 / 中文描述（优先用 QMT 的 `status_msg`） |
| `is_pending` | 是否在途，状态码 ∈ {48,49,50,51,52,55} |
| `order_time` | 报单时间，`YYYY-MM-DD HH:MM:SS`（QMT 原值是 Unix 时间戳） |

状态码与 `position_manager._has_pending_orders_fallback` 的活跃集合保持一致，
常量收口在 `order_utils.py`，两条链路（Flask / 网关）共用。

!!! note "交易记录口径"
    实盘网格在 `GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True` 时，`/api/trade-records` 只返回真实成交确认后的 `trade_records`。已报未成交的网格委托只体现在 `grid_orders` / 网格会话状态中，不会以 `ORDER_xxx` 形式伪装成成交。
    动态止盈止损的实盘卖出委托也遵循成交确认语义：首次止盈半仓提交成功或仅收到第一笔部分成交，均不等于 `profit_triggered=True`；v3.9.6 持续跟踪到目标成交量确认完成，撤单后仅重挂未成交余量。网格按委托聚合展示，保留真实分笔的时间、成交编号与分笔数用于审计。

!!! note "对账补记流水  [v3.9.5]"
    推送通道失效或外部成交（QMT 客户端策略单/手工单）时，流水由**持仓同步处的当日成交对账**
    补齐（`TradingExecutor.reconcile_missing_trades()`，挂在持仓同步之后：本进程首次成功同步、
    以及每次发现新增持仓各触发一次）。监控/前端侧需要知道三点：

    - 记录可能在**重启后**才出现，且 `trade_time` 早于当前时刻——这是补记，不是延迟成交
    - 本机没下过的单标 `strategy='external'`（显示"外部"）；同一委托的多笔分笔各占一行
    - 补记行 `time_source` 为 `exchange`（成交查询带回了真实成交时间）或 `reconcile_backfill`
      （拿不到成交时间，`trade_time` 即补记时刻）。该字段只在导出交割单的诊断列中可见，
      `/api/trade-records` 不下发

    对账幂等：同一笔成交不会被写第二遍（详见[交割单数据管道](settlement-export.md#push-outage-reconcile)）。

!!! tip "交易时间格式兼容"
    `trade_records.trade_time` 在库中可能混合多种写法：QMT 成交回报带微秒（`2026-07-31 09:31:50.610000`）、系统内部写入为秒级（`2026-07-31 09:30:44`），历史数据还可能是 ISO（含 `T` 分隔符）。`_format_trade_time_for_response()` 依次尝试带微秒/不带微秒的严格解析，再回退到 `pd.to_datetime`，全部失败才原样返回并记 WARNING。此前统一走 `pd.to_datetime` 在混合格式下会整列抛错，导致交易记录接口 500。

---

## 交易操作

| 方法 | 路径 | 说明 | 🌐 网关 |
|------|------|------|--------|
| POST | `/api/actions/execute_buy` | 执行买入（自动买入模块也复用该路径）🔑 需 Token | ❌ |
| POST | `/api/holdings/update` | 更新持仓参数（止盈标记/最高价/止损价）🔑 需 Token | ❌ |
| POST | `/api/holdings/stop_profit` | 设置个股「动态止盈止损」开关（`stock_code`、`enabled`）🔑 需 Token | ❌ |

**买入参数**：

```json
{
  "strategy": "custom_stock",
  "quantity": 1,
  "stocks": ["000001.SZ"]
}
```

!!! note "自动买入模块调用边界  [v3.9.4]"
    自动买入实盘模式复用该端点，并在请求前通过 `/api/positions` 做持仓防重；持仓查询非 200、非 JSON 或返回 `status=error` 时本轮 fail-safe，不调用买入端点。自动买入自身的 `--simulate` 模式只运行筛选与记录链路，不会向该端点发送请求；它不同于主程序 `ENABLE_SIMULATION_MODE=True` 时由该端点执行模拟成交。

!!! note "网关下单走 v1 接口"
    xtquant_manager 网关模式下，下单使用 `/api/v1/accounts/{account_id}/orders`。web2.0 在网关模式下通过 v1 接口下单，而不是调用 Flask 的 `/api/actions/execute_buy`。

!!! info "模拟模式下的行为差异"
    `ENABLE_SIMULATION_MODE=True` 时该端点的链路为
    `execute_buy` → `manual_buy` → `buy_stock` → `simulate_buy_position`，与实盘相比有三点差异：

    - **股票代码后缀**：模拟模式经 `Methods.add_xt_suffix` **补全**后缀（`000001` → `000001.SZ`）；实盘模式则**去除**后缀。
    - **交易时间**：模拟模式强制放行，不受 `config.is_trade_time()` 限制，非交易时段也能下单。
    - **策略标识**：写入 `trade_records_sim.strategy` 的值为 `M_simu`（实盘为 `M_real`）；Web 界面分别显示为「模买」「手买」（存储值不变，仅显示层映射）。

    `ENABLE_ALLOW_BUY=False` 时，模拟与实盘同样在 `manual_buy` 层被拦截，返回 `success_count=0`。
    模拟成交写入内存持仓与独立的 SQLite `trade_records_sim`，不落 SQLite `positions` 表。

---

## 网格交易 API

| 方法 | 路径 | 说明 | 🌐 网关 |
|------|------|------|--------|
| POST | `/api/grid/start` | 启动网格会话 🔑 需 Token | ❌ |
| POST | `/api/grid/stop/<session_id>` | 停止指定网格 🔑 需 Token | ❌ |
| POST | `/api/grid/stop` | 停止所有网格 🔑 需 Token | ❌ |
| POST | `/api/grid/session/<session_id>/enabled` | 设置单个网格会话自动/暂停 🔑 需 Token | ❌ |
| GET | `/api/grid/session/<stock_code>` | 按股票查网格状态 | ❌ |
| GET | `/api/grid/session/<session_id>` | 按会话 ID 查详情 | ❌ |
| GET | `/api/grid/sessions` | 所有网格会话 | ✅ 只读 |
| GET | `/api/grid/trades/<session_id>` | 网格交易记录 | ❌ |
| GET | `/api/grid/ledger/<session_id>` | 网格真实账本详情（批次、LIFO 配对、盈亏汇总） | ✅ 只读 |
| GET | `/api/grid/status/<stock_code>` | 网格快速状态 | ❌ |
| GET | `/api/grid/checkbox-states` | 所有股票网格勾选状态 | ❌ |
| GET | `/api/grid/checkbox-state/<stock_code>` | 单只股票网格勾选状态 | ❌ |
| GET | `/api/grid/config` | 网格配置 | ❌ |
| GET | `/api/grid/templates` | 网格模板列表 | ❌ |
| GET | `/api/grid/template/<name>` | 获取指定网格模板详情 | ❌ |
| POST | `/api/grid/template/save` | 保存网格模板 🔑 需 Token | ❌ |
| DELETE | `/api/grid/template/<name>` | 删除模板 | ❌ |
| POST | `/api/grid/template/use` | 使用模板 🔑 需 Token | ❌ |
| GET | `/api/grid/template/default` | 获取默认模板 | ❌ |
| PUT | `/api/grid/template/<name>/default` | 设为默认模板 | ❌ |
| GET | `/api/grid/risk-templates` | 风险分级模板 | ❌ |

!!! tip "网格交易份额模式（trade_mode）"
    `POST /api/grid/start` 请求体（含嵌套 `config`）支持 `trade_mode`（`amount` 固定金额 / `shares` 固定股数，默认 `amount`）与 `fixed_volume`（固定股数模式每次买卖股数，须为 100 整数倍；0=按持仓兜底）。`/api/grid/session/<stock_code>` 等查询接口的 `config` 会回显这两个字段供前端回填。详见[网格交易 · 交易份额模式](grid-trading.md)。

!!! info "网格写操作仅 Flask 直连"
    网格策略由 `grid_trading_manager` 主线程驱动，网关进程独立运行不持有策略状态。因此启动/停止/模板/账本详情等网格写操作和深度查询仍需 Flask 模式；网关模式仅兼容 `/api/grid/sessions`，从账号 SQLite 只读返回会话列表。
!!! note "实盘委托与成交分离"
    实盘网格下单成功后先登记到 `grid_orders`，等成交回报确认后才写入 `grid_trades`、真实盈亏账本和普通 `trade_records`。前端读取交易记录或账本时，应把未成交委托视为待确认状态，而不是成交。
!!! note "自动/暂停接口"
    `POST /api/grid/session/<session_id>/enabled` 请求体为 `{"enabled": true|false}`。关闭后保留会话和账本，只暂停后续新网格单；停止会话仍使用 `/api/grid/stop...`。

!!! tip "统一盈亏快照"
    Flask 直连下，`/api/grid/session/<...>`、`/api/grid/sessions`、`/api/grid/status/<stock_code>` 返回的会话数据含 `pnl_snapshot` 字段：基于 LIFO 账本计算的真实盈亏（`realized_pnl` / `unrealized_pnl` / `total_pnl` / `profit_ratio`），账本不可用时自动降级并以 `is_degraded` 标记。`/api/grid/ledger/<session_id>` 进一步返回 `summary`、`lots`、`matches`、`trades` 和分页信息，供前端账本详情面板展示。详见[网格交易 · 真实盈亏账本](grid-trading.md)。

### `/api/grid/session/<stock_code>` tooltip 字段

web1.0 网格悬停卡片直接使用该接口。为避免前端重复换算，所有比例字段均返回**小数格式**，例如 `-0.00925` 表示 `-0.925%`。

| 字段 | 说明 |
|------|------|
| `stats.profit_ratio` | 与 `stats.pnl_snapshot.profit_ratio` 同源，真实网格盈亏率，小数格式 |
| `stats.grid_profit` | 与 `stats.pnl_snapshot.total_pnl` 同源，真实网格盈亏金额 |
| `stats.current_investment` / `stats.max_investment` | 资金使用分子/分母 |
| `stats.deviation_ratio` | 网格中心漂移偏离，等同 `GridSession.get_deviation_ratio()`，即 `abs(current_center_price - center_price) / center_price` |
| `stats.center_deviation_ratio` | 带方向的中心漂移偏离，`(current_center_price - center_price) / center_price`；前端用它显示“上移/下移” |
| `stats.market_deviation_ratio` | 当前市价相对当前网格中心价的偏离，`abs(current_price - current_center_price) / current_center_price` |
| `stats.effective_deviation_ratio` | 后端风控退出使用的有效偏离，`max(deviation_ratio, market_deviation_ratio)` |
| `stats.market_price` | 本次快照使用的标记价，优先当前持仓市价，取不到时退回当前网格中心价 |

---

## 配置管理

| 方法 | 路径 | 说明 | 🌐 网关 |
|------|------|------|--------|
| GET | `/api/config` | 获取系统配置 | 🔒 只读（返回默认值） |
| POST | `/api/config/save` | 保存配置（需 Token） | ❌ |

### 配置字段要点

以下字段以 Flask 直连模式的 `/api/config` / `/api/config/save` 为准；网关兼容端点只读，写配置必须回到本机 web1.0 / Flask。

| 字段 | 对应配置 | 说明 |
|------|----------|------|
| `globalAllowBuySell` | `ENABLE_AUTO_TRADING` | 动态止盈止损自动执行开关 |
| `globalAllowGridTrading` | `ENABLE_GRID_TRADING` | 网格模块自动执行开关 |
| `pauseGridAfterTakeProfitFull` | `ENABLE_PAUSE_GRID_AFTER_TAKE_PROFIT_FULL` | 全仓止盈 `take_profit_full` 成交确认后是否暂停同股活跃网格会话，默认 `true` |

!!! note "字段语义  [v3.8.9]"
    `pauseGridAfterTakeProfitFull` 只控制全仓止盈成交后的网格联动暂停，不会停止或删除网格会话。保存为 `false` 后，`take_profit_full` 成交确认只完成动态止盈流水与持仓刷新，不再改动 `grid_trading_sessions.enabled`。

---

## 监控控制

| 方法 | 路径 | 说明 | 🌐 网关 |
|------|------|------|--------|
| POST | `/api/monitor/start` | 启动全局自动操作总开关（兼容旧 monitor 路径）🔑 需 Token | ❌ |
| POST | `/api/monitor/stop` | 停止全局自动操作总开关（兼容旧 monitor 路径）🔑 需 Token | ❌ |

!!! note "字段兼容"
    `/api/status`、SSE 和上述接口仍返回 `isMonitoring` 字段以兼容 web1.0/web2.0，当前语义是 `ENABLE_AUTO_OPERATION`。持仓监控线程是否运行由 `positionMonitorRunning` 表示，自动止盈由 `autoTradingEnabled` / `enableAutoTrading` 表示，自动网格由 `gridTradingEnabled` / `enableGridTrading` 表示。

---

## 股票池

| 方法 | 路径 | 说明 | 🌐 网关 |
|------|------|------|--------|
| GET | `/api/stock_pool/list` | 获取股票池列表 | ❌ |

---

## 实时推送

| 方法 | 路径 | 说明 | 🌐 网关 |
|------|------|------|--------|
| GET | `/api/sse` | Server-Sent Events 实时更新 | ❌（用 3s/10s 轮询） |

---

## 数据管理（需 Token）

| 方法 | 路径 | 说明 | 🌐 网关 |
|------|------|------|--------|
| POST | `/api/logs/clear` | 清空日志 | ❌ |
| POST | `/api/data/clear_buysell` | 清空买入/卖出日志（`DELETE FROM trade_records`，**不影响持仓数据**） | ❌ |
| POST | `/api/data/import` | 导入数据 | ❌ |

旧备份、运行事件及测试产物的有限保留通过后台维护和 `scripts/cleanup_runtime_artifacts.py` 管理，以上网页接口不提供同等能力。手动操作先预览，详见[无人值守运行](unattended.md#runtime-retention)。

---

## XtQuantManager 专属 API（v1）

网关模式额外提供 [`/api/v1/*`](../xqm/api/index.md) 端点（多账号管理、健康检查、动态止盈、Prometheus metrics 等）。详见 [XtQuantManager API 手册](../xqm/api/index.md)。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/health` | 网关全局健康（账号总数 / 在线数）；免 Token 可达，无 Token 时不返回 `accounts` 明细 |
| GET | `/api/v1/accounts` | 账号列表（需 Token） |
| GET | `/api/v1/stop-profit/status` | 动态止盈运行状态（需 Token） |
| POST | `/api/v1/stop-profit/config` | 更新止盈配置（需 Token） |
| POST | `/api/v1/stop-profit/toggle` | 启用/禁用动态止盈（需 Token） |
