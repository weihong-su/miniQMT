# 测试框架

## 概述

项目测试代码位于 [test/](https://github.com/weihong-su/miniQMT/tree/main/test) 目录，使用标准 `unittest`。当前回归配置见 `test/integration_test_config.json`，包含 31 个测试组（含 `fast` 快速子集）。

最近一次（2026-07-17）使用 `--all-with-fast` 实测：**31 组、107 模块、1933 用例、1933 通过、0 失败、0 错误、0 跳过，成功率 100%**。

## 测试统计速查

| 版本 | 日期 | 非fast组 | 含fast | 通过率 |
|------|------|----------|--------|--------|
| v3.8.1 | 2026-07-17 | 31 组, 107 模块, 1933 用例 | 1933 | 100% |
| v3.8.0 | 2026-07-13 | 31 组, 1912 用例 | 1912 | 100% |
| v3.6.0 | 2026-07-10 | 28 组, 70 模块, 1039 用例 | - | 100% |

---

## 测试基础设施

| 文件 | 说明 |
|------|------|
| `test/test_base.py` | `TestBase` 基类：测试 DB 创建、持仓 fixture、线程断言、条件等待 |
| `test/test_mocks.py` | `MockQmtTrader`：完整模拟 QMT API（连接、持仓、下单），无需真实 QMT |
| `test/test_utils.py` | 通用测试辅助函数 |

---

## 运行测试

### 回归测试框架（推荐）

```bash
# 快速验证（关键模块子集）
python test/run_integration_regression_tests.py --fast

# 运行全部回归测试
python test/run_integration_regression_tests.py --all

# 运行全部回归测试，并包含 fast 组
python test/run_integration_regression_tests.py --all-with-fast

# 按组运行
python test/run_integration_regression_tests.py --group autobuy
python test/run_integration_regression_tests.py --group system_integration
python test/run_integration_regression_tests.py --group stop_profit
python test/run_integration_regression_tests.py --group grid_signal
python test/run_integration_regression_tests.py --group grid_session
python test/run_integration_regression_tests.py --group grid_trade
python test/run_integration_regression_tests.py --group grid_exit
python test/run_integration_regression_tests.py --group grid_validation
python test/run_integration_regression_tests.py --group grid_comprehensive
python test/run_integration_regression_tests.py --group grid_bug_regression
python test/run_integration_regression_tests.py --group grid_true_pnl
python test/run_integration_regression_tests.py --group qmt_ipc_fallback
python test/run_integration_regression_tests.py --group qmt_rpc
python test/run_integration_regression_tests.py --group multi_account_isolation
python test/run_integration_regression_tests.py --group launcher_deployment
```

### 其他选项

```bash
# 失败重试
python test/run_integration_regression_tests.py --all --retry-failed

# 详细输出
python test/run_integration_regression_tests.py --all --verbose

# 跳过环境准备（不备份生产 DB）
python test/run_integration_regression_tests.py --all --skip-env-prep
```

### 单个测试文件

```bash
python test/run_single_test.py test.test_unattended_operation
python -m unittest test.test_system_integration -v
python test/run_all_grid_tests.py
```

---

## 测试报告

自动输出到：

- `test/integration_test_report.json` — JSON 格式
- `test/integration_test_report.md` — Markdown 格式

---

## 测试分组

| 组名 | 优先级 | 内容 |
|------|--------|------|
| `autobuy` | high | 自动买入候选池筛选、条件检查、防重、HTTP 下单 |
| `system_integration` | critical | 系统集成、无人值守、线程监控 |
| `stop_profit` | high | 动态止盈止损策略（7 个模块） |
| `grid_signal` | high | 网格信号检测与价格追踪 |
| `grid_session` | high | 网格会话生命周期管理 |
| `grid_trade` | high | 网格买卖执行与资金管理 |
| `grid_mece_regression` | critical | 网格状态机、并发预占、委托回调、真实账本、重启恢复等边界 |
| `grid_exit` | high | 网格退出条件检测 |
| `grid_comprehensive` | high | 网格综合端到端场景 |
| `grid_validation` | medium | 参数校验与边界情况 |
| `grid_bugfix_c1` | critical | BUG-C1 修复和 DESIGN-4 约束验证 |
| `grid_bug_regression` | high | 已修复 Bug 的回归验证 |
| `order_rejection` | critical | QMT 拒单保护和自适应卖出冷却 |
| `grid_qa_fixes` | high | MECE 审查修复验证 |
| `grid_max_investment_safety` | critical | max_investment 三重防护验证 |
| `core_metrics` | high | 网格利润计算与风险分级 |
| `trader_callback` | critical | 卖出委托 Callback 兜底机制 |
| `web_api` | critical | RESTful API 功能测试 |
| `multi_account_isolation` | critical | 多账号配置、数据目录、日志和端口隔离 |
| `launcher_deployment` | high | 总控制台环境检查与配置校验 |
| `db_thread_safety` | critical | 数据库线程安全验证 |
| `dual_layer_storage` | critical | 内存 + SQLite 双层存储一致性 |
| `xtdata_data_source` | high | xtdata 动态订阅、Mootdx fallback、股票名称解析、行情源健康评分 |
| `indicator_calculator` | high | 技术指标计算器全方法验证 |
| `grid_qa_gap_supplement` | critical | 网格 QA 缺口补充 |
| `grid_full_range_coverage` | critical | 全网格价格区间覆盖（114 个用例，A-K 11 个套件） |
| `grid_true_pnl` | critical | True P&L / FIFO 真实盈亏验证 |
| `grid_simulation` | high | 价格模拟测试 |
| `qmt_ipc_fallback` | high | 大QMT 文件 IPC 客户端、执行器、PositionManager 集成 |
| `qmt_rpc` | high | 大QMT RPC 交易后端契约、只读门禁、回调和委托映射 |
| `fast` | critical | 5 分钟快速验证子集（当前 33 个模块、717 个用例） |

---

## 编写新测试

```python
from test.test_base import TestBase
from test.test_mocks import MockQmtTrader

class TestMyFeature(TestBase):
    def setUp(self):
        super().setUp()
        self.mock_trader = MockQmtTrader()
        self.mock_trader.add_mock_position("000001.SZ", volume=1000, cost_price=10.0)

    def test_something(self):
        # 测试代码...
        self.wait_for_condition(lambda: condition_met, timeout=5)
```

测试运行时自动备份生产 DB，测试完成后恢复。
