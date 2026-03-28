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

### 方式二：一键启动（推荐）

配置 `launcher.ini`，然后双击 `launcher.bat`：

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
http://localhost:5000
```

---

## 3. 核心功能开关

所有开关在 `config.py` 中配置：

| 开关 | 默认值 | 说明 |
|------|--------|------|
| `ENABLE_SIMULATION_MODE` | `True` | 模拟交易/实盘切换 ⚠️ |
| `ENABLE_AUTO_TRADING` | `False` | 是否自动执行交易信号 ⚠️ |
| `ENABLE_DYNAMIC_STOP_PROFIT` | `True` | 动态止盈止损策略 |
| `ENABLE_GRID_TRADING` | `True` | 网格交易功能 |
| `ENABLE_THREAD_MONITOR` | `True` | 线程自愈监控（无人值守必需）⭐ |
| `ENABLE_SELL_MONITOR` | `True` | 卖出委托单超时撤单 |
| `ENABLE_XTQUANT_MANAGER` | `False` | 多账户 HTTP 网关（可选） |

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
ENABLE_AUTO_TRADING = True      # 启用自动交易
```

**⚠️ 切换实盘前检查清单**：
1. QMT 客户端已启动并登录
2. `account_config.json` 账号信息正确
3. `stock_pool.json` 股票池配置正确
4. 已在模拟模式下充分验证策略

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

# 检查系统状态
python test/check_system_status.py

# 诊断 QMT 连接
python test/diagnose_qmt_connection.py

# 诊断系统问题
python test/diagnose_system_issues.py

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

网格交易默认已启用（`ENABLE_GRID_TRADING = True`）。通过 Web 界面 (`http://localhost:5000`) 创建网格会话即可。如需禁用，在 `config.py` 中设置：
```python
ENABLE_GRID_TRADING = False
```

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

- **[README.md](README.md)** - 项目总览、功能特性、常见问题
- **[ARCHITECTURE.md](ARCHITECTURE.md)** - 系统架构、数据流、数据库设计
- **[CLAUDE.md](CLAUDE.md)** - 开发规范（面向 AI 助手和开发者）
- **[docs/xtquant_manager.md](docs/xtquant_manager.md)** - XtQuantManager 多账户网关
- **[docs/quick_start_unattended.md](docs/quick_start_unattended.md)** - 无人值守运行详细指南
