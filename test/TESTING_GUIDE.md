# 集成回归测试使用指南

## 快速开始

### 1. 运行所有测试（推荐）

```bash
python test/run_integration_regression_tests.py --all
```

**自动执行**:
- ✅ 备份生产数据库 (trading.db)
- ✅ 清理所有测试数据库
- ✅ 运行270个测试用例
- ✅ 生成测试报告

### 2. 快速验证（5分钟内完成）

```bash
python test/run_integration_regression_tests.py --fast
```

运行52个关键测试，快速验证核心功能。

### 3. 运行特定测试组

```bash
# 查看所有测试组
python test/run_integration_regression_tests.py --list-groups

# 运行止盈止损测试
python test/run_integration_regression_tests.py --group stop_profit

# 运行网格交易测试
python test/run_integration_regression_tests.py --group grid_signal
```

## 环境准备选项

### 默认行为（推荐）

```bash
python test/run_integration_regression_tests.py --all
```

自动执行：
1. **备份生产数据库** - 备份 `data/trading.db` 到 `data/trading.db.backup_YYYYMMDD_HHMMSS`
2. **清理测试数据库** - 删除所有 `*_test.db`, `grid_test*.db` 等测试文件
3. **验证环境** - 检查必要的目录和配置文件

### 跳过环境准备

```bash
# 完全跳过环境准备（不推荐）
python test/run_integration_regression_tests.py --all --skip-env-prep

# 不清理数据库（保留旧数据）
python test/run_integration_regression_tests.py --all --no-clean

# 不备份生产数据库
python test/run_integration_regression_tests.py --all --no-backup
```

## 高级选项

### 详细输出

```bash
python test/run_integration_regression_tests.py --all --verbose
```

显示：
- 每个备份/删除的文件
- 详细的环境检查信息
- 测试执行详情

### 失败重试

```bash
python test/run_integration_regression_tests.py --all --retry-failed
```

自动重试失败的测试（最多2次）。

### 不生成报告

```bash
python test/run_integration_regression_tests.py --all --no-report
```

跳过生成 JSON 和 Markdown 报告文件。

## 测试报告

测试完成后会生成两个报告文件：

### JSON 报告（机器可读）
```
test/integration_test_report.json
```

包含完整的测试结果数据，可用于CI/CD集成。

### Markdown 报告（人类可读）
```
test/integration_test_report.md
```

包含：
- 总体统计（通过率、耗时）
- 分组结果（每个功能模块的详细数据）
- 失败详情（如果有失败，会显示堆栈跟踪）

## 常见场景

### 场景1: 日常开发后测试

```bash
# 快速验证
python test/run_integration_regression_tests.py --fast

# 如果通过，运行完整测试
python test/run_integration_regression_tests.py --all
```

### 场景2: 修改了网格交易代码

```bash
# 只运行网格相关测试
python test/run_integration_regression_tests.py --group grid_signal
python test/run_integration_regression_tests.py --group grid_trade
python test/run_integration_regression_tests.py --group grid_exit
```

### 场景3: 正式发布前

```bash
# 完整测试 + 详细输出
python test/run_integration_regression_tests.py --all --verbose

# 检查报告
cat test/integration_test_report.md
```

### 场景4: CI/CD 集成

```bash
# 跳过交互式确认，自动处理错误
python test/run_integration_regression_tests.py --all --no-backup

# 检查退出码
if [ $? -eq 0 ]; then
    echo "All tests passed"
else
    echo "Tests failed"
    exit 1
fi
```

## 环境准备详情

### 备份的文件

- `data/trading.db` → `data/trading.db.backup_YYYYMMDD_HHMMSS`
- `data/positions.db` → `data/positions.db.backup_YYYYMMDD_HHMMSS` (如果存在)

### 清理的文件

测试数据库：
- `data/positions.db`
- `data/trading_test.db`
- `data/grid_test*.db`
- `data/grid_trading.db`

SQLite 临时文件：
- `data/*.db-journal`
- `data/*.db-wal`
- `data/*.db-shm`

**注意**: 生产数据库 `data/trading.db` 和备份文件 `*.backup_*` 不会被删除。

### 验证的项目

目录：
- `data/` - 数据目录
- `test/` - 测试目录
- `logs/` - 日志目录

配置文件：
- `config.py` - 系统配置
- `test/integration_test_config.json` - 测试配置

## 故障排除

### 问题1: 文件被占用无法删除

**症状**:
```
[!] 文件被占用，无法删除: trading.db-wal
```

**原因**: 数据库正在被其他进程使用

**解决**:
- 关闭正在运行的 main.py
- 关闭数据库管理工具
- 如果不影响测试，可以忽略（系统会自动跳过）

### 问题2: 环境准备失败

**症状**:
```
警告: 环境准备过程中出现 X 个问题
```

**解决**:
- 如果错误数 ≤ 5: 自动继续测试（非关键错误）
- 如果错误数 > 5: 提示用户确认是否继续
- 使用 `--verbose` 查看详细错误信息

### 问题3: 测试失败

**症状**:
```
[WARNING] Some tests failed, please check the report
```

**解决**:
1. 查看 `test/integration_test_report.md` 了解失败详情
2. 使用 `--retry-failed` 重试失败的测试
3. 如果是数据库schema问题，使用 `--no-clean` 跳过清理，手动删除数据库后重试

## 性能优化

### 减少测试时间

```bash
# 只运行快速测试（5分钟）
python test/run_integration_regression_tests.py --fast

# 跳过环境准备（节省10-20秒）
python test/run_integration_regression_tests.py --all --skip-env-prep
```

### 减少输出

```bash
# 不显示详细输出
python test/run_integration_regression_tests.py --all

# 不生成报告
python test/run_integration_regression_tests.py --all --no-report
```

## 最佳实践

1. **每次修改代码后运行快速测试**
   ```bash
   python test/run_integration_regression_tests.py --fast
   ```

2. **提交代码前运行完整测试**
   ```bash
   python test/run_integration_regression_tests.py --all
   ```

3. **正式发布前运行详细测试**
   ```bash
   python test/run_integration_regression_tests.py --all --verbose
   ```

4. **定期清理旧备份文件**
   ```bash
   # 删除7天前的备份
   find data/ -name "*.backup_*" -mtime +7 -delete
   ```

5. **CI/CD 集成**
   ```bash
   # 在CI环境中跳过备份，减少磁盘占用
   python test/run_integration_regression_tests.py --all --no-backup
   ```

## 总结

- ✅ **默认行为**: 自动备份、清理、测试
- ✅ **安全**: 生产数据库会被备份，不会丢失
- ✅ **灵活**: 支持多种选项组合
- ✅ **快速**: 快速模式5分钟内完成
- ✅ **详细**: 生成完整的测试报告

**推荐命令**:
```bash
# 日常开发
python test/run_integration_regression_tests.py --fast

# 正式发布
python test/run_integration_regression_tests.py --all --verbose
```
