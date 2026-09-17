"""
FastAPI 路由定义

所有路由都是对 XtQuantManager 的薄包装，业务逻辑在 manager 层。
安全层（IP 白名单、速率限制、token 验证）通过中间件和 Depends 实现。
"""
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
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
from order_utils import (
    ORDER_TYPE_BUY,
    format_order_time,
    is_pending as order_is_pending,
    sort_orders,
    status_desc as order_status_desc,
)

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
        _register_missing_web_ui_root(app)
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


def _register_missing_web_ui_root(app: FastAPI) -> None:
    """web2.0 未构建时为根路径提供明确诊断页，避免默认 404。"""

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def web_ui_missing():
        return HTMLResponse(
            """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>XtQuantManager 已启动</title>
  <style>
    body { margin: 0; font-family: "Microsoft YaHei", Arial, sans-serif; background: #f6f8fb; color: #18212f; }
    main { max-width: 760px; margin: 10vh auto; padding: 32px; background: #fff; border: 1px solid #d8dee8; border-radius: 8px; }
    h1 { margin: 0 0 16px; font-size: 26px; }
    p { line-height: 1.7; }
    code { background: #eef2f7; padding: 2px 6px; border-radius: 4px; }
    a { color: #0b65c2; }
  </style>
</head>
<body>
  <main>
    <h1>XtQuantManager HTTP 服务已启动</h1>
    <p>当前未找到 <code>web2.0/dist/index.html</code>，所以网页界面尚未构建。</p>
    <p>API 可正常访问：<a href="/api/v1/health">/api/v1/health</a>，
       接口文档：<a href="/docs">/docs</a>。</p>
    <p>如需启用 web2.0 界面，请在项目目录运行
       <code>cd web2.0</code>、<code>npm install</code>、<code>npm run build</code>，
       然后重启 XtQuantManager。</p>
  </main>
</body>
</html>"""
        )


def _make_token_verifier(security_config: SecurityConfig):
    """创建 token 验证 Depends"""
    api_key_header = APIKeyHeader(name="X-API-Token", auto_error=False)

    async def verify_token(
        request: Request,
        token: Optional[str] = Depends(api_key_header),
    ) -> str:
        client_ip = _get_client_ip(request, security_config.trust_proxy)
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


def _get_client_ip(request: Request, trust_proxy: bool = False) -> str:
    """委托给 security._get_client_ip，避免两份实现各自演进。"""
    from .security import _get_client_ip as _shared_get_client_ip
    return _shared_get_client_ip(request, trust_proxy)


