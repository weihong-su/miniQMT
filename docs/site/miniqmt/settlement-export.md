# 交割单数据管道

> 目标：**归因所需的一切在成交当下就写进数据库，导出只做 SELECT**，不再"抢救式重建"。

本页说明交割单相关表结构、`time_source` 语义、券商对账单导入、历史回填、
导出脚本与自检项，以及上线顺序与已知边界。

## 为什么需要它

首次尝试导出标准交割单时暴露三个硬伤，**根源都不在导出脚本，而在数据从未被记录**：

| 硬伤 | 实测证据 | 根因 |
|------|---------|------|
| `trade_time` 不是成交时间 | 全库 280 条**无一条**来自 QMT，全是本地 `datetime.now()` | 构造成交记录时逐字段取了 `traded_price`/`traded_volume`/`traded_id`，**唯独时间硬编码 `now()`** |
| `commission` 无真实值 | 非零值全部精确等于 `amount×0.0003`（纯佣金估算），2026-08 起 100% 为 0 | QMT 的 `XtTrade` 结构体**根本没有手续费字段** |
| 买入流水大面积缺失 | 51 只股票中 38 只"卖出多于买入" | 部分买入从未落库，且券商成交回报只推当日 |

结论：事后无法恢复，**必须改成成交当下写入**。

## 表结构

### trade_records（扩展 17 列）

| 字段 | 说明 |
|------|------|
| `account` | 账号标识（迁移时按库路径回填，如 `25105132`） |
| `deal_time` / `deal_time_str` | 交易所成交时间（Unix 秒 + 可读串）。取不到时**留空** |
| `time_source` | 成交时间来源，四态见下 |
| `recorded_at` | 落库时刻 |
| `order_id` | 委托编号。网格路径写 `trade_id` 的本就是它，历史行由回填补齐 |
| `fill_ids` / `fills` | 原始成交编号（分号分隔）/ 合并笔数。库里存**原始 deal 粒度**，`fills` 恒为 1 |
| `strategy_code` / `strategy_label` | 策略内部标识 / 中文枚举标签（落库时写死） |
| `is_simulation` | 是否模拟成交 |
| `commission_source` / `commission_rate` | 手续费来源（`broker`/`estimated`/`unknown`）与实际使用的费率 |
| `side_source` | 买卖方向来源（`deal`/`order`/`broker`） |
| `row_status` / `duplicate_of` | `active`/`superseded` + 指向保留行的 id |
| `trade_id_source` | `traded_id`（20 位成交编号）/ `order_id`（9-10 位）/ `placeholder`（已归档） |

`commission` 列**建表时已存在**，迁移逐列探测后跳过。

### 新增表

| 表 | 用途 |
|----|------|
| `position_snapshot` | 每交易日 09:25(`open`) / 15:05(`close`) 全量持仓快照，主键 `(account, snapshot_date, code, snapshot_type)` |
| `account_equity_daily` | 每日净值（open/close 两条）+ 恒等式校验 + 跳变标记 |
| `run_events` | 结构化运行事件（`logger.error` 的镜像 + 对账失配告警） |
| `trade_records_sim` | **模拟成交独立表**，与实盘物理隔离 |
| `broker_deals` / `broker_orders` | 券商对账单原始成交 / 委托，保留供审计 |

!!! warning "为什么持仓快照必须独立成表"
    `positions` 表是"**当前**持仓"，会被持续覆盖写，**不能当历史用**。
    对账基准只能取 `position_snapshot` —— 这也是快照必须尽早上线的原因。

## `time_source` 四态语义

| 取值 | 含义 | 何时产生 |
|------|------|---------|
| `exchange` | QMT 成交回报自带 `traded_time` | 正常实盘成交（`deal_callback`） |
| `local_fallback` | **取不到**交易所时间，`trade_time` 实为本地入库时刻 | 历史存量行 + 回报缺时间字段时 |
| `reconcile_backfill` | 启动对账补记 | 网格对账重放合成成交 |
| `broker` | 券商对账单回填的真实成交时间 | 导入对账单后 |

!!! danger "绝不把本地时间伪装成交易所时间"
    取不到成交时间时**标记 `local_fallback` 并告警**，`deal_time` 留空，
    **禁止用 `now()` 冒充**。`XtTrade.traded_time` 实测有三种编码
    （epoch 秒 / epoch 毫秒 / `yyyymmddHHMMSS` / `HHMMSS`），解析不出即判为
    `local_fallback`。

## 成交写入：单一入口

所有成交写入统一走 `settlement_db.record_trade()`，用 **`INSERT OR IGNORE` + 唯一索引**
做原子幂等——`check-then-insert` 存在竞态窗口，是历史上同一笔成交被写两遍的直接原因。

### deal 唯一键

```sql
UNIQUE(COALESCE(account,''), COALESCE(order_id,''), stock_code, trade_type,
       trade_id, COALESCE(deal_time, epoch(trade_time), 0), volume, price)
WHERE trade_id IS NOT NULL AND row_status='active'
```

