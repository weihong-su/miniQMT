# 大 QMT 信号下单包运行手册

更新时间：2026-07-01

## 1. 当前结论

这个包已经具备大 QMT 运行入口和 QMT 适配层：

- `bigqmt_signal_trader_strategy.py`：大 QMT 策略入口，响应 `init`、`handlebar/adjust`、委托回调、成交回调。
- `BigQmtMarketDataProvider`：封装 `ContextInfo.get_full_tick()` 和 `ContextInfo.get_instrumentdetail()`。
- `BigQmtPositionProvider`：封装 `get_trade_detail_data(account, 'STOCK', 'POSITION')`。
- `BigQmtOrderGateway`：按 `qmt_jq_trade` 的参数形状调用 `passorder()`。
- 默认模式仍是 `dryrun`，不会真实发委托。

截至 2026-07-01 凌晨，已完成：

- 本地单元测试：`32 tests OK`。
- QMT `python` 目录导入测试通过。
- 模拟 `init/adjust/sync_positions` 回调通过。
- 模拟 `mode="bigqmt"` + fake `passorder` 参数测试通过。
- Redis Stream 信号源、Redis 状态写回、Redis 持仓同步已实现。
- 本机 Redis `127.0.0.1:6379 db=5` dry-run 集成测试通过。

还没有完成：

- 没有在 QMT 页面里通过“模型交易”做真实实盘委托验证。
- 没有把 miniQMT 的真实买卖指令切成只写 Redis 信号。
- 没有在真实账号上启用大 QMT `passorder` 实盘执行。

所以今天开盘可以先验证“大 QMT 是否能持续加载和触发回调”，以及 Redis dry-run 链路是否能消费测试信号并写回状态。如果要真的由大 QMT 替代 miniQMT 下单，必须先完成 miniQMT 只写信号、单账户灰度和实盘风控确认。

## 1.1 当前 QMT 页面检查结果

2026-07-01 08:00 左右检查大 QMT“模型交易”页面：

- 页面里当前运行/展示的策略名称是“网格策略”。
- 没有看到 `bigqmt_signal_trader` 或 `bigqmt_signal_trader_redis_dryrun`。
- “策略日志”页没有 `[bigqmt_signal_trader] init ok` 或 `[bigqmt_signal_trader] adjust ok`。

结论：当前这个文件还没有真正挂到大 QMT 模型交易里运行。

## 2. 官方文档里的关键点

### 2.1 编辑器运行不等于真实交易

大 QMT 编辑器里的“运行/回测/模型运行”主要用于公式、模型、信号验证。要真正把委托发送到交易柜台，需要进入“模型交易”页面，把策略加入模型交易实例并绑定资金账号。

结论：

- 编辑器“运行”：适合检查 `init/handlebar` 是否报错，不用于确认真实下单。
- 模型交易“模拟信号”：适合开盘先观察回调、信号、价格、状态，不真实下单。
- 模型交易“实盘交易”：只有确认信号源、幂等、风控都 OK 后才能打开。

### 2.2 不要勾选“启动本地 python”

官方文档说明，“启动本地 python”是把脚本作为独立 Python 进程运行。这个模式不会按大 QMT 回调机制触发 `init(ContextInfo)`、`handlebar(ContextInfo)`。

本包是回调式策略入口，必须让 QMT 自己调用：

- 不勾选：`init`、`handlebar`、`order_callback`、`deal_callback` 正常触发。
- 勾选：脚本只会像普通 Python 文件一样执行 import，通常会马上结束，不会进入交易回调。

### 2.3 必须跳过历史 bar

QMT 加载策略时可能先跑历史 K 线，再进入最后一根实时 bar。入口已经加了保护：

```python
if hasattr(ContextInfo, "is_last_bar") and not ContextInfo.is_last_bar():
    return None
```

这可以避免未来接入真实信号源后，在历史回放阶段误消费当前待处理信号。

## 3. 文件部署

大 QMT 运行目录：

```text
<QMT_PYTHON_DIR>
```

源代码目录：

```text
<REPO_ROOT>\src
```

部署命令：

```powershell
Copy-Item -Path '<REPO_ROOT>\src\bigqmt_signal_trader\*' `
  -Destination '<QMT_PYTHON_DIR>\bigqmt_signal_trader' `
  -Recurse -Force

Copy-Item -LiteralPath '<REPO_ROOT>\src\bigqmt_signal_trader_strategy.py' `
  -Destination '<QMT_PYTHON_DIR>\bigqmt_signal_trader_strategy.py' `
  -Force

Copy-Item -LiteralPath '<REPO_ROOT>\src\bigqmt_signal_trader_dryrun.py' `
  -Destination '<QMT_PYTHON_DIR>\bigqmt_signal_trader_dryrun.py' `
  -Force

Copy-Item -LiteralPath '<REPO_ROOT>\src\bigqmt_signal_trader_redis_dryrun.py' `
  -Destination '<QMT_PYTHON_DIR>\bigqmt_signal_trader_redis_dryrun.py' `
  -Force
```

