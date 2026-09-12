# 交割单改造 · 验收清单

> 规则：Mock / 回放只能作为交付前自测，**不得写入本表的验收结论**。
> 它能证明解析、落库、合并、导出正确，证明不了 QMT 真的提供了 `traded_time`。
> 上一轮「162 笔买入流水缺失」正是自测只证明了"脚本能跑"、没证明"数据源给了什么"。

最后更新：2026-09-12

---

## 一、实施进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0.1 | 迁移基础设施（`db_migrate.py`：幂等补列、备份、测试库守卫、账号发现） | ✅ 完成并测试 |
| 0.2 | `position_snapshot` 表 + 09:25/15:05 快照写入 | ✅ 完成并测试 |
| 0.3 | `account_equity_daily` 表 + 恒等式校验 + 跳变告警 | ✅ 完成并测试 |
| 0.4 | 交易日历（`stock_daily_data` 反推，避长假误报） | ✅ 完成并测试 |
| 0.x | 线程接入 `main.py` / `premarket_sync.py` / 心跳采样 | ✅ 完成，**待实盘验证** |
| 1.1 | `trade_records` 扩展（17 列） | ✅ 代码完成，**未在生产库执行** |
| 1.2 | 占位流水归档 + 重复行标记 + 唯一索引 | ✅ 代码完成，**未在生产库执行** |
| 1.3a | 统一 helper `settlement_db.record_trade()`（INSERT OR IGNORE + 降级兜底） | ✅ 完成并测试 |
| 1.3b | `deal_time` / `time_source` 落库（含 `traded_time` 三态解析） | ✅ 完成并测试 |
| 1.3c | `strategy_label` / `account` / `commission_source` / `trade_id_source` 落库 | ✅ 完成并测试 |
| 1.3d | 网格成交走 helper；对账补记标 `reconcile_backfill` | ✅ 完成并测试 |
| 1.3e | 模拟成交隔离到 `trade_records_sim` | ✅ 完成并测试 |
| 1.4 | 回调重复投递 / 共享连接竞态 | 🟡 **不做**（用户决定）：落库已原子化，重复投递不再产生脏数据 |
| 2.x | `orders` 表 / 废除占位流水 / 补漏口 / 三方对账 / `run_events` 接入 | 🟡 **不做**（用户决定）：占位流水非 critical，暂缓 |
| 3.1 | 券商对账单导入（`broker_import.py` + `broker_deals`/`broker_orders`） | ✅ 完成并测试 |
| 3.2 | 历史回填（account/strategy_label/time_source/commission，幂等） | ✅ 完成并测试 |
| 4.x | 导出脚本重写（只读、不依赖日志、merge_deals 单点、缺快照报错退出） | ✅ 完成并测试 |

---

## 二、验收项

### 验收 3（最早可验）

| 项 | 内容 |
|---|---|
| 状态 | **pending** |
| 依赖 | 一、二阶段全部完成 + 历史回填 |
| 命令 | `python scripts/export_settlement.py --start <起> --end <止> --accounts all --out export/` |
| 判据 | `export_report.txt` 中「逐只股数闭合」不平股票数 = 0 |

### 验收 5（最早可验）

| 项 | 内容 |
|---|---|
| 状态 | **pending** |
| 依赖 | 验收 3 |
| 命令 | 同区间连跑两次，比对 `sha256sum export/*.csv` |
| 判据 | 两次 sha256 完全一致 |

### 验收 4（受限 —— 需补导对账单）

| 项 | 内容 |
|---|---|
| 状态 | **blocked** |
| 已核实的缺口 | 2026-07-09 ~ 2026-09-11（共 19 个交易日有成交） |
| 本次对账单覆盖 | **仅 2026-09-11 一天** |
| 需要补导 | 券商对账单覆盖 **2026-07-09 ~ 2026-09-10** |

**实测结果**（导入 09-11 对账单 + 历史回填后，全量 2026-01-01~2026-09-11 导出）：

| 指标 | 迁移前 | 迁移+回填+导入后 |
|---|---|---|
| 有真实成交的股票数 | 51 | **17** |
| 轧差为负（买入流水缺失） | 38 | **4** |
| 首笔不是 BUY 的股票 | 30+ | **2** |

> 股票数从 51 降到 17，是因为迁移归档了 75 条 `ORDER_` 占位**假成交** ——
> 多数"股票"本来就只有占位行、没有真实成交。

