# Web 前端（web1.0 / web2.0）

miniQMT 提供**两套**Web 界面，职责明确分工：

- **web1.0** — 完整操作台（读 + 写），仅绑本机
- **web2.0** — **纯只读监控端**（monitor），可远程访问

!!! warning "web2.0 是只读监控端"
    自 2026-08-01 起，web2.0 **不再提供任何写操作**：下单、配置保存、网格启停、
    个股开关、初始化持仓等入口全部移除，前端 HTTP 适配层也只导出 `apiGet`
    （`web2.0/src/api/adapter.ts`），从源头杜绝误触发。
    所有写操作请使用 web1.0。该约束由单元测试 `src/api/readonly.test.ts` 持续守护。

---

## 双端职责对比

| 维度 | web1.0（操作台） | web2.0（只读监控端） |
|------|-----------------|---------------------|
| 前端栈 | Flask 模板渲染 (`web1.0/`) | Vue3 + Vite + TypeScript + Tailwind + Pinia (`web2.0/`) |
| 定位 | 读 + 写，完整功能 | **只读**，展示与告警 |
| 后端 | 每账号独立 Flask 进程 (`web_server.py`) | Flask 直连 **或** xtquant_manager 网关 |
| 默认端口 | `:5000`、`:5001`、… | 直连沿用 Flask 端口；网关 `:8888` |
| 绑定地址 | `127.0.0.1` — **只绑本机** | 网关模式 `0.0.0.0`，可远程 |
| 实时推送 | ✅ SSE | 直连 ✅ SSE；网关 ❌（轮询兜底，不影响数据更新） |
| PWA 离线 | ❌ | ✅ 可安装到桌面 |
| 多账号切换 | ❌（每实例一个账号） | ✅ |

---

## web2.0 能力清单（全部只读）

| 能力 | Flask 直连 | 网关模式 | 说明 |
|------|-----------|---------|------|
| 持仓列表（16 列） | ✅ | ✅ | 含基准成本、浮盈金额、个股止盈状态；涨幅兼容股票、ETF 与基金 |
| **委托队列（在途优先）** | ✅ | ✅ | `/api/orders`，感知"已报未成交"挂单 |
| 成交记录（按天分组 + 筛选） | ✅ | ✅ | 支持代码/名称、买卖方向、策略筛选 |
| 账户资产 / QMT 连接状态 | ✅ | ✅ | 连接状态持续轮询（15s） |
| 网格会话列表 | ✅ | ✅ | 状态、盈亏、自动/暂停（只读徽章） |
| **网格真实账本** | ✅ | ✅ | 批次 / LIFO 配对 / 汇总；网关侧为只读 SQL |
| 网格悬停速览卡 | ✅ | ✅ | 悬停不发请求，复用已加载会话数据 |
| 参数展示 | ✅ | ✅ | 只读；读不到显示 `--`，不用默认值冒充 |
| 运行开关状态（含三级开关总览） | ✅ | ✅ 反向探测 Flask | 探测不到时标为「未知」，见下方「三态状态」 |
| MACD 建议 + 迷你 K 线 | ✅ | ✅ | 悬停弹出 |
| 数据新鲜度指示 | ✅ | ✅ | 见下方「数据新鲜度」 |
| 🔒 任何写操作 | ❌ | ❌ | 一律使用 web1.0 |

### 三态状态（开 / 关 / 未知）

顶栏的运行开关是**只读徽章**，有三种状态：

| 状态 | 含义 |
|------|------|
| 开 / 关 | 后端返回的真实值 |
| **未知** | 后端未提供该状态 |

大部分开关持久化在账号 SQLite 的 `system_config` 表里，网关直接读即可。
但 `ENABLE_AUTO_OPERATION`（全局总闸）和 `ENABLE_SIMULATION_MODE` 按设计**不持久化**
（见 `config_manager.apply_configs_to_runtime` 的注释：总闸每次启动需手动确认），
只存在于主进程内存中。

**网关通过反向探测获取它们**：收到 `/api/status` 时调用该账号 Flask 的
`/api/status`（端口 = `5000 + 账号在 account_config.json 中的索引`，
带 5 秒缓存、1 秒超时），拿到真实的内存态开关。
若 Flask 配置了 `QMT_API_TOKEN`，网关会在反向探测中携带同一个 Token：
优先读取 `config.WEB_API_TOKEN`，其次读取环境变量 `QMT_API_TOKEN`，最后回退网关
`api_token`，避免开启鉴权后状态探测持续出现 401 告警。

