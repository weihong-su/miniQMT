# -*- coding: utf-8 -*-
"""
券商对账单导入与历史回填。

为什么需要它：QMT 的 xttrader **没有任何历史成交查询接口**
（`query_stock_trades` 只返回当日），所以库里既成的
`time_source='local_fallback'` 记录（成交时间实为本地入库时刻）
只能靠券商对账单升级为 `'broker'`，并获得真实手续费。

本模块只做纯逻辑（解析 / 匹配 / 回填决策），不碰命令行与文件系统副作用，
便于单元测试。CLI 在 scripts/import_broker_statement.py。
"""
import csv
import io
import os
import re
from datetime import datetime

from logger import get_logger

logger = get_logger('broker_import')

# 对账单文件默认编码（QMT 导出为 GBK）
ENCODINGS = ('utf-8-sig', 'utf-8', 'gbk', 'gb18030')

# 匹配容差（秒）。仅在按编号匹配失败时才走时间邻近兜底。
DEFAULT_TIME_TOLERANCE_SEC = 60

MATCH_BY_TRADED_ID = 'traded_id'
MATCH_BY_ORDER_ID = 'order_id+code'
MATCH_BY_NEAREST = 'nearest'
MATCH_UNMATCHED = 'unmatched'

# 对账单给不出有效值时的占位
MISSING = (None, '', '-', '--', 'N/A')


# ============================== 解析工具 ==============================

def read_csv_rows(path):
    """读 CSV，自动探测编码。返回 (rows, encoding)。"""
    raw = open(path, 'rb').read()
    for enc in ENCODINGS:
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode('utf-8', errors='replace')
        enc = 'utf-8(replace)'
    # 去掉可能的 BOM
    if text.startswith('﻿'):
        text = text[1:]
    reader = csv.DictReader(io.StringIO(text))
    rows = [{k.strip(): (v.strip() if isinstance(v, str) else v)
             for k, v in row.items() if k is not None}
            for row in reader]
    # 全空行丢弃（表头-only 文件会得到 0 行）
    rows = [r for r in rows if any(v for v in r.values())]
    return rows, enc


def to_float(value, default=None):
    if value in MISSING:
        return default
    try:
        f = float(str(value).replace(',', ''))
        return default if f != f else f
    except (TypeError, ValueError):
        return default


def to_int(value, default=None):
    f = to_float(value, None)
    return default if f is None else int(f)


def norm_code(raw):
    """6 位纯数字代码，保留前导零。"""
    text = str(raw or '').strip()
    text = re.sub(r'^(sh|sz|bj)\.?', '', text, flags=re.I)
    return text.split('.')[0]


def extract_account(text, fallback=None):
    """从 '2____10064____001____49____25105132____' 这类串里取账号。

    取最后一段连续 6-12 位数字 —— 前面的分节号（10064/001/49）长度不一，
    只有末尾是资金账号。
    """
    runs = re.findall(r'\d{6,12}', str(text or ''))
    return runs[-1] if runs else fallback


def parse_side(op_text, buy_text='买入', sell_text='卖出'):
    text = str(op_text or '')
    if buy_text in text or '买' in text:
        return 'BUY'
    if sell_text in text or '卖' in text:
        return 'SELL'
    return None


def parse_deal_time(date_text, time_text):
    """对账单的 成交日期(yyyymmdd) + 成交时间(HHMMSS，可能只有 5 位)。

    返回 (epoch, 'YYYY-MM-DD HH:MM:SS')；解析不出返回 (None, None)。
    """
    try:
        day = str(date_text or '').strip()
        if len(day) != 8 or not day.isdigit():
            return None, None
        hhmmss = str(time_text or '').strip().zfill(6)
        if len(hhmmss) != 6 or not hhmmss.isdigit():
            return None, None
        dt = datetime.strptime(day + hhmmss, '%Y%m%d%H%M%S')
        return int(dt.timestamp()), dt.strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError):
        return None, None


# ============================== 各文件解析 ==============================

def parse_deals(rows, source_file='', default_account=None):
    """解析成交明细。这是回填的主力数据。"""
    result = []
    for row in rows:
        account = extract_account(row.get('账号'), default_account)
        code = norm_code(row.get('证券代码'))
        if not code:
            continue
        epoch, deal_time_str = parse_deal_time(row.get('成交日期'), row.get('成交时间'))
        side = parse_side(row.get('操作'))
        result.append({
            'account': account,
            'code': code,
            'stock_name': row.get('证券名称') or '',
            'side': side,
            'price': to_float(row.get('成交价格')),
            'volume': to_int(row.get('成交数量')),
            'amount': to_float(row.get('成交金额')),
            'commission': to_float(row.get('手续费')),
            'stamp_duty': to_float(row.get('印花税')),
            'transfer_fee': to_float(row.get('过户费')),
            'broker_traded_id': str(row.get('成交编号') or '').strip() or None,
            'broker_order_id': str(row.get('订单编号') or '').strip() or None,
            'broker_order_ref': str(row.get('委托编号') or '').strip() or None,
            'strategy_name': row.get('策略名称') or '',
            'deal_date': _date_of(deal_time_str),
            'deal_time': deal_time_str[11:] if deal_time_str else None,
            'deal_time_str': deal_time_str,
            'deal_time_epoch': epoch,
            'import_file': source_file,
        })
    return result


