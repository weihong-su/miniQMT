"""
安全层：IP 白名单 + API Key 认证 + 速率限制

局域网场景主要关注：
1. API Key 验证（X-API-Token 请求头）
2. IP 白名单（允许来源 IP 列表）
3. 速率限制（令牌桶，按 IP 独立计数）
4. HMAC 请求签名（预留接口，公网场景可选启用）

与 FastAPI 集成：
- SecurityMiddleware 作为 ASGI 中间件注册
- verify_api_token 作为 FastAPI Depends 使用
"""
import hashlib
import hmac
import time
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import List, Optional

try:
    from logger import get_logger
    logger = get_logger("xqm_sec")
except Exception:
    import logging
    logger = logging.getLogger("xtquant_manager.security")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class SecurityConfig:
    """安全配置"""
    api_token: str = ""                      # 空字符串 = 不验证（仅限本机）
    allowed_ips: List[str] = field(default_factory=list)   # 空列表 = 不限制
    rate_limit: int = 60                     # 每分钟每 IP 最大请求数（0=不限制）
    enable_hmac: bool = False                # 是否启用 HMAC 签名（公网可选）
    hmac_secret: str = ""                    # HMAC 密钥
    hmac_timestamp_tolerance: int = 300      # 时间戳容忍范围（秒）
    local_ips: List[str] = field(default_factory=lambda: [
        "127.0.0.1", "::1", "localhost"
    ])                                       # 本机 IP（始终允许）
    trust_proxy: bool = False                # 是否信任 X-Forwarded-For（仅在受信反代后开启）


# ---------------------------------------------------------------------------
# 速率限制（令牌桶）
# ---------------------------------------------------------------------------

class TokenBucket:
    """
    令牌桶速率限制器。

    - 每个 IP 拥有独立的桶
    - 每秒补充 rate/60 个令牌（rate=每分钟请求数）
    - 桶容量 = rate（允许短时突发）
    """

    def __init__(self, rate_per_minute: int):
        self._rate = rate_per_minute
        self._rate_per_second = rate_per_minute / 60.0
        self._lock = threading.Lock()
        # ip -> (tokens, last_refill_time)
        self._buckets: dict = defaultdict(lambda: [float(rate_per_minute), time.monotonic()])

    def allow(self, ip: str) -> bool:
        """检查 IP 是否允许本次请求，允许则消耗一个令牌。"""
        if self._rate == 0:
            return True

        with self._lock:
            bucket = self._buckets[ip]
            tokens, last_time = bucket

            # 补充令牌
            now = time.monotonic()
            elapsed = now - last_time
            tokens = min(float(self._rate), tokens + elapsed * self._rate_per_second)
            bucket[1] = now

            if tokens >= 1.0:
                bucket[0] = tokens - 1.0
                return True
            else:
                bucket[0] = tokens
                return False


# ---------------------------------------------------------------------------
# API Key 验证
# ---------------------------------------------------------------------------

def verify_api_key(token: str, expected: str, client_ip: str,
                   local_ips: List[str]) -> tuple:
    """
    验证 API Key。

    规则：
    - 本机 IP（127.0.0.1 等）始终允许，无需 token
    - 非本机访问：expected 为空时拒绝；非空时 token 必须匹配

    Args:
        token: 请求提供的 token
        expected: 配置中的期望 token
        client_ip: 客户端 IP
        local_ips: 本机 IP 列表

    Returns:
        (ok: bool, reason: str)
    """
    # 本机访问始终允许
    if client_ip in local_ips:
        return True, "本机访问"

    # 非本机访问：空 token 配置则拒绝
    if not expected:
        return False, "非本机访问需要配置 api_token"

    # 时间恒定比较，防止时序攻击
    ok = hmac.compare_digest(token or "", expected)
    if ok:
        return True, "token 验证通过"
    return False, "token 无效"


# ---------------------------------------------------------------------------
# HMAC 请求签名（公网扩展，预留接口）
# ---------------------------------------------------------------------------

def generate_hmac_signature(
    method: str,
    path: str,
    timestamp: int,
    body: str,
    secret: str,
) -> str:
    """
    生成 HMAC-SHA256 签名。

    签名内容: "{method}\\n{path}\\n{timestamp}\\n{body_hash}"

    Args:
        method: HTTP 方法（大写），如 "GET"
        path: 请求路径，如 "/api/v1/health"
        timestamp: Unix 时间戳（秒）
        body: 请求体字符串
        secret: HMAC 密钥

    Returns:
        十六进制签名字符串
    """
    body_hash = hashlib.sha256(body.encode()).hexdigest()
    message = f"{method}\n{path}\n{timestamp}\n{body_hash}"
    sig = hmac.new(secret.encode(), message.encode(), hashlib.sha256)
    return sig.hexdigest()


