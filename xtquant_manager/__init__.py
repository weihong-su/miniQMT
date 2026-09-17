"""xtquant_manager — miniQMT xtquant 接口统一管理层

主要类：
- XtQuantManager: 多账号注册表（单例），请求分发入口
- XtQuantAccount / AccountConfig: 单账号封装
- XtQuantClient / ClientConfig: HTTP 客户端（兼容 easy_qmt_trader）
- XtQuantServer / XtQuantServerConfig: uvicorn 服务封装
- HealthMonitor: 后台健康检查
- SecurityConfig: 安全配置
"""

from .account import AccountConfig, XtQuantAccount
from .client import ClientConfig, XtQuantClient, XtDataAdapter
from .exceptions import (
    AccountAlreadyExistsError,
    AccountNotFoundError,
    XtQuantCallError,
    XtQuantConnectionError,
    XtQuantManagerError,
    XtQuantTimeoutError,
)
from .health_monitor import HealthMonitor
from .manager import XtQuantManager
from .metrics import MetricsCollector
from .security import SecurityConfig
from .server import create_app
from .server_runner import XtQuantServer, XtQuantServerConfig
from .standalone_config import StandaloneConfig, AccountEntry, load_standalone_config
from .watchdog import ServerWatchdog
from .standalone import StandaloneApplication, main as standalone_main

__all__ = [
    # 核心管理
    "XtQuantManager",
    # 账号
    "XtQuantAccount",
    "AccountConfig",
    # 客户端
    "XtQuantClient",
    "ClientConfig",
    "XtDataAdapter",
    # 服务端
    "XtQuantServer",
    "XtQuantServerConfig",
    "create_app",
    # 健康监控
    "HealthMonitor",
    # 安全
    "SecurityConfig",
    # 指标
    "MetricsCollector",
    # 异常
    "XtQuantManagerError",
    "XtQuantTimeoutError",
    "XtQuantCallError",
    "XtQuantConnectionError",
    "AccountNotFoundError",
    "AccountAlreadyExistsError",
    # 独立运行
    "StandaloneConfig",
    "AccountEntry",
    "load_standalone_config",
    "ServerWatchdog",
    "StandaloneApplication",
    "standalone_main",
]