def parse_orders(rows, source_file='', default_account=None):
    result = []
    for row in rows:
        account = extract_account(row.get('账号'), default_account)
        code = norm_code(row.get('证券代码'))
        if not code:
            continue
        result.append({
            'account': account,
            'code': code,
            'order_date': row.get('委托日期'),
            'order_time': str(row.get('委托时间') or '').zfill(6),
            'side': parse_side(row.get('操作')),
            'order_ref': str(row.get('委托编号') or '').strip() or None,
            'broker_order_id': str(row.get('订单编号') or '').strip() or None,
            'status': row.get('委托状态') or '',
            'price': to_float(row.get('委托价格')),
            'volume': to_int(row.get('委托数量')),
            'traded_volume': to_int(row.get('成交数量'), 0),
            'cancel_volume': to_int(row.get('撤单数量'), 0),
            'reject_reason': row.get('废单原因') or '',
            'strategy_name': row.get('策略名称') or '',
            'import_file': source_file,
        })
    return result


def parse_fund_flows(rows, source_file='', default_account=None):
    """资金流水（出入金）。columns 见 QMT 导出的 stkFundFlow.csv。"""
    result = []
    for row in rows:
        account = extract_account(row.get('资金账号') or row.get('账号'), default_account)
        epoch, flow_str = parse_deal_time(row.get('发生日期'), row.get('发生时间'))
        result.append({
            'account': account,
            'flow_time': flow_str,
            'flow_type': row.get('业务类型') or row.get('业务名称') or '',
            'amount': to_float(row.get('发生数量/金额')),
            'note': row.get('业务名称') or '',
            'broker_ref': str(row.get('流水序号') or '').strip() or None,
            'import_file': source_file,
        })
    return result


def parse_delivery(rows, source_file='', default_account=None):
    """交割单。字段与成交明细高度重叠，手续费/印花税/杂费是分开的列。"""
    result = []
    for row in rows:
        account = extract_account(row.get('资金账号') or row.get('股票账号'), default_account)
        code = norm_code(row.get('证券代码'))
        if not code:
            continue
        epoch, deal_time_str = parse_deal_time(row.get('日期'), row.get('成交时间'))
        commission = to_float(row.get('手续费'), 0.0) or 0.0
        stamp = to_float(row.get('印花税'), 0.0) or 0.0
        other = to_float(row.get('其它杂费'), 0.0) or 0.0
        result.append({
            'account': account,
            'code': code,
            'stock_name': row.get('证券名称') or '',
            'side': parse_side(row.get('买卖') or row.get('操作')),
            'price': to_float(row.get('成交价格')),
            'volume': to_int(row.get('成交数量')),
            'amount': to_float(row.get('成交金额')),
            'commission': commission,
            'stamp_duty': stamp,
            'transfer_fee': other,
            'commission_total': commission + stamp + other,
            'broker_traded_id': str(row.get('成交序号') or '').strip() or None,
            'broker_order_ref': str(row.get('委托号') or '').strip() or None,
            'deal_date': _date_of(deal_time_str),
            'deal_time': deal_time_str[11:] if deal_time_str else None,
            'deal_time_str': deal_time_str,
            'deal_time_epoch': epoch,
            'import_file': source_file,
        })
    return result


def _date_of(deal_time_str):
    return deal_time_str[:10].replace('-', '') if deal_time_str else None


# ============================== 匹配 ==============================

