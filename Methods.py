"""用来创建公用的方法，方便程序进行调用"""

# from stockquant.quant import *
import os
import pandas as pd
import datetime
import time
import requests
import json
from MyTT import *
from mootdx.quotes import Quotes
import config
from logger import suppress_stdout_stderr

def backInDays(nday):
    """用来获得n天前的日期，用于从数据接口请求股票数据，避免一次要求过多数据影响程序效率"""
    """建议：30m数据，取值60，即回溯2个月的数据，约40个交易日，320个数据点，最多用于计算MA250"""
    """同理：60m数据，取值120; 日线数据，取值480; 周线数据，取值2400"""
    # 获取当前时间并减去n天
    n_days_back = datetime.datetime.now() - datetime.timedelta(days=float(nday))
    # 将时间转换为字符串格式
    n_days_back_str = n_days_back.strftime("%Y-%m-%d")
    return n_days_back_str


# 对code列进行处理, 在调用baostock接口前添加前缀
def add_bs_prefix(code):
    if code.startswith(('600', '601', '603', '688', '510', '511', '512', '513', '515', '113', '110', '118', '501')):
        return 'sh.' + code
    elif code.startswith(('0', '3')):
        return 'sz.' + code
    else:
        return code
    
# 对code列进行处理, 在调用xtquant接口前添加后缀
def add_xt_suffix(stock='600031.SH'):
    '''
    调整代码
    '''
    if stock is None:
        return stock

    stock = str(stock).strip().upper()
    if not stock:
        return stock

    if stock.startswith(('SH.', 'SZ.', 'BJ.')):
        market, code = stock.split('.', 1)
        return f'{code}.{market}' if len(code) == 6 and code.isdigit() else stock

    if stock.endswith(('.SH', '.SZ', '.BJ')):
        return stock

    if '.' in stock:
        return stock

    code = stock
    if len(code) != 6 or not code.isdigit():
        return stock

    if code.startswith('920'):
        return f'{code}.BJ'

    # 深市需先判断，避免 111xxx 可转债被 11xxx 沪市规则误吸收。
    if code.startswith((
        '000', '001', '002', '003',  # 深市主板
        '200',                      # 深市B股
        '300', '301',               # 创业板
        '111', '123', '127', '128', # 深市可转债
        '131',                      # 深市债券回购
        '15',                       # 深市ETF/分级基金等场内基金
        '16', '18',                 # 深市LOF/封基等基金
        '12'                        # 深市债券
    )):
        return f'{code}.SZ'

    if code.startswith((
        '5', '6', '9',              # 沪市基金/ETF/股票/B股
        '110', '113', '118', '132', # 沪市可转债/可交换债
        '204'                       # 沪市债券回购
    )):
        return f'{code}.SH'

    return stock

# 对code列进行分类, 调用xtquant接口
def select_data_type( stock='600031'):
    '''
    选择数据类型
    '''
    if stock[:3] in ['110','113','123','127','128','111','118'] or stock[:2] in ['11','12']:
        return 'bond'
    elif stock[:3] in ['510','511','512','513','514','515','516','517','518','588','159','501','164'] or stock[:2] in ['16']:
        return 'fund'
    else:
        return 'stock'

# 股票数据请求，用Baostock或者mootdx
# Baostock方式：
#       res = getStockData('600519', fields="date,open,high,low,close,preclose,volume,amount", start_date=Methods.backInDays(500), freq='d', adjustflag='2')
# mootdx方式：
#       res = Methods.getStockData('600519', offset=800, freq=9, adjustflag='qfq') 
#       res['datetime'] = pd.to_datetime(res['datetime']).dt.date
#       res = res.rename(columns={'datetime': 'date'})
#       res = res.reindex(columns=['date', 'open', 'high', 'low', 'close', 'preclose', 'volume', 'amount'])
#       res = res.reset_index(drop=True)
def _mootdx_daily_bars(code, freq, offset, adjustflag):
    """用 mootdx(通达信) 拉取日/周/月线。"""
    mootdx_freq = {'d': 9, 'w': 5, 'm': 6}[freq]
    if code.startswith(("sh.", "sz.")):
        code = code.split('.')[1]
    client = Quotes.factory('std')  # 使用标准版通达信数据
    return client.bars(symbol=code, frequency=mootdx_freq, offset=offset, adjust=adjustflag)


