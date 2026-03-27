# -*- coding: utf-8 -*-

"""
Web服务模块，提供RESTful API接口与前端交互
"""
import os
import re
import time
from functools import wraps
import json
import threading
import sqlite3
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory, make_response, Response, stream_with_context
from flask_cors import CORS
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import Methods as Methods
import config
from logger import get_logger
from data_manager import get_data_manager
from indicator_calculator import get_indicator_calculator
from position_manager import get_position_manager, _create_qmt_trader
from trading_executor import get_trading_executor
from strategy import get_trading_strategy
from config_manager import get_config_manager
from grid_validation import validate_grid_config, validate_grid_template
import utils


# 获取logger
logger = get_logger("web_server")
webpage_dir = 'web1.0'

# 创建Flask应用
app = Flask(__name__, static_folder=webpage_dir, static_url_path='')

# 允许局域网跨域请求（localhost + 常见局域网 IP 段：192.168.x.x / 10.x.x.x / 172.16-31.x.x）
_LAN_ORIGIN_PATTERN = re.compile(
    r'^https?://(localhost|127\.0\.0\.1'
    r'|192\.168\.\d{1,3}\.\d{1,3}'
    r'|10\.\d{1,3}\.\d{1,3}\.\d{1,3}'
    r'|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})'
    r'(:\d+)?$'
)
CORS(app, origins=_LAN_ORIGIN_PATTERN)