def generate_hmac_headers(
    method: str,
    path: str,
    secret: str,
    body: str = "",
) -> dict:
    """
    生成包含 HMAC 签名的请求头（供客户端使用）。

    Returns:
        {"X-Timestamp": "...", "X-Signature": "..."}
    """
    timestamp = int(time.time())
    signature = generate_hmac_signature(method, path, timestamp, body, secret)
    return {
        "X-Timestamp": str(timestamp),
        "X-Signature": signature,
    }


def verify_hmac_signature(
    method: str,
    path: str,
    timestamp_str: str,
    body: str,
    signature: str,
    secret: str,
    tolerance: int = 300,
) -> tuple:
    """
    验证 HMAC-SHA256 签名。

    Args:
        timestamp_str: 请求头中的时间戳字符串
        tolerance: 时间戳容忍范围（秒）

    Returns:
        (ok: bool, reason: str)
    """
    if not timestamp_str or not signature:
        return False, "缺少 X-Timestamp 或 X-Signature 请求头"

    try:
        timestamp = int(timestamp_str)
    except ValueError:
        return False, "X-Timestamp 格式无效"

    # 检查时间戳范围
    now = int(time.time())
    if abs(now - timestamp) > tolerance:
        return False, f"时间戳过期（差值 {abs(now - timestamp)}s，允许 {tolerance}s）"

    # 验证签名
    expected_sig = generate_hmac_signature(method, path, timestamp, body, secret)
    if hmac.compare_digest(signature, expected_sig):
        return True, "签名验证通过"
    return False, "签名不匹配"


# ---------------------------------------------------------------------------
# FastAPI 集成：中间件 + Depends
# ---------------------------------------------------------------------------

def create_security_middleware(config: SecurityConfig):
    """
    创建 FastAPI/Starlette SecurityMiddleware。

    Usage:
        from starlette.middleware.base import BaseHTTPMiddleware
        app.add_middleware(create_security_middleware(security_config))
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    rate_limiter = TokenBucket(config.rate_limit) if config.rate_limit > 0 else None

    class SecurityMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            client_ip = _get_client_ip(request, config.trust_proxy)

            # 1. IP 白名单检查
            if config.allowed_ips:
                if (client_ip not in config.allowed_ips
                        and client_ip not in config.local_ips):
                    logger.warning(f"IP 未授权: {client_ip}")
                    return JSONResponse(
                        {"success": False, "error": "IP 未授权"},
                        status_code=403,
                    )

            # 2. 速率限制
            if rate_limiter is not None:
                if not rate_limiter.allow(client_ip):
                    logger.warning(f"速率限制: {client_ip}")
                    return JSONResponse(
                        {"success": False, "error": "请求过于频繁"},
                        status_code=429,
                    )

            # 3. API Key（在路由层通过 Depends 验证，不在中间件中处理）
            # 中间件只做 IP + 速率限制，Token 在路由 Depends 中处理

            # 4. HMAC（可选）
            if config.enable_hmac and config.hmac_secret:
                # 跳过 /health 端点（心跳探测不需要签名）
                if not request.url.path.endswith("/health"):
                    body = await request.body()
                    timestamp_str = request.headers.get("X-Timestamp", "")
                    signature = request.headers.get("X-Signature", "")
                    ok, reason = verify_hmac_signature(
                        method=request.method,
                        path=request.url.path,
                        timestamp_str=timestamp_str,
                        body=body.decode(errors="replace"),
                        signature=signature,
                        secret=config.hmac_secret,
                        tolerance=config.hmac_timestamp_tolerance,
                    )
                    if not ok:
                        logger.warning(f"HMAC 验证失败: {reason}, IP={client_ip}")
                        return JSONResponse(
                            {"success": False, "error": f"签名验证失败: {reason}"},
                            status_code=401,
                        )

            return await call_next(request)

    return SecurityMiddleware


def create_token_verifier(config: SecurityConfig):
    """
    创建 FastAPI Depends 函数，用于验证 API Token。

    Usage:
        @app.get("/protected")
        async def protected(token: str = Depends(verify_token)):
            ...
    """
    from fastapi import Security, HTTPException
    from fastapi.security import APIKeyHeader

    api_key_header = APIKeyHeader(name="X-API-Token", auto_error=False)

    async def verify_token(
        request,
        token: Optional[str] = Security(api_key_header),
    ) -> str:
        client_ip = _get_client_ip(request, config.trust_proxy)
        ok, reason = verify_api_key(
            token=token or "",
            expected=config.api_token,
            client_ip=client_ip,
            local_ips=config.local_ips,
        )
        if not ok:
            raise HTTPException(status_code=401, detail=f"认证失败: {reason}")
        return token or ""

    return verify_token


def _get_client_ip(request, trust_proxy: bool = False) -> str:
    """从请求中获取客户端 IP。

    安全约束: X-Forwarded-For 由客户端完全可控，而 client_ip 会被用于
    local_ips 免 token 放行 / IP 白名单 / 速率限制三处判定。默认不信任该头，
    否则攻击者只需发送 X-Forwarded-For: 127.0.0.1 即可绕过全部三道防线。
    仅当部署在受信任反向代理之后（trust_proxy=True）时才读取。
    """
    if trust_proxy:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"