为此，**web2.0 启动模式下 Flask 仍会启动**（只绑 `127.0.0.1`，不对外暴露）：
launcher 先探测该账号的 Flask 端口，空闲则启动、已被占用则跳过（设 `QMT_NO_FLASK=1`）
避免端口冲突。

| 场景 | 总闸显示 |
|------|---------|
| Flask 直连模式 | ✅ 真实值 |
| 网关模式（web1.0 或 web2.0 方式启动主程序） | ✅ 真实值（反向探测成功） |
| 手动设 `QMT_NO_FLASK=1` 启动 | ⚠️ 未知 |
| 账号不在 `account_config.json` 中 | ⚠️ 未知 |

!!! danger "为什么宁可显示「未知」也不猜"
    网关早期实现把这些开关**硬编码为 `True`**，导致监控界面无论后端实际状态如何
    都显示「自动ON」；`/api/config` 同样返回一组写死的默认值（35000/5.0/7.0…）。
    同理，账号不在配置列表时网关**不会**回落到默认端口 5000 —— 那会读到
    另一个账号的状态并张冠李戴。监控端显示假状态比不显示更危险。
    回归用例见 `test/test_xqm_monitor_endpoints.py`
    的 `TestStatusNoFakeState` 与 `TestFlaskReverseProbe`。

### 三级开关总览

自动止盈止损和网格交易各有一条**三级门控链**，任何一级关闭则该策略不产生新单。
web2.0 用「三级开关总览」面板（`TierSwitches.vue`，位于参数面板下方）一屏呈现整条链路，
省去逐项排查：

| 策略 | 第 1 级（全局总闸） | 第 2 级（策略开关） | 第 3 级（个体开关） |
|------|--------------------|--------------------|--------------------|
| 动态止盈止损 | `ENABLE_AUTO_OPERATION` | `ENABLE_AUTO_TRADING` | `positions.stop_profit_enabled`（**个股级**） |
| 网格交易 | `ENABLE_AUTO_OPERATION` | `ENABLE_GRID_TRADING` | `grid_trading_sessions.enabled`（**会话级**） |

面板呈现方式：

- 前两级用 ✔ / ✘ / ? 徽章（绿 / 红 / 灰），`?` 即上文的「未知」态
- 第 3 级因为是逐个体的，用聚合计数展示：
    - 动态止盈止损：`已开启 / 持仓总数`
    - 网格交易：`启用 / 暂停 / 会话总数`
- 逐个体的明细在各自列表里查看：持仓列表末列「自动止盈」列、网格会话列表的「自动/暂停」徽章

!!! note "第 3 级只读，切换请用 web1.0"
    个股止盈开关（`POST /api/holdings/stop_profit`）和网格会话开关
    （`POST /api/grid/session/<id>/enabled`）都是写操作，web2.0 只展示状态。

`ENABLE_DYNAMIC_STOP_PROFIT` 是更底层的**模块开关**（控制信号是否被检测），
仅存在于 `config.py`，web1.0 和 web2.0 都无对应 UI 控件。

### 数据新鲜度

监控界面最危险的失效方式不是"没数据"，而是"显示着 10 分钟前的数据却看不出来"。
因此每个数据块都标注自身年龄，并按阈值降级：

| 状态 | 阈值 | 表现 |
|------|------|------|
| 新鲜 | < 30s | 灰色时间标注 |
| 陈旧 | ≥ 30s | 琥珀色 |
| 失联 | ≥ 90s | 红色 + 顶部横幅告警 |

实现见 `web2.0/src/utils/freshness.ts`。

### 轮询节奏

轮询**不因任何业务开关而停止**（旧版「停止自动操作」会连带停掉数据刷新），
只随页面可见性调整频率：前台 3s 基准，后台切换到 15s，回到前台立即补一次全量刷新。

| 数据 | 前台周期 |
|------|---------|
| 系统状态 | 9s |
| **QMT 连接状态** | 15s |
| 委托队列 | 15s |
| 成交记录 | 18s |
| 持仓 + 网格会话 | 30s（SSE 可用时主要靠推送） |

!!! note "QMT 连接状态必须轮询"
    旧版只在页面初始化时查一次连接状态，QMT 掉线后顶栏指示灯会永远停在
    「QMT·OK」—— 对监控盘这是最不能坏的指示灯。

