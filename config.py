"""
配置参数管理模块，集中管理所有可配置参数
优化版本：增强止盈止损配置的清晰度
"""
import os
import json
from datetime import datetime

# ======================= 系统配置 =======================
# 调试开关
DEBUG = False
DEBUG_SIMU_STOCK_DATA= False
LOG_LEVEL = "INFO"  # DEBUG, INFO, WARNING, ERROR, CRITICAL
LOG_FILE = "qmt_trading.log"
LOG_MAX_SIZE = 10 * 1024 * 1024  # 10MB
LOG_BACKUP_COUNT = 5  # 保留5个备份文件

# Web访问日志配置
ENABLE_WEB_ACCESS_LOG = True  # 是否启用web访问日志
WEB_ACCESS_LOG_LEVEL = "WARNING"  # web访问日志级别（DEBUG/INFO/WARNING）
WEB_ACCESS_LOG_EXCLUDE_PATHS = ["/api/sse", "/api/positions/stream", "/<path:filename>"]  # 排除的路径（静态文件、SSE长连接等）
WEB_ACCESS_LOG_INCLUDE_TIMING = True  # 是否包含请求耗时统计

# 循环日志优化配置
VERBOSE_LOOP_LOGGING = False  # 是否输出详细的循环日志（开启后会在DEBUG模式下输出每次循环的开始/结束）
ENABLE_HEARTBEAT_LOG = True   # 是否启用系统心跳日志（定期输出运行状态摘要）
HEARTBEAT_INTERVAL = 1800      # 心跳日志间隔（秒，默认1800=30分钟）

# ======================= 功能开关 =======================
ENABLE_SIMULATION_MODE = True   # 模拟交易模式开关（True=模拟，False=实盘）
ENABLE_MONITORING = False       # 控制前端UI监控状态
ENABLE_AUTO_TRADING = False     # 动态止盈止损自动执行开关（不影响网格交易）
ENABLE_ALLOW_BUY = True         # 是否允许买入操作
ENABLE_ALLOW_SELL = True        # 是否允许卖出操作

# 策略功能模块开关(独立控制)
ENABLE_DYNAMIC_STOP_PROFIT = True   # 止盈止损功能开关（信号检测）
# ENABLE_GRID_TRADING 已移至第470行的新网格交易配置区域

# 重要说明：
# - ENABLE_AUTO_TRADING：控制止盈止损信号的自动执行
# - ENABLE_GRID_TRADING：控制网格交易的检测和执行（独立开关，互不影响）
# - ENABLE_DYNAMIC_STOP_PROFIT：控制止盈止损信号的检测

# 其他功能开关
ENABLE_DATA_SYNC = True             # 是否启用数据同步
ENABLE_POSITION_MONITOR = True      # 是否启用持仓监控
ENABLE_LOG_CLEANUP = True           # 是否启用日志清理

# QMT API配置
USE_SYNC_ORDER_API = False          # 使用同步下单接口(True)还是异步接口(False)
                                     # False: 使用order_stock_async()返回seq号,需要回调映射
                                     # True: 使用order_stock()直接返回order_id

# 注释说明：
# - 策略线程始终运行，进行信号检测和监控
# - ENABLE_AUTO_TRADING 控制是否执行检测到的交易信号
# - ENABLE_DYNAMIC_STOP_PROFIT 控制止盈止损模块
# - ENABLE_GRID_TRADING 控制网格交易模块
# - ENABLE_SIMULATION_MODE 控制交易执行方式（模拟/实盘）

# ======================= 数据配置 =======================
# 历史数据存储路径
DATA_DIR = "data"
# 数据库配置（如果使用SQLite）
DB_PATH = os.path.join(DATA_DIR, "trading.db")
# 行情数据周期
PERIODS = ["1d", "1h", "30m", "15m", "5m", "1m"]
# 默认使用的周期
DEFAULT_PERIOD = "1d"
# 历史数据初始获取天数
INITIAL_DAYS = 365
# 定时更新间隔（秒）
UPDATE_INTERVAL = 60
# 备选池股票文件路径
STOCK2BUY_FILE = os.path.join(DATA_DIR, "stock2buy.json")

# 实时数据源配置
REALTIME_DATA_CONFIG = {
    'enable_multi_source': True,
    'health_check_interval': 30,
    'source_timeout': 5,
    'max_error_count': 3,
    'preferred_sources': [
        'XtQuant',
        'Mootdx'
    ]
}