def _has_valid_token(request: Request, security_config: SecurityConfig) -> bool:
    """非抛异常版的鉴权判定，供需要按权限裁剪返回内容的端点使用。"""
    ok, _reason = verify_api_key(
        token=request.headers.get("X-API-Token") or "",
        expected=security_config.api_token,
        client_ip=_get_client_ip(request, security_config.trust_proxy),
        local_ips=security_config.local_ips,
    )
    return ok


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
        request: Request,
        manager: XtQuantManager = Depends(_get_manager),
    ):
        """获取账号健康状态（免 token 可达，供存活探测/连接测试使用）。

        免 token 是刻意保留的：前端"测试连接"和客户端 connect() 在用户
        尚未配置 token 时就要调用它。但 accounts 明细含账号 ID，
        与受保护的 /api/accounts 是同一份数据，全公开会架空那层保护。
        因此未携带有效 token 时只返回聚合计数，不返回账号明细。
        """
        states = manager.get_all_states()
        data = {
            "total": len(states),
            "healthy": sum(1 for s in states.values() if s.get("connected")),
        }
        if _has_valid_token(request, security_config):
            data["accounts"] = states
        else:
            data["accounts"] = {}
        return ApiResponse(
            success=True,
            data=data,
        )

    @app.get(
        "/api/v1/health/{account_id}",
        response_model=ApiResponse,
        tags=["可观测性"],
    )
    async def health_account(
        account_id: str,
        _: str = Depends(verify_token),
        manager: XtQuantManager = Depends(_get_manager),
    ):
        """获取指定账号健康状态（含账号明细，需 token）"""
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
    async def stop_profit_status(request: Request, _: str = Depends(verify_token)):
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
    async def stop_profit_config(request: Request, _: str = Depends(verify_token)):
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
    async def stop_profit_toggle(request: Request, enabled: bool = True, _: str = Depends(verify_token)):
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

    def _account_db_path(aid: str) -> str:
        """账号级 SQLite 路径 data_<aid>/trading.db。"""
        import os as _os
        return _os.path.normpath(
            _os.path.join(_os.path.dirname(__file__), "..", f"data_{aid}", "trading.db")
        )

    def _load_sqlite_enrichment(aid: str) -> dict:
        """从 data_<aid>/trading.db 读取持久化的持仓元数据。

        position_manager 每 15 秒将内存数据同步到 SQLite，包含：
        stock_name / open_date / stop_loss_price / profit_triggered / highest_price
        / base_cost_price / stop_profit_enabled 等精确的策略计算值，远优于手工估算。

        Returns:
            {stock_code: {...}}，读取失败返回 {}。
        """
        import sqlite3
        import os as _os
        db_path = _account_db_path(aid)
        if not _os.path.exists(db_path):
            return {}
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            # 用 SELECT * 而非显式列名：旧库缺少 base_cost_price /
            # stop_profit_enabled 时不至于整条查询失败、退化成空字典
            rows = conn.execute("SELECT * FROM positions").fetchall()
            conn.close()
            result = {}
            for r in rows:
                cols = r.keys()

                def _get(key, default=None):
                    return r[key] if key in cols else default

                result[r["stock_code"]] = {
                    "stock_name": _get("stock_name") or "",
                    "open_date": _get("open_date") or "",
                    "stop_loss_price": _get("stop_loss_price") or 0,
                    "profit_triggered": bool(_get("profit_triggered")),
                    "highest_price": _get("highest_price") or 0,
                    "base_cost_price": _get("base_cost_price") or 0,
                    # 该列默认值为 1；旧库缺列时按"开启"处理，与 position_manager 一致
                    "stop_profit_enabled": bool(_get("stop_profit_enabled", 1)),
                }
            return result
        except Exception:
            return {}

    def _normalize_stock_code(code: str) -> str:
        """股票代码归一化为 6 位裸代码，用于跨接口匹配 003025 / 003025.SZ。"""
        return str(code or "").strip().split(".")[0]

    def _inject_sqlite_meta(raw: list, aid: str) -> None:
        """把 SQLite 持久化元数据与网格活跃标记就地注入 QMT 持仓 dict。"""
        sqlite = _load_sqlite_enrichment(aid)
        active_grid_codes = _active_grid_codes(aid)
        for p in raw:
            code = p.get("证券代码", "")
            enr = sqlite.get(code, {})
            p["_sqlite_name"]                 = enr.get("stock_name", "")
            p["_sqlite_open_date"]            = enr.get("open_date", "")
            p["_sqlite_stop_loss_price"]      = enr.get("stop_loss_price", 0)
            p["_sqlite_profit_triggered"]     = enr.get("profit_triggered", False)
            p["_sqlite_highest_price"]        = enr.get("highest_price", 0)
            p["_sqlite_base_cost_price"]      = enr.get("base_cost_price", 0)
            p["_sqlite_stop_profit_enabled"]  = enr.get("stop_profit_enabled", True)
            p["_grid_session_active"]         = _normalize_stock_code(code) in active_grid_codes

    def _load_grid_sessions_from_sqlite(aid: str) -> list:
        """从 data_<aid>/trading.db 读取网格会话，供 Flask 兼容端点使用。"""
        import sqlite3
        import os as _os
        db_path = _os.path.join(_os.path.dirname(__file__), "..", f"data_{aid}", "trading.db")
        db_path = _os.path.normpath(db_path)
        if not _os.path.exists(db_path):
            return []
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM grid_trading_sessions "
                "ORDER BY CASE WHEN status='active' THEN 0 ELSE 1 END, "
                "start_time DESC, id DESC"
            ).fetchall()
            conn.close()
            return [dict(row) for row in rows]
        except Exception:
            return []

    def _active_grid_codes(aid: str) -> set:
        """读取活跃网格股票集合，使用裸代码避免后缀差异导致误判。"""
        return {
            _normalize_stock_code(s.get("stock_code"))
            for s in _load_grid_sessions_from_sqlite(aid)
            if str(s.get("status") or "").lower() in ("active", "stopping")
        }

    def _grid_session_to_compat(row: dict) -> dict:
        """SQLite 网格会话 → web2.0 兼容字段。"""
        total_buy = float(row.get("total_buy_amount") or 0)
        total_sell = float(row.get("total_sell_amount") or 0)
        grid_profit = total_sell - total_buy
        denominator = float(row.get("max_investment") or 0)
        profit_ratio = grid_profit / denominator if denominator else 0
        return {
            "session_id": row.get("id"),
            "stock_code": row.get("stock_code") or "",
            "status": row.get("status") or "",
            "enabled": bool(row.get("enabled", 1)),
            "center_price": row.get("center_price") or 0,
            "current_center_price": row.get("current_center_price") or row.get("center_price") or 0,
            "trade_count": row.get("trade_count") or 0,
            "buy_count": row.get("buy_count") or 0,
            "sell_count": row.get("sell_count") or 0,
            "profit_ratio": profit_ratio,
            "grid_profit": grid_profit,
            "pnl_snapshot": {
                "profit_ratio": profit_ratio,
                "total_pnl": grid_profit,
                "realized_pnl": grid_profit,
                "unrealized_pnl": 0,
                "method": "cash_flow_legacy",
                "is_degraded": True,
                "denominator": denominator,
                "denominator_type": "max_investment",
            },
            "current_investment": row.get("current_investment") or 0,
            "max_investment": row.get("max_investment") or 0,
            "deviation_ratio": 0,
            "start_time": row.get("start_time"),
            "end_time": row.get("end_time"),
            "stop_time": row.get("stop_time"),
            "stop_reason": row.get("stop_reason"),
        }

    def _load_trade_records_from_sqlite(aid: str) -> list:
        """从 data_<aid>/trading.db 的 trade_records 表读取交易记录。

        与 web1.0 同源：position_manager/trading_executor 将买卖记录持久化到
        此表，含 名称/时间/策略/买卖历史，远优于 QMT 当日成交（缺名称/时间/策略、
        且只有当日）。按时间倒序返回，读取失败返回 []。
        """
        import sqlite3
        import os as _os
        db_path = _os.path.join(_os.path.dirname(__file__), "..", f"data_{aid}", "trading.db")
        db_path = _os.path.normpath(db_path)
        if not _os.path.exists(db_path):
            return []
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT stock_code, stock_name, trade_time, trade_type, price, "
                "volume, trade_id, strategy FROM trade_records ORDER BY trade_time DESC"
            ).fetchall()
            conn.close()
            result = []
            for r in rows:
                result.append({
                    "stock_code": r["stock_code"] or "",
                    "stock_name": r["stock_name"] or "",
                    "trade_type": r["trade_type"] or "",
                    "price": r["price"] or 0,
                    "volume": r["volume"] or 0,
                    "trade_time": r["trade_time"] or "--",
                    "trade_id": str(r["trade_id"] or ""),
                    "strategy": r["strategy"] or "",
                })
            return result
        except Exception:
            return []

    def _to_xt_code(code: str) -> str:
        """6 位证券代码补全市场后缀（xtdata get_full_tick 要求带后缀）。
        持仓接口返回的是 6 位裸代码，需按 A 股规则映射：
        5/6/9→.SH（上交所），0/2/3/15/16/18→.SZ（深交所）。
        已带后缀则原样返回。"""
        if not code or "." in code:
            return code
        if code.startswith("920"):
            return f"{code}.BJ"
        if code.startswith(("5", "6", "9")):
            return f"{code}.SH"
        if code.startswith(("0", "2", "3", "15", "16", "18")):
            return f"{code}.SZ"
        return code

    def _enrich_positions_with_tick(positions: list, manager) -> None:
        """批量获取全推行情，为每个持仓 dict 注入 _tick_change_pct。
        失败时字段为 0，不抛异常。"""
        # 持仓代码是 6 位裸码，get_full_tick 需带后缀，建立 裸码→后缀码 映射
        bare_to_xt = {p.get("证券代码", ""): _to_xt_code(p.get("证券代码", ""))
                      for p in positions if p.get("证券代码")}
        if not bare_to_xt:
            return
        tick = {}
        try:
            accounts = manager.list_accounts()
            if accounts:
                tick = manager.get_full_tick(accounts[0], list(bare_to_xt.values())) or {}
        except Exception:
            pass
        for p in positions:
            code = p.get("证券代码", "")
            q = tick.get(bare_to_xt.get(code, code), {})
            lp = q.get("lastPrice", 0) or 0
            lc = q.get("lastClose", 0) or 0
            p["_tick_change_pct"] = round(100 * (lp - lc) / lc, 2) if lc else 0

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
        name     = p.get("_sqlite_name") or p.get("证券名称") or p.get("stock_name") or ""
        open_dt  = p.get("_sqlite_open_date") or ""
        sl_price = p.get("_sqlite_stop_loss_price")
        trig     = p.get("_sqlite_profit_triggered", False)
        high_p   = p.get("_sqlite_highest_price")
        base_cp  = p.get("_sqlite_base_cost_price") or 0
        sp_on    = p.get("_sqlite_stop_profit_enabled", True)

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
            "change_percentage": p.get("_tick_change_pct", 0),
            "grid_session_active": bool(p.get("_grid_session_active", False)),
            # 补仓摊薄前的初次建仓成本；无记录时退回当前成本价
            "base_cost_price": base_cp or cost,
            "stop_profit_enabled": bool(sp_on),
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

    def _account_flask_url(aid: str):
        """推导该账号 Flask 实例的本机地址。

        端口规则与 config._apply_account_overrides 一致：WEB_SERVER_PORT + 账号在
        account_config.json 中的索引。

        账号不在配置列表时返回 None（探测放弃 → 状态显示为未知）。
        **不能**回落到默认 5000：那会读到另一个账号的状态并张冠李戴，
        正是"看似真实实则错误"的值，比未知更危险。
        """
        try:
            import config as _config
            accounts = _config.get_all_accounts_config() or []
            ids = [a.get("account_id", "") for a in accounts]
            if aid not in ids:
                return None
            base_port = getattr(_config, "WEB_SERVER_BASE_PORT",
                                getattr(_config, "WEB_SERVER_PORT", 5000))
            return "http://127.0.0.1:%d" % (base_port + ids.index(aid))
        except Exception:
            return None

    # 反向探测的短缓存：避免每次 /api/status 都打一次 Flask
    _flask_probe_cache = {}
    _FLASK_PROBE_TTL = 5.0
    _FLASK_PROBE_TIMEOUT = 1.0

    def _flask_probe_token() -> str:
        """获取 Flask /api/status 反向探测需要携带的 Web API Token。"""
        token = ""
        try:
            import config as _config
            token = getattr(_config, "WEB_API_TOKEN", "") or ""
        except Exception:
            token = ""
        if not token:
            try:
                import os as _os
                token = _os.environ.get("QMT_API_TOKEN", "") or ""
            except Exception:
                token = ""
        if not token:
            token = getattr(security_config, "api_token", "") or ""
        return str(token) if token else ""

    def _build_flask_status_request(url: str):
        token = _flask_probe_token()
        if not token:
            return url
        import urllib.request
        return urllib.request.Request(url, headers={"X-API-Token": token})

    def _probe_flask_settings(aid: str):
        """反向调用该账号的 Flask /api/status，取运行时内存态开关。

        ENABLE_AUTO_OPERATION / ENABLE_SIMULATION_MODE 按设计不持久化
        （见 config_manager.apply_configs_to_runtime 的注释：总闸每次启动
        需手动确认），只存在于主进程内存里。网关是独立进程，import config
        只会读到自己那份默认值 —— 那是个"看似真实实则错误"的值，
        比显示未知更危险。因此只能向主进程要。

        Flask 不可达时返回 None（调用方回落为"未知"），**绝不猜测**。
        注意 web2.0 启动模式下 launcher 会设 QMT_NO_FLASK=1 跳过 Flask，
        此时本探测必然失败，属预期行为。
        """
        import time as _time

        cached = _flask_probe_cache.get(aid)
        if cached and (_time.time() - cached[0]) < _FLASK_PROBE_TTL:
            return cached[1]

        result = None
        base = _account_flask_url(aid)
        if base:
            try:
                import urllib.request
                import json as _json
                req = _build_flask_status_request(base + "/api/status")
                with urllib.request.urlopen(
                    req, timeout=_FLASK_PROBE_TIMEOUT
                ) as resp:
                    body = _json.loads(resp.read().decode("utf-8"))
                if body.get("status") == "success":
                    result = body.get("settings") or {}
            except Exception:
                result = None  # 不可达/超时/格式异常 → 未知

        _flask_probe_cache[aid] = (_time.time(), result)
        return result

    def _load_account_settings(aid: str) -> dict:
        """获取账号真实的运行时开关状态。

        两个来源互补：

        1. **账号 SQLite** `system_config` — 已持久化的开关
           （ENABLE_AUTO_TRADING / ENABLE_GRID_TRADING / 买卖权限）
        2. **反向调用该账号 Flask** — 仅存在于主进程内存、不持久化的开关
           （ENABLE_AUTO_OPERATION 总闸、ENABLE_SIMULATION_MODE、持仓监控线程）

        绝不硬编码 True —— 监控界面显示假的"自动ON"比不显示更危险。
        两个来源都拿不到就返回 None，由前端展示为"未知"。
        """
        import json as _json
        import sqlite3
        import os as _os

        unknown = {
            "isMonitoring": None,
            "enableAutoTrading": None,
            "enableGridTrading": None,
            "positionMonitorRunning": None,
            "allowBuy": None,
            "allowSell": None,
            "simulationMode": None,
        }

        # ── 来源 2：主进程内存态（优先，最新且含不持久化项）──
        live = _probe_flask_settings(aid)

        def _live(key):
            if not live or key not in live or live[key] is None:
                return None
            return bool(live[key])

        # ── 来源 1：SQLite 持久化配置 ──
        raw = {}
        db_path = _account_db_path(aid)
        if _os.path.exists(db_path):
            try:
                conn = sqlite3.connect(db_path)
                rows = conn.execute(
                    "SELECT config_key, config_value FROM system_config"
                ).fetchall()
                conn.close()
                for key, value in rows:
                    try:
                        raw[key] = _json.loads(value)
                    except Exception:
                        raw[key] = value
            except Exception:
                raw = {}

        if not raw and live is None:
            return unknown

        def _flag(key):
            return bool(raw[key]) if key in raw else None

        def _pick(live_key, db_key=None):
            """内存态优先（可能被运行时修改过），回落到持久化值。"""
            v = _live(live_key)
            if v is not None:
                return v
            return _flag(db_key) if db_key else None

        return {
            # 只存在于主进程内存，SQLite 无此项 → 拿不到就是未知
            "isMonitoring": _live("isMonitoring"),
            "simulationMode": _live("simulationMode"),
            "positionMonitorRunning": _live("positionMonitorRunning"),
            "enableAutoTrading": _pick("enableAutoTrading", "ENABLE_AUTO_TRADING"),
            "enableGridTrading": _pick("enableGridTrading", "ENABLE_GRID_TRADING"),
            "allowBuy": _pick("allowBuy", "ENABLE_ALLOW_BUY"),
            "allowSell": _pick("allowSell", "ENABLE_ALLOW_SELL"),
        }

    @app.get("/api/status", tags=["兼容"])
    async def flask_status(request: Request, _: str = Depends(verify_token)):
        """Flask 兼容: /api/status → 返回指定账号的状态（顶层字段格式）"""
        aid = _get_request_account_id(request)
        if not aid:
            return JSONResponse({"status": "error", "error": "无已注册账号"})
        try:
            asset = _get_manager().query_asset(aid)
            settings = _load_account_settings(aid)
            return JSONResponse({
                "status": "success",
                "isMonitoring": settings["isMonitoring"],
                "account": {
                    "id": aid,
                    "availableBalance": asset.get("可用金额", 0),
                    "maxHoldingValue": asset.get("持仓市值", 0),
                    "totalAssets": asset.get("总资产", 0),
                    "timestamp": "",
                },
                "settings": settings,
            })
        except Exception:
            raise HTTPException(status_code=404, detail=f"账号不存在: {aid}")

    @app.get("/api/positions", tags=["兼容"])
    async def flask_positions(request: Request, version: int = -1, _: str = Depends(verify_token)):
        """Flask 兼容: /api/positions（字段映射为前端英文格式，顶层字段格式）"""
        aid = _get_request_account_id(request)
        if not aid:
            return JSONResponse({"status": "error", "error": "无已注册账号"})
        try:
            raw = _get_manager().query_positions(aid)
            _inject_sqlite_meta(raw, aid)
            _enrich_positions_with_tick(raw, _get_manager())
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
    async def flask_positions_all(request: Request, version: int = 0, _: str = Depends(verify_token)):
        """Flask 兼容: /api/positions-all"""
        aid = _get_request_account_id(request)
        if not aid:
            return JSONResponse({"status": "success", "data": [], "data_version": 0, "no_change": False})
        try:
            raw = _get_manager().query_positions(aid)
            _inject_sqlite_meta(raw, aid)
            _enrich_positions_with_tick(raw, _get_manager())
            positions = [_map_position_to_flask(p) for p in raw]
            return JSONResponse({
                "status": "success",
                "data": positions,
                "data_version": 0,
                "no_change": False,
            })
        except Exception:
            return JSONResponse({"status": "success", "data": [], "data_version": 0, "no_change": False})

    @app.get("/api/accounts", tags=["兼容"])
    async def flask_list_accounts(_: str = Depends(verify_token)):
        """Flask 兼容: /api/accounts —— 列出账号 ID 列表。

        与 v1 /api/v1/accounts 相同数据。早期为了让"互联网只读用户"免 token
        拿到账号列表而不加保护，但账号 ID 是遍历其他账号数据的入口，
        且同组只读端点会返回持仓/成本/盈亏，等同于把财务数据公开。
        现与其他兼容端点一致要求 token；前端 discoverAccounts 已带 token
        并有 fallback，无需改动。
        """
        accounts = _get_manager().list_accounts()
        return JSONResponse({"status": "success", "success": True, "data": {"accounts": accounts}})

    @app.get("/api/connection/status", tags=["兼容"])
    async def flask_connection_status(request: Request, _: str = Depends(verify_token)):
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
    async def flask_config(request: Request, _: str = Depends(verify_token)):
        """Flask 兼容: /api/config → 从账号 SQLite 读取真实已持久化的参数。

        早期实现返回一组写死的默认值（35000/5.0/7.0...），监控界面因此
        展示的是与后端无关的假参数。现在只回真实值，读不到的键返回 None，
        由前端渲染为"--"。
        """
        import json as _json
        import sqlite3
        import os as _os

        aid = _get_request_account_id(request)
        raw = {}
        if aid:
            db_path = _account_db_path(aid)
            if _os.path.exists(db_path):
                try:
                    conn = sqlite3.connect(db_path)
                    rows = conn.execute(
                        "SELECT config_key, config_value FROM system_config"
                    ).fetchall()
                    conn.close()
                    for key, value in rows:
                        try:
                            raw[key] = _json.loads(value)
                        except Exception:
                            raw[key] = value
                except Exception:
                    raw = {}

        def _num(key, scale=1.0, absolute=False):
            if key not in raw:
                return None
            try:
                v = float(raw[key]) * scale
            except (TypeError, ValueError):
                return None
            return abs(v) if absolute else v

        def _flag(key):
            return bool(raw[key]) if key in raw else None

        # 不持久化的开关只能向主进程要（带 5 秒缓存，不会每次都打 Flask）
        _settings = _load_account_settings(aid) if aid else {}

        # BUY_GRID_LEVEL_1 存的是比例系数(如 0.95)，前端展示的是跌幅百分比
        stop_loss_buy = None
        if "BUY_GRID_LEVEL_1" in raw:
            try:
                stop_loss_buy = abs(float(raw["BUY_GRID_LEVEL_1"]) - 1) * 100
            except (TypeError, ValueError):
                stop_loss_buy = None

        return JSONResponse({
            "status": "success",
            "data": {
                "singleBuyAmount": _num("POSITION_UNIT"),
                "firstProfitSell": _num("INITIAL_TAKE_PROFIT_RATIO", 100),
                "firstProfitSellEnabled": _flag("ENABLE_DYNAMIC_STOP_PROFIT"),
                "stockGainSellPencent": _num("INITIAL_TAKE_PROFIT_RATIO_PERCENTAGE", 100),
                "allowBuy": _flag("ENABLE_ALLOW_BUY"),
                "allowSell": _flag("ENABLE_ALLOW_SELL"),
                "stopLossBuy": stop_loss_buy,
                "stopLossBuyEnabled": _flag("ENABLE_STOP_LOSS_BUY"),
                "stockStopLoss": _num("STOP_LOSS_RATIO", 100, absolute=True),
                "singleStockMaxPosition": _num("MAX_POSITION_VALUE"),
                "totalMaxPosition": _num("MAX_TOTAL_POSITION_RATIO", 1000000),
                "globalAllowBuySell": _flag("ENABLE_AUTO_TRADING"),
                "globalAllowGridTrading": _flag("ENABLE_GRID_TRADING"),
                # 这两项不持久化，只能向主进程 Flask 取；不可达时为 None(未知)
                "globalAutoOperation": _settings.get("isMonitoring"),
                "simulationMode": _settings.get("simulationMode"),
            },
            "ranges": {},
        })

    @app.get("/api/macd/advice", tags=["兼容"])
    async def flask_macd_advice(request: Request, _: str = Depends(verify_token)):
        """Flask 兼容: /api/macd/advice → MACD 操盘建议。"""
        try:
            import macd_advisor
            code = request.query_params.get("code") or macd_advisor.SHENZHEN_INDEX_CODE
            return JSONResponse(macd_advisor.get_advice(code))
        except Exception as exc:
            logger.error(f"获取MACD操盘建议时出错: {exc}")
            return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)

    @app.get("/api/trade-records", tags=["兼容"])
    async def flask_trade_records(request: Request, _: str = Depends(verify_token)):
        """Flask 兼容: /api/trade-records（与 web1.0 同源：优先读 SQLite trade_records）"""
        aid = _get_request_account_id(request)
        if not aid:
            return JSONResponse({"status": "success", "data": []})
        # 优先从 SQLite trade_records 读取（含 名称/时间/策略/买卖历史，与 web1.0 一致）
        records = _load_trade_records_from_sqlite(aid)
        if records:
            return JSONResponse({"status": "success", "data": records})
        # 降级：SQLite 无记录时回退 QMT 当日成交/委托（缺名称/时间/策略）
        trades = _get_manager().query_trades(aid)
        if not trades:
            trades = _get_manager().query_orders(aid)
        mapped = [_map_trade_to_flask(t) for t in (trades or [])]
        return JSONResponse({"status": "success", "data": mapped})

    @app.get("/api/grid/sessions", tags=["兼容"])
    async def flask_grid_sessions(request: Request, _: str = Depends(verify_token)):
        """Flask 兼容: /api/grid/sessions（网关模式下从 SQLite 返回会话状态）。"""
        aid = _get_request_account_id(request)
        if not aid:
            return JSONResponse({"status": "success", "success": True, "sessions": [], "total": 0})
        sessions = [_grid_session_to_compat(row) for row in _load_grid_sessions_from_sqlite(aid)]
        return JSONResponse({
            "status": "success",
            "success": True,
            "sessions": sessions,
            "total": len(sessions),
        })

    @app.get("/api/orders", tags=["兼容"])
    async def flask_orders(request: Request, _: str = Depends(verify_token)):
        """Flask 兼容: /api/orders → 当日委托（含在途未成交）。

        监控视图靠它感知"已报未成交"的挂单——止盈卖单在成交前不会进入
        trade_records，仅看持仓和成交记录是察觉不到的。
        """
        aid = _get_request_account_id(request)
        if not aid:
            return JSONResponse({"status": "success", "data": []})
        try:
            raw = _get_manager().query_orders(aid) or []
        except Exception:
            return JSONResponse({"status": "success", "data": []})

        names = {
            code: meta.get("stock_name", "")
            for code, meta in _load_sqlite_enrichment(aid).items()
        }

        orders = []
        for o in raw:
            status = o.get("委托状态", o.get("order_status", 0)) or 0
            code = o.get("证券代码") or o.get("stock_code") or ""
            order_type = o.get("委托类型", o.get("order_type", ORDER_TYPE_BUY))
            orders.append({
                "order_id": str(o.get("订单编号") or o.get("order_id") or ""),
                "stock_code": code,
                "stock_name": names.get(code) or code,
                "trade_type": "BUY" if order_type == ORDER_TYPE_BUY else "SELL",
                "price": o.get("委托价格", o.get("price", 0)) or 0,
                "volume": o.get("委托数量", o.get("order_volume", 0)) or 0,
                "traded_volume": o.get("成交数量", o.get("traded_volume", 0)) or 0,
                "status": status,
                "status_desc": order_status_desc(status, o.get("状态描述", "")),
                "is_pending": order_is_pending(status),
                "order_time": format_order_time(o.get("报单时间")),
                "strategy": o.get("策略名称") or "",
            })

        sort_orders(orders)
        return JSONResponse({"status": "success", "data": orders})

    @app.get("/api/grid/ledger/{session_id}", tags=["兼容"])
    async def flask_grid_ledger(request: Request, session_id: int,
                                limit: int = 50, offset: int = 0,
                                _: str = Depends(verify_token)):
        """Flask 兼容: /api/grid/ledger/<id> → 网格真实账本（只读）。

        直接对账号 SQLite 执行只读查询，不实例化 GridDatabase——后者的
        __init__ 会建表，监控端不应写入被监控账号的数据库。
        SQL 与 grid_database._get_grid_ledger_summary_unlocked 保持一致。
        """
        import sqlite3
        import os as _os

        aid = _get_request_account_id(request)
        empty = {"success": False, "status": "error", "error": "账本数据不可用"}
        if not aid:
            return JSONResponse(empty)

        db_path = _account_db_path(aid)
        if not _os.path.exists(db_path):
            return JSONResponse(empty)

        limit = min(max(limit or 50, 1), 500)
        offset = max(offset or 0, 0)

        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                session_row = conn.execute(
                    "SELECT * FROM grid_trading_sessions WHERE id=?", (session_id,)
                ).fetchone()
                if session_row is None:
                    return JSONResponse(
                        {"success": False, "status": "error",
                         "error": f"会话{session_id}不存在"},
                        status_code=404,
                    )
                session = dict(session_row)
                current_price = session.get("current_center_price") or session.get("center_price")

                lot_row = dict(conn.execute("""
                    SELECT COUNT(*) AS lot_count,
                           COALESCE(SUM(original_volume), 0) AS bought_volume,
                           COALESCE(SUM(remaining_volume), 0) AS open_volume,
                           COALESCE(SUM(remaining_volume * buy_price), 0) AS open_cost
                    FROM grid_lots WHERE session_id=?
                """, (session_id,)).fetchone())

                match_row = dict(conn.execute("""
                    SELECT COUNT(*) AS match_count,
                           COALESCE(SUM(CASE WHEN match_type='matched'
                                        THEN volume ELSE 0 END), 0) AS matched_volume,
                           COALESCE(SUM(CASE WHEN match_type='unmatched'
                                        THEN volume ELSE 0 END), 0) AS unmatched_volume,
                           COALESCE(SUM(CASE WHEN match_type='matched'
                                        THEN realized_pnl ELSE 0 END), 0) AS realized_pnl
                    FROM grid_lot_matches WHERE session_id=?
                """, (session_id,)).fetchone())

                lots = [dict(r) for r in conn.execute(
                    "SELECT * FROM grid_lots WHERE session_id=? ORDER BY opened_at ASC, id ASC",
                    (session_id,)).fetchall()]
                matches = [dict(r) for r in conn.execute(
                    "SELECT * FROM grid_lot_matches WHERE session_id=? "
                    "ORDER BY matched_at ASC, id ASC", (session_id,)).fetchall()]
                trades = [dict(r) for r in conn.execute(
                    "SELECT * FROM grid_trades WHERE session_id=? "
                    "ORDER BY trade_time DESC, id DESC LIMIT ? OFFSET ?",
                    (session_id, limit, offset)).fetchall()]
                total_count = conn.execute(
                    "SELECT COUNT(*) FROM grid_trades WHERE session_id=?",
                    (session_id,)).fetchone()[0]
            finally:
                conn.close()
        except Exception as exc:
            logger.error(f"读取网格账本失败: {exc}")
            return JSONResponse(empty)

        price = float(current_price) if current_price else None
        open_volume = float(lot_row["open_volume"] or 0)
        open_cost = float(lot_row["open_cost"] or 0.0)
        open_market_value = open_volume * price if price and price > 0 else 0.0
        unrealized_pnl = open_market_value - open_cost
        realized_pnl = float(match_row["realized_pnl"] or 0.0)

        summary = {
            "has_ledger": bool(lot_row["lot_count"] or match_row["match_count"]),
            "lot_count": int(lot_row["lot_count"] or 0),
            "match_count": int(match_row["match_count"] or 0),
            "bought_volume": int(lot_row["bought_volume"] or 0),
            "open_volume": int(open_volume),
            "matched_volume": int(match_row["matched_volume"] or 0),
            "unmatched_volume": int(match_row["unmatched_volume"] or 0),
            "open_cost": open_cost,
            "open_market_value": open_market_value,
            "realized_pnl": realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "true_pnl": realized_pnl + unrealized_pnl,
        }

        session["session_id"] = session.get("id")
        return JSONResponse({
            "success": True,
            "status": "success",
            "session_id": session_id,
            "session": session,
            "current_price": price,
            "summary": summary,
            "lots": lots,
            "matches": matches,
            "trades": trades,
            "total_count": total_count,
            "pagination": {
                "limit": limit,
                "offset": offset,
                "has_more": offset + len(trades) < total_count,
            },
        })
