# _qmt_trader_base.py
# 大QMT 降级交易客户端的共享基础件（与传输方式无关的纯逻辑）。
#
# 抽出 QmtIpcTrader / QmtRpcTrader 共用的：DataFrame 列名、Fake 对象、买卖方向常量、
# IPC/RPC 回执 status → QMT 委托状态码映射、order_id 生成、代码规整/滑点/资金校验、
# 委托/成交对象 → DataFrame 的转换。
#
# 注意：现有 qmt_ipc_trader.py 保持原样不改（已通过 70+ 回归测试），本模块仅供
# qmt_rpc_trader.py 复用，避免两份重复代码，同时不冒回归 IPC 路径的风险。

import time
import threading

try:
    import pandas as pd
    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False


# ── 持仓 DataFrame 列名（与 easy_qmt_trader.position() 兼容）──
POSITION_COLUMNS = [
    '账号类型', '资金账号', '证券代码', '股票余额', '可用余额',
    '成本价', '市值', '选择', '持股天数', '交易状态', '明细',
    '证券名称', '冻结数量', '市价', '盈亏', '盈亏比(%)',
    '当日买入', '当日卖出',
]

# ── 资产 DataFrame 列名（与 easy_qmt_trader.balance() 兼容）──
BALANCE_COLUMNS = [
    '账号类型', '资金账户', '可用金额', '冻结金额', '持仓市值', '总资产',
]

# QMT 买卖方向常量（与 xtconstant 对齐，避免强依赖 xtquant 导入）
STOCK_BUY = 23
STOCK_SELL = 24

# 委托状态 → QMT 委托状态码映射
# QMT 状态码：48未报 49待报 50已报 51已报待撤 52部分待撤 53部撤
#             54已撤 55部成 56已成 57废单
STATUS_TO_QMT = {
    'filled': 56, 'partial': 55, 'pending': 50, 'reported': 50,
    'rejected': 57, 'error': 57, 'cancelled': 54,
    'cancelled_timeout': 54, 'cancelled_by_user': 54,
}

# 活跃（可撤）委托状态集合
ACTIVE_ORDER_STATUS = (48, 49, 50, 51, 52, 55)

# 进程内 order_id 自增序号（保证纯整数且唯一，IPC/RPC 共享同一序列）
_order_seq = 0
_order_seq_lock = threading.Lock()


def next_order_id():
    """生成纯整数、进程内唯一的 order_id。

    position_manager 会对 order_id 做 int() 转换，因此必须是纯数字，
    不能用字符串前缀格式。
    """
    global _order_seq
    with _order_seq_lock:
        _order_seq = (_order_seq + 1) % 10000
    return (int(time.time()) % 100000000) * 10000 + _order_seq


class FakeAccount:
    """占位账号对象，兼容 easy_qmt_trader.acc。"""
    def __init__(self, account_id, account_type='STOCK'):
        self.account_id = account_id
        self.account_type = account_type


class FakeXtObject:
    """把关键字参数转成属性访问对象，模拟 XtOrder / XtTrade。"""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


# ------------------------------------------------------------------
# 纯逻辑工具（与 easy_qmt_trader 对齐）
# ------------------------------------------------------------------

def adjust_stock(stock='600031.SH'):
    """调整股票代码为带交易所后缀格式。"""
    stock = str(stock).strip()
    if stock[-2:].upper() in ('SH', 'SZ'):
        return stock.upper()
    if stock[:3] in ['600', '601', '603', '688', '510', '511', '512', '513',
                     '515', '113', '110', '118', '501'] or stock[:2] in ['11']:
        return stock + '.SH'
    return stock + '.SZ'


def select_data_type(stock='600031'):
    """判断标的类型：bond/fund/stock。"""
    s = stock.split('.')[0] if '.' in stock else stock
    if s[:3] in ['110', '113', '123', '127', '128', '111', '118'] or s[:2] in ['11', '12']:
        return 'bond'
    if s[:3] in ['510', '511', '512', '513', '514', '515', '516', '517', '518',
                 '588', '159', '501', '164'] or s[:2] in ['16']:
        return 'fund'
    return 'stock'


def select_slippage(stock='600031', price=15.01, trader_type='buy', slippage=0.01):
    """滑点计算，与 easy_qmt_trader 一致。"""
    if not price or price <= 0:
        return price
    data_type = select_data_type(stock)
    slip = slippage / 10 if data_type in ('fund', 'bond') else slippage
    if trader_type in ('buy', STOCK_BUY, 23):
        return price + slip
    return price - slip


# ------------------------------------------------------------------
# DataFrame 构造
# ------------------------------------------------------------------

def empty_df(columns):
    if not _HAS_PANDAS:
        return []
    return pd.DataFrame(columns=columns)


def orders_to_df(orders):
    """委托对象列表 → DataFrame（与 easy_qmt_trader.query_stock_orders 兼容）。"""
    if not orders:
        return empty_df([])
    rows = [{
        '账号类型': getattr(o, 'account_type', ''),
        '资金账号': getattr(o, 'account_id', ''),
        '证券代码': str(o.stock_code)[:6],
        '订单编号': o.order_id,
        '柜台合同编号': getattr(o, 'order_sysid', ''),
        '报单时间': getattr(o, 'order_time', 0),
        '委托类型': o.order_type,
        '委托数量': o.order_volume,
        '报价类型': getattr(o, 'price_type', 50),
        '委托价格': o.price,
        '成交数量': o.traded_volume,
        '成交均价': getattr(o, 'traded_price', 0),
        '委托状态': o.order_status,
        '委托状态描述': getattr(o, 'status_msg', ''),
        '策略名称': getattr(o, 'strategy_name', ''),
        '委托备注': getattr(o, 'order_remark', ''),
    } for o in orders]
    return pd.DataFrame(rows) if _HAS_PANDAS else rows


def trades_to_df(orders):
    """成交对象列表 → DataFrame（与 easy_qmt_trader.query_stock_trades 兼容）。"""
    if not orders:
        return empty_df([])
    rows = [{
        '账号类型': getattr(o, 'account_type', ''),
        '资金账号': getattr(o, 'account_id', ''),
        '证券代码': str(o.stock_code)[:6],
        '委托类型': o.order_type,
        '成交编号': getattr(o, 'order_sysid', ''),
        '成交时间': getattr(o, 'order_time', 0),
        '成交均价': o.traded_price,
        '成交数量': o.traded_volume,
        '成交金额': o.traded_price * o.traded_volume,
        '订单编号': o.order_id,
        '柜台合同编号': getattr(o, 'order_sysid', ''),
        '策略名称': getattr(o, 'strategy_name', ''),
        '委托备注': getattr(o, 'order_remark', ''),
    } for o in orders]
    return pd.DataFrame(rows) if _HAS_PANDAS else rows
