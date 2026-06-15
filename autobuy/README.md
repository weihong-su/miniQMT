# miniqmt_autobuy — 自动买入模块

基于外部候选池的自动选股买入服务。独立进程运行,定期从 SQLite 候选池筛选"运行日前最近 N 个交易日入池"的股票,做技术面买入条件检查,从通过者中随机选一只,**复用 web1.0 的买入 API 路径**下单,后续止盈止损交由主程序 `position_manager` 自动接管。

> 本目录(`autobuy/`)集中存放本模块的代码、配置与说明文档。运行数据仍写入项目根目录下的 `data/` 与 `logs/`。

## 架构(方案 B:独立进程)

```
python -m autobuy.app  (独立进程, 由 miniqmt.bat [j] 启动)
  ├ 调度循环(每30s tick, 双触发: daily / interval / both)
  ├ 候选池筛选(多表并集)          ── 读 ──► C:\github-repo\stockquant\chan.db
  ├ 大盘指数门禁                  ── 读 ──► 999999 / 399001 / 399005 日线 MA5
  ├ 防重过滤(前置)                ── 读 ──► 运行中 web /api/positions
  ├ 洗牌 + 惰性条件检查(命中即停)  ── import ──► data_manager / xtdata (进程内自取行情)
  └ HTTP 下单 + 记录              ── POST ──► 运行中 web /api/actions/execute_buy
                                  ── 读写 ──► data/autobuy.db (买入历史/决策日志)
```

**下单严格复用 web API**;行情/指标在 autobuy 进程内自取(web 无通用行情查询端点,xtdata 为 C/S 架构,多进程取数互不冲突)。

## 候选池口径(重要)

- **多表并集**:`tables = stg_chan, zs_pool`,**每张表各自**取运行日前最近 N 个交易日对应 `date` 的记录,
  再合并去重。比如运行日是周日 `2026-06-14`,`latest_n_dates=2` 会筛选 `2026-06-12` 和 `2026-06-11`。
- **交易日口径**:当前按周一至周五回溯,自动跳过周末;A 股法定节假日可后续接入交易日历进一步精确化。
- **代码格式自动转换**:候选池为 `sh.600025`/`sz.000626`(市场前缀在前),自动转成系统标准 `600025.SH`
  (见 `to_xt_code`)。防重比较统一按 6 位数字(`normalize_code`)。
- **批量提示**:`stg_chan` 单个交易日可达数百只。`latest_n_dates=2` 可能产生两百余只候选;
  若只想要前一交易日,设 `latest_n_dates=1`。

## 大候选池的惰性求值

候选池可能数百只,而每轮只买 `max_buys_per_run`(默认 1)只。为避免对全部候选做昂贵的逐只行情/指标检查:
**先做大盘指数门禁 → 防重过滤 → 洗牌 → 顺序惰性检查,收集到所需数量即停**。对均匀洗牌列表取"前 k 个通过项",
数学上等价于在全部通过标的中均匀随机选 k 只,但通常只需检查少量标的即命中。

## 大盘指数门禁

自动买入前会检查 `999999`、`399001`、`399005` 三个指数。只要任一指数最近一根 MA5 大于前一根 MA5,本轮才继续执行个股防重、条件检查与下单;若三个指数 MA5 都未向上,本轮直接结束且不下单。

## 文件清单

| 文件 | 职责 |
|------|------|
| `miniqmt_autobuy.cfg` | INI 配置 |
| `config.py` | 配置解析 + 校验 + autobuy 独立 logger |
| `pool.py` | 候选池筛选(多表/最近N个交易日/代码格式转换) |
| `filter.py` | 买入条件检查 |
| `client.py` | HTTP 下单 + 查持仓 |
| `store.py` | 自有库 `data/autobuy.db`(防重 + 复盘) |
| `app.py` | 进程入口 + 调度 |
| `__init__.py` | Python 包入口 |

## 配置说明(autobuy/miniqmt_autobuy.cfg)

### [pool] 候选池
- `db_path` — 默认 `C:\github-repo\stockquant\chan.db`
- `tables` — 多表并集,逗号分隔,默认 `stg_chan, zs_pool`
- `code_column` / `date_column` — 默认 `code` / `date`
- `latest_n_dates` — 每表取运行日前最近 N 个交易日(默认 2)

### [filter] 买入条件(均可独立开关 + 阈值可配)
- 大盘门禁: `999999` / `399001` / `399005` 至少一个指数 MA5 向上才允许自动买入
- 换手率 `>= min_turnover_rate`(默认 5%);`volume_unit_multiplier` 控制成交量单位换算(手→股填 100,已是股填 1)
- 量比 `>= min_volume_ratio`(默认 2)
- 涨幅 `>= min_pct_change`(默认关闭)
- MA8 方向向上;现价 `<= MA8 * max_price_to_ma8_ratio`(默认 1.07)
- 涨停/停牌跳过

### [risk] 风控(防重复买入同一只股票)
- `dedup_by_position` — 已持仓该股则跳过(查 web `/api/positions`)
- `dedup_window_days` — 历史 N 天内买过则跳过(0=当天,-1=永久)
- `max_buys_per_run` — 每次触发最多买入只数(默认随机选 1 只)
- 持仓查询失败时**本轮不下单**(安全优先,避免重复)

### [web] 下单通道
- `base_url` — 目标账号 web_server(多账号改端口);`api_token` — 对应 `QMT_API_TOKEN`
- 单笔金额沿用 web 端 `config.POSITION_UNIT`

### [schedule] 触发
- `mode` — `daily` / `interval` / `both`
- `daily_times` — 每日定点(逗号分隔多个,如 `09:35,14:45`)
- `interval_minutes` — 固定间隔(仅交易时段);`only_trade_time` 兜底

## 启动与管理(miniqmt.bat 菜单)

| 菜单 | 功能 |
|------|------|
| `[j]` | 启动自动买入服务 |
| `[k]` | 停止自动买入服务 |
| `[l]` | 查看状态(读 `data/.autobuy_status.json`) |
| `[m]` | 查看日志(`logs/miniqmt_autobuy.log`) |

手动单次触发(测试):`python -m autobuy.app --once`

## 复盘(data/autobuy.db)

- `buy_history` — 每次买入尝试(代码/时间/触发源/成功标志/HTTP状态/订单结果/金额),用于防重 + 资金复盘
- `decision_log` — 每轮**已检查**标的的条件明细(`reason_json` 记录各项实际值与通过判定)。
  注:惰性求值下只记录实际检查过的标的(命中即停),非全候选池

## 端到端验证

1. 编辑 `autobuy/miniqmt_autobuy.cfg`:确认 `chan.db` 路径与 `stg_chan/zs_pool` 表存在
2. 确保目标账号 web_server 已运行(默认 :5000)
3. `miniqmt.bat` → `[j]` 启动 → `[l]` 看状态 → `[m]` 看日志
4. 首次跑 `--once`,查日志:目标交易日与命中数、大盘指数门禁结果、洗牌后检查了几只即命中、下单结果;
   核对 `turnover_rate` 实际值确认 `volume_unit_multiplier` 单位是否正确

## 注意事项

- **依赖 web_server 在线**:autobuy 仅"下单"走 HTTP,需对应账号 web 服务运行
- **换手率单位**:`volume_unit_multiplier` 默认 100(假设 volume 单位为手),若数据源 volume 已是股需改为 1
- **科创板/创业板**:候选含 688/300/301,需账户对应交易权限;688 最小 200 股。无权限/不足时由 QMT 拒单并记日志,不影响流程
