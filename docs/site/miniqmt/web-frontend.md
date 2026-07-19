# Web 前端（web1.0 / web2.0）

miniQMT 提供**两套**Web 界面，对应两种后端连接架构。本章节解释何时用哪一套、能力边界、连接设置面板用法、菜单启动方式，以及远程部署。

---

## 双模式架构对比

| 维度 | web1.0（Flask 直连） | web2.0（xtquant_manager 网关） |
|------|---------------------|--------------------------------|
| 前端栈 | Flask 模板渲染 (`web1.0/`) | Vue3 + Vite + TypeScript + Tailwind + Pinia (`web2.0/`) |
| 后端 | 每账号独立 Flask 进程 (`web_server.py`) | 单一 xtquant_manager 网关进程 |
| 默认端口 | `:5000`、`:5001`、…（每账号一个） | `:8888`（多账号共用） |
| 绑定地址 | `127.0.0.1` — **只绑本机**（避免误暴露写操作 API） | `0.0.0.0` — 全部网卡（含 LAN/WAN） |
| 适用场景 | 完整功能：配置管理 / 自动操作总开关 / 模拟买入 / 初始化持仓 / 网格操作 | 多账号只读监控 + 下单；远程访问 |
| 实时推送 | ✅ SSE | ❌ 仅 3s/10s 轮询 |
| PWA 离线 | ❌ | ✅ 可安装到桌面 |

!!! tip "为什么 web1.0 只绑本机"
    web1.0 暴露完整写操作 API（配置保存、模拟买入、数据清除等），如果绑定到公网会有安全风险。
    远程访问/局域网共享用 web2.0 + xtquant_manager 网关，写操作仅限本机使用 web1.0。

---

## 网关模式能力边界

web2.0 通过 xtquant_manager 网关连接时，**写操作受限**。设计上网关只承担「多账号只读监控 + 下单」职责，配置/初始化等敏感写操作回到 Flask 直连模式完成。

| 功能 | 网关模式 | 说明 |
|------|---------|------|
| 持仓查看（名称/盈亏/止损/建仓） | ✅ 完整 | QMT 实时数据 + SQLite 持久化元数据合并 |
| 多账号切换 | ✅ 完整 | `X-Account-Id` 请求头隔离 |
| 账户资产/连接状态 | ✅ 完整 | 可用余额 / 持仓市值 / 总资产 |
| 交易记录 | ✅ 完整 | 优先读 SQLite `trade_records`（与 web1.0 同源） |
| 下单（买/卖） | ✅ 完整 | 通过 v1 接口 |
| 动态止盈状态查询 | ✅ 只读 | `/api/v1/stop-profit/status` |
| 网格会话列表 | ✅ 只读 | `/api/grid/sessions` 从账号 SQLite 读取，盈亏口径为兼容降级快照 |
| 网格账本详情 | 🔒 不可用 | 需 Flask 直连模式查看 `/api/grid/ledger/<session_id>` |
| 参数设置面板 | 🔒 只读 | 修改需 Flask 直连模式 |
| 自动操作总开关 | 🔒 不可用 | 需 Flask 直连模式 |
| 模拟买入 / 清空 / 导入 / 初始化 | 🔒 不可用 | 需 Flask 直连模式 |
| 动态止盈控制（开关） | 🔒 不可用 | 需 Flask 直连模式 |
| SSE 实时推送 | 🔒 不可用 | 依赖 3s/10s 轮询 |

实盘网格的委托态和成交态在前端保持分离：已报未成交订单可通过网格会话待确认状态和后台 `grid_orders` 追踪，只有收到成交回报后才进入交易记录、网格成交明细和账本，避免把 `ORDER_xxx` 展示为真实成交。

动态止盈止损的实盘卖出委托同样以成交回报为准：首次止盈半仓委托提交后，Web 不会立即把 `profit_triggered` 视为已完成；只有成交确认后，持仓状态才进入“已触发首次止盈/动态止盈阶段”。如果同一股票已有在途卖单，本轮新止盈/止损信号会被后端阻断，前端看到的是状态保持不变而非重复下单。

