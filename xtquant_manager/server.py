"""
FastAPI 路由定义

所有路由都是对 XtQuantManager 的薄包装，业务逻辑在 manager 层。
安全层（IP 白名单、速率限制、token 验证）通过中间件和 Depends 实现。
"""
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import APIKeyHeader

from .exceptions import (
    AccountNotFoundError,
    XtQuantCallError,
    XtQuantTimeoutError,
)
from .manager import XtQuantManager
from .models import (
    AccountStatusResponse,
    ApiResponse,
    CancelOrderResponse,
    DownloadHistoryRequest,
    HealthResponse,
    MetricsResponse,
    OrderRequest,
    OrderResponse,
    RegisterAccountRequest,
)
from .account import AccountConfig
from .security import SecurityConfig, verify_api_key

try:
    from logger import get_logger
    logger = get_logger("xqm_server")
except Exception:
    import logging
    logger = logging.getLogger("xtquant_manager.server")


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------

def create_app(security_config: Optional[SecurityConfig] = None) -> FastAPI:
    """
    创建 FastAPI 应用。

    Args:
        security_config: 安全配置，None 使用默认（本机访问，无 token）

    Returns:
        FastAPI 应用实例
    """
    if security_config is None:
        security_config = SecurityConfig()

    app = FastAPI(
        title="XtQuantManager API",
        description="miniQMT xtquant 接口统一管理层",
        version="1.0.0",
    )

    # 注册安全中间件
    from .security import create_security_middleware
    app.add_middleware(create_security_middleware(security_config))

    # CORS：允许本地 HTML 文件（file://）和常用本地开发地址访问
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 将安全配置存入 app.state，供路由访问
    app.state.security_config = security_config

    # 注册路由
    _register_routes(app, security_config)

    # —— 托管 web2.0 前端（如果已构建） ——
    _mount_web_ui(app)

    return app


def _mount_web_ui(app: FastAPI) -> None:
    """将 web2.0/dist/ 挂载为静态站点（如果存在）。
    用户启动 xtquant_manager 后可直接访问 http://host:port/ 使用 web2.0 界面。
    """
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    dist_dir = root / "web2.0" / "dist"

    if not dist_dir.is_dir() or not (dist_dir / "index.html").exists():
        logger.info("web2.0 未构建，跳过 web2.0 界面。运行 cd web2.0 && npm run build 后可用。")
        return

    # 挂载静态资源（JS/CSS/图标等）
    assets_dir = dist_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="web2_assets")

    # SPA fallback: 非 /api/ 路径返回 index.html（API 路由已先注册，优先级更高）
    import re
    _api_pattern = re.compile(r"^/api/")
    _asset_pattern = re.compile(r"^/assets/")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        if _api_pattern.match(f"/{full_path}"):
            raise HTTPException(status_code=404, detail="Not found")
        if _asset_pattern.match(f"/{full_path}"):
            raise HTTPException(status_code=404, detail="Not found")
        file_path = dist_dir / full_path
        if file_path.is_file():
            return FileResponse(str(file_path))
        return FileResponse(str(dist_dir / "index.html"))

    # 首页
    @app.get("/")
    async def serve_index():
        return FileResponse(str(dist_dir / "index.html"))

    logger.info("web2.0 界面已就绪 — 访问根路径即可使用")


def _make_token_verifier(security_config: SecurityConfig):
    """创建 token 验证 Depends"""
    api_key_header = APIKeyHeader(name="X-API-Token", auto_error=False)

    async def verify_token(
        request: Request,
        token: Optional[str] = Depends(api_key_header),
    ) -> str:
        client_ip = _get_client_ip(request)
        ok, reason = verify_api_key(
            token=token or "",
            expected=security_config.api_token,
            client_ip=client_ip,
            local_ips=security_config.local_ips,
        )
        if not ok:
            raise HTTPException(status_code=401, detail=f"认证失败: {reason}")
        return token or ""

    return verify_token


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _get_manager() -> XtQuantManager:
    return XtQuantManager.get_instance()


