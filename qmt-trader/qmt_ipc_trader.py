# qmt_ipc_trader.py
# 大QMT文件IPC方案 —— xttrader 降级替代客户端
#
# 提供一个接口完全兼容 easy_qmt_trader 的交易客户端，底层通过文件系统 IPC
# 与大QMT内置Python脚本(QMT_trade_executor.py)通信来执行交易。
#
# 设计目标：当券商收紧 miniQMT 权限或 xttrader 连接失效时，把 QmtIpcTrader
# 作为 easy_qmt_trader 的直接替换插入 position_manager._create_qmt_trader()，
# 让 PositionManager / TradingExecutor / GridTradingManager 无感知切换。
#
# 关键兼容点：
#   1. 方法签名对齐 easy_qmt_trader（position/balance/buy/sell/order_stock/...）
#   2. 提供 .xt_trader / .acc / .order_id_map 属性（position_manager 有 4 处直接访问）
#   3. order_id 使用纯整数（position_manager 的 _query_order_status/_cancel_order 会 int() 转换）
#   4. 后台成交回报轮询线程，把 done/ 回执转成 callback，模拟 xttrader 推送
#
# 使用：由 position_manager._create_qmt_trader() 在 ENABLE_QMT_IPC_FALLBACK=True 时创建。

import os
import json
import time
import random
import math
import threading
from datetime import datetime
from qmt_ipc_data import IpcSnapshotUnavailable, QmtIpcDataAdapter, read_snapshot, source_timestamp, heartbeat_is_fresh

try:
    import pandas as pd
    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False

try:
    import config
except ImportError:
    config = None

try:
    from logger import get_logger, log_throttled, reset_log_throttle
    logger = get_logger("qmt_ipc_trader")
except Exception:
    import logging
    logger = logging.getLogger("qmt_ipc_trader")


# ── 持仓 DataFrame 列名（与 easy_qmt_trader.position() 兼容）──
_POSITION_COLUMNS = [
    '账号类型', '资金账号', '证券代码', '股票余额', '可用余额',
    '成本价', '市值', '选择', '持股天数', '交易状态', '明细',
    '证券名称', '冻结数量', '市价', '盈亏', '盈亏比(%)',
    '当日买入', '当日卖出',
]

# ── 资产 DataFrame 列名（与 easy_qmt_trader.balance() 兼容）──
_BALANCE_COLUMNS = [
    '账号类型', '资金账户', '可用金额', '冻结金额', '持仓市值', '总资产',
]

# ── IPC 回执 status → QMT 委托状态码映射 ──
# QMT 状态码：48未报 49待报 50已报 51已报待撤 52部分待撤 53部撤
#             54已撤 55部成 56已成 57废单
_IPC_STATUS_TO_QMT = {
    'filled': 56,             # 已成
    'partial': 55,            # 部成
    'partial_cancelled': 53,  # 部撤
    'pending': 50,            # 已报
    'submitted': 48,          # IPC 已提交，柜台尚未确认
    'rejected': 57,           # 废单
    'error': 57,              # 废单
    'cancelled': 54,          # 已撤
    'cancelled_timeout': 54,  # 已撤
    'cancelled_by_user': 54,  # 已撤
}

# QMT 买卖方向常量（与 xtconstant 对齐，避免强依赖 xtquant 导入）
_STOCK_BUY = 23
_STOCK_SELL = 24

# 进程内 order_id 自增序号（保证纯整数且唯一）
_order_seq = 0
_order_seq_lock = threading.Lock()


def _next_order_id():
    """生成纯整数、进程内唯一的 order_id。

    position_manager._query_order_status/_cancel_order 会对 order_id 做 int() 转换，
    因此 IPC order_id 必须是纯数字，不能用字符串前缀格式。
    """
    global _order_seq
    with _order_seq_lock:
        _order_seq = (_order_seq + 1) % 10000
    # 秒级时间戳后 8 位 * 10000 + 序号 → 纯整数，天级别内唯一
    return (int(time.time()) % 100000000) * 10000 + _order_seq


class _FakeAccount:
    """占位账号对象，兼容 easy_qmt_trader.acc（被当作参数传入 xt_trader 方法）。"""
    def __init__(self, account_id, account_type='STOCK'):
        self.account_id = account_id
        self.account_type = account_type


class _FakeXtObject:
    """把 dict 转成属性访问对象，模拟 XtOrder / XtTrade。"""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeXtTrader:
    """
    模拟 easy_qmt_trader.xt_trader（XtQuantTrader）底层对象。

    position_manager 有 4 处绕过封装方法直接访问 self.qmt_trader.xt_trader：
      - query_stock_orders(acc, cancelable_only) → 后备委托查询 (2769)
      - query_stock_order(acc, order_id)         → 查单个委托状态 (4318)
      - cancel_order_stock(acc, order_id)        → 撤单 (4365)
    这里全部转发到父 QmtIpcTrader 的 IPC 文件读写。
    """
    def __init__(self, ipc_trader):
        self._ipc = ipc_trader

    def query_stock_orders(self, acc, cancelable_only=False):
        """返回委托对象列表（从 done/ 目录聚合）。"""
        orders = self._ipc._read_all_orders()
        if cancelable_only:
            active = [48, 49, 50, 51, 52, 55]
            orders = [o for o in orders if o.order_status in active]
        return orders

    def query_stock_order(self, acc, order_id):
        """按 order_id 查单个委托，返回对象或 None。"""
        for o in self._ipc._read_all_orders():
            if str(o.order_id) == str(order_id):
                return o
        return None

    def cancel_order_stock(self, acc, order_id):
        """撤单：写 cancel/ 文件。返回 0=成功已提交，-1=已完成/未找到。"""
        return self._ipc._write_cancel(order_id)


