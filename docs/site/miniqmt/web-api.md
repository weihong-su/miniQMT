# Web API

miniQMT 提供 RESTful API。Flask 直连模式暴露完整 web1.0 API；xtquant_manager 网关模式暴露一部分 Flask 兼容端点，并额外提供 `/api/v1/*` 多账号 API。

| 后端 | 默认地址 | 适用 |
|------|---------|-----|
| Flask 直连 (`web_server.py`) | `http://127.0.0.1:5000`（每账号一个端口） | web1.0、单机完整功能 |
| xtquant_manager 网关 | `http://127.0.0.1:8888` | web2.0、多账号、远程访问 |

**认证**：需要 Token 的接口通过 `QMT_API_TOKEN` 环境变量（Flask）或 `api_token` 配置（网关）设置。Flask 直连模式下，`require_token` 装饰器在**未设置 `QMT_API_TOKEN` 时放行**（适合纯内网部署）；设置后需在请求头 `X-API-Token` 或 URL 参数 `?token=` 中携带令牌。除只读查询外，多数写操作端点（监控开关、初始化持仓、买入、持仓/网格参数更新、网格启停与模板保存、配置保存、数据管理等）均带 `@require_token` 保护，本文档在这些行的说明中标注 **🔑 需 Token**。

**多账号路由（网关）**：通过 `X-Account-Id` 请求头切换目标账号；未指定时回退到第一个已注册账号。

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
| GET | `/api/debug/status` | 详细调试状态 | ❌ |
| GET | `/api/accounts` | 列出已注册账号（无 Token，供前端账号发现）——**仅网关提供**，Flask 直连无此端点 | ✅ 完整 |

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

---

## 持仓与交易记录

| 方法 | 路径 | 说明 | 🌐 网关 |
|------|------|------|--------|
| GET | `/api/positions` | 当前持仓列表（含 SQLite 持久化字段：名称/建仓日/止损价） | ✅ 完整 |
| GET | `/api/positions-all` | 全部持仓详情 | ✅ 完整 |
| GET | `/api/trade-records` | 交易记录（优先读 SQLite `trade_records`） | ✅ 完整 |
| POST | `/api/initialize_positions` | 初始化持仓数据 🔑 需 Token | ❌ |
| POST | `/api/holdings/init` | 初始化持股配置 🔑 需 Token | ❌ |

!!! note "交易记录口径"
    实盘网格在 `GRID_CONFIRM_LIVE_ORDER_BY_DEAL = True` 时，`/api/trade-records` 只返回真实成交确认后的 `trade_records`。已报未成交的网格委托只体现在 `grid_orders` / 网格会话状态中，不会以 `ORDER_xxx` 形式伪装成成交。
    动态止盈止损的实盘卖出委托也遵循成交确认语义：首次止盈半仓提交成功不等于 `profit_triggered=True`，成交回报到达后才更新持仓状态与持久化字段。

---

## 交易操作

| 方法 | 路径 | 说明 | 🌐 网关 |
|------|------|------|--------|
| POST | `/api/actions/execute_buy` | 执行买入（自动买入模块也复用该路径）🔑 需 Token | ❌ |
| POST | `/api/holdings/update` | 更新持仓参数（止盈标记/最高价/止损价）🔑 需 Token | ❌ |

**买入参数**：

```json
{
  "strategy": "custom_stock",
  "quantity": 1,
  "stocks": ["000001.SZ"]
}
```

!!! note "网关下单走 v1 接口"
    xtquant_manager 网关模式下，下单使用 `/api/v1/accounts/{account_id}/orders`。web2.0 在网关模式下通过 v1 接口下单，而不是调用 Flask 的 `/api/actions/execute_buy`。

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
| GET | `/api/grid/ledger/<session_id>` | 网格真实账本详情（批次、FIFO 配对、盈亏汇总） | ❌ |
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

!!! info "网格写操作仅 Flask 直连"
    网格策略由 `grid_trading_manager` 主线程驱动，网关进程独立运行不持有策略状态。因此启动/停止/模板/账本详情等网格写操作和深度查询仍需 Flask 模式；网关模式仅兼容 `/api/grid/sessions`，从账号 SQLite 只读返回会话列表。
!!! note "实盘委托与成交分离"
    实盘网格下单成功后先登记到 `grid_orders`，等成交回报确认后才写入 `grid_trades`、真实盈亏账本和普通 `trade_records`。前端读取交易记录或账本时，应把未成交委托视为待确认状态，而不是成交。
!!! note "自动/暂停接口"
    `POST /api/grid/session/<session_id>/enabled` 请求体为 `{"enabled": true|false}`。关闭后保留会话和账本，只暂停后续新网格单；停止会话仍使用 `/api/grid/stop...`。

!!! tip "统一盈亏快照"
    Flask 直连下，`/api/grid/session/<...>`、`/api/grid/sessions`、`/api/grid/status/<stock_code>` 返回的会话数据含 `pnl_snapshot` 字段：基于 FIFO 账本计算的真实盈亏（`realized_pnl` / `unrealized_pnl` / `total_pnl` / `profit_ratio`），账本不可用时自动降级并以 `is_degraded` 标记。`/api/grid/ledger/<session_id>` 进一步返回 `summary`、`lots`、`matches`、`trades` 和分页信息，供前端账本详情面板展示。详见[网格交易 · 真实盈亏账本](grid-trading.md)。

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
| POST | `/api/data/clear_buysell` | 清除买卖数据 | ❌ |
| POST | `/api/data/import` | 导入数据 | ❌ |

---

## XtQuantManager 专属 API（v1）

网关模式额外提供 [`/api/v1/*`](../xqm/api/index.md) 端点（多账号管理、健康检查、动态止盈、Prometheus metrics 等）。详见 [XtQuantManager API 手册](../xqm/api/index.md)。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/health` | 网关全局健康（账号总数 / 在线数） |
| GET | `/api/v1/accounts` | 账号列表（需 Token） |
| GET | `/api/v1/stop-profit/status` | 动态止盈运行状态 |
| GET | `/api/v1/stop-profit/config` | 止盈配置 |
| POST | `/api/v1/stop-profit/toggle` | 启用/禁用动态止盈 |
