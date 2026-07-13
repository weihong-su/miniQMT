# qmt_rpc_trader.py
# 大QMT RPC 方案 —— xttrader 降级替代客户端（基于 xtquant_big_convert）
#
# 提供一个接口完全兼容 easy_qmt_trader 的交易客户端，底层通过 vendored
# bigqmt_signal_trader.xtquant_compat 的 Redis/ZMQ/MySQL RPC 与大QMT内置Python
# (BIGQMT_REDIS_DRYRUN.py) 通信来执行交易。
#
# 设计目标：作为 easy_qmt_trader 的直接替换插入 position_manager._create_qmt_trader()，
# 让 PositionManager / TradingExecutor / GridTradingManager 无感知切换。
#
# 关键兼容点（对齐 qmt_ipc_trader.py 契约）：
#   1. 方法签名对齐 easy_qmt_trader（position/balance/buy/sell/order_stock/...）
#   2. 提供 .xt_trader / .acc / .order_id_map 属性（position_manager 有多处直接访问）
#   3. order_id 使用纯整数（position_manager 会 int() 转换）
#   4. 回调：优先用 vendored 的 Redis pubsub 推送 + 兜底轮询 query_orders/query_trades
#
# ⚠️ order_id 映射：大QMT passorder 不同步返回订单号，vendored order_stock 返回的是
#    字符串 user_order_id（或 order_sys_id）。因此本客户端生成纯整数 order_id 返回给
#    上层，并维护 int_id ↔ (返回串 / order_sys_id) 的映射，供撤单与回调配对。

import os
import sys
import time
import random
import threading

# vendored bigqmt 客户端库：append 到 sys.path 末尾，避免其 src/xtquant shim 遮蔽
# 项目真实 xtquant（真实 xtquant 在更靠前的路径，append 不会覆盖）。
_VENDOR_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vender', 'bigqmt', 'src')
if _VENDOR_SRC not in sys.path:
    sys.path.append(_VENDOR_SRC)

from bigqmt_signal_trader.xtquant_compat import (  # noqa: E402
    BigQmtXtTrader, StockAccount, XtQuantTraderCallback, FIX_PRICE, LATEST_PRICE,
)

import _qmt_trader_base as base  # noqa: E402

try:
    import config
except ImportError:
    config = None

try:
    from logger import get_logger
    logger = get_logger("qmt_rpc_trader")
except Exception:
    import logging
    logger = logging.getLogger("qmt_rpc_trader")


class _FakeXtTrader:
    """
    模拟 easy_qmt_trader.xt_trader（XtQuantTrader）底层对象。

    position_manager 有多处绕过封装方法直接访问 self.qmt_trader.xt_trader：
      - query_stock_orders(acc, cancelable_only) → 后备委托查询
      - query_stock_order(acc, order_id)         → 查单个委托状态
      - cancel_order_stock(acc, order_id)        → 撤单
    这里全部转发到父 QmtRpcTrader。
    """
    def __init__(self, rpc_trader):
        self._rpc = rpc_trader

    def query_stock_orders(self, acc, cancelable_only=False):
        return self._rpc._read_all_orders(cancelable_only=cancelable_only)

    def query_stock_order(self, acc, order_id):
        for o in self._rpc._read_all_orders():
            if str(o.order_id) == str(order_id):
                return o
        return None

    def cancel_order_stock(self, acc, order_id):
        return self._rpc.cancel_order_stock(order_id)


class _RpcCallback(XtQuantTraderCallback):
    """把 vendored 的 XtQuantTraderCallback 推送转发到父 QmtRpcTrader。"""
    def __init__(self, rpc_trader):
        self._rpc = rpc_trader

    def on_stock_order(self, order):
        self._rpc._on_push_order(order)

    def on_stock_trade(self, trade):
        self._rpc._on_push_trade(trade)

    def on_disconnected(self):
        self._rpc._on_disconnect()


