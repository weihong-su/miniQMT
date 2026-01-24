# -*- coding: utf-8 -*-

"""
Web服务模块，提供RESTful API接口与前端交互
"""
import os
import time
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
from position_manager import get_position_manager
from trading_executor import get_trading_executor
from strategy import get_trading_strategy
from config_manager import get_config_manager
import utils


# 获取logger
logger = get_logger("web_server")
webpage_dir = 'web1.0'

# 创建Flask应用
app = Flask(__name__, static_folder=webpage_dir, static_url_path='')

# 允许跨域请求
CORS(app)

# 获取各个模块的实例
data_manager = get_data_manager()
indicator_calculator = get_indicator_calculator()
position_manager = get_position_manager()
trading_executor = get_trading_executor()
trading_strategy = get_trading_strategy()
config_manager = get_config_manager()

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
        # 直接检查对象存在性,不调用任何QMT API避免阻塞
        is_connected = False
        if hasattr(position_manager, 'qmt_trader') and position_manager.qmt_trader:
            if hasattr(position_manager.qmt_trader, 'xt_trader') and position_manager.qmt_trader.xt_trader:
                # xt_trader对象存在即认为已连接,不调用任何方法
                is_connected = True

        return jsonify({
            'status': 'success',
            'connected': bool(is_connected),
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
        # 从 position_manager 获取账户信息(使用超时保护)
        def get_account_data():
            return position_manager.get_account_info() or {}

        timeout_seconds = config.MONITOR_CALL_TIMEOUT if hasattr(config, 'MONITOR_CALL_TIMEOUT') else 5.0
        future = api_executor.submit(get_account_data)

        try:
            account_info = future.result(timeout=timeout_seconds)
        except FuturesTimeoutError:
            logger.warning(f"获取账户信息超时({timeout_seconds}秒),使用默认值")
            account_info = {}
        
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

        # 获取所有持仓数据
        positions_all_df = position_manager.get_all_positions_with_all_fields()
        # 修复NaN序列化问题: 将NaN替换为None以生成有效的JSON
        positions_all_df = positions_all_df.replace({pd.NA: None, float('nan'): None})
        realtime_data['positions_all'] = positions_all_df.to_dict('records')

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
            value = float(config_data["firstProfitSell"]) / 100
            config.INITIAL_TAKE_PROFIT_RATIO = value
            db_configs['INITIAL_TAKE_PROFIT_RATIO'] = value

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
            ratio = 1 - float(config_data["stopLossBuy"]) / 100
            config.BUY_GRID_LEVELS[1] = ratio
            db_configs['BUY_GRID_LEVEL_1'] = ratio

        if "stockStopLoss" in config_data:
            value = -float(config_data["stockStopLoss"]) / 100
            config.STOP_LOSS_RATIO = value
            db_configs['STOP_LOSS_RATIO'] = value

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

        # 处理模拟交易模式切换
        if "simulationMode" in config_data:
            old_simulation_mode = getattr(config, 'ENABLE_SIMULATION_MODE', False)
            new_simulation_mode = bool(config_data["simulationMode"])
            # 注意：模拟交易模式不持久化，避免误切换到实盘模式

            # 如果模式发生变化
            if old_simulation_mode != new_simulation_mode:
                setattr(config, 'ENABLE_SIMULATION_MODE', new_simulation_mode)

                # 模式变化时重新初始化内存数据库
                position_manager = get_position_manager()
                # 创建新的内存连接
                position_manager.memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
                position_manager._create_memory_table()
                position_manager._sync_db_to_memory()  # 从SQLite重新加载数据

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
def api_initialize_positions():
    """初始化持仓数据的API端点"""
    try:
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
        
        result = position_manager.initialize_all_positions_data()
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
def update_holding_params():
    """更新持仓参数"""
    try:
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
    """提供Server-Sent Events流 - 增强版"""
    def event_stream():
        last_positions_version = 0
        prev_data = None
        
        while True:
            try:
                # 检查持仓数据是否有变化
                version_info = position_manager.get_data_version_info()
                current_version = version_info['version']
                data_changed = version_info['changed']
                
                # 获取基础数据
                account_info = position_manager.get_account_info() or {}
                
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
                
                # 如果持仓数据有变化，添加持仓更新通知
                if current_version > last_positions_version:
                    current_data['positions_update'] = {
                        'version': current_version,
                        'changed': True
                    }
                    last_positions_version = current_version
                    logger.debug(f"SSE推送持仓数据变化通知: v{current_version}")
                
                # 只在数据变化时发送更新
                if current_data != prev_data:
                    yield f"data: {json.dumps(current_data)}\n\n"
                    prev_data = current_data
                    
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
        # 获取客户端版本号
        client_version = request.args.get('version', 0, type=int)
        
        # 获取当前数据版本
        version_info = position_manager.get_data_version_info()
        current_version = version_info['version']
        
        # 如果客户端版本是最新的，返回无变化
        if client_version >= current_version:
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
    global stop_push_flag

    while not stop_push_flag:
        try:
            # 只在交易时间更新数据
            if config.is_trade_time():
                # 更新所有持仓的最新价格
                position_manager.update_all_positions_price()

                # 获取所有持仓数据
                positions_all_df = position_manager.get_all_positions_with_all_fields()

                # 处理NaN值
                positions_all_df = positions_all_df.replace({pd.NA: None, float('nan'): None})

                # 更新实时数据
                realtime_data['positions_all'] = positions_all_df.to_dict('records')

            # 休眠间隔
            time.sleep(3)
        except Exception as e:
            logger.error(f"推送实时数据时出错: {str(e)}")
            time.sleep(3)  # 出错后休眠


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
    """🟢 20251219修复: Web服务器启动时同步ENABLE_AUTO_TRADING状态

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

@app.route('/api/grid/start', methods=['POST'])
def start_grid_trading():
    """启动网格交易"""
    try:
        data = request.get_json()
        stock_code = data.get('stock_code')

        if not stock_code:
            return jsonify({'success': False, 'error': '缺少stock_code参数'}), 400

        # 获取网格管理器
        position_manager = get_position_manager()
        if not position_manager.grid_manager:
            return jsonify({'success': False, 'error': '网格交易功能未启用'}), 400

        # 用户配置
        user_config = {
            'price_interval': data.get('price_interval', config.GRID_DEFAULT_PRICE_INTERVAL),
            'position_ratio': data.get('position_ratio', config.GRID_DEFAULT_POSITION_RATIO),
            'callback_ratio': data.get('callback_ratio', config.GRID_CALLBACK_RATIO),
            'max_investment': data.get('max_investment'),
            'max_deviation': data.get('max_deviation', config.GRID_MAX_DEVIATION_RATIO),
            'target_profit': data.get('target_profit', config.GRID_TARGET_PROFIT_RATIO),
            'stop_loss': data.get('stop_loss', config.GRID_STOP_LOSS_RATIO),
            'duration_days': data.get('duration_days', config.GRID_DEFAULT_DURATION_DAYS)
        }

        # 启动网格会话
        session = position_manager.grid_manager.start_grid_session(stock_code, user_config)

        return jsonify({
            'success': True,
            'session_id': session.id,
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
def stop_grid_trading(session_id):
    """停止网格交易"""
    try:
        position_manager = get_position_manager()
        if not position_manager.grid_manager:
            return jsonify({'success': False, 'error': '网格交易功能未启用'}), 400

        # 停止网格会话
        final_stats = position_manager.grid_manager.stop_grid_session(session_id, 'manual')

        return jsonify({
            'success': True,
            'final_stats': final_stats
        })

    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 404
    except Exception as e:
        logger.error(f"停止网格交易失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/grid/sessions', methods=['GET'])
def get_grid_sessions():
    """获取所有网格会话"""
    try:
        position_manager = get_position_manager()
        if not position_manager.grid_manager:
            return jsonify({'success': False, 'error': '网格交易功能未启用'}), 400

        sessions = []
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
                'end_time': session.end_time.isoformat() if session.end_time else None
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
        position_manager = get_position_manager()
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
        position_manager = get_position_manager()
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
        position_manager = get_position_manager()
        if not position_manager.grid_manager:
            return jsonify({'success': False, 'error': '网格交易功能未启用'}), 400

        # 检查是否有活跃会话
        session = position_manager.grid_manager.sessions.get(stock_code)

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
        position_manager = get_position_manager()
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
        position_manager = get_position_manager()
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
def save_grid_template():
    """保存网格配置模板"""
    try:
        data = request.get_json()
        template_name = data.get('template_name')

        if not template_name:
            return jsonify({'success': False, 'error': '缺少template_name参数'}), 400

        position_manager = get_position_manager()
        if not position_manager.db_manager:
            return jsonify({'success': False, 'error': '网格交易功能未启用'}), 400

        # 保存模板
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

        template_id = position_manager.db_manager.save_grid_template(template_data)

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
        position_manager = get_position_manager()
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


@app.route('/api/grid/config', methods=['GET'])
def get_grid_config():
    """获取网格交易默认配置"""
    try:
        # 获取持仓总市值
        position_manager = get_position_manager()
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

# ======================= 网格交易API端点结束 =======================

def shutdown_web_server():
    """关闭Web服务器并清理资源"""
    global stop_push_flag, api_executor

    logger.info("正在关闭Web服务器...")

    try:
        # 停止推送线程
        stop_push_flag = True
        logger.info("已停止推送线程")
    except Exception as e:
        logger.error(f"停止推送线程失败: {str(e)}")

    try:
        # 关闭线程池
        if api_executor:
            api_executor.shutdown(wait=True, cancel_futures=True)
            logger.info("已关闭API线程池")
    except Exception as e:
        logger.error(f"关闭API线程池失败: {str(e)}")

    logger.info("Web服务器已关闭")

def start_web_server():
    """启动Web服务器"""
    logger.info("正在启动Web服务器...")

    # 🟢 20251219新增: 启动时同步配置状态
    sync_auto_trading_status()

    start_push_thread()
    app.run(host=config.WEB_SERVER_HOST, port=config.WEB_SERVER_PORT, debug=config.WEB_SERVER_DEBUG, use_reloader=False)

if __name__ == '__main__':
     start_web_server()