def _baostock_daily_bars(code, fields, start_date, end_date, freq, adjustflag):
    """用 baostock 拉取日/周/月线；不可用/失败时返回 None 以便上层降级到 mootdx。

    适配新版 baostock(0.9.x) 收紧后的访问：登录前应用 API Key、复权类型
    归一化为 '1'/'2'/'3'、登录与查询错误码显式校验、确保 logout 释放连接。
    """
    try:
        import baostock as bs
    except ImportError:
        print("未安装 baostock，历史数据降级使用 mootdx")
        return None

    import baostock_helper
    bs_code = add_bs_prefix(code)
    bs_adjustflag = baostock_helper.normalize_adjustflag(adjustflag)
    logged_in = False
    try:
        with suppress_stdout_stderr():
            baostock_helper.apply_api_key(bs)
            lg = bs.login()
        if lg is None or lg.error_code != '0':
            err = baostock_helper.describe_login_error(
                getattr(lg, 'error_code', ''), getattr(lg, 'error_msg', ''))
            print(f"baostock 登录失败: {err}")
            return None
        logged_in = True

        with suppress_stdout_stderr():
            result = bs.query_history_k_data_plus(
                bs_code, fields,
                start_date=start_date, end_date=end_date,
                frequency=freq, adjustflag=bs_adjustflag)
        if result is None or result.error_code != '0':
            print(f"baostock 查询失败: {getattr(result, 'error_msg', 'unknown')}")
            return None

        data_list = []
        while (result.error_code == '0') & result.next():
            data_list.append(result.get_row_data())
        return pd.DataFrame(data_list, columns=result.fields)
    except Exception as e:
        print(f"baostock 历史数据异常: {e}")
        return None
    finally:
        if logged_in:
            with suppress_stdout_stderr():
                try:
                    bs.logout()
                except Exception:
                    pass


def getStockData(code,
                 fields="date,code,open,high,low,close,volume,amount,adjustflag", 
                 start_date=None, end_date=None, 
                 offset=100,
                 freq='d', adjustflag='2'):
    
    # 长周期K线数据如日线、周线、月线用Baostock接口，有换手率，PE等数据
    # 日k线；d=日k线、w=周、m=月、5=5分钟、15=15分钟、30=30分钟、60=60分钟k线数据，不区分大小写；
    # 指数没有分钟线数据；周线每周最后一个交易日才可以获取，月线每月最后一个交易日才可以获取
    if freq=='d' or freq=='w' or freq=='m':
        if getattr(config, 'ENABLE_BAOSTOCK_HISTORY_DATA', False):
            df = _baostock_daily_bars(code, fields, start_date, end_date, freq, adjustflag)
            if df is not None and not df.empty:
                return df
            # baostock 不可用/登录失败/无数据时降级到 mootdx，保证主流程不阻塞
        return _mootdx_daily_bars(code, freq, offset, adjustflag)
    # 其它数据用mootdx接口,默认取100根K线数据，，没有换手率，PE等数据
    # frequency -> K线种类 0 => 5分钟K线 => 5m 1 => 15分钟K线 => 15m 2 => 30分钟K线 => 30m 3 => 小时K线 => 1h 
    # 4 => 日K线 (小数点x100) => days 5 => 周K线 => week 6 => 月K线 => mon 
    # 7 => 1分钟K线(好像一样) => 1m 8 => 1分钟K线(好像一样) => 1m 
    # 9 => 日K线 => day 10 => 季K线 => 3mon 11 => 年K线 => year
    elif freq>=0 and freq<=11:
        if code.startswith(("sh.", "sz.")):
            code = code.split('.')[1]
        client = Quotes.factory('std')  # 使用标准版通达信数据
        df = client.bars(symbol=code, frequency=freq, offset=offset, adjust=adjustflag) 
        return df
    else:
        return None


