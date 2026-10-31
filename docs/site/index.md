# miniQMT 文档站

基于迅投 QMT API 的**无人值守量化交易系统**。

当前版本：**v3.9.6（2026-10-31）**。本版完善委托与部分成交闭环、Web 持仓提示及运行数据保留，详见[变更日志](changelog.md)。

<div class="grid cards" markdown>

-   :material-robot: **miniQMT 核心系统**

    ---

    自动化交易策略执行、智能止盈止损、网格交易、自动买入、线程自愈、Web 监控界面。

    [快速开始 :octicons-arrow-right-24:](miniqmt/index.md)

-   :material-api: **XtQuantManager**

    ---

    xtquant 多账号 HTTP 网关，RESTful API 封装，支持局域网远程调用。

    [快速开始 :octicons-arrow-right-24:](xqm/index.md)

</div>

---

## 功能概览

| 组件 | 端口 | 说明 |
|------|------|------|
| miniQMT 主程序 | 5000 | 交易策略引擎 + Web 监控 |
| XtQuantManager | 8888 | 多账号 HTTP 网关（可选） |
| miniqmt_autobuy | 无固定端口 | 独立自动买入进程，复用 Web API 下单 |

## 许可证

**Business Source License 1.1** — 个人/非商业免费使用，商业用途需获授权。详见[许可证](license.md)。
