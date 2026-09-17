"""
测试用 Mock 对象

MockXtTrader 和 MockXtData 完整模拟 xtquant API，
无需真实 QMT 环境即可运行所有测试。

设计风格参照 test/test_mocks.py 中的 MockQmtTrader。
"""
import threading
import time
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Mock 数据类（模拟 xtquant 返回的对象）
# ---------------------------------------------------------------------------

class MockXtPosition:
    """模拟 XtPosition 持仓对象"""
    def __init__(self, account_type, account_id, stock_code, volume,
                 can_use_volume, open_price, market_value):
        self.account_type = account_type
        self.account_id = account_id
        self.stock_code = stock_code
        self.volume = volume
        self.can_use_volume = can_use_volume
        self.open_price = open_price
        self.market_value = market_value


class MockXtAsset:
    """模拟 XtAsset 资产对象"""
    def __init__(self, account_type, account_id, cash, frozen_cash,
                 market_value, total_asset):
        self.account_type = account_type
        self.account_id = account_id
        self.cash = cash
        self.frozen_cash = frozen_cash
        self.market_value = market_value
        self.total_asset = total_asset


class MockXtOrder:
    """模拟 XtOrder 委托对象"""
    def __init__(self, account_type, account_id, stock_code, order_id,
                 order_type, order_volume, price, order_status):
        self.account_type = account_type
        self.account_id = account_id
        self.stock_code = stock_code
        self.order_id = order_id
        self.order_type = order_type
        self.order_volume = order_volume
        self.price = price
        self.order_status = order_status


class MockXtTrade:
    """模拟 XtTrade 成交对象"""
    def __init__(self, account_type, account_id, stock_code, order_type,
                 traded_id, traded_volume, traded_price, traded_amount):
        self.account_type = account_type
        self.account_id = account_id
        self.stock_code = stock_code
        self.order_type = order_type
        self.traded_id = traded_id
        self.traded_volume = traded_volume
        self.traded_price = traded_price
        self.traded_amount = traded_amount


# ---------------------------------------------------------------------------
# MockXtTrader — 模拟 XtQuantTrader
# ---------------------------------------------------------------------------

