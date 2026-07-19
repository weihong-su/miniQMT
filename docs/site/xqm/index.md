# XtQuantManager

**miniQMT xtquant 接口统一管理层** — 将迅投 QMT 的交易接口（xttrader）和行情接口（xtdata）封装为 HTTP RESTful API，支持多账号、自动重连、可观测性和独立止盈止损。

| 痛点 | 解决方案 |
|------|---------|
| 无法管理多账号 | 多账号注册表，支持同时管理任意数量 QMT 账号 |
| 断线无法自动重连 | 三级健康监控 + 指数退避自动重连 |
| 超时保护不统一 | 全接口统一超时保护（默认 3 秒） |
| 无可观测性 | 实时指标（延迟、错误率、P95）+ HTTP 查询 |
| 远程部署无策略保护 | **动态止盈止损后台线程**，独立于 main.py 运行 |
| xtquant 硬耦合 | HTTP 抽象层 + 开关，零侵入切换 |

---

## 快速开始

=== "管理脚本（推荐）"

    ```bat
    # 启动服务（Windows，5 秒内就绪）
    xtquant_manager\xqm_manager.bat start

    # 验证健康
    curl http://127.0.0.1:8888/api/v1/health
    ```

=== "命令行"

    ```bash
    # 直接启动（需先配置 xtquant_manager_config.json）
    python -m xtquant_manager --host 127.0.0.1 --port 8888
    ```

=== "Python 代码"

    ```python
    from xtquant_manager import XtQuantServer, XtQuantServerConfig
    from xtquant_manager import XtQuantManager, AccountConfig

    server = XtQuantServer(XtQuantServerConfig(host="127.0.0.1", port=8888))
    server.start(blocking=False)

    manager = XtQuantManager.get_instance()
    manager.register_account(AccountConfig(
        account_id="55009640",
        qmt_path="C:/QMT/userdata_mini",
    ))
    ```

服务启动后，访问 [http://127.0.0.1:8888/docs](http://127.0.0.1:8888/docs) 查看 Swagger 接口文档。

!!! info "web2.0 网关兼容能力"
    除 `/api/v1/*` 多账号 API 外，`server.py` 还提供一组 Flask 兼容只读端点供 web2.0 网关模式使用：`/api/status`、`/api/positions`、`/api/positions-all`、`/api/accounts`、`/api/connection/status`、`/api/config`、`/api/trade-records`、`/api/grid/sessions`。这些端点会通过 `X-Account-Id` 选择账号，并合并 QMT 实时字段与账号 SQLite 元数据；配置保存、初始化持仓、网格启停等写操作仍需 Flask 直连模式。

---

## 最简配置文件

将以下内容保存为项目根目录的 `xtquant_manager_config.json`，然后运行 `python -m xtquant_manager`：

```json
{
  "host": "127.0.0.1",
  "port": 8888,
  "accounts": [
    {
      "account_id": "55009640",
      "qmt_path": "C:/QMT/userdata_mini",
      "account_type": "STOCK"
    }
  ]
}
```

---

## 下一步

- [架构说明](architecture.md) — 了解分层设计和健康监控机制
- [配置指南](configuration/index.md) — 根据你的场景选择配置方式
- [API 手册](api/index.md) — 完整的接口参考
- [多账号实战](guides/multi-account.md) — Python 代码示例

!!! info "许可证"
    miniQMT v2.0 起采用 **BSL 1.1** 授权。个人/非商业免费使用，商业用途需获授权。详见[许可证](../license.md)。
