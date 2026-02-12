document.addEventListener('DOMContentLoaded', () => {
    console.log("DOM fully loaded and parsed");

    // --- Configuration ---
    let API_BASE_URL = ''; // 将根据用户配置的IP和端口动态设置
    const ORIGINAL_API_ENDPOINTS = {
        // --- GET Endpoints ---
        getConfig: `/api/config`,
        getStatus: `/api/status`,
        getHoldings: `/api/holdings`,
        getLogs: `/api/logs`,
        getPositionsAll: `/api/positions-all`, // 获取所有持仓数据
        getTradeRecords: `/api/trade-records`, // 获取交易记录
        getStockPool: `/api/stock_pool/list`,
        // --- POST Endpoints ---
        saveConfig: `/api/config/save`,
        checkConnection: '/api/connection/status',
        startMonitor: `/api/monitor/start`,
        stopMonitor: `/api/monitor/stop`,
        clearLogs: `/api/logs/clear`,
        clearCurrentData: `/api/data/clear_current`, 
        clearBuySellData: `/api/data/clear_buysell`, 
        importSavedData: `/api/data/import`,
        initHoldings: `/api/holdings/init`,
        executeBuy: `/api/actions/execute_buy`,
        updateHoldingParams: `/api/holdings/update`
    };
    // 工作用的API端点对象
    let API_ENDPOINTS = { ...ORIGINAL_API_ENDPOINTS };

    // 轮询设置
    let POLLING_INTERVAL = 5000; // 默认5秒
    const ACTIVE_POLLING_INTERVAL = 3000; // 活跃状态：3秒
    const INACTIVE_POLLING_INTERVAL = 10000; // 非活跃状态：10秒
    let pollingIntervalId = null;
    let isMonitoring = false; // 前端监控状态，仅控制UI数据刷新
    let isAutoTradingEnabled = false; // 自动交易状态，由全局监控总开关控制
    let isSimulationMode = false; // 模拟交易模式
    let isPageActive = true; // 页面活跃状态
    let userMonitoringIntent = null; // 用户监控意图（点击按钮后）
    let isApiConnected = true; // API连接状态，初始假设已连接
    
    // 为不同类型的数据设置不同的刷新频率
    const DATA_REFRESH_INTERVALS = {
        status: 5000,     // 状态信息每5秒刷新一次
        holdings: 3000,   // 持仓列表每3秒刷新一次
        logs: 5000        // 日志每5秒刷新一次
    };

    // SSE连接
    let sseConnection = null;
    
    // 数据版本号跟踪
    let currentDataVersions = {
        holdings: 0,
        logs: 0,
        status: 0
    };
    
    // 请求锁定状态 - 防止重复请求
    let requestLocks = {
        status: false,
        holdings: false,
        logs: false
    };
    
    // 最近一次显示刷新状态的时间戳
    let lastRefreshStatusShown = 0;
    
    // 最近数据更新时间戳
    let lastDataUpdateTimestamps = {
        status: 0,
        holdings: 0,
        logs: 0
    };

    // 参数范围
    let paramRanges = {};

    // 网格交易状态存储
    let gridTradingStatus = {};  // 格式: { stock_code: { sessionId, status, config, lastUpdate } }

    // ============ 网格分级策略: 全局变量 ============
    let riskTemplates = {};  // 缓存风险模板数据

    // ============ 网格Tooltip: 数据缓存 ============
    let tooltipDataCache = {};  // 缓存tooltip数据
    const TOOLTIP_CACHE_TIME = 30000;  // 30秒缓存

    // --- DOM Element References ---
    const elements = {
        messageArea: document.getElementById('messageArea'),
        simulationModeWarning: document.getElementById('simulationModeWarning'),
        // 配置表单元素
        singleBuyAmount: document.getElementById('singleBuyAmount'),
        firstProfitSell: document.getElementById('firstProfitSell'),
        firstProfitSellEnabled: document.getElementById('firstProfitSellEnabled'),
        stockGainSellPencent: document.getElementById('stockGainSellPencent'),
        firstProfitSellPencent: document.getElementById('firstProfitSellPencent'),
        allowBuy: document.getElementById('allowBuy'),
        allowSell: document.getElementById('allowSell'),
        stopLossBuy: document.getElementById('stopLossBuy'),
        stopLossBuyEnabled: document.getElementById('stopLossBuyEnabled'),
        stockStopLoss: document.getElementById('stockStopLoss'),
        StopLossEnabled: document.getElementById('StopLossEnabled'),
        singleStockMaxPosition: document.getElementById('singleStockMaxPosition'),
        totalMaxPosition: document.getElementById('totalMaxPosition'),
        connectPort: document.getElementById('connectPort'),
        totalAccounts: document.getElementById('totalAccounts'),
        globalAllowBuySell: document.getElementById('globalAllowBuySell'),
        simulationMode: document.getElementById('simulationMode'),
        // 错误提示元素
        singleBuyAmountError: document.getElementById('singleBuyAmountError'),
        firstProfitSellError: document.getElementById('firstProfitSellError'),
        stockGainSellPencentError: document.getElementById('stockGainSellPencentError'),
        stopLossBuyError: document.getElementById('stopLossBuyError'),
        stockStopLossError: document.getElementById('stockStopLossError'),
        singleStockMaxPositionError: document.getElementById('singleStockMaxPositionError'),
        totalMaxPositionError: document.getElementById('totalMaxPositionError'),
        connectPortError: document.getElementById('connectPortError'),
        // 账户信息和状态
        accountId: document.getElementById('accountId'),
        availableBalance: document.getElementById('availableBalance'),
        maxHoldingValue: document.getElementById('maxHoldingValue'),
        totalAssets: document.getElementById('totalAssets'),
        lastUpdateTimestamp: document.getElementById('last-update-timestamp'),
        statusIndicator: document.getElementById('statusIndicator'),
        // 按钮
        toggleMonitorBtn: document.getElementById('toggleMonitorBtn'),
        saveConfigBtn: document.getElementById('saveConfigBtn'),
        clearLogBtn: document.getElementById('clearLogBtn'),
        clearCurrentDataBtn: document.getElementById('clearCurrentDataBtn'),
        clearBuySellDataBtn: document.getElementById('clearBuySellDataBtn'),
        importDataBtn: document.getElementById('importDataBtn'),
        initHoldingsBtn: document.getElementById('initHoldingsBtn'),
        executeBuyBtn: document.getElementById('executeBuyBtn'),
        // 买入设置
        buyStrategy: document.getElementById('buyStrategy'),
        buyQuantity: document.getElementById('buyQuantity'),
        // 持仓表格
        holdingsTableBody: document.getElementById('holdingsTableBody'),
        selectAllHoldings: document.getElementById('selectAllHoldings'),
        holdingsLoading: document.getElementById('holdingsLoading'),
        holdingsError: document.getElementById('holdingsError'),
        // 订单日志
        orderLog: document.getElementById('orderLog'),
        logLoading: document.getElementById('logLoading'),
        logError: document.getElementById('logError'),
        // 连接状态
        connectionStatus: document.getElementById('connectionStatus')
    };

    // --- 监听页面可见性变化 ---
    document.addEventListener('visibilitychange', () => {
        isPageActive = !document.hidden;
        
        // 如果轮询已启动，重新调整轮询间隔
        if (pollingIntervalId && isMonitoring) {
            stopPolling();
            startPolling();
        }
    });
    
    // --- 添加参数验证函数 ---
    function validateParameter(inputElement, errorElement, min, max, fieldName) {
        const value = parseFloat(inputElement.value);
        let errorMessage = "";
        
        if (isNaN(value)) {
            errorMessage = `${fieldName || '参数'}必须是数字`;
        } else if (min !== undefined && value < min) {
            errorMessage = `${fieldName || '参数'}不能小于${min}`;
        } else if (max !== undefined && value > max) {
            errorMessage = `${fieldName || '参数'}不能大于${max}`;
        }
        
        if (errorMessage) {
            errorElement.textContent = errorMessage;
            errorElement.classList.remove('hidden');
            inputElement.classList.add('border-red-500');
            return false;
        } else {
            errorElement.classList.add('hidden');
            inputElement.classList.remove('border-red-500');
            return true;
        }
    }
    
    // --- 添加表单验证函数 ---
    function validateForm() {
        let isValid = true;
        
        // 验证单次买入金额
        isValid = validateParameter(
            elements.singleBuyAmount, 
            elements.singleBuyAmountError, 
            paramRanges.singleBuyAmount?.min, 
            paramRanges.singleBuyAmount?.max,
            "单次买入金额"
        ) && isValid;
        
        // 验证首次止盈比例
        isValid = validateParameter(
            elements.firstProfitSell, 
            elements.firstProfitSellError, 
            paramRanges.firstProfitSell?.min, 
            paramRanges.firstProfitSell?.max,
            "首次止盈比例"
        ) && isValid;
        
        // 验证首次盈利平仓卖出
        isValid = validateParameter(
            elements.stockGainSellPencent, 
            elements.stockGainSellPencentError, 
            paramRanges.stockGainSellPencent?.min, 
            paramRanges.stockGainSellPencent?.max,
            "首次盈利平仓卖出比例"
        ) && isValid;
        
        // 验证补仓跌幅
        isValid = validateParameter(
            elements.stopLossBuy, 
            elements.stopLossBuyError, 
            paramRanges.stopLossBuy?.min, 
            paramRanges.stopLossBuy?.max,
            "补仓跌幅"
        ) && isValid;
        
        // 验证止损比例
        isValid = validateParameter(
            elements.stockStopLoss, 
            elements.stockStopLossError, 
            paramRanges.stockStopLoss?.min, 
            paramRanges.stockStopLoss?.max,
            "止损比例"
        ) && isValid;
        
        // 验证单只股票最大持仓
        isValid = validateParameter(
            elements.singleStockMaxPosition, 
            elements.singleStockMaxPositionError, 
            paramRanges.singleStockMaxPosition?.min, 
            paramRanges.singleStockMaxPosition?.max,
            "单只股票最大持仓"
        ) && isValid;
        
        // 验证最大总持仓
        isValid = validateParameter(
            elements.totalMaxPosition, 
            elements.totalMaxPositionError, 
            paramRanges.totalMaxPosition?.min, 
            paramRanges.totalMaxPosition?.max,
            "最大总持仓"
        ) && isValid;
        
        // 验证端口号
        isValid = validateParameter(
            elements.connectPort, 
            elements.connectPortError, 
            paramRanges.connectPort?.min, 
            paramRanges.connectPort?.max,
            "端口号"
        ) && isValid;
        
        return isValid;
    }
    
    // --- 添加参数监听器 ---
    function addParameterValidationListeners() {
        // 为每个需要验证的输入框添加监听器
        elements.singleBuyAmount.addEventListener('change', () => {
            if (validateParameter(
                elements.singleBuyAmount, 
                elements.singleBuyAmountError, 
                paramRanges.singleBuyAmount?.min, 
                paramRanges.singleBuyAmount?.max,
                "单次买入金额"
            )) {
                throttledSyncParameter('singleBuyAmount', parseFloat(elements.singleBuyAmount.value));
            }
        });
        
        elements.firstProfitSell.addEventListener('change', () => {
            if (validateParameter(
                elements.firstProfitSell, 
                elements.firstProfitSellError, 
                paramRanges.firstProfitSell?.min, 
                paramRanges.firstProfitSell?.max,
                "首次止盈比例"
            )) {
                throttledSyncParameter('firstProfitSell', parseFloat(elements.firstProfitSell.value));
            }
        });
        
        elements.stockGainSellPencent.addEventListener('change', () => {
            if (validateParameter(
                elements.stockGainSellPencent, 
                elements.stockGainSellPencentError, 
                paramRanges.stockGainSellPencent?.min, 
                paramRanges.stockGainSellPencent?.max,
                "首次盈利平仓卖出比例"
            )) {
                throttledSyncParameter('stockGainSellPencent', parseFloat(elements.stockGainSellPencent.value));
            }
        });
        
        elements.stopLossBuy.addEventListener('change', () => {
            if (validateParameter(
                elements.stopLossBuy, 
                elements.stopLossBuyError, 
                paramRanges.stopLossBuy?.min, 
                paramRanges.stopLossBuy?.max,
                "补仓跌幅"
            )) {
                throttledSyncParameter('stopLossBuy', parseFloat(elements.stopLossBuy.value));
            }
        });
        
        elements.stockStopLoss.addEventListener('change', () => {
            if (validateParameter(
                elements.stockStopLoss, 
                elements.stockStopLossError, 
                paramRanges.stockStopLoss?.min, 
                paramRanges.stockStopLoss?.max,
                "止损比例"
            )) {
                throttledSyncParameter('stockStopLoss', parseFloat(elements.stockStopLoss.value));
            }
        });
        
        elements.singleStockMaxPosition.addEventListener('change', () => {
            if (validateParameter(
                elements.singleStockMaxPosition, 
                elements.singleStockMaxPositionError, 
                paramRanges.singleStockMaxPosition?.min, 
                paramRanges.singleStockMaxPosition?.max,
                "单只股票最大持仓"
            )) {
                throttledSyncParameter('singleStockMaxPosition', parseFloat(elements.singleStockMaxPosition.value));
            }
        });
        
        elements.totalMaxPosition.addEventListener('change', () => {
            if (validateParameter(
                elements.totalMaxPosition, 
                elements.totalMaxPositionError, 
                paramRanges.totalMaxPosition?.min, 
                paramRanges.totalMaxPosition?.max,
                "最大总持仓"
            )) {
                throttledSyncParameter('totalMaxPosition', parseFloat(elements.totalMaxPosition.value));
            }
        });
        
        elements.connectPort.addEventListener('change', () => {
            if (validateParameter(
                elements.connectPort, 
                elements.connectPortError, 
                paramRanges.connectPort?.min, 
                paramRanges.connectPort?.max,
                "端口号"
            )) {
                throttledSyncParameter('connectPort', parseInt(elements.connectPort.value));
                // 端口更改后更新API基础URL
                updateApiBaseUrl();
            }
        });
        
        // 开关类参数的实时同步
        elements.allowBuy.addEventListener('change', (event) => {
            throttledSyncParameter('allowBuy', event.target.checked);
        });

        elements.allowSell.addEventListener('change', (event) => {
            throttledSyncParameter('allowSell', event.target.checked);
        });

        // 模拟交易模式切换监听
        elements.simulationMode.addEventListener('change', (event) => {
            isSimulationMode = event.target.checked;
            updateSimulationModeUI();
            throttledSyncParameter('simulationMode', event.target.checked);
        });

        // 全局监控总开关 - 自动交易控制
        elements.globalAllowBuySell.addEventListener('change', (event) => {
            // 明确：这里只影响自动交易状态，不影响监控UI状态
            const autoTradingEnabled = event.target.checked;
            isAutoTradingEnabled = autoTradingEnabled; // 更新本地状态
            
            apiRequest(API_ENDPOINTS.saveConfig, {
                method: 'POST',
                body: JSON.stringify({ globalAllowBuySell: autoTradingEnabled })
            })
            .then(response => {
                console.log("自动交易状态已更新:", autoTradingEnabled);
            })
            .catch(error => {
                console.error("更新自动交易状态失败:", error);
                // 可选：回滚UI状态
                event.target.checked = !autoTradingEnabled;
                isAutoTradingEnabled = !autoTradingEnabled;
            });
        });
        
        // 其他开关类参数实时同步
        elements.firstProfitSellEnabled.addEventListener('change', (event) => {
            throttledSyncParameter('firstProfitSellEnabled', event.target.checked);
        });

        elements.firstProfitSellPencent.addEventListener('change', (event) => {
            throttledSyncParameter('firstProfitSellPencent', event.target.checked);
        });

        elements.stopLossBuyEnabled.addEventListener('change', (event) => {
            throttledSyncParameter('stopLossBuyEnabled', event.target.checked);
        });

        elements.StopLossEnabled.addEventListener('change', (event) => {
            throttledSyncParameter('StopLossEnabled', event.target.checked);
        });
        
        // 监听IP地址变更
        elements.totalAccounts.addEventListener('change', (event) => {
            throttledSyncParameter('totalAccounts', event.target.value);
            // IP变更后更新API基础URL
            updateApiBaseUrl();
        });
    }
    
    // 更新模拟交易模式UI
    function updateSimulationModeUI() {
        if (isSimulationMode) {
            elements.simulationModeWarning.classList.remove('hidden');
            elements.executeBuyBtn.classList.add('bg-orange-600', 'hover:bg-orange-700');
            elements.executeBuyBtn.classList.remove('bg-cyan-600', 'hover:bg-cyan-700');
        } else {
            elements.simulationModeWarning.classList.add('hidden');
            elements.executeBuyBtn.classList.remove('bg-orange-600', 'hover:bg-orange-700');
            elements.executeBuyBtn.classList.add('bg-cyan-600', 'hover:bg-cyan-700');
        }
    }
    
    // --- 节流函数 ---
    function throttle(func, limit) {
        let lastFunc;
        let lastRan;
        return function() {
            const context = this;
            const args = arguments;
            if (!lastRan) {
                func.apply(context, args);
                lastRan = Date.now();
            } else {
                clearTimeout(lastFunc);
                lastFunc = setTimeout(function() {
                    if ((Date.now() - lastRan) >= limit) {
                        func.apply(context, args);
                        lastRan = Date.now();
                    }
                }, limit - (Date.now() - lastRan));
            }
        }
    }
    
    // 判断两个数据是否基本相同（避免不必要的UI更新）
    function areDataEqual(oldData, newData, ignoreFields = []) {
        if (!oldData || !newData) return false;
        
        // 对于简单对象，比较关键字段
        for (const key in newData) {
            if (ignoreFields.includes(key)) continue;
            
            if (typeof newData[key] === 'number' && typeof oldData[key] === 'number') {
                // 对于数值，考虑舍入误差
                if (Math.abs(newData[key] - oldData[key]) > 0.001) return false;
            } else if (newData[key] !== oldData[key]) {
                return false;
            }
        }
        
        return true;
    }

    // --- 工具函数 ---
    function showMessage(text, type = 'info', duration = 5000) {
        elements.messageArea.innerHTML = ''; 
        const messageDiv = document.createElement('div');
        messageDiv.textContent = text;
        messageDiv.className = `status-message ${type}`;
        elements.messageArea.appendChild(messageDiv);

        if (duration > 0) {
            setTimeout(() => {
                if (messageDiv.parentNode === elements.messageArea) {
                    elements.messageArea.removeChild(messageDiv);
                }
            }, duration);
        }
        
        // 消息滚动到可见
        messageDiv.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    // 显示刷新状态 - 添加节流
    function showRefreshStatus() {
        // 限制刷新状态显示频率 - 最少间隔3秒
        const now = Date.now();
        if (now - lastRefreshStatusShown < 3000) {
            return;
        }
        lastRefreshStatusShown = now;
        
        // 如果已经存在刷新状态元素，则移除它
        const existingStatus = document.getElementById('refreshStatus');
        if (existingStatus) {
            existingStatus.remove();
        }
        
        // 创建新的刷新状态元素
        const statusElement = document.createElement('div');
        statusElement.id = 'refreshStatus';
        statusElement.className = 'fixed bottom-2 right-2 bg-blue-100 text-blue-800 px-2 py-1 rounded text-xs';
        statusElement.innerHTML = '数据刷新中...';
        document.body.appendChild(statusElement);
        
        // 0.5秒后淡出
        setTimeout(() => {
            statusElement.style.animation = 'fadeOut 0.5s ease-in-out';
            setTimeout(() => {
                if (statusElement.parentNode) {
                    statusElement.parentNode.removeChild(statusElement);
                }
            }, 500);
        }, 500);
    }

    // 显示更新指示器
    function showUpdatedIndicator() {
        // 检查最近是否已经显示过更新指示器
        const now = Date.now();
        if (now - lastRefreshStatusShown < 2000) {
            return; // 如果2秒内已显示过刷新状态，则不显示更新指示器
        }
        
        const indicator = document.createElement('div');
        indicator.className = 'fixed top-2 left-2 bg-green-100 text-green-800 px-2 py-1 rounded text-xs z-50';
        indicator.textContent = '数据已更新';
        document.body.appendChild(indicator);
        
        setTimeout(() => {
            indicator.style.opacity = '0';
            indicator.style.transition = 'opacity 0.5s';
            setTimeout(() => {
                if (indicator.parentNode) {
                    indicator.parentNode.removeChild(indicator);
                }
            }, 500);
        }, 1000);
    }

    // 更新API基础URL
    function updateApiBaseUrl() {
        const ip = elements.totalAccounts.value || '127.0.0.1';
        const port = elements.connectPort.value || '5000';
        API_BASE_URL = `http://${ip}:${port}`;
        
        // 更新所有API端点
        // for (let key in API_ENDPOINTS) {
        //     API_ENDPOINTS[key] = `${API_BASE_URL}${API_ENDPOINTS[key]}`;
        // }
        API_ENDPOINTS = {};
        for (let key in ORIGINAL_API_ENDPOINTS) {
            API_ENDPOINTS[key] = `${API_BASE_URL}${ORIGINAL_API_ENDPOINTS[key]}`;
        }
        console.log("API Base URL updated:", API_BASE_URL);
    }

    // API请求函数 - 添加节流
    async function apiRequest(url, options = {}) {
        // 提取URL中的关键部分用于日志
        const urlParts = url.split('/');
        const endpoint = urlParts[urlParts.length - 1].split('?')[0]; // 获取API路径的最后一部分
        
        console.log(`API Request: ${options.method || 'GET'} ${endpoint}`, options.body ? JSON.parse(options.body) : '');
        try {
            const response = await fetch(url, {
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    ...options.headers,
                },
                ...options,
            });

            if (!response.ok) {
                let errorMsg = `HTTP error! Status: ${response.status}`;
                try {
                    const errData = await response.json();
                    errorMsg += ` - ${errData.message || JSON.stringify(errData)}`;
                    
                    // 处理参数验证错误
                    if (errData.errors && Array.isArray(errData.errors)) {
                        errorMsg += `\n参数错误: ${errData.errors.join(', ')}`;
                    }
                } catch (e) { /* 忽略非JSON响应错误 */ }
                throw new Error(errorMsg);
            }

            const data = await response.json();
            console.log(`API Response: ${options.method || 'GET'} ${endpoint}`, data.status || 'success');
            
            // 更新API连接状态为已连接
            updateConnectionStatus(true);
            
            return data;
        } catch (error) {
            console.error(`API Error: ${options.method || 'GET'} ${endpoint}`, error);
            showMessage(`请求失败: ${error.message}`, 'error');
            
            // 可能是API连接问题，标记为未连接
            if (endpoint !== 'connection/status') {
                updateConnectionStatus(false);
            }
            
            throw error;
        }
    }

    // --- UI更新函数 ---
    function updateConfigForm(config) {
        console.log("Updating config form:", config);
        if (!config) return;
        elements.singleBuyAmount.value = config.singleBuyAmount ?? '35000';
        elements.firstProfitSell.value = config.firstProfitSell ? parseFloat(config.firstProfitSell).toFixed(2) : '5.00';
        elements.firstProfitSellEnabled.checked = config.firstProfitSellEnabled ?? true;
        elements.stockGainSellPencent.value = config.stockGainSellPencent ?? '60.00';
        elements.firstProfitSellPencent.checked = config.firstProfitSellPencent ?? true;
        elements.allowBuy.checked = config.allowBuy ?? true;
        elements.allowSell.checked = config.allowSell ?? true;
        elements.stopLossBuy.value = config.stopLossBuy ? parseFloat(config.stopLossBuy).toFixed(2) : '5.00';
        elements.stopLossBuyEnabled.checked = config.stopLossBuyEnabled ?? true;
        elements.stockStopLoss.value = config.stockStopLoss ? parseFloat(config.stockStopLoss).toFixed(2) : '7.00';
        elements.StopLossEnabled.checked = config.StopLossEnabled ?? true;
        elements.singleStockMaxPosition.value = config.singleStockMaxPosition ?? '70000';
        elements.totalMaxPosition.value = config.totalMaxPosition ?? '400000';
        elements.connectPort.value = config.connectPort ?? '5000';
        elements.totalAccounts.value = config.totalAccounts ?? '127.0.0.1';
        elements.globalAllowBuySell.checked = config.globalAllowBuySell ?? true;
        elements.simulationMode.checked = config.simulationMode ?? false;
        
        // 更新模拟交易模式状态
        isSimulationMode = config.simulationMode ?? false;
        updateSimulationModeUI();
    }

    // 修改后的updateStatusDisplay函数 - 关键修改在这里
    function updateStatusDisplay(statusData) {
        // 检查数据是否实际变化
        const lastStatusData = window._lastStatusData || {};
        const isDataChanged = !areDataEqual(lastStatusData, statusData, ['timestamp']);
        
        if (!isDataChanged && window._lastStatusData) {
            console.log("Status data unchanged, skipping update");
            return;
        }
        
        window._lastStatusData = {...statusData};
        console.log("Updating status display - data changed");
    
        if (!statusData) return;
    
        // 账户信息更新
        elements.accountId.textContent = statusData.account?.id ?? '--';
        elements.availableBalance.textContent = statusData.account?.availableBalance?.toFixed(2) ?? '--';
        elements.maxHoldingValue.textContent = statusData.account?.maxHoldingValue?.toFixed(2) ?? '--';
        elements.totalAssets.textContent = statusData.account?.totalAssets?.toFixed(2) ?? '--';
        elements.lastUpdateTimestamp.textContent = statusData.account?.timestamp ?? new Date().toLocaleString('zh-CN');
        
        // 获取后端状态，但不自动更新前端状态
        const backendMonitoring = statusData.isMonitoring ?? false;
        const backendAutoTrading = statusData.settings?.enableAutoTrading ?? false;
    
        // 更新自动交易状态 - 只更新全局监控总开关，不影响监控状态
        isAutoTradingEnabled = backendAutoTrading;
        elements.globalAllowBuySell.checked = isAutoTradingEnabled;
        
        // 核心修改：用户明确的监控意图优先，用户操作后不再让后端状态覆盖前端状态
        if (userMonitoringIntent !== null) {
            // 用户通过按钮明确表达了监控意图
            console.log(`使用用户意图设置监控状态: ${userMonitoringIntent}`);
            isMonitoring = userMonitoringIntent;
            
            // 检查状态是否一致并同步到后端，但不让后端状态影响前端
            if (isMonitoring !== backendMonitoring) {
                console.warn(`监控状态不一致: 前端=${isMonitoring}, 后端=${backendMonitoring}, 尝试同步`);
                // 发送额外同步请求，单向同步前端状态到后端
                const endpoint = isMonitoring ? API_ENDPOINTS.startMonitor : API_ENDPOINTS.stopMonitor;
                apiRequest(endpoint, { 
                    method: 'POST', 
                    body: JSON.stringify({ isMonitoring: isMonitoring }) 
                }).catch(err => console.error("同步监控状态失败:", err));
            }
            
            // 已使用用户意图，重置它
            userMonitoringIntent = null;
        }
        // 重要修改：不再自动使用后端状态覆盖前端监控状态
        // 只在初始加载时使用后端状态
        else if (!window._initialMonitoringLoaded) {
            isMonitoring = backendMonitoring;
            window._initialMonitoringLoaded = true;
            console.log(`初始化监控状态: ${isMonitoring}`);
        }
    
        // 根据最终确定的监控状态更新UI
        updateMonitoringUI();
        
        // 更新系统设置
        if (statusData.settings) {
            // 同步模拟交易模式状态
            isSimulationMode = statusData.settings.simulationMode || false;
            elements.simulationMode.checked = isSimulationMode;
            
            // 同步允许买卖设置
            elements.allowBuy.checked = statusData.settings.allowBuy || false;
            elements.allowSell.checked = statusData.settings.allowSell || false;
            
            // 更新模拟交易模式UI
            updateSimulationModeUI();
        }
    }

    // 新增：监控状态UI更新函数，与自动交易状态分离
    function updateMonitoringUI() {
        if (isMonitoring) {
            elements.statusIndicator.textContent = '运行中';
            elements.statusIndicator.className = 'text-lg font-bold text-green-600';
            elements.toggleMonitorBtn.textContent = '停止执行监控';
            elements.toggleMonitorBtn.classList.remove('bg-blue-600', 'hover:bg-blue-700');
            elements.toggleMonitorBtn.classList.add('bg-red-600', 'hover:bg-red-700');
            
            // 只有在非轮询状态下才开始轮询
            if (!pollingIntervalId) {
                startPolling();
            }
        } else {
            elements.statusIndicator.textContent = '未运行';
            elements.statusIndicator.className = 'text-lg font-bold text-red-600';
            elements.toggleMonitorBtn.textContent = '开始执行监控';
            elements.toggleMonitorBtn.classList.remove('bg-red-600', 'hover:bg-red-700');
            elements.toggleMonitorBtn.classList.add('bg-blue-600', 'hover:bg-blue-700');
            
            // 只有在轮询状态下才停止轮询
            if (pollingIntervalId) {
                stopPolling();
            }
        }
    }

    // 轻量级账户信息更新，用于SSE
    function updateQuickAccountInfo(accountInfo) {
        if (accountInfo.available !== undefined) {
            elements.availableBalance.textContent = parseFloat(accountInfo.available).toFixed(2);
            // 添加闪烁效果
            elements.availableBalance.classList.add('highlight-update');
            setTimeout(() => {
                elements.availableBalance.classList.remove('highlight-update');
            }, 1000);
        }

        if (accountInfo.market_value !== undefined) {
            elements.maxHoldingValue.textContent = parseFloat(accountInfo.market_value).toFixed(2);
            elements.maxHoldingValue.classList.add('highlight-update');
            setTimeout(() => {
                elements.maxHoldingValue.classList.remove('highlight-update');
            }, 1000);
        }

        if (accountInfo.total_asset !== undefined) {
            elements.totalAssets.textContent = parseFloat(accountInfo.total_asset).toFixed(2);
            elements.totalAssets.classList.add('highlight-update');
            setTimeout(() => {
                elements.totalAssets.classList.remove('highlight-update');
            }, 1000);
        }
    }

    // 更新监控状态UI，用于SSE - 修改后的版本，不再让监控状态和自动交易状态相互干扰
    function updateMonitoringInfo(monitoringInfo) {
        if (!monitoringInfo) return;

        // 只更新全局监控总开关状态，不影响监控开关状态
        if (monitoringInfo.autoTradingEnabled !== undefined) {
            const wasAutoTrading = isAutoTradingEnabled;
            isAutoTradingEnabled = monitoringInfo.autoTradingEnabled;
            
            // 只有状态有变化时才更新UI
            if (wasAutoTrading !== isAutoTradingEnabled) {
                elements.globalAllowBuySell.checked = isAutoTradingEnabled;
            }
        }

        // 更新允许买入/卖出状态
        if (monitoringInfo.allowBuy !== undefined) {
            elements.allowBuy.checked = monitoringInfo.allowBuy;
        }

        if (monitoringInfo.allowSell !== undefined) {
            elements.allowSell.checked = monitoringInfo.allowSell;
        }

        // 更新模拟交易模式
        if (monitoringInfo.simulationMode !== undefined) {
            const wasSimulationMode = isSimulationMode;
            isSimulationMode = monitoringInfo.simulationMode;
            elements.simulationMode.checked = isSimulationMode;
            
            // 只有状态有变化时才更新UI
            if (wasSimulationMode !== isSimulationMode) {
                updateSimulationModeUI();
            }
        }
    }

    // 显示股票选择对话框
    function showStockSelectDialog(title, content, confirmCallback) {
        const dialog = document.getElementById('stockSelectDialog');
        const dialogTitle = document.getElementById('dialogTitle');
        const dialogContent = document.getElementById('dialogContent');
        const dialogConfirmBtn = document.getElementById('dialogConfirmBtn');
        const dialogCancelBtn = document.getElementById('dialogCancelBtn');
        
        // 设置对话框标题和内容
        dialogTitle.textContent = title;
        dialogContent.innerHTML = content;
        
        // 设置确认按钮事件
        dialogConfirmBtn.onclick = () => {
            confirmCallback();
            dialog.classList.add('hidden');
        };
        
        // 设置取消按钮事件
        dialogCancelBtn.onclick = () => {
            dialog.classList.add('hidden');
        };
        
        // 显示对话框
        dialog.classList.remove('hidden');
    }

    // 处理从备选池随机买入（修改为可编辑版本）
    async function handleRandomPoolBuy(quantity) {
        try {
            // 从后端获取备选池股票列表
            const response = await apiRequest(API_ENDPOINTS.getStockPool);
            
            if (response.status === 'success' && Array.isArray(response.data)) {
                const stocks = response.data;
                
                // 构建对话框内容 - 使用可编辑的文本框而非只读显示
                const content = `
                    <p class="mb-2">以下股票将被用于随机买入（可编辑）：</p>
                    <textarea id="randomPoolStockInput" class="w-full border rounded p-2 h-40">${stocks.join('\n')}</textarea>
                `;
                
                // 显示对话框
                showStockSelectDialog(
                    '确认随机买入股票',
                    content,
                    () => {
                        // 获取用户可能编辑过的股票代码
                        const input = document.getElementById('randomPoolStockInput').value;
                        const editedStocks = input.split('\n')
                            .map(s => s.trim())
                            .filter(s => s.length > 0);
                        
                        if (editedStocks.length === 0) {
                            showMessage('请输入有效的股票代码', 'warning');
                            return;
                        }
                        
                        // 确认后执行买入，使用编辑后的股票列表
                        executeBuyAction('random_pool', quantity, editedStocks);
                    }
                );
            } else {
                throw new Error(response.message || '获取备选池股票失败');
            }
        } catch (error) {
            showMessage(`获取备选池股票失败: ${error.message}`, 'error');
        }
    }

    // 处理自定义股票买入
    function handleCustomStockBuy(quantity) {
        // 构建对话框内容
        const content = `
            <p class="mb-2">请输入要买入的股票代码（一行一个）：</p>
            <textarea id="customStockInput" class="w-full border rounded p-2 h-40"></textarea>
        `;
        
        // 显示对话框
        showStockSelectDialog(
            '自定义股票买入',
            content,
            () => {
                // 获取用户输入的股票代码
                const input = document.getElementById('customStockInput').value;
                const stocks = input.split('\n')
                    .map(s => s.trim())
                    .filter(s => s.length > 0);
                
                if (stocks.length === 0) {
                    showMessage('请输入有效的股票代码', 'warning');
                    return;
                }
                
                // 执行买入
                executeBuyAction('custom_stock', quantity, stocks);
            }
        );
    }

    // 执行买入动作
    async function executeBuyAction(strategy, quantity, stocks) {
        elements.executeBuyBtn.disabled = true;
        showMessage(`执行买入 (${strategy}, ${quantity}只)...`, 'loading', 0);
        
        try {
            const buyData = {
                strategy: strategy,
                quantity: quantity,
                stocks: stocks,
                ...getConfigData() // 包含所有配置参数
            };
            
            const data = await apiRequest(API_ENDPOINTS.executeBuy, {
                method: 'POST',
                body: JSON.stringify(buyData),
            });
            
            if (data.status === 'success') {
                showMessage(data.message || "买入指令已发送", 'success');
                
                // 重置请求锁定状态
                requestLocks.holdings = false;
                requestLocks.logs = false;
                currentHoldingsVersion = 0; // 重置版本号，强制刷新
                
                // 刷新相关数据
                await fetchHoldings();
                await fetchLogs();
                await fetchStatus(); // 更新余额等
            } else {
                showMessage(data.message || "买入指令发送失败", 'error');
            }
        } catch (error) {
            // 错误已由apiRequest处理
        } finally {
            elements.executeBuyBtn.disabled = false;
            // 3秒后清除消息
            setTimeout(() => {
                elements.messageArea.innerHTML = '';
            }, 3000);
        }
    }

    // 判断持仓数据是否需要更新
    function shouldUpdateRow(oldData, newData) {
        // 检查关键字段是否有变化
        const keysToCheck = ['current_price', 'market_value', 'profit_ratio', 'available', 'volume', 'change_percentage'];
        return keysToCheck.some(key => {
            // 对于数值，考虑舍入误差
            if (typeof oldData[key] === 'number' && typeof newData[key] === 'number') {
                return Math.abs(oldData[key] - newData[key]) > 0.001;
            }
            return oldData[key] !== newData[key];
        });
    }

    // 更新现有持仓行（仅更新持仓数据，不更新checkbox状态）
    function updateExistingRow(row, stock) {
        // 更新各个单元格的值
        const cells = row.querySelectorAll('td');

        // ⭐ 使用后端返回的grid_session_active字段来判断是否有活跃的网格会话
        const hasActiveGrid = stock.grid_session_active === true;

        // 更新行的边框样式（视觉指示，不影响checkbox状态）
        row.className = hasActiveGrid
            ? 'hover:bg-gray-50 even:bg-gray-100 border-l-4 border-l-green-500'
            : 'hover:bg-gray-50 even:bg-gray-100';

        // ⭐ checkbox状态由 updateAllGridTradingStatus 独立管理，此处不更新

        // 更新基本信息
        cells[1].textContent = stock.stock_code || '--';
        cells[2].textContent = stock.stock_name || stock.name || '--';

        // 更新涨跌幅，包括类名
        const changePercentage = parseFloat(stock.change_percentage || 0);
        cells[3].textContent = `${changePercentage.toFixed(2)}%`;
        cells[3].className = `border p-2 ${changePercentage >= 0 ? 'text-red-600' : 'text-green-600'}`;

        // 更新价格、成本和盈亏
        cells[4].textContent = parseFloat(stock.current_price || 0).toFixed(2);
        cells[5].textContent = parseFloat(stock.cost_price || 0).toFixed(2);

        const profitRatio = parseFloat(stock.profit_ratio || 0);
        cells[6].textContent = `${profitRatio.toFixed(2)}%`;
        cells[6].className = `border p-2 ${profitRatio >= 0 ? 'text-red-600' : 'text-green-600'}`;

        // 更新持仓信息
        cells[7].textContent = parseFloat(stock.market_value || 0).toFixed(0);
        cells[8].textContent = parseFloat(stock.available || 0).toFixed(0);
        cells[9].textContent = parseFloat(stock.volume || 0).toFixed(0);

        // 更新止盈标志
        cells[10].innerHTML = `<input type="checkbox" ${stock.profit_triggered ? 'checked' : ''} disabled>`;

        // 更新其他数据
        cells[11].textContent = parseFloat(stock.highest_price || 0).toFixed(2);
        cells[12].textContent = parseFloat(stock.stop_loss_price || 0).toFixed(2);
        cells[13].textContent = (stock.open_date || '').split(' ')[0];
        cells[14].textContent = parseFloat(stock.base_cost_price || stock.cost_price || 0).toFixed(2);

        // 高亮闪烁更新的单元格
        cells[4].classList.add('highlight-update');
        setTimeout(() => {
            cells[4].classList.remove('highlight-update');
        }, 1000);
    }

    // 创建新的持仓行（仅创建DOM结构，不设置checkbox状态）
    function createStockRow(stock) {
        const row = document.createElement('tr');
        // ⭐ 使用后端返回的grid_session_active字段来判断是否有活跃的网格会话
        const hasActiveGrid = stock.grid_session_active === true;
        // 如果有活跃网格，添加绿色边框（视觉指示）
        row.className = hasActiveGrid
            ? 'hover:bg-gray-50 even:bg-gray-100 border-l-4 border-l-green-500'
            : 'hover:bg-gray-50 even:bg-gray-100';
        row.dataset.stockCode = stock.stock_code; // 添加标识属性

        // 计算关键值
        const changePercentage = parseFloat(stock.change_percentage || 0);
        const profitRatio = parseFloat(stock.profit_ratio || 0);

        // ⭐ checkbox根据后端返回的grid_session_active字段设置初始状态
        // 后续的状态更新仍由 updateAllGridTradingStatus 独立管理
        // 构建行内容
        row.innerHTML = `
            <td class="border p-2">
                <input type="checkbox" class="holding-checkbox"
                       data-id="${stock.id || stock.stock_code}"
                       data-stock-code="${stock.stock_code}"
                       ${hasActiveGrid ? 'checked' : ''}
                       onmouseenter="showGridTooltip(event, '${stock.stock_code}')"
                       onmouseleave="hideGridTooltip()">
            </td>
            <td class="border p-2">${stock.stock_code || '--'}</td>
            <td class="border p-2">${stock.stock_name || stock.name || '--'}</td>
            <td class="border p-2 ${changePercentage >= 0 ? 'text-red-600' : 'text-green-600'}">${changePercentage.toFixed(2)}%</td>
            <td class="border p-2">${parseFloat(stock.current_price || 0).toFixed(2)}</td>
            <td class="border p-2">${parseFloat(stock.cost_price || 0).toFixed(2)}</td>
            <td class="border p-2 ${profitRatio >= 0 ? 'text-red-600' : 'text-green-600'}">${profitRatio.toFixed(2)}%</td>
            <td class="border p-2">${parseFloat(stock.market_value || 0).toFixed(0)}</td>
            <td class="border p-2">${parseFloat(stock.available || 0).toFixed(0)}</td>
            <td class="border p-2">${parseFloat(stock.volume || 0).toFixed(0)}</td>
            <td class="border p-2 text-center"><input type="checkbox" ${stock.profit_triggered ? 'checked' : ''} disabled></td>
            <td class="border p-2">${parseFloat(stock.highest_price || 0).toFixed(2)}</td>
            <td class="border p-2">${parseFloat(stock.stop_loss_price || 0).toFixed(2)}</td>
            <td class="border p-2 whitespace-nowrap">${(stock.open_date || '').split(' ')[0]}</td>
            <td class="border p-2">${parseFloat(stock.base_cost_price || stock.cost_price || 0).toFixed(2)}</td>
        `;

        // ⭐ checkbox状态由 updateAllGridTradingStatus 独立管理
        // 点击后立即恢复状态，防止浏览器默认行为导致的状态切换

        // 添加点击事件监听器（用于打开网格配置对话框）
        const checkbox = row.querySelector('.holding-checkbox');
        if (checkbox) {
            checkbox.addEventListener('click', async (event) => {
                event.preventDefault(); // 阻止默认的checkbox切换行为

                // ⭐ 立即恢复checkbox的正确状态（防止浏览器切换）
                checkbox.checked = hasActiveGrid;

                await showGridConfigDialog(stock.stock_code);
            });
        }

        return row;
    }

    // 更新持仓表格（增量更新版本）
    function updateHoldingsTable(holdings) {
        // 检查数据是否实际发生变化
        const holdingsStr = JSON.stringify(holdings);
        if (window._lastHoldingsStr === holdingsStr) {
            console.log("Holdings data unchanged, skipping update");
            return;
        }
        window._lastHoldingsStr = holdingsStr;

        console.log("Updating holdings table - data changed");
        elements.holdingsLoading.classList.add('hidden');
        elements.holdingsError.classList.add('hidden');

        if (!Array.isArray(holdings) || holdings.length === 0) {
            elements.holdingsTableBody.innerHTML = '<tr><td colspan="15" class="text-center p-4 text-gray-500">无持仓数据</td></tr>';
            return;
        }

        // 获取现有行
        const existingRows = {};
        const existingRowElements = elements.holdingsTableBody.querySelectorAll('tr[data-stock-code]');
        existingRowElements.forEach(row => {
            existingRows[row.dataset.stockCode] = row;
        });

        // 临时文档片段，减少DOM重绘
        const fragment = document.createDocumentFragment();

        // 记录处理过的股票代码
        const processedStocks = new Set();

        // 数据变化标记
        let hasChanges = false;

        holdings.forEach(stock => {
            processedStocks.add(stock.stock_code);
            
            // 检查是否已存在此股票行
            if (existingRows[stock.stock_code]) {
                // 获取现有数据
                const oldData = existingRows[stock.stock_code].data || {};
                
                // 检查是否需要更新
                if (shouldUpdateRow(oldData, stock)) {
                    updateExistingRow(existingRows[stock.stock_code], stock);
                    hasChanges = true;
                }
                
                // 更新存储的数据
                existingRows[stock.stock_code].data = {...stock};
            } else {
                // 创建新行
                const row = createStockRow(stock);
                // 存储数据引用
                row.data = {...stock};
                fragment.appendChild(row);
                hasChanges = true;
            }
        });

        // 添加新行
        if (fragment.childNodes.length > 0) {
            elements.holdingsTableBody.appendChild(fragment);
        }

        // 移除不再存在的行
        let hasRemovals = false;
        existingRowElements.forEach(row => {
            if (!processedStocks.has(row.dataset.stockCode)) {
                row.remove();
                hasRemovals = true;
            }
        });

        // 只有发生变化时才添加复选框监听器
        if (hasChanges || hasRemovals) {
            addHoldingCheckboxListeners();
        }
    }

    function addHoldingCheckboxListeners() {
        const checkboxes = elements.holdingsTableBody.querySelectorAll('.holding-checkbox');
        checkboxes.forEach(checkbox => {
            // ⭐ checkbox状态由 updateAllGridTradingStatus 独立管理
            // 此处只添加点击事件监听器，用于打开网格配置对话框

            // 检查是否已有监听器（避免重复添加）
            if (!checkbox.dataset.hasClickListener) {
                checkbox.addEventListener('click', async (e) => {
                    e.preventDefault(); // 阻止默认的checkbox切换行为
                    const stockCode = e.target.dataset.stockCode;

                    // ⭐ 立即恢复checkbox的正确状态（防止浏览器切换）
                    const hasActiveGrid = gridTradingStatus[stockCode]?.status === 'active';
                    e.target.checked = hasActiveGrid;

                    // 弹出配置对话框
                    // 如果有active session，对话框会显示当前配置和"停止"按钮
                    // 如果没有active session，对话框会显示默认配置和"启动"按钮
                    await showGridConfigDialog(stockCode);

                    // 检查是否所有复选框都被选中（用于全选框状态）
                    const allChecked = Array.from(checkboxes).every(cb => cb.checked);
                    elements.selectAllHoldings.checked = allChecked;
                });
                checkbox.dataset.hasClickListener = 'true';
            }
        });
    }

    function updateLogs(logEntries) {
        // 检查数据是否实际发生变化
        const logsStr = JSON.stringify(logEntries);
        if (window._lastLogsStr === logsStr) {
            console.log("Logs data unchanged, skipping update");
            return;
        }
        window._lastLogsStr = logsStr;

        // 记住当前滚动位置和是否在底部
        const isAtBottom = elements.orderLog.scrollTop + elements.orderLog.clientHeight >= elements.orderLog.scrollHeight - 10;
        const currentScrollTop = elements.orderLog.scrollTop;

        elements.logLoading.classList.add('hidden');
        elements.logError.classList.add('hidden');

        // 格式化日志内容
        if (Array.isArray(logEntries)) {
            // 新的格式化逻辑，符合要求的格式
            const formattedLogs = logEntries.map(entry => {
                if (typeof entry === 'object' && entry !== null) {
                    // 修改：转换日期格式为 MM-DD HH:MM:SS
                    let dateStr = '';
                    if (entry.trade_time) {
                        const date = new Date(entry.trade_time);
                        const month = String(date.getMonth() + 1).padStart(2, '0');
                        const day = String(date.getDate()).padStart(2, '0');
                        const hours = String(date.getHours()).padStart(2, '0');
                        const minutes = String(date.getMinutes()).padStart(2, '0');
                        const seconds = String(date.getSeconds()).padStart(2, '0');
                        dateStr = `${month}-${day} ${hours}:${minutes}:${seconds}`;
                    }                   
                    // 转换交易类型
                    const actionType = entry.trade_type === 'BUY' ? '买' : 
                                    (entry.trade_type === 'SELL' ? '卖' : entry.trade_type);
                    
                    // 格式化为要求的格式
                    const formattedPrice = entry.price ? Number(entry.price).toFixed(2) : '';
                    const formattedVolume = entry.volume ? Number(entry.volume).toFixed(0) : '';
                    return `${dateStr}, ${entry.stock_code || ''}, ${entry.stock_name || ''}, ${actionType}, 价: ${formattedPrice}, 量: ${formattedVolume}, 策略: ${entry.strategy || ''}`;
                } else {
                    return String(entry); // 如果不是对象，直接转换为字符串
                }
            });
            elements.orderLog.value = formattedLogs.join('\n');
            
            // 标记数据已更新
            console.log("Logs updated with new data");
        } else {
            elements.orderLog.value = "无可识别的日志数据";
            console.error("未知的日志数据格式:", logEntries);
        }

        // 只有当之前在底部时，才自动滚动到底部
        if (isAtBottom) {
            setTimeout(() => {
                elements.orderLog.scrollTop = elements.orderLog.scrollHeight;
            }, 10);
        } else {
            // 否则保持原来的滚动位置
            setTimeout(() => {
                elements.orderLog.scrollTop = currentScrollTop;
            }, 10);
        }
    }

    // --- 数据获取函数 ---
    async function fetchConfig() {
        try {
            const data = await apiRequest(API_ENDPOINTS.getConfig);
            if (data.status === 'success') {
                updateConfigForm(data.data);
                
                // 保存参数范围
                if (data.ranges) {
                    paramRanges = data.ranges;
                    // 添加参数验证监听器
                    addParameterValidationListeners();
                }
            } else {
                showMessage("加载配置失败: " + (data.message || "未知错误"), 'error');
            }
        } catch (error) {
            showMessage("加载配置失败", 'error');
        }
    }

    async function fetchStatus() {
        // 如果已经有请求在进行中，则跳过
        if (requestLocks.status) {
            console.log('Status request already in progress, skipping');
            return;
        }

        // 最小刷新间隔检查 - 3秒
        const now = Date.now();
        if (now - lastDataUpdateTimestamps.status < 3000) {
            console.log('Status data recently updated, skipping');
            return;
        }

        // 标记请求开始
        requestLocks.status = true;

        try {
            const data = await apiRequest(API_ENDPOINTS.getStatus);
            if (data.status === 'success') {
                updateStatusDisplay(data);
                lastDataUpdateTimestamps.status = Date.now();
            } else {
                showMessage("加载状态信息失败: " + (data.message || "未知错误"), 'error');
                // 不自动重置监控状态，保持用户设置
                // updateStatusDisplay({ isMonitoring: false, account: {} });
            }
        } catch (error) {
            showMessage("加载状态信息失败", 'error');
            // 不自动重置监控状态，保持用户设置
            // updateStatusDisplay({ isMonitoring: false, account: {} });
        } finally {
            // 释放请求锁定，添加小延迟避免立即重复请求
            setTimeout(() => {
                requestLocks.status = false;
            }, 1000);
        }
    }

    // 添加版本号跟踪
    let currentHoldingsVersion = 0;
    // ⭐ 不再需要activeGridSessions变量，直接使用后端返回的grid_session_active字段

    // 修改数据获取函数
    async function fetchHoldings() {
        // 如果已经有请求在进行中，则跳过
        if (requestLocks.holdings) {
            console.log('Holdings request already in progress, skipping');
            return;
        }

        // 标记请求开始
        requestLocks.holdings = true;

        try {
            // 带版本号的请求
            const url = `${API_ENDPOINTS.getPositionsAll}?version=${currentHoldingsVersion}`;
            const data = await apiRequest(url);

            // 检查是否有数据变化
            if (data.no_change) {
                console.log('Holdings data unchanged, skipping update');
                return;
            }

            // 更新版本号
            if (data.data_version) {
                currentHoldingsVersion = data.data_version;
                console.log(`Holdings data updated to version: ${currentHoldingsVersion}`);
            }

            // ⭐ 不再需要单独调用/api/grid/sessions，因为/api/positions响应已包含grid_session_active字段

            if (data.status === 'success' && Array.isArray(data.data)) {
                updateHoldingsTable(data.data);
                lastDataUpdateTimestamps.holdings = Date.now();

                // 更新网格交易状态
                await updateAllGridTradingStatus();
            } else {
                throw new Error(data.message || '数据格式错误');
            }

        } catch (error) {
            console.error('Error fetching holdings:', error);
        } finally {
            // 立即释放锁，移除1秒延迟以支持快速刷新
            requestLocks.holdings = false;
        }
    }

    async function fetchLogs() {  
        // 如果已经有请求在进行中，则跳过
        if (requestLocks.logs) {
            console.log('Logs request already in progress, skipping');
            return;
        }

        // 最小刷新间隔检查 - 3秒
        const now = Date.now();
        if (now - lastDataUpdateTimestamps.logs < 3000) {
            console.log('Logs data recently updated, skipping');
            return;
        }

        // 标记请求开始
        requestLocks.logs = true;

        // 使用延迟显示加载状态
        let loadingTimer = null;

        // 仅在加载时间超过300ms时才显示加载提示
        if (!elements.logLoading.classList.contains('shown')) {
            loadingTimer = setTimeout(() => {
                elements.logLoading.classList.remove('hidden');
                elements.logLoading.classList.add('shown');
            }, 300);
        }

        try {
            const data = await apiRequest(API_ENDPOINTS.getTradeRecords);
            
            // 取消加载提示定时器
            if (loadingTimer) clearTimeout(loadingTimer);
            
            if (data.status === 'success' && Array.isArray(data.data)) {
                // 更新日志内容
                updateLogs(data.data);
                lastDataUpdateTimestamps.logs = Date.now();
            } else {
                throw new Error(data.message || '数据格式错误');
            }
            
            // 短暂延迟后隐藏加载提示
            setTimeout(() => {
                elements.logLoading.classList.add('hidden');
                elements.logLoading.classList.remove('shown');
            }, 300);
        } catch (error) {
            // 取消加载提示定时器
            if (loadingTimer) clearTimeout(loadingTimer);
            
            elements.logLoading.classList.add('hidden');
            elements.logLoading.classList.remove('shown');
            
            // 显示错误信息
            elements.logError.classList.remove('hidden');
            elements.logError.textContent = `加载失败: ${error.message}`;
            
            // 5秒后自动隐藏错误信息
            setTimeout(() => {
                elements.logError.classList.add('hidden');
            }, 5000);
            
            showMessage("加载交易记录失败", 'error');
        } finally {
            // 释放请求锁定，添加小延迟避免立即重复请求
            setTimeout(() => {
                requestLocks.logs = false;
            }, 1000);
        }
    }

    // --- 连接状态检测 - 修改后只影响连接状态指示器，不影响监控状态 ---
    function updateConnectionStatus(isConnected) {
        // 更新连接状态
        isApiConnected = isConnected;
        
        // 只更新UI显示，不影响监控状态
        if (isConnected) {
            elements.connectionStatus.textContent = "QMT已连接";
            elements.connectionStatus.classList.remove('disconnected');
            elements.connectionStatus.classList.add('connected');
        } else {
            elements.connectionStatus.textContent = "QMT未连接";
            elements.connectionStatus.classList.remove('connected');
            elements.connectionStatus.classList.add('disconnected');
        }
    }

    // 添加节流的API连接检测
    const throttledCheckApiConnection = throttle(async function() {
        try {
            console.log("Checking API connection at:", API_ENDPOINTS.checkConnection);
            const response = await fetch(API_ENDPOINTS.checkConnection);
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            const data = await response.json();
            console.log("Connection check response:", data);
            
            // 只更新连接状态指示器，不影响监控状态
            updateConnectionStatus(data.connected);
        } catch (error) {
            console.error("Error checking API connection:", error);
            updateConnectionStatus(false);
        } finally {
            // 继续轮询连接状态
            setTimeout(throttledCheckApiConnection, 5000);
        }
    }, 5000);


    // --- 操作处理函数 ---
    // 修改后的监控开启/关闭函数 - 只影响前端数据刷新，不再与后端自动交易状态混淆
    async function handleToggleMonitor() {
        // 先验证表单数据
        if (!validateForm()) {
            showMessage("请检查配置参数，修正错误后再启动监控", 'error');
            return;
        }

        // 先设置本地用户意图状态
        const newMonitoringState = !isMonitoring;
        userMonitoringIntent = newMonitoringState; // 记录用户意图
        
        const endpoint = isMonitoring ? API_ENDPOINTS.stopMonitor : API_ENDPOINTS.startMonitor;
        const actionText = isMonitoring ? '停止' : '启动';
        elements.toggleMonitorBtn.disabled = true;
        // showMessage(`${actionText}监控中...`, 'loading', 0);

        try {
            // 构建仅包含监控状态的数据
            const monitoringData = {
                isMonitoring: newMonitoringState
            };
            
            const data = await apiRequest(endpoint, { 
                method: 'POST',                
                body: JSON.stringify(monitoringData)
            });

            if (data.status === 'success') {
                // 直接更新本地状态，不等待fetchStatus
                isMonitoring = newMonitoringState;
                
                // 更新UI
                updateMonitoringUI();
                
                // showMessage(`${actionText}监控成功: ${data.message || ''}（注意：此操作不影响自动交易）`, 'success');
            } else {
                showMessage(`${actionText}监控失败: ${data.message || '未知错误'}`, 'error');
                // 恢复用户意图，因为操作失败
                userMonitoringIntent = null;
            }
            
            // 跳过调用fetchStatus，因为我们已经主动设置了状态
        } catch (error) {
            showMessage(`${actionText}监控失败: ${error.message}`, 'error');
            // 恢复用户意图，因为操作失败
            userMonitoringIntent = null;
        } finally {
            elements.toggleMonitorBtn.disabled = false;
            // 3秒后清除消息
            setTimeout(() => {
                elements.messageArea.innerHTML = '';
            }, 3000);
        }
    }

    // 获取所有配置表单的值
    function getConfigData() {
        return {
            singleBuyAmount: parseFloat(elements.singleBuyAmount.value) || 35000,
            firstProfitSell: parseFloat(elements.firstProfitSell.value) || 5.0,
            firstProfitSellEnabled: elements.firstProfitSellEnabled.checked,
            stockGainSellPencent: parseFloat(elements.stockGainSellPencent.value) || 60.0,
            firstProfitSellPencent: elements.firstProfitSellPencent.checked,
            allowBuy: elements.allowBuy.checked,
            allowSell: elements.allowSell.checked,
            stopLossBuy: parseFloat(elements.stopLossBuy.value) || 5.0,
            stopLossBuyEnabled: elements.stopLossBuyEnabled.checked,
            stockStopLoss: parseFloat(elements.stockStopLoss.value) || 7.0,
            StopLossEnabled: elements.StopLossEnabled.checked,
            singleStockMaxPosition: parseFloat(elements.singleStockMaxPosition.value) || 70000,
            totalMaxPosition: parseFloat(elements.totalMaxPosition.value) || 400000,
            connectPort: elements.connectPort.value || '5000',
            totalAccounts: elements.totalAccounts.value || '127.0.0.1',
            globalAllowBuySell: elements.globalAllowBuySell.checked,
            simulationMode: elements.simulationMode.checked            
        };
    }

    async function handleSaveConfig() {
        // 先验证表单数据
        if (!validateForm()) {
            showMessage("请检查配置参数，修正错误后再保存", 'error');
            return;
        }

        const configData = getConfigData();
        console.log("Saving config:", configData);
        showMessage("保存配置中...", 'loading', 0);
        elements.saveConfigBtn.disabled = true;

        try {
            const data = await apiRequest(API_ENDPOINTS.saveConfig, {
                method: 'POST',                
                body: JSON.stringify(configData),
            });
            
            if (data.status === 'success') {
                showMessage(data.message || "配置已保存", 'success');
                
                // 更新模拟交易模式状态
                isSimulationMode = configData.simulationMode;
                updateSimulationModeUI();
                
                // 更新自动交易状态
                isAutoTradingEnabled = configData.globalAllowBuySell;
            } else {
                showMessage(data.message || "保存失败", 'error');
                
                // 如果有验证错误，显示详细信息
                if (data.errors && Array.isArray(data.errors)) {
                    showMessage(`参数错误: ${data.errors.join(', ')}`, 'error');
                }
            }
        } catch (error) {
            // 错误已由apiRequest处理
        } finally {
            elements.saveConfigBtn.disabled = false;
            // 3秒后清除消息
            setTimeout(() => {
                elements.messageArea.innerHTML = '';
            }, 3000);
        }
    }

    // 添加参数即时同步函数
    function syncParameterToBackend(paramName, value) {
        // 创建只包含变更参数的对象
        const paramData = {
            [paramName]: value
        };
        
        console.log(`同步参数到后台: ${paramName} = ${value}`);
        
        // 调用保存配置API，只发送变更的参数
        apiRequest(API_ENDPOINTS.saveConfig, {
            method: 'POST',
            body: JSON.stringify(paramData)
        })
        .then(data => {
            if (data.status === 'success') {
                console.log(`参数 ${paramName} 已同步到后台`);
            } else {
                console.error(`参数同步失败: ${data.message}`);
            }
        })
        .catch(error => {
            console.error(`同步参数时出错: ${error}`);
        });
    }

    // 使用节流防止频繁发送请求
    const throttledSyncParameter = throttle(syncParameterToBackend, 500);

    async function handleClearLogs() {
        if (!confirm("⚠️ 确定要清空所有日志吗？\n\n此操作不可撤销！")) return;
        showMessage("清空日志中...", 'loading', 0);
        elements.clearLogBtn.disabled = true;
        try {
            const data = await apiRequest(API_ENDPOINTS.clearLogs, { method: 'POST' });
            
            if (data.status === 'success') {
                showMessage(data.message || "日志已清空", 'success');
                elements.orderLog.value = ''; // 立即清空前端显示
                window._lastLogsStr = ''; // 重置日志缓存
            } else {
                showMessage(data.message || "清空日志失败", 'error');
            }
        } catch (error) {
            // 错误已由apiRequest处理
        } finally {
            elements.clearLogBtn.disabled = false;
            // 3秒后清除消息
            setTimeout(() => {
                elements.messageArea.innerHTML = '';
            }, 3000);
        }
    }

    // 初始化持仓数据函数
    async function handleInitHoldings() {
        if (!confirm("⚠️ 危险操作警告！\n\n确定要初始化持仓数据吗？\n此操作将从QMT重新同步所有持仓数据。")) return;

        // 二次确认
        if (!confirm("再次确认：您真的要执行初始化持仓数据吗？")) return;

        // 更新API基础URL
        updateApiBaseUrl();

        // 先验证表单数据
        if (!validateForm()) {
            showMessage("请检查配置参数，修正错误后再初始化持仓", 'error');
            return;
        }

        elements.initHoldingsBtn.disabled = true;
        const originalText = elements.initHoldingsBtn.textContent;
        elements.initHoldingsBtn.textContent = "初始化中...";
        showMessage("正在初始化持仓数据...", 'loading', 0);

        try {
            const configData = getConfigData();
            const data = await apiRequest(API_ENDPOINTS.initHoldings, {                
                method: 'POST',
                body: JSON.stringify(configData),
            });
            
            if (data.status === 'success') {
                showMessage(data.message || "持仓数据初始化成功", 'success');
                
                // 重置请求锁定状态
                requestLocks.holdings = false;
                
                // 强制刷新持仓数据和账户状态
                await fetchHoldings(); 
                await fetchStatus();
            } else {
                showMessage(data.message || "初始化持仓数据失败", 'error');
            }
        } catch (error) {
            // 错误已由apiRequest处理
        } finally {
            elements.initHoldingsBtn.disabled = false;
            elements.initHoldingsBtn.textContent = originalText;
            // 3秒后清除消息
            setTimeout(() => {
                elements.messageArea.innerHTML = '';
            }, 3000);
        }
    }

    // 通用操作处理
    async function handleGenericAction(button, endpoint, confirmationMessage) {
        if (confirmationMessage && !confirm(confirmationMessage)) return;

        button.disabled = true;
        const originalText = button.textContent;
        button.textContent = "处理中...";
        showMessage("正在执行操作...", 'loading', 0);

        try {
            const data = await apiRequest(endpoint, { method: 'POST' });            
            
            if (data.status === 'success') {
                showMessage(data.message || "操作成功", 'success');
                
                // 重置请求锁定状态
                requestLocks.holdings = false;
                requestLocks.logs = false;
                
                // 根据操作类型刷新相关数据
                if (endpoint === API_ENDPOINTS.clearCurrentData || endpoint === API_ENDPOINTS.clearBuySellData) {
                    await fetchHoldings(); // 刷新持仓数据
                }
                if (endpoint === API_ENDPOINTS.importSavedData) {
                    await fetchAllData(); // 导入数据后刷新所有数据
                }
            } else {
                showMessage(data.message || "操作失败", 'error');
            }
        } catch (error) {
            // 错误已由apiRequest处理
        } finally {
            button.disabled = false;
            button.textContent = originalText;
            // 3秒后清除消息
            setTimeout(() => {
                elements.messageArea.innerHTML = '';
            }, 3000);
        }
    }

    async function handleExecuteBuy() {
        // 先验证交易量
        const quantity = parseInt(elements.buyQuantity.value) || 0;
        if (quantity <= 0) {
            showMessage("请输入有效的买入数量", "error");
            return;
        }
        
        const strategy = elements.buyStrategy.value;
        
        // 根据不同策略显示不同对话框
        if (strategy === 'random_pool') {
            await handleRandomPoolBuy(quantity);
        } else if (strategy === 'custom_stock') {
            handleCustomStockBuy(quantity);
        }
    }

    // --- 轮询机制 - 修改后确保只依赖于isMonitoring状态 ---
    function startPolling() {
        if (pollingIntervalId) {
            console.log("已存在轮询，停止旧轮询");
            clearInterval(pollingIntervalId);
            pollingIntervalId = null;
        }

        // 设置适当的轮询间隔
        POLLING_INTERVAL = isPageActive ? ACTIVE_POLLING_INTERVAL : INACTIVE_POLLING_INTERVAL;

        // 确保轮询间隔至少为3秒
        const actualInterval = Math.max(POLLING_INTERVAL, 3000);

        console.log(`Starting data polling with interval: ${actualInterval}ms`);

        // 先立即轮询一次
        pollData();

        pollingIntervalId = setInterval(pollData, actualInterval);

        console.log(`Polling started with interval: ${actualInterval}ms`);  
    }

    function stopPolling() {
        if (!pollingIntervalId) return; // 未在轮询
        console.log("Stopping data polling.");
        clearInterval(pollingIntervalId);
        pollingIntervalId = null;
    }

    async function pollData() {
        if (!isMonitoring) {
            console.log("Monitor is off, stopping polling");
            stopPolling();
            return;
        }
        
        console.log("Polling for data updates...");
    
        try {
            const now = Date.now();
            
            // 只轮询状态和日志，持仓数据主要靠SSE推送
            if (!requestLocks.status && now - lastDataUpdateTimestamps.status >= 10000) { // 增加到10秒
                await fetchStatus();
                await new Promise(r => setTimeout(r, 200));
            }
            
            if (!requestLocks.logs && now - lastDataUpdateTimestamps.logs >= 10000) { // 增加到10秒
                await fetchLogs();
            }
            
            // 持仓数据降低轮询频率，主要依赖SSE推送
            if (!requestLocks.holdings && now - lastDataUpdateTimestamps.holdings >= 10000) { // 轮询兜底间隔缩短为10秒
                await fetchHoldings();
            }
            
        } catch (error) {
            console.error("Poll cycle error:", error);
        }
    
        console.log("Polling cycle finished.");
    }

    // --- 浏览器性能检测 ---
    function checkBrowserPerformance() {
        // 检测帧率
        let lastTime = performance.now();
        let frames = 0;
        let fps = 0;

        function checkFrame() {
            frames++;
            const time = performance.now();
            
            if (time > lastTime + 1000) {
                fps = Math.round((frames * 1000) / (time - lastTime));
                console.log(`Current FPS: ${fps}`);
                
                // 根据帧率调整UI更新策略
                if (fps < 30) {
                    // 低性能模式
                    document.body.classList.add('low-performance-mode');
                    // 减少动画和视觉效果
                    POLLING_INTERVAL = Math.max(POLLING_INTERVAL, 10000); // 降低轮询频率
                    if (pollingIntervalId) {
                        stopPolling();
                        startPolling();
                    }
                } else {
                    document.body.classList.remove('low-performance-mode');
                }
                
                frames = 0;
                lastTime = time;
            }
            
            requestAnimationFrame(checkFrame);
        }

        requestAnimationFrame(checkFrame);
    }

    // --- SSE连接 - 修改后确保不混淆两种状态 ---
    function initSSE() {
        if (sseConnection) {
            sseConnection.close();
        }

        const sseURL = `${API_BASE_URL}/api/sse`;
        sseConnection = new EventSource(sseURL);

        // SSE心跳检测：记录最后一次收到消息的时间
        let lastSSEMessageTime = Date.now();

        sseConnection.onmessage = function(event) {
            try {
                // 更新心跳时间
                lastSSEMessageTime = Date.now();

                const data = JSON.parse(event.data);
                console.log('SSE update received:', data);

                // 更新账户信息
                if (data.account_info) {
                    updateQuickAccountInfo(data.account_info);
                }

                // 更新监控状态
                if (data.monitoring) {
                    updateMonitoringInfo(data.monitoring);
                }

                // 处理持仓数据变化通知
                if (data.positions_update && data.positions_update.changed) {
                    console.log(`Received positions update notification: v${data.positions_update.version}`);
                    // 立即获取最新持仓数据
                    setTimeout(() => {
                        if (!requestLocks.holdings) {
                            fetchHoldings();
                        }
                    }, 100); // 短暂延迟避免冲突
                }

            } catch (e) {
                console.error('SSE data parse error:', e);
            }
        };

        sseConnection.onerror = function(error) {
            console.error('SSE connection error:', error);
            setTimeout(() => {
                initSSE();
            }, 5000); // 减少重连时间到5秒
        };

        // SSE心跳检测：每10秒检查SSE是否存活
        setInterval(() => {
            const elapsed = Date.now() - lastSSEMessageTime;
            if (elapsed > 15000) {  // 15秒没收到消息
                console.warn(`⚠️ SSE heartbeat timeout (${Math.round(elapsed/1000)}s), reconnecting...`);
                if (sseConnection) {
                    sseConnection.close();
                }
                initSSE();
            }
        }, 10000);
    }

    // --- 事件监听器 ---
    elements.toggleMonitorBtn.addEventListener('click', handleToggleMonitor);
    elements.saveConfigBtn.addEventListener('click', handleSaveConfig);
    elements.clearLogBtn.addEventListener('click', handleClearLogs);
    elements.clearCurrentDataBtn.addEventListener('click', () => handleGenericAction(
        elements.clearCurrentDataBtn,
        API_ENDPOINTS.clearCurrentData,
        "⚠️ 警告：确定要清空当前数据吗？\n\n此操作不可撤销！"
    ));
    elements.clearBuySellDataBtn.addEventListener('click', async () => {
        // 危险操作：需要二次确认
        if (!confirm("⚠️⚠️⚠️ 危险操作警告！⚠️⚠️⚠️\n\n您即将清空所有买入/卖出数据！\n这将删除所有交易记录和持仓信息。\n\n此操作不可撤销！确定继续吗？")) return;

        if (!confirm("最后确认：您真的要清空所有买入/卖出数据吗？")) return;

        await handleGenericAction(
            elements.clearBuySellDataBtn,
            API_ENDPOINTS.clearBuySellData,
            null // 已经确认过了，不需要再次确认
        );
    });
    elements.importDataBtn.addEventListener('click', () => handleGenericAction(
        elements.importDataBtn,
        API_ENDPOINTS.importSavedData,
        "确定要导入已保存的数据吗？当前设置和持仓将被覆盖。"
    ));
    elements.initHoldingsBtn.addEventListener('click', handleInitHoldings);
    elements.executeBuyBtn.addEventListener('click', handleExecuteBuy);

    // 持仓表格"全选"复选框监听器
    elements.selectAllHoldings.addEventListener('change', (event) => {
        const isChecked = event.target.checked;
        const checkboxes = elements.holdingsTableBody.querySelectorAll('.holding-checkbox');
        checkboxes.forEach(cb => cb.checked = isChecked);
    });

    // IP/端口变化监听器
    elements.totalAccounts.addEventListener('change', updateApiBaseUrl);
    elements.connectPort.addEventListener('change', updateApiBaseUrl);

    // --- 初始数据加载 ---
    async function fetchAllData() {
        // 初始化API基础URL
        updateApiBaseUrl();

        showMessage("正在加载初始数据...", 'loading', 0);

        try {
            // 顺序加载而非并行，避免过多并发请求
            await fetchConfig();
            await new Promise(r => setTimeout(r, 200));
            
            await fetchStatus();
            await new Promise(r => setTimeout(r, 200));
            
            await fetchHoldings();
            await new Promise(r => setTimeout(r, 200));
            
            await fetchLogs();
            
            showMessage("数据加载完成", 'success', 2000);
        } catch (error) {
            showMessage("部分数据加载失败", 'error', 3000);
        }

        // 如果监控状态为开启，则自动启动轮询
        if (isMonitoring) {
            startPolling();
        }

        // 启动SSE
        setTimeout(() => {
            initSSE();
        }, 1000);

        // 检测浏览器性能
        setTimeout(checkBrowserPerformance, 5000);

        // 开始API连接检查
        setTimeout(throttledCheckApiConnection, 2000);
    }

    // ============ 网格交易相关函数 ============

    // ⭐ 事件处理器存储变量（函数级作用域，用于移除旧监听器）
    let gridConfirmHandler = null;
    let gridCancelHandler = null;

    /**
     * 显示网格交易配置对话框
     * @param {string} stockCode - 股票代码
     */
    async function showGridConfigDialog(stockCode) {
        // 显示loading覆盖层
        const loadingOverlay = document.getElementById('gridConfigLoading');
        if (loadingOverlay) {
            loadingOverlay.classList.remove('hidden');
        }

        try {
            // 从DOM中获取持仓信息
            const row = document.querySelector(`tr[data-stock-code="${stockCode}"]`);
            if (!row) {
                showMessage('未找到该股票持仓信息', 'error');
                // 隐藏loading覆盖层
                if (loadingOverlay) {
                    loadingOverlay.classList.add('hidden');
                }
                // 恢复checkbox状态
                const checkbox = document.querySelector(`.holding-checkbox[data-stock-code="${stockCode}"]`);
                if (checkbox) {
                    const hasActiveGrid = gridTradingStatus[stockCode]?.status === 'active';
                    checkbox.checked = hasActiveGrid;
                }
                return;
            }

            // 从DOM中提取价格信息
            const cells = row.querySelectorAll('td');
            const currentPrice = cells[4] ? parseFloat(cells[4].textContent) : 0;
            const costPrice = cells[5] ? parseFloat(cells[5].textContent) : 0;

            // ⭐ 价格容错处理：支持盘前时间为0的情况
            let centerPrice = currentPrice;

            if (centerPrice <= 0) {
                // 尝试使用成本价作为中心价
                if (costPrice > 0) {
                    centerPrice = costPrice;
                    showMessage(`当前价格不可用，使用成本价作为网格中心价`, 'warning');
                } else {
                    // 两种价格都不可用，允许继续但不设置默认中心价
                    centerPrice = 0;
                    priceSource = '待手动设置';
                    showMessage(`当前价格和成本价都不可用，请在配置对话框中手动设置网格中心价`, 'warning');
                }
            }

            // ⭐ 使用新的 /api/grid/session/<stock_code> API 直接获取该股票的会话状态
            const sessionResponse = await fetch(`${API_BASE_URL}/api/grid/session/${stockCode}`);
            if (!sessionResponse.ok) {
                throw new Error('获取网格会话状态失败');
            }

            const sessionData = await sessionResponse.json();

            if (!sessionData.success) {
                throw new Error(sessionData.error || '获取网格会话状态失败');
            }

            // 隐藏loading覆盖层
            if (loadingOverlay) {
                loadingOverlay.classList.add('hidden');
            }

            const hasActiveSession = sessionData.has_session;
            let config = sessionData.config;  // ⭐ API直接返回配置（小数格式，前端乘以100显示）
            const activeSessionId = sessionData.session_id;

            // ⭐ 调试日志
            console.log('网格会话数据:', {
                stockCode,
                hasActiveSession,
                activeSessionId,
                config
            });

            // 填充对话框信息
            document.getElementById('gridStockCode').textContent = stockCode;

            // ⭐ 显示实时市场价（移除来源说明，界面已明确标注）
            const centerPriceInput = document.getElementById('gridCenterPriceInput');

            // ⭐ 优化: 如果有active session，输入框显示已保存的center_price，否则显示实时市场价
            if (hasActiveSession && config && config.center_price) {
                // 有active session: 显示已保存的配置
                document.getElementById('gridCurrentPrice').textContent = `¥${centerPrice.toFixed(2)}`;
                centerPriceInput.value = parseFloat(config.center_price).toFixed(2);
            } else if (centerPrice > 0) {
                // 无active session: 显示实时市场价
                document.getElementById('gridCurrentPrice').textContent = `¥${centerPrice.toFixed(2)}`;
                centerPriceInput.value = centerPrice.toFixed(2);
            } else {
                document.getElementById('gridCurrentPrice').textContent = `价格不可用`;
                centerPriceInput.value = '';
                centerPriceInput.placeholder = '请手动输入网格中心价格';
            }

            // ⭐ 如果存在active session，显示原网格中心价
            const existingCenterPriceRow = document.getElementById('gridExistingCenterPriceRow');
            const existingCenterPriceSpan = document.getElementById('gridExistingCenterPrice');
            if (hasActiveSession && config && config.center_price) {
                existingCenterPriceRow.style.display = 'block';
                existingCenterPriceSpan.textContent = `¥${parseFloat(config.center_price).toFixed(2)}`;
            } else {
                existingCenterPriceRow.style.display = 'none';
            }

            // ⭐ 验证config对象完整性，如果缺失字段使用默认值
            const defaultConfig = {
                price_interval: 0.05,  // 5%
                position_ratio: 0.25,  // 25%
                callback_ratio: 0.05,  // 5%
                max_investment: 10000,
                duration_days: 30,
                max_deviation: 0.10,  // 10%
                target_profit: 0.10,  // 10%
                stop_loss: -0.10  // -10%
            };

            // 如果config不存在或为空，使用默认配置
            if (!config || typeof config !== 'object') {
                console.warn('配置数据缺失，使用默认配置');
                config = defaultConfig;
            } else {
                // 合并配置，确保所有字段都存在
                config = { ...defaultConfig, ...config };
            }

            // ⭐ 配置是小数格式，乘以100转换为百分比显示
            document.getElementById('gridPriceInterval').value = (config.price_interval * 100).toFixed(2);
            document.getElementById('gridPositionRatio').value = (config.position_ratio * 100).toFixed(2);
            document.getElementById('gridCallbackRatio').value = (config.callback_ratio * 100).toFixed(2);
            document.getElementById('gridMaxInvestment').value = config.max_investment;
            document.getElementById('gridDurationDays').value = config.duration_days;
            document.getElementById('gridMaxDeviation').value = (config.max_deviation * 100).toFixed(0);
            document.getElementById('gridTargetProfit').value = (config.target_profit * 100).toFixed(0);
            document.getElementById('gridStopLoss').value = (config.stop_loss * 100).toFixed(0);  // ⭐ 负数转换为百分比

            // 显示对话框
            const dialog = document.getElementById('gridConfigDialog');
            dialog.classList.remove('hidden');

            // ⭐ 立即恢复checkbox的正确状态（防止点击时的状态切换）
            const checkbox = document.querySelector(`.holding-checkbox[data-stock-code="${stockCode}"]`);
            if (checkbox) {
                const hasActiveGrid = gridTradingStatus[stockCode]?.status === 'active';
                checkbox.checked = hasActiveGrid;
            }

            // ⭐ 绑定按钮事件（优雅方式：先移除旧监听器，再添加新监听器）
            const confirmBtn = document.getElementById('gridDialogConfirmBtn');
            const cancelBtn = document.getElementById('gridDialogCancelBtn');

            // 1. 移除旧的事件监听器（如果存在）
            if (gridConfirmHandler) {
                confirmBtn.removeEventListener('click', gridConfirmHandler);
            }
            if (gridCancelHandler) {
                cancelBtn.removeEventListener('click', gridCancelHandler);
            }

            // 2. 根据是否有active session来创建确认按钮的处理函数
            if (hasActiveSession) {
                // 有active session，按钮显示"停止网格交易"
                confirmBtn.textContent = '停止网格交易';
                confirmBtn.classList.remove('bg-blue-600', 'hover:bg-blue-700');
                confirmBtn.classList.add('bg-red-600', 'hover:bg-red-700');

                // 创建命名函数用于停止网格
                gridConfirmHandler = async () => {
                    // ⭐ 使用新的灵活API，支持通过session_id停止
                    await stopGridSessionById(activeSessionId, stockCode);

                    dialog.classList.add('hidden');
                    fetchHoldings(); // 立即刷新持仓数据
                };
            } else {
                // 没有active session，按钮显示"启动网格交易"
                confirmBtn.textContent = '启动网格交易';
                confirmBtn.classList.remove('bg-red-600', 'hover:bg-red-700');
                confirmBtn.classList.add('bg-blue-600', 'hover:bg-blue-700');

                // 创建命名函数用于启动网格
                gridConfirmHandler = async () => {
                    // ⭐ 从输入框读取用户输入的中心价格（允许用户手动修改）
                    const userInputPrice = parseFloat(document.getElementById('gridCenterPriceInput').value);
                    await startGridSession(stockCode, userInputPrice);
                };
            }

            // 3. 创建取消按钮的处理函数
            gridCancelHandler = () => {
                dialog.classList.add('hidden');

                // ⭐ 立即恢复checkbox的正确状态（避免刷新延迟导致的状态不一致）
                const checkbox = document.querySelector(`.holding-checkbox[data-stock-code="${stockCode}"]`);
                if (checkbox) {
                    const hasActiveGrid = gridTradingStatus[stockCode]?.status === 'active';
                    checkbox.checked = hasActiveGrid;
                }

                // 刷新持仓数据以确保所有状态同步
                fetchHoldings();
            };

            // 4. 添加新的事件监听器
            confirmBtn.addEventListener('click', gridConfirmHandler);
            cancelBtn.addEventListener('click', gridCancelHandler);

        } catch (error) {
            // 隐藏loading覆盖层
            if (loadingOverlay) {
                loadingOverlay.classList.add('hidden');
            }

            console.error('显示网格配置对话框失败:', error);
            showMessage('显示配置对话框失败: ' + error.message, 'error');
            // 刷新持仓数据以确保状态一致
            fetchHoldings();
        }
    }

    /**
     * 更新网格交易checkbox样式和状态（优化版：简化逻辑）
     * @param {string} stockCode - 股票代码
     * @param {string} status - 状态: 'active'(绿色), 'paused'(黄色), 'stopped'(红色), 'none'(默认)
     */
    function updateGridCheckboxStyle(stockCode, status) {
        const checkbox = document.querySelector(`.holding-checkbox[data-stock-code="${stockCode}"]`);
        if (!checkbox) return;

        // 移除所有状态类
        checkbox.classList.remove('grid-active', 'grid-paused', 'grid-stopped');

        // 根据状态添加类和样式，并同步checked属性
        switch(status) {
            case 'active':
                checkbox.classList.add('grid-active');
                checkbox.checked = true;  // ⭐ 同步checked状态
                break;
            case 'paused':
                checkbox.classList.add('grid-paused');
                checkbox.checked = true;  // ⭐ 暂停状态也保持勾选
                break;
            case 'stopped':
                checkbox.classList.add('grid-stopped');
                checkbox.checked = false;  // ⭐ 停止后取消勾选
                break;
            default:
                // 默认状态
                checkbox.checked = false;  // ⭐ 无会话时取消勾选
        }
    }

    // ============ 加载风险模板 ============
    async function loadRiskTemplates() {
        try {
            const response = await fetch('/api/grid/risk-templates');
            const data = await response.json();

            if (data.success) {
                riskTemplates = data.templates;
                console.log('✅ 风险模板加载成功', riskTemplates);
            } else {
                console.error('❌ 风险模板加载失败:', data.error);
            }
        } catch (error) {
            console.error('❌ 加载风险模板异常:', error);
        }
    }

    // ============ 风险等级切换时自动填充参数 ============
    function applyRiskTemplate() {
        const riskLevel = document.getElementById('riskLevel').value;
        const template = riskTemplates[riskLevel];

        if (!template) {
            console.warn('模板不存在:', riskLevel);
            return;
        }

        console.log('应用风险模板:', riskLevel, template);

        // 自动填充表单参数 (小数转百分比)
        document.getElementById('gridPriceInterval').value = (template.price_interval * 100).toFixed(1);
        document.getElementById('gridPositionRatio').value = (template.position_ratio * 100).toFixed(0);
        document.getElementById('gridCallbackRatio').value = (template.callback_ratio * 100).toFixed(2);
        document.getElementById('gridMaxDeviation').value = (template.max_deviation * 100).toFixed(0);
        document.getElementById('gridTargetProfit').value = (template.target_profit * 100).toFixed(0);
        document.getElementById('gridStopLoss').value = (template.stop_loss * 100).toFixed(0);
        document.getElementById('gridDurationDays').value = template.duration_days;

        // 更新风险描述
        const descriptions = {
            'aggressive': '🚀 适合高波动成长股,档位密集(3%),容忍大回撤(-15%),追求高收益(+15%)',
            'moderate': '⚖️ 适合主流蓝筹股,平衡风险收益,默认推荐策略',
            'conservative': '🛡️ 适合低波动指数或大盘股,档位稀疏(8%),快速止损(-8%),稳健盈利(+8%)'
        };
        document.getElementById('riskDescription').textContent = descriptions[riskLevel] || template.description;

        // 视觉反馈动画
        const selector = document.querySelector('.risk-level-selector');
        selector.classList.add('pulsing');
        setTimeout(() => selector.classList.remove('pulsing'), 500);
    }

    /**
     * 启动网格交易会话
     * @param {string} stockCode - 股票代码
     * @param {number} centerPrice - 中心价格
     */
    async function startGridSession(stockCode, centerPrice) {
        try {
            // 收集配置参数(转换百分比为小数)
            const config = {
                risk_level: document.getElementById('riskLevel').value,  // ⚠️ 新增风险等级
                price_interval: parseFloat(document.getElementById('gridPriceInterval').value) / 100,
                position_ratio: parseFloat(document.getElementById('gridPositionRatio').value) / 100,
                callback_ratio: parseFloat(document.getElementById('gridCallbackRatio').value) / 100,
                max_investment: parseFloat(document.getElementById('gridMaxInvestment').value),
                max_deviation: parseFloat(document.getElementById('gridMaxDeviation').value) / 100,
                target_profit: parseFloat(document.getElementById('gridTargetProfit').value) / 100,
                stop_loss: parseFloat(document.getElementById('gridStopLoss').value) / 100  // ⭐ 前端也用负数，直接除以100
            };

            const durationDays = parseInt(document.getElementById('gridDurationDays').value);

            // 验证参数
            if (config.price_interval <= 0 || config.price_interval > 0.2) {
                showMessage('网格价格间隔必须在0.01%-20%之间', 'error');
                return;
            }
            if (config.position_ratio <= 0 || config.position_ratio > 1) {
                showMessage('每档交易比例必须在1%-100%之间', 'error');
                return;
            }
            if (config.callback_ratio < 0.001 || config.callback_ratio > 0.1) {
                showMessage('回调触发比例必须在0.1%-10%之间', 'error');
                return;
            }
            if (config.max_investment < 0) {
                showMessage('最大追加投入不能为负数', 'error');
                return;
            }
            if (durationDays < 1 || durationDays > 365) {
                showMessage('运行时长必须在1-365天之间', 'error');
                return;
            }

            // ⭐ 价格验证：支持使用成本价作为后备
            console.log(`价格验证 - 中心价格: ${centerPrice}, 类型: ${typeof centerPrice}`);

            if (!centerPrice || centerPrice <= 0) {
                console.error('价格验证失败:', { centerPrice, stockCode });
                showMessage('无法获取有效价格（当前价和成本价都不可用），请在交易时间或确保有持仓成本价后重试，或者手动输入中心价格', 'error');
                return;
            }

            console.log(`✅ 启动网格交易 - 股票: ${stockCode}, 中心价格: ¥${centerPrice.toFixed(2)}`);

            const response = await fetch(`${API_BASE_URL}/api/grid/start`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    stock_code: stockCode,
                    center_price: centerPrice,
                    duration_days: durationDays,
                    config: config
                })
            });

            const result = await response.json();

            if (!response.ok) {
                throw new Error(result.error || '启动网格交易失败');
            }

            // ⚠️ 新增: 显示风险等级信息
            const riskNames = {
                'aggressive': '激进型',
                'moderate': '稳健型',
                'conservative': '保守型'
            };
            showMessage(`✅ ${result.message || '网格交易启动成功'}\n会话ID: ${result.session_id}\n风险等级: ${riskNames[result.risk_level] || result.risk_level}`, 'success');

            // 关闭对话框
            document.getElementById('gridConfigDialog').classList.add('hidden');

            // ⭐ 立即更新checkbox状态（使用独立API，不依赖持仓刷新）
            await updateSingleGridCheckboxStatus(stockCode);

            // 刷新持仓数据（确保所有数据一致）
            await fetchHoldings();

        } catch (error) {
            console.error('启动网格交易失败:', error);
            showMessage('启动网格交易失败: ' + error.message, 'error');

            // 刷新持仓数据以确保状态一致
            await fetchHoldings();
        }
    }

    /**
     * 停止指定ID的网格交易会话
     * @param {number} sessionId - 会话ID
     * @param {string} stockCode - 股票代码
     */
    async function stopGridSessionById(sessionId, stockCode) {
        try {
            // 调用停止API
            const response = await fetch(`${API_BASE_URL}/api/grid/stop/${sessionId}`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                }
            });

            if (!response.ok) {
                const errorData = await response.json();
                throw new Error(errorData.error || '停止网格交易失败');
            }

            const result = await response.json();
            showMessage(`网格交易已停止 (会话ID: ${sessionId})`, 'success');

            // 关闭对话框
            document.getElementById('gridConfigDialog').classList.add('hidden');

            // ⭐ 立即更新checkbox状态（使用独立API，不依赖持仓刷新）
            await updateSingleGridCheckboxStatus(stockCode);

            // ⭐ 延迟1秒后刷新持仓数据（给后端时间更新数据库）
            setTimeout(async () => {
                await fetchHoldings();
            }, 1000);

        } catch (error) {
            console.error('停止网格交易失败:', error);
            showMessage('停止网格交易失败: ' + error.message, 'error');
            // 刷新持仓数据并同步网格交易状态（确保checkbox与后端状态一致）
            await fetchHoldings();
            await updateAllGridTradingStatus();
        }
    }

    async function stopGridSession(stockCode) {
        try {
            // 先获取该股票的会话ID
            const sessionsResponse = await fetch(`${API_BASE_URL}/api/grid/sessions`);
            if (!sessionsResponse.ok) {
                throw new Error('获取网格会话列表失败');
            }

            const sessionsData = await sessionsResponse.json();
            if (!sessionsData.success || !Array.isArray(sessionsData.sessions)) {
                throw new Error('网格会话数据格式错误');
            }

            // 查找该股票的运行中会话
            const session = sessionsData.sessions.find(s => s.stock_code === stockCode && s.status === 'active');
            if (!session) {
                showMessage('未找到该股票的运行中网格会话', 'warning');
                // 刷新持仓数据以同步状态
                await fetchHoldings();
                return;
            }

            // 调用stopGridSessionById
            await stopGridSessionById(session.session_id, stockCode);

        } catch (error) {
            console.error('停止网格交易失败:', error);
            showMessage('停止网格交易失败: ' + error.message, 'error');
            // 刷新持仓数据并同步网格交易状态（确保checkbox与后端状态一致）
            await fetchHoldings();
            await updateAllGridTradingStatus();
        }
    }

    /**
     * 更新所有网格交易状态（使用单个股票API逐个查询）
     * 定期从服务器获取最新状态并更新UI
     * ⭐ 优化点：checkbox状态与持仓数据完全解耦，独立更新
     */
    async function updateAllGridTradingStatus() {
        try {
            // ⭐ 获取所有持仓股票的checkbox元素
            const checkboxes = elements.holdingsTableBody.querySelectorAll('.holding-checkbox');
            if (checkboxes.length === 0) {
                console.log('[Grid] 没有持仓股票，跳过checkbox状态更新');
                return;
            }

            // 先清除所有本地状态（准备全量更新）
            const previousStates = {...gridTradingStatus};
            const currentStates = {};

            // ⭐ 对每个持仓股票单独调用 /api/grid/session/<stock_code> 接口
            for (const checkbox of checkboxes) {
                const stockCode = checkbox.dataset.stockCode;
                if (!stockCode) continue;

                try {
                    const response = await fetch(`${API_BASE_URL}/api/grid/session/${stockCode}`);
                    if (!response.ok) {
                        console.warn(`[Grid] 获取${stockCode}的session状态失败`);
                        continue;
                    }

                    const data = await response.json();
                    if (!data.success) {
                        console.warn(`[Grid] 获取${stockCode}的session状态失败:`, data.error);
                        continue;
                    }

                    // ⭐ 根据返回的 has_session 字段更新checkbox状态
                    const hasActiveSession = data.has_session === true;

                    if (hasActiveSession) {
                        // 有活跃session
                        gridTradingStatus[stockCode] = {
                            sessionId: data.session_id,
                            status: 'active',
                            lastUpdate: Date.now()
                        };
                        currentStates[stockCode] = true;
                        updateGridCheckboxStyle(stockCode, 'active');
                    } else {
                        // 无活跃session
                        currentStates[stockCode] = false;
                        if (previousStates[stockCode]) {
                            updateGridCheckboxStyle(stockCode, 'none');
                            delete gridTradingStatus[stockCode];
                        }
                    }
                } catch (error) {
                    console.error(`[Grid] 获取${stockCode}的session状态异常:`, error);
                }
            }

            // 检查是否有本地状态但当前没有的（说明session已停止）
            Object.keys(previousStates).forEach(stockCode => {
                if (!currentStates[stockCode]) {
                    updateGridCheckboxStyle(stockCode, 'none');
                    delete gridTradingStatus[stockCode];
                }
            });

            console.log(`[Grid] checkbox状态已更新，共查询${checkboxes.length}只股票`);

        } catch (error) {
            console.error('更新网格交易状态失败:', error);
        }
    }

    /**
     * 更新单个股票的checkbox状态（用于启动/停止后立即更新）
     * @param {string} stockCode - 股票代码
     */
    async function updateSingleGridCheckboxStatus(stockCode) {
        try {
            const response = await fetch(`${API_BASE_URL}/api/grid/checkbox-state/${stockCode}`);
            if (!response.ok) {
                console.warn(`获取${stockCode}的checkbox状态失败`);
                return;
            }

            const data = await response.json();
            if (!data.success) {
                console.warn(`获取${stockCode}的checkbox状态失败:`, data.error);
                return;
            }

            // 更新本地状态
            if (data.active) {
                gridTradingStatus[stockCode] = {
                    sessionId: data.session_id,
                    status: 'active',
                    lastUpdate: Date.now()
                };
                updateGridCheckboxStyle(stockCode, 'active');
            } else {
                delete gridTradingStatus[stockCode];
                updateGridCheckboxStyle(stockCode, 'none');
            }

            // 更新版本号
            localStorage.setItem('gridCheckboxVersion', data.version);

            console.log(`[Grid] ${stockCode} checkbox状态已更新: ${data.active ? 'active' : 'inactive'}`);

        } catch (error) {
            console.error(`更新${stockCode}的checkbox状态失败:`, error);
        }
    }

    // ======================= 网格模板管理功能已移除 =======================

    // ======================= 网格Tooltip功能 =======================

    // ============ 显示Tooltip ============
    async function showGridTooltip(event, stockCode) {
        const tooltip = document.getElementById('gridTooltip');

        // 检查缓存
        const cached = tooltipDataCache[stockCode];
        const now = Date.now();

        if (cached && (now - cached.timestamp < TOOLTIP_CACHE_TIME)) {
            // 使用缓存数据
            updateTooltipContent(cached.data);
        } else {
            // 请求新数据
            try {
                const response = await fetch(`${API_BASE_URL}/api/grid/session/${stockCode}`);
                const data = await response.json();

                if (!data.success || !data.has_session) {
                    return;  // 无会话,不显示tooltip
                }

                // 缓存数据
                tooltipDataCache[stockCode] = {
                    data: data,
                    timestamp: now
                };

                updateTooltipContent(data);
            } catch (error) {
                console.error('加载Tooltip数据失败:', error);
                return;
            }
        }

        // 定位tooltip
        const rect = event.target.getBoundingClientRect();
        tooltip.style.left = `${rect.left + window.scrollX}px`;
        tooltip.style.top = `${rect.bottom + window.scrollY + 10}px`;
        tooltip.style.display = 'block';
    }

    // ============ 更新Tooltip内容 ============
    function updateTooltipContent(data) {
        // 风险等级徽章
        const riskLevel = data.risk_level || 'moderate';
        const riskNames = {
            'aggressive': '激进型',
            'moderate': '稳健型',
            'conservative': '保守型'
        };

        const badgeElement = document.getElementById('tooltipRiskLevel');
        badgeElement.textContent = riskNames[riskLevel];
        badgeElement.className = `tooltip-risk-badge ${riskLevel}`;

        // 股票代码
        document.getElementById('tooltipStockCode').textContent = data.config?.stock_code || '未知';

        // 运行时长
        if (data.stats && data.stats.start_time) {
            const duration = calculateDuration(data.stats.start_time, new Date());
            document.getElementById('tooltipDuration').textContent = duration;
        } else {
            document.getElementById('tooltipDuration').textContent = '计算中...';
        }

        // 网格盈亏
        if (data.stats) {
            const profitRatio = data.stats.profit_ratio || 0;
            const profitElement = document.getElementById('tooltipProfit');
            const profitSign = profitRatio >= 0 ? '+' : '';
            profitElement.textContent = `${profitSign}${profitRatio.toFixed(2)}%`;
            profitElement.className = profitRatio >= 0 ? 'tooltip-value profit' : 'tooltip-value loss';
        }

        // 交易次数
        if (data.stats) {
            const buyCount = data.stats.buy_count || 0;
            const sellCount = data.stats.sell_count || 0;
            const total = data.stats.trade_count || (buyCount + sellCount);
            document.getElementById('tooltipTrades').textContent = `${total}次 (买${buyCount}/卖${sellCount})`;
        }

        // 资金使用
        if (data.stats && data.config) {
            const used = data.stats.current_investment || 0;
            const max = data.config.max_investment || 1;
            const percent = ((used / max) * 100).toFixed(0);
            document.getElementById('tooltipInvestment').textContent = `${percent}% (${used.toFixed(0)}/${max.toFixed(0)}元)`;
        }

        // 中心价偏离
        if (data.stats) {
            const deviation = calculateDeviation(
                data.stats.center_price,
                data.stats.current_center_price
            );
            const deviationElement = document.getElementById('tooltipDeviation');
            deviationElement.textContent = `${deviation >= 0 ? '+' : ''}${deviation.toFixed(2)}%`;

            // 根据偏离度设置颜色
            if (Math.abs(deviation) > 10) {
                deviationElement.className = 'tooltip-value warning';
            } else {
                deviationElement.className = 'tooltip-value';
            }
        }
    }

    // ============ 隐藏Tooltip ============
    function hideGridTooltip() {
        document.getElementById('gridTooltip').style.display = 'none';
    }

    // ============ 辅助函数 ============
    function calculateDuration(startTime, currentTime) {
        const diff = currentTime - new Date(startTime);
        const days = Math.floor(diff / (1000 * 60 * 60 * 24));
        const hours = Math.floor((diff % (1000 * 60 * 60 * 24)) / (1000 * 60 * 60));
        return `${days}天${hours}小时`;
    }

    function calculateDeviation(centerPrice, currentPrice) {
        if (!centerPrice || centerPrice === 0) return 0;
        return ((currentPrice - centerPrice) / centerPrice) * 100;
    }

    // 定时清空缓存(可选)
    setInterval(() => {
        tooltipDataCache = {};
        console.log('Tooltip缓存已清空');
    }, 60000);  // 每分钟清空一次

    // ======================= 网格Tooltip功能结束 =======================

    console.log("Adding event listeners and fetching initial data...");

    // ⚠️ 新增: 加载风险模板
    loadRiskTemplates();

    fetchAllData(); // 脚本运行时加载初始数据

    // 暴露函数到全局作用域供HTML内联事件处理器使用
    window.applyRiskTemplate = applyRiskTemplate;
    window.showGridTooltip = showGridTooltip;
    window.hideGridTooltip = hideGridTooltip;
});

console.log("Script loaded");