# 无人值守运行

## 概述

miniQMT 支持长期持续运行，通过线程健康监控实现自动恢复，配合超时保护和非交易时段优化，适合 7x24 小时无人值守部署。

---

## 线程自愈机制

`ThreadHealthMonitor`（[thread_monitor.py](https://github.com/weihong-su/miniQMT/blob/main/thread_monitor.py)）每 60 秒检查所有注册线程的存活状态。

**工作流程**：

```
每 60 秒:
  遍历所有注册线程:
    获取线程对象（通过 lambda）
    如果线程不存活:
      记录日志
      调用 restart_func()
      记录重启历史
      进入 60 秒冷却期
```

### 线程注册规范

```python
# main.py 中的正确注册方式
thread_monitor = get_thread_monitor()

thread_monitor.register_thread(
    "持仓监控",
    lambda: position_manager.monitor_thread,  # lambda 获取最新引用
    position_manager.start_position_monitor_thread,
)
```

!!! danger "常见错误"
    ```python
    # 错误: 直接传递线程对象，重启后引用失效
    monitor.register_thread(
        "持仓监控",
        position_manager.monitor_thread,  # ❌ 错误
        restart_func,
    )
    ```

---

## 超时保护

持仓监控线程中的 API 调用有超时保护（当前默认 8 秒）：

```python
try:
    future.result(timeout=config.MONITOR_CALL_TIMEOUT)  # 默认 8 秒
except TimeoutError:
    logger.warning("API 调用超时，跳过本次更新")
```

超时不阻塞主循环，下一次循环继续尝试。

### xttrader 连接与清理超时

重连路径上的 QMT 底层调用同样有超时兜底。`easy_qmt_trader._stop_trader_with_timeout()` 把 `xt_trader.stop()` 放进 daemon 线程执行，超过 `QMT_STOP_TIMEOUT`（默认 5 秒）就放弃等待并继续重连：

```python
stop_thread = threading.Thread(target=_do_stop, daemon=True, name='qmt_stop_worker')
stop_thread.start()
stop_thread.join(timeout)          # config.QMT_STOP_TIMEOUT
if stop_thread.is_alive():
    logger.warning(f'停止{label}超时({timeout}秒)，跳过等待并继续重连')
```

`connect()` 的四条清理路径（清理旧实例、连接超时、连接异常、连接失败）全部走这个统一入口。**背景**：QMT 在处理外部成交回报时，`stop()` 可能与底层回调线程互相等待而永久阻塞，此前会把整个重连线程拖死，Fail-Safe 自愈随之失效。

连接本身由 `QMT_CONNECT_TIMEOUT`（默认 30 秒）保护，超时后中止本次连接并清理残留实例，避免遗留后台线程。

### 超时线程无法回收（可观测，非可修复）  [v3.8.6]

`run_with_timeout()` 超时后**无法真正终止底层线程**：`future.cancel()` 只能取消尚未开始的任务，`cancel_futures` 也不中断运行中的线程，而 Python 没有强制杀死线程的机制。若 QMT 长期无响应，每次超时都会泄漏一个线程，无人值守长跑会持续累积。

这一点不作「已修复」的假象处理，改为让它**可观测**：

```python
from timeout_utils import get_leaked_call_count
get_leaked_call_count()    # 累计泄漏的超时调用数
```

泄漏首次发生以及此后每 10 次会输出一条 `[TIMEOUT_LEAK]` WARNING。**若该数值持续增长，说明底层（通常是 QMT）调用长期卡死，需人工检查 QMT 客户端状态**——它是排查「系统看似在跑但数据不更新」的关键信号。

### 重连瞬间的状态一致性  [v3.8.6]

重连要销毁旧 `XtQuantTrader` 再建新实例，这几百毫秒是状态最易错乱的窗口，有三处收口：

- **旧 callback 失效**：`connect()` 每次都创建全新 callback，但旧 trader 仍持有旧 callback。若 `stop()` 超时（daemon 线程杀不掉），旧 trader 连同回调继续存活，其延迟触发的 `on_disconnected` 会把新连接刚设好的 `qmt_connected` 错误置回 `False` 并清零重连冷却，引发本可避免的 stop/connect 周期（每次都有再卡死的风险，可级联）。故在 **stop 之前**调用 `callback.detach()` 尽早关窗。
    - 刻意**只切连接状态类推送**（`on_disconnected` / `on_stock_order`），保留 `on_stock_trade` 转发：成交回报是真实资金变动，迟到仍有价值，且落库层按 `trade_id` 幂等去重，一并拦掉反而可能永久丢失一笔成交流水。
- **重连后强制刷新持仓**：成功分支置零 `last_position_update_time`，否则最长 `QMT_POSITION_QUERY_INTERVAL`（10 秒）内继续用断连前的持仓快照；断连期间若有外部成交，止盈止损会基于错误持仓判断。
- **QMT 自恢复探测**：QMT 进程自动重启后 `position()` 已能返回真实数据，但 `qmt_connected` 仍为 `False`。`_probe_qmt_recovered()` 用 `ping_xttrader()`（真实 `query_stock_asset` 探针）确认后直接自恢复，省去一次冗余重连。**不以「持仓查询返回空」为依据**——`position()` 断连时同样返回空 DataFrame，与「真的没有持仓」无法区分，据此自恢复会造成假健康。重连进行中 / 模拟模式 / 网关模式一律不探测。

---

## 非交易时段优化

```python
if not config.is_trade_time():
    time.sleep(60)  # 非交易时段每分钟检查一次
    continue
```

**效果**：非交易时段 CPU 占用从 ~30% 降至 <2%。

---

## 心跳日志

每 30 分钟输出一次系统运行状态（`ENABLE_HEARTBEAT_LOG = True`），包含：

- 各线程存活状态
- 持仓数量和总市值
- 最近的交易活动
- QMT 连接状态
- **进程资源占用**：`线程数:18(OS 104) | 句柄:1037 | 内存:RSS 195MB / VMS 182MB`

### 怎么读资源指标  [v3.9.1]

| 现象 | 判读 |
|------|------|
| OS 线程数单调上升 | 原生线程泄漏（xtquant/QMT SDK 侧），最终以 `can't start new thread` 收场 |
| 句柄数单调上升 | 句柄泄漏，表现为打开文件失败（`[Errno 22]`） |
| VMS（私有提交）持续增长 | 真实内存泄漏 |
| 仅 RSS 上升、VMS 平稳 | **不是泄漏**，是 Windows 工作集驻留，系统会自行 trim |

- **看 OS 口径，不要看 Python 口径**：`threading.active_count()` 只覆盖 Python 层，
  xtquant 的原生线程完全不可见。实测同一进程 Python 报 18、OS 实际 104，
  差的 86 条正是最可能泄漏的部分。
- **RSS 会骗人**：实测收盘后 RSS 从 195MB 跌到 31MB，而私有提交始终 182MB。
  判断泄漏一律以 VMS 为准。

---

## 终端刷屏导致的进程级故障  [v3.9.1]

这是一类**外部原因造成的自身死亡**，`thread_monitor` 完全兜不住（它只能重启线程，
不能重启进程），务必了解。

### 事故链（2026-09-09 实盘）

```
13:43  某股因摊薄成本导致止损价失真，剩余持仓又是 T+1 冻结(available=0)
       → 形成持续 77 分钟、每轮轮询都命中的稳定状态，日志刷出约 3800 行
       ＋ spinner 每 0.25 秒写一次 stdout（138 小时累计约 199 万次）
15:24  Windows 事件 ID 2004：虚拟内存不足，WindowsTerminal.exe 占用 27.8GB
16:24  同上，第三次
16:25  miniQMT: RuntimeError: can't start new thread → 静默死亡 3 小时
```

**根因不在本项目**：Windows Terminal 在高频写入下会无限泄漏内存
（[microsoft/terminal#8283](https://github.com/microsoft/terminal/issues/8283)
记录了同型模式——每 10ms 回移光标打印，内存以 0.1MB/10s 增长；
[#768](https://github.com/microsoft/terminal/issues/768) 指出持续刷屏的应用触发最激进的增长）。
它吃满系统提交上限后，本进程连 1MB 线程栈都提交不到。

### 三层防御

| 层 | 手段 | 覆盖范围 | 配置 |
|----|------|---------|------|
| L1 | spinner 降频 | 写 stdout 的**最大单一来源** | `SPINNER_INTERVAL = 1.0` |
| L2 | 按 key 节流日志 | **已知**的持续性状态刷屏 | `LOG_THROTTLE_INTERVAL = 300` |
| L3 | 控制台令牌桶限速 | **未预见**的突发刷屏（无差别兜底） | `CONSOLE_LOG_RATE` / `CONSOLE_LOG_BURST` |

**L3 只作用于控制台，文件日志始终完整** —— 诊断能力零损失。被抑制时会输出
`[控制台限速] 已抑制 N 条控制台输出，完整日志见 ...`，不会让人误以为程序卡死。

默认参数按实测流量选定：稳态日志仅 0.01~0.08 行/秒，启动瞬间峰值约 140 行/秒，
因此 `CONSOLE_LOG_BURST = 300` 容得下启动峰值，`CONSOLE_LOG_RATE = 20` 只在
异常刷屏时才会截断。设 `CONSOLE_LOG_RATE = 0` 可关闭限速。

### 进一步降低风险（可选）

- **换用 conhost**：`conhost.exe python main.py` 绕开 Windows Terminal，
  从根本上避开其泄漏 bug。
- **限制终端回滚缓冲**：Windows Terminal `settings.json` 的
  `profiles.defaults.historySize`（默认 9001 行）。注意这只减缓不根治——
  27.8GB 远超 9001 行文本的体量，泄漏与写入**次数**相关而非字节数。
- **定期重启**：138 小时不重启已被这次事故证伪，建议每日盘后重启一次进程与终端。

---

## 盘前自动初始化

每日 9:25 自动重新初始化 xtquant 连接（`ENABLE_PREMARKET_XTQUANT_REINIT = True`），确保交易日开盘前连接就绪。

---

## 数据库维护与运行产物保留 {#runtime-retention}

主程序根据 `ENABLE_DB_MAINTENANCE` 启动维护线程，默认每天 `00:10:00` 后执行一次；错过目标时间会补执行，且默认仅在非交易时段运行。参数统一在 `config.py`，详见[配置参考](configuration.md#runtime-maintenance)。

| 对象 | 默认规则 |
|------|----------|
| 实盘 `trade_records` | 保留 1095 天 |
| 已结束网格会话 | 非 active 会话保留 365 天，按外键级联处理关联记录；活跃与暂停中的 active 会话保留 |
| `run_events` | 在线 365 天，按月写入 `data_<账号>/archive/run_events/`；核对归档内容成功后才删除在线记录，归档按事件月份保留 1095 天 |
| 资产待复核事件 | 仍关联待复核净值的资产事件暂缓归档，保留排查证据 |
| `trade_records_sim` | 保留 90 天 |
| 自动买入 `decision_log` | 保留 90 天 |
| 自动买入 `buy_history` | 失败或模拟尝试保留 90 天；成功实盘记录继续用于永久防重 |
| 盘前同步 / 配置变更历史 | 保留 365 天 |
| 回归备份 | 每个源库最新 10 份有效备份，覆盖默认库与账号库；按文件名的生成时间排序 |
| 已完成测试任务 | 完整工作副本最多 5 次且不超过 30 天；报告和必要复现证据保留 180 天 |

回归备份使用 SQLite `Connection.backup()`，包含 WAL 中已提交的记录；新备份校验成功后才淘汰旧备份。备份失败时回归运行器中止后续清理和测试。不要只复制正在使用的 `.db` 文件，也不要手动删除运行数据库的 `-wal` / `-shm`。

测试产物仅处理配置白名单前缀的任务目录，要求存在合法 `full_regression.exitcode` 且未发现存活 PID。运行中、完成状态不明或包含链接的目录受到保护；源码工作副本先保存复现证据，原始 `source.zip` 保留供人工核实。该规则不会递归清空所有 `logs/` 子目录。

主日志按 10 MiB 大小轮转，保留 5 份备份；账号启动模式写入 `logs/account_<账号>.log`。顶层日志过期清理默认按文件修改时间保留 30 天，不逐条裁剪持续更新的日志。XtQuantManager 普通重定向日志另按 `XQM_LOG_*` 轮转。

### 手动预览与清理

从项目根目录先生成预览：

```bash
python scripts/cleanup_runtime_artifacts.py --report logs/cleanup_preview.json
```

核对清单后，在非交易时段执行：

```bash
python scripts/cleanup_runtime_artifacts.py --apply --snapshots --maintain-databases --report logs/cleanup_applied.json
```

- 默认预览不删除数据；`--apply` 执行产物清理，`--snapshots` 先创建一致性备份，`--maintain-databases` 再维护默认库、账号库和自动买入库。
- 报告包含处理路径、规则、大小、错误及数据库表的前后摘要；未成功处理的项目保留错误记录。文件逻辑大小与磁盘实际释放空间可能不同。
- 当前持仓、持仓/净值快照、行情和指标缓存不因本轮新增规则被删除；不要把网页“清空下单日志”当作该维护工具。
- 删除达到阈值且配置允许时才执行 `VACUUM`；只有数 MiB 的库通常无需单独安排压缩。

---

## 5 分钟启用清单

```python
# config.py 必改项
ENABLE_THREAD_MONITOR = True     # 线程自愈（默认已开）
ENABLE_SELL_MONITOR = True       # 卖出超时撤单（默认已开）
ENABLE_HEARTBEAT_LOG = True      # 心跳日志（默认已开）
ENABLE_DB_MAINTENANCE = True     # 数据库维护与 XtQuantManager 日志轮转（默认已开）
```

```bash
# 启动
python main.py

# 查看日志
Get-Content logs/qmt_trading.log -Wait   # PowerShell
tail -f logs/qmt_trading.log             # Git Bash
```

---

## 诊断工具

```bash
# 系统状态检查
python -m unittest test.test_system_integration -v

# QMT 连接诊断
python -m unittest test.test_qmt_connection -v
```

## 日志模块别名  [v3.9.2]

日志行格式为 `时间 [级别] miniQMT.<别名> - 消息`，别名定义见
[logger.py](https://github.com/weihong-su/miniQMT/blob/main/logger.py) 的
`MODULE_NAME_MAP`。v3.9.2 起统一为三字母（此前两字母缩写辨识度低，
`st` 策略与 `sm` 卖出监控、`mt` 维护与 `tm` 线程监控极易混淆）：

| 别名 | 模块 | 别名 | 模块 |
|------|------|------|------|
| `pos` | position_manager（持仓核心） | `web` | web_server |
| `dat` | data_manager（行情数据） | `thd` | thread_monitor |
| `tra` | trading_executor（下单执行） | `syn` | premarket_sync |
| `stg` | strategy（策略） | `cfg` | config_manager |
| `cal` | indicator_calculator（指标） | `mon` | sell_monitor |
| `gtm` | grid_trading_manager（网格） | `gdb` | grid_database |
| `gvd` | grid_validation（网格校验） | `qmt` | easy_qmt_trader |
| `stl` | settlement_db（交割单） | `mig` | db_migrate |
| `mtn` | maintenance | `main` | main |

排日志常用组合：

```bash
# 只看交易执行与持仓（Windows PowerShell）
Select-String -Path logs/account_*.log -Pattern 'miniQMT\.(tra|pos)'

# 只看错误
Select-String -Path logs/account_*.log -Pattern '\[E\]'

# 按模块统计错误分布（定位故障源头最快的一招）
Select-String -Path logs/account_*.log -Pattern '\[E\].*miniQMT\.(\w+)' |
  ForEach-Object { $_.Matches.Groups[1].Value } | Group-Object | Sort-Object Count -Descending
```

!!! tip "升级后旧日志仍是旧别名"
    `MODULE_NAME_MAP` 只影响新写入的日志行。排查 v3.9.2 之前的历史日志时，
    对照关系为：`pm`→`pos`、`dm`→`dat`、`te`→`tra`、`st`→`stg`、`ws`→`web`、
    `tm`→`thd`、`ps`→`syn`、`cm`→`cfg`、`ic`→`cal`、`sm`→`mon`、
    `qt`→`qmt`、`mt`→`mtn`。
