"""
Pydantic 请求/响应模型
"""
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 通用响应
# ---------------------------------------------------------------------------

class ApiResponse(BaseModel):
    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# 账号管理
# ---------------------------------------------------------------------------

class RegisterAccountRequest(BaseModel):
    account_id: str = Field(..., description="交易账号 ID")
    qmt_path: str = Field(..., description="QMT userdata_mini 路径")
    account_type: str = Field("STOCK", description="账户类型")
    session_id: Optional[int] = Field(None, description="会话编号，None=随机生成")
    call_timeout: float = Field(3.0, description="API 调用超时（秒）")
    reconnect_interval: float = Field(300.0, description="重连间隔（秒）")
    max_reconnect_attempts: int = Field(5, description="最大重连次数")


class AccountStatusResponse(BaseModel):
    account_id: str
    connected: bool
    reconnecting: bool
    reconnect_attempts: int
    last_ping_ok_time: Optional[float]
    connected_at: Optional[float]
    xtdata_available: bool
    xttrader_available: bool


# ---------------------------------------------------------------------------
# 交易操作
# ---------------------------------------------------------------------------

class OrderRequest(BaseModel):
    stock_code: str = Field(..., description="证券代码，如 000001.SZ")
    order_type: int = Field(..., description="委托类型：23=买入，24=卖出")
    order_volume: int = Field(..., gt=0, description="委托数量（股）")
    price_type: int = Field(11, description="报价类型：11=限价")
    price: float = Field(..., gt=0, description="委托价格")
    strategy_name: str = Field("", description="策略名称")
    order_remark: str = Field("", description="备注")


class OrderResponse(BaseModel):
    order_id: int


class CancelOrderResponse(BaseModel):
    result: int  # 0=成功，非0=失败


# ---------------------------------------------------------------------------
# 行情操作
# ---------------------------------------------------------------------------

class DownloadHistoryRequest(BaseModel):
    account_id: str
    stock_code: str
    period: str = Field("1d", description="周期：1d, 1h, 30m 等")
    start_time: str = Field("20200101", description="开始时间 YYYYMMDD")
    end_time: str = Field("", description="结束时间 YYYYMMDD，空=今天")


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------

class MetricsResponse(BaseModel):
    total_calls: int
    success_calls: int
    error_calls: int
    timeout_calls: int
    error_rate: float
    avg_latency_ms: float
    p50_latency_ms: float
    p95_latency_ms: float
    last_error_time: Optional[float]
    last_error_msg: str
    uptime_seconds: float
    ops: Dict[str, Dict[str, int]]


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    monitor_running: bool
    accounts: Dict[str, AccountStatusResponse]