---

## 盈亏率口径

单只持仓的 `profit_ratio` 后端已乘 100（百分比数），而汇总指标是**小数**，
且两个后端键名还不一致：

| 后端 | 汇总键名 | 单位 |
|------|---------|------|
| Flask (`utils.calculate_position_metrics`) | `profit_ratio` | 小数 |
| 网关 (`server.flask_positions`) | `total_profit_ratio` | 小数 |

前端在 `web2.0/src/utils/metrics.ts` 的 `normalizeMetrics()` 统一收口：
兼容两个键名并 ×100，出口一律是百分比数。

!!! warning "历史缺陷"
    旧版直接把小数当百分比展示，5.23% 显示成 **0.05%**；Flask 直连模式下
    因键名不匹配更是恒显示 **0.00%**。回归用例见 `src/utils/metrics.test.ts`。

---

## 涨跌幅口径

持仓列表的 `change_percentage` 表示当日涨跌幅，计算来源为实时 tick 的 `lastPrice` 与 `lastClose`。

网关模式下，QMT 持仓接口常返回 6 位裸代码，而 xtdata `get_full_tick` 需要带交易所后缀。后端在请求 tick 前统一补全：

| 代码前缀 | 交易所后缀 | 示例 |
|---------|------------|------|
| `5` / `6` / `9` | `.SH` | `515050` → `515050.SH` |
| `0` / `2` / `3` / `15` / `16` / `18` | `.SZ` | `159915` → `159915.SZ` |

因此股票、ETF 与基金持仓都走同一实时行情口径；取不到 tick 或缺少前收盘价时才降级为 `0`。
回归用例见 `test/test_xqm_flask_compat.py` 的 `test_positions_change_percentage_for_etf_uses_xt_suffix`。

---

## 页面标题与发布版本

web1.0 和 web2.0 的页面标题都使用 `%MINIQMT_RELEASE_VERSION%` 占位符，真实发布版本统一来自项目根目录的 `release_version.json`。

- web1.0：`web_server.py` 在返回 `web1.0/index.html` 时注入版本号，同时给 `script.js` 添加基于 mtime 的缓存破坏参数
- web2.0：`web2.0/vite.config.ts` 在 Vite 构建阶段注入版本号，更新 `release_version.json` 后需要重新执行 `npm run build`

`web2.0/package.json` 中的 `version` 仅表示前端包自身元数据，不作为 miniQMT 发布版本来源。

---

## 顶部控制条

Flask 直连模式下，顶部控制条包含以下控件（部分仅后端存在，前端由配置表单统一渲染）：

| 控件 | 后端字段/配置 | 作用 |
|------|---------------|------|
| 开始/停止自动操作按钮 | `ENABLE_AUTO_OPERATION`（API 兼容字段 `isMonitoring`） | 全局自动操作总开关，只运行时生效不持久化；关闭时动态止盈止损和网格交易都不再产生新单 |
| 模拟交易模式 | `ENABLE_SIMULATION_MODE` | 切换实盘/模拟模式，**只运行时生效不持久化**；切换会重建内存持仓库，切到模拟时释放 `qmt_trader`、切到实盘时异步重连 QMT |
| 允许自动止盈 | `ENABLE_AUTO_TRADING`（保存配置字段 `globalAllowBuySell`） | 动态止盈止损自动执行开关，持久化 |
| 允许自动网格 | `ENABLE_GRID_TRADING`（保存配置字段 `globalAllowGridTrading`） | 网格模块自动执行开关，持久化 |
| 买 / 卖 | `ENABLE_ALLOW_BUY` / `ENABLE_ALLOW_SELL` | 手动和自动交易的方向权限 |
| 动态止盈（后端配置开关） | `ENABLE_DYNAMIC_STOP_PROFIT` | 控制动态止盈止损模块是否检测信号（此开关仅在后端 config.py 中存在，web1.0 前端无对应 UI 控件；如需切换请直接编辑配置文件） |
| 全仓止盈后暂停网格（后端配置/API） | `ENABLE_PAUSE_GRID_AFTER_TAKE_PROFIT_FULL`（保存配置字段 `pauseGridAfterTakeProfitFull`） | `take_profit_full` 成交确认后暂停同股网格会话，默认启用；当前 web1.0 无独立可视化开关，可通过配置文件、环境变量或 API 调整 |
| 网格自动/暂停 | `grid_trading_sessions.enabled` | 单只股票网格会话开关，位于**网格配置对话框内**（非顶部控制条），暂停后保留会话但不发新网格单 |
| 自动止盈（持仓列表末列） | `positions.stop_profit_enabled` | **个股级**动态止盈止损拨动开关，暂停后该股不再检测止盈止损信号，持久化 |