**仍缺买入的 4 只**（全部落在对账单未覆盖的 7~8 月）：

| 代码 | 名称 | 轧差 | 涉及日期 |
|---|---|---|---|
| 603466 | 风语筑 | −1700 | 2026-07-09 |
| 002440 | 闰土股份 | −6400 | 2026-07-27 |
| 000799 | 酒鬼酒 | −1900 | 2026-07-31 起 |
| 301218 | 华是科技 | −1000 | 2026-08-13 起 |

**结论**：验收 4 不是代码问题，是**数据覆盖问题** —— 补导 7/9~9/10 的对账单后即可闭合
（导入脚本已就绪，直接重跑即可）。

### 验收 1

| 项 | 内容 |
|---|---|
| 状态 | **pending** |
| 最早可验 | 2026-09-14 收盘后 |
| 命令 | `sqlite3 data_25105132/trading.db "SELECT time_source, account, strategy_label, commission_source FROM trade_records WHERE deal_time_str >= '<当日>'"` |
| 判据 | 新成交 `time_source='exchange'`；`account`/`strategy_label`/`commission_source` 非空 |

### 验收 2

| 项 | 内容 |
|---|---|
| 状态 | **pending** |
| 最早可验 | 2026-09-18 收盘后（需连续 5 个交易日：9/14、15、16、17、18） |
| 命令 | `sqlite3 data_25105132/trading.db "SELECT snapshot_date, snapshot_type, COUNT(*) FROM position_snapshot GROUP BY 1,2 ORDER BY 1"` |
| 判据 | 每交易日均 open + close 两行；`account_equity_daily` 每日 `total_asset > 0`；恒等式校验通过 |

### 验收 6

| 项 | 内容 |
|---|---|
| 状态 | **pending（条件受限）** |
| 约束 | **禁止盘中在持仓或委托存在时断连** —— 那是拿真金白银做测试 |
| 优先顺序 | ① 测试库 + 测试账号演练 → ② 15:00 收盘后、确认无未完成委托时断开 2 分钟 → ③ 两者都不可得则标 `待条件允许` |
| 判据 | `run_events` 出现 `qmt_disconnect` / `qmt_reconnect`，且演练期间无真实成交 |

### 验收 7（人为失配演练）

| 项 | 内容 |
|---|---|
| 状态 | **pending** |
| 依赖 | 阶段 2 的对账机制 |
| 命令 | 测试库内临时把持仓表设为只读后成交一笔 |
| 判据 | `run_events` 出现 `position_mismatch` 且内容准确，系统未静默修正 |

### 验收 8（出入金跳变）

| 项 | 内容 |
|---|---|
| 状态 | **pending** |
| 命令 | 人为转入 1 万元后观察当日 close 快照 |
| 判据 | `suspected_flow` 或 `unexplained_delta` 标出（**允许误报，不允许漏报**） |

---

## 三、交付前自测（不作为验收证据）

```
python -m unittest test.test_settlement_db test.test_grid_deal_time_source   # 临时库
python test/run_integration_regression_tests.py --group settlement_export    # 96 用例
python test/run_integration_regression_tests.py --fast                       # 1054 用例
python db_migrate.py --accounts all --dry-run                                # 迁移预演
```

最近一次结果（2026-09-12）：

| 命令 | 结果 |
|---|---|
| `--group settlement_export` | 168 / 168 通过 |
| `--fast` 回归 | 1150 / 1150 通过（100%） |
| `--all-with-fast` **完整集成回归** | **36 组 / 2994 用例 / 100% 通过** |
| 迁移 + 回填 + 导入 + 导出 真实数据演练 | 全部成功 |

新增测试模块（已登记进 `test/integration_test_config.json`）：

| 模块 | 覆盖 |
|---|---|
| `test/test_settlement_db.py` | 快照 / 净值 / run_events / 交易日历 / 成交统一写入口 / 模拟隔离 / 迁移守卫 |
| `test/test_grid_deal_time_source.py` | 网格 time_source 透传（exchange / local_fallback / reconcile_backfill） |
| `test/test_export_settlement.py` | 合并规则 / 14 列结构 / 缺快照报错 / 幂等 sha256 |
| `test/test_broker_import.py` | GBK 解析 / 三级匹配 / 回填决策 / 未匹配不硬凑 |
| `test/test_backfill_trade_records.py` | 手续费来源判定 / 幂等 / dry-run / 不伪装 exchange |
| `test/test_data_pipeline_e2e.py` | **Mock 全链路**：成交→落库→快照→对账单导入→导出，逐只股数闭合=0 |

