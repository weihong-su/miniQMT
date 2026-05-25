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

    def _get_request_account_id(request: Request):
        """从 X-Account-Id 请求头获取目标账号，fallback 到第一个注册账号。"""
        header_id = (request.headers.get("X-Account-Id") or "").strip()
        if header_id:
            ids = _get_manager().list_accounts()
            if header_id in ids:
                return header_id
        return _first_account_id()

    def _load_sqlite_enrichment(aid: str) -> dict:
        """从 data_<aid>/trading.db 读取持久化的持仓元数据。

        position_manager 每 15 秒将内存数据同步到 SQLite，包含：
        stock_name / open_date / stop_loss_price / profit_triggered / highest_price
        等精确的策略计算值，远优于手工估算。

        Returns:
            {stock_code: {stock_name, open_date, stop_loss_price,
                          profit_triggered, highest_price}}，读取失败返回 {}。
        """
        import sqlite3
        import os as _os
        db_path = _os.path.join(_os.path.dirname(__file__), "..", f"data_{aid}", "trading.db")
        db_path = _os.path.normpath(db_path)
        if not _os.path.exists(db_path):
            return {}
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT stock_code, stock_name, open_date, stop_loss_price, "
                "profit_triggered, highest_price FROM positions"
            ).fetchall()
            conn.close()
            result = {}
            for r in rows:
                result[r["stock_code"]] = {
                    "stock_name": r["stock_name"] or "",
                    "open_date": r["open_date"] or "",
                    "stop_loss_price": r["stop_loss_price"] or 0,
                    "profit_triggered": bool(r["profit_triggered"]),
                    "highest_price": r["highest_price"] or 0,
                }
            return result
        except Exception:
            return {}

    def _map_position_to_flask(p: dict) -> dict:
        """xtquant 中文字段持仓 + SQLite 持久化元数据 → Flask 英文字段。

        QMT 提供实时交易字段（量/价/市值），SQLite 提供策略元数据
        （名称/建仓日期/止损价/止盈触发/最高价），通过 p 中的 _sqlite_* 键合并。"""
        vol = p.get("股票余额", 0) or 0
        cost = p.get("成本价", 0) or 0
        mv = p.get("市值", 0) or 0
        cur = p.get("市价")
        if not cur and vol:
            cur = mv / vol
        cur = cur or 0
        profit_ratio = round(100 * (cur - cost) / cost, 2) if cost else 0  # 百分比，与 Flask 对齐
        code = p.get("证券代码", "")

        # SQLite 持久化元数据（由 _enrich_positions_from_sqlite 注入）
        name     = p.get("_sqlite_name") or ""
        open_dt  = p.get("_sqlite_open_date") or ""
        sl_price = p.get("_sqlite_stop_loss_price")
        trig     = p.get("_sqlite_profit_triggered", False)
        high_p   = p.get("_sqlite_highest_price")

        if sl_price is None or sl_price == 0:
            sl_price = round(cost * 0.925, 2)  # fallback: 与 STOP_LOSS_RATIO=-0.075 对齐
        if high_p is None or high_p == 0:
            high_p = cur

        return {
            "stock_code": code,
            "stock_name": name or code,
            "volume": vol,
            "available": p.get("可用余额", 0) or 0,
            "cost_price": cost,
            "current_price": cur,
            "market_value": mv,
            "profit_ratio": profit_ratio,
            "profit_amount": (cur - cost) * vol,
            "profit_triggered": trig,
            "highest_price": high_p,
            "stop_loss_price": sl_price,
            "open_date": (open_dt or "")[:10] or "--",
            "grid_session_active": False,
        }

    def _map_trade_to_flask(t: dict) -> dict:
        """xtquant 中文字段成交/委托 → Flask 英文字段交易记录。"""
        # 委托类型: 23=买入, 24=卖出（其余按奇偶兜底）
        order_type = t.get("委托类型", 0) or 0
        trade_type = "BUY" if order_type == 23 else ("SELL" if order_type == 24 else ("BUY" if order_type % 2 == 1 else "SELL"))
        return {
            "stock_code": t.get("证券代码", ""),
            "stock_name": t.get("证券名称") or "",
            "trade_type": trade_type,
            "price": t.get("成交价格") or t.get("委托价格") or 0,
            "volume": t.get("成交数量") or t.get("委托数量") or 0,
            "trade_time": t.get("成交时间") or "--",
            "trade_id": str(t.get("成交编号") or t.get("订单编号") or ""),
            "strategy": "manual",
        }

    @app.get("/api/status", tags=["兼容"])
    async def flask_status(request: Request):
        """Flask 兼容: /api/status → 返回指定账号的状态（顶层字段格式）"""
        aid = _get_request_account_id(request)
        if not aid:
            return JSONResponse({"status": "error", "error": "无已注册账号"})
        try:
            asset = _get_manager().query_asset(aid)
            return JSONResponse({
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

    @app.get("/api/positions", tags=["兼容"])
    async def flask_positions(request: Request, version: int = -1):
        """Flask 兼容: /api/positions（字段映射为前端英文格式，顶层字段格式）"""
        aid = _get_request_account_id(request)
        if not aid:
            return JSONResponse({"status": "error", "error": "无已注册账号"})
        try:
            raw = _get_manager().query_positions(aid)
            sqlite = _load_sqlite_enrichment(aid)
            # 将 SQLite 持久化字段注入到 QMT 持仓 dict 中
            for p in raw:
                code = p.get("证券代码", "")
                enr = sqlite.get(code, {})
                p["_sqlite_name"]              = enr.get("stock_name", "")
                p["_sqlite_open_date"]         = enr.get("open_date", "")
                p["_sqlite_stop_loss_price"]   = enr.get("stop_loss_price", 0)
                p["_sqlite_profit_triggered"]  = enr.get("profit_triggered", False)
                p["_sqlite_highest_price"]     = enr.get("highest_price", 0)
            positions = [_map_position_to_flask(p) for p in raw]
            total_mv = sum(p["market_value"] for p in positions)
            total_profit = sum(p["profit_amount"] for p in positions)
            total_cost = sum(p["cost_price"] * p["volume"] for p in positions)
            metrics = {
                "total_market_value": total_mv,
                "total_profit": total_profit,
                "total_profit_ratio": (total_profit / total_cost) if total_cost else 0,
                "position_count": len(positions),
                "stock_count": len(positions),
            }
            return JSONResponse({
                "status": "success",
                "data": {
                    "positions": positions,
                    "metrics": metrics,
                    "positions_all": positions,
                },
                "data_version": 0,
                "no_change": False,
            })
        except Exception:
            return JSONResponse({
                "status": "success",
                "data": {"positions": [], "metrics": {}, "positions_all": []},
                "data_version": 0,
                "no_change": False,
            })

    @app.get("/api/positions-all", tags=["兼容"])
    async def flask_positions_all(request: Request, version: int = 0):
        """Flask 兼容: /api/positions-all"""
        aid = _get_request_account_id(request)
        if not aid:
            return JSONResponse({"status": "success", "data": [], "data_version": 0, "no_change": False})
        try:
            raw = _get_manager().query_positions(aid)
            sqlite = _load_sqlite_enrichment(aid)
            for p in raw:
                code = p.get("证券代码", "")
                enr = sqlite.get(code, {})
                p["_sqlite_name"]              = enr.get("stock_name", "")
                p["_sqlite_open_date"]         = enr.get("open_date", "")
                p["_sqlite_stop_loss_price"]   = enr.get("stop_loss_price", 0)
                p["_sqlite_profit_triggered"]  = enr.get("profit_triggered", False)
                p["_sqlite_highest_price"]     = enr.get("highest_price", 0)
            positions = [_map_position_to_flask(p) for p in raw]
            return JSONResponse({
                "status": "success",
                "data": positions,
                "data_version": 0,
                "no_change": False,
            })
        except Exception:
            return JSONResponse({"status": "success", "data": [], "data_version": 0, "no_change": False})

    @app.get("/api/connection/status", tags=["兼容"])
    async def flask_connection_status(request: Request):
        """Flask 兼容: /api/connection/status（connected 为顶层字段）"""
        aid = _get_request_account_id(request)
        if not aid:
            return JSONResponse({"status": "success", "connected": False, "timestamp": ""})
        state = _get_manager().get_account_state(aid)
        return JSONResponse({
            "status": "success",
            "connected": state.get("connected", False),
            "timestamp": "",
        })

    @app.get("/api/config", tags=["兼容"])
    async def flask_config():
        """Flask 兼容: /api/config → 返回默认配置（data/ranges 为顶层字段）"""
        return JSONResponse({
            "status": "success",
            "data": {
                "singleBuyAmount": 35000,
                "firstProfitSell": 5.0, "firstProfitSellEnabled": True,
                "stockGainSellPencent": 60.0, "firstProfitSellPencent": True,
                "allowBuy": True, "allowSell": True,
                "stopLossBuy": 5.0, "stopLossBuyEnabled": True,
                "stockStopLoss": 7.0, "StopLossEnabled": True,
                "singleStockMaxPosition": 70000, "totalMaxPosition": 400000,
                "globalAllowBuySell": True, "simulationMode": False,
            },
            "ranges": {},
        })

    @app.get("/api/trade-records", tags=["兼容"])
    async def flask_trade_records(request: Request):
        """Flask 兼容: /api/trade-records（字段映射为前端英文格式，data 为顶层数组）"""
        aid = _get_request_account_id(request)
        if not aid:
            return JSONResponse({"status": "success", "data": []})
        trades = _get_manager().query_trades(aid)
        if not trades:
            trades = _get_manager().query_orders(aid)
        mapped = [_map_trade_to_flask(t) for t in (trades or [])]
        return JSONResponse({"status": "success", "data": mapped})
