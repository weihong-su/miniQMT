# miniQMT - 快速入门指南

> 完整系统文档请查看 [README.md](README.md) 和 [ARCHITECTURE.md](ARCHITECTURE.md)

---

## 1. 环境准备

### 1.1 系统要求

- **操作系统**: Windows 10/11（QMT 仅支持 Windows）
- **Python**: 3.8+（推荐 Anaconda3/envs/python39）
- **QMT 客户端**: 实盘交易需安装并登录迅投 QMT

### 1.2 安装依赖

```bash
# 从项目根目录运行
pip install -r utils/requirements.txt

# 验证安装
python utils/check_dependencies.py
```

### 1.3 配置账户（必需）

创建 `account_config.json`（参考以下模板）：

```json
{
  "account_id": "您的交易账号",
  "account_type": "STOCK",
  "qmt_path": "C:/光大证券金阳光QMT实盘/userdata_mini"
}
```

### 1.4 配置股票池（可选）

创建 `stock_pool.json`：

```json
[
  "000001.SZ",
  "600036.SH",
  "000333.SZ"
]
```

---

## 2. 启动系统

### 方式一：直接启动

```bash
python main.py
```

首次运行会自动创建 `data/positions.db` 数据库文件。

### 方式二：交互式总控台（推荐）

```bash
miniqmt.bat                       # 打开交互式菜单（环境检查/配置校验/启动/停止/网关管理）
python scripts/_launcher.py menu  # 等效命令
```

常用入口：`[7]`-`[9]` 启动账号并选择 web1.0/web2.0，`[d]`-`[i]` 管理 XtQuantManager，`[j]`-`[m]` 管理自动买入，`[n]` 配置 Tushare，`[o]` 配置大QMT 文件 IPC，`[p]` 统一切换 xttrader 直连 / IPC / RPC 交易通道。

也可配置 `launcher.ini` 后双击 `launcher.bat` 直接拉起 `main.py`：

```ini
[Environment]
ENV_TYPE=conda           # conda 或 uv
CONDA_ENV=python39       # conda 虚拟环境名称

[Project]
WORK_DIR=c:\github-repo\miniQMT

[Script]
PYTHON_SCRIPT=main.py
```

### 访问 Web 界面

```
http://localhost:5000          # web1.0 (Flask)，随系统自动启动
```

> web2.0 (Vue3 + PWA) 需先构建：`cd web2.0 && npm install && npm run build`，再由 Flask / xtquant_manager 托管，或独立部署到 Vercel。

---

## 3. 核心功能开关

所有开关在 `config.py` 中配置：

| 开关 | 默认值 | 说明 |
|------|--------|------|
| `ENABLE_SIMULATION_MODE` | `True` | 模拟交易/实盘切换 ⚠️ |
| `ENABLE_AUTO_OPERATION` | `False` | 全局自动操作总开关，关闭时所有自动策略不产生新单 ⚠️ |
| `ENABLE_AUTO_TRADING` | `False` | 非网格自动策略执行开关（动态止盈止损） |
| `ENABLE_DYNAMIC_STOP_PROFIT` | `True` | 动态止盈止损策略 |
| `ENABLE_GRID_TRADING` | `True` | 网格交易功能 |
| `GRID_REQUIRE_PROFIT_TRIGGERED` | `False` | 是否要求持仓已首次止盈后才能启动网格；默认不要求 |
| `MARKET_HEALTH_ENABLED` | `True` | 行情源健康评分（内存版，不落库） |
| `MARKET_HEALTH_OBSERVE_ONLY` | `False` | 默认按评分和数据源策略拦截不可信行情；如只观察可显式改为 `True` |
| `ENABLE_THREAD_MONITOR` | `True` | 线程自愈监控（无人值守必需）⭐ |
| `ENABLE_SELL_MONITOR` | `True` | 卖出委托单超时撤单 |
| `ENABLE_XTQUANT_MANAGER` | `False` | 多账户 HTTP 网关（可选） |

交易通道由 `position_manager._create_qmt_trader()` 四选一：默认 xttrader 直连；也可启用 `ENABLE_XTQUANT_MANAGER`、`ENABLE_QMT_IPC_FALLBACK` 或 `ENABLE_QMT_RPC_FALLBACK`。后三个后端互斥，同时开启会启动失败；RPC 默认 `QMT_RPC_ALLOW_ORDER=False`，只读不下单。

---

## 4. 模式说明

### 模拟模式（默认，安全）

- `ENABLE_SIMULATION_MODE = True`
- 不调用 QMT API，使用虚拟资金
- 无交易时间限制，可随时测试

### 实盘模式（需要 QMT 客户端）

```python
# config.py
ENABLE_SIMULATION_MODE = False  # 切换到实盘
ENABLE_AUTO_OPERATION = True    # 打开全局自动操作总开关
ENABLE_AUTO_TRADING = True      # 启用非网格自动策略
```

**⚠️ 切换实盘前检查清单**：
1. QMT 客户端已启动并登录
2. `account_config.json` 账号信息正确
3. `stock_pool.json` 股票池配置正确
4. 已在模拟模式下充分验证策略
5. 若使用 RPC 通道，确认 `QMT_RPC_ALLOW_ORDER=True` 后才允许真实下单