class QmtRpcTrader:
    """
    大QMT RPC 交易客户端，接口兼容 easy_qmt_trader。

    通过 vendored bigqmt_signal_trader RPC 与大QMT内置脚本通信，实现
    下单/撤单/查持仓/查成交。延迟毫秒级，可跨机。
    """

    def __init__(self, path=None, session_id=None, account=None, account_type='STOCK',
                 is_slippage=True, slippage=0.01):
        self.path = path
        self.session_id = session_id or random.randint(100000, 999999)
        if account is None and config is not None:
            try:
                account = config.get_account_config().get('account_id', '')
            except Exception:
                account = ''
        self.account = str(account or '')
        self.account_type = account_type
        self.slippage = slippage if is_slippage else 0

        # 从 miniQMT config 组装 vendored RPC 客户端配置
        redis_cfg = dict(getattr(config, 'QMT_RPC_REDIS', {}) or {}) if config else {}
        redis_cfg = dict(redis_cfg)
        redis_cfg['transport'] = getattr(config, 'QMT_RPC_TRANSPORT', 'redis') if config else 'redis'
        redis_cfg.setdefault('account_id', self.account)
        timeout = getattr(config, 'QMT_RPC_TIMEOUT_SECONDS', 6.0) if config else 6.0
        self.order_timeout = getattr(config, 'QMT_RPC_ORDER_TIMEOUT', 30) if config else 30
        self._allow_order = getattr(config, 'QMT_RPC_ALLOW_ORDER', False) if config else False

        # vendored 交易对象 + 账号
        self._bq = BigQmtXtTrader(
            path=path, session_id=self.session_id, account_id=self.account,
            redis_config=redis_cfg, timeout_seconds=timeout,
        )
        self._bq_acc = StockAccount(self.account, account_type)

        # 兼容 easy_qmt_trader 的属性（position_manager 直接访问）
        self.xt_trader = _FakeXtTrader(self)
        self.acc = base.FakeAccount(self.account, account_type)
        self.order_id_map = {}

        # order_id 映射（int_id ↔ 返回串 / order_sys_id）
        self._map_lock = threading.Lock()
        self._id_map = {}          # int_id -> {user_order_id, order_sys_id, stock_code, action, volume}
        self._return_index = {}    # order_stock 返回串 -> int_id
        self._sysid_index = {}     # order_sys_id -> int_id
        self._seen_orders = set()  # 已触发 order_callback 的 (int_id, status)
        self._seen_deals = set()   # 已触发 trade_callback 的 int_id

        # 回调列表
        self._trade_callbacks = []
        self._order_callbacks = []
        self._disconnect_callbacks = []

        # 兜底轮询线程控制
        self._poller_thread = None
        self._poller_stop = False

        self._connected = False
        self._cb = _RpcCallback(self)
        logger.info('操作提示: QmtRpcTrader 已创建，请确保大QMT端 BIGQMT_REDIS_DRYRUN.py 已运行且 RPC 服务在线')

    # ------------------------------------------------------------------
    # 连接与生命周期
    # ------------------------------------------------------------------

    def connect(self):
        """探测 RPC 连通性 + 注册推送回调。返回 (self, self) 可用，None 不可用。"""
        try:
            if not self._is_alive():
                logger.error('QmtRpcTrader 连接失败: 大QMT RPC ping 无响应，请确认大QMT端脚本在运行')
                self._connected = False
                return None
            self._bq.register_callback(self._cb)
            self._bq.start()          # 启动 vendored Redis pubsub 事件监听线程
            self._bq.subscribe(self._bq_acc)
            self._connected = True
            self._start_poller()
            logger.info('QmtRpcTrader 连接成功（大QMT RPC 在线）')
            if not self._allow_order:
                logger.warning('QMT_RPC_ALLOW_ORDER=False：当前为只读模式，真实下单/撤单将被拒绝')
            return (self, self)
        except Exception as e:
            logger.error(f'QmtRpcTrader 连接异常: {e}')
            self._connected = False
            return None

    def _is_alive(self):
        """通过 RPC ping 探测大QMT是否在线。"""
        try:
            self._bq.client.call('ping')
            return True
        except Exception:
            return False

    def ping_xttrader(self):
        return self._is_alive()

    def reconnect_xttrader(self):
        logger.warning('QmtRpcTrader 正在重连（重新探测大QMT RPC）...')
        result = self.connect()
        return result is not None

    def stop(self):
        self._poller_stop = True
        try:
            self._bq.stop()
        except Exception:
            pass

    def get_rpc_health(self):
        """返回 RPC 通道健康诊断快照，供控制台/日志排障。"""
        return {
            'account': self.account,
            'transport': getattr(config, 'QMT_RPC_TRANSPORT', 'redis') if config else 'redis',
            'connected': self._connected,
            'rpc_alive': self._is_alive(),
            'allow_order': self._allow_order,
            'tracked_orders': len(self._id_map),
            'poller_alive': bool(self._poller_thread and self._poller_thread.is_alive()),
        }

    # ------------------------------------------------------------------
    # 回调注册 + 推送/轮询
    # ------------------------------------------------------------------

    def register_trade_callback(self, cb):
        self._trade_callbacks.append(cb)

    def register_order_callback(self, cb):
        self._order_callbacks.append(cb)

    def register_disconnect_callback(self, cb):
        self._disconnect_callbacks.append(cb)

    def _start_poller(self):
        if self._poller_thread and self._poller_thread.is_alive():
            return
        self._poller_stop = False
        self._poller_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poller_thread.start()
        logger.info('QmtRpcTrader 兜底轮询线程已启动')

    def _poll_loop(self):
        """兜底轮询 query_orders：推送不可用/丢失时补偿触发 order/trade/disconnect 回调。"""
        interval = getattr(config, 'QMT_RPC_DEAL_POLL_INTERVAL', 1.0) if config else 1.0
        was_alive = True
        while not self._poller_stop:
            try:
                alive = self._is_alive()
                if was_alive and not alive:
                    logger.error('QmtRpcTrader 检测到大QMT RPC 断连，触发断连回调')
                    self._on_disconnect()
                was_alive = alive
                if alive:
                    for o in self._read_all_orders():
                        self._maybe_fire_from_order(o)
            except Exception as e:
                logger.warning(f'RPC 兜底轮询异常: {e}')
            time.sleep(interval)

    def _on_disconnect(self):
        for cb in self._disconnect_callbacks:
            try:
                cb()
            except Exception as e:
                logger.error(f'disconnect_callback 异常: {e}')

    def _on_push_order(self, order):
        """vendored Redis 推送的委托事件 → 归一为内部 _FakeXtObject 后触发。"""
        try:
            self._maybe_fire_from_order(self._bq_order_to_fake(order))
        except Exception as e:
            logger.warning(f'处理推送委托异常: {e}')

    def _on_push_trade(self, trade):
        """vendored Redis 推送的成交事件（无 remark，靠 order_sys_id 配对）。"""
        try:
            sysid = str(getattr(trade, 'order_sysid', '') or getattr(trade, 'order_sys_id', ''))
            with self._map_lock:
                int_id = self._sysid_index.get(sysid)
            if int_id is None:
                return  # 委托事件尚未到达，轮询会补齐
            self._fire_trade(int_id, self._bq_trade_to_fake(trade, int_id))
        except Exception as e:
            logger.warning(f'处理推送成交异常: {e}')

    def _maybe_fire_from_order(self, o):
        """对一条内部委托对象做映射配对 + 去重触发 order/trade 回调。"""
        int_id = self._reconcile(o)
        if int_id is None:
            return
        o.order_id = int_id
        status = o.order_status
        key = (int_id, status)
        with self._map_lock:
            new_order = key not in self._seen_orders
            if new_order:
                self._seen_orders.add(key)
            new_deal = status in (55, 56) and int_id not in self._seen_deals
            if new_deal:
                self._seen_deals.add(int_id)
        if new_order:
            self._fire_order(int_id, o)
        if new_deal:
            self._fire_trade(int_id, o)

    def _reconcile(self, o):
        """把内部委托对象配对到 int_id，并回填 sysid 映射。返回 int_id 或 None。"""
        sysid = str(getattr(o, 'order_sysid', '') or '')
        remark = str(getattr(o, 'order_remark', '') or '')
        with self._map_lock:
            int_id = None
            if sysid and sysid in self._sysid_index:
                int_id = self._sysid_index[sysid]
            elif remark and remark in self._return_index:
                int_id = self._return_index[remark]
            elif sysid and sysid in self._return_index:
                int_id = self._return_index[sysid]
            if int_id is not None and sysid:
                self._sysid_index[sysid] = int_id
                if int_id in self._id_map:
                    self._id_map[int_id]['order_sys_id'] = sysid
            return int_id

    def _fire_order(self, int_id, o):
        for cb in self._order_callbacks:
            try:
                cb(o)
            except Exception as e:
                logger.error(f'order_callback 异常: {e}')

    def _fire_trade(self, int_id, o):
        trade = base.FakeXtObject(
            order_id=int_id,
            stock_code=getattr(o, 'stock_code', ''),
            traded_volume=getattr(o, 'traded_volume', 0),
            traded_price=getattr(o, 'traded_price', 0),
            traded_amount=getattr(o, 'traded_price', 0) * getattr(o, 'traded_volume', 0),
            account_id=self.account,
            account_type=self.account_type,
            traded_id=str(getattr(o, 'order_sysid', int_id)),
            traded_time=int(time.time()),
            order_type=getattr(o, 'order_type', base.STOCK_BUY),
            order_status=getattr(o, 'order_status', 56),
        )
        for cb in self._trade_callbacks:
            try:
                cb(trade)
            except Exception as e:
                logger.error(f'trade_callback 异常: {e}')

    # ------------------------------------------------------------------
    # vendored CompatObject → 内部 _FakeXtObject
    # ------------------------------------------------------------------

    def _bq_order_to_fake(self, order):
        return base.FakeXtObject(
            account_type=self.account_type,
            account_id=self.account,
            stock_code=str(getattr(order, 'stock_code', '')),
            order_id=getattr(order, 'order_id', ''),
            order_sysid=str(getattr(order, 'order_sysid', '') or getattr(order, 'order_sys_id', '')),
            order_time=int(time.time()),
            order_type=getattr(order, 'order_type', base.STOCK_BUY),
            order_volume=int(getattr(order, 'order_volume', 0) or 0),
            price_type=50,
            price=getattr(order, 'price', 0),
            traded_volume=int(getattr(order, 'traded_volume', 0) or 0),
            traded_price=getattr(order, 'price', 0),
            order_status=int(getattr(order, 'order_status', 56) or 56),
            status_msg='',
            strategy_name=str(getattr(order, 'strategy_name', '')),
            order_remark=str(getattr(order, 'order_remark', '')),
        )

    def _bq_trade_to_fake(self, trade, int_id):
        return base.FakeXtObject(
            stock_code=str(getattr(trade, 'stock_code', '')),
            order_sysid=str(getattr(trade, 'order_sysid', '') or getattr(trade, 'order_sys_id', '')),
            order_type=getattr(trade, 'order_type', base.STOCK_BUY),
            traded_volume=int(getattr(trade, 'traded_volume', 0) or 0),
            traded_price=getattr(trade, 'traded_price', 0),
            order_status=56,
        )

    # ------------------------------------------------------------------
    # 下单
    # ------------------------------------------------------------------

    def _send(self, action, stock_code, volume, price, strategy_name='', order_remark='',
              price_type=None):
        if volume is None or volume <= 0:
            logger.error(f'RPC下单参数错误: volume={volume}')
            return None
        if not self._allow_order:
            logger.error(f'RPC下单被拒: QMT_RPC_ALLOW_ORDER=False（只读安全模式）. {action} {stock_code} {volume}股')
            return None
        if not self._is_alive():
            logger.error(f'RPC下单中止: 大QMT RPC 不在线，快速失败不阻塞. {action} {stock_code} {volume}股')
            return None
        try:
            code = base.adjust_stock(stock_code)
            price = base.select_slippage(code, price or 0, action, self.slippage)
            if price_type is not None:
                pass  # 调用方显式指定
            elif price is None or price == 0:
                price_type = LATEST_PRICE
            else:
                price_type = FIX_PRICE
            order_type = base.STOCK_BUY if action == 'buy' else base.STOCK_SELL
            int_id = base.next_order_id()
            ret = self._bq.order_stock(
                self._bq_acc, code, order_type, int(volume), price_type,
                float(price or 0), strategy_name or '', str(int_id),
            )
            if ret in (None, -1, '-1'):
                logger.warning(f'RPC下单被拒: {action} {code} {volume}股，返回={ret}')
                return None
            ret_str = str(ret)
            with self._map_lock:
                self._id_map[int_id] = {
                    'user_order_id': ret_str, 'order_sys_id': None,
                    'stock_code': code, 'action': action, 'volume': int(volume),
                }
                self._return_index[ret_str] = int_id
                self.order_id_map[int_id] = int_id
                if len(self._id_map) > 4096:
                    for k in list(self._id_map)[:2048]:
                        self._id_map.pop(k, None)
            logger.info(f'RPC下单已提交: {action} {code} {volume}股 @ {price}, order_id={int_id}, 返回={ret_str}')
            return int_id
        except Exception as e:
            logger.error(f'RPC下单异常: {e}')
            return None

    def buy(self, security='600031.SH', order_type=base.STOCK_BUY, amount=100,
            price_type=None, price=20, strategy_name='', order_remark=''):
        return self._send('buy', security, amount, price, strategy_name, order_remark,
                          price_type=price_type)

    def sell(self, security='600031.SH', order_type=base.STOCK_SELL, amount=100,
             price_type=None, price=20, strategy_name='', order_remark=''):
        return self._send('sell', security, amount, price, strategy_name, order_remark,
                          price_type=price_type)

    def order_stock(self, stock_code='600031.SH', order_type=base.STOCK_BUY, order_volume=100,
                    price_type=None, price=20, strategy_name='', order_remark=''):
        action = 'buy' if order_type == base.STOCK_BUY else 'sell'
        return self._send(action, stock_code, order_volume, price, strategy_name, order_remark,
                          price_type=price_type)

    def order_stock_async(self, stock_code='600031.SH', order_type=base.STOCK_BUY, order_volume=100,
                          price_type=None, price=20, strategy_name='', order_remark=''):
        return self.order_stock(stock_code, order_type, order_volume, price_type, price,
                                strategy_name, order_remark)

    # ------------------------------------------------------------------
    # 撤单
    # ------------------------------------------------------------------

    def _resolve_sysid(self, int_id):
        with self._map_lock:
            info = self._id_map.get(int_id)
            if info and info.get('order_sys_id'):
                return info['order_sys_id']
        # 回填：轮询一次委托，尝试建立 sysid 映射
        try:
            for o in self._read_all_orders():
                self._reconcile(o)
        except Exception:
            pass
        with self._map_lock:
            info = self._id_map.get(int_id)
            return info.get('order_sys_id') if info else None

    def cancel_order_stock(self, order_id=0):
        try:
            int_id = int(order_id)
        except (TypeError, ValueError):
            int_id = order_id
        sysid = self._resolve_sysid(int_id)
        if not sysid:
            logger.warning(f'RPC撤单失败: order_id={order_id} 未找到对应 order_sys_id（可能未成交回报或已终态）')
            return -1
        try:
            ok = self._bq.cancel_order_stock(self._bq_acc, sysid)
            if ok:
                logger.info(f'RPC撤单指令已提交: order_id={order_id}, sysid={sysid}')
                return 0
            logger.warning(f'RPC撤单被拒: order_id={order_id}, sysid={sysid}')
            return -1
        except Exception as e:
            logger.error(f'RPC撤单异常: {e}')
            return -1

    def cancel_order_stock_async(self, order_id=0):
        return self.cancel_order_stock(order_id)

    # ------------------------------------------------------------------
    # 账户查询（持仓/资产）
    # ------------------------------------------------------------------

    def position(self):
        """查询持仓，兼容 easy_qmt_trader.position()。返回 DataFrame。

        RPC 持仓无实时市价，市值按 成本价*余额 兜底（data_manager 后续用现价刷新）。
        必需 5 列：证券代码/股票余额/可用余额/成本价/市值。
        """
        empty = base.empty_df(base.POSITION_COLUMNS)
        try:
            positions = self._bq.query_stock_positions(self._bq_acc) or []
        except Exception as e:
            logger.warning(f'RPC 查询持仓失败: {e}')
            return empty
        if not positions:
            return empty
        data_list = []
        for pos in positions:
            stock = str(getattr(pos, 'stock_code', ''))
            code6 = stock.split('.')[0] if '.' in stock else stock
            volume = int(getattr(pos, 'volume', 0) or 0)
            available = int(getattr(pos, 'can_use_volume', getattr(pos, 'available_amount', volume)) or 0)
            cost = float(getattr(pos, 'cost_price', getattr(pos, 'avg_price', 0)) or 0)
            data_list.append({
                '账号类型': self.account_type,
                '资金账号': self.account,
                '证券代码': code6,
                '股票余额': volume,
                '可用余额': available,
                '成本价': cost,
                '参考成本价': cost,
                '市值': cost * volume,
                '证券名称': str(getattr(pos, 'stock_name', '')),
            })
        if not base._HAS_PANDAS:
            return data_list
        return base.pd.DataFrame(data_list)

    def query_stock_positions(self):
        return self.position()

    def balance(self):
        """查询账户资产，兼容 easy_qmt_trader.balance()。返回单行 DataFrame。"""
        empty = base.empty_df(base.BALANCE_COLUMNS)
        asset = self._query_asset_obj()
        if asset is None:
            return empty
        row = {
            '账号类型': self.account_type,
            '资金账户': self.account,
            '可用金额': asset.get('available', 0),
            '冻结金额': asset.get('frozen', 0),
            '持仓市值': asset.get('market_value', 0),
            '总资产': asset.get('total_asset', 0),
        }
        if not base._HAS_PANDAS:
            return [row]
        return base.pd.DataFrame([row])

    def _query_asset_obj(self):
        try:
            a = self._bq.query_stock_asset(self._bq_acc)
        except Exception as e:
            logger.warning(f'RPC 查询资产失败: {e}')
            return None
        if a is None:
            return None
        cash = getattr(a, 'cash', None)
        cash = getattr(a, 'available_cash', cash) if cash is None else cash
        return {
            'available': float(cash or 0),
            'frozen': 0.0,
            'market_value': float(getattr(a, 'market_value', 0) or 0),
            'total_asset': float(getattr(a, 'total_asset', 0) or 0),
        }

    def query_stock_asset(self):
        asset = self._query_asset_obj() or {}
        return {
            '账号类型': self.account_type,
            '资金账户': self.account,
            '可用金额': asset.get('available', 0),
            '冻结金额': asset.get('frozen', 0),
            '持仓市值': asset.get('market_value', 0),
            '总资产': asset.get('total_asset', 0),
        }

    # ------------------------------------------------------------------
    # 委托/成交查询
    # ------------------------------------------------------------------

    def _read_all_orders(self, cancelable_only=False):
        """查询委托列表，归一为内部 _FakeXtObject（order_id 映射为 int）。"""
        try:
            raw = self._bq.query_stock_orders(self._bq_acc, cancelable_only=cancelable_only) or []
        except Exception as e:
            logger.warning(f'RPC 查询委托失败: {e}')
            return []
        result = []
        for order in raw:
            o = self._bq_order_to_fake(order)
            int_id = self._reconcile(o)
            if int_id is not None:
                o.order_id = int_id
            result.append(o)
        if cancelable_only:
            result = [o for o in result if o.order_status in base.ACTIVE_ORDER_STATUS]
        return result

    def query_stock_orders(self):
        return base.orders_to_df(self._read_all_orders())

    def today_entrusts(self):
        return self.query_stock_orders()

    def query_stock_trades(self):
        orders = [o for o in self._read_all_orders() if o.order_status in (55, 56)]
        return base.trades_to_df(orders)

    def today_trades(self):
        return self.query_stock_trades()

    def get_active_orders_by_stock(self, stock_code):
        stock_code = base.adjust_stock(stock_code)
        result = []
        for o in self._read_all_orders():
            order_stock = str(o.stock_code)
            if (order_stock == stock_code or order_stock[:6] == stock_code[:6]) \
                    and o.order_status in base.ACTIVE_ORDER_STATUS:
                result.append(o)
        return result

    def get_active_order_info_by_stock(self, stock_code):
        active = self.get_active_orders_by_stock(stock_code)
        return [{
            'order_id': o.order_id,
            'stock_code': o.stock_code,
            'order_type': o.order_type,
            'order_status': o.order_status,
            'status_msg': getattr(o, 'status_msg', ''),
            'order_volume': o.order_volume,
            'traded_volume': o.traded_volume,
            'price': o.price,
            'order_time': o.order_time,
            'strategy_name': o.strategy_name,
            'order_remark': o.order_remark,
        } for o in active]

    # ------------------------------------------------------------------
    # 辅助工具方法（与 easy_qmt_trader 对齐）
    # ------------------------------------------------------------------

    def adjust_stock(self, stock='600031.SH'):
        return base.adjust_stock(stock)

    def select_data_type(self, stock='600031'):
        return base.select_data_type(stock)

    def select_slippage(self, stock='600031', price=15.01, trader_type='buy'):
        return base.select_slippage(stock, price, trader_type, self.slippage)

    def check_stock_is_av_buy(self, stock='128036', price=156.7, amount=10, hold_limit=100000):
        try:
            asset = self._query_asset_obj() or {}
            cash = float(asset.get('available', 0))
            value = float(price) * float(amount)
            if cash >= value:
                logger.info(f'允许买入 股票={stock}, 可用现金={cash:.2f} >= 买入金额={value:.2f}')
                return True
            logger.warning(f'不允许买入 股票={stock}, 可用现金={cash:.2f} < 买入金额={value:.2f}')
            return False
        except Exception as e:
            logger.error(f'check_stock_is_av_buy 异常: {e}')
            return False

    def check_stock_is_av_sell(self, stock='128036', amount=10):
        try:
            stock6 = stock.split('.')[0] if '.' in stock else stock
            for pos in (self._bq.query_stock_positions(self._bq_acc) or []):
                pos_stock = str(getattr(pos, 'stock_code', ''))
                pos6 = pos_stock.split('.')[0] if '.' in pos_stock else pos_stock
                if pos6 == stock6:
                    available = int(getattr(pos, 'can_use_volume', getattr(pos, 'available_amount', 0)) or 0)
                    if available >= amount:
                        logger.info(f'允许卖出 股票={stock}, 可用={available} >= 卖出={amount}')
                        return True
                    logger.warning(f'不允许卖出,持股不足 股票={stock}, 可用={available} < 卖出={amount}')
                    return False
            logger.warning(f'不允许卖出,无持股 股票={stock}')
            return False
        except Exception as e:
            logger.error(f'check_stock_is_av_sell 异常: {e}')
            return False
