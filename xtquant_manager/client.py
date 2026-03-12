"""
XtQuantClient — HTTP 客户端，接口兼容 easy_qmt_trader

通过 HTTP 与 XtQuantManager 服务通信，暴露与 easy_qmt_trader 相同的方法签名。
失败时返回空 DataFrame/dict，不抛异常（与现有行为一致）。

Usage:
    from xtquant_manager.client import XtQuantClient

    client = XtQuantClient(
        base_url="http://127.0.0.1:8888",
        account_id="55009640",
        api_token="",          # 局域网访问需设置
    )

    # 与 easy_qmt_trader 完全兼容
    positions = client.position()      # -> pd.DataFrame
    asset = client.balance()           # -> pd.DataFrame
    order_id = client.order_stock(...) # -> int
"""
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    import pandas as pd
    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False

try:
    from logger import get_logger
    logger = get_logger("xqm_client")
except Exception:
    import logging
    logger = logging.getLogger("xtquant_manager.client")

# 持仓 DataFrame 列名（与 easy_qmt_trader.position() 兼容）
_POSITION_COLUMNS = [
    "账号类型", "资金账号", "证券代码", "股票余额", "可用余额",
    "成本价", "市值", "选择", "持股天数", "交易状态", "明细",
    "证券名称", "冻结数量", "市价", "盈亏", "盈亏比(%)",
    "当日买入", "当日卖出",
]

# 资产 DataFrame 列名（与 easy_qmt_trader.balance() 兼容）
_BALANCE_COLUMNS = [
    "账号类型", "资金账户", "可用金额", "冻结金额", "持仓市值", "总资产",
]


@dataclass
class ClientConfig:
    """客户端配置"""
    base_url: str = "http://127.0.0.1:8888"
    account_id: str = ""
    api_token: str = ""
    timeout: float = 5.0          # 请求超时（秒）
    max_retries: int = 2           # 最大重试次数（连接错误时）
    retry_delay: float = 0.5       # 重试间隔（秒）
    verify_ssl: bool = False       # 是否验证 SSL 证书（自签证书时设为 False）
    ca_cert: str = ""              # CA 证书路径（自签证书验证）


