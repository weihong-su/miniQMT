# XtQuantManager 使用说明书

> **版本**: 2.0.0 | **最后更新**: 2026-04-11 | **许可证**: BSL 1.1

---

## 目录

1. [产品概述](#1-产品概述)
2. [架构说明](#2-架构说明)
3. [场景配置指南](#3-场景配置指南)
4. [API 远程调用手册](#4-api-远程调用手册)
5. [多账户使用实例](#5-多账户使用实例)
6. [Python SDK 参考](#6-python-sdk-参考)
7. [安全配置](#7-安全配置)
8. [可观测性](#8-可观测性)
9. [与 miniQMT 集成](#9-与-miniqmt-集成)
10. [服务管理脚本](#10-服务管理脚本)
11. [常见问题](#11-常见问题)

---

## 1. 产品概述

XtQuantManager 是 miniQMT 的 **xtquant 接口统一管理层**，通过 HTTP 服务将迅投 QMT 的交易接口（xttrader）和行情接口（xtdata）封装为 RESTful API。

当前实现同时提供两类接口：标准 `/api/v1/*` 多账号 API，以及供 web2.0 网关模式复用的 Flask 兼容端点（`/api/status`、`/api/positions`、`/api/positions-all`、`/api/accounts`、`/api/connection/status`、`/api/config`、`/api/trade-records`、`/api/grid/sessions`）。兼容端点以只读监控为主，通过 `X-Account-Id` 选择账号，并合并 QMT 实时字段与账号 SQLite 元数据；配置保存、初始化持仓、网格启停等写操作仍需 Flask 直连模式。

| 痛点 | 解决方案 |
|------|---------|
| 无法管理多账号 | 多账号注册表，支持同时管理任意数量 QMT 账号 |
| 断线无法自动重连 | 三级健康监控 + 指数退避自动重连 |
| 超时保护不统一 | 全接口统一超时保护（默认 3 秒） |
| 无可观测性 | 实时指标（延迟、错误率、P95）+ HTTP 查询 |
| xtquant 硬耦合 | HTTP 抽象层 + 开关，零侵入切换 |

---

## 2. 架构说明

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
│  XtQuantAccount × N   HealthMonitor                      │
│  · 连接管理            · 三级健康检查                      │
│  · 超时保护            · 指数退避重连                      │
│  · 指标收集            · 断连回调（<1s感知）               │
├──────────────────────────────────────────────────────────┤
│  xtquant API (xttrader + xtdata，本机 QMT 客户端)         │
└──────────────────────────────────────────────────────────┘
```

### 健康监控三级策略

```
Level 0  每 30s  is_healthy()     内存检查，无 I/O
              │ 不健康
Level 1        ping()             真实探测（get_full_tick，3s 超时）
              │ 失败
Level 2        reconnect()        指数退避重连（60s → 3600s）
                                  + on_disconnected 事件驱动（<1s）
```

---

## 3. 场景配置指南

### 3.1 场景一：本机单账号（开发/测试）

最简配置，无需认证，适合本机开发调试。

**启动方式 1 — 命令行（推荐）：**

```bash
# 使用管理脚本（见第 10 节）
xtquant_manager\xqm_manager.bat start

# 或直接启动
C:\Users\PC\Anaconda3\envs\python39\python.exe -m xtquant_manager
```

**启动方式 2 — Python 代码：**

```python
from xtquant_manager import XtQuantServer, XtQuantServerConfig, XtQuantManager, AccountConfig

server = XtQuantServer(XtQuantServerConfig(
    host="127.0.0.1",
    port=8888,
))
server.start(blocking=False)

manager = XtQuantManager.get_instance()
manager.register_account(AccountConfig(
    account_id="TEST_ACC_1",
    qmt_path="C:/QMT/userdata_mini",
))
```

**配置文件方式（`xtquant_manager_config.json`，放项目根目录）：**

```json
{
  "host": "127.0.0.1",
  "port": 8888,
  "api_token": "",
  "accounts": [
    {
      "account_id": "TEST_ACC_1",
      "qmt_path": "C:/QMT/userdata_mini",
      "account_type": "STOCK"
    }
  ]
}
```

---

### 3.2 场景二：本机双账号（多策略隔离）

两个 QMT 账号同时运行，每账号独立的连接、指标和健康监控。

**前提**：两个 QMT 客户端实例分别登录，路径不同。

**`xtquant_manager_config.json`：**

```json
{
  "host": "127.0.0.1",
  "port": 8888,
  "api_token": "",
  "health_check_interval": 30.0,
  "reconnect_cooldown": 60.0,
  "accounts": [
    {
      "account_id": "TEST_ACC_1",
      "qmt_path": "C:/QMT/userdata_mini",
      "account_type": "STOCK",
      "call_timeout": 3.0
    },
    {
      "account_id": "TEST_ACC_2",
      "qmt_path": "C:/QMT1/userdata_mini",
      "account_type": "STOCK",
      "call_timeout": 3.0
    }
  ]
}
```

验证两个账号均已注册：

```bash
curl http://127.0.0.1:8888/api/v1/accounts
# {"success":true,"data":{"accounts":["TEST_ACC_1","TEST_ACC_2"]}}

curl http://127.0.0.1:8888/api/v1/health
# {"data":{"total":2,"healthy":2,...}}
```

---

### 3.3 场景三：局域网共享（多机访问）

交易机运行 XtQuantManager，分析机/监控机通过局域网调用。

**服务端配置（交易机，如 192.168.1.100）：**

```json
{
  "host": "192.168.1.100",
  "port": 8888,
  "api_token": "your-secret-token-here",
  "allowed_ips": ["192.168.1.0/24"],
  "rate_limit": 120,
  "ssl_certfile": "certs/server.crt",
  "ssl_keyfile": "certs/server.key",
  "accounts": [
    {
      "account_id": "TEST_ACC_1",
      "qmt_path": "C:/QMT/userdata_mini"
    }
  ]
}
```

生成自签 TLS 证书：

```bash
python xtquant_manager/utils/gen_cert.py --ip 192.168.1.100 --out certs/
```

**客户端调用（分析机）：**

```python
from xtquant_manager.client import XtQuantClient, ClientConfig

client = XtQuantClient(config=ClientConfig(
    base_url="https://192.168.1.100:8888",
    account_id="TEST_ACC_1",
    api_token="your-secret-token-here",
    verify_ssl=False,       # 自签证书跳过验证
    # ca_cert="certs/ca.crt"  # 或指定 CA 验证
))
positions = client.position()
```

---

### 3.4 场景四：嵌入 miniQMT 主程序

不单独启动进程，由 miniQMT `main.py` 自动启动并管理。

**`config.py` 修改：**

```python
ENABLE_XTQUANT_MANAGER = True        # 开启
XTQUANT_MANAGER_URL = "http://127.0.0.1:8888"
XTQUANT_MANAGER_TOKEN = ""           # 可选
```

启动 `python main.py` 后系统自动：
1. 在后台线程启动 HTTP 服务（127.0.0.1:8888）
2. 从 `account_config.json` 注册账号
3. 所有 xtquant 调用透明路由到 HTTP

> 此模式下交易和行情接口与 `ENABLE_XTQUANT_MANAGER=False` 行为完全一致，无需修改任何业务代码。

---

### 3.5 场景五：无人值守长期运行

配合看门狗和健康监控，在断线、崩溃时自动恢复。

**关键配置项：**

```json
{
  "host": "127.0.0.1",
  "port": 8888,
  "health_check_interval": 30.0,
  "reconnect_cooldown": 60.0,
  "watchdog_interval": 10.0,
  "watchdog_restart_cooldown": 30.0,
  "heartbeat_interval": 1800.0,
  "accounts": [
    {
      "account_id": "TEST_ACC_1",
      "qmt_path": "C:/QMT/userdata_mini",
      "reconnect_base_wait": 60.0,
      "max_reconnect_attempts": 5
    }
  ]
}
```

| 参数 | 作用 |
|------|------|
| `health_check_interval` | HealthMonitor 轮询间隔，建议 30s |
| `reconnect_cooldown` | 两次重连之间的最小冷却时间，防止重连风暴 |
| `watchdog_interval` | 服务线程存活检查间隔，崩溃后 10s 内重启 |
| `reconnect_base_wait` | 指数退避起点（第 1 次重连等待 60s，第 2 次 120s…） |

断线感知三条路径（互补）：

| 路径 | 感知延迟 | 机制 |
|------|---------|------|
| 事件驱动 | < 1 秒 | `on_disconnected` 回调 → 立即标记 |
| 累计失败 | ~ 15 秒 | 连续 3 次 API 失败 → 触发重连 |
| 主动探测 | 最长 30 秒 | HealthMonitor ping → 三级检查 |

---

### 3.6 配置参数全量参考

#### `xtquant_manager_config.json` 全量字段

```json
{
  "host": "127.0.0.1",
  "port": 8888,
  "api_token": "",
  "allowed_ips": [],
  "rate_limit": 60,
  "enable_hmac": false,
  "hmac_secret": "",
  "ssl_certfile": "",
  "ssl_keyfile": "",
  "health_check_interval": 30.0,
  "reconnect_cooldown": 60.0,
  "watchdog_interval": 10.0,
  "watchdog_restart_cooldown": 30.0,
  "heartbeat_interval": 1800.0,
  "accounts": [
    {
      "account_id": "必填",
      "qmt_path": "必填",
      "account_type": "STOCK",
      "call_timeout": 3.0,
      "reconnect_base_wait": 60.0,
      "max_reconnect_attempts": 5
    }
  ]
}
```

#### `AccountConfig` 参数

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `account_id` | str | 必需 | QMT 交易账号 |
| `qmt_path` | str | 必需 | `userdata_mini` 目录路径 |
| `account_type` | str | `STOCK` | `STOCK` / `FUTURE` |
| `call_timeout` | float | `3.0` | API 调用超时（秒） |
| `connect_timeout` | float | `30.0` | 连接超时，超时不挂起进程 |
| `reconnect_base_wait` | float | `60.0` | 指数退避起点（秒） |
| `max_reconnect_attempts` | int | `5` | 触发指数退避的最大次数 |
| `ping_stock` | str | `000001.SZ` | 心跳探测股票代码 |

---

## 4. API 远程调用手册

**Base URL**: `http://{host}:{port}/api/v1`

**认证**: 请求头 `X-API-Token: <token>`（本机访问 / 未配置 token 时可省略）

**统一响应格式**:
```json
{
  "success": true,
  "data": { ... },
  "error": null
}
```

**HTTP 状态码**:

| 状态码 | 含义 |
|-------|------|
| 200 | 成功 |
| 201 | 创建成功（注册账号、下单） |
| 401 | Token 错误 |
| 403 | IP 未授权 |
| 404 | 账号不存在 |
| 422 | 请求参数错误 |
| 429 | 速率限制 |
| 502 | xtquant 调用失败 |
| 504 | 操作超时 |

---

### 4.1 账号管理

#### 注册账号

```http
POST /api/v1/accounts
Content-Type: application/json
X-API-Token: <token>

{
  "account_id": "TEST_ACC_1",
  "qmt_path": "C:/QMT/userdata_mini",
  "account_type": "STOCK",
  "call_timeout": 3.0,
  "reconnect_interval": 60.0,
  "max_reconnect_attempts": 5
}
```

响应（HTTP 201）：
```json
{
  "success": true,
  "data": {
    "account_id": "TEST_ACC_1",
    "connected": true,
    "message": "注册成功"
  }
}
```

> 账号已存在时幂等处理，重复注册返回 201（不报错）。

#### 注销账号

```http
DELETE /api/v1/accounts/{account_id}
```

#### 列出所有账号

```http
GET /api/v1/accounts
```

响应：
```json
{"success": true, "data": {"accounts": ["TEST_ACC_1", "TEST_ACC_2"]}}
```

#### 账号状态

```http
GET /api/v1/accounts/{account_id}/status
```

响应：
```json
{
  "success": true,
  "data": {
    "account_id": "TEST_ACC_1",
    "connected": true,
    "reconnecting": false,
    "reconnect_attempts": 0,
    "last_ping_ok_time": 1775550307.58,
    "connected_at": 1775550307.58,
    "xtdata_available": true,
    "xttrader_available": true
  }
}
```

---

### 4.2 交易操作

#### 下单

```http
POST /api/v1/accounts/{account_id}/orders
Content-Type: application/json

{
  "stock_code": "000001.SZ",
  "order_type": 23,
  "order_volume": 100,
  "price_type": 11,
  "price": 10.50,
  "strategy_name": "grid",
  "order_remark": "网格第3层买入"
}
```

| 字段 | 说明 |
|------|------|
| `order_type` | 23 = 限价买入，24 = 限价卖出 |
| `price_type` | 11 = 限价，5 = 市价 |
| `order_volume` | 股数（最小 100） |

响应（HTTP 201）：
```json
{"success": true, "data": {"order_id": 2014314497}}
```

#### 撤单

```http
DELETE /api/v1/accounts/{account_id}/orders/{order_id}
```

#### 查询持仓

```http
GET /api/v1/accounts/{account_id}/positions
```

响应：
```json
{
  "success": true,
  "data": {
    "positions": [
      {
        "资金账号": "TEST_ACC_1",
        "证券代码": "300057",
        "股票余额": 7400,
        "可用余额": 0,
        "成本价": 6.661,
        "市值": 49358.0,
        "冻结数量": 7400
      }
    ]
  }
}
```

#### 查询资产

```http
GET /api/v1/accounts/{account_id}/asset
```

响应：
```json
{
  "success": true,
  "data": {
    "资金账户": "TEST_ACC_1",
    "可用金额": 0.0,
    "冻结金额": 0.0,
    "持仓市值": 49358.0,
    "总资产": 544607.71
  }
}
```

#### 查询当日委托

```http
GET /api/v1/accounts/{account_id}/orders
```

#### 查询当日成交

```http
GET /api/v1/accounts/{account_id}/trades
```

---

### 4.3 行情操作

#### 实时 Tick

```http
GET /api/v1/market/tick?stock_codes=000001.SZ,600036.SH&account_id=TEST_ACC_1
```

响应：
```json
{
  "success": true,
  "data": {
    "000001.SZ": {
      "lastPrice": 10.52,
      "open": 10.40,
      "high": 10.60,
      "low": 10.35,
      "volume": 123456789,
      "amount": 1298765432.0
    }
  }
}
```

#### 历史行情

```http
GET /api/v1/market/history
  ?stock_code=000001.SZ
  &account_id=TEST_ACC_1
  &period=1d
  &start_time=20260101
  &end_time=20260411
```

| 参数 | 说明 | 可选值 |
|------|------|--------|
| `period` | K 线周期 | `1m` `5m` `15m` `30m` `60m` `1d` |
| `start_time` | 开始日期 | `YYYYMMDD` 格式 |
| `end_time` | 结束日期，空 = 今天 | `YYYYMMDD` 格式 |

#### 下载历史数据

```http
POST /api/v1/market/download
Content-Type: application/json

{
  "account_id": "TEST_ACC_1",
  "stock_code": "000001.SZ",
  "period": "1d",
  "start_time": "20260101",
  "end_time": "20260411"
}
```

---

### 4.4 可观测性

#### 全局健康检查（无需 Token）

```http
GET /api/v1/health
```

响应：
```json
{
  "success": true,
  "data": {
    "accounts": {
      "TEST_ACC_1": {"connected": true, "reconnecting": false, ...},
      "TEST_ACC_2": {"connected": true, "reconnecting": false, ...}
    },
    "total": 2,
    "healthy": 2
  }
}
```

#### 单账号健康（无需 Token）

```http
GET /api/v1/health/{account_id}
```

#### 全局调用指标

```http
GET /api/v1/metrics
```

响应：
```json
{
  "success": true,
  "data": {
    "TEST_ACC_1": {
      "total_calls": 42,
      "success_calls": 42,
      "error_calls": 0,
      "timeout_calls": 0,
      "error_rate": 0.0,
      "avg_latency_ms": 4.0,
      "p50_latency_ms": 3.0,
      "p95_latency_ms": 16.0,
      "uptime_seconds": 3600.0,
      "ops": {
        "query_positions": {"total": 20, "success": 20, "error": 0, "timeout": 0},
        "query_asset":     {"total": 10, "success": 10, "error": 0, "timeout": 0},
        "order_stock":     {"total": 5,  "success": 5,  "error": 0, "timeout": 0}
      }
    }
  }
}
```

#### 单账号指标

```http
GET /api/v1/metrics/{account_id}
```

---

### 4.5 curl 速查表

```bash
BASE="http://127.0.0.1:8888/api/v1"
TOKEN="your-token"   # 无 Token 时删除 -H 行

# 健康检查（无需 Token）
curl $BASE/health

# 注册账号
curl -X POST $BASE/accounts \
  -H "Content-Type: application/json" \
  -H "X-API-Token: $TOKEN" \
  -d '{"account_id":"TEST_ACC_1","qmt_path":"C:/QMT/userdata_mini","account_type":"STOCK"}'

# 列出账号
curl -H "X-API-Token: $TOKEN" $BASE/accounts

# 账号状态
curl -H "X-API-Token: $TOKEN" $BASE/accounts/TEST_ACC_1/status

# 查询持仓
curl -H "X-API-Token: $TOKEN" $BASE/accounts/TEST_ACC_1/positions

# 查询资产
curl -H "X-API-Token: $TOKEN" $BASE/accounts/TEST_ACC_1/asset

# 当日委托
curl -H "X-API-Token: $TOKEN" $BASE/accounts/TEST_ACC_1/orders

# 当日成交
curl -H "X-API-Token: $TOKEN" $BASE/accounts/TEST_ACC_1/trades

# 下单（限价买入 000001.SZ 100 股，价格 10.50）
curl -X POST $BASE/accounts/TEST_ACC_1/orders \
  -H "Content-Type: application/json" \
  -H "X-API-Token: $TOKEN" \
  -d '{"stock_code":"000001.SZ","order_type":23,"order_volume":100,"price_type":11,"price":10.50}'

# 撤单
curl -X DELETE -H "X-API-Token: $TOKEN" $BASE/accounts/TEST_ACC_1/orders/2014314497

# 实时行情
curl "$BASE/market/tick?stock_codes=000001.SZ,600036.SH&account_id=TEST_ACC_1" \
  -H "X-API-Token: $TOKEN"

# 历史行情
curl "$BASE/market/history?stock_code=000001.SZ&account_id=TEST_ACC_1&period=1d&start_time=20260101" \
  -H "X-API-Token: $TOKEN"

# 调用指标
curl -H "X-API-Token: $TOKEN" $BASE/metrics/TEST_ACC_1

# 注销账号
curl -X DELETE -H "X-API-Token: $TOKEN" $BASE/accounts/TEST_ACC_1
```

---

## 5. 多账户使用实例

### 5.1 实例一：双账号独立交易

**场景**：账号 A（TEST_ACC_1）做网格交易，账号 B（TEST_ACC_2）做止盈止损，互不干扰。

**配置**（`xtquant_manager_config.json`）：

```json
{
  "host": "127.0.0.1",
  "port": 8888,
  "accounts": [
    {
      "account_id": "TEST_ACC_1",
      "qmt_path": "C:/QMT/userdata_mini",
      "account_type": "STOCK"
    },
    {
      "account_id": "TEST_ACC_2",
      "qmt_path": "C:/QMT1/userdata_mini",
      "account_type": "STOCK"
    }
  ]
}
```

**Python 代码 — 分别查询两账号持仓**：

```python
from xtquant_manager.client import XtQuantClient, ClientConfig

# 账号 A
client_a = XtQuantClient(config=ClientConfig(
    base_url="http://127.0.0.1:8888",
    account_id="TEST_ACC_1",
))

# 账号 B
client_b = XtQuantClient(config=ClientConfig(
    base_url="http://127.0.0.1:8888",
    account_id="TEST_ACC_2",
))

pos_a = client_a.position()   # 账号 A 持仓
pos_b = client_b.position()   # 账号 B 持仓

balance_a = client_a.balance()
balance_b = client_b.balance()

print(f"账号A总资产: {balance_a['总资产'].iloc[0]:,.2f}")
print(f"账号B总资产: {balance_b['总资产'].iloc[0]:,.2f}")
```

**验证两账号均健康**：

```bash
curl http://127.0.0.1:8888/api/v1/health
```

```json
{
  "data": {
    "accounts": {
      "TEST_ACC_1": {"connected": true, "xtdata_available": true, "xttrader_available": true},
      "TEST_ACC_2": {"connected": true, "xtdata_available": true, "xttrader_available": true}
    },
    "total": 2,
    "healthy": 2
  }
}
```

---

### 5.2 实例二：运行时动态添加第二个账号

已有服务在运行，无需重启，通过 API 注册新账号。

```bash
# 服务已运行，当前只有账号 A
curl http://127.0.0.1:8888/api/v1/accounts
# {"data":{"accounts":["TEST_ACC_1"]}}

# 动态注册账号 B
curl -X POST http://127.0.0.1:8888/api/v1/accounts \
  -H "Content-Type: application/json" \
  -d '{
    "account_id": "TEST_ACC_2",
    "qmt_path": "C:/QMT1/userdata_mini",
    "account_type": "STOCK"
  }'
# {"data":{"account_id":"TEST_ACC_2","connected":true,"message":"注册成功"}}

# 确认两个账号
curl http://127.0.0.1:8888/api/v1/accounts
# {"data":{"accounts":["TEST_ACC_1","TEST_ACC_2"]}}
```

---

### 5.3 实例三：多账号资产汇总监控

统计所有账号的总资产、总持仓市值。

```python
import httpx

BASE = "http://127.0.0.1:8888/api/v1"

def get_all_assets():
    # 获取账号列表
    accounts = httpx.get(f"{BASE}/accounts").json()["data"]["accounts"]

    total_asset = 0.0
    total_market_value = 0.0

    for account_id in accounts:
        resp = httpx.get(f"{BASE}/accounts/{account_id}/asset").json()
        if resp["success"]:
            asset = resp["data"]
            total_asset += asset.get("总资产", 0)
            total_market_value += asset.get("持仓市值", 0)
            print(f"  [{account_id}] 总资产: {asset['总资产']:>12,.2f}  "
                  f"持仓市值: {asset['持仓市值']:>12,.2f}  "
                  f"可用资金: {asset['可用金额']:>12,.2f}")

    print(f"\n  汇总: 总资产 {total_asset:,.2f}  持仓市值 {total_market_value:,.2f}")

get_all_assets()
```

---

### 5.4 实例四：多账号同步下单（Python）

```python
import httpx
import concurrent.futures

BASE = "http://127.0.0.1:8888/api/v1"

def place_order(account_id, stock_code, order_type, volume, price):
    resp = httpx.post(
        f"{BASE}/accounts/{account_id}/orders",
        json={
            "stock_code": stock_code,
            "order_type": order_type,
            "order_volume": volume,
            "price_type": 11,
            "price": price,
        },
        timeout=10,
    ).json()
    return account_id, resp

# 两账号同时买入同一只股票
orders = [
    ("TEST_ACC_1", "000001.SZ", 23, 100, 10.50),
    ("TEST_ACC_2", "000001.SZ", 23, 200, 10.50),
]

with concurrent.futures.ThreadPoolExecutor() as pool:
    futures = [pool.submit(place_order, *o) for o in orders]
    for f in concurrent.futures.as_completed(futures):
        account_id, result = f.result()
        order_id = result["data"].get("order_id", "FAILED")
        print(f"账号 {account_id}: order_id = {order_id}")
```

---

### 5.5 实例五：多账号健康监控脚本

```python
import httpx
import time

BASE = "http://127.0.0.1:8888/api/v1"
CHECK_INTERVAL = 30  # 秒

def check_health():
    resp = httpx.get(f"{BASE}/health", timeout=5).json()
    data = resp["data"]
    print(f"[{time.strftime('%H:%M:%S')}] 账号总数: {data['total']}  "
          f"健康: {data['healthy']}")
    for acct_id, state in data["accounts"].items():
        status = "OK" if state["connected"] else "DISCONNECTED"
        retries = state.get("reconnect_attempts", 0)
        print(f"  {acct_id}: {status}"
              + (f"  (重连次数: {retries})" if retries else ""))
    if data["healthy"] < data["total"]:
        print("  [ALERT] 有账号断线！请检查 QMT 客户端。")

while True:
    check_health()
    time.sleep(CHECK_INTERVAL)
```

---

### 5.6 多账号配置注意事项

| 要点 | 说明 |
|------|------|
| QMT 路径必须不同 | 每个账号需要独立的 `userdata_mini` 目录 |
| QMT 客户端独立登录 | 两个账号需要两个 QMT 进程分别登录 |
| `session_id` 自动分配 | 无需手动指定，Manager 自动管理 |
| 账号 ID 大小写敏感 | `TEST_ACC_1` 和 `TEST_ACC_1 `（有空格）视为不同账号 |
| 连接失败不阻止注册 | `connected: false` 时 HealthMonitor 会持续重试 |
| 独立指标 | `GET /metrics/{id}` 每账号独立统计，互不干扰 |

---

## 6. Python SDK 参考

### `XtQuantClient` 方法列表

```python
from xtquant_manager.client import XtQuantClient, ClientConfig

client = XtQuantClient(config=ClientConfig(
    base_url="http://127.0.0.1:8888",
    account_id="TEST_ACC_1",
    api_token="",          # 无 Token 时留空
    timeout=5.0,
    max_retries=2,
))
```

#### 连接与状态

```python
client.connect()                # 验证服务可达 → (self, self) 或 None
client.close()                  # 释放 HTTP 连接池
client.is_connected() -> bool
client.health() -> dict
client.get_account_status() -> dict
client.get_metrics() -> dict
```

#### 持仓与资产（兼容 easy_qmt_trader）

```python
client.position() -> pd.DataFrame       # 所有持仓
client.balance() -> pd.DataFrame        # 账户资金
client.query_stock_asset() -> dict      # 资产 dict
client.query_stock_orders() -> pd.DataFrame   # 当日委托
client.query_stock_trades() -> pd.DataFrame   # 当日成交
```

#### 交易

```python
client.order_stock(
    stock_code,      # "000001.SZ"
    order_type,      # 23=限价买, 24=限价卖
    order_volume,    # 股数
    price_type=11,   # 11=限价
    price=0.0,
    strategy_name="",
    order_remark="",
) -> int             # order_id >= 0, 失败返回 -1

client.buy(security, order_type, amount, price=0.0) -> int
client.sell(security, order_type, amount, price=0.0) -> int
client.cancel_order_stock(order_id: int) -> int  # 0=成功
```

#### 行情

```python
client.get_full_tick(stock_codes: list) -> dict
client.get_market_data_ex(fields, stock_list, period, start_time, end_time) -> dict
client.download_history_data(stock_code, period, start_time, end_time) -> bool
```

### `XtDataAdapter` — xtdata 兼容

```python
from xtquant_manager.client import XtDataAdapter

xtdata = XtDataAdapter(client)

# 与 xtquant.xtdata 接口兼容
xtdata.connect() -> bool
xtdata.get_full_tick(stock_codes) -> dict
xtdata.get_market_data_ex(fields, stock_list, period, start_time, end_time) -> dict
xtdata.download_history_data(stock_code, period, start_time, end_time)
```

---

## 7. 安全配置

### 7.1 本机开发（无认证）

```json
{"host": "127.0.0.1", "port": 8888, "api_token": ""}
```

`/api/v1/health` 和 `/api/v1/health/{id}` 始终无需 Token，供存活探针使用。

### 7.2 局域网（Token + IP 白名单）

```json
{
  "host": "192.168.1.100",
  "port": 8888,
  "api_token": "at-least-32-char-random-string",
  "allowed_ips": ["192.168.1.0/24"],
  "rate_limit": 120
}
```

### 7.3 HTTPS（自签证书）

```bash
# 生成证书
python xtquant_manager/utils/gen_cert.py --ip 192.168.1.100 --out certs/
```

```json
{
  "ssl_certfile": "certs/server.crt",
  "ssl_keyfile": "certs/server.key"
}
```

### 7.4 HMAC 签名（公网）

```json
{"enable_hmac": true, "hmac_secret": "very-long-random-secret"}
```

```python
from xtquant_manager.security import generate_hmac_headers
headers = generate_hmac_headers("GET", "/api/v1/health", secret="very-long-random-secret")
```

---

## 8. 可观测性

所有指标均基于滑动窗口（延迟: 最近 1000 次，错误率: 最近 100 次），实时更新，通过 `/metrics` API 查询。

| 指标 | 说明 |
|------|------|
| `total_calls` | 累计调用次数 |
| `error_rate` | 最近 100 次错误率 |
| `avg_latency_ms` | 平均延迟（毫秒） |
| `p50_latency_ms` | P50 延迟 |
| `p95_latency_ms` | P95 延迟 |
| `timeout_calls` | 超时次数 |
| `ops` | 按操作类型分组统计 |

---

## 9. 与 miniQMT 集成

| 场景 | `ENABLE_XTQUANT_MANAGER = False` | `= True` |
|------|----------------------------------|---------|
| 交易接口 | 直接调用 `easy_qmt_trader` | 通过 `XtQuantClient` HTTP |
| 行情接口 | 直接调用 `xtquant.xtdata` | 通过 `XtDataAdapter` HTTP |
| 启动 | 无额外服务 | 自动启动 HTTP 服务 |
| 代码改动 | — | 零改动 |

**注意**：`True` 模式下 `register_trade_callback` 为 no-op，`_on_trade_callback` 不触发。不影响核心交易功能，`pending_orders` 在下次持仓轮询时自动同步。

---

## 10. 服务管理脚本

[`xtquant_manager/xqm_manager.bat`](../xtquant_manager/xqm_manager.bat) — Windows 批处理管理工具。

```
# 交互式菜单（双击运行）
xqm_manager.bat

# 命令行模式
xqm_manager.bat start    启动服务（等待就绪，最多 15s）
xqm_manager.bat stop     停止服务
xqm_manager.bat restart  重启服务
xqm_manager.bat status   查看状态 + 健康检查 + 最近日志
xqm_manager.bat ui       用浏览器打开 test_ui_a.html
xqm_manager.bat logs     实时追踪日志（PowerShell tail）
```

**手动测试界面**（位于 `xtquant_manager/test_ui/`）：

| 文件 | 风格 | 特色功能 |
|------|------|---------|
| `test_ui_a.html` | 终端/功能 | JSON 高亮、历史记录 50 条、cURL 导出 |
| `test_ui_b.html` | 可视化 | 持仓表格、资产卡片、指标进度条、自动刷新 |

详见 [`xtquant_manager/test_ui/TEST_GUIDE.md`](../xtquant_manager/test_ui/TEST_GUIDE.md)。

---

## 11. 常见问题

**Q: 服务启动后账号 `connected: false`？**
- 检查 `qmt_path` 是否指向正确的 `userdata_mini`
- 确认 QMT 客户端已启动并登录对应账号
- 等待 30 秒，HealthMonitor 会自动重连

**Q: `timeout /t` 报错（Git Bash 环境）？**
- `xqm_manager.bat` 在 Git Bash 里执行时 `timeout` 会报语法错误（Linux timeout 语法不同）
- 实际功能正常，直接在 cmd.exe 或资源管理器双击运行则无此问题

**Q: 网页测试界面发请求报网络错误？**
- 原因：浏览器从 `file://` 打开 HTML，CORS 策略阻止跨域请求
- 解决：重启服务（服务已内置 `CORSMiddleware allow_origins=*`，重启后生效）

**Q: 多账号时某账号断线，其他账号受影响吗？**
- 不受影响。每个账号独立的连接实例，一个断线不影响其他账号的交易和行情。

**Q: 如何不重启服务临时增加账号？**
- 通过 `POST /api/v1/accounts` 动态注册，无需重启（见 5.2 节）。

**Q: `position()` 返回空 DataFrame？**
- 确认 `connected: true`（`GET /accounts/{id}/status`）
- 查看 `/metrics/{id}` 中 `query_positions` 的 `error_calls` 是否非零
- 确认账号下确实有持仓

**Q: 如何确认 CORS 已生效？**

```bash
curl -s -I -X OPTIONS "http://127.0.0.1:8888/api/v1/accounts" \
  -H "Origin: null" -H "Access-Control-Request-Method: POST" | grep -i access-control
# 期望: access-control-allow-origin: *
```

---

*文档版本 2.0.0 | BSL 1.1 授权 | 商业用途需获授权 | 2030-04-13 后转 MIT | miniQMT 开发团队*