### 券商对账单实测（2026-09-12）

导入 `E:\...\QMT导出\25105132_2_*.csv`（**GBK 编码**）：

| 文件 | 数据 | 说明 |
|---|---|---|
| `deals.csv` | **13 笔** | 回填主力，含真实成交编号与交易所成交时间 |
| `orders.csv` | 63 笔 | 委托（含状态/废单原因），存入 `broker_orders` |
| `positions.csv` | 7 只 | 券商侧持仓，用于交叉核对 |
| `stkDelivery.csv` | **0 条** | 只有表头 —— 交割单无数据 |
| `stkFundFlow.csv` | **0 条** | 只有表头 —— **出入金仍无数据源** |
| `account.csv` | 1 行 | 总资产=0.00，`交易日=18989999` 为无效值，不可用 |

**匹配结果：13 / 13 全部命中**

| 匹配方式 | 笔数 | 说明 |
|---|---|---|
| `traded_id`（成交编号 == trade_id） | 11 | 一级匹配 |
| `order_id+code`（订单号 + 代码消歧） | 2 | order_id 跨股复用，必须带代码才能选对 |

**回填实效**（例）：`id=797` 本地记录 `13:00:02`，**对账单真实成交时间 `13:00:00`** —— 2 秒误差被修正。

**一处保守取舍**：对账单的 `手续费` 全为 `0.00`，脚本**不覆盖**本地估算值
（对账单里 0.00 通常表示"该字段未导出"而非"真的免费"），因此 `commission_source`
保持 `estimated`。若后续导出带真实手续费，重跑导入即自动升级为 `broker`。

---

## 四、迁移预演结果（在只读副本上跑）

`python scripts/migrate_settlement.py --accounts all --dry-run`：

| 账号库 | 现有行数 | account 回填 | 占位流水归档 | 标记重复 | 迁移后行数 |
|---|---|---|---|---|---|
| 25105132 | 280 | 280 | 75 | 2 组 | 205 |
| 25106531 | 18 | 18 | 3 | 0 组 | 15 |
| data（默认库） | 25 | 0（路径推不出账号） | 25 | 0 组 | 0 |

**待标记的 2 组重复行**（同一条 deal 被写两遍，`grid_*` 表对这 4 行无引用）：

| 保留 | 标记 superseded | trade_id | 代码 |
|---|---|---|---|
| id=627 | id=628 | `74500104000054860480` | 300454 深信服 |
| id=631 | id=632 | `74640105000030409438` | 300454 深信服 |

根因（2026-09-12 查证，含一处自我更正）：