清理缓存：

```powershell
Get-ChildItem -LiteralPath '<QMT_PYTHON_DIR>\bigqmt_signal_trader' `
  -Recurse -Filter '__pycache__' -Directory -ErrorAction SilentlyContinue |
  Remove-Item -Recurse -Force

Get-ChildItem -LiteralPath '<QMT_PYTHON_DIR>' `
  -Filter '__pycache__' -Directory -ErrorAction SilentlyContinue |
  Remove-Item -Recurse -Force
```

## 4. QMT 编辑器加载测试

目的：只验证 QMT 能加载入口、能触发 `init/adjust`，不会真实下单。

### 4.1 策略文件内容

在 QMT 策略编辑器中新建一个策略，例如 `大QMT信号下单_dryrun`，内容使用下面这段。脚本必须保持 ASCII，避免 QMT 编辑器编码问题。

```python
#coding:gbk
from bigqmt_signal_trader_strategy import (
    adjust,
    configure,
    deal_callback,
    handlebar,
    init,
    on_order,
    on_trade,
    order_callback,
    set_account_id,
    sync_positions,
)

try:
    ACCOUNT_ID = account
except NameError:
    ACCOUNT_ID = ""

if ACCOUNT_ID:
    set_account_id(ACCOUNT_ID)

configure(mode="dryrun", account_id=ACCOUNT_ID or "dryrun")
```

### 4.2 QMT 页面设置

在策略编辑器右侧/基本信息里：

- 运行周期：建议先选 `1分钟` 或 `3分钟`。
- 标的：建议先用流动性稳定的指数或股票，例如 `000300.SH`。
- 启动本地 python：不要勾选。
- 自动交易/实盘交易：不要在编辑器测试阶段打开。

点击顺序：

1. 保存。
2. 编译。
3. 运行。

期望输出：

```text
[bigqmt_signal_trader] init ok
[bigqmt_signal_trader] adjust ok
```

如果只看到“开始运行/结束运行”，但没有 `init ok/adjust ok`：

- 检查是否勾选了“启动本地 python”。
- 检查是否真的导入了 `bigqmt_signal_trader_strategy.py`。
- 检查 QMT 输出窗或 `XtClient_Formula_YYYYMMDD.log` 是否有 traceback。

## 4.3 Redis dry-run 入口

已经新增安全观察入口：

```text
<QMT_PYTHON_DIR>\bigqmt_signal_trader_redis_dryrun.py
```

默认配置：

```text
ACCOUNT_ID = bigqmt_probe
Redis = 127.0.0.1:6379 db=5
Stream = bigqmt:signals:bigqmt_probe
Status = bigqmt:signal_status:bigqmt_probe:{signal_id}
Position = bigqmt:positions:bigqmt_probe
OrderGateway = DryRunOrderGateway
```

如果 QMT 的 `python` 目录存在本地私有配置文件，则 Redis 连接会被覆盖：

```text
<QMT_PYTHON_DIR>\bigqmt_signal_trader_local_config.py
```

格式：

```python
# coding: utf-8
BIGQMT_REDIS_CONFIG = {
    "host": "YOUR_REDIS_HOST",
    "port": 6379,
    "db": 5,
    "username": "",
    "password": "...",
}
```

这个文件含 Redis 密码，只放 QMT 本地目录，不提交到源码仓库，也不要贴进文档。

这个入口只用于开盘观察 Redis 链路，不会真实下单，也不会消费真实账号流。不要把 `ACCOUNT_ID` 改成真实资金账号，除非你明确知道 dry-run 会 ack 掉该账号的 Redis 信号。

写入一条测试信号：

```powershell
cd <REPO_ROOT>
python -B -c "import sys,datetime,json,redis; sys.path.insert(0,'src'); from bigqmt_signal_trader.adapters.signal_redis import push_trade_signal; r=redis.Redis(host='127.0.0.1',port=6379,db=5); push_trade_signal(r, {'signal_id':'probe-001','account_id':'bigqmt_probe','action':'BUY','stock_code':'600000.SH','amount':100,'price_type':'FIX_PRICE','price':10.0,'created_at':'2026-07-01 09:31:00','expire_at':'2026-07-01 23:59:00','schema_version':1})"
```

运行后检查状态：

```powershell
python -B -c "import redis; r=redis.Redis(host='127.0.0.1',port=6379,db=5,decode_responses=True); print(r.hgetall('bigqmt:signal_status:bigqmt_probe:probe-001'))"
```

期望状态里出现：

```text
status = DRY_RUN
user_order_id = dryrun:bq:...
```

## 5. 模型交易页面运行

目的：开盘后观察策略在真实行情驱动下是否持续触发，而不是只在编辑器里跑一次。

### 5.1 第一步只跑模拟信号

进入 QMT 的“模型交易”页面：

