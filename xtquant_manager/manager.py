"""
XtQuantManager — 多账号注册表 + 请求分发（单例）

职责：
- 维护多个 XtQuantAccount 实例的注册表
- 提供统一的请求分发接口
- 单例模式，全局唯一实例
"""
import threading
from typing import Dict, List, Optional

from .account import AccountConfig, XtQuantAccount
from .exceptions import AccountAlreadyExistsError, AccountNotFoundError

try:
    from logger import get_logger
    logger = get_logger("xqm_mgr")
except Exception:
    import logging
    logger = logging.getLogger("xtquant_manager.manager")


class XtQuantManager:
    """
    多账号管理器，线程安全单例。

    Usage:
        manager = XtQuantManager.get_instance()
        manager.register_account(AccountConfig(...))
        order_id = manager.order_stock("55009640", ...)
    """

    _instance: Optional["XtQuantManager"] = None
    _init_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "XtQuantManager":
        """获取全局单例"""
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """重置单例（仅用于测试）"""
        with cls._init_lock:
            if cls._instance is not None:
                cls._instance.shutdown()
            cls._instance = None

    def __init__(self):
        self._accounts: Dict[str, XtQuantAccount] = {}
        self._registry_lock = threading.RLock()

    # ------------------------------------------------------------------
    # 账号管理
    # ------------------------------------------------------------------

    def register_account(self, config: AccountConfig) -> bool:
        """
        注册账号并建立连接。
        如果 account_id 已存在，先断开旧实例再覆盖。

        Args:
            config: 账号配置

        Returns:
            True = 注册并连接成功，False = 连接失败（账号仍被注册）
        """
        with self._registry_lock:
            if config.account_id in self._accounts:
                logger.info(f"账号 {config.account_id[:4]}*** 已存在，先断开旧实例")
                self._accounts[config.account_id].disconnect()

            account = XtQuantAccount(config)
            self._accounts[config.account_id] = account

        connected = account.connect()
        if connected:
            logger.info(f"账号 {config.account_id[:4]}*** 注册成功")
        else:
            logger.warning(f"账号 {config.account_id[:4]}*** 注册但连接失败，可稍后重试")
        return connected

    def unregister_account(self, account_id: str) -> bool:
        """
        断开并移除账号。

        Args:
            account_id: 账号 ID

        Returns:
            True = 成功移除，False = 账号不存在
        """
        with self._registry_lock:
            account = self._accounts.pop(account_id, None)
            if account is None:
                return False
            account.disconnect()
            logger.info(f"账号 {account_id[:4]}*** 已注销")
            return True

    def get_account(self, account_id: str) -> XtQuantAccount:
        """
        获取账号对象。

        Raises:
            AccountNotFoundError: 账号不存在
        """
        with self._registry_lock:
            account = self._accounts.get(account_id)
        if account is None:
            raise AccountNotFoundError(f"账号不存在: {account_id}")
        return account

    def list_accounts(self) -> List[str]:
        """返回所有已注册账号 ID 列表"""
        with self._registry_lock:
            return list(self._accounts.keys())

    def shutdown(self) -> None:
        """断开所有账号连接"""
        with self._registry_lock:
            for account in self._accounts.values():
                try:
                    account.disconnect()
                except Exception as e:
                    logger.warning(f"断开账号时出错: {e}")
            self._accounts.clear()
        logger.info("XtQuantManager 已关闭")

    # ------------------------------------------------------------------
    # 请求分发（交易操作）
    # ------------------------------------------------------------------

    def order_stock(
        self,
        account_id: str,
        stock_code: str,
        order_type: int,
        order_volume: int,
        price_type: int,
        price: float,
        strategy_name: str = "",
        order_remark: str = "",
    ) -> int:
        """下单，失败返回 -1"""
        return self.get_account(account_id).order_stock(
            stock_code, order_type, order_volume, price_type, price,
            strategy_name, order_remark,
        )

    def cancel_order(self, account_id: str, order_id: int) -> int:
        """撤单"""
        return self.get_account(account_id).cancel_order(order_id)

    def query_positions(self, account_id: str) -> List[dict]:
        """查询持仓"""
        return self.get_account(account_id).query_positions()

    def query_asset(self, account_id: str) -> dict:
        """查询资产"""
        return self.get_account(account_id).query_asset()

    def query_orders(self, account_id: str) -> List[dict]:
        """查询委托"""
        return self.get_account(account_id).query_orders()

    def query_trades(self, account_id: str) -> List[dict]:
        """查询成交"""
        return self.get_account(account_id).query_trades()

    # ------------------------------------------------------------------
    # 请求分发（行情操作）
    # ------------------------------------------------------------------

    def get_full_tick(self, account_id: str, stock_codes: List[str]) -> dict:
        """获取全推行情"""
        return self.get_account(account_id).get_full_tick(stock_codes)

    def get_market_data_ex(
        self,
        account_id: str,
        fields: list,
        stock_list: List[str],
        period: str = "1d",
        start_time: str = "20200101",
        end_time: str = "",
    ) -> dict:
        """获取历史行情"""
        return self.get_account(account_id).get_market_data_ex(
            fields, stock_list, period, start_time, end_time
        )

    def download_history_data(
        self,
        account_id: str,
        stock_code: str,
        period: str = "1d",
        start_time: str = "20200101",
        end_time: str = "",
    ) -> bool:
        """下载历史数据"""
        return self.get_account(account_id).download_history_data(
            stock_code, period, start_time, end_time
        )

    # ------------------------------------------------------------------
    # 可观测性
    # ------------------------------------------------------------------

    def get_all_states(self) -> Dict[str, dict]:
        """返回所有账号状态快照"""
        with self._registry_lock:
            accounts = dict(self._accounts)
        return {aid: acc.get_state() for aid, acc in accounts.items()}

    def get_all_metrics(self) -> Dict[str, dict]:
        """返回所有账号指标快照"""
        with self._registry_lock:
            accounts = dict(self._accounts)
        return {aid: acc.get_metrics() for aid, acc in accounts.items()}

    def get_account_state(self, account_id: str) -> dict:
        """返回指定账号状态"""
        return self.get_account(account_id).get_state()

    def get_account_metrics(self, account_id: str) -> dict:
        """返回指定账号指标"""
        return self.get_account(account_id).get_metrics()