!!! note "为什么 API 仍叫 isMonitoring"
    早期前端使用 `isMonitoring` 表示顶部开关状态。为兼容旧接口，字段名保留不变，但当前语义已经是全局自动操作总开关；持仓监控线程状态请看 `positionMonitorRunning`。

Web1.0 参数区中，`API Token` 与"模拟交易模式""允许自动止盈""允许自动网格"位于同一行；Token 值由前端 `localStorage` 持久化，通过 `X-API-Token` 请求头发送，后端与 `QMT_API_TOKEN` 环境变量比对验证。

v3.8.9 起，web1.0 的请求链路统一收口到 `apiFetch`：配置读取、网格悬停卡、模板预览等 GET 请求都会携带 Token；SSE 也从原生 `EventSource` 改为 fetch stream，以便带上 `X-API-Token` 请求头。Token 输入框在 `input` / `change` 时都会即时写入 `localStorage`，刷新页面后无需重新输入。

!!! warning "URL 参数不再作为推荐 Token 通路"
    Token 应放在 `X-API-Token` 请求头，不要放在 `?token=` 查询参数中。查询参数容易被浏览器历史、代理日志和访问日志记录，且当前 web1.0 前端不会依赖这种形式。

### web1.0 总闸状态同步  [v3.8.9]

顶部“开始/停止自动操作”按钮对应运行时变量 `ENABLE_AUTO_OPERATION`。旧版只在页面初始化时读取一次 `isMonitoring`，如果后端被其他入口切换，总闸状态会长期停留在旧值。

当前 web1.0 会从 `/api/status` 和 SSE 推送持续同步 `isMonitoring`。用户刚点击按钮后的短时间内以前端意图为准，避免刚发出的操作被旧推送覆盖；随后以后端真实状态为准，保证界面不会漂移。

Web2.0 顶部不再有任何控制控件：原先的 7 个开关/按钮已全部替换为**只读状态徽章**
（开 / 关 / 未知三态），切换请到 web1.0 完成。

### 持仓列表（web1.0）

**列定义**（共 16 列）：网格 / 代码 / 名称 / 涨幅 / 价格 / 成本 / 盈亏 / 市值 / 可用 / 总数 / 浮盈 / 冲高 / 止损 / 建仓 / 基准 / 自动止盈。

首列「网格」为网格配置入口（点击打开网格配置对话框，非勾选框语义），列头已从旧版全选 checkbox 改为「网格」文本并居中。末列「自动止盈」为个股级动态止盈止损拨动开关：

- 开关状态来自 `positions.stop_profit_enabled`，切换即调用 `POST /api/holdings/stop_profit` 并持久化到 SQLite
- 关闭后该股不再检测止盈止损信号（不影响其网格会话，也不影响其他股票）
- 切换失败时开关自动回滚到原状态并提示

**字段释义**：
| 列名 | 后端字段 | 含义 |
|------|---------|------|
| 代码 | `stock_code` | 6 位股票代码 |
| 名称 | `stock_name` | 股票名称（悬停弹出 MACD 操盘建议） |
| 涨幅 | `change_percentage` | 当日涨跌幅（%），按 tick `lastPrice` / `lastClose` 计算 |
| 价格 | `current_price` | 实时市价 |
| 成本 | `cost_price` | 平均持仓成本 |
| 盈亏 | `profit_ratio` | 浮动盈亏比例（%） |
| 市值 | `market_value` | 持仓市值 |
| 可用 | `available` | 可卖股数 |
| 总数 | `volume` | 持仓总股数 |
| 浮盈 | `profit_triggered` | 首次止盈是否已触发（成交确认后标记） |
| 冲高 | `highest_price` | 持仓期间最高价 |
| 止损 | `stop_loss_price` | 动态止损/止盈价格（随 profit_triggered 切换算法） |
| 建仓 | `open_date` | 首次建仓日期 |
| 基准 | `base_cost_price` | 初次建仓成本（补仓摊薄后保持不变）。仅在后端返回有效值（> 0）时显示，缺失或无效显示 `--`，不再回退到 `cost_price` 冒充基准成本 |
| 自动止盈 | `stop_profit_enabled` | 个股级动态止盈止损开关（拨动切换） |

