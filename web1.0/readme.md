# web1.0 说明

当前发布版本：**v3.9.6（2026-10-31）**，统一读取根目录 `release_version.json`。

web1.0 是 miniQMT 当前的 Flask 直连监控界面，由 `web_server.py` 启动并托管 `web1.0/index.html`、`script.js`、`static/tailwind.min.css` 等静态资源。默认监听 `127.0.0.1:5000`；多账号启动时端口按账号顺序递增为 `5000`、`5001`、……

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

配置 `QMT_API_TOKEN` 后，所有 `/api/*`（包括 GET 查询与 SSE）均需认证，推荐使用 `X-API-Token` 请求头；不要在 URL 中传播令牌。未配置 Token 且 `WEB_PUBLIC_MODE=false` 时默认放行，仅适合本机访问；`WEB_PUBLIC_MODE=true` 时缺少 Token 也会拒绝请求。不要把 Token 写进仓库文件。

## 前端开关语义

- “开始/停止自动操作”对应 `ENABLE_AUTO_OPERATION`，只在当前进程运行时生效。
- “允许自动止盈”对应 `ENABLE_AUTO_TRADING`，控制动态止盈止损自动执行。
- “允许自动网格”对应 `ENABLE_GRID_TRADING`，控制网格模块自动执行。
- 单只网格会话的“自动/暂停”对应 `grid_trading_sessions.enabled`，暂停后保留会话但不再发新网格单。


## v3.9.6 持仓与委托说明

- “可用”显示当前可卖股数，悬浮数字可查看不可卖数量、受限原因与核对时间；缺乏证据时显示原因待确认，不能把所有受限股份当作 T+1。
- 部分成交后委托继续跟踪；撤单后只重挂未成交余量，首次止盈标记等待目标成交量确认完成。
- 对手价用于选择限价委托的报价；在途数量、撤废状态应与委托队列和后台日志一起核对。
- 网页清空日志不会清理旧备份或测试目录。运行数据维护使用默认预览的 `scripts/cleanup_runtime_artifacts.py`，详见[无人值守说明](../docs/site/miniqmt/unattended.md#runtime-retention)。

## 本地样式构建

页面使用随项目提供的 `static/tailwind.min.css`，无需访问 Tailwind CDN，也无需在运行机器上安装 Node.js。

修改 `index.html` 或 `script.js` 中的 Tailwind 类名后，在开发机器重新构建：

```bash
cd web1.0
npm ci
npm run build:css
```

构建扫描 HTML 和 JS，包含动态切换和弹窗使用的完整类名。新增样式请使用完整类名，避免拼接类名片段导致扫描遗漏。
发布时一并提供更新后的 `static/tailwind.min.css`。Flask 会按文件修改时间添加 CSS 版本参数，使浏览器在文件更新后重新加载。
