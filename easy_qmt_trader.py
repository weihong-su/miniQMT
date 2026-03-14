from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount
from xtquant import xtconstant
import time
import pandas as pd
import random
import math
import json
import math
import config
from logger import get_logger

logger = get_logger("easy_qmt_trader")
def conv_time(ct):
    '''
    conv_time(1476374400000) --> '20161014000000.000'
    '''
    local_time = time.localtime(ct / 1000)
    data_head = time.strftime('%Y%m%d%H%M%S', local_time)
    data_secs = (ct - int(ct)) * 1000
    time_stamp = '%s.%03d' % (data_head, data_secs)
    return time_stamp
class MyXtQuantTraderCallback(XtQuantTraderCallback):
    def __init__(self, order_id_map):
        super().__init__()
        self.order_id_map = order_id_map
        self.trade_callbacks = []       # 成交回报外部回调列表
        self.disconnect_callbacks = []  # 断连事件外部回调列表（Fail-Safe 用）

    def on_disconnected(self):
        """
        连接断开推送 — QMT 进程崩溃或网络中断时由 xtquant 主动回调。
        立即通知所有已注册的外部断连回调，使 PositionManager 能第一时间
        将 qmt_connected 设为 False，无需等待监控循环连续超时 3 次（约 15 秒）。
        """
        logger.error("⚠ QMT连接断开，正在通知外部模块...")
        for cb in self.disconnect_callbacks:
            try:
                cb()
            except Exception as e:
                logger.error(f"on_disconnected 外部回调异常: {e}")
    def on_stock_order(self, order):
        """
        委托回报推送
        :param order: XtOrder对象
        :return:
        """
        logger.info(f"委托回报: 股票代码={order.stock_code}, 委托状态={order.order_status}, 系统订单号={order.order_sysid}")
    def on_stock_asset(self, asset):
        """
        资金变动推送
        :param asset: XtAsset对象
        :return:
        """
        logger.info(f"资金变动: 账户={asset.account_id}, 可用资金={asset.cash:.2f}, 总资产={asset.total_asset:.2f}")
    def on_stock_trade(self, trade):
        """
        成交变动推送
        :param trade: XtTrade对象
        :return:
        """
        logger.info(f"成交回报: 账户={trade.account_id}, 股票代码={trade.stock_code}, 订单号={trade.order_id}")
        # 通知所有注册的外部回调（如 position_manager 的委托跟踪清理）
        for cb in self.trade_callbacks:
            try:
                cb(trade)
            except Exception as e:
                logger.error(f"on_stock_trade 外部回调异常: {e}")
    def on_stock_position(self, position):
        """
        持仓变动推送
        :param position: XtPosition对象
        :return:
        """
        logger.info(f"持仓变动: 股票代码={position.stock_code}, 持仓数量={position.volume}")
    def on_order_error(self, order_error):
        """
        委托失败推送
        :param order_error:XtOrderError 对象
        :return:
        """
        logger.error(f"委托失败: 订单号={order_error.order_id}, 错误码={order_error.error_id}, 错误信息={order_error.error_msg}")
    def on_cancel_error(self, cancel_error):
        """
        撤单失败推送
        :param cancel_error: XtCancelError 对象
        :return:
        """
        logger.error(f"撤单失败: 订单号={cancel_error.order_id}, 错误码={cancel_error.error_id}, 错误信息={cancel_error.error_msg}")
    def on_order_stock_async_response(self, response):
        """
        异步下单回报推送
        :param response: XtOrderResponse 对象
        :return:
        """
        logger.info(f"异步下单回报: 账户={response.account_id}, 订单号={response.order_id}, 请求序号={response.seq}")
        self.order_id_map[response.seq] = response.order_id