!!! note "增量刷新的比较字段  [v3.8.8]"
    web1.0 的持仓行走增量更新：`shouldUpdateRow()` 只比较关键字段，命中才重写单元格。`base_cost_price` 已加入该字段列表，否则基准成本从无效变为有效时（如首次建仓或旧库迁移回填后）整行会因「无变化」被跳过，页面一直停在 `--`。

与旧版布局相比的改动：

- ~~全选 checkbox~~ → 文本「网格」（全选逻辑随之移除）
- 新增末列「自动止盈」iOS 风格拨动开关
- 表头文字精简并统一居中
- 消息提示改为 `position:fixed` 浮层，不再挤压页面布局
- 持仓列表与下单日志的宽度比例从 2:1 调整为 **3:1**（`lg:grid-cols-4` + `col-span-3`）

**下单日志**：单行左至右为 名称 → B/S 方向（买红卖绿加粗）→ 金额 → 策略标签 → 时间，所有列严格对齐表头。内容靠左聚拢，无多余空白。时间列由后端 `/api/trade-records` 统一格式化为 `YYYY-MM-DD HH:MM:SS`，QMT 回报的微秒精度不会透出到前端。

### 数据管理按钮语义

| 按钮 | 端点 | 实际影响 |
|------|------|---------|
| 清空买卖日志 | `POST /api/data/clear_buysell` | 仅 `DELETE FROM trade_records`，**持仓数据不受影响** |
| 初始化持股 | `POST /api/holdings/init` | 从 QMT 拉取持仓重建本地元数据，成功与否以返回体 `status` 字段判定 |

!!! warning "二次确认文案与实际行为对齐"
    「清空买卖日志」的两道 `confirm` 提示曾写作"删除所有交易记录**和持仓信息**"，与后端只删 `trade_records` 的实现不符，容易让人误以为持仓会被清掉而不敢点。文案已改为"清空全部买入/卖出日志……不会清空持仓数据"。

### 卖出委托状态
动态止盈止损卖出委托由后端 `pending_orders` 跟踪。委托超时后，如果启用了自动重挂，系统会先撤销旧委托，再以新价格重新提交；`best` 对手价模式下买三价无效时会降级到买一价、最新价、收盘价或原信号价。

这意味着 Web 中“首次止盈已触发”代表**成交确认后的状态**，不是“委托已提交”。生产排查时应同时查看交易记录、后台日志和 QMT 当日委托，避免把待成交卖单误判为已经落账。

v3.8.9 起，若 `ENABLE_PAUSE_GRID_AFTER_TAKE_PROFIT_FULL=True`，`take_profit_full` 全仓止盈委托成交确认后，后端会把同股活跃网格会话切到暂停（`grid_trading_sessions.enabled=False`）。**v3.9.0 起 `stop_loss` 止损清仓成交后同样触发该暂停**（两者都卖出 `available` 全量，属同一清仓语义），复用同一开关。web1.0 / web2.0 中该会话仍会显示为 active，但“自动/暂停”状态会变为暂停；用户可在 web1.0 的网格配置对话框中手动恢复自动执行。

### 下单日志的策略标签  [v3.9.0]

下单日志中每条记录尾部的标签由 `trade_records.strategy` 映射而来，**存储值始终是英文标识，仅显示层做中文映射**：

| 存储值 | 显示 | 来源 |
|--------|------|------|
| `simu` | 模拟 | 模拟成交 |
| `auto_partial` | 浮盈 | 首次止盈（卖 60%） |
| `auto_full` | 止盈 | 动态止盈清仓 |
| `stop_loss` | 止损 | 止损清仓 |
| `grid` | 网格 | 网格买卖 |
| `manual` | 手动 | XtQuantManager 网关侧手动卖出 |
| `M_real` / `M_simu` | 手买 / 模买 | `strategy.manual_buy()` 实盘 / 模拟 |
| `manual_real` / `manual_simu` | 手卖 / 模卖 | `strategy.manual_sell()` 实盘 / 模拟 |
| `external` | 外部 | QMT 客户端等本机未发的委托 |
| `default` | 默认 | 未指定策略 |