def getIndexData(code, offset=100, freq=9, adjustflag='qfq'):
    """指数走通达信指数接口，显式保留交易所，避免误取同号股票。"""
    normalized = str(code).upper()
    pure = normalized.split('.')[0]
    if normalized.startswith(('SH.', 'SZ.')):
        market, pure = normalized.split('.', 1)
        normalized = pure + '.' + market
    if pure in ('000001', '999999') and normalized.endswith('.SH'):
        market, pure = 1, '000001'
    elif pure.startswith('399'):
        market = 0
    else:
        raise ValueError("指数代码缺少明确交易所")
    client = Quotes.factory('std')
    return client.index(symbol=pure, market=market, frequency=freq, start=0, offset=offset)


def IsMarketGoingUp():
    # 指数代码
    indices = {
        'sh.000001': '上证指数',   # 上证指数
        'sz.399001': '深证成指',   # 深证成指
        'sz.399005': '中小板指'    # 中小板指
    }

    # 遍历每个指数
    for code, name in indices.items():
        # 获取30天K线数据
        fields = "date,code,open,high,low,close"
        start_date = backInDays(30)
        end_date = datetime.datetime.now().strftime("%Y-%m-%d")  # 当前日期
        df = getStockData(
            code,
            fields=fields,
            start_date=start_date,
            end_date=end_date,
            offset=30,
            freq='d',
            adjustflag='qfq'
        )

        # 计算MA5
        if df is not None and len(df) >= 5 and 'close' in df.columns:
            df['close'] = df['close'].astype(float)
            df['MA5'] = df['close'].rolling(window=5).mean()

            # 检查MA5是否呈上升趋势
            if df['MA5'].iloc[-1] > df['MA5'].iloc[-2] and df['MA5'].iloc[-2] > df['MA5'].iloc[-3]:
                print(f"{name} 的MA5呈现上升趋势。")
                return True

    # 如果没有任何一个指数的MA5呈上升趋势
    print("所有检查的指数的MA5都没有呈现上升趋势。")
    return False

def calmacd(df):
    df2 = df
    if len(df2) > 33:
        dif, dea, hist = MACD(df2['close'].astype(float).values, fastperiod=12, slowperiod=26, signalperiod=9)
        df3 = pd.DataFrame({'dif': dif[33:], 'dea': dea[33:], 'hist': hist[33:]}, index=df2['date'][33:], columns=['dif', 'dea', 'hist'])
        return df3


def WX_send(msg):
    token = os.environ.get("PUSHPLUS_TOKEN", "")
    if not token:
        return  # 未配置则不发送
    title = "Stockquant"
    # 在pushplus推送加微信公众号-功能-个人中心-渠道配置-新增-webhook编码为“stockquant”， 请求地址为企微机器人的webhook地址 
    # webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxxxxxxxxxxxxxxx"

    url = "http://www.pushplus.plus/send"
    headers = {"Content-Type": "application/json"}
    data = {
        "token": token,
        "title": title,
        "content": msg,
        "channel": "webhook",
        "webhook": "stockquant"
    }
    response = requests.post(url, headers=headers, data=json.dumps(data))
    if response.status_code == 200:
        return response.json()
    else:
        return None


# def sendTradeMsg(msg):
#     try:
#         DingTalk.markdown("python交易提醒："+msg)
#     except Exception as e:
#         print(e)
        
#     try:
#         WX_send("Stockquant："+msg)
#     except Exception as e:
#         print(e)


if __name__ == '__main__':

    IsMarketGoingUp()
