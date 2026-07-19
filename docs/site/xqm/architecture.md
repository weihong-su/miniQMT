# 架构说明

## 分层架构

```
┌──────────────────────────────────────────────────────────┐
│  miniQMT 主体代码 / 外部调用方                             │
├──────────────────────────────────────────────────────────┤
│  XtQuantClient (HTTP 客户端，兼容 easy_qmt_trader 接口)   │
│  XtDataAdapter (行情适配器，兼容 xtquant.xtdata 接口)     │
├──────────────────────────────────────────────────────────┤
│                  HTTP REST API / JSON                    │
├──────────────────────────────────────────────────────────┤
│  XtQuantServer (FastAPI + uvicorn)                       │
│  SecurityMiddleware │ CORSMiddleware │ API 路由           │
├──────────────────────────────────────────────────────────┤
│  XtQuantManager (单例，多账号注册表)                       │
├──────────────────────────────────────────────────────────┤
│  XtQuantAccount × N         HealthMonitor                │
│  · 连接管理                  · 三级健康检查               │
│  · 超时保护                  · 指数退避重连               │
│  · 指标收集                  · 断连回调（<1s 感知）        │
├──────────────────────────────────────────────────────────┤
│  StopProfitMonitor           · 动态止盈止损后台线程       │
│  · 止损检测（复刻 position_manager.check_trading_signals）│
│  · 首次止盈回撤监控           · 动态止盈档位计算          │
├──────────────────────────────────────────────────────────┤
│  xtquant API (xttrader + xtdata，本机 QMT 客户端)         │
└──────────────────────────────────────────────────────────┘
```

**核心设计原则**：xtquant 接口（xttrader/xtdata）运行在本机，XtQuantManager 将其包装为 HTTP 服务；调用方通过 HTTP 客户端访问，不直接依赖 xtquant。切换 `ENABLE_XTQUANT_MANAGER` 开关即可在直连模式和 HTTP 模式间透明切换，无需改动业务代码。

---

## 健康监控三级策略

```
Level 0  每 30s   is_healthy()    内存检查，无 I/O
              │ 不健康
Level 1         ping()            真实探测（get_full_tick，3s 超时）
              │ 失败
Level 2         reconnect()       指数退避重连（60s → 120s → ... → 3600s）
```

并发路径：`on_disconnected` 事件回调（< 1 秒）与 Level 2 轮询互补，确保断线感知延迟最短。

### 断线感知三条路径

| 路径 | 感知延迟 | 机制 |
|------|---------|------|
| 事件驱动 | < 1 秒 | `on_disconnected` 回调 → 立即标记 |
| 累计失败 | ~ 15 秒 | 连续 3 次 API 失败 → 触发重连 |
| 主动探测 | 最长 30 秒 | HealthMonitor ping → 三级检查 |

---

## 模块文件说明

| 文件 | 职责 |
|------|------|
| `account.py` | 单账号封装（`XtQuantAccount`）：连接、超时、指标收集 |
| `manager.py` | 多账号注册表（`XtQuantManager` 单例） |
| `health_monitor.py` | 后台健康检查线程（三级策略 + 指数退避） |
| `stop_profit.py` | **动态止盈止损监控** — 后台线程，复用 `position_manager.py` 算法 |
| `server.py` | FastAPI 路由定义（所有 `/api/v1/` 端点，含止盈止损 API） |
| `server_runner.py` | 启动入口（uvicorn + 信号处理 + StopProfitMonitor 启停） |
| `standalone.py` | 独立运行应用（StandaloneApplication） |
| `standalone_config.py` | 独立运行配置加载器（含止盈止损参数） |
| `client.py` | HTTP 客户端（`XtQuantClient` + `XtDataAdapter`） |
| `security.py` | 安全中间件（IP 白名单、Token 验证、HMAC、速率限制） |
| `models.py` | Pydantic 请求/响应模型 |
| `exceptions.py` | 自定义异常类型 |
| `watchdog.py` | HTTP 服务看门狗（崩溃自动重启） |
| `metrics.py` | 调用指标收集器 |
| `timeout.py` | 统一超时保护 |

`server.py` 同时包含两类路由：标准 `/api/v1/*` 多账号 API，以及供 web2.0 网关模式复用的 Flask 兼容端点（`/api/status`、`/api/positions`、`/api/positions-all`、`/api/accounts`、`/api/connection/status`、`/api/config`、`/api/trade-records`、`/api/grid/sessions`）。兼容端点以只读监控为主，持仓会合并 QMT 实时数据和 `data_<account_id>/trading.db` 中的持久化元数据；网格会话从 SQLite 降级读取，不持有 Flask 进程里的策略写状态。