设计约束（每一条都是踩过的坑）：

1. **不能只按 `(account, trade_id)`** —— `trade_id` 混存三种语义。9-10 位短 id 是网格
   路径写入的 `str(order_id)`，**不是全局唯一成交编号**；实测 15 组重复 **100% 跨标的**
   （同一 `order_id` 在不同股票/日期上复用），只按 `trade_id` 建键会互相冲突
2. **`trade_type` 入键** —— 同 id 同股同日但方向不同是两笔成交
3. **时间分量 NULL 安全，且只能用成交自身的属性** —— `deal_time` 对全部历史行为 NULL，
   直接入键会让 SQLite 把 NULL 视为互不相等、索引彻底失效；
   用 `recorded_at`（落库时刻）兜底**也是错的**：同一秒落库的两笔**不同**成交会撞键
   被静默丢弃。故用 `trade_time`
4. **`volume`/`price` 入键** —— 同一委托的分笔成交价量不同，必须都保留

!!! note "rowcount=0 不等于重复"
    `INSERT OR IGNORE` 被挡下时**必须比对冲突行**：内容相同才算重复投递，
    内容不同则落 `run_events(deal_key_collision)` 并**强制写入**（trade_id 加后缀，
    原编号保留在 `fill_ids`）。只看 rowcount 会把不同成交当重复丢掉。

## 券商对账单导入

```bash
python scripts/import_broker_statement.py --dir "<对账单目录>" --dry-run
python scripts/import_broker_statement.py --dir "<对账单目录>"
```

!!! info "这是历史成交时间的唯一来源"
    QMT 的 xttrader **没有任何历史成交查询接口**（`query_stock_trades` 只返回当日），
    数据库里既成的 `local_fallback` 记录只能靠对账单升级为 `broker`。

**对账单为 GBK 编码**，文件名形如 `<账号>_<序号>_deals.csv`。三级匹配：

| 优先级 | 条件 | 说明 |
|--------|------|------|
| 1 | `成交编号 == trade_records.trade_id` | 最强，唯一 |
| 2 | `订单编号 == trade_records.trade_id` **且代码相同** | `order_id` 跨股跨日复用，**必须用代码消歧**，否则一次匹配到多行 |
| 3 | 代码+方向+价量相同且时间邻近（默认 ±60 秒） | 兜底 |

实测 2026-09-11 的对账单 **13/13 全部命中**（11 笔走第 1 级、2 笔走第 2 级）。

!!! warning "对账单手续费为 0 时不覆盖本地估算"
    对账单里 `0.00` 通常表示"该字段未导出"而非"真的免费"。用它覆盖估算值只会更差，
    因此仅在 `> 0` 时才采纳并标 `commission_source='broker'`。

## 历史回填

```bash
python scripts/backfill_trade_records.py --dry-run
python scripts/backfill_trade_records.py
```

| 字段 | 回填口径 |
|------|---------|
| `account` | 按库路径推导（`data_<id>/trading.db` → `<id>`） |
| `strategy_label` | 按 `strategy` 映射；**映射不到写 `UNKNOWN`**，不留空不猜 |
| `time_source` | 一律 `local_fallback` —— **绝不伪装 `exchange`** |
| `commission` | 按证据判定：可证实等于旧代码 `amount×0.0003` 的用现行税费重算并标 `estimated`；来源不明的保留原值标 `unknown` |
| `order_id` | 网格行 `trade_id` 本就是 `order_id`，据此回填 |
| `is_simulation` | 按 `SIM_` 前缀 / strategy 推断，并把模拟行**物理迁移**到 `trade_records_sim` |

幂等，可重复执行；输出前后行数对比（模拟行迁移造成的减少会单独说明）。

## 导出

```bash
python scripts/export_settlement.py --start 2026-09-14 --end 2026-10-10 \
    --accounts all --out export/
```

**只读**、不依赖 `logs/*.log`（日志会滚动，不是数据源）。多账号时各自输出到
`export/<account>/` 子目录。

### 输出文件

| 文件 | 内容 |
|------|------|
| `trading_events_<实际起>_<实际止>.csv` | 主交割单。**前 14 列是固定契约**（顺序不变），另追加 `time_source`/`order_id`/`row_status` 三个诊断列 |
| `positions_begin.csv` | 期初持仓。无快照时写 `BLOCKER: no snapshot before <start>` 行 |
| `positions_end.csv` | 期末持仓，优先取**区间内** `position_snapshot` |
| `account_daily.csv` | 每日净值 |
| `cash_flows.csv` | 出入金（当前恒为空表，QMT 无出入金接口） |
| `export_report.txt` | 完整自检报告 |
| `changelog_since_last_export.txt` | 与上次交付对比的逐行变更（id + 字段 + 旧值→新值） |

### 合并规则