1. 新建模型交易实例。
2. 选择上面的策略文件。
3. 绑定资金账号。
4. 标的使用 `000300.SH` 或其他稳定标的。
5. 周期先用 `1分钟`。
6. 运行方式先选择“模拟信号”或等价的非实盘模式。
7. 确认“启动本地 python”没有勾选。
8. 启动模型交易。

开盘后观察：

- 输出窗是否出现 `[bigqmt_signal_trader] init ok`。
- 第一根实时 bar 后是否出现 `[bigqmt_signal_trader] adjust ok`。
- 日志里是否没有 `Traceback`、`ModuleNotFoundError`、`run script failed`。
- 委托页面不应该出现真实委托，因为当前是 dry-run 且空信号源。

如果要验证 Redis dry-run 链路，则选择 `bigqmt_signal_trader_redis_dryrun.py`，并向 `bigqmt:signals:bigqmt_probe` 写入测试信号。它只会写 Redis 状态，不会真实委托。

### 5.2 真实大 QMT adapter 连通模式

如果只想确认大 QMT adapter 可以装配，但仍然没有真实信号源，可以把策略最后一行改成：

```python
configure(mode="bigqmt", account_id=ACCOUNT_ID or "dryrun")
```

注意：当前没有配置真实 `SignalSource`，所以即使是 `mode="bigqmt"`，也不会产生订单。它只会装配行情、持仓、委托 adapter，用于确认 QMT 环境里这些函数可用。

### 5.3 真正实盘委托前置条件

只有满足下面全部条件，才能考虑切到实盘交易：

- 已在 Redis db5 上验证 Redis Stream 信号源、`StateStore.claim()`、状态回写都正常。
- 已用模拟信号验证 `passorder` 参数、持仓查询、撤单查询全部正常。
- 已确认历史 bar 不会触发下单。
- 已确认 miniQMT 不再对同一账户重复真实下单，避免双系统抢单。
- 已确认 dry-run 没有 ack 掉真实账号待实盘处理的信号。

未满足这些条件时，不要切到“实盘交易”。

## 6. 今日开盘观察清单

日期：2026-07-01

### 9:10 前

- 确认 QMT 已登录。
- 确认文件已部署到 `<QMT_PYTHON_DIR>`。
- 在策略编辑器里编译成功。
- 确认“启动本地 python”未勾选。
- 在模型交易页面用“模拟信号”启动 `bigqmt_signal_trader_dryrun.py` 或 `bigqmt_signal_trader_redis_dryrun.py`。

### 9:30 到 9:35

- 看输出窗是否出现 `init ok` 和 `adjust ok`。
- 看 `XtClient_Formula_20260701.log` 是否有 traceback。
- 看策略是否持续运行，没有自动结束。
- 看委托页面确认没有真实委托。
- 如果跑 Redis dry-run，向 `bigqmt:signals:bigqmt_probe` 写一条测试信号，确认状态 key 变成 `DRY_RUN`。

### 9:35 后

如果模拟信号稳定：

- 可以把标的周期从 `1分钟` 调整到实际希望的触发周期。
- 继续保持 dry-run 观察一段时间。
- 不要直接改成实盘，除非真实信号源和状态存储已经接好。

## 7. 日志排查

QMT 公式日志：

```text
<QMT_USERDATA_LOG_DIR>\XtClient_Formula_YYYYMMDD.log
```

重点搜索：

```text
bigqmt_signal_trader
Traceback
ModuleNotFoundError
SyntaxError
run script failed
passorder
```

常见问题：

| 现象 | 原因 | 处理 |
|---|---|---|
| 只开始运行/结束运行，没有回调日志 | 勾选了启动本地 python，或没有进入模型交易回调模式 | 取消勾选，使用模型交易运行 |
| `No module named dataclasses` | QMT 内置 Python 版本低，不能依赖 dataclasses | 当前代码已移除 dataclasses，重新部署并清 `__pycache__` |
| `__file__ is not defined` | QMT 编辑器脚本没有 `__file__` | 策略入口不要依赖 `__file__` |
| 编码错误 | 编辑器保存编码和 `coding` 声明不一致 | 入口脚本用 `#coding:gbk`，内容保持 ASCII |
| 启动后处理很多历史 bar | 未过滤历史 K 线 | 当前 `adjust` 已用 `is_last_bar()` 保护 |

## 8. 当前不能误解的点

- 当前包不是 `qmt_jq_trade` 的目标持仓同步脚本。
- 当前包的设计是“外部系统产出逐笔交易信号，大 QMT 只执行”。
- Redis db5 链路已经具备，但当前安全入口默认使用 `bigqmt_probe` 测试账号流。
- 编辑器里点“运行”不是实盘验证。
- 真正实盘必须走模型交易页面，并且要显式接入信号源、幂等状态和账户切换流程。

## 9. 官方文档参考

- ThinkTrader 大 QMT 接口文档：`https://dict.thinktrader.net/innerApi/interface_operation.html`
- 大 QMT Python API 文档：`https://qmt.ptradeapi.com/QMT_Python_API_Doc.html`
- 本项目参考脚本：`<REPO_ROOT>\src\api\qmt_jq_trade`