---

## 5. 运行测试

### 快速验证（推荐，约 5 分钟）

```bash
python test/run_integration_regression_tests.py --fast
```

### 全量回归测试

```bash
# 全部测试
python test/run_integration_regression_tests.py --all

# 按模块运行
python test/run_integration_regression_tests.py --group system_integration  # 系统集成
python test/run_integration_regression_tests.py --group stop_profit         # 止盈止损
python test/run_integration_regression_tests.py --group grid_signal         # 网格信号
python test/run_integration_regression_tests.py --group grid_comprehensive  # 网格综合
python test/run_integration_regression_tests.py --group grid_bug_regression # Bug 回归
python test/run_integration_regression_tests.py --group grid_full_range_coverage  # 全区间覆盖

# 其他选项
python test/run_integration_regression_tests.py --all --retry-failed   # 失败重试
python test/run_integration_regression_tests.py --all --verbose        # 详细输出
```

测试报告：`test/integration_test_report.md`

当前回归配置包含 31 个测试组（含 `fast` 快速子集）。`--all` 默认排除重复的 `fast` 组，`--all-with-fast` 会连同快速子集一起运行；最近一次 `--all-with-fast` 实测为 31 组、107 个模块、1933 个用例，100% 通过。

### 单个测试文件

```bash
python -m unittest test.test_system_integration -v
python test/run_single_test.py test.test_unattended_operation
```

---

## 6. 系统诊断

```bash
# 检查依赖安装
python utils/check_dependencies.py

# 查看运行状态（Web 界面或总控台菜单 [6]）
miniqmt.bat

# 查看实时日志
Get-Content logs/qmt_trading.log -Wait   # Windows PowerShell
tail -f logs/qmt_trading.log             # Git Bash / WSL
```

---

## 7. 常见问题

### Q: 如何查看系统运行状态？

访问 `http://localhost:5000`，或查看日志：

```bash
Get-Content logs/qmt_trading.log -Wait   # Windows PowerShell
```

### Q: 线程崩溃后系统会自动恢复吗？

会的。`ENABLE_THREAD_MONITOR = True`（默认启用）时，系统每 60 秒检查线程存活状态，崩溃后自动重启，并有 60 秒冷却时间防止重启风暴。

### Q: 如何启用网格交易？

网格交易默认已启用（`ENABLE_GRID_TRADING = True`），但仍受全局自动操作总开关 `ENABLE_AUTO_OPERATION` 控制。通过 Web 界面 (`http://localhost:5000`) 创建网格会话即可；个股网格配置/详情界面里的“自动/暂停”开关对应 `grid_trading_sessions.enabled`，暂停后保留会话但不再发新网格单。如需全局禁用网格，在 `config.py` 中设置：
```python
ENABLE_GRID_TRADING = False
```

默认不再要求持仓先触发首次止盈；如需恢复旧风控，在 `config.py` 中设置 `GRID_REQUIRE_PROFIT_TRIGGERED = True`。

### Q: 如何查看行情源健康评分？

Flask 直连模式下访问：

```bash
curl http://localhost:5000/api/market/health
```

第一阶段为轻量内存版，不落库、重启即清空；默认 `MARKET_HEALTH_OBSERVE_ONLY = False`，会按评分和数据源策略参与交易信号门禁。若只想观察评分不影响交易，可显式改为 `True`。

### Q: 如何通过 Web 界面修改配置？

访问 `http://localhost:5000/config`，修改后点击保存。配置实时生效，无需重启。

### Q: 如何启用多账户支持（XtQuantManager）？

```python
# config.py
ENABLE_XTQUANT_MANAGER = True
XTQUANT_MANAGER_URL = "http://127.0.0.1:8888"
```

详见 [docs/xtquant_manager.md](docs/xtquant_manager.md)

---

## 8. 重要文件位置

| 文件 | 路径 | 说明 |
|------|------|------|
| 核心配置 | `config.py` | 所有系统参数 |
| 账户配置 | `account_config.json` | QMT 账号信息 |
| 股票池 | `stock_pool.json` | 监控股票列表 |
| 数据库 | `data/positions.db` | SQLite 持久化数据 |
| 系统日志 | `logs/qmt_trading.log` | 运行日志 |
| 测试报告 | `test/integration_test_report.md` | 回归测试结果 |
| 依赖清单 | `utils/requirements.txt` | Python 包依赖 |
| 安装指南 | `utils/INSTALL.md` | 详细安装文档 |

---

## 9. 相关文档

- **[在线文档站](https://weihong-su.github.io/miniQMT/)** - 完整文档（无人值守/网格/止盈止损/Web 双模式/数据库）
- **[README.md](README.md)** - 项目总览、功能特性、常见问题
- **[ARCHITECTURE.md](ARCHITECTURE.md)** - 系统架构、数据流、数据库设计
- **[CLAUDE.md](CLAUDE.md)** - 开发规范（面向 AI 助手和开发者）
- **[docs/xtquant_manager.md](docs/xtquant_manager.md)** - XtQuantManager 多账户网关