1. **成交被底层投递了两次**。`成交回报` 这行日志位于 [easy_qmt_trader.py:137](easy_qmt_trader.py#L137)
   的 `on_stock_trade` **内部、转发循环之前**，同一毫秒出现两行，说明 `on_stock_trade`
   本身被推了两次 —— 不是同一个回调列表里塞了多个（那里已有 `_append_unique_callback` 去重）。

2. **`detach()` 刻意保留 `trade_callbacks`**，其文档字符串写明理由：
   "落库层按 trade_id 幂等去重，重复投递无害"。**这个假设从未成立** ——
   当时既没有唯一索引，判重又是 `SELECT` 后再 `INSERT`，存在竞态窗口。

3. **未能完全钉死的部分**：两个线程同时进入 `with record_lock:` 临界区
   （582ms 与 584ms 都打印了锁内日志），而锁在 `__init__` 就建好、TradingExecutor
   又是单例，按常理不应发生。**这一层我没有查清，不编造结论。**

**已采取的修法不依赖解开第 3 点**：把落库从 `check-then-insert` 改为
`INSERT OR IGNORE` + 唯一索引，这是**原子**的，任何锁/线程/进程组合下都不可能插入两行。

**尚未修的**：投递层的重复（第 1、2 点）。落库已幂等，重复投递不再产生脏数据，
但会浪费一次处理。彻底的修法是让旧 callback 在 `detach` 时停止转发成交，
或确保重连后旧 `XtQuantTrader` 被回收 —— 属阶段 1.4 剩余部分。

---

## 五、必须记录的事件

### 2026-09-12 · dry-run 误写生产库

**经过**：`scripts/migrate_settlement.py --dry-run` 在 dry-run 分支之前先调用了
`migrate_settlement_schema()`，而该函数当时不是 dry-run 感知的，于是在 3 个生产库上
各创建了 3 张空表（`position_snapshot` / `account_equity_daily` / `run_events`）。

**影响范围**：仅新增 3 张**空表**。

| 检查项 | 结果 |
|---|---|
| `trade_records` 列 | 未改动 |
| `trade_records` 行数 | 280 / 18 / 25，未变 |
| 既有表数据 | 未改动 |
| 运行中的进程（PID 21800） | 未受影响（跑的是改动前的代码，不引用 settlement_db） |
| 新建表行数 | 均为 0 |

**根因**：`CREATE TABLE` 与 `ALTER TABLE` 一样在 SQLite 里是隐式提交的 DDL，
靠 `rollback()` 撤不回来。dry-run 必须走**临时副本**，不能靠"跳过写操作"。

**修复**：`migrate_settlement_schema()` 增加 `dry_run` 参数，走 `tempfile` 副本；
回归测试 `test_schema_migration_dry_run_does_not_create_tables` 锁死该行为。

**遗留**：3 张空表仍在。它们是纯增量、幂等的（代码统一用 `CREATE TABLE IF NOT EXISTS`），
后续正式迁移会直接复用，不会产生冲突。如需恢复原状需执行 `DROP TABLE`——
**该决定权归用户**，未擅自执行。

---

## 七、复核问题处置（2026-09-12 第二轮）

### P0-1 · deal 唯一键

**核查结论**：短 id 的 15 组重复 **100% 跨标的**（9 位 9/9、10 位 6/6），确认是
`order_id` 跨股跨日复用产生的**假重复**；真重复只有 2 组，均为 20 位长 id
（627/628、631/632），已标记 superseded。**当前索引键下冲突组 = 0，未发生静默丢单**
（行数也能精确对上：280 − 75 归档 = 205）。

**索引改为**：
```sql
UNIQUE(COALESCE(account,''), COALESCE(order_id,''), stock_code, trade_type,
       trade_id, COALESCE(deal_time, epoch(trade_time), 0), volume, price)
WHERE trade_id IS NOT NULL AND row_status='active'
```
四处关键设计（每一处都是踩过的坑）：
1. **不能只按 (account, trade_id)** —— 短 id 跨标的复用会互相冲突
2. **trade_type 入键** —— 同 id 同股同日但方向不同是两笔成交
3. **时间分量 NULL 安全且只用成交自身属性** —— 曾用 `recorded_at`（落库时刻）兜底，
   那是错的：同一秒落库的两笔**不同**成交会撞键被静默丢弃
4. **volume/price 入键** —— 同委托的分笔成交价量不同，必须都保留

**防丢单**：`INSERT OR IGNORE` 的 rowcount=0 不再直接判为重复，而是**比对冲突行**；
内容不同则落 `run_events(deal_key_collision)` 并强制写入（trade_id 加后缀，
原编号保留在 `fill_ids`）。实测：重复投递被拒、价量方向不同的三笔全部保留。

**order_id 落库**：回填 56 行（网格路径 `trade_id` 本就是 `order_id`）；
149 行真实成交编号无 order_id 可用，正确留空。

### P0-2 · 文件名与区间标注

- 文件名改用**实际首末成交日期**：`trading_events_20260709_20260911.csv`
- 报告新增「实际首末成交」，并在请求起点早于首笔成交时显式说明
- 报告新增「逐只缺口归因」。**期初快照缺失时如实写"无法判定"** ——
  负缺口既可能是期初持仓也可能是买入缺失，没有快照就无法区分，不假装知道
- 报告新增「券商对账单匹配情况」：未匹配清单 + 按可操作性分类的原因分布

### P0-3 · account_daily 拒写无效读数

- `total_asset <= 0` 或全零读数一律拒写，落 `run_events(asset_write_failed, reason=invalid_asset_reading)`
- `snapshot_type` 收敛为 **open/close 两种**，`intraday` 已删除（心跳采样入口同步移除）
- 已删除库中残留的 9-12 无效行（先备份到 `data/backup/migrations/`）

> ⚠️ 运行中的进程仍是旧代码，重启前心跳仍会写 intraday 行（已实测复现一次）。

### P1/P2

| 项 | 处置 |
|---|---|
| P1-4 changelog | 新增 `changelog_since_last_export.txt`，逐行列出新增/修改/删除 + id + 字段 + 旧值→新值。实测能捕获 `commission: 18.941 → 9.99` |
| P1-5 commission_source | 实打实打印分布（source / 行数 / 金额合计），不再写"见数据库统计" |
| P1-6 账号脱敏 | `账户A(***5132)` 稳定映射（按账号排序编号，跨交付不变），报告附映射表 |
| P1-7 账户B | `--accounts all` 输出到独立子目录 `export/<account>/` |
| P2-8 诊断列 | CSV 追加 `time_source` / `order_id` / `row_status`（14 列契约不动，追加在后） |
| P2-9 positions | `positions_begin.csv` 补 `cost_price` 列；无快照写 `BLOCKER: no snapshot before <start>` 且退出码 2；`positions_end` 优先取**区间内**快照 |

### 300454 金额变化的说明（P1-4 要求）

**买入金额合计 134,420 → 73,320**，差额 61,100 恰为 id=628 的金额：

| 行 | 金额 | 处置 |
|---|---|---|
| id=627 | 61,100 | 保留（active） |
| id=628 | 61,100 | 与 627 是**同一条 deal 被写两遍** → 标 superseded |
| id=629 | 12,220 | 保留 |

排除重复行后：61,100 + 12,220 = **73,320**。这正是本次改造要根治的问题。

### 尚未完成（依赖用户操作）

- **对账单覆盖范围**：当前只覆盖 2026-09-11。要补齐缺口需导入 **2026-01-01 起** 的对账单
- **position_snapshot**：库中目前无任何快照，所有导出都返回退出码 2（预期行为）。9/14 起自然积累

---

## 八、关键设计决定与实测依据

| # | 规格原文 | 实测事实 | 处置 |
|---|---|---|---|
| 1 | 在 `_on_deal_callback` 写 deal_time | 该函数是**死代码**（`xtquant.xttrader` 模块无 `register_callback`） | 落点改到 `position_manager._on_trade_callback` |
| 2 | `ALTER TABLE ... ADD COLUMN commission` | 该列**已存在** | 迁移逐列探测跳过 |
| 3 | `UNIQUE(account, trade_id)` | 建不出来 | 索引降级为 `(COALESCE(account,''), trade_id, stock_code, trade_time)` |
| 4 | 成交 + 持仓同事务 | 两库均 **WAL**，持仓权威副本在 `:memory:` | 降级为同库事务 + 跨库对账 |
| 5 | `traded_time` 直接可用 | 编码三态（epoch 秒/毫秒、`yyyymmddHHMMSS`、`HHMMSS`） | 复用 `_order_time_to_timestamp` |
| 6 | equity 表含 deposit/withdraw | QMT **无任何出入金接口**；`cash` 与 `available` 是同一个数 | 建列默认 NULL + 恒等式校验 + 跳变告警 |

### trade_id 形态分类（决定唯一索引设计）

| 形态 | 行数 | 重复组 | 跨股票 | 性质 |
|---|---|---|---|---|
| `ORDER_` 前缀（占位流水） | 75 | 19 | 18/19 | order_id 跨日复用 |
| 短数字 9-10 位（**100% 是 grid**） | 58 | 15 | 15/15 | 网格写的是 `str(order_id)` 而非成交号 |
| 长数字 20 位（真实 traded_id） | 147 | 2 | 0/2 | **真重复**，仅 627/628、631/632 |

⚠️ 废除占位流水只能消掉 19 组；**那 15 组网格的不会消失**，且仍在持续产生。
因此唯一索引不能只按 `(account, trade_id)`。

### SQLite NULL 陷阱

SQLite 在唯一索引中把 **NULL 视为互不相等**（即使 NULL 对 NULL）。
历史行 `account` 全为 NULL，若索引直接写 `account`，整个索引对历史数据**形同虚设**——
看着建成了，却一条重复都拦不住。索引用 `COALESCE(account,'')` 包起来堵死该洞，
并配合 `backfill_account` 回填（回填保证导出可用，COALESCE 保证索引无死角）。

---

## 九、上线顺序（强依赖）

```
1. 停止 miniQMT（tasklist | findstr python 确认无残留）
2. python scripts/migrate_settlement.py --accounts all --dry-run   # 先看会改什么
3. python scripts/migrate_settlement.py --accounts all             # 正式迁移
4. 重启 miniQMT，确认日志出现「启动收盘快照线程」
5. 2026-09-14 09:25 验证首份 open 快照
```

**顺序不可颠倒**：占位流水未清理就建唯一索引 → `IntegrityError`。
