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

## 数据库维护与日志轮转

主程序启动时会根据 `ENABLE_DB_MAINTENANCE` 启动数据库维护线程。默认每天 `00:10:00` 附近执行一次，且 `DB_MAINTENANCE_REQUIRE_NON_TRADE_TIME = True` 时只在非交易时段运行，避免盘中对 SQLite 做重维护。

维护内容：

- 清理 `trade_records`、非 active 的 `grid_trading_sessions`、`premarket_sync_history`、`config_history` 等追加型历史表
- 清理自动买入复盘库 `data/autobuy.db` 中过期的 `decision_log`
- 删除行数达到 `DB_MAINTENANCE_VACUUM_MIN_DELETED_ROWS` 且 `DB_MAINTENANCE_ENABLE_VACUUM = True` 时执行 `VACUUM`
- 按 `XQM_LOG_MAX_SIZE` / `XQM_LOG_BACKUP_COUNT` 轮转 `logs/xqm_manager.log`

保留策略集中在 `config.py`：

```python
TRADE_RECORD_RETENTION_DAYS = 1095
GRID_SESSION_RETENTION_DAYS = 365
AUTOBUY_DECISION_LOG_RETENTION_DAYS = 90
PREMARKET_HISTORY_RETENTION_DAYS = 365
CONFIG_HISTORY_RETENTION_DAYS = 365
```

如需临时手动执行，可在确认非交易时段后运行：

```python
from maintenance import run_database_maintenance, rotate_xqm_log

rotate_xqm_log()
run_database_maintenance()
```

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