def _register_routes(app: FastAPI, security_config: SecurityConfig):
    """注册所有路由"""
    verify_token = _make_token_verifier(security_config)

    # ------------------------------------------------------------------
    # 账号管理
    # ------------------------------------------------------------------

    @app.post(
        "/api/v1/accounts",
        response_model=ApiResponse,
        status_code=201,
        tags=["账号管理"],
    )
    async def register_account(
        req: RegisterAccountRequest,
        _: str = Depends(verify_token),
        manager: XtQuantManager = Depends(_get_manager),
    ):
        """注册并连接账号"""
        config = AccountConfig(
            account_id=req.account_id,
            qmt_path=req.qmt_path,
            account_type=req.account_type,
            session_id=req.session_id,
            call_timeout=req.call_timeout,
            reconnect_base_wait=req.reconnect_interval,
            max_reconnect_attempts=req.max_reconnect_attempts,
        )
        connected = manager.register_account(config)
        return ApiResponse(
            success=True,
            data={
                "account_id": req.account_id,
                "connected": connected,
                "message": "注册成功" if connected else "注册成功但连接失败，可稍后重试",
            },
        )

    @app.delete(
        "/api/v1/accounts/{account_id}",
        response_model=ApiResponse,
        tags=["账号管理"],
    )
    async def unregister_account(
        account_id: str,
        _: str = Depends(verify_token),
        manager: XtQuantManager = Depends(_get_manager),
    ):
        """断开并注销账号"""
        removed = manager.unregister_account(account_id)
        if not removed:
            raise HTTPException(status_code=404, detail=f"账号不存在: {account_id}")
        return ApiResponse(success=True, data={"account_id": account_id})

    @app.get(
        "/api/v1/accounts",
        response_model=ApiResponse,
        tags=["账号管理"],
    )
    async def list_accounts(
        _: str = Depends(verify_token),
        manager: XtQuantManager = Depends(_get_manager),
    ):
        """列出所有已注册账号"""
        accounts = manager.list_accounts()
        return ApiResponse(success=True, data={"accounts": accounts})

    @app.get(
        "/api/v1/accounts/{account_id}/status",
        response_model=ApiResponse,
        tags=["账号管理"],
    )
    async def get_account_status(
        account_id: str,
        _: str = Depends(verify_token),
        manager: XtQuantManager = Depends(_get_manager),
    ):
        """获取账号连接状态"""
        try:
            state = manager.get_account_state(account_id)
            return ApiResponse(success=True, data=state)
        except AccountNotFoundError:
            raise HTTPException(status_code=404, detail=f"账号不存在: {account_id}")

    # ------------------------------------------------------------------
    # 交易操作
    # ------------------------------------------------------------------

    @app.post(
        "/api/v1/accounts/{account_id}/orders",
        response_model=ApiResponse,
        status_code=201,
        tags=["交易操作"],
    )
    async def create_order(
        account_id: str,
        req: OrderRequest,
        _: str = Depends(verify_token),
        manager: XtQuantManager = Depends(_get_manager),
    ):
        """下单"""
        try:
            order_id = manager.order_stock(
                account_id=account_id,
                stock_code=req.stock_code,
                order_type=req.order_type,
                order_volume=req.order_volume,
                price_type=req.price_type,
                price=req.price,
                strategy_name=req.strategy_name,
                order_remark=req.order_remark,
            )
            if order_id < 0:
                return ApiResponse(success=False, error="下单失败，请检查账号状态")
            return ApiResponse(success=True, data={"order_id": order_id})
        except AccountNotFoundError:
            raise HTTPException(status_code=404, detail=f"账号不存在: {account_id}")

    @app.delete(
        "/api/v1/accounts/{account_id}/orders/{order_id}",
        response_model=ApiResponse,
        tags=["交易操作"],
    )
    async def cancel_order(
        account_id: str,
        order_id: int,
        _: str = Depends(verify_token),
        manager: XtQuantManager = Depends(_get_manager),
    ):
        """撤单"""
        try:
            result = manager.cancel_order(account_id, order_id)
            return ApiResponse(success=True, data={"result": result, "order_id": order_id})
        except AccountNotFoundError:
            raise HTTPException(status_code=404, detail=f"账号不存在: {account_id}")

    @app.get(
        "/api/v1/accounts/{account_id}/positions",
        response_model=ApiResponse,
        tags=["交易操作"],
    )
    async def get_positions(
        account_id: str,
        _: str = Depends(verify_token),
        manager: XtQuantManager = Depends(_get_manager),
    ):
        """查询持仓"""
        try:
            positions = manager.query_positions(account_id)
            return ApiResponse(success=True, data={"positions": positions})
        except AccountNotFoundError:
            raise HTTPException(status_code=404, detail=f"账号不存在: {account_id}")

    @app.get(
        "/api/v1/accounts/{account_id}/asset",
        response_model=ApiResponse,
        tags=["交易操作"],
    )
    async def get_asset(
        account_id: str,
        _: str = Depends(verify_token),
        manager: XtQuantManager = Depends(_get_manager),
    ):
        """查询账户资产"""
        try:
            asset = manager.query_asset(account_id)
            return ApiResponse(success=True, data=asset)
        except AccountNotFoundError:
            raise HTTPException(status_code=404, detail=f"账号不存在: {account_id}")

    @app.get(
        "/api/v1/accounts/{account_id}/orders",
        response_model=ApiResponse,
        tags=["交易操作"],
    )
    async def get_orders(
        account_id: str,
        _: str = Depends(verify_token),
        manager: XtQuantManager = Depends(_get_manager),
    ):
        """查询当日委托"""
        try:
            orders = manager.query_orders(account_id)
            return ApiResponse(success=True, data={"orders": orders})
        except AccountNotFoundError:
            raise HTTPException(status_code=404, detail=f"账号不存在: {account_id}")

    @app.get(
        "/api/v1/accounts/{account_id}/trades",
        response_model=ApiResponse,
        tags=["交易操作"],
    )
    async def get_trades(
        account_id: str,
        _: str = Depends(verify_token),
        manager: XtQuantManager = Depends(_get_manager),
    ):
        """查询当日成交"""
        try:
            trades = manager.query_trades(account_id)
            return ApiResponse(success=True, data={"trades": trades})
        except AccountNotFoundError:
            raise HTTPException(status_code=404, detail=f"账号不存在: {account_id}")

    # ------------------------------------------------------------------
    # 行情操作
    # ------------------------------------------------------------------

    @app.get(
        "/api/v1/market/tick",
        response_model=ApiResponse,
        tags=["行情操作"],
    )
    async def get_tick(
        stock_codes: str,  # 逗号分隔，如 "000001.SZ,600036.SH"
        account_id: str,
        _: str = Depends(verify_token),
        manager: XtQuantManager = Depends(_get_manager),
    ):
        """获取全推行情"""
        try:
            codes = [c.strip() for c in stock_codes.split(",") if c.strip()]
            tick_data = manager.get_full_tick(account_id, codes)
            return ApiResponse(success=True, data=tick_data)
        except AccountNotFoundError:
            raise HTTPException(status_code=404, detail=f"账号不存在: {account_id}")

    @app.get(
        "/api/v1/market/history",
        response_model=ApiResponse,
        tags=["行情操作"],
    )
    async def get_history(
        stock_code: str,
        account_id: str,
        period: str = "1d",
        start_time: str = "20200101",
        end_time: str = "",
        _: str = Depends(verify_token),
        manager: XtQuantManager = Depends(_get_manager),
    ):
        """获取历史行情"""
        try:
            data = manager.get_market_data_ex(
                account_id=account_id,
                fields=[],
                stock_list=[stock_code],
                period=period,
                start_time=start_time,
                end_time=end_time,
            )
            return ApiResponse(success=True, data=data)
        except AccountNotFoundError:
            raise HTTPException(status_code=404, detail=f"账号不存在: {account_id}")

    @app.post(
        "/api/v1/market/download",
        response_model=ApiResponse,
        tags=["行情操作"],
    )
    async def download_history(
        req: DownloadHistoryRequest,
        _: str = Depends(verify_token),
        manager: XtQuantManager = Depends(_get_manager),
    ):
        """下载历史数据到本地"""
        try:
            success = manager.download_history_data(
                account_id=req.account_id,
                stock_code=req.stock_code,
                period=req.period,
                start_time=req.start_time,
                end_time=req.end_time,
            )
            return ApiResponse(success=success)
        except AccountNotFoundError:
            raise HTTPException(status_code=404, detail=f"账号不存在: {req.account_id}")

    # ------------------------------------------------------------------
    # 可观测性
    # ------------------------------------------------------------------

    @app.get(
        "/api/v1/health",
        response_model=ApiResponse,
        tags=["可观测性"],
    )
    async def health(
        manager: XtQuantManager = Depends(_get_manager),
    ):
        """获取所有账号健康状态（无需认证，供监控系统使用）"""
        states = manager.get_all_states()
        return ApiResponse(
            success=True,
            data={
                "accounts": states,
                "total": len(states),
                "healthy": sum(1 for s in states.values() if s.get("connected")),
            },
        )

    @app.get(
        "/api/v1/health/{account_id}",
        response_model=ApiResponse,
        tags=["可观测性"],
    )
    async def health_account(
        account_id: str,
        manager: XtQuantManager = Depends(_get_manager),
    ):
        """获取指定账号健康状态"""
        try:
            state = manager.get_account_state(account_id)
            return ApiResponse(success=True, data=state)
        except AccountNotFoundError:
            raise HTTPException(status_code=404, detail=f"账号不存在: {account_id}")

    @app.get(
        "/api/v1/metrics",
        response_model=ApiResponse,
        tags=["可观测性"],
    )
    async def metrics(
        _: str = Depends(verify_token),
        manager: XtQuantManager = Depends(_get_manager),
    ):
        """获取所有账号调用指标"""
        all_metrics = manager.get_all_metrics()
        return ApiResponse(success=True, data=all_metrics)

    @app.get(
        "/api/v1/metrics/{account_id}",
        response_model=ApiResponse,
        tags=["可观测性"],
    )
    async def metrics_account(
        account_id: str,
        _: str = Depends(verify_token),
        manager: XtQuantManager = Depends(_get_manager),
    ):
        """获取指定账号调用指标"""
        try:
            m = manager.get_account_metrics(account_id)
            return ApiResponse(success=True, data=m)
        except AccountNotFoundError:
            raise HTTPException(status_code=404, detail=f"账号不存在: {account_id}")

    # ------------------------------------------------------------------
    # 止盈止损策略 API
    # ------------------------------------------------------------------

    @app.get(
        "/api/v1/stop-profit/status",
        response_model=ApiResponse,
        tags=["止盈止损"],
    )
    async def stop_profit_status(request: Request):
        """获取止盈止损监控状态和各持仓跟踪数据"""
        mon = getattr(request.app.state, "stop_profit_monitor", None)
        if mon is None:
            return ApiResponse(success=True, data={"running": False, "reason": "未启用"})
        cfg = mon.get_config()
        return ApiResponse(success=True, data={
            "running": mon.is_running,
            "config": {
                "enabled": cfg.enabled,
                "stop_loss_ratio": cfg.stop_loss_ratio,
                "initial_take_profit_ratio": cfg.initial_take_profit_ratio,
                "initial_take_profit_pullback_ratio": cfg.initial_take_profit_pullback_ratio,
                "initial_take_profit_sell_ratio": cfg.initial_take_profit_sell_ratio,
                "monitor_interval": cfg.monitor_interval,
                "signal_dedup_seconds": cfg.signal_dedup_seconds,
            },
            "positions": mon.get_states(),
        })

    @app.post(
        "/api/v1/stop-profit/config",
        response_model=ApiResponse,
        tags=["止盈止损"],
    )
    async def stop_profit_config(request: Request):
        """更新止盈止损配置（JSON body）"""
        mon = getattr(request.app.state, "stop_profit_monitor", None)
        if mon is None:
            raise HTTPException(status_code=400, detail="止盈止损监控未启动")

        try:
            body = await request.json()
        except Exception:
            body = {}

        cfg = mon.get_config()
        if "enabled" in body:
            cfg.enabled = bool(body["enabled"])
        if "stop_loss_ratio" in body:
            cfg.stop_loss_ratio = float(body["stop_loss_ratio"])
        if "initial_take_profit_ratio" in body:
            cfg.initial_take_profit_ratio = float(body["initial_take_profit_ratio"])
        if "initial_take_profit_pullback_ratio" in body:
            cfg.initial_take_profit_pullback_ratio = float(body["initial_take_profit_pullback_ratio"])
        if "initial_take_profit_sell_ratio" in body:
            cfg.initial_take_profit_sell_ratio = float(body["initial_take_profit_sell_ratio"])
        if "monitor_interval" in body:
            cfg.monitor_interval = float(body["monitor_interval"])
        mon.update_config(cfg)
        return ApiResponse(success=True, data={"message": "配置已更新"})

    @app.post(
        "/api/v1/stop-profit/toggle",
        response_model=ApiResponse,
        tags=["止盈止损"],
    )
    async def stop_profit_toggle(request: Request, enabled: bool = True):
        """启用/停止止盈止损监控"""
        mon = getattr(request.app.state, "stop_profit_monitor", None)
        if mon is None:
            raise HTTPException(status_code=400, detail="止盈止损监控未启动，请在配置中设置 enable_stop_profit=true 并重启服务。")
        cfg = mon.get_config()
        cfg.enabled = enabled
        mon.update_config(cfg)
        return ApiResponse(success=True, data={"enabled": enabled, "message": "已启用" if enabled else "已暂停"})

    # ------------------------------------------------------------------
    # Flask web1.0 兼容端点 — 让 web2.0 前端在 xtquant_manager 上也能运行
    # ------------------------------------------------------------------

    def _first_account_id():
        """获取第一个已注册账号 ID，用于兼容端点。"""
        ids = _get_manager().list_accounts()
        return ids[0] if ids else None

    @app.get("/api/status", response_model=ApiResponse, tags=["兼容"])
    async def flask_status():
        """Flask 兼容: /api/status → 返回首个账号的状态"""
        aid = _first_account_id()
        if not aid:
            return ApiResponse(success=False, error="无已注册账号")
        try:
            state = _get_manager().get_account_state(aid)
            asset = _get_manager().query_asset(aid)
            return ApiResponse(success=True, data={
                "status": "success",
                "isMonitoring": True,
                "account": {
                    "id": aid,
                    "availableBalance": asset.get("可用金额", 0),
                    "maxHoldingValue": asset.get("持仓市值", 0),
                    "totalAssets": asset.get("总资产", 0),
                    "timestamp": "",
                },
                "settings": {
                    "isMonitoring": True,
                    "enableAutoTrading": True,
                    "positionMonitorRunning": True,
                    "allowBuy": True,
                    "allowSell": True,
                    "simulationMode": False,
                },
            })
        except Exception:
            raise HTTPException(status_code=404, detail=f"账号不存在: {aid}")

    @app.get("/api/positions", response_model=ApiResponse, tags=["兼容"])
    async def flask_positions(version: int = -1):
        """Flask 兼容: /api/positions"""
        aid = _first_account_id()
        if not aid:
            return ApiResponse(success=False, error="无已注册账号")
        try:
            positions = _get_manager().query_positions(aid)
            return ApiResponse(success=True, data={
                "positions": positions,
                "metrics": {},
                "positions_all": positions,
                "data_version": 0,
                "no_change": False,
            })
        except Exception:
            return ApiResponse(success=True, data={"positions": [], "metrics": {}, "positions_all": [], "data_version": 0, "no_change": False})

    @app.get("/api/positions-all", response_model=ApiResponse, tags=["兼容"])
    async def flask_positions_all(version: int = 0):
        """Flask 兼容: /api/positions-all"""
        return await flask_positions(version=version)

    @app.get("/api/connection/status", response_model=ApiResponse, tags=["兼容"])
    async def flask_connection_status():
        """Flask 兼容: /api/connection/status"""
        aid = _first_account_id()
        if not aid:
            return ApiResponse(success=True, data={"status": "success", "connected": False})
        state = _get_manager().get_account_state(aid)
        return ApiResponse(success=True, data={
            "status": "success",
            "connected": state.get("connected", False),
            "timestamp": "",
        })

    @app.get("/api/config", response_model=ApiResponse, tags=["兼容"])
    async def flask_config():
        """Flask 兼容: /api/config → 返回默认配置"""
        return ApiResponse(success=True, data={
            "singleBuyAmount": 35000,
            "firstProfitSell": 5.0, "firstProfitSellEnabled": True,
            "stockGainSellPencent": 60.0, "firstProfitSellPencent": True,
            "allowBuy": True, "allowSell": True,
            "stopLossBuy": 5.0, "stopLossBuyEnabled": True,
            "stockStopLoss": 7.0, "StopLossEnabled": True,
            "singleStockMaxPosition": 70000, "totalMaxPosition": 400000,
            "globalAllowBuySell": True, "simulationMode": False,
        }, ranges={})

    @app.get("/api/trade-records", response_model=ApiResponse, tags=["兼容"])
    async def flask_trade_records():
        """Flask 兼容: /api/trade-records"""
        aid = _first_account_id()
        if not aid:
            return ApiResponse(success=True, data={"status": "success", "data": []})
        orders = _get_manager().query_orders(aid)
        trades = _get_manager().query_trades(aid)
        return ApiResponse(success=True, data={"status": "success", "data": trades or orders})