class MockXtTrader:
    """
    完整模拟 XtQuantTrader 对象。

    额外测试控制接口：
    - simulate_disconnect(): 模拟连接断开（后续调用返回失败）
    - simulate_timeout(): 下一次调用会超时（sleep 10s）
    - simulate_error(): 下一次调用会抛异常
    - add_mock_position(): 添加测试持仓
    - clear_positions(): 清空持仓
    """

    def __init__(self, path: str = "mock_path", session_id: int = 100000):
        self.path = path
        self.session_id = session_id
        self._lock = threading.Lock()

        # 连接状态
        self._connected = False
        self._subscribed = False
        self._callbacks = []

        # 测试控制标志
        self._force_disconnect = False
        self._next_timeout = False
        self._next_error = False
        self._next_order_id = 1000

        # 模拟数据存储
        self._positions: Dict[str, MockXtPosition] = {}
        self._orders: Dict[int, MockXtOrder] = {}
        self._trades: List[MockXtTrade] = []
        self._cash = 100000.0

    # --- QMT API 接口 ---

    def start(self):
        pass

    def stop(self):
        self._connected = False

    def connect(self) -> int:
        """返回 0=成功，非 0=失败"""
        if self._force_disconnect:
            return -1
        self._connected = True
        return 0

    def subscribe(self, acc) -> int:
        """返回 0=成功"""
        self._subscribed = True
        return 0

    def register_callback(self, callback):
        self._callbacks.append(callback)

    def order_stock(self, acc, stock_code, order_type, order_volume,
                    price_type, price, strategy_name="", order_remark="") -> int:
        """返回 order_id（> 0），失败返回 -1"""
        self._maybe_timeout_or_error("order_stock")
        with self._lock:
            if self._force_disconnect:
                return -1
            order_id = self._next_order_id
            self._next_order_id += 1
            order = MockXtOrder(
                account_type="STOCK",
                account_id=acc.account_id if hasattr(acc, "account_id") else "test",
                stock_code=stock_code,
                order_id=order_id,
                order_type=order_type,
                order_volume=order_volume,
                price=price,
                order_status=56,  # 56=全部成交
            )
            self._orders[order_id] = order

            # 买入(23)/卖出(24) 更新持仓
            if order_type == 23:  # 买入
                cost = price * order_volume
                self._cash -= cost
                key = stock_code
                if key in self._positions:
                    pos = self._positions[key]
                    new_vol = pos.volume + order_volume
                    new_cost = (pos.open_price * pos.volume + price * order_volume) / new_vol
                    pos.volume = new_vol
                    pos.can_use_volume = new_vol
                    pos.open_price = new_cost
                    pos.market_value = new_vol * price
                else:
                    self._positions[key] = MockXtPosition(
                        account_type="STOCK",
                        account_id=acc.account_id if hasattr(acc, "account_id") else "test",
                        stock_code=stock_code,
                        volume=order_volume,
                        can_use_volume=order_volume,
                        open_price=price,
                        market_value=price * order_volume,
                    )
            elif order_type == 24:  # 卖出
                revenue = price * order_volume
                self._cash += revenue
                key = stock_code
                if key in self._positions:
                    pos = self._positions[key]
                    pos.volume -= order_volume
                    pos.can_use_volume = max(0, pos.can_use_volume - order_volume)
                    if pos.volume <= 0:
                        del self._positions[key]

            # 记录成交
            trade = MockXtTrade(
                account_type="STOCK",
                account_id=acc.account_id if hasattr(acc, "account_id") else "test",
                stock_code=stock_code,
                order_type=order_type,
                traded_id=f"T{order_id}",
                traded_volume=order_volume,
                traded_price=price,
                traded_amount=price * order_volume,
            )
            self._trades.append(trade)

            return order_id

    def cancel_order_stock(self, acc, order_id: int) -> int:
        """返回 0=成功，-1=失败"""
        self._maybe_timeout_or_error("cancel_order")
        with self._lock:
            if order_id in self._orders:
                del self._orders[order_id]
                return 0
            return -2  # 未找到

    def query_stock_positions(self, acc) -> List[MockXtPosition]:
        self._maybe_timeout_or_error("query_positions")
        with self._lock:
            return list(self._positions.values())

    def query_stock_asset(self, acc) -> Optional[MockXtAsset]:
        self._maybe_timeout_or_error("query_asset")
        with self._lock:
            market_value = sum(p.market_value for p in self._positions.values())
            return MockXtAsset(
                account_type="STOCK",
                account_id=acc.account_id if hasattr(acc, "account_id") else "test",
                cash=self._cash,
                frozen_cash=0.0,
                market_value=market_value,
                total_asset=self._cash + market_value,
            )

    def query_stock_orders(self, acc) -> List[MockXtOrder]:
        self._maybe_timeout_or_error("query_orders")
        with self._lock:
            return list(self._orders.values())

    def query_stock_trades(self, acc) -> List[MockXtTrade]:
        self._maybe_timeout_or_error("query_trades")
        with self._lock:
            return list(self._trades)

    # --- 测试控制接口 ---

    def add_mock_position(self, stock_code: str, volume: int,
                          cost_price: float, current_price: float = None):
        """添加模拟持仓"""
        price = current_price if current_price is not None else cost_price
        with self._lock:
            self._positions[stock_code] = MockXtPosition(
                account_type="STOCK",
                account_id="test_account",
                stock_code=stock_code,
                volume=volume,
                can_use_volume=volume,
                open_price=cost_price,
                market_value=price * volume,
            )

    def update_mock_price(self, stock_code: str, new_price: float):
        """更新持仓市价"""
        with self._lock:
            if stock_code in self._positions:
                pos = self._positions[stock_code]
                pos.market_value = pos.volume * new_price

    def clear_positions(self):
        """清空持仓和成交记录"""
        with self._lock:
            self._positions.clear()
            self._orders.clear()
            self._trades.clear()
            self._cash = 100000.0

    def simulate_disconnect(self):
        """模拟连接断开，后续所有调用返回失败"""
        self._force_disconnect = True
        self._connected = False

    def simulate_reconnect(self):
        """模拟重新连接"""
        self._force_disconnect = False
        self._connected = True

    def simulate_timeout(self):
        """下一次 API 调用将超时（sleep 10s）"""
        self._next_timeout = True

    def simulate_error(self, msg: str = "模拟错误"):
        """下一次 API 调用将抛异常"""
        self._next_error = msg

    def reset(self):
        """重置到初始状态"""
        self.clear_positions()
        self._force_disconnect = False
        self._next_timeout = False
        self._next_error = False
        self._next_order_id = 1000
        self._callbacks.clear()

    def _maybe_timeout_or_error(self, op: str):
        """检查是否需要模拟超时或错误"""
        if self._next_timeout:
            self._next_timeout = False
            time.sleep(10)  # 超时
        if self._next_error:
            msg = self._next_error
            self._next_error = False
            raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# MockXtData — 模拟 xtquant.xtdata 模块