`merge_deals()` 是**全项目单点实现**（导出与任何对账工具都必须调用它，
禁止复制第二套）：同账户 + 同代码 + 同方向 + 同策略，相邻间隔 ≤ 10 秒，
**不得跨日界**。

### 报告自检项

总量与金额合计 · 合并参数（窗口 / 合并行数 / 最大跨度）· 时间异常（<09:15 / >15:00 / >20:00）·
`time_source` 分布 · **trade_id 唯一性检查**（分长度统计 + 重复组 + 跨标的重复组）·
代码合法性（6 位纯数字、前导零）· 数值自检 · `commission_source` 分布 ·
枚举越界检查 · **逐只股数闭合**（期初+买−卖=期末，不平的逐只列原因）·
券商对账单匹配情况与未匹配原因 · 已处置重复行清单 · 期初/期末/净值/出入金来源。

!!! danger "两条硬性约束"
    **1. 无期初快照时写 `BLOCKER` 行并以退出码 2 报错**，绝不用 0 填充 ——
    否则会把"缺数据"伪装成"期初空仓"。

    **2. 禁止在导出时剔除任何股票**，股数不闭合的逐只列在报告里说明原因，由人决定。

!!! warning "缺口归因在无快照时无法判定"
    期初快照缺失时，负缺口既可能是期初持仓、也可能是区间内买入流水缺失，
    **二者无法区分**。报告会如实写"无法判定"，而不是猜一个原因。

## 配置项

| 配置 | 默认 | 说明 |
|------|------|------|
| `ENABLE_SETTLEMENT_SNAPSHOT` | `True` | 持仓快照与净值落库总开关 |
| `SETTLEMENT_CLOSE_SNAPSHOT_TIME` | `"15:05:00"` | 收盘快照时间 |
| `SETTLEMENT_SNAPSHOT_CHECK_INTERVAL` | `300` | 收盘任务轮询间隔（秒） |
| `SETTLEMENT_SNAPSHOT_HEALTH_LOOKBACK` | `7` | 快照完整性回溯天数 |
| `SETTLEMENT_ASSET_IDENTITY_TOLERANCE` | `1.0` | 恒等式 `total_asset = cash + frozen_cash + market_value` 的容差（元） |
| `SETTLEMENT_ASSET_JUMP_RATIO` / `_ABSOLUTE` | `0.02` / `5000` | 资产跳变告警阈值 |
| `SETTLEMENT_COMMISSION_RATE` | `0.0003` | 佣金 0.03%（双边） |
| `SETTLEMENT_STAMP_DUTY_RATE` | `0.0005` | 印花税 0.05%（仅卖出） |
| `SETTLEMENT_TRANSFER_FEE_RATE` | `0.00001` | 过户费 0.001%（双边） |

!!! note "`snapshot_type` 只有两种"
    `open`(09:25) 与 `close`(15:05)。曾经的 `intraday`（心跳采样）已删除 ——
    它既把"QMT 未连接时的全零读数"引入了库里，又与 open/close 语义重叠。

## 上线顺序

!!! danger "顺序不可颠倒"
    占位流水未清理就建唯一索引会直接 `IntegrityError`。

```bash
# 1. 停止 miniQMT，确认无残留进程
tasklist | findstr python

# 2. 预演（只读，在临时副本上跑，不会写库）
python scripts/migrate_settlement.py --accounts all --dry-run

# 3. 正式迁移（自动备份到 data/backup/migrations/）
python scripts/migrate_settlement.py --accounts all

# 4. 历史回填
python scripts/backfill_trade_records.py --accounts all --dry-run
python scripts/backfill_trade_records.py --accounts all

# 5. 导入对账单
python scripts/import_broker_statement.py --dir "<目录>"

# 6. 重启，确认日志出现「启动收盘快照线程」
```

或在总控制台 `[3] 数据与配置` 页使用 `[r]`/`[s]`/`[t]`/`[u]`——
前三项内置**停机守卫**（有账号运行则拒绝执行）并强制先跑 dry-run。

!!! warning "代码与迁移的部署窗口"
    **代码先上、迁移未跑**：`record_trade()` 检测缺列后自动降级为旧列写入
    （成交流水不丢，但归因字段缺失）并告警。
    **迁移先跑、代码未上**：无影响。

## 已知边界

- **对账单覆盖范围决定历史时间能补多少**。QMT 客户端导出通常只含导出当日；
  区间内更早的成交无法由此补齐，需补导对应日期的对账单。
- **`deal_time` 对历史行多为 NULL**（`local_fallback`），唯一键的时间分量退回 `trade_time`。
- **出入金无接口**。QMT 的 `XtAsset` 只有 `cash`/`frozen_cash`/`market_value`/`total_asset`
  四个数值字段，没有任何出入金查询接口；只能靠对账单导入或人工填报。
- **资产跳变无法区分盈亏与银证转账**，超过阈值时标记 `unexplained_delta` 供人工判断
  （允许误报、不允许漏报）。