前端通过 [`isGatewayMode()`](https://github.com/weihong-su/miniQMT/blob/main/web2.0/src/api/accounts.ts) 检测当前模式，自动隐藏写操作按钮并在顶栏显示「网关模式 · 只读监控+下单」徽章（配置面板另有「🔒 网关模式 · 参数为只读展示，修改请使用 Flask 直连模式」提示）。

---

## 页面标题与发布版本

web1.0 和 web2.0 的页面标题都使用 `%MINIQMT_RELEASE_VERSION%` 占位符，真实发布版本统一来自项目根目录的 `release_version.json`。

- web1.0：`web_server.py` 在返回 `web1.0/index.html` 时注入版本号，同时给 `script.js` 添加基于 mtime 的缓存破坏参数
- web2.0：`web2.0/vite.config.ts` 在 Vite 构建阶段注入版本号，更新 `release_version.json` 后需要重新执行 `npm run build`

`web2.0/package.json` 中的 `version` 仅表示前端包自身元数据，不作为 miniQMT 发布版本来源。

---

## 顶部控制条

Flask 直连模式下，顶部控制条包含几类容易混淆的开关：

| 控件 | 后端字段/配置 | 作用 |
|------|---------------|------|
| 开始/停止自动操作按钮 | `ENABLE_AUTO_OPERATION`（API 兼容字段 `isMonitoring` / `globalAutoOperation`） | 全局自动操作总开关，只运行时生效不持久化；关闭时动态止盈止损和网格交易都不再产生新单 |
| 允许自动止盈 | `ENABLE_AUTO_TRADING`（保存配置字段 `globalAllowBuySell`） | 动态止盈止损自动执行开关，持久化 |
| 允许自动网格 | `ENABLE_GRID_TRADING`（保存配置字段 `globalAllowGridTrading`） | 网格模块自动执行开关，持久化 |
| 动态止盈 | `ENABLE_DYNAMIC_STOP_PROFIT` | 控制动态止盈止损模块是否检测信号 |
| 买 / 卖 | `ENABLE_ALLOW_BUY` / `ENABLE_ALLOW_SELL` | 手动和自动交易的方向权限 |
| 网格自动/暂停 | `grid_trading_sessions.enabled` | 单只股票网格会话开关，暂停后保留会话但不发新网格单 |

!!! note "为什么 API 仍叫 isMonitoring"
    早期前端使用 `isMonitoring` 表示顶部开关状态。为兼容旧接口，字段名保留不变，但当前语义已经是全局自动操作总开关；持仓监控线程状态请看 `positionMonitorRunning`。

Web1.0 参数区中，`API Token` 与“模拟交易模式”“允许自动止盈”“允许自动网格”位于同一行；Web2.0 顶部控制条同样提供“模拟交易”“允许自动止盈”“允许自动网格”三个分开关。全局自动操作不再单独显示开关，由“开始/停止自动操作”按钮统一控制。

### 卖出委托状态

动态止盈止损卖出委托由后端 `pending_orders` 跟踪。委托超时后，如果启用了自动重挂，系统会先撤销旧委托，再以新价格重新提交；`best` 对手价模式下买三价无效时会降级到买一价、最新价、收盘价或原信号价。

这意味着 Web 中“首次止盈已触发”代表**成交确认后的状态**，不是“委托已提交”。生产排查时应同时查看交易记录、后台日志和 QMT 当日委托，避免把待成交卖单误判为已经落账。

### 网格悬停卡片口径

web1.0 持仓列表中，鼠标悬停在已启动网格交易的个股复选框上会显示网格状态卡片。卡片所有比例字段统一使用后端小数比例，由前端格式化为百分比，避免重复乘以 100。

| 字段 | 数据来源 | 口径 |
|------|----------|------|
| 网格盈亏 | `stats.pnl_snapshot.profit_ratio` | FIFO 账本真实盈亏率；账本不可用时使用 `get_pnl_snapshot()` 的降级口径 |
| 已实现/未实现 | `stats.pnl_snapshot.realized_pnl` / `unrealized_pnl` | 已配对卖出收益 + 未平网格库存浮动盈亏 |
| 交易次数 | `stats.trade_count` / `buy_count` / `sell_count` | 已成交确认并落账后的网格交易次数 |
| 资金使用 | `stats.current_investment` / `config.max_investment` | 当前未平网格投入占最大投入额度比例 |
| 中心价偏离 | `stats.deviation_ratio` + `stats.center_deviation_ratio` | 当前网格中心价相对初始中心价的漂移幅度；显示为正数并标注“上移/下移” |

!!! note "中心价偏离不是当前市价偏离"
    网格风控内部同时计算两种偏离：`drift_deviation = abs(current_center_price - center_price) / center_price` 和 `market_deviation = abs(current_price - current_center_price) / current_center_price`，退出判断取二者最大值。悬停卡片中的“中心价偏离”只展示前者，即网格中心价漂移；当前市价偏离作为后端字段保留，不混入该展示项。

---

## 连接设置面板

顶部 ⚙ 齿轮按钮打开「连接设置」面板：

```
┌──────────────────────────────────────────────┐
│ 当前: HTTPS (安全) — 后端也必须 HTTPS         │
├──────────────────────────────────────────────┤
│ 后端模式：  [ 网关模式 ]  [ 直连模式 ]        │
├──────────────────────────────────────────────┤
│ 网关地址：  http://127.0.0.1:8888             │
│ API Token： •••••••••••• (留空=不验证)        │
├──────────────────────────────────────────────┤
│ 连通性测试：[ 测试连接 ]                      │
│   ✓ 连接成功 — 2 个账号, 2 个在线             │
└──────────────────────────────────────────────┘
```

### 字段说明

| 字段 | 网关模式 | 直连模式 |
|------|---------|---------|
| 地址 | 网关地址（所有账户共用），如 `http://127.0.0.1:8888` | Flask 地址（每账户独立），在账户下拉菜单的 ✎ 中编辑 |
| Token | xtquant_manager 的 `api_token` | Flask 的 `QMT_API_TOKEN` 环境变量值 |
| 测试连接 | `GET /api/v1/health` — 显示账号总数与在线数 | `GET /api/status` — 显示账户 ID |

### 自动化行为

- **保存后自动发现账号**：保存连接配置后调用 `discoverAccounts()`，从网关同步真实账号 ID 到下拉列表（无需手动新增）
- **HTTPS Mixed Content 警告**：HTTPS 页面访问 HTTP 后端会被浏览器阻止，面板自动给出警告
- **无 Token 远程警告**：远程连接而不设 Token 时提示安全风险
- **8s 超时 + 非 JSON 检测**：测试连接遇到反代/Nginx 错误页时给出明确诊断

---

## 启动菜单（miniqmt.bat）

```
[7] 启动所有账号 (实盘，启动时选择 web1.0/web2.0)
[8] 启动所有账号 (模拟，启动时选择 web1.0/web2.0)
[9] 启动指定账号 (选择实盘/模拟 + web1.0/web2.0)
       web1.0 = Flask :5000 起, 仅本机访问 (配置/监控用)
       web2.0 = xtquant_manager :8888, 全网卡 (只读查询/对外)
[d] 启动 XtQuantManager 网关
[e] 停止 XtQuantManager 网关
[f] XtQuantManager 状态
[g] 打开 web2.0 UI（浏览器）
[h] 重启 XtQuantManager 网关
[i] 查看 XtQuantManager 日志
[j] 启动自动买入服务
[k] 停止自动买入服务
[l] 查看自动买入状态
[m] 查看自动买入日志
[n] Tushare Pro 数据源配置
[o] 大QMT IPC Trader 配置
[p] XtTrader 通道总控（miniQMT 直连 / IPC-Trader / RPC-Trader）
```

### Web 模式偏好记忆

启动菜单 [7]/[8]/[9] 会读取 `data/.web_mode` 中上次的选择：

- `1` → web1.0（Flask）— 系统在每账号端口上启动完整 Flask 服务
- `2` → web2.0（xtquant_manager）— 主程序设 `QMT_NO_FLASK=1` 跳过 Flask，由 xtquant_manager 统一提供网关与静态文件托管

每次选择都会持久化，下次启动直接套用偏好。

### 绑定地址 vs 客户端地址分离

xtquant_manager 在 [_launcher.py](https://github.com/weihong-su/miniQMT/blob/main/scripts/_launcher.py) 中明确分离两个概念，避免 `0.0.0.0` 被错误用作客户端目标：

| 常量 | 值 | 用途 |
|------|----|----|
| `XQM_DEFAULT_HOST` | `0.0.0.0` | **绑定地址** — 监听全部网卡，对外可达 |
| `XQM_CLIENT_HOST` | `127.0.0.1` | **客户端地址** — 本机健康检查、浏览器打开 |

启动后菜单会同时显示「本机 URL」和「局域网 URL」：

```
✓ xtquant_manager 已启动
  Web UI:          http://127.0.0.1:8888  (本机)  |  http://192.168.1.10:8888  (局域网)
  API 文档:        http://127.0.0.1:8888/docs
```

---

## Vercel 远程部署（web2.0）

如需将 web2.0 部署到 Vercel/Netlify 等公网托管，通过 Cloudflare Tunnel 暴露 Windows 上的 xtquant_manager：

```
Vercel (静态 UI) ──HTTPS──► Cloudflare Tunnel ──► Windows xtquant_manager :8888 ──► QMT
```

完整步骤（含 Tunnel 配置、Token 安全清单、CORS 排错）参见 [web2.0/VERCEL_DEPLOY.md](https://github.com/weihong-su/miniQMT/blob/main/web2.0/VERCEL_DEPLOY.md)。

### 关键安全要点

- ⚠️ **必须设置 `api_token`**：远程暴露唯一的安全防线，用强随机字符串（≥32 位）
- ⚠️ **必须用 HTTPS 隧道**：Cloudflare Tunnel 自动 HTTPS；直接暴露 `:8888` 到公网会触发浏览器 Mixed Content 拦截
- 远程用户始终通过网关模式接入，写操作（配置/监控/初始化）只能在本机用 web1.0 完成

---

## 开发与构建

```bash
cd web2.0
npm install               # 仅首次
npm run dev               # 开发模式 (http://localhost:5173，热更新)
npm run build             # 生产构建 → dist/
```

构建产物 `web2.0/dist/` 会被 xtquant_manager 自动托管（静态文件 + SPA fallback），也可直接部署到 Vercel。

---

## 相关文档

- [Web API](web-api.md) — REST 端点完整列表（标注网关模式可用性）
- [自动买入](autobuy.md) — 自动买入独立进程、候选池筛选和调度配置
- [架构说明](architecture.md) — 双层存储、信号检测与执行分离
- [XtQuantManager 概述](../xqm/index.md) — 网关详细文档
- [web2.0/VERCEL_DEPLOY.md](https://github.com/weihong-su/miniQMT/blob/main/web2.0/VERCEL_DEPLOY.md) — Vercel 远程部署完整指南