# ---------------------------------------------------------------------------

class MockXtData:
    """
    模拟 xtquant.xtdata 模块。

    默认行为：
    - connect() 返回 True
    - get_full_tick() 返回模拟 tick 数据
    - get_market_data_ex() 返回模拟历史数据
    - download_history_data() 立即返回（无操作）

    测试控制：
    - simulate_disconnect(): 后续 connect() 返回 False
    - simulate_tick_failure(): 下一次 get_full_tick 返回空 dict
    - simulate_timeout(): 下一次调用 sleep 10s
    """

    def __init__(self):
        self._connected = True
        self._next_tick_failure = False
        self._next_timeout = False
        self._tick_data: Dict[str, dict] = {}
        self._instrument_names: Dict[str, str] = {}

    def connect(self) -> bool:
        return self._connected

    def get_full_tick(self, stock_list: List[str]) -> dict:
        if self._next_timeout:
            self._next_timeout = False
            time.sleep(10)
        if self._next_tick_failure:
            self._next_tick_failure = False
            return {}
        result = {}
        for code in stock_list:
            result[code] = self._tick_data.get(code, {
                "lastPrice": 10.0,
                "open": 9.8,
                "high": 10.5,
                "low": 9.5,
                "volume": 100000,
                "amount": 1000000.0,
            })
        return result

    def get_instrument_detail(self, stock_code: str) -> dict:
        """模拟 xtdata.get_instrument_detail：返回证券信息（含名称）。"""
        name = self._instrument_names.get(stock_code)
        if name:
            return {"InstrumentName": name, "ExchangeID": "SZ" if ".SZ" in stock_code else "SH"}
        # 根据代码前缀推断默认名称
        prefix = stock_code[:6] if stock_code else ""
        default_names = {
            "000001": "平安银行", "600036": "招商银行", "600509": "天富能源",
            "300473": "德尔股份", "301587": "中瑞股份",
        }
        return {"InstrumentName": default_names.get(prefix, f"测试股票{prefix}"), "ExchangeID": "SZ"}

    def get_market_data_ex(self, fields, stock_list, period="1d",
                            start_time="20240101", end_time="20241231") -> dict:
        result = {}
        for code in stock_list:
            result[code] = [
                {"time": start_time, "open": 10.0, "high": 10.5,
                 "low": 9.5, "close": 10.2, "volume": 100000}
            ]
        return result

    def download_history_data(self, stock_code, period="1d",
                               start_time="20240101", end_time="20241231",
                               incrementally=True):
        pass  # 模拟无操作

    # --- 测试控制接口 ---

    def set_tick_data(self, stock_code: str, data: dict):
        """设置指定股票的 tick 数据"""
        self._tick_data[stock_code] = data

    def set_instrument_name(self, stock_code: str, name: str):
        """设置指定证券的名称（get_instrument_detail 会返回）"""
        self._instrument_names[stock_code] = name

    def simulate_disconnect(self):
        self._connected = False

    def simulate_reconnect(self):
        self._connected = True

    def simulate_tick_failure(self):
        """下一次 get_full_tick 返回空 dict"""
        self._next_tick_failure = True

    def simulate_timeout(self):
        """下一次调用超时"""
        self._next_timeout = True


# ---------------------------------------------------------------------------
# MockStockAccount — 模拟 StockAccount
# ---------------------------------------------------------------------------

class MockStockAccount:
    """模拟 xtquant.xttype.StockAccount"""
    def __init__(self, account_id: str = "test_account",
                 account_type: str = "STOCK"):
        self.account_id = account_id
        self.account_type = account_type