def load_local_trades(conn, account=None, include_superseded=False):
    """读本地成交流水，供匹配使用。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trade_records)")}
    where = []
    params = []
    if 'row_status' in cols and not include_superseded:
        where.append("COALESCE(row_status,'active')='active'")
    if account and 'account' in cols:
        where.append("(account=? OR account IS NULL)")
        params.append(account)
    sql = ("SELECT id, stock_code, trade_time, trade_type, price, volume, amount, "
           "trade_id, commission, strategy, "
           + ("COALESCE(deal_time_str, trade_time)" if 'deal_time_str' in cols
              else "trade_time") + " AS effective_time "
           + "FROM trade_records")
    if where:
        sql += " WHERE " + " AND ".join(where)
    out = []
    for r in conn.execute(sql, params):
        out.append({
            'id': r['id'], 'code': norm_code(r['stock_code']),
            'trade_time': str(r['trade_time'] or '').replace('T', ' '),
            'effective_time': str(r['effective_time'] or '').replace('T', ' '),
            'side': str(r['trade_type'] or '').upper(),
            'price': to_float(r['price'], 0.0),
            'volume': to_int(r['volume'], 0),
            'amount': to_float(r['amount'], 0.0),
            'trade_id': str(r['trade_id']) if r['trade_id'] is not None else None,
            'commission': to_float(r['commission'], 0.0),
            'strategy': r['strategy'],
        })
    return out


def match_deals(broker_deals, local_trades, time_tolerance_sec=DEFAULT_TIME_TOLERANCE_SEC):
    """把对账单成交匹配到本地流水。

    匹配优先级（实测 2026-09-11 的对账单 13 笔全部命中）：
      1. 成交编号 == trade_id          —— 最强，直接唯一
      2. 订单编号 == trade_id 且 代码相同 —— 网格路径写的是 order_id，
         而 order_id 会跨股跨日复用，必须用代码消歧
      3. 代码+方向+价量相同且时间邻近    —— 兜底

    返回 (matches, unmatched_broker)，matches 元素含 matched local id 与匹配方式。
    """
    by_traded_id = {}
    by_order_code = {}
    for t in local_trades:
        if t['trade_id']:
            by_traded_id.setdefault(t['trade_id'], []).append(t)
            by_order_code.setdefault((t['trade_id'], t['code']), []).append(t)

    used = set()
    matches = []
    unmatched = []

    for deal in broker_deals:
        picked, method = None, None

        # 1) 成交编号精确匹配
        candidates = [t for t in by_traded_id.get(deal['broker_traded_id'], [])
                      if t['id'] not in used]
        if len(candidates) == 1:
            picked, method = candidates[0], MATCH_BY_TRADED_ID

        # 2) 订单号 + 代码消歧
        if picked is None and deal.get('broker_order_id'):
            candidates = [t for t in by_order_code.get(
                (deal['broker_order_id'], deal['code']), []) if t['id'] not in used]
            if len(candidates) == 1:
                picked, method = candidates[0], MATCH_BY_ORDER_ID

        # 3) 代码+方向+价量+时间邻近兜底
        if picked is None:
            picked, method = _nearest_match(deal, local_trades, used, time_tolerance_sec)

        if picked is None:
            unmatched.append(deal)
            continue

        used.add(picked['id'])
        matches.append({'broker': deal, 'local': picked, 'method': method})

    return matches, unmatched


def _nearest_match(deal, local_trades, used, tolerance_sec):
    best, best_gap = None, None
    for t in local_trades:
        if t['id'] in used:
            continue
        if t['code'] != deal['code'] or t['side'] != deal['side']:
            continue
        if t['volume'] != deal['volume']:
            continue
        if abs((t['price'] or 0) - (deal['price'] or 0)) > 0.0001:
            continue
        gap = _time_gap_sec(t['trade_time'], deal['deal_time_str'])
        if gap is None or gap > tolerance_sec:
            continue
        if best_gap is None or gap < best_gap:
            best, best_gap = t, gap
    return best, MATCH_BY_NEAREST if best is not None else None


def _time_gap_sec(local_time, broker_time):
    try:
        a = datetime.strptime(str(local_time)[:19].replace('T', ' '),
                              '%Y-%m-%d %H:%M:%S')
        b = datetime.strptime(str(broker_time)[:19], '%Y-%m-%d %H:%M:%S')
        return abs((a - b).total_seconds())
    except (ValueError, TypeError):
        return None


# ============================== 回填决策 ==============================

def plan_backfill(match):
    """决定单条匹配要改哪些字段。返回 (updates dict, notes list)。

    刻意保守：
    - 成交时间一律以对账单为准（这正是导入的目的）
    - 手续费只在对账单给出 **> 0** 的值时才覆盖 —— 对账单里 0.00 通常表示
      "该字段未导出"而不是"真的免费"，用它覆盖会让估算值变得更差
    - trade_id 只在当前是 order_id 形态时才改写为真实成交编号
    """
    deal, local = match['broker'], match['local']
    updates, notes = {}, []

    if deal.get('deal_time_str'):
        updates['deal_time_str'] = deal['deal_time_str']
        updates['deal_time'] = deal['deal_time_epoch']
        updates['time_source'] = 'broker'
    else:
        notes.append('对账单未给出成交时间，保持原值')

    broker_fee_total = _broker_fee_total(deal)
    if broker_fee_total is not None and broker_fee_total > 0:
        updates['commission'] = round(broker_fee_total, 4)
        updates['commission_source'] = 'broker'
    else:
        notes.append('对账单手续费为 0/缺失，保留本地估算值')

    real_id = deal.get('broker_traded_id')
    if real_id and str(local['trade_id']) != str(real_id):
        if _looks_like_order_id(local['trade_id']):
            updates['trade_id'] = str(real_id)
            updates['trade_id_source'] = 'traded_id'
        else:
            notes.append(f"本地 trade_id={local['trade_id']} 非 order_id 形态，不改写")

    return updates, notes


def _broker_fee_total(deal):
    parts = [deal.get('commission'), deal.get('stamp_duty'), deal.get('transfer_fee')]
    present = [p for p in parts if p is not None]
    if not present:
        return None
    if 'commission_total' in deal and deal['commission_total'] is not None:
        return deal['commission_total']
    return sum(present)


def _looks_like_order_id(trade_id):
    text = str(trade_id or '')
    return text.isdigit() and 8 <= len(text) <= 12
