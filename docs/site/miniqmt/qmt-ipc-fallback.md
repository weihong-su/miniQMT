# 大 QMT 文件 IPC

miniQMT 使用账号独立的 JSON 目录，与大 QMT 内置策略交换账户、行情、委托及成交。具备原生接口时，大 QMT 在模型交易主线程调用 `get_trade_detail_data`、`passorder`、`cancel` 和行情上下文，交易与行情不要求恢复 miniQMT 的 xttrader 直连权限。

## 能力与适用范围

| 项目 | 当前实现 |
| --- | --- |
| 账户与持仓 | 原生 ACCOUNT / POSITION 快照，保留真实可卖数量 |
| 实时行情 | `ContextInfo` 报价导出到 `quotes.json`，分别校验导出时间与单股来源时间 |
| 下单 / 撤单 | 文件指令驱动原生接口，按委托与真实分笔回执确认 |
| 多账号 | 每账号独立目录及账号校验 |
| 历史日线 | miniQMT 统一历史源策略；IPC 行情适配器不提供原生历史下载 |
| 延迟 | 文件轮询，适合中低频；不作为 tick 级交易通道 |

原生账户接口不存在时仍保留 xttrader 快照兼容路径；有原生接口但查询失败时保留上一份文件，并在生产端诊断中记录失败，不能把失败导出当成空仓。

## 部署与更新