class easy_qmt_trader:
    def __init__(self,path= r'D:/国金QMT交易端模拟/userdata_mini',
                  session_id = 123456,account='55009640',account_type='STOCK',
                  is_slippage=True,slippage=0.01) -> None:
        '''
        简化版的qmt_trder方便大家做策略的开发类的继承
        '''
        self.xt_trader=''
        self.acc=''
        self.path=path
        self.session_id=int(self.random_session_id())
        self.account=account
        self.account_type=account_type
        if is_slippage==True:
            self.slippage=slippage
        else:
            self.slippage=0
        self.order_id_map = {}  # 新增：用于存储下单请求序号和qmt订单编号的映射关系
        self.xtdata = None  # 初始化xtdata属性
        self.xtdata_connected = False  # 初始化连接状态
        self._callback = None  # 保存callback对象，供外部注册trade_callbacks
        logger.info('操作提示: 请登录QMT,选择行情加交易选项,选择极简模式')

    def register_trade_callback(self, cb):
        """注册成交回报外部回调，cb(trade) 在每次成交时被调用"""
        if self._callback is not None:
            self._callback.trade_callbacks.append(cb)
        else:
            logger.warning("register_trade_callback: callback尚未初始化，请在connect()后调用")

    def register_disconnect_callback(self, cb):
        """
        注册断连事件外部回调，cb() 在 QMT 连接断开时被立即调用。
        主要供 PositionManager 注册以即时更新 qmt_connected 标志。
        """
        if self._callback is not None:
            self._callback.disconnect_callbacks.append(cb)
        else:
            logger.warning("register_disconnect_callback: callback尚未初始化，请在connect()后调用")
        
    def random_session_id(self):
        '''
        随机id
        '''
        session_id=''
        for i in range(0,9):
            session_id+=str(random.randint(1,9))
        return session_id
    def select_slippage(self,stock='600031',price=15.01,trader_type='buy'):
        '''
        选择滑点
        安价格来滑点，比如0.01就是一块
        etf3位数,股票可转债2位数
        '''
        stock=self.adjust_stock(stock=stock)
        data_type=self.select_data_type(stock=stock)
        if data_type=='fund' or data_type=='bond':
            slippage=self.slippage/10
            if trader_type=='buy' or trader_type==23:
                price=price+slippage
            else:
                price=price-slippage
        else:
            slippage=self.slippage
            if trader_type=='buy' or trader_type==23:
                price=price+slippage
            else:
                price=price-slippage
        return price
    def check_is_trader_date_1(self,trader_time=4,start_date=9,end_date=14,start_mi=0,jhjj='否'):
        '''
        检测是不是交易时间
        '''
        if jhjj=='是':
            jhjj_time=15
        else:
            jhjj_time=30
        loc=time.localtime()
        tm_hour=loc.tm_hour
        tm_min=loc.tm_min
        wo=loc.tm_wday
        if wo<=trader_time:
            if tm_hour>=start_date and tm_hour<=end_date:
                if tm_hour==9 and tm_min<jhjj_time:
                    return False
                elif tm_min>=start_mi:
                    return True
                else:
                    return False
            else:
                return False    
        else:
            logger.debug('今天是周末，非交易时间')
            return False
    def select_data_type(self,stock='600031'):
        '''
        选择数据类型
        '''
        if stock[:3] in ['110','113','123','127','128','111','118'] or stock[:2] in ['11','12']:
            return 'bond'
        elif stock[:3] in ['510','511','512','513','514','515','516','517','518','588','159','501','164'] or stock[:2] in ['16']:
            return 'fund'
        else:
            return 'stock'
    def adjust_stock(self,stock='600031.SH'):
        '''
        调整代码
        '''
        if stock[-2:]=='SH' or stock[-2:]=='SZ' or stock[-2:]=='sh' or stock[-2:]=='sz':
            stock=stock.upper()
        else:
            if stock[:3] in ['600','601','603','688','510','511',
                             '512','513','515','113','110','118','501'] or stock[:2] in ['11']:
                stock=stock+'.SH'
            else:
                stock=stock+'.SZ'
        return stock
    def check_stock_is_av_buy(self,stock='128036',price='156.700',amount=10,hold_limit=100000):
        '''
        检查是否可以买入
        '''
        hold_stock=self.position()
        try:
            del hold_stock['Unnamed: 0']
        except:
            pass
        account=self.balance()
        try:
            del account['Unnamed: 0']
        except:
            pass
        #买入是价值
        value=price*amount
        cash=account['可用金额'].tolist()[-1]
        frozen_cash=account['冻结金额'].tolist()[-1]
        market_value=account['持仓市值'].tolist()[-1]
        total_asset=account['总资产'].tolist()[-1]
        if cash>=value:
            logger.info(f'允许买入 股票={stock}, 可用现金={cash:.2f}大于买入金额={value:.2f}, 价格={price:.2f}, 数量={amount}')
            return True
        else:
            logger.warning(f'不允许买入 股票={stock}, 可用现金={cash:.2f}小于买入金额={value:.2f}, 价格={price:.2f}, 数量={amount}')
            return False
    def check_stock_is_av_sell(self,stock='128036',amount=10):
        '''
        检查是否可以卖出
        '''
        #stock=self.adjust_stock(stock=stock)
        hold_data=self.position()
        try:
            del hold_data['Unnamed: 0']
        except:
            pass
        account=self.balance()
        try:
            del account['Unnamed: 0']
        except:
            pass
        #买入是价值
        cash=account['可用金额'].tolist()[-1]
        frozen_cash=account['冻结金额'].tolist()[-1]
        market_value=account['持仓市值'].tolist()[-1]
        total_asset=account['总资产'].tolist()[-1]
        stock_list=hold_data['证券代码'].tolist()
        if stock in stock_list:
            hold_num=hold_data[hold_data['证券代码']==stock]['可用余额'].tolist()[-1]
            if hold_num>=amount:
                logger.info(f'允许卖出 股票={stock}, 持股={hold_num}, 卖出={amount}')
                return True
            else:
                logger.warning(f'不允许卖出,持股不足 股票={stock}, 持股={hold_num}, 卖出={amount}')
                return False
        else:
            logger.warning(f'不允许卖出,没有持股 股票={stock}, 持股=0, 卖出={amount}')
            return False
    def connect(self):
        '''
        连接
        path qmt userdata_min是路径
        session_id 账户的标志,随便
        account账户,
        account_type账户内类型
        '''
        logger.info('正在连接QMT交易接口...')

        # 🔧 Fail-Safe 修复: 先清理旧连接，防止重复调用时资源泄漏
        old_trader = getattr(self, 'xt_trader', None)
        if old_trader and old_trader != '':
            try:
                old_trader.stop()
                logger.info('已停止旧 XtQuantTrader 实例')
            except Exception as e:
                logger.warning(f'停止旧 XtQuantTrader 时出错 (忽略): {e}')
            self.xt_trader = ''

        # path为mini qmt客户端安装目录下userdata_mini路径
        path = self.path
        # session_id为会话编号，策略使用方对于不同的Python策略需要使用不同的会话编号
        session_id = self.session_id
        xt_trader = XtQuantTrader(path, session_id)
        # 创建资金账号为1000000365的证券账号对象
        account=self.account
        account_type=self.account_type
        acc = StockAccount(account_id=account,account_type=account_type)
        # 创建交易回调类对象，并声明接收回调
        callback = MyXtQuantTraderCallback(self.order_id_map)
        xt_trader.register_callback(callback)
        # 保存callback对象，供外部注册trade_callbacks使用
        self._callback = callback
        # 启动交易线程
        xt_trader.start()
        # 建立交易连接，返回0表示连接成功
        connect_result = xt_trader.connect()
        if connect_result==0:
            # 对交易回调进行订阅，订阅后可以收到交易主推，返回0表示订阅成功
            subscribe_result = xt_trader.subscribe(acc)
            logger.info(f'QMT交易接口连接成功, 订阅结果={subscribe_result}')
            self.xt_trader=xt_trader
            self.acc=acc
            return xt_trader,acc
        else:
            logger.error(f'QMT连接失败, 连接结果={connect_result}')
            # 🔧 修复：连接失败时返回None，方便调用方检测
            return None
    def order_stock(self,stock_code='600031.SH', order_type=xtconstant.STOCK_BUY,
                    order_volume=100,price_type=xtconstant.FIX_PRICE,price=20,strategy_name='',order_remark=''):
            '''
            下单，统一接口
            :param account: 证券账号
                :param stock_code: 证券代码, 例如"600000.SH"
                :param order_type: 委托类型, 23:买, 24:卖
                :param order_volume: 委托数量, 股票以'股'为单位, 债券以'张'为单位
                :param price_type: 报价类型, 详见帮助手册
                :param price: 报价价格, 如果price_type为指定价, 那price为指定的价格, 否则填0
                :param strategy_name: 策略名称
                :param order_remark: 委托备注
                :return: 返回下单请求序号, 成功委托后的下单请求序号为大于0的正整数, 如果为-1表示委托失败
            '''

            # 对交易回调进行订阅，订阅后可以收到交易主推，返回0表示订阅成功
            subscribe_result = self.xt_trader.subscribe(self.acc)
            logger.debug(f'查询资产回调结果={self.xt_trader.query_stock_asset_async(account=self.acc,callback=subscribe_result)}')
            #print(subscribe_result)
            stock_code = self.adjust_stock(stock=stock_code)
            price=self.select_slippage(stock=stock_code,price=price,trader_type=order_type)
            # 使用指定价下单，接口返回订单编号，后续可以用于撤单操作以及查询委托状态
            fix_result_order_id = self.xt_trader.order_stock(account=self.acc,stock_code=stock_code, order_type=order_type,
                                                            order_volume=order_volume, price_type=price_type,
                                                            price=price, strategy_name=strategy_name, order_remark=order_remark)
            logger.info(f'下单成功 交易类型={order_type}, 代码={stock_code}, 价格={price:.2f}, 数量={order_volume}, 订单编号={fix_result_order_id}')
            return fix_result_order_id
    def buy(self,security='600031.SH', order_type=xtconstant.STOCK_BUY,
                    amount=100,price_type=xtconstant.FIX_PRICE,price=20,strategy_name='',order_remark=''):
        '''
        单独独立股票买入函数
        支持配置开关控制使用同步或异步接口
        '''
        # 对交易回调进行订阅，订阅后可以收到交易主推，返回0表示订阅成功
        subscribe_result = self.xt_trader.subscribe(self.acc)
        logger.debug(f'查询资产回调结果={self.xt_trader.query_stock_asset_async(account=self.acc,callback=subscribe_result)}')
        #print(subscribe_result)
        stock_code =self.adjust_stock(stock=security)
        price=self.select_slippage(stock=security,price=price,trader_type='buy')
        order_volume=amount
        # 根据配置选择同步或异步接口
        if order_volume>0:
            if config.USE_SYNC_ORDER_API:
                # 使用同步接口，直接返回order_id
                fix_result_order_id = self.xt_trader.order_stock(account=self.acc,stock_code=stock_code, order_type=order_type,
                                                                    order_volume=order_volume, price_type=price_type,
                                                                    price=price, strategy_name=strategy_name, order_remark=order_remark)
                logger.info(f'买入成功(同步) 交易类型={order_type}, 代码={stock_code}, 价格={price:.2f}, 数量={order_volume}, 订单编号={fix_result_order_id}')
                return fix_result_order_id
            else:
                # 使用异步接口，返回seq号（需要通过回调映射到order_id）
                fix_result_order_id = self.xt_trader.order_stock_async(account=self.acc,stock_code=stock_code, order_type=order_type,
                                                                    order_volume=order_volume, price_type=price_type,
                                                                    price=price, strategy_name=strategy_name, order_remark=order_remark)
                logger.info(f'买入请求提交(异步) 交易类型={order_type}, 代码={stock_code}, 价格={price:.2f}, 数量={order_volume}, 请求序号={fix_result_order_id}')
                return fix_result_order_id  # 返回API的seq号，回调会建立seq->order_id映射
        else:
            logger.error(f'买入参数错误 标的={stock_code}, 价格={price:.2f}, 委托数量={order_volume}小于0')
            return None
    def sell(self,security='600031.SH', order_type=xtconstant.STOCK_SELL,
                    amount=100,price_type=xtconstant.FIX_PRICE,price=20,strategy_name='',order_remark=''):
        '''
        单独独立股票卖出函数
        支持配置开关控制使用同步或异步接口
        '''
        # 对交易回调进行订阅，订阅后可以收到交易主推，返回0表示订阅成功
        subscribe_result = self.xt_trader.subscribe(self.acc)
        logger.debug(f'查询资产回调结果={self.xt_trader.query_stock_asset_async(account=self.acc,callback=subscribe_result)}')
        #print(subscribe_result)
        stock_code =self.adjust_stock(stock=security)
        price=self.select_slippage(stock=security,price=price,trader_type='sell')
        order_volume=amount
        # 根据配置选择同步或异步接口
        if order_volume>0:
            if config.USE_SYNC_ORDER_API:
                # 使用同步接口，直接返回order_id
                fix_result_order_id = self.xt_trader.order_stock(account=self.acc,stock_code=stock_code, order_type=order_type,
                                                                    order_volume=order_volume, price_type=price_type,
                                                                    price=price, strategy_name=strategy_name, order_remark=order_remark)
                logger.info(f'卖出成功(同步) 交易类型={order_type}, 代码={stock_code}, 价格={price:.2f}, 数量={order_volume}, 订单编号={fix_result_order_id}')
                return fix_result_order_id
            else:
                # 使用异步接口，返回seq号（需要通过回调映射到order_id）
                fix_result_order_id = self.xt_trader.order_stock_async(account=self.acc,stock_code=stock_code, order_type=order_type,
                                                                    order_volume=order_volume, price_type=price_type,
                                                                    price=price, strategy_name=strategy_name, order_remark=order_remark)
                logger.info(f'卖出请求提交(异步) 交易类型={order_type}, 代码={stock_code}, 价格={price:.2f}, 数量={order_volume}, 请求序号={fix_result_order_id}')
                return fix_result_order_id  # 返回API的seq号，回调会建立seq->order_id映射
        else:
            logger.error(f'卖出参数错误 标的={stock_code}, 价格={price:.2f}, 委托数量={order_volume}小于0')
            return None

    def order_stock_async(self,stock_code='600031.SH', order_type=xtconstant.STOCK_BUY,
                    order_volume=100,price_type=xtconstant.FIX_PRICE,price=20,strategy_name='',order_remark=''):
        '''
         释义 
        - 对股票进行异步下单操作，异步下单接口如果正常返回了下单请求序号seq，会收到on_order_stock_async_response的委托反馈
        * 参数
        - account - StockAccount 资金账号
        - stock_code - str 证券代码， 如'600000.SH'
        - order_type - int 委托类型
        - order_volume - int 委托数量，股票以'股'为单位，债券以'张'为单位
        - price_type - int 报价类型
        - price - float 委托价格
        - strategy_name - str 策略名称
        - order_remark - str 委托备注
        '''
        # 对交易回调进行订阅，订阅后可以收到交易主推，返回0表示订阅成功
        subscribe_result = self.xt_trader.subscribe(self.acc)
        logger.debug(f'查询资产回调结果={self.xt_trader.query_stock_asset_async(account=self.acc,callback=subscribe_result)}')
        #print(subscribe_result)
        stock_code = self.adjust_stock(stock=stock_code)
        price=self.select_slippage(stock=stock_code,price=price,trader_type=order_type)
        # 使用指定价下单，接口返回订单编号，后续可以用于撤单操作以及查询委托状态
        fix_result_order_id = self.xt_trader.order_stock_async(account=self.acc,stock_code=stock_code, order_type=order_type,
                                                            order_volume=order_volume, price_type=price_type,
                                                            price=price, strategy_name=strategy_name, order_remark=order_remark)
        logger.info(f'异步下单请求提交 交易类型={order_type}, 代码={stock_code}, 价格={price:.2f}, 数量={order_volume}, 请求序号={fix_result_order_id}')
        return fix_result_order_id
    def cancel_order_stock(self,order_id=12):
        '''
        :param account: 证券账号
            :param order_id: 委托编号, 报单时返回的编号
            :return: 返回撤单成功或者失败, 0:成功,  -1:委托已完成撤单失败, -2:未找到对应委托编号撤单失败, -3:账号未登陆撤单失败
        '''
        # 使用订单编号撤单
        cancel_order_result = self.xt_trader.cancel_order_stock(account=self.acc,order_id=order_id)
        if cancel_order_result==0:
            logger.info(f'撤单成功 订单号={order_id}')
        elif cancel_order_result==-1:
            logger.error(f'撤单失败-委托已完成 订单号={order_id}')
        elif cancel_order_result==-2:
            logger.error(f'撤单失败-未找到对应委托编号 订单号={order_id}')
        elif cancel_order_result==-3:
            logger.error(f'撤单失败-账号未登陆 订单号={order_id}')
        else:
            logger.warning(f'撤单结果未知 订单号={order_id}, 结果码={cancel_order_result}')
        return cancel_order_result
    def cancel_order_stock_async(self,order_id=12):
        '''
        * 释义 
        - 根据订单编号对委托进行异步撤单操作
        * 参数
        - account - StockAccount  资金账号 
        - order_id - int 下单接口返回的订单编号
        * 返回 
        - 返回撤单请求序号, 成功委托后的撤单请求序号为大于0的正整数, 如果为-1表示委托失败
        * 备注
        - 如果失败，则通过撤单失败主推接口返回撤单失败信息
        '''
        # 使用订单编号撤单
        cancel_order_result = self.xt_trader.cancel_order_stock_async(account=self.acc,order_id=order_id)
        if cancel_order_result==0:
            logger.info(f'异步撤单请求提交成功 订单号={order_id}')
        elif cancel_order_result==-1:
            logger.error(f'异步撤单失败-委托已完成 订单号={order_id}')
        elif cancel_order_result==-2:
            logger.error(f'异步撤单失败-未找到对应委托编号 订单号={order_id}')
        elif cancel_order_result==-3:
            logger.error(f'异步撤单失败-账号未登陆 订单号={order_id}')
        else:
            logger.warning(f'异步撤单结果未知 订单号={order_id}, 结果码={cancel_order_result}')
        return cancel_order_result
    def query_stock_asset(self):
        '''
        :param account: 证券账号
            :return: 返回当前证券账号的资产数据
        '''
        # 查询证券资产
        
        asset = self.xt_trader.query_stock_asset(account=self.acc)
        data_dict={}
        if asset:
            data_dict['账号类型']=asset.account_type
            data_dict['资金账户']=asset.account_id
            data_dict['可用金额']=asset.cash
            data_dict['冻结金额']=asset.frozen_cash
            data_dict['持仓市值']=asset.market_value
            data_dict['总资产']=asset.total_asset
            return data_dict
        else:
            logger.warning('查询资产失败-asset对象为空')
            data_dict['账号类型']=[None]
            data_dict['资金账户']=[None]
            data_dict['可用金额']=[None]
            data_dict['冻结金额']=[None]
            data_dict['持仓市值']=[None]
            data_dict['总资产']=[None]
            return  data_dict
    def balance(self):
        '''
        对接同花顺
        '''
        try:
            asset = self.xt_trader.query_stock_asset(account=self.acc)
            df=pd.DataFrame()
            if asset:
                df['账号类型']=[asset.account_type]
                df['资金账户']=[asset.account_id]
                df['可用金额']=[asset.cash]
                df['冻结金额']=[asset.frozen_cash]
                df['持仓市值']=[asset.market_value]
                df['总资产']=[asset.total_asset]
                return df
            # ===== 新增：asset为空时返回空DataFrame而非隐式None =====
            return df
        except Exception as e:
            logger.error(f'获取账户资产失败: {str(e)}，返回空DataFrame')
            df=pd.DataFrame()
            return df
    def query_stock_orders(self):
        '''
        当日委托
         :param account: 证券账号
        :param cancelable_only: 仅查询可撤委托
        :return: 返回当日所有委托的委托对象组成的list
        '''
        orders = self.xt_trader.query_stock_orders(self.acc)
        logger.debug(f"查询当日委托数量: {len(orders)}")
        data=pd.DataFrame()
        if len(orders) != 0:
            for i in range(len(orders)):
                df=pd.DataFrame()
                df['账号类型']=[orders[i].account_type]
                df['资金账号']=[orders[i].account_id]
                df['证券代码']=[orders[i].stock_code]
                df['证券代码']=df['证券代码'].apply(lambda x:str(x)[:6])
                df['订单编号']=[orders[i].order_id]
                df['柜台合同编号']=[orders[i].order_sysid]
                df['报单时间']=[orders[i].order_time]
                df['委托类型']=[orders[i].order_type]
                df['委托数量']=[orders[i].order_volume]
                df['报价类型']=[orders[i].price_type]
                df['委托价格']=[orders[i].price]
                df['成交数量']=[orders[i].traded_volume]
                df['成交均价']=[orders[i].traded_price]
                df['委托状态']=[orders[i].order_status]
                df['委托状态描述']=[orders[i].status_msg]
                df['策略名称']=[orders[i].strategy_name]
                df['委托备注']=[orders[i].order_remark]
                data=pd.concat([data,df],ignore_index=True)
            data['报单时间']=pd.to_datetime(data['报单时间'],unit='s')
            return data
        else:
            logger.debug('当前没有委托单')
            return data
    def today_entrusts(self):
        '''
        对接同花顺
        今天委托
        '''
        def select_data(x):
            if x==48:
                return '未报'
            elif x==49:
                return '待报'
            elif x==50:
                return '已报'
            elif x==51:
                return '已报待撤'
            elif x==52:
                return '部分待撤'
            elif x==53:
                return '部撤'
            elif x==54:
                return '已撤'
            elif x==55:
                return '部成'
            elif x==56:
                return '已成'
            elif x==57:
                return '废单'
            else:
                return '废单'
        orders = self.xt_trader.query_stock_orders(self.acc)
        logger.debug(f"查询今日委托数量: {len(orders)}")
        data=pd.DataFrame()
        if len(orders) != 0:
            for i in range(len(orders)):
                df=pd.DataFrame()
                df['账号类型']=[orders[i].account_type]
                df['资金账号']=[orders[i].account_id]
                df['证券代码']=[orders[i].stock_code]
                df['证券代码']=df['证券代码'].apply(lambda x:str(x)[:6])
                df['订单编号']=[orders[i].order_id]
                df['柜台合同编号']=[orders[i].order_sysid]
                df['报单时间']=[orders[i].order_time]
                df['委托类型']=[orders[i].order_type]
                df['委托数量']=[orders[i].order_volume]
                df['报价类型']=[orders[i].price_type]
                df['委托价格']=[orders[i].price]
                df['成交数量']=[orders[i].traded_volume]
                df['成交均价']=[orders[i].traded_price]
                df['委托状态']=[orders[i].order_status]
                df['委托状态描述']=[orders[i].status_msg]
                df['策略名称']=[orders[i].strategy_name]
                df['委托备注']=[orders[i].order_remark]
                data=pd.concat([data,df],ignore_index=True)
            data['报单时间']=df['报单时间'].apply(conv_time)
            data['委托状态翻译']=data['委托状态'].apply(select_data)
            data['未成交数量']=data['委托数量']-data['成交数量']
            data['未成交价值']=data['未成交数量']*data['委托价格']
            return data
        else:
            logger.debug('今日没有委托单')
            return data
    def query_stock_trades(self):
        '''
        当日成交
        '''
        trades = self.xt_trader.query_stock_trades(self.acc)
        logger.debug(f"查询当日成交数量: {len(trades)}")
        data=pd.DataFrame()
        if len(trades) != 0:
            for i in range(len(trades)):
                df=pd.DataFrame()
                df['账号类型']=[trades[i].account_type]
                df['资金账号']=[trades[i].account_id]
                df['证券代码']=[trades[i].stock_code]
                df['证券代码']=df['证券代码'].apply(lambda x:str(x)[:6])
                df['委托类型']=[trades[i].order_type]
                df['成交编号']=[trades[i].traded_id]
                df['成交时间']=[trades[i].traded_time]
                df['成交均价']=[trades[i].traded_price]
                df['成交数量']=[trades[i].traded_volume]
                df['成交金额']=[trades[i].traded_amount]
                df['订单编号']=[trades[i].order_id]
                df['柜台合同编号']=[trades[i].order_sysid]
                df['策略名称']=[trades[i].strategy_name]
                df['委托备注']=[trades[i].order_remark]
                data=pd.concat([data,df],ignore_index=True)
            data['成交时间']=pd.to_datetime(data['成交时间'],unit='s')
            return data
        else:
            logger.debug('今日没有成交记录')
            return data
    def get_active_orders_by_stock(self, stock_code):
        """
        根据股票代码查询活跃委托单

        参数:
            stock_code (str): 股票代码,如 '600031.SH' 或 '600031'

        返回:
            list: 活跃委托单对象列表,每个对象包含委托详细信息
                  如果没有活跃委托则返回空列表

        活跃委托状态码:
            48: 未报
            49: 待报
            50: 已报
            51: 已报待撤
            52: 部分待撤
            55: 部成
        """
        # 调整股票代码格式(如果需要)
        stock_code = self.adjust_stock(stock=stock_code)

        # 查询所有委托单
        orders = self.xt_trader.query_stock_orders(self.acc, cancelable_only=False)

        # 活跃委托状态码
        active_status = [48, 49, 50, 51, 52, 55]

        # 筛选指定股票的活跃委托
        active_orders = []
        for order in orders:
            # 匹配股票代码(考虑可能的格式差异)
            order_stock = str(order.stock_code)
            if order_stock == stock_code or order_stock[:6] == stock_code[:6]:
                # 检查是否为活跃状态
                if order.order_status in active_status:
                    active_orders.append(order)

        return active_orders

    def get_active_order_info_by_stock(self, stock_code):
        """
        根据股票代码查询活跃委托单的详细信息(字典格式)

        参数:
            stock_code (str): 股票代码

        返回:
            list[dict]: 活跃委托单信息字典列表,每个字典包含:
                - order_id: 订单编号
                - stock_code: 证券代码
                - order_type: 委托类型(23=买入, 24=卖出)
                - order_status: 委托状态
                - order_volume: 委托数量
                - traded_volume: 成交数量
                - price: 委托价格
                - order_time: 报单时间
                - strategy_name: 策略名称
                - order_remark: 委托备注
        """
        active_orders = self.get_active_orders_by_stock(stock_code)

        # 转换为字典格式
        order_info_list = []
        for order in active_orders:
            order_info = {
                'order_id': order.order_id,
                'stock_code': order.stock_code,
                'order_type': order.order_type,
                'order_status': order.order_status,
                'status_msg': order.status_msg,
                'order_volume': order.order_volume,
                'traded_volume': order.traded_volume,
                'price': order.price,
                'order_time': order.order_time,
                'strategy_name': order.strategy_name,
                'order_remark': order.order_remark
            }
            order_info_list.append(order_info)

        return order_info_list

    def today_trades(self):
        '''
        对接同花顺
        今日成交
        '''
        trades = self.xt_trader.query_stock_trades(self.acc)
        logger.debug(f"查询今日成交数量: {len(trades)}")
        data=pd.DataFrame()
        if len(trades) != 0:
            for i in range(len(trades)):
                df=pd.DataFrame()
                df['账号类型']=[trades[i].account_type]
                df['资金账号']=[trades[i].account_id]
                df['证券代码']=[trades[i].stock_code]
                df['证券代码']=df['证券代码'].apply(lambda x:str(x)[:6])
                df['委托类型']=[trades[i].order_type]
                df['成交编号']=[trades[i].traded_id]
                df['成交时间']=[trades[i].traded_time]
                df['成交均价']=[trades[i].traded_price]
                df['成交数量']=[trades[i].traded_volume]
                df['成交金额']=[trades[i].traded_amount]
                df['订单编号']=[trades[i].order_id]
                df['柜台合同编号']=[trades[i].order_sysid]
                df['策略名称']=[trades[i].strategy_name]
                df['委托备注']=[trades[i].order_remark]
                data=pd.concat([data,df],ignore_index=True)
            def select_data(x):
                if x==xtconstant.STOCK_BUY:
                    return '证券买入'
                elif x==xtconstant.STOCK_SELL:
                    return '证券卖出'
                else:
                    return '无'
            df['操作']=df['委托类型'].apply(select_data)
            data['成交时间']=pd.to_datetime(data['成交时间'],unit='s')
            return data
        else:
            logger.debug('今日没有成交记录')
            return data
    def query_stock_positions(self):
        '''
        查询账户所有的持仓
        '''
        positions = self.xt_trader.query_stock_positions(self.acc)
        logger.debug(f"query_stock_positions()-持仓数量: {len(positions)}")
        data=pd.DataFrame()
        if len(positions) != 0:
            for i in range(len(positions)):
                df=pd.DataFrame()
                df['账号类型']=[positions[i].account_type]
                df['资金账号']=[positions[i].account_id]
                df['证券代码']=[positions[i].stock_code]
                df['证券代码']=df['证券代码'].apply(lambda x:str(x)[:6])
                df['持仓数量']=[positions[i].volume]
                df['可用数量']=[positions[i].can_use_volume]
                df['平均建仓成本']=[positions[i].open_price]
                df['市值']=[positions[i].market_value]
                data=pd.concat([data,df],ignore_index=True)
            return data
        else:
            logger.debug('当前没有持仓')
            df=pd.DataFrame()
            df['账号类型']=[None]
            df['资金账号']=[None]
            df['证券代码']=[None]
            df['持仓数量']=[None]
            df['可用数量']=[None]
            df['平均建仓成本']=[None]
            df['市值']=[None]
            return df
        
    def position(self):
        '''对接同花顺持股'''
        try:
            # 🔧 修复：检查xt_trader是否已正确初始化
            if not hasattr(self, 'xt_trader') or self.xt_trader is None or isinstance(self.xt_trader, str):
                logger_error_msg = f"QMT未连接或连接失败，无法获取持仓。xt_trader类型: {type(self.xt_trader) if hasattr(self, 'xt_trader') else 'undefined'}"
                logger.error(f"获取持仓信息时出错: {logger_error_msg}")

                # 返回预定义空DataFrame
                columns = ['账号类型', '资金账号', '证券代码', '股票余额', '可用余额',
                        '成本价', '市值', '选择', '持股天数', '交易状态', '明细',
                        '证券名称', '冻结数量', '市价', '盈亏', '盈亏比(%)',
                        '当日买入', '当日卖出']
                return pd.DataFrame(columns=columns)

            positions = self.xt_trader.query_stock_positions(self.acc)
            # print("easy_qmt_trader.position-持仓数量:", len(positions))
            
            # 一次性构建数据列表，再创建DataFrame
            if len(positions) > 0:
                data_list = []
                for pos in positions:
                    data_list.append({
                        '账号类型': pos.account_type,
                        '资金账号': pos.account_id,
                        '证券代码': str(pos.stock_code)[:6],
                        '股票余额': pos.volume,
                        '可用余额': pos.can_use_volume,
                        '成本价': pos.open_price,
                        '参考成本价': pos.open_price,
                        '市值': pos.market_value
                    })
                
                # 一次性创建DataFrame
                return pd.DataFrame(data_list)
            else:
                # 预定义列名，创建空DataFrame
                columns = ['账号类型', '资金账号', '证券代码', '股票余额', '可用余额', 
                        '成本价', '市值', '选择', '持股天数', '交易状态', '明细',
                        '证券名称', '冻结数量', '市价', '盈亏', '盈亏比(%)', 
                        '当日买入', '当日卖出']
                return pd.DataFrame(columns=columns)

        except Exception as e:
            logger.error(f"获取持仓信息时出错: {str(e)}")
            columns = ['账号类型', '资金账号', '证券代码', '股票余额', '可用余额', 
                    '成本价', '市值', '选择', '持股天数', '交易状态', '明细',
                    '证券名称', '冻结数量', '市价', '盈亏', '盈亏比(%)', 
                    '当日买入', '当日卖出']
            return pd.DataFrame(columns=columns)
    
    def run_forever(self):
        '''
        阻塞线程，接收交易推送
        '''
        self.xt_trader.run_forever()
    def stop(self):
        self.xt_trader.stop()

    # ============ 新增: xtquant统一管理接口 ============
    def connect_xtdata(self):
        """连接xtdata行情接口"""
        try:
            import xtquant.xtdata as xt
            self.xtdata = xt
            if xt.connect():
                self.xtdata_connected = True
                logger.info("xtdata行情接口连接成功")
                return True
            else:
                logger.error("xtdata行情接口连接失败")
                self.xtdata_connected = False
                return False
        except Exception as e:
            logger.error(f"连接xtdata失败: {e}")
            self.xtdata_connected = False
            return False

    def reconnect_xtdata(self):
        """重新连接xtdata行情接口"""
        try:
            if not hasattr(self, 'xtdata') or not self.xtdata:
                import xtquant.xtdata as xt
                self.xtdata = xt
            if self.xtdata.reconnect():
                self.xtdata_connected = True
                logger.info("xtdata行情接口重连成功")
                return True
            else:
                logger.error("xtdata行情接口重连失败")
                self.xtdata_connected = False
                return False
        except Exception as e:
            logger.error(f"重连xtdata失败: {e}")
            self.xtdata_connected = False
            return False

    def ping_xttrader(self):
        """
        探测 xttrader 交易接口是否仍然连通（不执行重连，仅探测）。

        通过调用同步资产查询接口来确认 QMT 进程是否在线。
        xtdata 行情接口即使 QMT 进程崩溃后也可能因缓存返回数据，
        因此必须额外探测 xttrader 才能准确感知断连。

        Returns:
            bool: True 表示 xttrader 连通，False 表示断连或异常
        """
        try:
            xt_trader = getattr(self, 'xt_trader', None)
            acc = getattr(self, 'acc', None)
            if not xt_trader or xt_trader == '' or not acc or acc == '':
                return False
            # 使用同步资产查询作为探针（轻量、不产生副作用）
            asset = xt_trader.query_stock_asset(account=acc)
            return asset is not None
        except Exception as e:
            logger.warning(f'ping_xttrader 探测失败: {e}')
            return False

    def reconnect_xttrader(self):
        """
        重新连接 xttrader 交易接口（Fail-Safe 自动重连入口）。

        直接复用 connect() 逻辑，connect() 内部已保证先清理旧连接。
        重连成功后外部调用者需要重新注册 trade_callback。

        Returns:
            bool: True 表示重连成功，False 表示失败
        """
        logger.warning('正在重新连接 xttrader 交易接口...')
        try:
            result = self.connect()
            if result is not None:
                logger.info('✅ xttrader 重连成功')
                return True
            else:
                logger.error('❌ xttrader 重连失败（connect() 返回 None）')
                return False
        except Exception as e:
            logger.error(f'❌ xttrader 重连异常: {e}')
            return False

    def verify_xtdata_connection(self):
        """验证xtdata连接状态"""
        try:
            if not hasattr(self, 'xtdata') or not hasattr(self, 'xtdata_connected'):
                return False
            if not self.xtdata or not self.xtdata_connected:
                return False
            test_codes = ['000001.SZ']
            test_data = self.xtdata.get_full_tick(test_codes)
            if test_data:
                logger.debug("xtdata连接验证成功")
                return True
            else:
                logger.warning("xtdata连接验证失败-返回数据为空")
                return False
        except Exception as e:
            logger.error(f"xtdata连接验证异常: {e}")
            return False

    def get_full_tick(self, stock_codes):
        """获取全推行情数据(通过xtdata)"""
        if hasattr(self, 'xtdata') and self.xtdata:
            return self.xtdata.get_full_tick(stock_codes)
        return {}

if __name__=='__main__':
    models=easy_qmt_trader()
    models.connect()
    logger.info(f"测试查询委托: {models.query_stock_orders()}")
    models.buy()
    models1=easy_qmt_trader(account='55009680',session_id=123457)
    models1.connect()
    logger.info(f"测试查询持仓: {models1.query_stock_positions()}")

