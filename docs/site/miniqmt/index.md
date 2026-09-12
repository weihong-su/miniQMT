# miniQMT 概述

基于迅投 QMT API 的**无人值守量化交易系统**，集成自动化交易策略执行、智能持仓管理、动态止盈止损和网格交易。

## 核心特性

- **无人值守运行** — 线程健康监控与自动重启，7x24 小时稳定运行
- **双层存储架构** — 内存数据库 + SQLite 持久化，高性能与数据安全兼顾
- **动态止盈止损** — 浮盈越高止盈位越高，最大化收益同时控制风险
- **智能网格交易** — 自动低吸高抛，支持回调触发、多档位、风险分级、全仓止盈后暂停同股网格和 LIFO 真实盈亏账本
- **北交所代码支持** — `.BJ` / `BJ.` / 裸 `920xxx` 自动归一，交易、行情、Web 与 IPC/RPC 路径一致识别
- **Web API 统一鉴权** — web1.0 配置 `QMT_API_TOKEN` 后所有 `/api/*`（含 GET/SSE）需 Token，公网模式可 fail-closed
- **自动买入模块** — 独立进程从候选池筛选标的，复用 Web 买入 API 下单
- **Web 监控界面** — 实时持仓、资产、日志查看，端口 5000
- **交易通道四选一** — 默认 xttrader 直连，控制台可切换直连/IPC/RPC
- **行情源健康评分** — 内存观察 xtdata/Mootdx 成功率、延迟、新鲜度，作为数据质量门禁
- **交割单数据管道** — 归因所需字段在成交当下落库（交易所成交时间、手续费来源、持仓快照、每日净值），导出只读；支持券商对账单导入回填历史成交时间


---

## 快速开始

### 1. 安装依赖

```bash
pip install -r utils/requirements.txt
```

### 2. 配置账号

创建 `account_config.json`（项目根目录）：

```json
{
  "account_id": "您的交易账号",
  "account_type": "STOCK",
  "qmt_path": "C:/光大证券金阳光QMT实盘/userdata_mini"
}
```

### 3. 配置股票池（可选）

创建 `stock_pool.json`：

```json
["000001.SZ", "600036.SH", "920118.BJ"]
```

也可填写裸代码，系统会按交易所规则补全后缀；发布配置建议显式写 `.SH` / `.SZ` / `.BJ`。

### 4. 启动

```bash
python main.py
```

### 5. 访问 Web 界面

浏览器打开 `http://localhost:5000`

!!! warning "实盘前必查"
    - `ENABLE_SIMULATION_MODE = True` — 默认模拟模式
    - `ENABLE_AUTO_OPERATION = False` — 默认不产生自动交易动作
    - `ENABLE_AUTO_TRADING = False` — 默认不执行非网格自动策略
    - 切换实盘前务必确认总开关、分策略开关和账号配置

---

## 下一步

- [架构说明](architecture.md) — 核心设计原则和线程模型
- [配置参考](configuration.md) — 所有可配置参数详解
- [止盈止损](stop-profit-loss.md) — 动态止盈止损策略
- [网格交易](grid-trading.md) — 网格交易功能使用指南
- [自动买入](autobuy.md) — 候选池筛选、指数门禁、防重买入、调度与复盘
- [大QMT文件 IPC](qmt-ipc-fallback.md) — xttrader 失效时的大QMT降级交易通道
- [大QMT RPC Redis 部署](qmt-rpc-redis-setup.md) — RPC 交易通道的 Redis 安装与配置
- [QMT order_id 匹配](qmt-order-id-matching.md) — 异步下单 `seq -> order_id` 调试、实盘验证和防重复下单
- [Web 前端](web-frontend.md) — web1.0 / web2.0 双模式架构、连接设置、远程部署
- [Web API](web-api.md) — RESTful API 接口文档
- [无人值守运行](unattended.md) — 长期运行和自动恢复
- [交割单数据管道](settlement-export.md) — 归因字段落库、券商对账单导入、历史回填与交割单导出