!!! warning "映射表分散在三处，修改需同步"
    - `web_server.py` 的 `strategy_labels` —— 服务端下发 `strategy_label` 字段的源头。**web2.0 在 Flask 直连模式下优先取该值**，因此只改前端不生效。
    - `web1.0/script.js` 的 `LOG_STRATEGY_LABELS` —— web1.0 只读取原始 `strategy` 字段，不使用 `strategy_label`。
    - `web2.0/src/components/OrderLog.vue` 的 `strategyLabels` —— 网关模式兜底，因为 XtQuantManager 不下发 `strategy_label`。

    取值链为 `t.strategy_label || strategyLabels[t.strategy] || t.strategy`；未命中映射时会直接显示原始英文值。

### 网格悬停卡片口径

web1.0 持仓列表中，鼠标悬停在已启动网格交易的个股复选框上会显示网格状态卡片。卡片顶部显示**运行时长**，其余比例字段统一使用后端小数比例，由前端格式化为百分比，避免重复乘以 100。

| 字段 | 数据来源 | 口径 |
|------|----------|------|
| 运行时长 | 会话创建时间差 | 从 `created_at` 差值计算（天/时/分） |
| 网格盈亏 | `stats.pnl_snapshot.profit_ratio` | LIFO 账本真实盈亏率；账本不可用时使用 `get_pnl_snapshot()` 的降级口径 |
| 已实现/未实现 | `stats.pnl_snapshot.realized_pnl` / `unrealized_pnl` | 已配对卖出收益 + 未平网格库存浮动盈亏 |
| 交易次数 | `stats.trade_count` / `buy_count` / `sell_count` | 已成交确认并落账后的网格交易次数 |
| 资金使用 | `stats.current_investment` / `config.max_investment` | 当前未平网格投入占最大投入额度比例 |
| 中心价偏离 | `stats.deviation_ratio` + `stats.center_deviation_ratio` | 当前网格中心价相对初始中心价的漂移幅度；显示为正数并标注”上移/下移” |

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
│ API Token： •••••••••••• (远程访问必填)        │
├──────────────────────────────────────────────┤
│ 连通性测试：[ 测试连接 ]                      │
│   ✓ 连接成功 — 2 个账号, 2 个在线             │
└──────────────────────────────────────────────┘
```

### 字段说明

| 字段 | 网关模式 | 直连模式 |
|------|---------|---------|
| 地址 | 网关地址（所有账户共用），如 `http://127.0.0.1:8888` | Flask 地址（每账户独立），在账户下拉菜单的 ✎ 中编辑 |
| Token | xtquant_manager 的 `api_token`（**远程访问必填**：网关所有数据端点均需 Token） | Flask 的 `QMT_API_TOKEN` 环境变量值 |
| 测试连接 | `GET /api/v1/health` — 显示账号总数与在线数（该端点免 Token 可达，故未填 Token 也能测通，但随后拉取持仓/成交会返回 401） | `GET /api/status` — 显示账户 ID |

### 自动化行为

- **保存后自动发现账号**：保存连接配置后调用 `discoverAccounts()`，从网关同步真实账号 ID 到下拉列表（无需手动新增）。该接口需 Token；未填 Token 时返回空列表，界面回退到本地保存的账号条目
- **HTTPS Mixed Content 警告**：HTTPS 页面访问 HTTP 后端会被浏览器阻止，面板自动给出警告
- **无 Token 远程警告**：远程连接而不设 Token 时提示安全风险
- **8s 超时 + 非 JSON 检测**：测试连接遇到反代/Nginx 错误页时给出明确诊断

---

## 启动菜单（miniqmt.bat）

`miniqmt.bat`（或 `python scripts/_launcher.py menu`）打开交互式控制台菜单。

**v3.9.1 起菜单改为分页**：原先 8 个分区共 27 项一屏装不下；现首页只放**日常运行**
（按键 `5-9` / `a-c` **保持不变**），其余折叠为三个二级页。首页共 28 行，一屏可容纳。

```
首页（日常运行）：
  [5] 查看账号配置      [6] 查看运行状态
  [7] 启动全部(实盘)    [8] 启动全部(模拟)    [9] 启动指定账号
  [a] 停止全部(优雅)    [b] 停止指定账号      [c] 强制全部
  账号状态: N/M 运行中（每账号一行）
  XtTrader 通道: ✓ miniQMT (xttrader 直连)

  [1] 环境与部署    [2] 服务管理    [3] 数据与配置    [q] 退出
```