class QmtIpcTrader:
    """
    大QMT文件IPC交易客户端，接口兼容 easy_qmt_trader。

    通过文件系统 IPC 与大QMT内置脚本(QMT_trade_executor.py)通信，实现
    下单/撤单/查持仓/查成交。延迟约 1-2 秒，适用于中低频策略作为 xttrader
    失效时的降级方案。
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
        self.account = account or ''
        self.account_type = account_type
        self.slippage = slippage if is_slippage else 0

        # 多账号隔离：每个账号使用 {QMT_IPC_ROOT}/{account_id}/ 独立子目录，
        # 天然支持 miniQMT 多账号（每账号一进程一实例，互不干扰）。
        # account 为空（异常情况）时回退根目录。
        base_root = getattr(config, 'QMT_IPC_ROOT', r'C:\QuantIPC') if config else r'C:\QuantIPC'
        self.ipc_base_root = base_root
        self.ipc_root = os.path.join(base_root, str(self.account)) if self.account else base_root
        self.order_timeout = getattr(config, 'QMT_IPC_ORDER_TIMEOUT', 30) if config else 30
        self.heartbeat_max_age = getattr(config, 'QMT_IPC_HEARTBEAT_MAX_AGE', 10) if config else 10
        self.done_lookback = getattr(config, 'QMT_IPC_DONE_LOOKBACK_SECONDS', 86400) if config else 86400

        # 兼容 easy_qmt_trader 的属性（position_manager 直接访问）
        self.xt_trader = _FakeXtTrader(self)
        self.acc = _FakeAccount(self.account, account_type)
        # IPC 同步返回真实 order_id，本无需 seq→order_id 映射。但 position_manager
        # ._get_real_order_id() 在异步模式(USE_SYNC_ORDER_API=False，config 默认值)下会
        # 去 order_id_map 里查 returned_id，查不到就等 2 秒后返回 None → 下单被判失败。
        # 因此每次下单把 order_id 自映射(id→id)写入本表，兼容 config 两种模式，避免
        # 用户忘记把 USE_SYNC_ORDER_API 改成 True 时所有委托静默失败。
        self.order_id_map = {}
        self._cache_lock = threading.RLock()
        self.order_id_map_lock = self._cache_lock

        # 回调列表
        self._trade_callbacks = []
        self._order_callbacks = []
        self._disconnect_callbacks = []

        # 成交回报轮询线程控制
        self._poller_thread = None
        self._poller_stop = False
        self._seen_deals = {}      # order_id -> 已推送过的累计成交量
        self._seen_orders = set()  # 已触发过 order_callback 的 (order_id, status)
        self._timeout_orders = {}  # order_id -> 客户端等待超时后已追撤的订单快照

        self._connected = False
        self.position_snapshot_valid = False
        self._seen_native_deals = set()
        self._seen_deal_amounts = {}
        logger.info('操作提示: QmtIpcTrader 已创建，请确保大QMT端 QMT_trade_executor.py 已运行且心跳持续刷新')

    # ------------------------------------------------------------------
    # 目录工具
    # ------------------------------------------------------------------

    def _dir(self, *parts):
        return os.path.join(self.ipc_root, *parts)

    def _ensure_dirs(self):
        for d in [self._dir('orders', 'pending'), self._dir('orders', 'processing'),
                  self._dir('orders', 'done'), self._dir('cancel'), self._dir('status')]:
            os.makedirs(d, exist_ok=True)

    def _write_ipc_config(self):
        """把账号信息写入 IPC 共享配置 config.json，供大QMT端 executor 自动读取。

        免去在 QMT 端脚本里手改 ACCOUNT_ID：策略端每次连接时把当前账号写入，
        大QMT端 QMT_trade_executor.py 启动时从同一文件读取。
        qmt_path 只在用户尚未填写时给出建议默认值，不覆盖用户手填的大QMT路径。
        """
        cfg_path = self._dir('config.json')
        existing = self._read_json(cfg_path) or {}
        previous = dict(existing)
        existing['account_id'] = self.account
        existing['account_type'] = self.account_type
        existing['stocks'] = [self.adjust_stock(code) for code in getattr(config, 'STOCK_POOL', [])]
        existing['quote_interval'] = config.QMT_IPC_QUOTE_INTERVAL_SECONDS
        ipc_secret = getattr(config, 'QMT_IPC_SECRET', '') if config else ''
        if ipc_secret:
            existing['ipc_secret'] = ipc_secret
        # qmt_path：保留用户已填的大QMT路径；未填时用策略端已知 path 作建议默认
        if not existing.get('qmt_path') and self.path:
            existing['qmt_path'] = self.path
        existing['updated_by'] = 'miniQMT'
        if existing == previous:
            return
        existing['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            with open(cfg_path, 'w', encoding='utf-8') as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f'写入 IPC config.json 失败: {e}')

    # ------------------------------------------------------------------
    # 连接与生命周期
    # ------------------------------------------------------------------

    def connect(self):
        """检查 IPC 目录 + 大QMT心跳。返回 (self, self) 表示可用，None 表示不可用。"""
        try:
            self._ensure_dirs()
            self._write_ipc_config()
            if not self._is_qmt_alive():
                log_throttled(logger, 30, 'ipc_connect_failure:' + str(self.account),
                              'IPC 生产端心跳不可用，等待恢复；请核对大 QMT 脚本及输出目录')
                self._connected = False
                return None
            self._connected = True
            reset_log_throttle('ipc_connect_failure:' + str(self.account))
            self._start_deal_poller()
            logger.info('QmtIpcTrader 连接成功（大QMT心跳正常）')
            return (self, self)
        except Exception as e:
            logger.error(f'QmtIpcTrader 连接异常: {e}')
            self._connected = False
            return None

    def _is_qmt_alive(self):
        """检查大QMT心跳文件是否新鲜。"""
        path = self._dir('status', 'heartbeat.json')
        return heartbeat_is_fresh(path, self.heartbeat_max_age)

    def ping_xttrader(self):
        """探测大QMT是否在线（供 position_manager 心跳检查）。"""
        return self._is_qmt_alive()

    def _register_order_id(self, order_id):
        """order_id 自映射(id→id)写入 order_id_map，兼容 position_manager 同步/异步两种模式。

        长时间运行时限制表容量，避免内存无限增长（IPC 每次下单新增一条）。
        """
        with self._cache_lock:
            self.order_id_map[order_id] = order_id
            if len(self.order_id_map) > 4096:
                for k in list(self.order_id_map)[:2048]:
                    self.order_id_map.pop(k, None)

    def _trim_seen_cache(self):
        """限制回调去重缓存容量，避免长时间运行缓慢增长。"""
        with self._cache_lock:
            if len(self._seen_orders) > 4096:
                for k in list(self._seen_orders)[:2048]:
                    self._seen_orders.discard(k)
            if len(self._seen_deals) > 4096:
                for k in list(self._seen_deals)[:2048]:
                    self._seen_deals.pop(k, None)
            self._seen_native_deals = {key for key in self._seen_native_deals if key[0] in self._seen_deals}
            self._seen_deal_amounts = {key: value for key, value in self._seen_deal_amounts.items() if key in self._seen_deals}
            if len(self._timeout_orders) > 4096:
                for k in list(self._timeout_orders)[:2048]:
                    self._timeout_orders.pop(k, None)

    def get_ipc_health(self):
        """返回 IPC 通道健康诊断快照，供控制台/日志排障（参考 xtquant_big_convert metrics）。"""
        def _count(dirpath):
            try:
                return sum(1 for f in os.listdir(dirpath) if f.endswith('.json'))
            except OSError:
                return 0
        hb = self._dir('status', 'heartbeat.json')
        hb_age = round(time.time() - os.path.getmtime(hb), 2) if os.path.exists(hb) else None
        try:
            snapshot = self._read_account_snapshot()
            snapshot_time, snapshot_error = snapshot['timestamp'], None
        except IpcSnapshotUnavailable as exc:
            snapshot_time, snapshot_error = None, str(exc)
        return {
            'readiness': QmtIpcDataAdapter(account=self.account).get_readiness(),
            'snapshot_available': snapshot_error is None,
            'snapshot_time': snapshot_time,
            'snapshot_error': snapshot_error,
            'account': self.account,
            'ipc_root': self.ipc_root,
            'connected': self._connected,
            'qmt_alive': self._is_qmt_alive(),
            'heartbeat_age': hb_age,
            'heartbeat_max_age': self.heartbeat_max_age,
            'pending_count': _count(self._dir('orders', 'pending')),
            'processing_count': _count(self._dir('orders', 'processing')),
            'done_count': _count(self._dir('orders', 'done')),
            'timeout_unknown_count': len(self._timeout_orders),
            'poller_alive': bool(self._poller_thread and self._poller_thread.is_alive()),
        }

    def reconnect_xttrader(self):
        """重连：重新检查心跳并重启轮询线程。"""
        logger.debug('QmtIpcTrader 正在重连（重新检查大QMT心跳）...')
        result = self.connect()
        return result is not None

    def stop(self):
        """停止成交回报轮询线程。"""
        self._poller_stop = True

    # ------------------------------------------------------------------
    # 回调注册 + 后台成交回报轮询线程
    # ------------------------------------------------------------------

    def register_trade_callback(self, cb):
        """注册成交回报回调。由后台轮询线程扫 done/ 目录后触发。"""
        with self._cache_lock:
            if cb not in self._trade_callbacks:
                self._trade_callbacks.append(cb)

    def register_order_callback(self, cb):
        """注册委托状态回调。"""
        with self._cache_lock:
            if cb not in self._order_callbacks:
                self._order_callbacks.append(cb)

    def register_disconnect_callback(self, cb):
        """注册断连回调。心跳超时时触发。"""
        with self._cache_lock:
            if cb not in self._disconnect_callbacks:
                self._disconnect_callbacks.append(cb)

    def _start_deal_poller(self):
        """启动后台线程，轮询 done/ 目录，把新回执转成 callback。"""
        if self._poller_thread and self._poller_thread.is_alive():
            return
        self._poller_stop = False
        self._poller_thread = threading.Thread(target=self._deal_poll_loop, daemon=True)
        self._poller_thread.start()
        logger.info('QmtIpcTrader 成交回报轮询线程已启动')

    def _deal_poll_loop(self):
        """轮询 done/ 目录，触发 trade_callback / order_callback / disconnect_callback。"""
        interval = getattr(config, 'QMT_IPC_DEAL_POLL_INTERVAL', 1.0) if config else 1.0
        was_alive = True
        while not self._poller_stop:
            try:
                # 断连检测
                alive = self._is_qmt_alive()
                if was_alive and not alive:
                    logger.error('QmtIpcTrader 检测到大QMT心跳丢失，触发断连回调')
                    for cb in self._disconnect_callbacks:
                        try:
                            cb()
                        except Exception as e:
                            logger.error(f'disconnect_callback 异常: {e}')
                was_alive = alive

                # 扫描 done/ 回执
                done_dir = self._dir('orders', 'done')
                if os.path.isdir(done_dir):
                    for fname in os.listdir(done_dir):
                        if not fname.endswith('.json'):
                            continue
                        rec = self._read_json(os.path.join(done_dir, fname))
                        if not rec:
                            continue
                        oid = rec.get('order_id')
                        status = rec.get('status', '')

                        # 委托状态回调（每个 order_id 每种状态触发一次）
                        order_key = (str(oid), status)
                        with self._cache_lock:
                            unseen = order_key not in self._seen_orders
                        if unseen and self._fire_order_callback(rec):
                            with self._cache_lock:
                                self._seen_orders.add(order_key)

                        if status in ('filled', 'partial', 'partial_cancelled'):
                            self._dispatch_deals(rec)
                        self._trim_seen_cache()
            except Exception as e:
                logger.warning(f'成交回报轮询异常: {e}')
            time.sleep(interval)

    def _dispatch_deals(self, rec):
        """原生成交按真实编号投递；旧累计回执按金额差换算分笔价。"""
        with self._cache_lock:
            if not self._trade_callbacks:
                return
        key = str(rec.get('order_id'))
        fills = rec.get('deals')
        if isinstance(fills, list):
            with self._cache_lock:
                pending = [deal for deal in fills if (key, str(deal['trade_id'])) not in self._seen_native_deals]
                self._seen_deals[key] = int(rec.get('filled_volume') or 0)
            for deal in pending:
                if self._fire_trade_callback(rec, deal=deal):
                    with self._cache_lock:
                        self._seen_native_deals.add((key, str(deal['trade_id'])))
            return
        with self._cache_lock:
            volume = int(rec.get('filled_volume') or 0)
            delta = volume - int(self._seen_deals.get(key, 0) or 0)
            amount = float(rec.get('filled_amount') or float(rec.get('filled_price', 0)) * volume)
            delta_amount = amount - self._seen_deal_amounts.get(key, 0)
        if delta > 0 and self._fire_trade_callback(
                rec, traded_volume=delta, traded_id_suffix='cum_%s' % volume,
                traded_price=delta_amount / delta):
            with self._cache_lock:
                self._seen_deals[key] = volume
                self._seen_deal_amounts[key] = amount

    def _fire_trade_callback(self, rec, traded_volume=None, traded_id_suffix=None,
                             traded_price=None, deal=None):
        """把回执构造成 XtTrade-like 对象，触发 trade_callbacks。"""
        volume = rec.get('filled_volume', 0) if traded_volume is None else traded_volume
        base_trade_id = str(rec.get('entrust_id', rec.get('order_id', '')))
        if traded_id_suffix:
            base_trade_id = f'{base_trade_id}_{traded_id_suffix}'
        deal = deal or {}
        volume = deal.get('volume', volume)
        price = deal.get('price', rec.get('filled_price', 0) if traded_price is None else traded_price)
        trade = _FakeXtObject(
            order_id=rec.get('order_id'),
            stock_code=rec.get('stock_code', ''),
            traded_volume=volume,
            traded_price=price,
            traded_amount=deal.get('amount', price * volume),
            account_id=self.account,
            account_type=self.account_type,
            traded_id=deal.get('trade_id', base_trade_id),
            traded_time=deal.get('time', self._receipt_time(rec)),
            order_type=_STOCK_BUY if rec.get('action') == 'buy' else _STOCK_SELL,
            order_status=_IPC_STATUS_TO_QMT.get(rec.get('status'), 56),
        )
        with self._cache_lock:
            callbacks = list(self._trade_callbacks)
        success = bool(callbacks)
        for cb in callbacks:
            try:
                cb(trade)
            except Exception as e:
                success = False
                logger.error(f'trade_callback 异常: {e}')
        return success

    def _fire_order_callback(self, rec):
        """只在已注册回调实际投递成功后确认，启动空窗及异常允许下轮重试。"""
        order = self._rec_to_order(rec)
        with self._cache_lock:
            callbacks = list(self._order_callbacks)
        success = bool(callbacks)
        for cb in callbacks:
            try:
                cb(order)
            except Exception as e:
                success = False
                logger.error(f'order_callback 异常: {e}')
        return success

    # ------------------------------------------------------------------
    # 下单
    # ------------------------------------------------------------------

    def _send_ipc_order(self, action, stock_code, volume, price, strategy_name='', order_remark='', price_type=None, wait_for_receipt=None):
        """
        写 pending/ 下单文件 + 轮询 done/ 等待回执。

        Returns:
            int order_id（>0，已提交/成交；超时时已追撤），或 None（确认失败）
        """
        if volume is None or volume <= 0:
            logger.error(f'IPC下单参数错误: volume={volume}')
            return None
        # 快速失败：大QMT离线时不再空等 order_timeout(默认30秒)阻塞策略线程，
        # 立即返回并给出明确错误（断连感知，参考 xtquant_big_convert 的连接健康门禁）。
        if not self._is_qmt_alive():
            logger.error(f'IPC下单中止: 大QMT心跳缺失/过期(>{self.heartbeat_max_age}秒)，判定离线，'
                         f'快速失败不阻塞. {action} {stock_code} {volume}股')
            return None
        try:
            self._read_account_snapshot()
            self._ensure_dirs()
            stock_code = self.adjust_stock(stock_code)
            market_order = price_type == 5 or price is None or float(price) == 0
            price = 0 if market_order else self.select_slippage(stock_code, float(price), action)
            if not market_order and (not math.isfinite(price) or price <= 0):
                raise ValueError('委托价无效')
            order_id = _next_order_id()
            self._register_order_id(order_id)
            order = {
                'version': '1.0',
                'order_id': order_id,
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:23],
                'action': action,
                'stock_code': stock_code,
                'price_type': 'market' if market_order else 'limit',
                'qmt_price_type': int(price_type or (5 if market_order else 11)),
                'price': price,
                'volume': int(volume),
                'strategy': strategy_name or '',
                'timeout_sec': self.order_timeout,
                'remark': order_remark or '',
            }
            ipc_secret = getattr(config, 'QMT_IPC_SECRET', '') if config else ''
            if ipc_secret:
                order['secret'] = ipc_secret
            # 原子写入：.tmp → rename
            tmp = self._dir('orders', 'pending', f'_{order_id}.tmp')
            final = self._dir('orders', 'pending', f'ord_{order_id}.json')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(order, f, ensure_ascii=False, indent=2)
            os.rename(tmp, final)
            logger.info(f'IPC下单已提交: {action} {stock_code} {volume}股 @ {price}, order_id={order_id}')

            if wait_for_receipt is None:
                wait_for_receipt = bool(getattr(config, 'USE_SYNC_ORDER_API', False))
            if not wait_for_receipt:
                return order_id

            # 同步调用仅等待回执；异步调用交由回调和委托跟踪处理。
            deadline = time.time() + self.order_timeout
            while time.time() < deadline:
                rec = self._find_done_record(order_id)
                if rec:
                    status = rec.get('status', '')
                    if status in ('submitted', 'pending', 'filled', 'partial', 'partial_cancelled'):
                        logger.info(f'IPC下单成交: order_id={order_id}, status={status}, '
                                    f"价={rec.get('filled_price')}, 量={rec.get('filled_volume')}")
                        return order_id
                    else:
                        logger.warning(f'IPC下单未成交: order_id={order_id}, status={status}, '
                                       f"error={rec.get('error')}")
                        return None
                time.sleep(0.2)

            logger.warning(f'IPC下单等待回执超时({self.order_timeout}秒): order_id={order_id}')
            self._timeout_orders[order_id] = dict(order)
            self._trim_seen_cache()
            cancel_result = self._write_cancel(order_id)
            logger.warning(
                f'IPC下单状态未知，已提交追撤并返回order_id阻止上层重复下单: '
                f'order_id={order_id}, cancel_result={cancel_result}'
            )
            return order_id
        except Exception as e:
            logger.error(f'IPC下单异常: {e}')
            return None

    def buy(self, security='600031.SH', order_type=_STOCK_BUY, amount=100,
            price_type=None, price=20, strategy_name='', order_remark=''):
        """买入，兼容 easy_qmt_trader.buy()。返回 order_id 或 None。"""
        return self._send_ipc_order('buy', security, amount, price, strategy_name, order_remark, price_type)

    def sell(self, security='600031.SH', order_type=_STOCK_SELL, amount=100,
             price_type=None, price=20, strategy_name='', order_remark=''):
        """卖出，兼容 easy_qmt_trader.sell()。返回 order_id 或 None。"""
        return self._send_ipc_order('sell', security, amount, price, strategy_name, order_remark, price_type)

    def order_stock(self, stock_code='600031.SH', order_type=_STOCK_BUY, order_volume=100,
                    price_type=None, price=20, strategy_name='', order_remark=''):
        """统一下单接口，兼容 easy_qmt_trader.order_stock()。"""
        action = 'buy' if order_type == _STOCK_BUY else 'sell'
        return self._send_ipc_order(action, stock_code, order_volume, price, strategy_name, order_remark, price_type)

    def order_stock_async(self, stock_code='600031.SH', order_type=_STOCK_BUY, order_volume=100,
                          price_type=None, price=20, strategy_name='', order_remark=''):
        """原子发布指令后立即返回本地委托号，不阻塞监控等待柜台。"""
        action = 'buy' if order_type == _STOCK_BUY else 'sell'
        return self._send_ipc_order(action, stock_code, order_volume, price, strategy_name,
                                    order_remark, price_type, wait_for_receipt=False)

    # ------------------------------------------------------------------
    # 撤单
    # ------------------------------------------------------------------

    def _write_cancel(self, order_id):
        """写 cancel/ 撤单文件。返回 0=已提交撤单，-1=委托已完成/无法撤。"""
        try:
            # 已经在 done/ 中且为终态的委托无法撤单
            rec = self._find_done_record(order_id)
            if rec and rec.get('status') in ('filled', 'partial_cancelled', 'rejected',
                                             'cancelled', 'cancelled_timeout', 'error'):
                logger.warning(f'IPC撤单失败: order_id={order_id} 已是终态({rec.get("status")})')
                return -1
            self._ensure_dirs()
            cancel = {
                'version': '1.0',
                'cancel_id': f'CAN_{order_id}',
                'order_id': order_id,
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:23],
                'reason': 'strategy_cancel',
            }
            path = self._dir('cancel', f'cancel_{order_id}.json')
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(cancel, f, ensure_ascii=False, indent=2)
            logger.info(f'IPC撤单指令已提交: order_id={order_id}')
            return 0
        except Exception as e:
            logger.error(f'IPC撤单异常: {e}')
            return -1

    def cancel_order_stock(self, order_id=0):
        """撤单，兼容 easy_qmt_trader.cancel_order_stock()。返回 0=成功，非0=失败。"""
        return self._write_cancel(order_id)

    def cancel_order_stock_async(self, order_id=0):
        """异步撤单，IPC 同步执行。"""
        return self._write_cancel(order_id)

    # ------------------------------------------------------------------
    # 账户快照读取（持仓/资产）
    # ------------------------------------------------------------------

    def _read_account_snapshot(self):
        """读取有效账户快照，失败不能被解释为空仓或零资金。"""
        return read_snapshot(self.ipc_root, self.account, 'account.json',
                             config.QMT_IPC_SNAPSHOT_MAX_AGE_SECONDS)

    def position(self):
        """
        查询持仓，兼容 easy_qmt_trader.position()。

        从 status/account.json 快照构造 DataFrame，列名与 easy_qmt_trader 对齐。
        _sync_real_positions_to_memory 依赖的 5 个必需列：
        证券代码/股票余额/可用余额/成本价/市值
        """
        empty = self._empty_df(_POSITION_COLUMNS)
        self.position_snapshot_valid = False
        snapshot = self._read_account_snapshot()
        positions = snapshot.get('positions')
        if not isinstance(positions, list):
            raise IpcSnapshotUnavailable('持仓列表格式无效')
        if not positions:
            self.position_snapshot_valid = True
            return empty

        data_list = []
        for pos in positions:
            stock = str(pos.get('stock', ''))
            code6 = stock.split('.')[0] if '.' in stock else stock
            volume = pos.get('volume', 0)
            available = pos.get('available')  # 缺失值不能用总持仓冒充可卖量
            if (not code6 or isinstance(volume, bool) or not math.isfinite(float(volume)) or
                    float(volume) < 0 or int(volume) != float(volume)):
                raise IpcSnapshotUnavailable('持仓数量格式无效')
            if available is not None and (isinstance(available, bool) or
                    not math.isfinite(float(available)) or float(available) < 0 or
                    int(available) != float(available) or float(available) > float(volume)):
                raise IpcSnapshotUnavailable('可卖数量格式无效')
            cost = pos.get('cost', 0)
            market_price = pos.get('market_price', 0)
            market_value = pos.get('market_value', market_price * volume)
            data_list.append({
                '账号类型': self.account_type,
                '资金账号': self.account,
                '证券代码': code6,
                '证券名称': pos.get('name') or code6,
                '股票余额': volume,
                '可用余额': available,
                '成本价': cost,
                '参考成本价': cost,
                '市值': market_value,
                '市价': market_price,
                '快照时间': snapshot['timestamp'],
            })
        self.position_snapshot_valid = True
        if not _HAS_PANDAS:
            return data_list
        return pd.DataFrame(data_list)

    def query_stock_positions(self):
        """查询持仓（同 position()）。"""
        return self.position()

    def balance(self):
        """查询账户资产，兼容 easy_qmt_trader.balance()。返回单行 DataFrame。"""
        empty = self._empty_df(_BALANCE_COLUMNS)
        snapshot = self._read_account_snapshot()
        if not snapshot:
            return empty
        if not _HAS_PANDAS:
            return [{
                '账号类型': self.account_type,
                '资金账户': self.account,
                '可用金额': snapshot.get('available', 0),
                '冻结金额': snapshot.get('frozen', 0),
                '持仓市值': snapshot.get('market_value', 0),
                '总资产': snapshot.get('total_asset', 0),
            }]
        df = pd.DataFrame()
        df['账号类型'] = [self.account_type]
        df['资金账户'] = [self.account]
        df['可用金额'] = [snapshot.get('available', 0)]
        df['冻结金额'] = [snapshot.get('frozen', 0)]
        df['持仓市值'] = [snapshot.get('market_value', 0)]
        df['总资产'] = [snapshot.get('total_asset', 0)]
        df['快照时间'] = [snapshot['timestamp']]
        return df

    def query_stock_asset(self):
        """查询资产，兼容 easy_qmt_trader.query_stock_asset()。返回 dict。"""
        snapshot = self._read_account_snapshot()
        return {
            '账号类型': self.account_type,
            '资金账户': self.account,
            '可用金额': snapshot.get('available', 0),
            '冻结金额': snapshot.get('frozen', 0),
            '持仓市值': snapshot.get('market_value', 0),
            '总资产': snapshot.get('total_asset', 0),
        }

    # ------------------------------------------------------------------
    # 委托/成交查询（从 done/ 目录聚合）
    # ------------------------------------------------------------------

    @staticmethod
    def _receipt_time(rec):
        try:
            return int(source_timestamp(rec.get('orig_timestamp') or rec.get('exec_timestamp')))
        except (ValueError, TypeError):
            return 0

    def _rec_to_order(self, rec):
        """把 done/ 回执 dict 转成 XtOrder-like 对象。"""
        oid = rec.get('order_id')
        status = rec.get('status', '')
        volume = rec.get('total_volume', 0)
        filled = rec.get('filled_volume', 0)
        return _FakeXtObject(
            account_type=self.account_type,
            account_id=self.account,
            stock_code=str(rec.get('stock_code', '')),
            order_id=oid,
            order_sysid=str(rec.get('entrust_id', '')),
            order_time=self._receipt_time(rec),
            native_deals=rec.get('deals'),
            order_type=_STOCK_BUY if rec.get('action') == 'buy' else _STOCK_SELL,
            order_volume=volume,
            price_type=rec.get('qmt_price_type', 11),
            price=rec.get('price', 0) or rec.get('filled_price', 0),
            traded_volume=filled,
            traded_price=rec.get('filled_price', 0),
            order_status=_IPC_STATUS_TO_QMT.get(status, 50),
            status_msg=status,
            strategy_name=rec.get('strategy', ''),
            order_remark=rec.get('remark', ''),
        )

    def _read_all_orders(self):
        """合并回执及在途文件；终态回执优先，避免新发布委托漏算预占。"""
        orders = {}
        cutoff = time.time() - self.done_lookback
        for directory in ('pending', 'processing', 'done'):
            path = self._dir('orders', directory)
            if not os.path.isdir(path):
                continue
            for fname in os.listdir(path):
                if not fname.endswith('.json'):
                    continue
                fpath = os.path.join(path, fname)
                try:
                    if directory == 'done' and os.path.getmtime(fpath) < cutoff:
                        continue
                except OSError:
                    continue
                rec = self._read_json(fpath)
                if not rec:
                    continue
                if rec.get('account_id') and str(rec['account_id']) != str(self.account):
                    continue
                if directory != 'done':
                    rec = dict(rec, status='pending', total_volume=rec.get('volume', 0),
                               orig_timestamp=rec.get('timestamp'))
                order = self._rec_to_order(rec)
                orders[str(order.order_id)] = order
        return list(orders.values())

    def query_stock_orders(self):
        """查询当日委托，兼容 easy_qmt_trader.query_stock_orders()。返回 DataFrame。"""
        orders = self._read_all_orders()
        return self._orders_to_df(orders)

    def today_entrusts(self):
        """今日委托（同 query_stock_orders）。"""
        return self.query_stock_orders()

    def query_stock_trades(self):
        """查询当日成交，兼容 easy_qmt_trader.query_stock_trades()。返回 DataFrame。"""
        orders = [o for o in self._read_all_orders() if o.order_status in (53, 55, 56)]
        return self._trades_to_df(orders)

    def today_trades(self):
        """今日成交（同 query_stock_trades）。"""
        return self.query_stock_trades()

    def get_active_orders_by_stock(self, stock_code):
        """查询指定股票的活跃委托列表（对象），兼容 easy_qmt_trader。"""
        stock_code = self.adjust_stock(stock_code)
        active_status = [48, 49, 50, 51, 52, 55]
        result = []
        for o in self._read_all_orders():
            order_stock = str(o.stock_code)
            if (order_stock == stock_code or order_stock[:6] == stock_code[:6]) \
                    and o.order_status in active_status:
                result.append(o)
        return result

    def get_active_order_info_by_stock(self, stock_code):
        """查询指定股票的活跃委托信息（dict 列表），兼容 easy_qmt_trader。"""
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
    # 辅助工具方法（与 easy_qmt_trader 对齐，纯逻辑）
    # ------------------------------------------------------------------

    def adjust_stock(self, stock='600031.SH'):
        """调整股票代码为带交易所后缀格式。"""
        if stock is None:
            return stock
        code = str(stock).strip().upper()
        if not code:
            return code
        if code.startswith(('SH.', 'SZ.', 'BJ.')):
            market, pure = code.split('.', 1)
            return f'{pure}.{market}' if len(pure) == 6 and pure.isdigit() else code
        if code.endswith(('.SH', '.SZ', '.BJ')):
            return code
        if '.' in code:
            return code
        if code.startswith('920'):
            return code + '.BJ'
        if code[:3] in ['000', '001', '002', '003', '200', '300', '301',
                        '111', '123', '127', '128', '131'] or code[:2] in ['12', '15', '16', '18']:
            return code + '.SZ'
        if code[:3] in ['600', '601', '603', '605', '688', '689', '900',
                        '510', '511', '512', '513', '514', '515', '516', '517', '518', '588',
                        '110', '113', '118', '132', '204'] or code[:1] in ['5', '6', '9']:
            return code + '.SH'
        return code

    def select_data_type(self, stock='600031'):
        """判断标的类型：bond/fund/stock。"""
        s = stock.split('.')[0] if '.' in stock else stock
        if s[:3] in ['110', '113', '123', '127', '128', '111', '118'] or s[:2] in ['11', '12']:
            return 'bond'
        if s[:3] in ['510', '511', '512', '513', '514', '515', '516', '517', '518',
                     '588', '159', '501', '164'] or s[:2] in ['16']:
            return 'fund'
        return 'stock'

    def select_slippage(self, stock='600031', price=15.01, trader_type='buy'):
        """滑点计算，与 easy_qmt_trader 一致。"""
        if not price or price <= 0:
            return price
        data_type = self.select_data_type(stock)
        slippage = self.slippage / 10 if data_type in ('fund', 'bond') else self.slippage
        if trader_type in ('buy', _STOCK_BUY, 23):
            return price + slippage
        return price - slippage

    def check_stock_is_av_buy(self, stock='128036', price=156.7, amount=10, hold_limit=100000):
        """检查资金是否够买入。"""
        try:
            snapshot = self._read_account_snapshot()
            cash = float(snapshot.get('available', 0))
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
        """检查持仓是否够卖出。"""
        try:
            snapshot = self._read_account_snapshot()
            stock6 = stock.split('.')[0] if '.' in stock else stock
            for pos in snapshot.get('positions', []):
                pos_stock = str(pos.get('stock', ''))
                pos6 = pos_stock.split('.')[0] if '.' in pos_stock else pos_stock
                if pos6 == stock6:
                    from order_utils import position_quantities
                    quantities = position_quantities(pos)
                    available = quantities[1] if quantities is not None else None
                    if available is not None and amount > 0 and available >= amount:
                        logger.info(f'允许卖出 股票={stock}, 可用={available} >= 卖出={amount}')
                        return True
                    logger.warning(f'不允许卖出,持股不足 股票={stock}, 可用={available} < 卖出={amount}')
                    return False
            logger.warning(f'不允许卖出,无持股 股票={stock}')
            return False
        except Exception as e:
            logger.error(f'check_stock_is_av_sell 异常: {e}')
            return False

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _read_json(self, path):
        """安全读取 JSON 文件，失败返回 None。"""
        try:
            if not os.path.exists(path):
                return None
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return None

    def _find_done_record(self, order_id):
        """在 done/ 目录查找指定 order_id 的回执。"""
        done_dir = self._dir('orders', 'done')
        if not os.path.isdir(done_dir):
            return None
        # 优先按标准文件名精确查找
        exact = os.path.join(done_dir, f'ord_{order_id}.json')
        rec = self._read_json(exact)
        if rec:
            return rec
        # 回退：遍历匹配 order_id 字段
        for fname in os.listdir(done_dir):
            if not fname.endswith('.json'):
                continue
            rec = self._read_json(os.path.join(done_dir, fname))
            if rec and str(rec.get('order_id')) == str(order_id):
                return rec
        return None

    def _write_cancel_dir_check(self):
        return self._dir('cancel')

    @staticmethod
    def _empty_df(columns):
        if not _HAS_PANDAS:
            return []
        return pd.DataFrame(columns=columns)

    def _orders_to_df(self, orders):
        """委托对象列表 → DataFrame（与 easy_qmt_trader.query_stock_orders 兼容）。"""
        if not orders:
            return self._empty_df([])
        rows = [{
            '账号类型': o.account_type,
            '资金账号': o.account_id,
            '证券代码': str(o.stock_code)[:6],
            '订单编号': o.order_id,
            '柜台合同编号': getattr(o, 'order_sysid', ''),
            '报单时间': o.order_time,
            '委托类型': o.order_type,
            '委托数量': o.order_volume,
            '报价类型': getattr(o, 'price_type', 50),
            '委托价格': o.price,
            '成交数量': o.traded_volume,
            '成交均价': o.traded_price,
            '委托状态': o.order_status,
            '委托状态描述': getattr(o, 'status_msg', ''),
            '策略名称': o.strategy_name,
            '委托备注': o.order_remark,
        } for o in orders]
        return pd.DataFrame(rows) if _HAS_PANDAS else rows

    def _trades_to_df(self, orders):
        """原生回执逐笔保留成交编号、价量与时间，兼容旧累计回执。"""
        rows = []
        for order in orders:
            fills = getattr(order, 'native_deals', None)
            if fills is None:
                fills = [{'trade_id': getattr(order, 'order_sysid', ''),
                          'time': order.order_time, 'price': order.traded_price,
                          'volume': order.traded_volume,
                          'amount': order.traded_price * order.traded_volume}]
            for fill in fills:
                rows.append({
                    '账号类型': order.account_type, '资金账号': order.account_id,
                    '证券代码': str(order.stock_code)[:6], '委托类型': order.order_type,
                    '成交编号': fill['trade_id'], '成交时间': fill['time'],
                    '成交均价': fill['price'], '成交数量': fill['volume'],
                    '成交金额': fill['amount'], '订单编号': order.order_id,
                    '柜台合同编号': getattr(order, 'order_sysid', ''),
                    '策略名称': order.strategy_name, '委托备注': order.order_remark,
                })
        return pd.DataFrame(rows) if _HAS_PANDAS else rows
