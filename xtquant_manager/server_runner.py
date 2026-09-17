"""
XtQuantServer — uvicorn 启动/停止封装

支持两种运行模式：
1. 阻塞模式（blocking=True）：适合独立进程运行
2. 后台线程模式（blocking=False）：适合嵌入到 miniQMT 的线程架构中

与 miniQMT 的 web_server.py 风格对齐：在独立线程中启动服务。
"""
import threading
import time
from typing import Optional

from .manager import XtQuantManager
from .health_monitor import HealthMonitor
from .security import SecurityConfig
from .stop_profit import StopProfitMonitor, StopProfitConfig

try:
    from logger import get_logger
    logger = get_logger("xqm_runner")
except Exception:
    import logging
    logger = logging.getLogger("xtquant_manager.server_runner")


class XtQuantServerConfig:
    """HTTP 服务配置"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8888,
        # 安全配置
        api_token: str = "",
        allowed_ips: list = None,
        rate_limit: int = 60,
        enable_hmac: bool = False,
        hmac_secret: str = "",
        trust_proxy: bool = False,
        # TLS 配置（局域网场景推荐开启）
        ssl_certfile: str = "",
        ssl_keyfile: str = "",
        # 健康监控配置
        health_check_interval: float = 30.0,
        reconnect_cooldown: float = 60.0,
        # 止盈止损监控配置
        enable_stop_profit: bool = True,
        stop_loss_ratio: float = -0.075,
    ):
        self.host = host
        self.port = port
        self.ssl_certfile = ssl_certfile
        self.ssl_keyfile = ssl_keyfile
        self.health_check_interval = health_check_interval
        self.reconnect_cooldown = reconnect_cooldown
        self.security = SecurityConfig(
            api_token=api_token,
            allowed_ips=allowed_ips or [],
            rate_limit=rate_limit,
            enable_hmac=enable_hmac,
            hmac_secret=hmac_secret,
            trust_proxy=trust_proxy,
        )
        # 止盈止损配置
        self.enable_stop_profit = enable_stop_profit
        self.stop_loss_ratio = stop_loss_ratio

    @property
    def use_tls(self) -> bool:
        return bool(self.ssl_certfile and self.ssl_keyfile)


class XtQuantServer:
    """
    XtQuantManager HTTP 服务启动/停止封装。

    Usage:
        server = XtQuantServer(XtQuantServerConfig(host="0.0.0.0", port=8888))
        server.start()          # 后台线程启动
        # ... 运行中 ...
        server.stop()

    独立进程模式:
        server = XtQuantServer(XtQuantServerConfig(...))
        server.start(blocking=True)
    """

    def __init__(self, config: Optional[XtQuantServerConfig] = None):
        if config is None:
            config = XtQuantServerConfig()
        self.config = config

        self._app = None
        self._server = None
        self._server_thread: Optional[threading.Thread] = None
        self._health_monitor: Optional[HealthMonitor] = None
        self._stop_profit_monitor: Optional[StopProfitMonitor] = None
        self._running = False
        self._startup_error: Optional[str] = None

    def start(self, blocking: bool = False) -> None:
        """
        启动服务。

        Args:
            blocking: True=阻塞当前线程（独立进程模式），False=后台线程（嵌入模式）
        """
        self._startup_error = None

        # 创建 FastAPI 应用。延迟导入可以把 fastapi/pydantic 缺失转成明确启动错误。
        try:
            from .server import create_app
            self._app = create_app(self.config.security)
        except ImportError as e:
            message = self._format_dependency_error(e)
            logger.error(message)
            raise RuntimeError(message) from e
        except Exception as e:
            message = f"创建 XtQuantManager HTTP 应用失败: {e}"
            logger.error(message)
            raise RuntimeError(message) from e

        # 启动健康监控
        manager = XtQuantManager.get_instance()
        self._health_monitor = HealthMonitor(
            manager=manager,
            check_interval=self.config.health_check_interval,
            reconnect_cooldown=self.config.reconnect_cooldown,
        )
        self._health_monitor.start()

        # 启动止盈止损监控
        if self.config.enable_stop_profit:
            sp_cfg = StopProfitConfig(
                enabled=True,
                stop_loss_ratio=self.config.stop_loss_ratio,
            )
            self._stop_profit_monitor = StopProfitMonitor(manager, sp_cfg)
            self._stop_profit_monitor.start()
            # 存入 app.state 供路由访问
            if self._app is not None:
                self._app.state.stop_profit_monitor = self._stop_profit_monitor

        self._running = True

        if blocking:
            self._run_uvicorn()
            if self._startup_error:
                raise RuntimeError(self._startup_error)
        else:
            self._server_thread = threading.Thread(
                target=self._run_uvicorn,
                name="XtQuantManagerServer",
                daemon=True,
            )
            self._server_thread.start()
            # 等待后台线程暴露早期启动错误，避免 uvicorn 未启动却显示成功。
            deadline = time.time() + 1.0
            while time.time() < deadline:
                if self._startup_error:
                    self._cleanup_startup_failure()
                    raise RuntimeError(self._startup_error)
                if not self._server_thread.is_alive():
                    message = "XtQuantManager HTTP 服务启动失败，uvicorn 线程已退出"
                    self._startup_error = self._startup_error or message
                    self._cleanup_startup_failure()
                    raise RuntimeError(self._startup_error)
                time.sleep(0.05)
            logger.info(
                f"XtQuantManager 服务已启动: "
                f"{'https' if self.config.use_tls else 'http'}://"
                f"{self.config.host}:{self.config.port}"
            )

    def stop(self, timeout: float = 5.0) -> None:
        """优雅停止服务"""
        self._running = False

        # 停止止盈止损监控
        if self._stop_profit_monitor is not None:
            self._stop_profit_monitor.stop(timeout=timeout)

        # 停止健康监控
        if self._health_monitor is not None:
            self._health_monitor.stop(timeout=timeout)
            self._health_monitor = None

        # 停止 uvicorn
        if self._server is not None:
            self._server.should_exit = True

        if self._server_thread is not None:
            self._server_thread.join(timeout=timeout)
            if self._server_thread.is_alive():
                logger.warning("XtQuantServer 线程未在超时内退出")
            self._server_thread = None

        logger.info("XtQuantManager 服务已停止")

    def is_running(self) -> bool:
        return (
            self._running
            and self._server_thread is not None
            and self._server_thread.is_alive()
        )

    @property
    def stop_profit_monitor(self) -> Optional[StopProfitMonitor]:
        return self._stop_profit_monitor

    @staticmethod
    def _format_dependency_error(exc: ImportError) -> str:
        missing = getattr(exc, "name", "") or str(exc)
        return (
            "需要安装 XtQuantManager HTTP 依赖: "
            "pip install -r utils/requirements.txt "
            f"(缺少 {missing})"
        )

    def _cleanup_startup_failure(self) -> None:
        """启动早期失败时清理已启动的后台组件。"""
        self._running = False

        if self._stop_profit_monitor is not None:
            try:
                self._stop_profit_monitor.stop(timeout=1.0)
            except Exception as e:
                logger.warning(f"清理止盈止损监控时出错: {e}")
            self._stop_profit_monitor = None

        if self._health_monitor is not None:
            try:
                self._health_monitor.stop(timeout=1.0)
            except Exception as e:
                logger.warning(f"清理健康监控时出错: {e}")
            self._health_monitor = None

        if self._server is not None:
            try:
                self._server.should_exit = True
            except Exception:
                pass

        if self._server_thread is not None:
            if self._server_thread.is_alive():
                self._server_thread.join(timeout=1.0)
            self._server_thread = None

    def _run_uvicorn(self) -> None:
        """在当前线程中运行 uvicorn"""
        try:
            import uvicorn

            ssl_kwargs = {}
            if self.config.use_tls:
                ssl_kwargs["ssl_certfile"] = self.config.ssl_certfile
                ssl_kwargs["ssl_keyfile"] = self.config.ssl_keyfile

            uvicorn_config = uvicorn.Config(
                app=self._app,
                host=self.config.host,
                port=self.config.port,
                log_level="warning",
                **ssl_kwargs,
            )
            self._server = uvicorn.Server(uvicorn_config)
            self._server.run()

        except ImportError as e:
            self._startup_error = self._format_dependency_error(e)
            logger.error(self._startup_error)
        except Exception as e:
            self._startup_error = f"uvicorn 运行出错: {e}"
            logger.error(self._startup_error)
        finally:
            self._running = False