def require_token(f):
    """Token 认证装饰器，保护敏感 API 端点。
    通过环境变量 QMT_API_TOKEN 启用：未设置或为空时跳过验证（适合纯内网部署）。
    验证方式：请求头 X-API-Token 或 URL 参数 token。
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        token = config.WEB_API_TOKEN
        if not token:
            return f(*args, **kwargs)
        provided = request.headers.get("X-API-Token") or request.args.get("token")
        if provided != token:
            logger.warning(f"[安全] 未授权访问被拒绝: {request.method} {request.path} 来自 {request.remote_addr}")
            return jsonify({"success": False, "error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

# ======================= Web访问日志中间件 =======================
@app.before_request
def log_request_start():
    """记录请求开始时间"""
    if config.ENABLE_WEB_ACCESS_LOG:
        from flask import g
        g.request_start_time = time.time()

@app.after_request
def log_request_end(response):
    """记录请求完成信息"""
    try:
        # 检查是否启用访问日志
        if not config.ENABLE_WEB_ACCESS_LOG:
            return response

        # 获取请求信息
        method = request.method
        path = request.path
        status_code = response.status_code

        # 检查是否在排除列表中
        for exclude_path in config.WEB_ACCESS_LOG_EXCLUDE_PATHS:
            # 处理通配符路径（如 "/<path:filename>"）
            if exclude_path.startswith("/<path:"):
                # 静态文件路径，检查是否以常见静态文件扩展名结尾
                if any(path.endswith(ext) for ext in ['.html', '.css', '.js', '.png', '.jpg', '.ico', '.svg', '.woff', '.woff2', '.ttf']):
                    return response
            elif path.startswith(exclude_path):
                return response

        # 计算请求耗时
        elapsed_ms = None
        if config.WEB_ACCESS_LOG_INCLUDE_TIMING:
            from flask import g
            if hasattr(g, 'request_start_time'):
                elapsed_ms = int((time.time() - g.request_start_time) * 1000)

        # 构建日志消息
        if elapsed_ms is not None:
            log_msg = f"[WEB] {method} {path} {status_code} {elapsed_ms}ms"
        else:
            log_msg = f"[WEB] {method} {path} {status_code}"

        # 根据配置的日志级别和状态码决定记录方式
        log_level = config.WEB_ACCESS_LOG_LEVEL.upper()

        if log_level == "DEBUG":
            # DEBUG级别：记录所有请求的详细信息
            logger.debug(log_msg)
        elif log_level == "INFO":
            # INFO级别：记录所有请求的基本信息
            logger.info(log_msg)
        elif log_level == "WARNING":
            # WARNING级别：仅记录4xx/5xx错误
            if status_code >= 400:
                logger.warning(log_msg)

    except Exception as e:
        # 中间件异常不应影响业务逻辑
        logger.error(f"访问日志中间件异常: {str(e)}")

    return response

# 获取各个模块的实例
# 注意: position_manager通过set_position_manager由main.py传入
# 原因: 单例模式在多线程+Flask debug环境下不可靠
data_manager = get_data_manager()
indicator_calculator = get_indicator_calculator()
trading_executor = get_trading_executor()
trading_strategy = get_trading_strategy()
config_manager = get_config_manager()

# 全局变量，用于存储main.py传入的position_manager实例
_position_manager_instance = None

def set_position_manager(pm):
    """设置position_manager实例（由main.py调用）"""
    global _position_manager_instance
    _position_manager_instance = pm
    # logger.info(f"[DEBUG] set_position_manager: 设置position_manager id={id(pm)}")

def get_position_manager_instance():
    """获取position_manager实例（供API端点使用）"""
    global _position_manager_instance
    if _position_manager_instance is None:
        # 如果未设置，回退到单例模式
        # logger.warning("[DEBUG] _position_manager_instance为None，使用get_position_manager()单例")
        return get_position_manager()
    # logger.debug(f"[DEBUG] get_position_manager_instance: 返回position_manager id={id(_position_manager_instance)}")
    return _position_manager_instance

# 实时推送的数据
realtime_data = {
    'positions': {},
    'latest_prices': {},
    'trading_signals': {},
    'account_info': {},
    'positions_all': []  # Add new field for all positions data
}

# 创建线程池用于超时调用(最大2个工作线程,避免资源消耗)
api_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="api_timeout")

# ======================= 账户信息缓存（避免SSE阻塞） =======================
# 缓存有效期（秒），可在config.py中覆盖
ACCOUNT_INFO_CACHE_TTL = getattr(config, 'ACCOUNT_INFO_CACHE_TTL', 5.0)
# 刷新最小间隔（秒），防止频繁触发后台刷新
ACCOUNT_INFO_REFRESH_MIN_INTERVAL = getattr(config, 'ACCOUNT_INFO_REFRESH_MIN_INTERVAL', 2.0)

_account_info_cache = {'data': None, 'ts': 0.0}
_account_info_lock = threading.Lock()
_account_info_refresh_lock = threading.Lock()
_account_info_refreshing = False
_last_account_info_refresh_start = 0.0

def _build_default_account_info():
    """构造默认账户信息（保证前端字段完整）"""
    return {
        'account_id': '--',
        'account_type': '--',
        'available': 0.0,
        'frozen_cash': 0.0,
        'market_value': 0.0,
        'total_asset': 0.0,
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }

def _refresh_account_info_worker():
    """后台刷新账户信息，允许阻塞但不影响SSE"""
    global _account_info_refreshing
    try:
        position_manager = get_position_manager_instance()
        # 使用超时保护，避免 get_account_info() 无限阻塞导致刷新标志永久卡死

        # ⚠️ 检查executor是否已关闭
        if not api_executor or api_executor._shutdown:
            logger.debug("[账户刷新] API线程池已关闭，跳过刷新")
            return

        try:
            future = api_executor.submit(position_manager.get_account_info)
        except RuntimeError as e:
            # 捕获"cannot schedule new futures after shutdown"错误
            if "shutdown" in str(e).lower():
                logger.debug(f"[账户刷新] 线程池已关闭，跳过刷新: {str(e)}")
                return
            raise

        try:
            account_info = future.result(timeout=5.0) or {}
        except FuturesTimeoutError:
            logger.warning("账户信息查询超时（5秒），跳过本次刷新，更新缓存时间戳防止立即重试")
            with _account_info_lock:
                if _account_info_cache.get('data') is None:
                    _account_info_cache['data'] = _build_default_account_info()
                _account_info_cache['ts'] = time.time()
            return

        if not account_info:
            account_info = _build_default_account_info()
        else:
            account_info.setdefault('timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

        with _account_info_lock:
            _account_info_cache['data'] = account_info
            _account_info_cache['ts'] = time.time()
    except Exception as e:
        logger.warning(f"账户信息刷新失败: {str(e)}")
    finally:
        with _account_info_refresh_lock:
            _account_info_refreshing = False

def _schedule_account_info_refresh(now_ts: float):
    """触发后台刷新（幂等+限频）"""
    global _account_info_refreshing, _last_account_info_refresh_start
    with _account_info_refresh_lock:
        if _account_info_refreshing:
            return
        if (now_ts - _last_account_info_refresh_start) < ACCOUNT_INFO_REFRESH_MIN_INTERVAL:
            return
        _account_info_refreshing = True
        _last_account_info_refresh_start = now_ts

        t = threading.Thread(
            target=_refresh_account_info_worker,
            name="account_info_refresh",
            daemon=True
        )
        t.start()

def get_account_info_cached(max_age: float = None):
    """获取账户信息缓存，过期则后台刷新，避免阻塞调用链"""
    if max_age is None:
        max_age = ACCOUNT_INFO_CACHE_TTL

    now_ts = time.time()
    with _account_info_lock:
        cached = _account_info_cache.get('data')
        ts = _account_info_cache.get('ts', 0.0)

    if cached is None or (now_ts - ts) > max_age:
        _schedule_account_info_refresh(now_ts)

    if not cached:
        return _build_default_account_info()
    return cached

# 实时推送线程
push_thread = None
stop_push_flag = False

@app.route('/')
def index():
    """Serve the index.html file"""
    return send_from_directory(os.path.join(os.path.dirname(__file__), webpage_dir), 'index.html')

@app.route('/<path:filename>')
def serve_static(filename):
    """Serve static files from the 'web' directory"""
    return send_from_directory(os.path.join(os.path.dirname(__file__), webpage_dir), filename)

@app.route('/api/connection/status', methods=['GET'])
def connection_status():
    """返回API连接状态 - 简化版本,避免阻塞"""
    try:
        # 使用传入的position_manager实例
        position_manager = get_position_manager_instance()

        # 模拟模式：无需真实 QMT，始终视为已连接
        if config.ENABLE_SIMULATION_MODE:
            is_connected = True
        else:
            # 读取 qmt_connected 标志位（非阻塞布尔读，由 on_disconnected/重连逻辑维护）
            # 不检查 xt_trader 对象存在性——对象存在不代表 QMT 进程在线
            is_connected = bool(getattr(position_manager, 'qmt_connected', False))

        return jsonify({
            'status': 'success',
            'connected': is_connected,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })
    except Exception as e:
        logger.error(f"检查API连接状态时出错: {str(e)}")
        return jsonify({
            'status': 'error',
            'connected': False,
            'message': f"检查API连接状态时出错: {str(e)}",
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })

@app.route('/api/status', methods=['GET'])
def get_status():
    """获取系统状态 - 增加超时保护"""
    try:
        # 动态获取position_manager以确保grid_manager已初始化
        position_manager = get_position_manager_instance()

        # 从缓存获取账户信息，避免阻塞请求线程
        account_info = get_account_info_cached()
        
        # 如果没有账户信息，使用默认值
        if not account_info:
            account_info = {
                'account_id': '--',
                'account_type': '--',
                'available': 0.0,
                'frozen_cash': 0.0,
                'market_value': 0.0,
                'total_asset': 0.0,
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            
        # 格式化为前端期望的结构
        account_data = {
            'id': account_info.get('account_id', '--'),
            'availableBalance': account_info.get('available', 0.0),
            'maxHoldingValue': account_info.get('market_value', 0.0),
            'totalAssets': account_info.get('total_asset', 0.0),
            'timestamp': account_info.get('timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        }
        
        # 监控状态 - 使用独立的配置标志，不再依赖线程状态判断
        is_monitoring = config.ENABLE_MONITORING

        # 添加额外日志用于调试
        logger.debug(f"当前状态: UI监控={is_monitoring}, 自动交易={config.ENABLE_AUTO_TRADING}, 持仓监控={config.ENABLE_POSITION_MONITOR}")

        # 获取全局设置状态 - 明确区分自动交易和监控状态
        system_settings = {
            'isMonitoring': is_monitoring,  # 监控状态
            'enableAutoTrading': config.ENABLE_AUTO_TRADING,  # 自动交易状态
            'positionMonitorRunning': config.ENABLE_POSITION_MONITOR,  # 增加持仓监控状态
            'allowBuy': getattr(config, 'ENABLE_ALLOW_BUY', True),
            'allowSell': getattr(config, 'ENABLE_ALLOW_SELL', True),
            'simulationMode': getattr(config, 'ENABLE_SIMULATION_MODE', False)
        }

        return jsonify({
            'status': 'success',
            'isMonitoring': is_monitoring,  # 顶层也返回监控状态
            'account': account_data,
            'settings': system_settings,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })
    except Exception as e:
        logger.error(f"获取系统状态时出错: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': f"获取系统状态时出错: {str(e)}"
        }), 500

@app.route('/api/positions', methods=['GET'])
def get_positions():
    """获取持仓信息 - 增加版本号支持"""
    try:
        # 动态获取position_manager以确保grid_manager已初始化
        position_manager = get_position_manager_instance()

        # ⭐ 确保grid_manager已初始化(用于Web界面重启的情况)
        if not position_manager.grid_manager and config.ENABLE_GRID_TRADING:
            try:
                position_manager.init_grid_manager(trading_executor)
                logger.info("[API] 已在API调用中初始化grid_manager")
            except Exception as e:
                logger.error(f"[API] 初始化grid_manager失败: {str(e)}")

        # ⭐ 性能优化: 获取客户端版本号
        # 🔧 修复: 默认值改为-1,确保首次请求返回完整数据
        client_version = request.args.get('version', -1, type=int)

        # 获取当前数据版本
        version_info = position_manager.get_data_version_info()
        current_version = version_info['version']

        # ⭐ 如果客户端版本是最新的，返回简化响应(减少90%数据传输)
        if client_version >= current_version:
            return jsonify({
                'status': 'success',
                'data': {
                    'positions': [],
                    'metrics': {},
                    'positions_all': []
                },
                'data_version': current_version,
                'no_change': True
            })

        # 版本变化，返回完整数据
        positions = trading_executor.get_stock_positions()
        positions_df = pd.DataFrame(positions)

        # 计算持仓指标
        metrics = utils.calculate_position_metrics(positions_df)

        # 更新实时数据
        for pos in positions:
            stock_code = pos['stock_code']
            realtime_data['positions'][stock_code] = pos

        # ⭐ 新增: 为每个持仓添加网格会话状态
        grid_manager = getattr(position_manager, 'grid_manager', None)
        if grid_manager:
            # 为positions添加grid_session_active字段
            for pos in positions:
                stock_code = pos.get('stock_code')
                session = grid_manager.sessions.get(stock_code)
                pos['grid_session_active'] = (session is not None and session.status == 'active')

            # 为positions_all添加grid_session_active字段
            for pos in realtime_data['positions_all']:
                stock_code = pos.get('stock_code')
                session = grid_manager.sessions.get(stock_code)
                pos['grid_session_active'] = (session is not None and session.status == 'active')

        # 获取所有持仓数据
        positions_all_df = position_manager.get_all_positions_with_all_fields()
        # 修复NaN序列化问题: 将NaN替换为None以生成有效的JSON
        positions_all_df = positions_all_df.replace({pd.NA: None, float('nan'): None})
        realtime_data['positions_all'] = positions_all_df.to_dict('records')

        # ⭐ 为positions_all添加grid_session_active字段 (必须在to_dict之后)
        if grid_manager:
            for pos in realtime_data['positions_all']:
                stock_code = pos.get('stock_code')
                session = grid_manager.sessions.get(stock_code)
                pos['grid_session_active'] = (session is not None and session.status == 'active')

        response = make_response(jsonify({
            'status': 'success',
            'data': {
                'positions': positions,
                'metrics': metrics,
                'positions_all': realtime_data['positions_all']
            },
            'data_version': current_version,
            'no_change': False
        }))
        response.headers['Content-Type'] = 'application/json; charset=utf-8'
        return response
    except Exception as e:
        logger.error(f"获取持仓信息时出错: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': f"获取持仓信息时出错: {str(e)}"
        }), 500

@app.route('/api/trade-records', methods=['GET'])
def get_trade_records():
    """获取交易记录"""
    try:
        # 从交易执行器获取交易记录
        trades_df = trading_executor.get_trades()
        
        # 如果没有交易记录，返回空列表
        if trades_df.empty:
            return jsonify({'status': 'success', 'data': []})


        # 确保包含股票名称字段，如果没有则尝试获取
        if 'stock_name' not in trades_df.columns or trades_df['stock_name'].isnull().any():
            data_manager = get_data_manager()
            def get_name(code):
                try:
                    return data_manager.get_stock_name(code)
                except:
                    return code
            trades_df['stock_name'] = trades_df['stock_code'].apply(get_name)

        # Format 'trade_time' to 'YYYY-MM-DD'
        if 'trade_time' in trades_df.columns:
            trades_df['trade_time'] = pd.to_datetime(trades_df['trade_time']).dt.strftime('%Y-%m-%d %H:%M:%S')
        
        # Replace NaN with None (which will become null in JSON)
        trades_df = trades_df.replace({pd.NA: None, float('nan'): None})
        
        # 将 DataFrame 转换为 JSON 格式
        trade_records = trades_df.to_dict(orient='records')
        
        response = make_response(jsonify({
            'status': 'success',
            'data': trade_records
        }))        
        response.headers['Content-Type'] = 'application/json; charset=utf-8'
        return response
    except Exception as e:
        logger.error(f"获取交易记录时出错: {str(e)}")
        return jsonify({'status': 'error', 'message': f"获取交易记录时出错: {str(e)}"}), 500

# 配置管理API
@app.route('/api/config', methods=['GET'])
def get_config():
    """获取系统配置"""
    try:
        # 从config模块获取配置项
        config_data = {
            "singleBuyAmount": config.POSITION_UNIT,
            "firstProfitSell": config.INITIAL_TAKE_PROFIT_RATIO * 100,
            "firstProfitSellEnabled": config.ENABLE_DYNAMIC_STOP_PROFIT,
            "stockGainSellPencent": config.INITIAL_TAKE_PROFIT_RATIO_PERCENTAGE * 100,
            "allowBuy": getattr(config, 'ENABLE_ALLOW_BUY', True),
            "allowSell": getattr(config, 'ENABLE_ALLOW_SELL', True),
            "stopLossBuy": abs(config.BUY_GRID_LEVELS[1] - 1) * 100,
            "stopLossBuyEnabled": getattr(config, 'ENABLE_STOP_LOSS_BUY', True),  
            "stockStopLoss": abs(config.STOP_LOSS_RATIO) * 100,
            "StopLossEnabled": True,
            "singleStockMaxPosition": config.MAX_POSITION_VALUE,
            "totalMaxPosition": config.MAX_TOTAL_POSITION_RATIO * 1000000,
            "connectPort": config.WEB_SERVER_PORT,
            "totalAccounts": "127.0.0.1",
            "globalAllowBuySell": config.ENABLE_AUTO_TRADING,
            "simulationMode": getattr(config, 'ENABLE_SIMULATION_MODE', False)
        }
        
        # 获取参数范围
        param_ranges = {k: {'min': v['min'], 'max': v['max']} for k, v in config.CONFIG_PARAM_RANGES.items()}
        
        return jsonify({
            'status': 'success',
            'data': config_data,
            'ranges': param_ranges
        })
    except Exception as e:
        logger.error(f"获取配置时出错: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': f"获取配置时出错: {str(e)}"
        }), 500

@app.route('/api/config/save', methods=['POST'])
@require_token
def save_config():
    """保存系统配置（持久化到数据库）"""
    try:
        config_data = request.json

        # 参数校验
        validation_errors = []
        for param_name, value in config_data.items():
            # 检查类型，跳过布尔值和字符串
            if isinstance(value, bool) or isinstance(value, str):
                continue

            # 校验参数
            is_valid, error_msg = config.validate_config_param(param_name, value)
            if not is_valid:
                validation_errors.append(error_msg)

        # 如果有验证错误，返回错误信息
        if validation_errors:
            return jsonify({
                'status': 'error',
                'message': '参数校验失败',
                'errors': validation_errors
            }), 400

        # 用于持久化的配置字典（数据库键名 -> 实际值）
        db_configs = {}

        # 更新主要参数并准备持久化
        if "singleBuyAmount" in config_data:
            value = float(config_data["singleBuyAmount"])
            config.POSITION_UNIT = value
            db_configs['POSITION_UNIT'] = value

        if "firstProfitSell" in config_data:
            old_profit_ratio = config.INITIAL_TAKE_PROFIT_RATIO
            value = float(config_data["firstProfitSell"]) / 100
            config.INITIAL_TAKE_PROFIT_RATIO = value
            db_configs['INITIAL_TAKE_PROFIT_RATIO'] = value
            logger.info(f"平仓盈利阈值: {old_profit_ratio*100:.1f}% -> {float(config_data['firstProfitSell']):.1f}%")

        if "firstProfitSellEnabled" in config_data:
            value = bool(config_data["firstProfitSellEnabled"])
            config.ENABLE_DYNAMIC_STOP_PROFIT = value
            db_configs['ENABLE_DYNAMIC_STOP_PROFIT'] = value

        if "stockGainSellPencent" in config_data:
            value = float(config_data["stockGainSellPencent"]) / 100
            config.INITIAL_TAKE_PROFIT_RATIO_PERCENTAGE = value
            db_configs['INITIAL_TAKE_PROFIT_RATIO_PERCENTAGE'] = value

        if "stopLossBuy" in config_data:
            # 更新第二个网格级别
            old_stop_loss_buy_ratio = config.BUY_GRID_LEVELS[1]
            ratio = 1 - float(config_data["stopLossBuy"]) / 100
            config.BUY_GRID_LEVELS[1] = ratio
            db_configs['BUY_GRID_LEVEL_1'] = ratio
            logger.info(f"补仓止损阈值: {(1-old_stop_loss_buy_ratio)*100:.1f}% -> {float(config_data['stopLossBuy']):.1f}%")

        if "stockStopLoss" in config_data:
            old_stop_loss = config.STOP_LOSS_RATIO
            value = -float(config_data["stockStopLoss"]) / 100
            config.STOP_LOSS_RATIO = value
            db_configs['STOP_LOSS_RATIO'] = value
            logger.info(f"平仓止损比例: {abs(old_stop_loss)*100:.1f}% -> {float(config_data['stockStopLoss']):.1f}%")

        if "singleStockMaxPosition" in config_data:
            value = float(config_data["singleStockMaxPosition"])
            config.MAX_POSITION_VALUE = value
            db_configs['MAX_POSITION_VALUE'] = value

        if "totalMaxPosition" in config_data:
            value = float(config_data["totalMaxPosition"]) / 1000000
            config.MAX_TOTAL_POSITION_RATIO = value
            db_configs['MAX_TOTAL_POSITION_RATIO'] = value

        # 开关类参数
        if "allowBuy" in config_data:
            value = bool(config_data["allowBuy"])
            setattr(config, 'ENABLE_ALLOW_BUY', value)
            db_configs['ENABLE_ALLOW_BUY'] = value

        if "allowSell" in config_data:
            value = bool(config_data["allowSell"])
            setattr(config, 'ENABLE_ALLOW_SELL', value)
            db_configs['ENABLE_ALLOW_SELL'] = value

        if "globalAllowBuySell" in config_data:
            old_auto_trading = config.ENABLE_AUTO_TRADING
            value = bool(config_data["globalAllowBuySell"])
            config.ENABLE_AUTO_TRADING = value
            # 注意：自动交易总开关不持久化，为了安全每次启动需手动确认
            logger.info(f"自动交易总开关: {old_auto_trading} -> {config.ENABLE_AUTO_TRADING} (仅运行时，不持久化)")
            # 从非自动交易切换到自动交易时，清除之前积累的信号，避免执行过期信号
            if not old_auto_trading and value:
                position_manager = get_position_manager_instance()
                position_manager.clear_all_signals(reason="切换到自动交易模式")

        # 处理模拟交易模式切换
        if "simulationMode" in config_data:
            old_simulation_mode = getattr(config, 'ENABLE_SIMULATION_MODE', False)
            new_simulation_mode = bool(config_data["simulationMode"])
            # 注意：模拟交易模式不持久化，避免误切换到实盘模式

            # 如果模式发生变化
            if old_simulation_mode != new_simulation_mode:
                setattr(config, 'ENABLE_SIMULATION_MODE', new_simulation_mode)

                # 模式变化时重新初始化内存数据库
                position_manager = get_position_manager_instance()
                # 创建新的内存连接
                position_manager.memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
                position_manager._create_memory_table()
                position_manager._sync_db_to_memory()  # 从SQLite重新加载数据

                # 🔧 Fix: 模式切换时同步初始化/清理 qmt_trader
                # 从模拟切换到实盘：初始化 qmt_trader 并连接
                # 从实盘切换到模拟：清理 qmt_trader
                if not new_simulation_mode:
                    # 切换到实盘模式：尝试初始化 QMT 连接
                    logger.info("模式切换: 尝试初始化实盘交易接口...")
                    try:
                        # 异步连接，避免阻塞API线程
                        position_manager.qmt_connected = False
                        position_manager.start_qmt_connect_async(reason="mode_switch")
                        logger.info("模式切换: 已发起QMT连接请求(异步)")
                    except Exception as e:
                        logger.error(f"模式切换: 初始化实盘交易接口失败: {e}")
                        position_manager.qmt_connected = False
                else:
                    # 切换到模拟模式：清理 qmt_trader
                    logger.info("模式切换: 清理实盘交易接口，切换到模拟模式")
                    position_manager.qmt_trader = None
                    position_manager.qmt_connected = False

                logger.warning(f"交易模式切换: {'模拟交易' if new_simulation_mode else '实盘交易'} (仅运行时，不持久化)")

        # 处理补仓功能开关
        if "stopLossBuyEnabled" in config_data:
            old_stop_loss_buy = getattr(config, 'ENABLE_STOP_LOSS_BUY', True)
            new_stop_loss_buy = bool(config_data["stopLossBuyEnabled"])
            setattr(config, 'ENABLE_STOP_LOSS_BUY', new_stop_loss_buy)
            db_configs['ENABLE_STOP_LOSS_BUY'] = new_stop_loss_buy
            logger.info(f"补仓功能开关: {old_stop_loss_buy} -> {new_stop_loss_buy}")

        # 持久化所有配置到数据库
        success_count, fail_count = config_manager.save_batch_configs(db_configs)

        logger.info(f"配置已更新并持久化: {len(db_configs)} 个配置项, 成功: {success_count}, 失败: {fail_count}")

        return jsonify({
            'status': 'success',
            'message': f'配置已保存并应用 (成功: {success_count}, 失败: {fail_count})',
            'isMonitoring': config.ENABLE_MONITORING,
            'autoTradingEnabled': config.ENABLE_AUTO_TRADING,
            'saved_count': success_count,
            'failed_count': fail_count
        })
    except Exception as e:
        logger.error(f"保存配置时出错: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': f"保存配置失败: {str(e)}"
        }), 500

@app.route('/api/monitor/start', methods=['POST'])
def start_monitor():
    """启动监控 - 仅控制前端数据刷新"""
    try:
        old_state = config.ENABLE_MONITORING
        config.ENABLE_MONITORING = True
        
        logger.info(f"UI监控状态变更: {old_state} -> {config.ENABLE_MONITORING} (通过API)")
        
        return jsonify({
            'status': 'success',
            'message': '监控已启动',
            'isMonitoring': config.ENABLE_MONITORING,
            'autoTradingEnabled': config.ENABLE_AUTO_TRADING  # 返回不变的自动交易状态
        })
    except Exception as e:
        logger.error(f"启动监控时出错: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': f"启动监控失败: {str(e)}"
        }), 500

@app.route('/api/monitor/stop', methods=['POST'])
def stop_monitor():
    """停止监控"""
    try:
        old_state = config.ENABLE_MONITORING
        
        # 确保变量类型一致，统一使用布尔值
        config.ENABLE_MONITORING = False
        
        # 如果状态没有发生变化，发出警告日志
        if old_state == config.ENABLE_MONITORING:
            logger.warning(f"UI监控状态未变化: {old_state} -> {config.ENABLE_MONITORING} (通过API)")
        else:
            logger.info(f"UI监控状态变更: {old_state} -> {config.ENABLE_MONITORING} (通过API)")
        
        return jsonify({
            'status': 'success',
            'message': '监控已停止',
            'isMonitoring': config.ENABLE_MONITORING,  # 明确返回新状态
            'autoTradingEnabled': config.ENABLE_AUTO_TRADING  # 同时返回自动交易状态
        })
    except Exception as e:
        logger.error(f"停止监控时出错: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': f"停止监控失败: {str(e)}"
        }), 500

# @app.route('/api/data_sources/status', methods=['GET'])
# def get_data_sources_status():
#     """获取数据源状态"""
#     try:
#         # 添加更详细的错误处理
#         try:
#             from realtime_data_manager import get_realtime_data_manager
#             manager = get_realtime_data_manager()
#         except ImportError as e:
#             logger.error(f"导入realtime_data_manager失败: {str(e)}")
#             return jsonify({
#                 'status': 'error',
#                 'message': f"数据管理器模块导入失败: {str(e)}"
#             }), 500
#         except Exception as e:
#             logger.error(f"初始化数据管理器失败: {str(e)}")
#             return jsonify({
#                 'status': 'error',
#                 'message': f"数据管理器初始化失败: {str(e)}"
#             }), 500
        
#         status = manager.get_source_status()
        
#         return jsonify({
#             'status': 'success',
#             'data': status,
#             'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
#         })
#     except Exception as e:
#         logger.error(f"获取数据源状态时出错: {str(e)}")
#         return jsonify({
#             'status': 'error',
#             'message': f"获取数据源状态失败: {str(e)}"
#         }), 500

# @app.route('/api/data_sources/switch', methods=['POST'])
# def switch_data_source():
#     """手动切换数据源"""
#     try:
#         data = request.json
#         if not data:
#             return jsonify({
#                 'status': 'error',
#                 'message': '请求数据不能为空'
#             }), 400
            
#         source_name = data.get('source_name')
#         if not source_name:
#             return jsonify({
#                 'status': 'error',
#                 'message': '缺少source_name参数'
#             }), 400
        
#         # 数据源名称映射
#         source_mapping = {
#             'MootdxSource': 'Mootdx',
#             'XtQuantSource': 'XtQuant',
#             'Mootdx': 'Mootdx',
#             'XtQuant': 'XtQuant'
#         }
        
#         actual_source_name = source_mapping.get(source_name, source_name)
        
#         try:
#             from realtime_data_manager import get_realtime_data_manager
#             manager = get_realtime_data_manager()
#         except Exception as e:
#             return jsonify({
#                 'status': 'error',
#                 'message': f'数据管理器初始化失败: {str(e)}'
#             }), 500
        
#         # 使用新的切换方法
#         if manager.switch_to_source(actual_source_name):
#             return jsonify({
#                 'status': 'success',
#                 'message': f"已切换到数据源: {actual_source_name}",
#                 'current_source': actual_source_name
#             })
#         else:
#             return jsonify({
#                 'status': 'error',
#                 'message': f"无法切换到数据源: {actual_source_name}，请检查数据源名称是否正确"
#             }), 400
        
#     except Exception as e:
#         logger.error(f"切换数据源时出错: {str(e)}")
#         return jsonify({
#             'status': 'error',
#             'message': f"切换数据源失败: {str(e)}"
#         }), 500

# @app.route('/api/realtime/quote/<stock_code>', methods=['GET'])
# def get_realtime_quote(stock_code):
#     """获取单只股票的实时行情"""
#     try:
#         # 直接从实时数据管理器获取数据
#         from realtime_data_manager import get_realtime_data_manager
#         manager = get_realtime_data_manager()
        
#         start_time = time.time()
#         data = manager.get_realtime_data(stock_code)
#         end_time = time.time()
        
#         if data:
#             data['response_time_ms'] = round((end_time - start_time) * 1000, 2)
#             return jsonify({
#                 'status': 'success',
#                 'data': data,
#                 'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
#             })
#         else:
#             return jsonify({
#                 'status': 'error',
#                 'message': f'无法获取{stock_code}的实时数据'
#             }), 404
            
#     except Exception as e:
#         logger.error(f"获取实时行情时出错: {str(e)}")
#         return jsonify({
#             'status': 'error',
#             'message': f'获取实时行情失败: {str(e)}'
#         }), 500

# @app.route('/api/realtime/test/<stock_code>', methods=['GET'])
# def test_all_sources(stock_code):
#     """测试所有数据源获取指定股票数据"""
#     try:
#         from realtime_data_manager import get_realtime_data_manager
#         manager = get_realtime_data_manager()
        
#         results = {}
        
#         # 测试每个数据源
#         for source in manager.data_sources:
#             start_time = time.time()
#             try:
#                 data = source.get_data(stock_code)
#                 end_time = time.time()
                
#                 if data:
#                     results[source.name] = {
#                         'success': True,
#                         'data': data,
#                         'response_time_ms': round((end_time - start_time) * 1000, 2),
#                         'error_count': source.error_count,
#                         'is_healthy': source.is_healthy
#                     }
#                 else:
#                     results[source.name] = {
#                         'success': False,
#                         'error': '无数据返回',
#                         'response_time_ms': round((end_time - start_time) * 1000, 2),
#                         'error_count': source.error_count,
#                         'is_healthy': source.is_healthy
#                     }
#             except Exception as e:
#                 end_time = time.time()
#                 results[source.name] = {
#                     'success': False,
#                     'error': str(e),
#                     'response_time_ms': round((end_time - start_time) * 1000, 2),
#                     'error_count': source.error_count,
#                     'is_healthy': source.is_healthy
#                 }
        
#         return jsonify({
#             'status': 'success',
#             'stock_code': stock_code,
#             'results': results,
#             'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
#         })
        
#     except Exception as e:
#         logger.error(f"测试所有数据源时出错: {str(e)}")
#         return jsonify({
#             'status': 'error',
#             'message': f'测试失败: {str(e)}'
#         }), 500


@app.route('/api/debug/status', methods=['GET'])
def debug_status():
    """返回详细的系统状态，用于调试"""
    try:
        return jsonify({
            'status': 'success',
            'system_status': {
                'ENABLE_MONITORING': config.ENABLE_MONITORING,
                'ENABLE_AUTO_TRADING': config.ENABLE_AUTO_TRADING,
                'ENABLE_POSITION_MONITOR': config.ENABLE_POSITION_MONITOR,
                'ENABLE_ALLOW_BUY': getattr(config, 'ENABLE_ALLOW_BUY', True),
                'ENABLE_ALLOW_SELL': getattr(config, 'ENABLE_ALLOW_SELL', True),
                'ENABLE_SIMULATION_MODE': getattr(config, 'ENABLE_SIMULATION_MODE', False),
            },
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })
    except Exception as e:
        logger.error(f"获取调试状态时出错: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': f"获取调试状态失败: {str(e)}"
        }), 500

# @app.route('/api/debug/db-test', methods=['GET'])
# def test_database():
#     """测试数据库连接"""
#     try:
#         cursor = data_manager.conn.cursor()
#         cursor.execute("SELECT COUNT(*) FROM trade_records")
#         count = cursor.fetchone()[0]
#         return jsonify({
#             'status': 'success',
#             'message': '数据库连接正常',
#             'trade_records_count': count
#         })
#     except Exception as e:
#         return jsonify({
#             'status': 'error',
#             'message': f"数据库连接错误: {str(e)}"
#         }), 500


@app.route('/api/logs/clear', methods=['POST'])
@require_token
def clear_logs():
    """清空当天日志"""
    try:
        # 获取当天日期
        today = datetime.now().strftime('%Y-%m-%d')
        
        # 执行清空当天日志的操作
        cursor = data_manager.conn.cursor()
        # 修改SQL，添加日期过滤条件
        cursor.execute("DELETE FROM trade_records WHERE DATE(trade_time) = ?", (today,))
        affected_rows = cursor.rowcount
        data_manager.conn.commit()
        
        logger.info(f"已清除当天({today})的交易记录，共{affected_rows}条")
        
        return jsonify({
            'status': 'success',
            'message': f'已清除当天交易记录，共{affected_rows}条'
        })
    except Exception as e:
        logger.error(f"清空当天日志时出错: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': f"清空当天日志失败: {str(e)}"
        }), 500

# @app.route('/api/data/clear_current', methods=['POST'])
# def clear_current_data():
#     try:
#         # 修改：清空内存数据库中的持仓数据，而非SQLite
#         cursor = position_manager.memory_conn.cursor()
#         cursor.execute("DELETE FROM positions")
#         position_manager.memory_conn.commit()
        
#         # 重置缓存
#         position_manager.positions_cache = None
#         position_manager.last_position_update_time = 0
        
#         logger.info("内存数据库中的持仓数据已清空")
        
#         return jsonify({
#             'status': 'success',
#             'message': '当前数据已清空'
#         })
#     except Exception as e:
#         logger.error(f"清空当前数据时出错: {str(e)}")
#         return jsonify({
#             'status': 'error',
#             'message': f"清空当前数据失败: {str(e)}"
#         }), 500

@app.route('/api/data/clear_buysell', methods=['POST'])
@require_token
def clear_buysell_data():
    """清空买入/卖出数据"""
    try:
        # 清空交易记录
        cursor = data_manager.conn.cursor()
        cursor.execute("DELETE FROM trade_records")
        data_manager.conn.commit()
        
        return jsonify({
            'status': 'success',
            'message': '买入/卖出数据已清空'
        })
    except Exception as e:
        logger.error(f"清空买入/卖出数据时出错: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': f"清空买入/卖出数据失败: {str(e)}"
        }), 500

@app.route('/api/data/import', methods=['POST'])
@require_token
def import_data():
    """导入保存数据"""
    try:
        # 这里需要实现导入数据的逻辑
        # 由于没有具体实现，返回成功消息
        return jsonify({
            'status': 'success',
            'message': '数据导入成功'
        })
    except Exception as e:
        logger.error(f"导入数据时出错: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': f"导入数据失败: {str(e)}"
        }), 500

@app.route('/api/initialize_positions', methods=['POST'])
@require_token
def api_initialize_positions():
    """初始化持仓数据的API端点"""
    try:
        # 动态获取position_manager以确保grid_manager已初始化
        position_manager = get_position_manager_instance()

        result = position_manager.initialize_all_positions_data()
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"API调用初始化持仓数据失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': f'操作失败: {str(e)}',
            'updated_count': 0
        }), 500

@app.route('/api/holdings/init', methods=['POST'])
@require_token
def init_holdings():
    """初始化持仓数据"""
    try:
        # 获取配置数据
        if request.is_json:
            config_data = request.json
            
            # 校验并保存配置
            # 这里重复使用save_config的代码
            validation_errors = []
            for param_name, value in config_data.items():
                # 检查类型，跳过布尔值和字符串
                if isinstance(value, bool) or isinstance(value, str):
                    continue
                    
                # 校验参数
                is_valid, error_msg = config.validate_config_param(param_name, value)
                if not is_valid:
                    validation_errors.append(error_msg)
            
            # 如果有验证错误，返回错误信息
            if validation_errors:
                return jsonify({
                    'status': 'error',
                    'message': '参数校验失败，无法初始化持仓',
                    'errors': validation_errors
                }), 400
            
            # 应用配置
            # 更新主要参数
            if "singleBuyAmount" in config_data:
                config.POSITION_UNIT = float(config_data["singleBuyAmount"])
            if "firstProfitSell" in config_data:
                config.INITIAL_TAKE_PROFIT_RATIO = float(config_data["firstProfitSell"]) / 100
            if "firstProfitSellEnabled" in config_data:
                config.ENABLE_DYNAMIC_STOP_PROFIT = bool(config_data["firstProfitSellEnabled"])
            if "stockGainSellPencent" in config_data:
                config.INITIAL_TAKE_PROFIT_RATIO_PERCENTAGE = float(config_data["stockGainSellPencent"]) / 100
            if "stopLossBuy" in config_data:
                # 更新第二个网格级别
                ratio = 1 - float(config_data["stopLossBuy"]) / 100
                config.BUY_GRID_LEVELS[1] = ratio
            if "stockStopLoss" in config_data:
                config.STOP_LOSS_RATIO = -float(config_data["stockStopLoss"]) / 100
            if "singleStockMaxPosition" in config_data:
                config.MAX_POSITION_VALUE = float(config_data["singleStockMaxPosition"])
            if "totalMaxPosition" in config_data:
                config.MAX_TOTAL_POSITION_RATIO = float(config_data["totalMaxPosition"]) / 1000000
                
            # 开关类参数
            if "allowBuy" in config_data:
                setattr(config, 'ENABLE_ALLOW_BUY', bool(config_data["allowBuy"]))
            if "allowSell" in config_data:
                setattr(config, 'ENABLE_ALLOW_SELL', bool(config_data["allowSell"]))
            if "globalAllowBuySell" in config_data:
                config.ENABLE_AUTO_TRADING = bool(config_data["globalAllowBuySell"])
            if "simulationMode" in config_data:
                setattr(config, 'ENABLE_SIMULATION_MODE', bool(config_data["simulationMode"]))
        
        # 初始化持仓数据
        # 这里需要实现初始化持仓的逻辑
        # 假设我们直接从交易执行器获取持仓
        # positions = trading_executor.get_stock_positions()
        
        # # 导入最新持仓
        # for pos in positions:
        #     # 假设position_manager有一个update_position方法
        #     position_manager.update_position(
        #         stock_code=pos['stock_code'],
        #         volume=pos['volume'],
        #         cost_price=pos['cost_price'],
        #         current_price=pos['current_price']
        #     )

        # return jsonify({
        #     'status': 'success',
        #     'message': '持仓数据初始化成功',
        #     'count': len(positions)
        # })

        result = get_position_manager_instance().initialize_all_positions_data()
        return jsonify(result)

    except Exception as e:
        logger.error(f"初始化持仓数据时出错: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': f"初始化持仓数据失败: {str(e)}"
        }), 500

@app.route('/api/stock_pool/list', methods=['GET'])
def get_stock_pool():
    """获取备选池股票列表"""
    try:
        # 读取备选池股票文件
        file_path = config.STOCK2BUY_FILE
        if os.path.exists(file_path):
            with open(file_path, 'r', encoding='utf-8') as f:
                stock_pool = json.load(f)
        else:
            # 如果文件不存在，使用默认股票池
            stock_pool = config.STOCK_POOL
            
        return jsonify({
            'status': 'success',
            'data': stock_pool
        })
    except Exception as e:
        logger.error(f"获取备选池股票列表时出错: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': f"获取备选池股票列表失败: {str(e)}"
        }), 500

@app.route('/api/actions/execute_buy', methods=['POST'])
@require_token
def execute_buy():
    """执行买入操作"""
    try:
        buy_data = request.json
        strategy = buy_data.get('strategy', 'random_pool')
        quantity = int(buy_data.get('quantity', 0))
        stocks = buy_data.get('stocks', [])
        
        if quantity <= 0:
            return jsonify({
                'status': 'error',
                'message': '买入数量必须大于0'
            }), 400
        
        if not stocks:
            return jsonify({
                'status': 'error',
                'message': '未提供股票列表'
            }), 400
        
        # 根据当前交易模式调整股票代码格式
        is_simulation = getattr(config, 'ENABLE_SIMULATION_MODE', False)
        formatted_stocks = []
        
        for stock in stocks:
            # 移除已有的后缀（如果有）
            if stock.endswith(('.SH', '.SZ', '.sh', '.sz')):
                stock_code = stock.split('.')[0]
            else:
                stock_code = stock
                
            # 根据交易模式决定是否添加后缀
            if is_simulation:
                # 模拟交易模式：使用Methods.add_xt_suffix添加市场后缀
                formatted_stock = Methods.add_xt_suffix(stock_code)
            else:
                # 实盘交易模式：使用无后缀格式
                formatted_stock = stock_code
                
            formatted_stocks.append(formatted_stock)
        
        # 使用修改后的股票列表
        logger.info(f"交易模式: {'模拟' if is_simulation else '实盘'}, 股票代码格式化: {stocks} -> {formatted_stocks}")
        
        # 根据策略选择股票
        selected_stocks = []
        if strategy == 'random_pool':
            # 随机选择指定数量的股票
            import random
            if quantity <= len(formatted_stocks):
                selected_stocks = random.sample(formatted_stocks, quantity)
            else:
                selected_stocks = formatted_stocks
        elif strategy == 'custom_stock':
            # 使用用户提供的股票列表
            selected_stocks = formatted_stocks[:quantity]  # 取指定数量
        
        # 执行买入
        success_count = 0
        for stock_code in selected_stocks:
            # 计算买入金额
            amount = config.POSITION_UNIT
            
            # 执行买入
            order_id = trading_strategy.manual_buy(
                stock_code=stock_code,
                amount=amount
            )
            
            if order_id:
                success_count += 1
        
        return jsonify({
            'status': 'success',
            'message': f'成功发送{success_count}个买入指令',
            'success_count': success_count,
            'total_count': len(selected_stocks)
        })
    except Exception as e:
        logger.error(f"执行买入操作时出错: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': f"执行买入操作失败: {str(e)}"
        }), 500

@app.route('/api/holdings/update', methods=['POST'])
@require_token
def update_holding_params():
    """更新持仓参数"""
    try:
        # 动态获取position_manager以确保grid_manager已初始化
        position_manager = get_position_manager_instance()

        data = request.json
        stock_code = data.get('stock_code')
        profit_triggered = data.get('profit_triggered')
        highest_price = data.get('highest_price')
        stop_loss_price = data.get('stop_loss_price')
        
        if not stock_code:
            return jsonify({
                'status': 'error',
                'message': '股票代码不能为空'
            }), 400
        
        # 获取当前持仓
        position = position_manager.get_position(stock_code)
        if not position:
            return jsonify({
                'status': 'error',
                'message': f'未找到{stock_code}的持仓信息'
            }), 404
        
        # 更新持仓参数
        position_manager.update_position(
            stock_code=stock_code,
            volume=position['volume'],
            cost_price=position['cost_price'],
            profit_triggered=profit_triggered if profit_triggered is not None else position['profit_triggered'],
            highest_price=highest_price if highest_price is not None else position['highest_price'],
            stop_loss_price=stop_loss_price if stop_loss_price is not None else position['stop_loss_price']
        )
        
        return jsonify({
            'status': 'success',
            'message': f'{stock_code}持仓参数更新成功'
        })
    except Exception as e:
        logger.error(f"更新持仓参数时出错: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': f"更新持仓参数失败: {str(e)}"
        }), 500

# 添加SSE接口
@app.route('/api/sse', methods=['GET'])
def sse():
    """提供Server-Sent Events流 - 增强版（支持定时推送）"""
    # 动态获取position_manager以确保grid_manager已初始化
    position_manager = get_position_manager_instance()
    def event_stream():
        last_positions_version = 0
        prev_data = None
        last_push_time = time.time()  # 🔧 新增：记录上次推送时间
        FORCE_PUSH_INTERVAL = 5.0  # 🔧 强制推送间隔（秒）

        while True:
            try:
                # 检查持仓数据是否有变化
                version_info = position_manager.get_data_version_info()
                current_version = version_info['version']
                data_changed = version_info['changed']
                current_time = time.time()

                # 获取基础数据
                account_info = get_account_info_cached()

                current_data = {
                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'account_info': {
                        'available': account_info.get('available', 0),
                        'market_value': account_info.get('market_value', 0),
                        'total_asset': account_info.get('total_asset', 0)
                    },
                    'monitoring': {
                        'isMonitoring': config.ENABLE_MONITORING,
                        'autoTradingEnabled': config.ENABLE_AUTO_TRADING,
                        'allowBuy': getattr(config, 'ENABLE_ALLOW_BUY', True),
                        'allowSell': getattr(config, 'ENABLE_ALLOW_SELL', True),
                        'simulationMode': getattr(config, 'ENABLE_SIMULATION_MODE', False)
                    }
                }

                # 🔧 修改：始终添加持仓更新通知（无论是否变化）
                # 🔴 BUG修复：检测版本号任何变化（包括回退），防止系统重启后页面冻结
                version_changed = current_version != last_positions_version
                if version_changed:
                    # 检测版本回退（系统可能重启）
                    if current_version < last_positions_version:
                        logger.warning(f"⚠️ 检测到版本号回退: v{last_positions_version} → v{current_version} (系统可能已重启)")

                    current_data['positions_update'] = {
                        'version': current_version,
                        'changed': True
                    }
                    last_positions_version = current_version
                    logger.debug(f"SSE推送持仓数据变化通知: v{current_version}")
                else:
                    # 即使版本未变化，也添加字段（标记为未变化）
                    current_data['positions_update'] = {
                        'version': current_version,
                        'changed': False
                    }

                # 🔧 修改推送逻辑：数据变化或超过5秒都要推送
                should_push = False
                if current_data != prev_data:
                    should_push = True
                    logger.debug("SSE推送：数据变化")
                elif current_time - last_push_time >= FORCE_PUSH_INTERVAL:
                    should_push = True
                    logger.debug("SSE推送：定时推送（5秒）")

                if should_push:
                    yield f"data: {json.dumps(current_data)}\n\n"
                    prev_data = current_data
                    last_push_time = current_time

                    # 标记数据已被消费
                    if data_changed:
                        position_manager.mark_data_consumed()

            except Exception as e:
                logger.error(f"SSE流生成数据时出错: {str(e)}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

            time.sleep(1)  # 减少到1秒检查一次
    
    return Response(stream_with_context(event_stream()), 
                   mimetype="text/event-stream",
                   headers={"Cache-Control": "no-cache",
                            "X-Accel-Buffering": "no"})

# 修改get_positions_all函数，添加数据版本号
@app.route('/api/positions-all', methods=['GET'])
def get_positions_all():
    """获取所有持仓信息 - 增加版本号支持"""
    try:
        # 动态获取position_manager以确保grid_manager已初始化
        position_manager = get_position_manager_instance()

        # 获取客户端版本号
        client_version = request.args.get('version', 0, type=int)
        
        # 获取当前数据版本
        version_info = position_manager.get_data_version_info()
        current_version = version_info['version']

        # 🔧 修复: 只有当客户端版本大于服务器版本时才返回无变化
        # 初始请求(version=0)必须返回完整数据
        if client_version > 0 and client_version >= current_version:
            return jsonify({
                'status': 'success',
                'data': [],
                'data_version': current_version,
                'no_change': True
            })
        
        # 获取完整数据
        positions_all_df = position_manager.get_all_positions_with_all_fields()
        positions_all_df = positions_all_df.replace({pd.NA: None, float('nan'): None})
        positions_all = positions_all_df.to_dict('records')

        # ⭐ 为positions_all添加grid_session_active字段 (必须在to_dict之后)
        grid_manager = position_manager.grid_manager
        if grid_manager:
            for pos in positions_all:
                stock_code = pos.get('stock_code')
                # 🔧 修复: 尝试带后缀和不带后缀两种格式查询
                session = grid_manager.sessions.get(stock_code)
                if not session and '.' not in stock_code:
                    # 如果不带后缀，尝试添加.SH和.SZ后缀
                    session = grid_manager.sessions.get(f"{stock_code}.SH") or \
                              grid_manager.sessions.get(f"{stock_code}.SZ")
                pos['grid_session_active'] = (session is not None and session.status == 'active')
        else:
            # 如果grid_manager未初始化，所有股票设为False
            for pos in positions_all:
                pos['grid_session_active'] = False

        # 更新实时数据
        realtime_data['positions_all'] = positions_all

        response = make_response(jsonify({
            'status': 'success',
            'data': positions_all,
            'data_version': current_version,
            'no_change': False
        }))
        response.headers['Content-Type'] = 'application/json; charset=utf-8'
        return response
    except Exception as e:
        logger.error(f"获取所有持仓信息时出错: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': f"获取所有持仓信息时出错: {str(e)}"
        }), 500

def push_realtime_data():
    """推送实时数据的线程函数"""
    # 动态获取position_manager以确保grid_manager已初始化
    position_manager = get_position_manager_instance()
    global stop_push_flag

    while not stop_push_flag:
        try:
            # 不限制交易时间：只要 xtquant 接口正常，随时更新 web 持仓数据
            # 更新所有持仓的最新价格（内部已做 None 判断，非交易时段价格不变则跳过）
            position_manager.update_all_positions_price()

            # ⚠️ 检查停止标志，如果已设置则立即退出
            if stop_push_flag:
                logger.info("[推送线程] 检测到停止标志，退出循环")
                break

            # 获取所有持仓数据
            positions_all_df = position_manager.get_all_positions_with_all_fields()

            # ⚠️ 再次检查停止标志
            if stop_push_flag:
                logger.info("[推送线程] 检测到停止标志，退出循环")
                break

            # 处理NaN值
            positions_all_df = positions_all_df.replace({pd.NA: None, float('nan'): None})

            # 转换为字典列表
            positions_all = positions_all_df.to_dict('records')

            # ⭐ 为positions_all添加grid_session_active字段 (必须在to_dict之后)
            grid_manager = position_manager.grid_manager
            if grid_manager:
                for pos in positions_all:
                    stock_code = pos.get('stock_code')
                    session = grid_manager.sessions.get(stock_code)
                    pos['grid_session_active'] = (session is not None and session.status == 'active')
            else:
                # 如果grid_manager未初始化，所有股票设为False
                for pos in positions_all:
                    pos['grid_session_active'] = False

            # 更新实时数据
            realtime_data['positions_all'] = positions_all

            # 休眠间隔（分段休眠，更快响应停止信号）
            for _ in range(30):  # 3秒 = 30 * 0.1秒
                if stop_push_flag:
                    logger.info("[推送线程] 休眠中检测到停止标志，立即退出")
                    return
                time.sleep(0.1)
        except Exception as e:
            logger.error(f"推送实时数据时出错: {str(e)}")
            # 出错后也要分段休眠
            for _ in range(30):
                if stop_push_flag:
                    return
                time.sleep(0.1)


def start_push_thread():
    """启动实时推送线程"""
    global push_thread
    global stop_push_flag

    if push_thread is None or not push_thread.is_alive():
        stop_push_flag = False
        push_thread = threading.Thread(target=push_realtime_data)
        push_thread.daemon = True
        push_thread.start()
        logger.info("实时推送线程已启动")
    else:
        logger.warning("实时推送线程已在运行")

def sync_auto_trading_status():
    """ 20251219修复: Web服务器启动时同步ENABLE_AUTO_TRADING状态

    问题: ENABLE_AUTO_TRADING不持久化导致重启后数据库和内存不一致
    - 数据库: 保存Web界面设置的值(可能是True)
    - 内存: 程序启动时从config.py加载默认值(False)

    解决: Web启动时将内存状态同步到数据库,确保显示与实际一致
    """
    try:
        memory_value = config.ENABLE_AUTO_TRADING
        db_value = config_manager.load_config('ENABLE_AUTO_TRADING', None)

        if db_value is None:
            # 数据库中没有记录,写入当前内存值
            config_manager.save_config('ENABLE_AUTO_TRADING', memory_value)
            logger.info(f"🔄 初始化配置同步: ENABLE_AUTO_TRADING = {memory_value} (内存 → 数据库)")
        elif db_value != memory_value:
            # 数据库和内存不一致,以内存为准(因为不持久化设计)
            config_manager.save_config('ENABLE_AUTO_TRADING', memory_value)
            logger.warning(f"🔄 配置不一致修复: ENABLE_AUTO_TRADING 数据库={db_value} → 内存={memory_value}")
            logger.warning(f"⚠️  Web界面现在将显示实际运行状态: {memory_value}")
        else:
            logger.info(f"✅ 配置一致性验证通过: ENABLE_AUTO_TRADING = {memory_value}")
    except Exception as e:
        logger.error(f"❌ 同步ENABLE_AUTO_TRADING状态失败: {str(e)}")

# ======================= 网格交易API端点 (2026-01-24) =======================

def normalize_stock_code(stock_code: str) -> str:
    """
    标准化股票代码，自动补充市场后缀

    Args:
        stock_code: 股票代码，可能缺少.SH或.SZ后缀

    Returns:
        标准化后的股票代码 (格式: XXXXXX.SH 或 XXXXXX.SZ)
    """
    if not stock_code:
        return stock_code

    # 如果已经有后缀，直接返回
    if '.' in stock_code:
        return stock_code

    # 自动判断市场（基于A股规则）
    # 上海交易所: 60xxxx(主板), 688xxx(科创板), 689xxx(科创板), 900xxx(B股)
    # 深圳交易所: 00xxxx(主板), 30xxxx(创业板), 200xxx(B股)
    if stock_code.startswith(('6', '900')):
        return stock_code + '.SH'
    elif stock_code.startswith(('0', '3', '200')):
        return stock_code + '.SZ'
    else:
        # 默认返回原值（让后续验证处理）
        return stock_code


@app.route('/api/grid/start', methods=['POST'])
@require_token
def start_grid_trading():
    """启动网格交易"""
    try:
        data = request.get_json()
        stock_code = data.get('stock_code')

        if not stock_code:
            return jsonify({'success': False, 'error': '缺少stock_code参数'}), 400

        # 标准化股票代码（自动补充市场后缀）
        stock_code = normalize_stock_code(stock_code)

        # 获取网格管理器
        position_manager = get_position_manager_instance()

        # DEBUG: 详细检查grid_manager状态
        pm_id = id(position_manager)
        # logger.info(f"[DEBUG] position_manager id: {pm_id}")
        # logger.info(f"[DEBUG] position_manager类型: {type(position_manager)}")
        # logger.info(f"[DEBUG] position_manager有grid_manager属性: {hasattr(position_manager, 'grid_manager')}")
        # logger.info(f"[DEBUG] grid_manager值: {position_manager.grid_manager}")
        # logger.info(f"[DEBUG] grid_manager类型: {type(position_manager.grid_manager) if position_manager.grid_manager else 'None'}")

        if not position_manager.grid_manager:
            # logger.error("[DEBUG] grid_manager为None，无法启动网格交易")
            # logger.error(f"[DEBUG] 检查position_manager.__dict__.keys(): {list(position_manager.__dict__.keys()) if hasattr(position_manager, '__dict__') else 'N/A'}")
            return jsonify({'success': False, 'error': '网格交易功能未启用'}), 400

        logger.info(f"[DEBUG] grid_manager检查通过，继续处理请求")

        # ⚠️ 新增: 获取风险等级参数
        risk_level = data.get('risk_level', 'moderate')  # 默认稳健型

        # 从嵌套的config对象中提取参数（兼容前端发送的数据结构）
        frontend_config = data.get('config', {})

        # DEBUG: 详细的请求数据日志
        logger.info(f"[DEBUG] 收到的原始data keys: {list(data.keys())}")
        logger.info(f"[DEBUG] frontend_config存在: {bool(frontend_config)}")
        if frontend_config:
            logger.info(f"[DEBUG] frontend_config keys: {list(frontend_config.keys())}")
            logger.info(f"[DEBUG] frontend_config内容: {frontend_config}")
        else:
            logger.warning("[DEBUG] frontend_config为空，将使用默认值")

        # 调试日志
        logger.info(f"启动网格交易请求: stock_code={stock_code}, risk_level={risk_level}, has_config={bool(frontend_config)}")
        if frontend_config:
            logger.debug(f"前端config参数: {frontend_config}")

        # ⚠️ 新增: 根据risk_level自动应用模板参数
        template_name_map = {
            'aggressive': '激进型网格',
            'moderate': '稳健型网格',
            'conservative': '保守型网格'
        }

        template_name = template_name_map.get(risk_level, '稳健型网格')
        db_manager = position_manager.db_manager
        template = db_manager.get_grid_template(template_name)

        if template:
            logger.info(f"应用风险模板: {template_name}, risk_level={risk_level}")
            # 模板参数作为默认值，用户自定义参数优先
            user_config = {
                'stock_code': stock_code,
                'center_price': data.get('center_price'),  # ⭐ 新增: 读取前端传入的中心价格
                'price_interval': frontend_config.get('price_interval') or data.get('price_interval') or template['price_interval'],
                'position_ratio': frontend_config.get('position_ratio') or data.get('position_ratio') or template['position_ratio'],
                'callback_ratio': frontend_config.get('callback_ratio') or data.get('callback_ratio') or template['callback_ratio'],
                'max_investment': frontend_config.get('max_investment') or data.get('max_investment'),  # 必需参数
                'max_deviation': frontend_config.get('max_deviation') or data.get('max_deviation') or template['max_deviation'],
                'target_profit': frontend_config.get('target_profit') or data.get('target_profit') or template['target_profit'],
                'stop_loss': frontend_config.get('stop_loss') or data.get('stop_loss') or template['stop_loss'],
                'duration_days': int(data.get('duration_days', template['duration_days']))
            }
        else:
            logger.warning(f"风险模板不存在: {template_name}, 使用用户自定义参数或默认值")
            # 用户配置（优先使用config对象中的值，否则使用顶层值）
            # 注意：前端发送的是百分比格式（已经除以100），直接使用
            user_config = {
                'stock_code': stock_code,
                'center_price': data.get('center_price'),  # ⭐ 新增: 读取前端传入的中心价格
                'price_interval': frontend_config.get('price_interval') or data.get('price_interval', config.GRID_DEFAULT_PRICE_INTERVAL),
                'position_ratio': frontend_config.get('position_ratio') or data.get('position_ratio', config.GRID_DEFAULT_POSITION_RATIO),
                'callback_ratio': frontend_config.get('callback_ratio') or data.get('callback_ratio', config.GRID_CALLBACK_RATIO),
                'max_investment': frontend_config.get('max_investment') or data.get('max_investment'),
                'max_deviation': frontend_config.get('max_deviation') or data.get('max_deviation', config.GRID_MAX_DEVIATION_RATIO),
                'target_profit': frontend_config.get('target_profit') or data.get('target_profit', config.GRID_TARGET_PROFIT_RATIO),
                'stop_loss': frontend_config.get('stop_loss') or data.get('stop_loss', config.GRID_STOP_LOSS_RATIO),
                'duration_days': int(data.get('duration_days', config.GRID_DEFAULT_DURATION_DAYS))
            }

        logger.debug(f"解析后的user_config: {user_config}")

        # DEBUG: 参数校验前日志
        logger.info(f"[DEBUG] 开始参数校验...")
        logger.info(f"[DEBUG] user_config['max_investment']: {user_config.get('max_investment')} (type: {type(user_config.get('max_investment'))})")

        # 参数校验
        is_valid, result = validate_grid_config(user_config)

        logger.info(f"[DEBUG] 校验结果: is_valid={is_valid}")
        if not is_valid:
            logger.error(f"[DEBUG] 参数校验失败，错误详情: {result}")
            return jsonify({
                'success': False,
                'error': '参数校验失败',
                'details': result
            }), 400

        logger.info(f"[DEBUG] 参数校验通过，validated_config: {result}")

        # 检查是否有旧session(用于返回警告消息)
        grid_manager = position_manager.grid_manager
        old_session = grid_manager.sessions.get(grid_manager._normalize_code(stock_code))
        had_old_session = old_session is not None
        old_session_id = old_session.id if old_session else None

        # 启动网格会话（从校验后的数据中移除stock_code）
        validated_config = {k: v for k, v in result.items() if k != 'stock_code'}
        session = grid_manager.start_grid_session(stock_code, validated_config)

        # 触发前端数据更新
        position_manager._increment_data_version()

        return jsonify({
            'success': True,
            'session_id': session.id,
            'risk_level': risk_level,  # ⚠️ 新增: 返回风险等级
            'template_name': template_name if template else None,  # ⚠️ 新增: 返回模板名称
            'warning': '已自动停止旧的网格会话' if had_old_session else None,
            'old_session_id': old_session_id,
            'message': f'网格交易会话启动成功 ({template_name if template else "自定义配置"}, ID: {session.id})',
            'config': {
                'stock_code': session.stock_code,
                'center_price': session.center_price,
                'price_interval': session.price_interval,
                'position_ratio': session.position_ratio,
                'callback_ratio': session.callback_ratio,
                'max_investment': session.max_investment,
                'max_deviation': session.max_deviation,
                'target_profit': session.target_profit,
                'stop_loss': session.stop_loss,
                'duration_days': (session.end_time - session.start_time).days
            }
        })

    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logger.error(f"启动网格交易失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/grid/stop/<int:session_id>', methods=['POST'])
@require_token
def stop_grid_trading(session_id):
    """停止网格交易(通过session_id)"""
    try:
        position_manager = get_position_manager_instance()
        if not position_manager.grid_manager:
            return jsonify({'success': False, 'error': '网格交易功能未启用'}), 400

        # 停止网格会话
        final_stats = position_manager.grid_manager.stop_grid_session(session_id, 'manual')

        # 触发前端数据更新
        position_manager._increment_data_version()

        return jsonify({
            'success': True,
            'final_stats': final_stats,
            'message': f'网格交易会话已停止 (ID: {session_id})'
        })

    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 404
    except Exception as e:
        logger.error(f"停止网格交易失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/grid/stop', methods=['POST'])
@require_token
def stop_grid_trading_flexible():
    """
    停止网格交易(支持通过session_id或stock_code)

    请求体:
    {
        "session_id": 123  # 或者
        "stock_code": "000001.SZ"
    }
    """
    try:
        data = request.get_json()
        session_id = data.get('session_id')
        stock_code = data.get('stock_code')

        position_manager = get_position_manager_instance()
        if not position_manager.grid_manager:
            return jsonify({'success': False, 'error': '网格交易功能未启用'}), 400

        grid_manager = position_manager.grid_manager

        # 如果提供stock_code,查找对应的session_id
        if not session_id and stock_code:
            session = grid_manager.sessions.get(grid_manager._normalize_code(stock_code))
            if not session:
                return jsonify({
                    'success': False,
                    'error': 'session_not_found',
                    'message': f'{stock_code}没有活跃的网格会话'
                }), 404
            session_id = session.id

        if not session_id:
            return jsonify({
                'success': False,
                'error': 'missing_parameter',
                'message': '必须提供session_id或stock_code'
            }), 400

        # 停止会话
        stats = grid_manager.stop_grid_session(session_id, 'manual_stop')

        # 触发前端数据更新
        position_manager._increment_data_version()

        return jsonify({
            'success': True,
            'stats': stats,
            'message': f'网格交易会话已停止 (ID: {session_id})'
        })

    except ValueError as e:
        return jsonify({
            'success': False,
            'error': 'session_not_found',
            'message': str(e)
        }), 404
    except Exception as e:
        logger.error(f"[API] stop_grid_session失败: {str(e)}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'server_error',
            'message': str(e)
        }), 500


# ======================= 新增:网格交易Web配置对话框专用API =======================

@app.route('/api/grid/session/<stock_code>', methods=['GET'])
def get_grid_session_status(stock_code):
    """
    查询指定股票的网格交易会话状态(供Web配置对话框使用)

    返回:
        - 如果有活跃session: 返回完整配置
        - 如果无session: 返回默认配置模板
    """
    try:
        position_manager = get_position_manager_instance()

        # ⭐ 确保grid_manager已初始化(用于Web界面重启的情况)
        if not position_manager.grid_manager and config.ENABLE_GRID_TRADING:
            try:
                position_manager.init_grid_manager(trading_executor)
                logger.info("[API] 已在API调用中初始化grid_manager")
            except Exception as e:
                logger.error(f"[API] 初始化grid_manager失败: {str(e)}")
                return jsonify({'success': False, 'error': '网格交易功能初始化失败'}), 500

        if not position_manager.grid_manager:
            return jsonify({'success': False, 'error': '网格交易功能未启用'}), 400

        grid_manager = position_manager.grid_manager

        # 标准化股票代码(自动补充市场后缀)
        stock_code = normalize_stock_code(stock_code)
        # logger.info(f"[API] 查询网格会话状态: stock_code={stock_code}")

        # 从内存中查询活跃会话（sessions dict key 为无后缀代码）
        session = grid_manager.sessions.get(grid_manager._normalize_code(stock_code))

        if session and session.status == 'active':
            # ⭐ 添加调试日志：检查session对象的实际值
            # logger.info(f"[API] 找到活跃session: id={session.id}, stock_code={session.stock_code}")
            # logger.info(f"[API] session配置值: price_interval={session.price_interval} ({session.price_interval*100:.1f}%), "
            #            f"position_ratio={session.position_ratio} ({session.position_ratio*100:.1f}%), "
            #            f"stop_loss={session.stop_loss} ({session.stop_loss*100:.1f}%)")

            # ⚠️ 新增: 从数据库获取risk_level (内存GridSession对象没有此字段)
            db_session = position_manager.db_manager.get_grid_session_by_stock(stock_code)
            risk_level = db_session.get('risk_level', 'moderate') if db_session else 'moderate'
            template_name = db_session.get('template_name') if db_session else None

            # 返回现有配置(小数格式，前端会乘以100显示)
            return jsonify({
                'success': True,
                'has_session': True,
                'session_id': session.id,
                'risk_level': risk_level,  # ⚠️ 新增
                'template_name': template_name,  # ⚠️ 新增
                'config': {
                    'center_price': session.center_price,  # ⭐ 新增: 中心价格，用于前端回显
                    'price_interval': session.price_interval,  # ⭐ 小数格式，前端乘以100显示
                    'position_ratio': session.position_ratio,
                    'callback_ratio': session.callback_ratio,
                    'max_investment': session.max_investment,
                    'duration_days': (session.end_time - datetime.now()).days,
                    'max_deviation': session.max_deviation,
                    'target_profit': session.target_profit,
                    'stop_loss': session.stop_loss
                },
                'stats': {
                    'center_price': session.center_price,
                    'current_center_price': session.current_center_price,
                    'trade_count': session.trade_count,
                    'buy_count': session.buy_count,
                    'sell_count': session.sell_count,
                    'profit_ratio': session.get_profit_ratio() * 100,
                    'current_investment': session.current_investment
                }
            })
        else:
            # 返回默认配置(百分比格式)
            # ⭐ 计算当前股票的持仓市值，用于计算max_investment（当前持仓的一半）
            try:
                # ⚡ 性能优化：直接从内存数据库查询，避免调用 get_all_positions()
                # 这样不会触发QMT API调用，响应时间从2-5秒降低到<50ms
                stock_market_value = 0

                # 提取基础股票代码（去除 .SH/.SZ 等后缀）
                base_stock_code = stock_code.split('.')[0]

                # 直接查询内存数据库（超快）
                query = f"""
                    SELECT market_value, volume, current_price, cost_price
                    FROM positions
                    WHERE stock_code = ? OR stock_code = ?
                """

                cursor = position_manager.memory_conn.cursor()
                cursor.execute(query, (stock_code, base_stock_code))
                results = cursor.fetchall()

                # 累加所有匹配记录的市值（同一股票可能有多条记录）
                for row in results:
                    market_value, volume, current_price, cost_price = row
                    if market_value:
                        stock_market_value += float(market_value)

                # logger.info(f"[API] {stock_code}当前持仓市值: {stock_market_value:.2f}元 (内存数据库查询)")

                # ⭐ max_investment = 当前持仓市值的一半
                if stock_market_value and stock_market_value > 0:
                    max_investment = stock_market_value * config.GRID_DEFAULT_MAX_INVESTMENT_RATIO
                    # logger.info(f"[API] {stock_code} max_investment计算: {stock_market_value:.2f} * {config.GRID_DEFAULT_MAX_INVESTMENT_RATIO} = {max_investment:.2f}元")
                else:
                    max_investment = 10000  # 无持仓时使用固定默认值
                    # logger.info(f"[API] {stock_code}无持仓或市值为0，使用固定默认值: {max_investment}元")

            except Exception as e:
                logger.warning(f"[API] 计算{stock_code}的max_investment失败: {str(e)},使用固定默认值")
                max_investment = 10000  # 降级到固定默认值

            return jsonify({
                'success': True,
                'has_session': False,
                'config': {
                    'price_interval': config.GRID_DEFAULT_PRICE_INTERVAL,  # ⭐ 小数格式，前端乘以100显示
                    'position_ratio': config.GRID_DEFAULT_POSITION_RATIO,
                    'callback_ratio': config.GRID_CALLBACK_RATIO,
                    'max_investment': max_investment,
                    'duration_days': config.GRID_DEFAULT_DURATION_DAYS,
                    'max_deviation': config.GRID_MAX_DEVIATION_RATIO,
                    'target_profit': config.GRID_TARGET_PROFIT_RATIO,
                    'stop_loss': config.GRID_STOP_LOSS_RATIO
                }
            })
    except Exception as e:
        logger.error(f"[API] get_grid_session_status失败: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ======================= 网格交易会话管理API =======================

@app.route('/api/grid/sessions', methods=['GET'])
def get_grid_sessions():
    """获取所有网格会话(包括stopped状态)

    优化: 返回所有会话,包括内存中的active sessions和数据库中的stopped sessions
    """
    try:
        position_manager = get_position_manager_instance()
        if not position_manager.grid_manager:
            # 返回200和空列表，而不是400错误
            # 这符合RESTful最佳实践："没有数据"不是错误
            return jsonify({
                'success': True,
                'sessions': [],
                'total': 0,
                'message': '网格交易功能未启用'
            })

        sessions = []

        # 1. 从内存获取active sessions
        for stock_code, session in position_manager.grid_manager.sessions.items():
            sessions.append({
                'session_id': session.id,
                'stock_code': session.stock_code,
                'status': session.status,
                'center_price': session.center_price,
                'current_center_price': session.current_center_price,
                'trade_count': session.trade_count,
                'buy_count': session.buy_count,
                'sell_count': session.sell_count,
                'profit_ratio': session.get_profit_ratio(),
                'deviation_ratio': session.get_deviation_ratio(),
                'start_time': session.start_time.isoformat() if session.start_time else None,
                'end_time': session.end_time.isoformat() if session.end_time else None,
                'stop_time': session.stop_time.isoformat() if session.stop_time else None,
                'stop_reason': session.stop_reason
            })

        # 2. 从数据库获取所有sessions(避免重复添加active sessions)
        db_sessions = position_manager.grid_manager.db.get_all_grid_sessions()
        for session_data in db_sessions:
            # 检查是否已在列表中(避免重复)
            if not any(s['session_id'] == session_data['id'] for s in sessions):
                # 将sqlite3.Row转换为字典以支持.get()方法
                session_dict = dict(session_data)
                sessions.append({
                    'session_id': session_dict['id'],
                    'stock_code': session_dict['stock_code'],
                    'status': session_dict['status'],
                    'center_price': session_dict['center_price'],
                    'current_center_price': session_dict['current_center_price'],
                    'trade_count': session_dict['trade_count'],
                    'buy_count': session_dict['buy_count'],
                    'sell_count': session_dict['sell_count'],
                    # 计算盈亏率
                    'profit_ratio': (session_dict['total_sell_amount'] - session_dict['total_buy_amount']) / session_dict.get('max_investment', 0) if session_dict.get('max_investment', 0) > 0 else 0,
                    # 计算偏离度
                    'deviation_ratio': abs(session_dict['current_center_price'] - session_dict['center_price']) / session_dict['center_price'] if session_dict['center_price'] > 0 else 0,
                    'start_time': session_dict['start_time'],
                    'end_time': session_dict['end_time'],
                    'stop_time': session_dict.get('stop_time'),
                    'stop_reason': session_dict.get('stop_reason')
                })

        return jsonify({
            'success': True,
            'sessions': sessions,
            'total': len(sessions)
        })

    except Exception as e:
        logger.error(f"获取网格会话失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/grid/session/<int:session_id>', methods=['GET'])
def get_grid_session_detail(session_id):
    """获取网格会话详情"""
    try:
        # ⭐ 如果session_id看起来像股票代码（6位数字），转发到股票代码处理逻辑
        # 检查原始URL路径，因为Flask会将000001转换为整数1
        from flask import request
        path = request.path.split('/')[-1]  # 获取URL最后一部分

        # 如果原始路径是6位数字，说明这是股票代码
        if len(path) == 6 and path.isdigit():
            return get_grid_session_status(path)

        # 检查转换后的整数是否在股票代码范围（用于不带前导零的情况）
        if 100000 <= session_id <= 999999:
            stock_code = str(session_id)
            return get_grid_session_status(stock_code)

        position_manager = get_position_manager_instance()
        if not position_manager.grid_manager:
            return jsonify({'success': False, 'error': '网格交易功能未启用'}), 400

        # 查找会话
        session = None
        for s in position_manager.grid_manager.sessions.values():
            if s.id == session_id:
                session = s
                break

        if not session:
            return jsonify({'success': False, 'error': f'会话{session_id}不存在'}), 404

        # 获取追踪器状态
        tracker = position_manager.grid_manager.trackers.get(session_id)
        tracker_state = None
        if tracker:
            tracker_state = {
                'last_price': tracker.last_price,
                'peak_price': tracker.peak_price,
                'valley_price': tracker.valley_price,
                'direction': tracker.direction,
                'crossed_level': tracker.crossed_level,
                'waiting_callback': tracker.waiting_callback
            }

        # 获取网格档位
        levels = session.get_grid_levels()

        return jsonify({
            'success': True,
            'session': {
                'id': session.id,
                'stock_code': session.stock_code,
                'status': session.status,
                'center_price': session.center_price,
                'current_center_price': session.current_center_price,
                'price_interval': session.price_interval,
                'position_ratio': session.position_ratio,
                'callback_ratio': session.callback_ratio,
                'max_investment': session.max_investment,
                'current_investment': session.current_investment,
                'max_deviation': session.max_deviation,
                'target_profit': session.target_profit,
                'stop_loss': session.stop_loss,
                'trade_count': session.trade_count,
                'buy_count': session.buy_count,
                'sell_count': session.sell_count,
                'total_buy_amount': session.total_buy_amount,
                'total_sell_amount': session.total_sell_amount,
                'profit_ratio': session.get_profit_ratio(),
                'deviation_ratio': session.get_deviation_ratio(),
                'start_time': session.start_time.isoformat() if session.start_time else None,
                'end_time': session.end_time.isoformat() if session.end_time else None,
                'grid_levels': levels,
                'tracker_state': tracker_state
            }
        })

    except Exception as e:
        logger.error(f"获取网格会话详情失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/grid/trades/<int:session_id>', methods=['GET'])
def get_grid_trades(session_id):
    """获取网格交易历史"""
    try:
        position_manager = get_position_manager_instance()
        if not position_manager.grid_manager:
            return jsonify({'success': False, 'error': '网格交易功能未启用'}), 400

        # 获取分页参数
        limit = request.args.get('limit', 50, type=int)
        offset = request.args.get('offset', 0, type=int)

        # 获取交易记录
        trades = position_manager.grid_manager.db.get_grid_trades(session_id, limit, offset)
        total_count = position_manager.grid_manager.db.get_grid_trade_count(session_id)

        return jsonify({
            'success': True,
            'trades': trades,
            'total_count': total_count,
            'pagination': {
                'limit': limit,
                'offset': offset,
                'has_more': offset + len(trades) < total_count
            }
        })

    except Exception as e:
        logger.error(f"获取网格交易历史失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/grid/status/<stock_code>', methods=['GET'])
def get_grid_status(stock_code):
    """获取网格实时状态"""
    try:
        position_manager = get_position_manager_instance()
        if not position_manager.grid_manager:
            return jsonify({'success': False, 'error': '网格交易功能未启用'}), 400

        # 检查是否有活跃会话
        session = position_manager.grid_manager.sessions.get(
            position_manager.grid_manager._normalize_code(stock_code)
        )

        if not session:
            return jsonify({
                'success': True,
                'is_active': False,
                'stock_code': stock_code
            })

        # 获取追踪器状态
        tracker = position_manager.grid_manager.trackers.get(session.id)
        tracker_state = None
        if tracker:
            tracker_state = {
                'last_price': tracker.last_price,
                'peak_price': tracker.peak_price,
                'valley_price': tracker.valley_price,
                'direction': tracker.direction,
                'waiting_callback': tracker.waiting_callback
            }

        # 获取网格档位
        levels = session.get_grid_levels()

        return jsonify({
            'success': True,
            'is_active': True,
            'stock_code': stock_code,
            'session_id': session.id,
            'current_center_price': session.current_center_price,
            'grid_levels': levels,
            'tracker_state': tracker_state,
            'stats': {
                'trade_count': session.trade_count,
                'buy_count': session.buy_count,
                'sell_count': session.sell_count,
                'profit_ratio': session.get_profit_ratio(),
                'deviation_ratio': session.get_deviation_ratio()
            }
        })

    except Exception as e:
        logger.error(f"获取网格状态失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/grid/templates', methods=['GET'])
def get_grid_templates():
    """获取所有网格配置模板"""
    try:
        position_manager = get_position_manager_instance()
        if not position_manager.db_manager:
            return jsonify({'success': False, 'error': '网格交易功能未启用'}), 400

        templates = position_manager.db_manager.get_all_grid_templates()

        return jsonify({
            'success': True,
            'templates': templates,
            'total': len(templates)
        })

    except Exception as e:
        logger.error(f"获取网格配置模板失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/grid/template/<template_name>', methods=['GET'])
def get_grid_template(template_name):
    """获取指定网格配置模板"""
    try:
        position_manager = get_position_manager_instance()
        if not position_manager.db_manager:
            return jsonify({'success': False, 'error': '网格交易功能未启用'}), 400

        template = position_manager.db_manager.get_grid_template(template_name)

        if not template:
            return jsonify({'success': False, 'error': f'模板{template_name}不存在'}), 404

        return jsonify({
            'success': True,
            'template': template
        })

    except Exception as e:
        logger.error(f"获取网格配置模板失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/grid/template/save', methods=['POST'])
@require_token
def save_grid_template():
    """保存网格配置模板"""
    try:
        data = request.get_json()
        template_name = data.get('template_name')

        if not template_name:
            return jsonify({'success': False, 'error': '缺少template_name参数'}), 400

        position_manager = get_position_manager_instance()
        if not position_manager.db_manager:
            return jsonify({'success': False, 'error': '网格交易功能未启用'}), 400

        # 准备模板数据
        template_data = {
            'template_name': template_name,
            'price_interval': data.get('price_interval', 0.05),
            'position_ratio': data.get('position_ratio', 0.25),
            'callback_ratio': data.get('callback_ratio', 0.005),
            'max_deviation': data.get('max_deviation', 0.15),
            'target_profit': data.get('target_profit', 0.10),
            'stop_loss': data.get('stop_loss', -0.10),
            'duration_days': data.get('duration_days', 7),
            'max_investment_ratio': data.get('max_investment_ratio', 0.5),
            'description': data.get('description', ''),
            'is_default': data.get('is_default', False)
        }

        # 参数校验
        is_valid, result = validate_grid_template(template_data)
        if not is_valid:
            return jsonify({
                'success': False,
                'error': '参数校验失败',
                'details': result
            }), 400

        # 保存模板（使用校验后的数据）
        template_id = position_manager.db_manager.save_grid_template(result)

        return jsonify({
            'success': True,
            'template_id': template_id,
            'message': f'模板{template_name}保存成功'
        })

    except Exception as e:
        logger.error(f"保存网格配置模板失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/grid/template/<template_name>', methods=['DELETE'])
def delete_grid_template(template_name):
    """删除网格配置模板"""
    try:
        position_manager = get_position_manager_instance()
        if not position_manager.db_manager:
            return jsonify({'success': False, 'error': '网格交易功能未启用'}), 400

        position_manager.db_manager.delete_grid_template(template_name)

        return jsonify({
            'success': True,
            'message': f'模板{template_name}删除成功'
        })

    except Exception as e:
        logger.error(f"删除网格配置模板失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/grid/template/use', methods=['POST'])
@require_token
def use_grid_template():
    """使用模板（更新使用统计）"""
    try:
        data = request.get_json()
        template_name = data.get('template_name')

        if not template_name:
            return jsonify({'success': False, 'error': '缺少template_name参数'}), 400

        position_manager = get_position_manager_instance()
        if not position_manager.db_manager:
            return jsonify({'success': False, 'error': '网格交易功能未启用'}), 400

        # 更新使用统计
        position_manager.db_manager.increment_template_usage(template_name)

        # 返回模板配置
        template = position_manager.db_manager.get_grid_template(template_name)

        return jsonify({
            'success': True,
            'template': template,
            'message': f'模板{template_name}已应用'
        })

    except Exception as e:
        logger.error(f"使用网格配置模板失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/grid/template/default', methods=['GET'])
def get_default_grid_template():
    """获取默认网格配置模板"""
    try:
        position_manager = get_position_manager_instance()
        if not position_manager.db_manager:
            return jsonify({'success': False, 'error': '网格交易功能未启用'}), 400

        template = position_manager.db_manager.get_default_grid_template()

        if not template:
            return jsonify({
                'success': True,
                'template': None,
                'message': '未设置默认模板'
            })

        return jsonify({
            'success': True,
            'template': template
        })

    except Exception as e:
        logger.error(f"获取默认网格配置模板失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/grid/template/<template_name>/default', methods=['PUT'])
def set_default_grid_template(template_name):
    """设置默认网格配置模板"""
    try:
        position_manager = get_position_manager_instance()
        if not position_manager.db_manager:
            return jsonify({'success': False, 'error': '网格交易功能未启用'}), 400

        # 检查模板是否存在
        template = position_manager.db_manager.get_grid_template(template_name)
        if not template:
            return jsonify({'success': False, 'error': f'模板{template_name}不存在'}), 404

        # 设置为默认模板（通过更新is_default字段）
        template['is_default'] = True
        position_manager.db_manager.save_grid_template(template)

        return jsonify({
            'success': True,
            'message': f'已将{template_name}设置为默认模板'
        })

    except Exception as e:
        logger.error(f"设置默认网格配置模板失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== ⚠️ 新增接口: 获取风险等级模板 ====================
@app.route('/api/grid/risk-templates', methods=['GET'])
def get_risk_level_templates():
    """
    获取三档风险等级模板

    返回格式:
    {
        "success": true,
        "templates": {
            "aggressive": { /* 激进型配置 */ },
            "moderate": { /* 稳健型配置 */ },
            "conservative": { /* 保守型配置 */ }
        }
    }
    """
    try:
        position_manager = get_position_manager_instance()
        if not position_manager.db_manager:
            return jsonify({'success': False, 'error': '网格交易功能未启用'}), 400

        db_manager = position_manager.db_manager

        # 获取三档模板
        templates = {
            'aggressive': db_manager.get_grid_template('激进型网格'),
            'moderate': db_manager.get_grid_template('稳健型网格'),
            'conservative': db_manager.get_grid_template('保守型网格')
        }

        # 简化返回数据，移除不必要的字段
        simplified_templates = {}
        for key, template in templates.items():
            if template:
                simplified_templates[key] = {
                    'template_name': template['template_name'],
                    'price_interval': template['price_interval'],
                    'position_ratio': template['position_ratio'],
                    'callback_ratio': template['callback_ratio'],
                    'max_deviation': template['max_deviation'],
                    'target_profit': template['target_profit'],
                    'stop_loss': template['stop_loss'],
                    'duration_days': template['duration_days'],
                    'max_investment_ratio': template.get('max_investment_ratio', 0.5),
                    'description': template.get('description', '')
                }
            else:
                logger.warning(f"风险模板不存在: {key}")
                simplified_templates[key] = None

        logger.info(f"[API] 获取风险模板成功，返回 {len([t for t in simplified_templates.values() if t])} 个模板")

        return jsonify({
            'success': True,
            'templates': simplified_templates
        })

    except Exception as e:
        logger.error(f"获取风险模板失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/grid/config', methods=['GET'])
def get_grid_config():
    """获取网格交易默认配置"""
    try:
        # 获取持仓总市值
        position_manager = get_position_manager_instance()
        positions = position_manager.get_all_positions()
        total_market_value = 0
        if not positions.empty:
            for _, pos in positions.iterrows():
                market_value = pos.get('market_value', 0)
                if market_value:
                    total_market_value += float(market_value)

        # 获取默认配置
        default_config = config.get_grid_default_config(total_market_value)

        return jsonify({
            'status': 'success',
            'data': default_config
        })

    except Exception as e:
        logger.error(f"获取网格配置失败: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': f"获取网格配置失败: {str(e)}"
        }), 500


# ======================= 新增: 独立的checkbox状态API =======================

@app.route('/api/grid/checkbox-states', methods=['GET'])
def get_grid_checkbox_states():
    """
    获取所有股票的网格交易checkbox状态（独立于持仓数据）

    返回格式:
    {
        "success": true,
        "states": {
            "000001.SZ": {"active": true, "session_id": 123},
            "600036.SH": {"active": false, "session_id": null}
        },
        "version": 12345  # 数据版本号，用于前端判断是否需要更新
    }
    """
    try:
        position_manager = get_position_manager_instance()

        # 如果grid_manager未初始化，返回空状态
        if not position_manager.grid_manager:
            return jsonify({
                'success': True,
                'states': {},
                'version': position_manager.data_version
            })

        grid_manager = position_manager.grid_manager

        # 构建checkbox状态字典
        checkbox_states = {}

        # 遍历所有活跃的网格session
        for stock_code, session in grid_manager.sessions.items():
            checkbox_states[stock_code] = {
                'active': (session.status == 'active'),
                'session_id': session.id if session.status == 'active' else None
            }

        # 可选：也包含持仓中但没有网格session的股票
        stock_codes = request.args.get('stock_codes')  # 前端可以传入需要查询的股票列表
        if stock_codes:
            stock_list = stock_codes.split(',')
            for stock_code in stock_list:
                stock_code = stock_code.strip()
                if stock_code not in checkbox_states:
                    checkbox_states[stock_code] = {
                        'active': False,
                        'session_id': None
                    }

        return jsonify({
            'success': True,
            'states': checkbox_states,
            'version': position_manager.data_version
        })

    except Exception as e:
        logger.error(f"获取checkbox状态失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/grid/checkbox-state/<stock_code>', methods=['GET'])
def get_single_grid_checkbox_state(stock_code):
    """
    获取单个股票的网格交易checkbox状态（独立于持仓数据）

    返回格式:
    {
        "success": true,
        "stock_code": "000001.SZ",
        "active": true,
        "session_id": 123,
        "version": 12345
    }
    """
    try:
        position_manager = get_position_manager_instance()

        # 标准化股票代码
        stock_code = normalize_stock_code(stock_code)

        # 如果grid_manager未初始化，返回inactive状态
        if not position_manager.grid_manager:
            return jsonify({
                'success': True,
                'stock_code': stock_code,
                'active': False,
                'session_id': None,
                'version': position_manager.data_version
            })

        grid_manager = position_manager.grid_manager

        # 检查是否有活跃session
        session = grid_manager.sessions.get(grid_manager._normalize_code(stock_code))
        active = (session is not None and session.status == 'active')
        session_id = session.id if active else None

        return jsonify({
            'success': True,
            'stock_code': stock_code,
            'active': active,
            'session_id': session_id,
            'version': position_manager.data_version
        })

    except Exception as e:
        logger.error(f"获取{stock_code}的checkbox状态失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ======================= 网格交易API端点结束 =======================

def shutdown_web_server():
    """关闭Web服务器并清理资源"""
    global stop_push_flag, api_executor, push_thread

    logger.info("正在关闭Web服务器...")

    try:
        # 停止推送线程
        stop_push_flag = True
        logger.info("已设置停止标志，等待推送线程结束...")

        # ⚠️ 修复: 等待推送线程真正结束（最多5秒）
        if push_thread and push_thread.is_alive():
            push_thread.join(timeout=5.0)
            if push_thread.is_alive():
                logger.warning("推送线程未在5秒内结束，继续关闭")
            else:
                logger.info("已停止推送线程")
    except Exception as e:
        logger.error(f"停止推送线程失败: {str(e)}")

    try:
        # 关闭线程池
        if api_executor:
            api_executor.shutdown(wait=False, cancel_futures=True)
            logger.info("已关闭API线程池")
    except Exception as e:
        logger.error(f"关闭API线程池失败: {str(e)}")

    logger.info("Web服务器已关闭")

def start_web_server(position_manager=None):
    """启动Web服务器

    Args:
        position_manager: 已初始化的position_manager实例（从main.py传入）
    """
    logger.info("正在启动Web服务器...")

    # 设置position_manager实例（如果提供了）
    if position_manager is not None:
        set_position_manager(position_manager)
        # logger.info(f"[DEBUG] start_web_server: 已设置position_manager id={id(position_manager)}")
    else:
        logger.warning("[DEBUG] start_web_server: 未提供position_manager参数")

    #  20251219新增: 启动时同步配置状态
    sync_auto_trading_status()

    start_push_thread()

    # 禁用Flask默认的Werkzeug访问日志（使用自定义中间件）
    import logging
    werkzeug_logger = logging.getLogger('werkzeug')
    if config.ENABLE_WEB_ACCESS_LOG:
        # 如果启用了自定义访问日志，禁用Werkzeug的访问日志
        werkzeug_logger.setLevel(logging.ERROR)

    app.run(host=config.WEB_SERVER_HOST, port=config.WEB_SERVER_PORT, debug=config.WEB_SERVER_DEBUG, use_reloader=False)

if __name__ == '__main__':
     start_web_server()
