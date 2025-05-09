# -*- coding: utf-8 -*-

"""
Web服务模块，提供RESTful API接口与前端交互
"""
import os
import time
import json
import threading
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory, make_response
from flask_cors import CORS
import pandas as pd

import config
from logger import get_logger
from data_manager import get_data_manager
from indicator_calculator import get_indicator_calculator
from position_manager import get_position_manager
from trading_executor import get_trading_executor
from strategy import get_trading_strategy
import utils

# 获取logger
logger = get_logger("web_server")

# 创建Flask应用
app = Flask(__name__, static_folder='web', static_url_path='')

# 允许跨域请求
CORS(app)

# 获取各个模块的实例
data_manager = get_data_manager()
indicator_calculator = get_indicator_calculator()
position_manager = get_position_manager()
trading_executor = get_trading_executor()
trading_strategy = get_trading_strategy()

# 实时推送的数据
realtime_data = {
    'positions': {},
    'latest_prices': {},
    'trading_signals': {},
    'account_info': {},
    'positions_all': []  # Add new field for all positions data
}

# 实时推送线程
push_thread = None
stop_push_flag = False

@app.route('/')
def index():
    """Serve the index.html file"""
    return send_from_directory(os.path.join(os.path.dirname(__file__), 'web'), 'index.html')

@app.route('/<path:filename>')
def serve_static(filename):
    """Serve static files from the 'web' directory"""
    return send_from_directory(os.path.join(os.path.dirname(__file__), 'web'), filename)


@app.route('/api/positions', methods=['GET'])
def get_positions():
    """获取持仓信息"""
    try:
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
        realtime_data['positions_all'] = positions_all_df.to_dict('records')
        
        response = make_response(jsonify({
            'status': 'success',
            'data': {
                'positions': positions,
                'metrics': metrics,
                'positions_all': realtime_data['positions_all']
            }
        }))
        response.headers['Content-Type'] = 'application/json; charset=utf-8'
        return response
    except Exception as e:
        logger.error(f"获取持仓信息时出错: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': f"获取持仓信息时出错: {str(e)}"
        }), 500

@app.route('/api/positions-all', methods=['GET'])
def get_positions_all():
    """获取所有持仓信息（包括所有字段）"""
    try:
        positions_all_df = position_manager.get_all_positions_with_all_fields()
        
        # Replace NaN with None (which will become null in JSON)
        positions_all_df = positions_all_df.replace({pd.NA: None, float('nan'): None}) # Add this line
        
        # 转换为JSON可序列化的格式
        positions_all = positions_all_df.to_dict('records')
        
        # 更新实时数据
        realtime_data['positions_all'] = positions_all
        # print(f"realtime_data['positions_all'] in get_positions_all: {realtime_data['positions_all']}") # Add this line
        
        response = make_response(jsonify({
            'status': 'success',
            'data': positions_all
        }))
        response.headers['Content-Type'] = 'application/json; charset=utf-8'
        return response
    except Exception as e:
        logger.error(f"获取所有持仓信息（所有字段）时出错: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': f"获取所有持仓信息（所有字段）时出错: {str(e)}"
        }), 500

def push_realtime_data():
    """推送实时数据的线程函数"""
    global stop_push_flag
    
    while not stop_push_flag:
        try:
            # 更新所有持仓的最新价格
            if config.is_trade_time():
                position_manager.update_all_positions_price()
            
            # 获取所有持仓数据
            positions_all_df = position_manager.get_all_positions_with_all_fields()
            
            # Replace NaN with None (which will become null in JSON)
            positions_all_df = positions_all_df.replace({pd.NA: None, float('nan'): None}) # Add this line
            
            realtime_data['positions_all'] = positions_all_df.to_dict('records')
            # print(f"realtime_data['positions_all'] in push_realtime_data: {realtime_data['positions_all']}") # Add this line
            
            # 休眠一段时间
            time.sleep(5)
        except Exception as e:
            logger.error(f"推送实时数据时出错: {str(e)}")
            time.sleep(5)


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

def start_web_server():
    """启动Web服务器"""
    start_push_thread()
    app.run(host=config.WEB_SERVER_HOST, port=config.WEB_SERVER_PORT, debug=config.WEB_SERVER_DEBUG, use_reloader=False)

# ... (rest of the code)
