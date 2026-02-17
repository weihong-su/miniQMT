# miniQMT - 无人值守量化交易系统

<div align="center">

[![Python Version](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![QMT API](https://img.shields.io/badge/QMT-API-orange)](https://dict.thinktrader.net)

**基于迅投QMT API的智能量化交易系统**

[功能特性](#功能特性) • [快速开始](#快速开始) • [系统架构](#系统架构) • [文档](#文档) • [常见问题](#常见问题)

</div>

---

## 📖 项目简介

miniQMT 是一个专为A股市场设计的**无人值守量化交易系统**,集成了自动化交易策略执行、智能持仓管理、动态止盈止损等功能。系统采用双层存储架构和线程自愈机制,可7x24小时稳定运行。

### 🎯 核心优势

- **🛡️ 无人值守**: 线程健康监控与自动重启,系统崩溃自动恢复
- **🔄 双层存储**: 内存数据库 + SQLite持久化,高性能与数据安全兼顾
- **🎯 信号分离**: 信号检测与执行分离,策略逻辑清晰可控
- **📈 智能止盈止损**: 动态止盈策略,最大化收益同时控制风险
- **🌐 网格交易**: 智能网格策略,自动低吸高抛
- **⚙️ 配置管理**: Web界面动态配置,无需重启系统
- **🔍 卖出监控**: 委托单超时监控与自动撤单
- **🧪 模拟交易**: 完整的模拟交易功能,策略验证零风险

---

## ✨ 功能特性

### 交易功能
- ✅ **自动交易执行**: 支持模拟交易和实盘交易
- ✅ **动态止盈止损**: 首次止盈 + 动态止盈双重保护
- ✅ **网格交易**: 智能网格会话管理,自动低吸高抛
- ✅ **信号验证机制**: 防止重复执行,确保交易安全
- ✅ **委托单管理**: 卖出委托单超时监控与自动撤单

### 系统功能
- ✅ **线程健康监控**: 自动检测线程崩溃并重启
- ✅ **优雅关闭**: 有序关闭各模块,避免数据丢失
- ✅ **超时保护**: API调用超时保护,防止线程阻塞
- ✅ **非交易时段优化**: 自动降低CPU占用(30% → <2%)
- ✅ **内存数据库并发优化**: 16处加锁保护,确保线程安全
- ✅ **配置动态更新**: Web界面修改配置,无需重启系统

### 监控功能
- ✅ **Web实时监控**: 账户信息、持仓列表、网格会话实时更新
- ✅ **SSE推送**: Server-Sent Events实时数据推送
- ✅ **配置管理**: Web界面动态修改系统配置
- ✅ **系统诊断**: 内置多个诊断工具,快速定位问题

---

## 🚀 快速开始

### 环境要求

- **Python**: 3.8+ (推荐 3.9)
- **操作系统**: Windows (QMT仅支持Windows)
- **QMT客户端**: 实盘交易需要安装并登录QMT

### 安装步骤

1. **克隆项目**
```bash
git clone https://github.com/your-repo/miniQMT.git
cd miniQMT
```

2. **安装依赖**
```bash
pip install pandas numpy flask flask-cors xtquant mootdx
```

3. **配置账户** (创建 `account_config.json`)
```json
{
  "account_id": "您的交易账号",
  "account_type": "STOCK",
  "qmt_path": "C:/光大证券金阳光QMT实盘/userdata_mini"
}
```

4. **配置股票池** (可选,创建 `stock_pool.json`)
```json
[
  "000001.SZ",
  "600036.SH",
  "000333.SZ"
]
```

5. **启动系统**
```bash
python main.py
```

6. **访问Web界面**
```
http://localhost:5000
```

### ⚠️ 首次运行建议

**强烈建议先使用模拟模式测试**:

1. 确认 `config.py` 中 `ENABLE_SIMULATION_MODE = True`
2. 运行无人值守功能测试: `python test/test_unattended_operation.py`
3. 观察系统运行稳定后,再切换到实盘模式

---

## 🏗️ 系统架构

### 核心设计

```
┌─────────────────────────────────────────────────────────┐
│                    线程监控器 (60秒)                      │
│              检测线程崩溃 → 自动重启                       │
└─────────────────────────────────────────────────────────┘
                            ↓
┌──────────┬──────────┬──────────┬──────────┬──────────┬────────┐
│数据更新  │持仓监控  │策略执行  │网格交易  │卖出监控  │Web服务 │
│ (60秒)  │ (3秒)   │ (5秒)   │ (5秒)   │ (2秒)   │(持续)  │
└──────────┴──────────┴──────────┴──────────┴──────────┴────────┘
     ↓        ↓        ↓        ↓        ↓        ↓
┌─────────────────────────────────────────────────────────┐
│                    核心架构                              │
├─────────────────────────────────────────────────────────┤
│  配置管理 (config_manager) → 配置持久化数据库             │
│  双层存储 (position_manager) → 内存DB + SQLite          │
│  网格交易 (grid_trading_manager) → 网格会话管理          │
│  卖出监控 (sell_monitor) → 委托单超时撤单                │
└─────────────────────────────────────────────────────────┘


### 数据流

**实盘模式**:
```
QMT实盘账户 → 持仓同步(10秒) → 内存数据库 → 定时同步(15秒) → SQLite
```

**模拟模式**:
```
Web界面 → 交易执行器 → 内存数据库 (跳过QMT接口)
```

详细架构说明请查看 [ARCHITECTURE.md](ARCHITECTURE.md)

---

## 📊 核心配置

### 功能开关 (config.py)

```python
# 交易模式
ENABLE_SIMULATION_MODE = True   # True=模拟, False=实盘 ⚠️
ENABLE_AUTO_TRADING = False     # 自动交易执行开关 ⚠️

# 策略功能
ENABLE_DYNAMIC_STOP_PROFIT = True  # 止盈止损功能
ENABLE_GRID_TRADING = False        # 网格交易功能

# 系统功能
ENABLE_THREAD_MONITOR = True    # 线程健康监控（无人值守必需）⭐
ENABLE_SELL_MONITOR = False     # 卖出监控开关
DEBUG = False                   # 调试模式
```

### 止盈止损配置

```python
# 止损
STOP_LOSS_RATIO = -0.075  # 成本价下跌7.5%触发止损

# 首次止盈
INITIAL_TAKE_PROFIT_RATIO = 0.06  # 盈利6%触发
INITIAL_TAKE_PROFIT_RATIO_PERCENTAGE = 0.6  # 卖出60%

# 动态止盈
DYNAMIC_TAKE_PROFIT = [
    (0.05, 0.96),  # 最高浮盈5%时,止盈位=最高价*96%
    (0.10, 0.93),  # 最高浮盈10%时,止盈位=最高价*93%
    (0.15, 0.90),
    (0.20, 0.87),
    (0.30, 0.85),
]
```

### 网格交易配置

```python
ENABLE_GRID_TRADING = False     # 网格交易功能开关
GRID_BUY_AMOUNT = 5000          # 每次买入金额(元)
GRID_PRICE_DROP_THRESHOLD = 0.02  # 价格下跌阈值(2%)
GRID_TAKE_PROFIT_RATIO = 0.08   # 网格止盈比例(8%)
GRID_STOP_LOSS_RATIO = -0.10    # 网格止损比例(-10%)
GRID_MAX_HOLD_DAYS = 30         # 最大持有天数
```

### 委托单管理配置

```python
ENABLE_SELL_MONITOR = False     # 卖出监控开关
SELL_ORDER_TIMEOUT = 30         # 委托单超时时间(秒)
ORDER_CHECK_INTERVAL = 2        # 委托单检查间隔(秒)
```

---

## 📚 文档

- **[CLAUDE.md](CLAUDE.md)** - 开发指南(面向AI助手和开发者)
- **[ARCHITECTURE.md](ARCHITECTURE.md)** - 详细架构说明
- **[docs/quick_start_unattended.md](docs/quick_start_unattended.md)** - 无人值守运行指南
- **[docs/unattended_operation_summary.md](docs/unattended_operation_summary.md)** - 线程监控详解

---

## 🧪 测试

### 回归测试框架（推荐）

项目集成了完整的回归测试框架,支持按模块运行、快速验证和失败重试。

```bash
# 快速验证（5分钟内完成，检查关键功能）
python test/run_integration_regression_tests.py --fast

# 运行所有回归测试
python test/run_integration_regression_tests.py --all

# 按组运行
python test/run_integration_regression_tests.py --group system_integration  # 系统集成
python test/run_integration_regression_tests.py --group stop_profit         # 止盈止损
python test/run_integration_regression_tests.py --group grid_comprehensive  # 网格综合

# 其他选项
python test/run_integration_regression_tests.py --all --retry-failed   # 失败重试
python test/run_integration_regression_tests.py --all --verbose        # 详细输出
```

测试报告自动输出到 `test/integration_test_report.json` 和 `test/integration_test_report.md`。

### 推荐测试顺序

```bash
# 1. 无人值守功能测试（验证线程自愈机制）
python test/test_unattended_operation.py

# 2. 系统综合测试
python test/comprehensive_test.py

# 3. 止盈止损测试
python test/test_stop_loss_buy_param.py

# 4. Web数据刷新测试
python test/test_web_data_refresh.py
```

### 系统诊断工具

```bash
# 检查依赖安装
python utils/check_dependencies.py

# 检查系统状态
python test/check_system_status.py

# 诊断QMT连接
python test/diagnose_qmt_connection.py

# 诊断系统问题
python test/diagnose_system_issues.py
```

---

## ❓ 常见问题

### Q1: 如何切换到实盘交易?

**A**: 修改 `config.py`:
```python
ENABLE_SIMULATION_MODE = False  # 切换到实盘
ENABLE_AUTO_TRADING = True      # 启用自动交易
```
**⚠️ 注意**: 确保QMT客户端已启动并登录!

### Q2: 系统崩溃后会自动恢复吗?

**A**: 会的! 系统内置线程健康监控,每60秒检查一次线程状态,检测到崩溃立即重启。详见 [无人值守运行文档](docs/quick_start_unattended.md)

### Q3: 如何查看系统运行日志?

**A**: 日志文件位于 `logs/qmt_trading.log`,可以使用:
```bash
# 实时查看日志
tail -f logs/qmt_trading.log

# 查看最近100行
tail -n 100 logs/qmt_trading.log
```

### Q4: 模拟交易和实盘交易有什么区别?

**A**:
- **模拟交易**: 不调用QMT API,使用虚拟资金,无交易时间限制
- **实盘交易**: 通过QMT API执行真实订单,使用实际账户资金

### Q5: 如何添加新的股票到股票池?

**A**: 编辑 `stock_pool.json` 文件,添加股票代码(格式: `股票代码.交易所`):
```json
[
  "000001.SZ",  // 深圳交易所
  "600036.SH",  // 上海交易所
  "新增股票.SZ"
]
```

### Q6: 如何启用网格交易?

**A**: 修改 `config.py`:
```python
ENABLE_GRID_TRADING = True  # 启用网格交易
GRID_BUY_AMOUNT = 5000      # 每次买入5000元
```
通过 Web 界面创建网格会话，系统会自动检测买入信号并执行。网格会话支持自动止盈、止损和超时退出。

### Q7: 如何通过Web界面修改配置?

**A**: 访问 `http://localhost:5000/config` 页面，修改配置后点击保存。配置会立即生效，无需重启系统。支持的配置包括:
- 止盈止损比例
- 网格交易参数
- 自动交易开关
- 线程监控开关

所有配置变更会记录到 `system_config` 和 `config_history` 表中，支持审计和回滚。

更多问题请查看 [CLAUDE.md - 常见问题](CLAUDE.md#常见问题与解决方案)

---

## 🛡️ 风险提示

**⚠️ 重要提醒**:

1. **量化交易有风险,投资需谨慎**
2. **本系统仅供学习研究使用,不构成投资建议**
3. **实盘交易前请充分测试策略,确保理解系统运行逻辑**
4. **建议设置合理的止损比例,控制单日最大亏损**
5. **定期检查系统运行状态,关注异常日志**

---

## 📄 许可证

本项目采用 MIT 许可证 - 详见 [LICENSE](LICENSE) 文件

---

## 🤝 贡献

欢迎提交 Issue 和 Pull Request!

---

## 📧 联系方式

如有问题或建议,请通过以下方式联系:

- 提交 [GitHub Issue](https://github.com/your-repo/miniQMT/issues)
- 邮箱: your-email@example.com

---

<div align="center">

**⭐ 如果这个项目对你有帮助,请给个Star支持一下! ⭐**

Made with ❤️ by miniQMT Team

</div>