1. 在大 QMT 内置编辑器加载仓库的 [QMT_trade_executor.py](https://github.com/weihong-su/miniQMT/blob/main/qmt-trader/QMT_trade_executor.py)，使用模型交易入口运行。
2. 两端设置相同 IPC 根目录和账号。策略端创建账号 `config.json`；非默认根目录须同时配置生产端的 `QMT_IPC_ROOT`。
3. miniQMT 开启 `ENABLE_QMT_IPC_FALLBACK=true`，关闭另外两个可选交易后端。完整参数见 [配置参考](configuration.md)。
4. 先核对心跳、账户、报价和生产端诊断，再确认 Web 中的总开关、策略分开关和会话状态。需要验证订单行为时使用 [隔离模拟回归](testing.md)。
5. 更新 Python 后端后重启相应进程，核对启动日志的 `code_id`。仅修改 miniQMT 启动时序时，无需重新部署未变动的大 QMT 脚本；完整通道升级需保证两端版本兼容。

模型交易中的原生查询、行情和交易接口必须在大 QMT 主线程执行。脚本通过前台循环保持轮询；独立心跳线程只负责保活，不能代替主线程或伪造导出成功时间。不要选择外部本地 Python 运行，否则可能缺少注入接口。

## 目录与诊断文件

```text
C:\QuantIPC\
  TEST_ACCOUNT\
    config.json
    status\
      heartbeat.json
      account.json
      quotes.json
      producer.json
      subscriptions.json
    orders\
      pending\
      processing\
      done\
      done_archive\
    cancel\
```

| 文件 | 核对内容 |
| --- | --- |
| `heartbeat.json` | 生产端是否持续保活 |
| `account.json` | 账号、来源时间、资产字段、持仓与可卖量 |
| `quotes.json` | 文件导出时间，以及各股原始 `time` / `timetag`、价格和盘口 |
| `producer.json` | 版本、PID、启动时间、最后主循环时点、各导出任务的尝试 / 成功时间及错误 |
| `subscriptions.json` | miniQMT 请求导出的证券列表 |

`config.json` 至少包含账号与类型；`qmt_path` 用于兼容路径。示例：

```json
{
  "account_id": "TEST_ACCOUNT",
  "account_type": "STOCK",
  "qmt_path": "C:/QMT/userdata_mini",
  "ipc_secret": ""
}
```

`QMT_IPC_SECRET` 非空时由两端校验订单共享密钥；凭据通过本地环境配置，不提交到仓库。

## 快照与资产口径

原生快照的 `source` 为 `vba`，兼容路径为 `xttrader`。`volume<=0` 的残留持仓行会被过滤；原生持仓缺少有效可卖量、账号不符或只返回部分可解析数据时，不发布一份不完整的新持仓组合。行情价缺失而市值存在时，可由市值 / 股数得到持仓参考价，仍需与报价文件的来源时间分开判断。

```json
{
  "timestamp": "2026-11-27 10:00:00",
  "account_id": "TEST_ACCOUNT",
  "source": "vba",
  "total_asset": 100000.0,
  "available": 45000.0,
  "frozen": 5000.0,
  "market_value": 50000.0,
  "positions": [
    {
      "stock": "000001",
      "volume": 5000,
      "available": 4000,
      "cost": 9.5,
      "market_price": 10.0,
      "market_value": 50000.0,
      "name": "示例股票"
    }
  ]
}
```

资产核对公式为 `total_asset - available - frozen - market_value`。普通估值时点差只告警，不断连、不关闭全局自动操作，不用差额反推可用现金。字段无效或整份快照不可用时保留旧证据并标记状态，不能用零金额冒充刷新成功。

生产端原生路径在总资产缺失或非正时保留“可用 + 冻结 + 持仓市值”的兼容合成；旧 xttrader 兼容路径仍有自身兜底。合成读数不等同于财务校验通过，应结合 `source`、资产差额及 `openValidation` 核对。

## 时效与就绪

| miniQMT 配置 | 默认值 | 含义 |
| --- | ---: | --- |
| `QMT_IPC_HEARTBEAT_MAX_AGE` | 10 秒 | 心跳年龄上限 |
| `QMT_IPC_SNAPSHOT_MAX_AGE_SECONDS` | 90 秒 | 账户来源时间上限 |
| `QMT_IPC_QUOTE_MAX_AGE_SECONDS` | 15 秒 | 连续交易时段单股报价来源时间上限 |
| `QMT_IPC_TIMESTAMP_FUTURE_TOLERANCE_SECONDS` | 5 秒 | 允许的未来时钟偏差 |
| `QMT_IPC_READ_RETRIES` | 2 | 瞬态共享 / 访问冲突的额外读取次数 |
| `QMT_IPC_READ_RETRY_DELAY_SECONDS` | 0.02 秒 | 额外读取间隔 |
| `QMT_IPC_RECONNECT_MAX_INTERVAL_SECONDS` | 300 秒 | 完整重连失败的退避上限 |
| `QMT_IPC_HEALTH_PROBE_INTERVAL_SECONDS` | 5 秒 | 独立恢复探测间隔 |

消费端重试成功后仍重新校验账号、格式和来源时间；解析错误与错误账号不会被当成临时冲突放行。单轮就绪诊断复用已读取快照，不用长期缓存制造新鲜时间。

`GET /api/connection/status` 的 `ipc` 分别返回 `heartbeat`、`account`、`quotes.stocks`、`producer`、`ready` 与 `trading_ready`。后两者含义不同：

- `ready`：当前时段的数据链路具备就绪证据，仍需查看单股诊断。
- `trading_ready`：连续交易时段内，配置股票池的报价可用于交易判断。
- 午休、收盘允许 `ready=true` 且 `trading_ready=false`，并标注参考行情。
- 新心跳或新文件时间不能替代账户及单股来源时间；来源停留超过 15 秒的报价仍会在交易时段被拒绝。

## 回执、启动与跨日恢复

异步下单原子发布指令后返回本地委托号，成交由后续回执确认。同步等待超时时保留已知委托号并提交追撤，避免上层重复下单；返回撤单已提交不等于撤单已完成，应继续核对终态。

`filled`、`partial`、`partial_cancelled` 按真实成交编号推送分笔；部分成交继续跟踪，撤单后仅处理未成交余量。`rejected` / `cancelled` 不被当作成交。

启动顺序为：完整初始化 PositionManager 状态与锁 → 连接与注册回调 → 启动同步线程。后台回报获取全局管理器或执行器时等待完整实例；没有注册消费者或投递抛出异常时，不提前确认回执已成功处理。

持仓与订单查询合并在途文件和已完成回执。成交对账按实际成交日、账号、委托、证券和方向隔离；网格聚合与分笔核对 `fill_ids`，历史补录不能删除今日同号占位。诊断及账本详见 [交割单数据管道](settlement-export.md)。

## 历史与 Web 展示

IPC 行情适配器不调用本地 xtdata 历史下载。标准历史链为 Tushare → Mootdx；指数分别使用 `index_daily` 与指数接口，股票和基金使用对应接口。日线截至最近已完成交易日。

账户 `fresh/stale/unavailable`、实时/参考报价、开盘财务 `valid/pending_review` 和历史缓存分别显示。指数历史刷新失败但缓存足量时标记原日期；缺少必需日线时不给出新的 MACD 建议。字段见 [Web API](web-api.md)，界面见 [Web 前端](web-frontend.md)。

## 排障顺序

1. 对照两端账号、根目录、主进程 PID、`code_id` 与 `producer.version`，确认运行的是本次代码。
2. 查看心跳，再看生产端 `last_main_tick_at` 和账户 / 报价的 `last_success_at`，定位主循环停顿或导出失败。
3. 查看账户来源时间和数量字段；只凭空持仓、零金额或新文件时间不能判断清仓、断连或恢复。
4. 查看各股报价来源时间及拒绝原因；不通过放宽时效阈值消除来源停顿。
5. 查看 pending / processing / done 和撤单终态；未知提交先核对券商与回执，不手写第二笔相同指令。
6. 如果旧版本出现缺少 `pending_orders_lock` / `callback_refresh_lock` 的启动错误，更新 miniQMT 的启动时序修正后，在维护窗口重启核对。