# ======================= 交易配置 =======================
# 交易账号信息（从外部文件读取，避免敏感信息硬编码）
ACCOUNT_CONFIG_FILE = "account_config.json"

# QMT路径配置优先级：
# 1. 环境变量 QMT_PATH
# 2. account_config.json 中的 qmt_path 字段
# 3. 默认值（自动检测常见路径）
DEFAULT_QMT_PATHS = [
    r'C:/QMT/userdata_mini',
    r'C:/光大证券金阳光QMT实盘/userdata_mini',
    r'C:/迅投QMT交易端/userdata_mini',
    r'D:/QMT/userdata_mini',
    r'D:/光大证券金阳光QMT实盘/userdata_mini',
]

def get_account_config():
    """从外部文件读取账号配置"""
    try:
        with open(ACCOUNT_CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        # 如果配置文件不存在，返回默认空配置
        return {"account_id": "", "account_type": "STOCK"}
    except Exception as e:
        print(f"读取账户配置文件失败: {str(e)}")
        return {"account_id": "", "account_type": "STOCK"}

def get_qmt_path():
    """
    获取QMT路径 - 灵活配置方案

    优先级：
    1. 环境变量 QMT_PATH
    2. account_config.json 中的 qmt_path 字段
    3. 自动检测常见路径（存在性检查）
    4. 使用第一个默认路径（兜底）

    Returns:
        str: QMT userdata_mini 路径
    """
    import os

    # 优先级1: 环境变量
    env_path = os.environ.get('QMT_PATH')
    if env_path and os.path.exists(env_path):
        print(f"[QMT_PATH] Using environment variable: {env_path}")
        return env_path

    # 优先级2: 配置文件中的qmt_path字段
    account_config = get_account_config()
    config_path = account_config.get('qmt_path')
    if config_path and os.path.exists(config_path):
        print(f"[QMT_PATH] Using path from account_config.json: {config_path}")
        return config_path

    # 优先级3: 自动检测常见路径
    for path in DEFAULT_QMT_PATHS:
        if os.path.exists(path):
            print(f"[QMT_PATH] Auto-detected path: {path}")
            return path

    # 优先级4: 兜底，返回第一个默认路径（即使不存在，后续会有错误提示）
    fallback_path = DEFAULT_QMT_PATHS[0]
    print(f"[QMT_PATH] WARNING: QMT path not found, using default: {fallback_path}")
    print(f"[QMT_PATH] TIP: If QMT is installed elsewhere, set 'qmt_path' in account_config.json")
    return fallback_path

# 动态获取QMT路径
QMT_PATH = get_qmt_path()

# 账号信息
ACCOUNT_CONFIG = get_account_config()

# ======================= 策略配置 =======================
# 仓位管理
POSITION_UNIT = 35000  # 每次买入金额
MAX_POSITION_VALUE = 70000  # 单只股票最大持仓金额
MAX_TOTAL_POSITION_RATIO = 0.95  # 最大总持仓比例（占总资金）
SIMULATION_BALANCE = 1000000 # 模拟持仓

# ======================= 补仓策略配置（止盈止损策略专用） =======================
# 说明：此配置仅用于动态止盈止损策略的补仓功能，与网格交易策略无关
BUY_GRID_LEVELS = [1.0, 0.93, 0.88]  # 建仓价格网格（第一个是初次建仓价格比例，后面是补仓价格比例）
# 说明：
# - BUY_GRID_LEVELS[0] = 1.0：初次建仓价格比例（100%）
# - BUY_GRID_LEVELS[1] = 0.93：首次补仓阈值，当前价格跌至成本价的93%时触发补仓（下跌7%）
# - BUY_GRID_LEVELS[2] = 0.88：第二次补仓阈值，当前价格跌至成本价的88%时触发补仓（下跌12%）
#
# 补仓金额说明：
# - 补仓策略固定使用 POSITION_UNIT（35000元）作为补仓金额
# - 补仓金额会受 MAX_POSITION_VALUE 限制，剩余空间不足时补仓金额自动调整
# - 不再使用 BUY_AMOUNT_RATIO 比例，避免配置冗余

# ⚠️ 已废弃：BUY_AMOUNT_RATIO（保留仅为向后兼容）
# 此参数仅被网格交易策略使用，补仓策略不使用此参数
# 建议使用新的网格交易配置参数：GRID_PRICE_LEVELS 和 GRID_AMOUNT_RATIOS
BUY_AMOUNT_RATIO = [0.4, 0.3, 0.3]  # 每次买入金额占单元的比例（已废弃，保留向后兼容）

# ======================= 网格交易策略配置（独立配置） =======================
# ===== 旧的网格交易配置已废弃 =====
# GRID_PRICE_LEVELS, GRID_AMOUNT_RATIOS, get_grid_config()
# 已被config.py末尾的新网格交易配置替代(第504-547行)

# ======================= 止盈止损策略配置 =======================
# 补仓功能开关
ENABLE_STOP_LOSS_BUY = True  # 是否启用止损补仓功能

# 统一的止损比例
STOP_LOSS_RATIO = -0.075  # 固定止损比例：成本价下跌7.5%触发止损

# 动态止盈配置
ENABLE_DYNAMIC_STOP_PROFIT = True  # 启用动态止盈功能
INITIAL_TAKE_PROFIT_RATIO = 0.06   # 首次止盈触发阈值：盈利6%时触发
INITIAL_TAKE_PROFIT_PULLBACK_RATIO = 0.005  # 回撤比例：0.5%（可配置）
INITIAL_TAKE_PROFIT_RATIO_PERCENTAGE = 0.6  # 首次止盈卖出比例：50%（半仓）

# 🔑 委托单超时管理配置
ENABLE_PENDING_ORDER_AUTO_CANCEL = True  # 是否启用委托单超时自动撤单
PENDING_ORDER_TIMEOUT_MINUTES = 5        # 委托单超时时间（分钟），默认5分钟
PENDING_ORDER_AUTO_REORDER = True        # 撤单后是否自动重新挂单
PENDING_ORDER_REORDER_PRICE_MODE = "best"  # 重新挂单价格模式: "market"=市价, "limit"=限价, "best"=对手价

# 说明:
# - 当止盈止损委托单提交后超过指定时间仍未成交时:
#   1. ENABLE_PENDING_ORDER_AUTO_CANCEL=True: 自动撤销旧委托单
#   2. PENDING_ORDER_AUTO_REORDER=True: 撤单后自动以新价格重新挂单
#   3. 价格模式:
#      - "market": 以当前市价(最新成交价)挂单
#      - "limit": 以原价格挂单(适用于价格回调情况)
#      - "best": 以对手方最优价格挂单(买单用卖三价,卖单用买三价)
# - ENABLE_PENDING_ORDER_AUTO_CANCEL=False: 仅提示用户，不自动处理

# 分级动态止盈设置（已触发首次止盈后的动态止盈位）
# 格式：(最高盈利比例阈值, 止盈位系数)
# 说明：当最高盈利达到阈值后，止盈位 = 最高价 × 系数
DYNAMIC_TAKE_PROFIT = [
    (0.05, 0.96),  # 最高浮盈达5%时，止盈位为最高价的96%
    (0.10, 0.93),  # 最高浮盈达10%时，止盈位为最高价的93%
    (0.15, 0.90),  # 最高浮盈达15%时，止盈位为最高价的90%
    (0.20, 0.87),  # 最高浮盈达20%时，止盈位为最高价的87%
    (0.30, 0.85),  # 最高浮盈达30%时，止盈位为最高价的85%
    (0.40, 0.83),  # 最高浮盈达40%时，止盈位为最高价的83%
    (0.50, 0.80)   # 最高浮盈达50%时，止盈位为最高价的80%    
]

# 止盈止损优先级说明：
# 1. 止损检查优先级最高
# 2. 未触发首次止盈时：盈利5%触发首次止盈（卖出50%）
# 3. 已触发首次止盈后：使用动态止盈位进行全仓止盈
# 4. 止损价格计算：未触发首次止盈时为成本价×(1-7%)，已触发后为最高价×对应系数

# ===== 旧的网格交易参数已废弃 =====
# GRID_TRADING_ENABLED, GRID_STEP_RATIO, GRID_POSITION_RATIO, GRID_MAX_LEVELS
# 已被config.py末尾的新网格交易配置替代(第504-547行)


# ======================= 指标配置 =======================
# MACD参数
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# 均线参数
MA_PERIODS = [10, 20, 30, 60]

# ======================= 参数配置范围 =======================
# 参数范围定义，用于前后端校验
CONFIG_PARAM_RANGES = {
    "singleBuyAmount": {"min": 1000, "max": 100000, "type": "float", "desc": "单只单次买入金额"},
    "firstProfitSell": {"min": 1.0, "max": 20.0, "type": "float", "desc": "首次止盈比例(%)"},
    "stockGainSellPencent": {"min": 1.0, "max": 100.0, "type": "float", "desc": "首次盈利平仓卖出比例(%)"},
    "stopLossBuy": {"min": 1.0, "max": 20.0, "type": "float", "desc": "补仓跌幅(%)"},
    "stockStopLoss": {"min": 1.0, "max": 20.0, "type": "float", "desc": "止损比例(%)"},
    "singleStockMaxPosition": {"min": 10000, "max": 100000, "type": "float", "desc": "单只股票最大持仓"},
    "totalMaxPosition": {"min": 50000, "max": 1000000, "type": "float", "desc": "最大总持仓"},
    "connectPort": {"min": 1, "max": 65535, "type": "int", "desc": "连接端口"}
}

# 实现参数校验函数
def validate_config_param(param_name, value):
    """验证配置参数是否在有效范围内"""
    if param_name not in CONFIG_PARAM_RANGES:
        return True, ""  # 未定义范围的参数默认通过
        
    param_range = CONFIG_PARAM_RANGES[param_name]
    param_type = param_range.get("type", "float")
    param_min = param_range.get("min")
    param_max = param_range.get("max")
    
    try:
        # 类型转换
        if param_type == "float":
            value = float(value)
        elif param_type == "int":
            value = int(value)
            
        # 范围检查
        if param_min is not None and value < param_min:
            return False, f"{param_range['desc']}不能小于{param_min}"
            
        if param_max is not None and value > param_max:
            return False, f"{param_range['desc']}不能大于{param_max}"
            
        return True, ""
    except (ValueError, TypeError):
        return False, f"{param_range['desc']}必须是{param_type}类型"

# ======================= Web服务配置 =======================
WEB_SERVER_HOST = "localhost"
WEB_SERVER_PORT = 5000
WEB_SERVER_DEBUG = True

# ======================= 日志清理配置 =======================
LOG_CLEANUP_DAYS = 30  # 保留最近30天的日志
LOG_CLEANUP_TIME = "00:00:00"  # 每天凌晨执行清理

# ======================= 功能配置 =======================
# 交易时间配置
# DEBUG模式下使用24小时全周交易，方便测试
if DEBUG:
    TRADE_TIME = {
        "morning_start": "00:00:00",
        "morning_end": "23:59:59",
        "afternoon_start": "00:00:00",
        "afternoon_end": "23:59:59",
        "trade_days": [1, 2, 3, 4, 5, 6, 7]  # 周一至周日
    }
else:
    TRADE_TIME = {
        "morning_start": "09:30:00",
        "morning_end": "13:00:00",
        "afternoon_start": "13:00:00",
        "afternoon_end": "15:00:00",
        "trade_days": [1, 2, 3, 4, 5]  # 周一至周五
    }

# ============ 新增: 盘前同步配置 ============
PREMARKET_SYNC_TIME = {
    "hour": 9,                          # 同步时间: 9点
    "minute": 25,                       # 25分(9:30开盘前5分钟)
    "compensation_window_minutes": 5    # 补偿窗口: 9:25-9:30
}

# 盘前同步功能开关
ENABLE_PREMARKET_XTQUANT_REINIT = True  # 是否在盘前重新初始化xtquant接口
PREMARKET_REINIT_XTDATA = True          # 是否重新初始化xtdata行情接口
PREMARKET_REINIT_XTTRADER = True        # 是否重新初始化xttrader交易接口
ENABLE_WEB_REFRESH_AFTER_REINIT = True  # 接口初始化成功后是否触发Web数据刷新

# ============ xtquant接口鲁棒性配置 ============
XTQUANT_RECONNECT_INTERVAL = 300  # xtquant重连间隔(秒)
XTQUANT_CALL_TIMEOUT = 3.0  # xtquant默认调用超时(秒)
XTQUANT_NON_TRADE_TIMEOUT = 1.0  # 非交易时段超时(秒)

# ============ 线程监控配置 ============
ENABLE_THREAD_MONITOR = True  # 启用线程健康监控
THREAD_CHECK_INTERVAL = 60  # 线程检查间隔(秒)
THREAD_RESTART_COOLDOWN = 60  # 重启冷却时间(秒)

# ============ 持仓监控优化配置 ============
MONITOR_LOOP_INTERVAL = 3  # 监控循环间隔(秒)
MONITOR_CALL_TIMEOUT = 8.0  # 监控调用超时(秒) - 增加到8秒,避免QMT API调用超时
MONITOR_NON_TRADE_SLEEP = 60  # 非交易时段休眠(秒)

def is_trade_time():
    """判断当前是否为交易时间"""
    if DEBUG_SIMU_STOCK_DATA or ENABLE_SIMULATION_MODE:
        return True

    now = datetime.now()
    weekday = now.weekday() + 1  # 转换为1-7表示周一至周日
    
    if weekday not in TRADE_TIME["trade_days"]:
        return False
    
    current_time = now.strftime("%H:%M:%S")
    if (TRADE_TIME["morning_start"] <= current_time <= TRADE_TIME["morning_end"]) or \
       (TRADE_TIME["afternoon_start"] <= current_time <= TRADE_TIME["afternoon_end"]):
        return True
    
    return False

# ======================= 预设股票池 =======================
# 可以在这里定义预设的股票池，也可以从外部文件加载
DEFAULT_STOCK_POOL = [
    "000001.SZ",  # 平安银行
    "600036.SH",  # 招商银行
    "000333.SZ",  # 美的集团
    "600519.SH",  # 贵州茅台
    "000858.SZ",  # 五粮液
]

STOCK_POOL_FILE = "stock_pool.json" 

def load_stock_pool(file_path=STOCK_POOL_FILE):
    """从外部文件加载股票池"""
    try:
        with open(file_path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return DEFAULT_STOCK_POOL

# 实际使用的股票池
STOCK_POOL = load_stock_pool()

# ======================= 动态优先级决策函数 =======================
def determine_stop_loss_add_position_priority():
    """
    根据配置参数动态确定止损和补仓的优先级顺序

    设计逻辑:
    - 场景A: 补仓阈值 < 止损阈值 → 先补仓,达到仓位上限后再止损
    - 场景B: 止损阈值 < 补仓阈值 → 先止损,永不补仓

    返回:
    dict: {
        'priority': str,  # 'add_position_first' 或 'stop_loss_first'
        'add_position_threshold': float,  # 补仓阈值(正数,如0.05表示5%)
        'stop_loss_threshold': float,     # 止损阈值(正数,如0.07表示7%)
        'scenario': str  # 'A' 或 'B'
    }
    """
    # 计算补仓阈值:1 - BUY_GRID_LEVELS[1]
    # 例如: BUY_GRID_LEVELS[1]=0.95 → 补仓阈值=1-0.95=0.05 (5%)
    add_position_threshold = 1 - BUY_GRID_LEVELS[1]

    # 计算止损阈值:abs(STOP_LOSS_RATIO)
    # 例如: STOP_LOSS_RATIO=-0.07 → 止损阈值=0.07 (7%)
    stop_loss_threshold = abs(STOP_LOSS_RATIO)

    # 动态判断优先级
    if add_position_threshold < stop_loss_threshold:
        # 场景A: 补仓5% < 止损7% → 先补仓后止损
        return {
            'priority': 'add_position_first',
            'add_position_threshold': add_position_threshold,
            'stop_loss_threshold': stop_loss_threshold,
            'scenario': 'A',
            'description': f'补仓{add_position_threshold*100:.0f}% < 止损{stop_loss_threshold*100:.0f}% → 先补仓(至仓位上限)后止损'
        }
    else:
        # 场景B: 止损5% <= 补仓7% → 先止损,永不补仓
        return {
            'priority': 'stop_loss_first',
            'add_position_threshold': add_position_threshold,
            'stop_loss_threshold': stop_loss_threshold,
            'scenario': 'B',
            'description': f'止损{stop_loss_threshold*100:.0f}% <= 补仓{add_position_threshold*100:.0f}% → 先止损,永不补仓'
        }

def log_priority_scenario():
    """记录当前优先级场景信息(用于系统启动时打印)"""
    priority_info = determine_stop_loss_add_position_priority()
    scenario = priority_info['scenario']
    description = priority_info['description']

    print(f"\n{'='*60}")
    print(f"动态优先级止损补仓系统 - 场景{scenario}")
    print(f"{'='*60}")
    print(f"补仓阈值: {priority_info['add_position_threshold']*100:.1f}%")
    print(f"止损阈值: {priority_info['stop_loss_threshold']*100:.1f}%")
    print(f"执行策略: {description}")
    print(f"{'='*60}\n")

    return priority_info

# ============================================================
# 性能优化配置 - 2025-12-12
# ============================================================

# QMT持仓查询间隔(秒) - 从3秒延长到10秒
QMT_POSITION_QUERY_INTERVAL = 10.0  # ↓70% API调用

# SQLite同步间隔(秒) - 从5秒延长到15秒
POSITION_SYNC_INTERVAL = 15.0       # ↓87% I/O操作

# 缓存配置
CACHE_CONFIG = {
    'positions_ttl': 5.0,      # 持仓数据缓存5秒
    'quotes_ttl': 3.0,         # 行情数据缓存3秒
    'max_cache_size': 100      # 最大缓存条目
}

# HTTP API节流
HTTP_API_MIN_INTERVAL = 1.0    # 最小请求间隔1秒

# HTTP版本号机制(用于减少无效数据传输)
ENABLE_HTTP_VERSION_CONTROL = True  # 是否启用版本号机制

# 版本号定期升级间隔(秒) - 用于Web界面定期刷新
VERSION_INCREMENT_INTERVAL = 15.0   # Web界面持仓数据每15秒自动刷新一次

# ======================= 卖出监控配置 (2026-01-12) =======================
# 卖出监控功能开关
ENABLE_SELL_MONITOR = True                    # 监控总开关
ENABLE_SELL_ALERT_NOTIFICATION = False         # 告警通知开关（微信/企微）

# 告警配置
SELL_ALERT_CONFIG = {
    'P0_notification': True,    # P0级别告警推送通知（极高风险）
    'P1_notification': True,   # P1级别告警推送通知（高风险）
    'P2_notification': True    # P2级别告警推送通知（中等风险）
}

# ======================= 网格交易高级配置 (2026-01-24) =======================

# ⭐ 网格交易总开关（独立控制，与ENABLE_AUTO_TRADING互不影响）
# - ENABLE_GRID_TRADING = True：启用网格交易检测和执行
# - ENABLE_GRID_TRADING = False：完全禁用网格交易功能
ENABLE_GRID_TRADING = True  # 启用后才能使用网格交易功能

# 回调触发机制
GRID_CALLBACK_RATIO = 0.005  # 回调比例0.5%触发交易

# 档位冷却时间(秒)
GRID_LEVEL_COOLDOWN = 60  # 同一档位60秒内不重复触发

# 启动条件配置
GRID_REQUIRE_PROFIT_TRIGGERED = True  # 是否要求已触发止盈才能启动网格交易（True=更安全，False=更灵活）

# 混合退出机制 - 默认值
GRID_MAX_DEVIATION_RATIO = 0.15    # 网格中心最大偏离±15%
GRID_TARGET_PROFIT_RATIO = 0.10    # 目标盈利10%
GRID_STOP_LOSS_RATIO = -0.10       # 止损-10%
GRID_DEFAULT_DURATION_DAYS = 7     # 默认运行7天

# Web界面默认值
GRID_DEFAULT_PRICE_INTERVAL = 0.05           # 默认价格间隔5%
GRID_DEFAULT_POSITION_RATIO = 0.25           # 默认每档交易25%
GRID_DEFAULT_MAX_INVESTMENT_RATIO = 0.5      # 默认最大投入为持仓市值50%

# 日志级别
GRID_LOG_LEVEL = "INFO"  # DEBUG时输出详细价格追踪

# 网格交易策略标识
GRID_STRATEGY_NAME = "grid"  # 用于trade_records表的strategy字段

def get_grid_default_config(position_market_value: float) -> dict:
    """
    获取网格交易默认配置

    Args:
        position_market_value: 当前持仓市值

    Returns:
        默认配置字典
    """
    return {
        'price_interval': GRID_DEFAULT_PRICE_INTERVAL,
        'position_ratio': GRID_DEFAULT_POSITION_RATIO,
        'callback_ratio': GRID_CALLBACK_RATIO,
        # 非交易时间市值可能为None，使用默认值10000元兜底
        'max_investment': (position_market_value * GRID_DEFAULT_MAX_INVESTMENT_RATIO) if position_market_value and position_market_value > 0 else 10000,
        'max_deviation': GRID_MAX_DEVIATION_RATIO,
        'target_profit': GRID_TARGET_PROFIT_RATIO,
        'stop_loss': GRID_STOP_LOSS_RATIO,
        'duration_days': GRID_DEFAULT_DURATION_DAYS
    }
