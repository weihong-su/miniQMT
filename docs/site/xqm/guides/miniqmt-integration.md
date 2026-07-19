# 与 miniQMT 集成

## 切换方式

在 `config.py` 中修改一个开关，无需改动任何业务代码：

```python
ENABLE_XTQUANT_MANAGER = True        # False = 直连 xtquant，True = HTTP 代理
XTQUANT_MANAGER_URL = "http://127.0.0.1:8888"
XTQUANT_MANAGER_TOKEN = ""
```

## 行为对比

| 功能 | `= False`（直连） | `= True`（HTTP 代理） |
|------|------|------|
| 交易接口 | 直接调用 `easy_qmt_trader` | 通过 `XtQuantClient` HTTP |
| 行情接口 | 直接调用 `xtquant.xtdata` | 通过 `XtDataAdapter` HTTP |
| 服务启动 | 无额外进程 | 自动启动 HTTP 服务 |
| `register_trade_callback` | 实时触发 | no-op |
| `pending_orders` 同步 | 实时 | 约 3 秒后持仓轮询更新 |

`ENABLE_XTQUANT_MANAGER=True` 时，miniQMT 主程序会在模块初始化前尝试启动本机 `:8888` HTTP 网关，并让 `position_manager` 使用 `XtQuantClient`、`data_manager` 使用 `XtDataAdapter`。如果通过启动器选择 web2.0 模式，主进程会设置 `QMT_NO_FLASK=1` 跳过 Flask Web 服务，由 xtquant_manager 托管 web2.0 静态文件和兼容 API。

!!! note "网关模式不是完整 Flask 替代"
    xtquant_manager 提供 `/api/status`、`/api/positions`、`/api/trade-records`、`/api/grid/sessions` 等 Flask 兼容读端点，便于 web2.0 多账号监控；配置保存、自动操作总开关、模拟买入、初始化持仓、网格启停等仍需 Flask 直连模式。

## 适用场景

| 场景 | 推荐设置 |
|------|---------|
| 单机开发/测试，一个账号 | `False`（直连，最简单） |
| 需要 HTTP 接口供外部调用 | `True` |
| 多账号，策略需要同时访问多个账号 | `True` |
| 分析机/监控机分离 | `True` |

## 透明兼容性

`XtQuantClient` 的方法签名与 `easy_qmt_trader` 完全一致，`XtDataAdapter` 的方法签名与 `xtquant.xtdata` 完全一致，因此 `strategy.py`、`position_manager.py` 等业务代码无需任何改动，切换透明。