| 二级页 | 入口 | 选项 |
|--------|------|------|
| 环境与部署 | `[1]` | `[0]` 首次部署向导 · `[1]` 检查 Python 环境 · `[2]` 安装/更新依赖 · `[3]` 检查配置文件 · `[4]` git pull |
| 服务管理 | `[2]` | `[d]` 启动网关 · `[e]` 停止网关 · `[f]` 网关状态 · **`[g]` 打开 web2.0 UI** · `[h]` 重启网关 · `[i]` 网关日志 · `[j]`-`[m]` 自动买入 |
| 数据与配置 | `[3]` | `[n]` Tushare · `[o]` 大QMT IPC · `[p]` XtTrader 通道总控 · `[r]`-`[u]` [交割单数据](settlement-export.md) |

**导航**：二级页内按 **`[b]` 或直接回车**返回主菜单；`[q]` 在任何页都退出。
标题栏显示当前页名（如 `miniQMT 总控制台 · 服务管理`）。

!!! note "实现要点"
    各页按键**天然不重叠**（env 用 `0-4`、首页用 `5-9,a-c`、services 用 `d-m`、
    data 用 `n-p,r-u`），因此底层分派链是**共用的**，只在入口加
    「首页 `1/2/3` 改判为导航」+「按页校验按键」两道守卫，改动面最小。
    顺带修正：以前在任何页面输入非法键只会静默重绘，现在会提示。

!!! warning "交割单相关的 `[r][s][t]` 有停机守卫"
    这三项会改写 `trade_records`，有账号运行时会**直接拒绝执行**；
    且强制先跑 dry-run 预演，需输入 `yes` 才正式执行。`[u]` 导出是只读，随时可跑。

菜单底部自动显示账号运行摘要（有缓存，仅在可能改变运行状态的操作后重新采集）
与当前 XtTrader 通道状态。

### 控制台编码鲁棒性  [v3.8.9]

`miniqmt.bat` 会设置 `PYTHONIOENCODING=utf-8:replace`，`scripts/_launcher.py` 对控制台 `print` / `input` 做安全包装。旧版 Windows 控制台或 ASCII 输出环境遇到中文、勾选符号等 Unicode 文本时，会以替换字符输出而不是抛 `UnicodeError` 终止菜单；依赖检查同步包含 `python-dotenv`，确保项目根 `.env` 可作为环境变量 fallback 加载。

### 停止流程  [v3.8.8]

菜单 `[a]` / `[b]` 的优雅停止**只靠文件信号**：控制台向 `data_<account_id>/stop_signal` 写入目标 PID，`main.py` 主循环 1 秒内检测到该文件并走 `cleanup()` 优雅退出；超过 `--timeout` 仍未退出才 `taskkill` 强制结束。`[c]` 跳过信号直接强杀。

- **不再发送 Ctrl+C**：`CREATE_NEW_CONSOLE` 启动的账号进程有独立控制台，`GenerateConsoleCtrlEvent` 送不到目标，反而可能误打到当前 `miniqmt.bat` 控制台，弹出 "Terminate batch job (Y/N)?" 把总控制台自己中断掉。
- **写信号前先建目录**：账号数据目录不存在时（首次启动尚未落盘）此前会因 `FileNotFoundError` 判定"停止信号发送失败"，现自动补建。
- **pid.txt 失效时按端口兜底**：`pid.txt` 缺失或指向已死进程时，控制台按该账号的 Flask 端口 `netstat` 反查 LISTENING 进程，并用命令行校验确属本项目的 `main.py`（唯一命中才采用），输出中会标注「端口兜底」。
- **批量先发后等**：多账号停止时先给所有目标写完信号再统一等待退出，避免前一个账号等待超时期间后面的账号完全没被通知。
- 启动时显式传入 `--account-id`，进程命令行自带账号标识，端口兜底与状态查看都能准确归属到账号。
- 菜单 `[6] 查看运行状态` 同样走这套解析：靠 `pid.txt` 命中显示「运行中」，靠端口反查命中显示「运行中(端口)」，`pid.txt` 存在但进程已死且端口无对应进程显示「PID失效」。

### Web 模式偏好记忆

启动菜单 [7]/[8]/[9] 会读取 `data/.web_mode` 中上次的选择：

- `1` → web1.0（Flask）— 系统在每账号端口上启动完整 Flask 服务
- `2` → web2.0（xtquant_manager）— 界面与网关由 xtquant_manager 统一托管；Flask 仍在本机启动（端口空闲时），供网关反向读取运行时开关

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
