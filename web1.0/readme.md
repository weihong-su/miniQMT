# web1.0 说明

web1.0 是 miniQMT 当前的 Flask 直连监控界面，由 `web_server.py` 启动并托管 `web1.0/index.html`、`script.js`、`style.css` 等静态资源。默认监听 `127.0.0.1:5000`；多账号启动时端口按账号顺序递增为 `5000`、`5001`、……

## 适用场景

- 本机完整操作：配置保存、自动操作总开关、模拟买入、初始化持仓、网格启停与模板管理。
- 单账号或每账号独立 Flask 进程监控。
- 需要 SSE 实时推送的本机监控。

远程访问和多账号统一监控优先使用 web2.0 + XtQuantManager 网关；网关模式只提供部分 Flask 兼容读端点和 `/api/v1/*` 多账号 API，不等同于完整 web1.0。

## 关键 API

当前实际端点以 `web_server.py` 为准，完整说明见 `docs/site/miniqmt/web-api.md`。

| 功能 | 端点 |
|------|------|
| 系统状态 | `GET /api/status` |
| QMT 连接状态 | `GET /api/connection/status` |
| 行情源健康 | `GET /api/market/health` |
| 持仓列表 | `GET /api/positions`、`GET /api/positions-all` |
| 交易记录 | `GET /api/trade-records` |
| 配置读取/保存 | `GET /api/config`、`POST /api/config/save` |
| 自动操作总开关 | `POST /api/monitor/start`、`POST /api/monitor/stop` |
| 买入 | `POST /api/actions/execute_buy` |
| 持仓参数更新 | `POST /api/holdings/update` |
| 初始化持仓 | `POST /api/initialize_positions`、`POST /api/holdings/init` |
| SSE 推送 | `GET /api/sse` |
| 网格会话 | `POST /api/grid/start`、`POST /api/grid/stop/<session_id>`、`GET /api/grid/sessions` |
| 网格账本 | `GET /api/grid/ledger/<session_id>` |
| 网格模板 | `GET /api/grid/templates`、`POST /api/grid/template/save`、`POST /api/grid/template/use` |

旧草稿中提到的 `/api/account_info`、`/api/holdings`、`/api/holdings/initialize`、`/api/trade/buy`、`/api/logs/orders` 不是当前 Flask 实现端点。

## 认证

写操作通过 `QMT_API_TOKEN` 对应的 `WEB_API_TOKEN` 控制。未设置 Token 时 Flask 直连默认放行；设置后需在请求头 `X-API-Token` 或 URL 参数 `?token=` 中携带令牌。不要把 Token 写进仓库文件。

## 前端开关语义

- “开始/停止自动操作”对应 `ENABLE_AUTO_OPERATION`，只在当前进程运行时生效。
- “允许自动止盈”对应 `ENABLE_AUTO_TRADING`，控制动态止盈止损自动执行。
- “允许自动网格”对应 `ENABLE_GRID_TRADING`，控制网格模块自动执行。
- 单只网格会话的“自动/暂停”对应 `grid_trading_sessions.enabled`，暂停后保留会话但不再发新网格单。