class XtQuantClient:
    """
    XtQuantManager HTTP 客户端。

    接口与 easy_qmt_trader 兼容：
    - position()           -> pd.DataFrame（持仓）
    - balance()            -> pd.DataFrame（资产）
    - order_stock(...)     -> int（订单 ID，失败返回 -1）
    - buy(...)             -> int
    - sell(...)            -> int
    - cancel_order_stock() -> int（0=成功，非0=失败）
    - query_stock_orders() -> pd.DataFrame
    - query_stock_trades() -> pd.DataFrame
    - query_stock_asset()  -> dict

    额外接口（xtdata）：
    - get_full_tick(codes)            -> dict
    - get_market_data_ex(...)         -> dict
    - download_history_data(...)      -> bool
    """

    def __init__(self, config: ClientConfig = None, **kwargs):
        """
        初始化客户端。

        Args:
            config: ClientConfig 配置对象
            **kwargs: 直接传递配置参数（快速初始化）
                base_url, account_id, api_token, timeout, max_retries, verify_ssl
        """
        if config is None:
            config = ClientConfig(**{k: v for k, v in kwargs.items()
                                     if hasattr(ClientConfig, k) or k in ClientConfig.__dataclass_fields__})
        self.config = config

        # 延迟导入 httpx，避免在不需要时强制依赖
        try:
            import httpx
            self._httpx = httpx
        except ImportError:
            self._httpx = None
            logger.warning("httpx 未安装，XtQuantClient 不可用。请运行: pip install httpx")

        self._session = None
        self._base = config.base_url.rstrip("/")
        self._headers = {}
        if config.api_token:
            self._headers["X-API-Token"] = config.api_token

    # ------------------------------------------------------------------
    # 会话管理
    # ------------------------------------------------------------------

    def _get_session(self):
        """获取或创建 httpx.Client 会话"""
        if self._session is None:
            if self._httpx is None:
                raise ImportError("httpx 未安装")
            ssl_context = None
            if self.config.verify_ssl and self.config.ca_cert:
                ssl_context = self.config.ca_cert
            self._session = self._httpx.Client(
                timeout=self.config.timeout,
                verify=ssl_context if ssl_context else self.config.verify_ssl,
            )
        return self._session

    def close(self):
        """关闭 HTTP 会话"""
        if self._session is not None:
            self._session.close()
            self._session = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ------------------------------------------------------------------
    # 底层 HTTP 方法
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> Optional[dict]:
        """
        发送 HTTP 请求，自动重试。

        Returns:
            响应 JSON（dict），失败返回 None
        """
        if self._httpx is None:
            logger.error("httpx 未安装，无法发送请求")
            return None

        url = f"{self._base}{path}"
        headers = dict(self._headers)

        for attempt in range(self.config.max_retries + 1):
            try:
                session = self._get_session()
                resp = session.request(method, url, headers=headers, **kwargs)
                resp.raise_for_status()
                return resp.json()
            except self._httpx.ConnectError as e:
                if attempt < self.config.max_retries:
                    logger.warning(f"连接失败，{self.config.retry_delay}s 后重试 ({attempt+1}/{self.config.max_retries}): {e}")
                    time.sleep(self.config.retry_delay)
                    self._session = None  # 重置会话
                else:
                    logger.error(f"请求失败（连接错误）: {method} {path}: {e}")
                    return None
            except self._httpx.TimeoutException as e:
                logger.warning(f"请求超时: {method} {path}: {e}")
                return None
            except self._httpx.HTTPStatusError as e:
                logger.warning(f"HTTP 错误: {method} {path}: {e.response.status_code}")
                return None
            except Exception as e:
                logger.error(f"请求异常: {method} {path}: {e}")
                return None

    def _get(self, path: str, params: dict = None) -> Optional[dict]:
        return self._request("GET", path, params=params)

    def _post(self, path: str, json: dict = None) -> Optional[dict]:
        return self._request("POST", path, json=json)

    def _delete(self, path: str) -> Optional[dict]:
        return self._request("DELETE", path)

    def _account_path(self, suffix: str = "") -> str:
        return f"/api/v1/accounts/{self.config.account_id}{suffix}"

    # ------------------------------------------------------------------
    # easy_qmt_trader 兼容接口
    # ------------------------------------------------------------------

    def position(self):
        """
        查询持仓，兼容 easy_qmt_trader.position()。

        Returns:
            pd.DataFrame，含与 easy_qmt_trader 相同的列名。
            失败返回含列名的空 DataFrame。
        """
        empty = _empty_df(_POSITION_COLUMNS)
        resp = self._get(self._account_path("/positions"))
        if resp is None or not resp.get("success"):
            return empty
        positions = resp.get("data", {}).get("positions", [])
        if not positions:
            return empty
        if not _HAS_PANDAS:
            return positions  # 无 pandas 时退化为 list[dict]
        return _to_df(positions, _POSITION_COLUMNS)

    def balance(self):
        """
        查询账户资产，兼容 easy_qmt_trader.balance()。

        Returns:
            pd.DataFrame（单行），含 账号类型、资金账户、可用金额、冻结金额、持仓市值、总资产。
            失败返回空 DataFrame。
        """
        empty = _empty_df(_BALANCE_COLUMNS)
        resp = self._get(self._account_path("/asset"))
        if resp is None or not resp.get("success"):
            return empty
        asset = resp.get("data", {})
        if not asset:
            return empty
        if not _HAS_PANDAS:
            return asset
        return _to_df([asset], _BALANCE_COLUMNS)

    def order_stock(
        self,
        stock_code: str,
        order_type: int,
        order_volume: int,
        price_type: int,
        price: float,
        strategy_name: str = "",
        order_remark: str = "",
    ) -> int:
        """
        下单，兼容 easy_qmt_trader.order_stock()。

        Returns:
            int: 订单 ID（>0），失败返回 -1。
        """
        resp = self._post(self._account_path("/orders"), json={
            "stock_code": stock_code,
            "order_type": order_type,
            "order_volume": order_volume,
            "price_type": price_type,
            "price": price,
            "strategy_name": strategy_name,
            "order_remark": order_remark,
        })
        if resp is None or not resp.get("success"):
            return -1
        return resp.get("data", {}).get("order_id", -1)

    def buy(
        self,
        security: str,
        order_type: int = 23,       # xtconstant.STOCK_BUY
        amount: int = 100,
        price_type: int = 11,       # xtconstant.FIX_PRICE
        price: float = 0.0,
        strategy_name: str = "",
        order_remark: str = "",
    ) -> int:
        """买入，兼容 easy_qmt_trader.buy()"""
        return self.order_stock(
            stock_code=security,
            order_type=order_type,
            order_volume=amount,
            price_type=price_type,
            price=price,
            strategy_name=strategy_name,
            order_remark=order_remark,
        )

    def sell(
        self,
        security: str,
        order_type: int = 24,       # xtconstant.STOCK_SELL
        amount: int = 100,
        price_type: int = 11,       # xtconstant.FIX_PRICE
        price: float = 0.0,
        strategy_name: str = "",
        order_remark: str = "",
    ) -> int:
        """卖出，兼容 easy_qmt_trader.sell()"""
        return self.order_stock(
            stock_code=security,
            order_type=order_type,
            order_volume=amount,
            price_type=price_type,
            price=price,
            strategy_name=strategy_name,
            order_remark=order_remark,
        )

    def cancel_order_stock(self, order_id: int) -> int:
        """
        撤单，兼容 easy_qmt_trader.cancel_order_stock()。

        Returns:
            int: 0=成功，非0=失败。
        """
        resp = self._delete(self._account_path(f"/orders/{order_id}"))
        if resp is None or not resp.get("success"):
            return -1
        return resp.get("data", {}).get("result", -1)

    def query_stock_asset(self) -> dict:
        """
        查询资产，兼容 easy_qmt_trader.query_stock_asset()。

        Returns:
            dict，含 账号类型、资金账户、可用金额、冻结金额、持仓市值、总资产。
            失败返回空 dict。
        """
        resp = self._get(self._account_path("/asset"))
        if resp is None or not resp.get("success"):
            return {}
        return resp.get("data", {})

    def query_stock_orders(self):
        """
        查询当日委托，兼容 easy_qmt_trader.query_stock_orders()。

        Returns:
            pd.DataFrame，含委托信息列。失败返回空 DataFrame。
        """
        resp = self._get(self._account_path("/orders"))
        if resp is None or not resp.get("success"):
            return _empty_df([])
        orders = resp.get("data", {}).get("orders", [])
        if not orders:
            return _empty_df([])
        if not _HAS_PANDAS:
            return orders
        return _to_df(orders)

    def query_stock_trades(self):
        """
        查询当日成交，兼容 easy_qmt_trader.query_stock_trades()。

        Returns:
            pd.DataFrame，含成交信息列。失败返回空 DataFrame。
        """
        resp = self._get(self._account_path("/trades"))
        if resp is None or not resp.get("success"):
            return _empty_df([])
        trades = resp.get("data", {}).get("trades", [])
        if not trades:
            return _empty_df([])
        if not _HAS_PANDAS:
            return trades
        return _to_df(trades)

    # ------------------------------------------------------------------
    # 行情接口（扩展 xtdata 兼容）
    # ------------------------------------------------------------------

    def get_full_tick(self, stock_codes: List[str]) -> dict:
        """
        获取全推行情。

        Args:
            stock_codes: 股票代码列表

        Returns:
            dict {code: tick_data}，失败返回空 dict。
        """
        codes_str = ",".join(stock_codes)
        resp = self._get("/api/v1/market/tick", params={
            "stock_codes": codes_str,
            "account_id": self.config.account_id,
        })
        if resp is None or not resp.get("success"):
            return {}
        return resp.get("data", {})

    def get_market_data_ex(
        self,
        fields: list,
        stock_list: List[str],
        period: str = "1d",
        start_time: str = "20200101",
        end_time: str = "",
    ) -> dict:
        """
        获取历史行情数据。

        Returns:
            dict，失败返回空 dict。
        """
        params = {
            "stock_code": stock_list[0] if stock_list else "",
            "account_id": self.config.account_id,
            "period": period,
            "start_time": start_time,
        }
        if end_time:
            params["end_time"] = end_time
        resp = self._get("/api/v1/market/history", params=params)
        if resp is None or not resp.get("success"):
            return {}
        return resp.get("data", {})

    def download_history_data(
        self,
        stock_code: str,
        period: str = "1d",
        start_time: str = "20200101",
        end_time: str = "",
    ) -> bool:
        """
        下载历史数据到本地。

        Returns:
            bool: True=成功，False=失败。
        """
        resp = self._post("/api/v1/market/download", json={
            "account_id": self.config.account_id,
            "stock_code": stock_code,
            "period": period,
            "start_time": start_time,
            "end_time": end_time,
        })
        if resp is None:
            return False
        return resp.get("success", False)

    # ------------------------------------------------------------------
    # 可观测性接口
    # ------------------------------------------------------------------

    def health(self) -> dict:
        """获取服务健康状态"""
        resp = self._get("/api/v1/health")
        if resp is None:
            return {}
        return resp.get("data", {})

    def get_account_status(self) -> dict:
        """获取本账号连接状态"""
        resp = self._get(f"/api/v1/accounts/{self.config.account_id}/status")
        if resp is None or not resp.get("success"):
            return {}
        return resp.get("data", {})

    def get_metrics(self) -> dict:
        """获取本账号调用指标"""
        resp = self._get(f"/api/v1/metrics/{self.config.account_id}")
        if resp is None or not resp.get("success"):
            return {}
        return resp.get("data", {})

    def is_connected(self) -> bool:
        """快速检查账号是否在线"""
        state = self.get_account_status()
        return state.get("connected", False)

    # ------------------------------------------------------------------
    # easy_qmt_trader 生命周期兼容接口
    # ------------------------------------------------------------------

    def connect(self):
        """
        连接到 XtQuantManager 服务（兼容 easy_qmt_trader.connect()）。

        Returns:
            (self, self): 服务可达时（模拟 (xt_trader, acc) 元组）
            None: 服务不可达时
        """
        try:
            resp = self._get("/api/v1/health")
            if resp is not None:
                logger.info("XtQuantManager 服务连接成功")
                return (self, self)
        except Exception as e:
            logger.error(f"XtQuantManager 连接失败: {e}")
        return None

    def register_trade_callback(self, cb) -> None:
        """no-op：XtQuantManager 模式下成交回报通过轮询获取，无需注册回调"""
        logger.debug("register_trade_callback: XtQuantManager 模式下忽略（no-op）")

    def subscribe_callback(self) -> None:
        """no-op：兼容 easy_qmt_trader 接口"""
        logger.debug("subscribe_callback: XtQuantManager 模式下忽略（no-op）")


# ---------------------------------------------------------------------------
# 内部工具函数
# ---------------------------------------------------------------------------

def _empty_df(columns: List[str]):
    """返回含指定列名的空 DataFrame"""
    if not _HAS_PANDAS:
        return []
    import pandas as pd
    return pd.DataFrame(columns=columns)


def _to_df(records: List[dict], columns: List[str] = None):
    """将 list[dict] 转为 DataFrame，可选指定列顺序"""
    if not _HAS_PANDAS:
        return records
    import pandas as pd
    df = pd.DataFrame(records)
    if columns:
        # 只保留存在的列，按指定顺序排列
        existing = [c for c in columns if c in df.columns]
        extra = [c for c in df.columns if c not in columns]
        df = df[existing + extra]
    return df
