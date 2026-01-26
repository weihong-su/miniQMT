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

    // 更新现有持仓行
    function updateExistingRow(row, stock) {
        // 更新各个单元格的值
        const cells = row.querySelectorAll('td');

        // 检查是否有活跃的网格会话
        const hasActiveGrid = activeGridSessions.has(stock.stock_code);

        // 更新行的边框样式
        row.className = hasActiveGrid
            ? 'hover:bg-gray-50 even:bg-gray-100 border-l-4 border-l-green-500'
            : 'hover:bg-gray-50 even:bg-gray-100';

        // 更新checkbox和网格状态标识
        const checkboxCell = cells[0];
        const checkbox = checkboxCell.querySelector('.holding-checkbox');
        if (checkbox) {
            checkbox.checked = hasActiveGrid;
        }
        // 更新"运行中"标识
        const existingLabel = checkboxCell.querySelector('span');
        if (hasActiveGrid && !existingLabel) {
            checkboxCell.innerHTML += '<span class="ml-1 text-green-600 text-xs">🟢</span>';
        } else if (!hasActiveGrid && existingLabel) {
            existingLabel.remove();
        }

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

    // 创建新的持仓行
    function createStockRow(stock) {
        const row = document.createElement('tr');
        // 检查是否有活跃的网格会话
        const hasActiveGrid = activeGridSessions.has(stock.stock_code);
        // 如果有活跃网格，添加绿色边框
        row.className = hasActiveGrid
            ? 'hover:bg-gray-50 even:bg-gray-100 border-l-4 border-l-green-500'
            : 'hover:bg-gray-50 even:bg-gray-100';
        row.dataset.stockCode = stock.stock_code; // 添加标识属性

        // 计算关键值
        const changePercentage = parseFloat(stock.change_percentage || 0);
        const profitRatio = parseFloat(stock.profit_ratio || 0);

        // 构建行内容
        row.innerHTML = `
            <td class="border p-2">
                <input type="checkbox" class="holding-checkbox"
                       data-id="${stock.id || stock.stock_code}"
                       data-stock-code="${stock.stock_code}"
                       ${hasActiveGrid ? 'checked' : ''}>
                ${hasActiveGrid ? '<span class="ml-1 text-green-600 text-xs">🟢</span>' : ''}
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
            checkbox.addEventListener('change', (e) => {
                const stockCode = e.target.dataset.stockCode;

                if (e.target.checked) {
                    // 选中时弹出网格交易配置对话框
                    showGridConfigDialog(stockCode);
                } else {
                    // 取消选中时停止网格交易
                    stopGridSession(stockCode);
                }

                // 检查是否所有复选框都被选中
                const allChecked = Array.from(checkboxes).every(cb => cb.checked);
                elements.selectAllHoldings.checked = allChecked;
            });

            // 初始化时应用当前网格交易状态的样式
            const stockCode = checkbox.dataset.stockCode;
            if (gridTradingStatus[stockCode]) {
                const status = gridTradingStatus[stockCode].status;
                updateGridCheckboxStyle(stockCode, status);
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
                    const actionType = entry.trade_type === 'BUY' ? '买入' : 
                                    (entry.trade_type === 'SELL' ? '卖出' : entry.trade_type);
                    
                    // 格式化为要求的格式
                    const formattedPrice = entry.price ? Number(entry.price).toFixed(2) : '';
                    const formattedVolume = entry.volume ? Number(entry.volume).toFixed(0) : '';
                    return `${dateStr}, ${entry.stock_code || ''}, ${entry.stock_name || ''}, ${actionType}, 价格: ${formattedPrice}, 数量: ${formattedVolume}, 策略: ${entry.strategy || ''}`;
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
    // 全局变量存储活跃网格会话
    let activeGridSessions = new Set();

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

            // 获取活跃的网格会话
            try {
                const gridResponse = await fetch(`${API_BASE_URL}/api/grid/sessions`);
                if (gridResponse.ok) {
                    const gridData = await gridResponse.json();
                    if (gridData.success && Array.isArray(gridData.sessions)) {
                        // 更新活跃会话集合（只包含运行中的会话，状态为'active'）
                        activeGridSessions = new Set(
                            gridData.sessions
                                .filter(s => s.status === 'active')
                                .map(s => s.stock_code)
                        );
                        console.log(`Active grid sessions: ${Array.from(activeGridSessions).join(', ')}`);
                    }
                }
            } catch (gridError) {
                console.log('Failed to fetch grid sessions:', gridError);
                // 不影响持仓数据的显示
            }

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
            setTimeout(() => {
                requestLocks.holdings = false;
            }, 1000);
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
        if (!confirm("确定要清空所有日志吗？此操作不可撤销。")) return;
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
        if (!confirm("确定要初始化持仓数据吗？")) return;

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
            if (!requestLocks.holdings && now - lastDataUpdateTimestamps.holdings >= 30000) { // 增加到30秒
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
    
        sseConnection.onmessage = function(event) {
            try {
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
    }

    // --- 事件监听器 ---
    elements.toggleMonitorBtn.addEventListener('click', handleToggleMonitor);
    elements.saveConfigBtn.addEventListener('click', handleSaveConfig);
    elements.clearLogBtn.addEventListener('click', handleClearLogs);
    elements.clearCurrentDataBtn.addEventListener('click', () => handleGenericAction(
        elements.clearCurrentDataBtn,
        API_ENDPOINTS.clearCurrentData,
        "确定要清空当前数据吗？"
    ));
    elements.clearBuySellDataBtn.addEventListener('click', () => handleGenericAction(
        elements.clearBuySellDataBtn,
        API_ENDPOINTS.clearBuySellData,
        "确定要清空买入/卖出数据吗？"
    ));
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

    /**
     * 显示网格交易配置对话框
     * @param {string} stockCode - 股票代码
     */
    async function showGridConfigDialog(stockCode) {
        try {
            // 从DOM中获取持仓信息
            const row = document.querySelector(`tr[data-stock-code="${stockCode}"]`);
            if (!row) {
                showMessage('未找到该股票持仓信息', 'error');
                // 取消checkbox选中状态
                const checkbox = document.querySelector(`.holding-checkbox[data-stock-code="${stockCode}"]`);
                if (checkbox) checkbox.checked = false;
                return;
            }

            // 从DOM中提取当前价格(第5列,索引4)
            const cells = row.querySelectorAll('td');
            const currentPrice = cells[4] ? parseFloat(cells[4].textContent) : 0;

            if (currentPrice <= 0) {
                showMessage('无法获取当前价格', 'error');
                const checkbox = document.querySelector(`.holding-checkbox[data-stock-code="${stockCode}"]`);
                if (checkbox) checkbox.checked = false;
                return;
            }

            // 获取默认配置
            const response = await fetch(`${API_BASE_URL}/api/grid/config`);
            if (!response.ok) {
                throw new Error('获取网格配置失败');
            }
            const result = await response.json();
            const defaultConfig = result.data;  // 从返回的data字段中获取配置

            // 填充对话框信息
            document.getElementById('gridStockCode').textContent = stockCode;
            document.getElementById('gridCurrentPrice').textContent = `¥${currentPrice.toFixed(2)}`;

            // 填充配置参数(转换为百分比显示)
            document.getElementById('gridPriceInterval').value = (defaultConfig.price_interval * 100).toFixed(2);
            document.getElementById('gridPositionRatio').value = (defaultConfig.position_ratio * 100).toFixed(2);
            document.getElementById('gridCallbackRatio').value = (defaultConfig.callback_ratio * 100).toFixed(2);
            document.getElementById('gridMaxInvestment').value = defaultConfig.max_investment;
            document.getElementById('gridDurationDays').value = defaultConfig.duration_days;
            document.getElementById('gridMaxDeviation').value = (defaultConfig.max_deviation * 100).toFixed(0);
            document.getElementById('gridTargetProfit').value = (defaultConfig.target_profit * 100).toFixed(0);
            document.getElementById('gridStopLoss').value = (defaultConfig.stop_loss * 100).toFixed(0);

            // 显示对话框
            const dialog = document.getElementById('gridConfigDialog');
            dialog.classList.remove('hidden');

            // 绑定按钮事件(移除旧事件监听器,避免重复绑定)
            const confirmBtn = document.getElementById('gridDialogConfirmBtn');
            const cancelBtn = document.getElementById('gridDialogCancelBtn');

            const newConfirmBtn = confirmBtn.cloneNode(true);
            const newCancelBtn = cancelBtn.cloneNode(true);

            confirmBtn.parentNode.replaceChild(newConfirmBtn, confirmBtn);
            cancelBtn.parentNode.replaceChild(newCancelBtn, cancelBtn);

            newConfirmBtn.addEventListener('click', () => startGridSession(stockCode, currentPrice));
            newCancelBtn.addEventListener('click', () => {
                dialog.classList.add('hidden');
                // 取消checkbox选中状态
                const checkbox = document.querySelector(`.holding-checkbox[data-stock-code="${stockCode}"]`);
                if (checkbox) checkbox.checked = false;
            });

        } catch (error) {
            console.error('显示网格配置对话框失败:', error);
            showMessage('显示配置对话框失败: ' + error.message, 'error');
            // 取消checkbox选中状态
            const checkbox = document.querySelector(`.holding-checkbox[data-stock-code="${stockCode}"]`);
            if (checkbox) checkbox.checked = false;
        }
    }

    /**
     * 更新网格交易checkbox样式
     * @param {string} stockCode - 股票代码
     * @param {string} status - 状态: 'active'(绿色), 'paused'(黄色), 'stopped'(红色), 'none'(默认)
     */
    function updateGridCheckboxStyle(stockCode, status) {
        const checkbox = document.querySelector(`.holding-checkbox[data-stock-code="${stockCode}"]`);
        if (!checkbox) return;

        // 移除所有状态类
        checkbox.classList.remove('grid-active', 'grid-paused', 'grid-stopped');

        // 根据状态添加类和样式
        switch(status) {
            case 'active':
                checkbox.classList.add('grid-active');
                checkbox.style.backgroundColor = '#22c55e';  // 绿色
                checkbox.style.borderColor = '#16a34a';
                break;
            case 'paused':
                checkbox.classList.add('grid-paused');
                checkbox.style.backgroundColor = '#eab308';  // 黄色
                checkbox.style.borderColor = '#ca8a04';
                break;
            case 'stopped':
                checkbox.classList.add('grid-stopped');
                checkbox.style.backgroundColor = '#ef4444';  // 红色
                checkbox.style.borderColor = '#dc2626';
                break;
            default:
                // 默认状态，移除自定义样式
                checkbox.style.backgroundColor = '';
                checkbox.style.borderColor = '';
        }
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
                price_interval: parseFloat(document.getElementById('gridPriceInterval').value) / 100,
                position_ratio: parseFloat(document.getElementById('gridPositionRatio').value) / 100,
                callback_ratio: parseFloat(document.getElementById('gridCallbackRatio').value) / 100,
                max_investment: parseFloat(document.getElementById('gridMaxInvestment').value),
                max_deviation: parseFloat(document.getElementById('gridMaxDeviation').value) / 100,
                target_profit: parseFloat(document.getElementById('gridTargetProfit').value) / 100,
                stop_loss: parseFloat(document.getElementById('gridStopLoss').value) / 100
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

            // 调用API启动网格交易
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

            if (!response.ok) {
                const errorData = await response.json();
                throw new Error(errorData.error || '启动网格交易失败');
            }

            const result = await response.json();
            showMessage(`网格交易启动成功! 会话ID: ${result.session_id}`, 'success');

            // 保存网格交易状态
            gridTradingStatus[stockCode] = {
                sessionId: result.session_id,
                status: 'active',  // active, paused, stopped
                config: result.config,
                lastUpdate: Date.now()
            };

            // 更新checkbox状态
            updateGridCheckboxStyle(stockCode, 'active');

            // 关闭对话框
            document.getElementById('gridConfigDialog').classList.add('hidden');

            // 刷新持仓数据
            await fetchHoldings();

        } catch (error) {
            console.error('启动网格交易失败:', error);
            showMessage('启动网格交易失败: ' + error.message, 'error');
            // 取消checkbox选中状态
            const checkbox = document.querySelector(`.holding-checkbox[data-stock-code="${stockCode}"]`);
            if (checkbox) checkbox.checked = false;
        }
    }

    /**
     * 停止网格交易会话
     * @param {string} stockCode - 股票代码
     */
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

            // 调用停止API
            const response = await fetch(`${API_BASE_URL}/api/grid/stop/${session.session_id}`, {
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
            showMessage(`网格交易已停止 (会话ID: ${session.session_id})`, 'success');

            // 清除网格交易状态
            delete gridTradingStatus[stockCode];

            // 更新checkbox为停止状态（红色）
            updateGridCheckboxStyle(stockCode, 'stopped');

            // 刷新持仓数据
            await fetchHoldings();

        } catch (error) {
            console.error('停止网格交易失败:', error);
            showMessage('停止网格交易失败: ' + error.message, 'error');
            // 刷新持仓数据以同步状态
            await fetchHoldings();
        }
    }

    /**
     * 更新所有网格交易状态
     * 定期从服务器获取最新状态并更新UI
     */
    async function updateAllGridTradingStatus() {
        try {
            const response = await fetch(`${API_BASE_URL}/api/grid/sessions`);
            if (!response.ok) {
                console.warn('获取网格会话列表失败');
                return;
            }

            const data = await response.json();
            if (!data.success || !Array.isArray(data.sessions)) {
                return;
            }

            // 更新每个活跃会话的状态
            data.sessions.forEach(session => {
                if (session.status === 'active') {
                    gridTradingStatus[session.stock_code] = {
                        sessionId: session.session_id,
                        status: 'active',
                        config: session.config,
                        lastUpdate: Date.now()
                    };
                    updateGridCheckboxStyle(session.stock_code, 'active');
                }
            });

            // 检查是否有session被停止（存在于本地状态但不在服务器响应中）
            Object.keys(gridTradingStatus).forEach(stockCode => {
                const localSession = gridTradingStatus[stockCode];
                const serverSession = data.sessions.find(s => s.stock_code === stockCode);

                // 如果本地有状态但服务器没有对应会话，说明已停止
                if (!serverSession || serverSession.status !== 'active') {
                    updateGridCheckboxStyle(stockCode, 'stopped');
                    // 可以选择删除本地状态或标记为stopped
                    if (!serverSession) {
                        delete gridTradingStatus[stockCode];
                    }
                }
            });

        } catch (error) {
            console.error('更新网格交易状态失败:', error);
        }
    }

    // ======================= 网格模板管理功能 (v1.1) =======================

    /**
     * 加载网格配置模板列表
     */
    async function loadGridTemplates() {
        try {
            const response = await fetch(`${API_BASE_URL}/api/grid/templates`);
            if (!response.ok) throw new Error('加载模板列表失败');

            const result = await response.json();
            if (!result.success) throw new Error(result.error || '加载模板列表失败');

            const select = document.getElementById('gridTemplateSelect');
            // 清空现有选项（保留"自定义配置"）
            select.innerHTML = '<option value="">自定义配置</option>';

            // 添加模板选项
            result.templates.forEach(tpl => {
                const option = document.createElement('option');
                option.value = tpl.template_name;
                option.textContent = `${tpl.template_name}${tpl.is_default ? ' (默认)' : ''}`;
                if (tpl.usage_count > 0) {
                    option.textContent += ` [已用${tpl.usage_count}次]`;
                }
                select.appendChild(option);
            });

            return result.templates;
        } catch (error) {
            console.error('加载模板列表失败:', error);
            showMessage('加载模板列表失败: ' + error.message, 'error');
            return [];
        }
    }

    /**
     * 应用选中的模板
     */
    async function applyGridTemplate(templateName) {
        if (!templateName) return;

        try {
            // 调用使用模板API（更新统计）
            const response = await fetch(`${API_BASE_URL}/api/grid/template/use`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({template_name: templateName})
            });

            if (!response.ok) throw new Error('应用模板失败');

            const result = await response.json();
            if (!result.success) throw new Error(result.error || '应用模板失败');

            const tpl = result.template;

            // 填充表单
            document.getElementById('gridPriceInterval').value = (tpl.price_interval * 100).toFixed(2);
            document.getElementById('gridPositionRatio').value = (tpl.position_ratio * 100).toFixed(2);
            document.getElementById('gridCallbackRatio').value = (tpl.callback_ratio * 100).toFixed(2);
            document.getElementById('gridDurationDays').value = tpl.duration_days;
            document.getElementById('gridMaxDeviation').value = (tpl.max_deviation * 100).toFixed(0);
            document.getElementById('gridTargetProfit').value = (tpl.target_profit * 100).toFixed(0);
            document.getElementById('gridStopLoss').value = (tpl.stop_loss * 100).toFixed(0);

            showMessage(`已应用模板: ${templateName}`, 'success');
        } catch (error) {
            console.error('应用模板失败:', error);
            showMessage('应用模板失败: ' + error.message, 'error');
        }
    }

    /**
     * 显示保存模板对话框
     */
    function showSaveTemplateDialog() {
        document.getElementById('saveTemplateDialog').classList.remove('hidden');
        document.getElementById('saveTemplateName').value = '';
        document.getElementById('saveTemplateDesc').value = '';
        document.getElementById('saveTemplateDefault').checked = false;
    }

    /**
     * 保存模板
     */
    async function saveGridTemplate() {
        const name = document.getElementById('saveTemplateName').value.trim();
        const desc = document.getElementById('saveTemplateDesc').value.trim();
        const isDefault = document.getElementById('saveTemplateDefault').checked;

        if (!name) {
            showMessage('请输入模板名称', 'error');
            return;
        }

        try {
            // 收集当前配置
            const templateData = {
                template_name: name,
                price_interval: parseFloat(document.getElementById('gridPriceInterval').value) / 100,
                position_ratio: parseFloat(document.getElementById('gridPositionRatio').value) / 100,
                callback_ratio: parseFloat(document.getElementById('gridCallbackRatio').value) / 100,
                duration_days: parseInt(document.getElementById('gridDurationDays').value),
                max_deviation: parseFloat(document.getElementById('gridMaxDeviation').value) / 100,
                target_profit: parseFloat(document.getElementById('gridTargetProfit').value) / 100,
                stop_loss: parseFloat(document.getElementById('gridStopLoss').value) / 100,
                description: desc,
                is_default: isDefault
            };

            const response = await fetch(`${API_BASE_URL}/api/grid/template/save`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(templateData)
            });

            if (!response.ok) throw new Error('保存模板失败');

            const result = await response.json();
            if (!result.success) throw new Error(result.error || '保存模板失败');

            showMessage(`模板"${name}"保存成功`, 'success');
            document.getElementById('saveTemplateDialog').classList.add('hidden');

            // 重新加载模板列表
            await loadGridTemplates();
        } catch (error) {
            console.error('保存模板失败:', error);
            showMessage('保存模板失败: ' + error.message, 'error');
        }
    }

    /**
     * 显示模板管理对话框
     */
    async function showManageTemplatesDialog() {
        document.getElementById('manageTemplatesDialog').classList.remove('hidden');
        await refreshTemplatesList();
    }

    /**
     * 刷新模板列表
     */
    async function refreshTemplatesList() {
        try {
            const templates = await loadGridTemplates();
            const container = document.getElementById('templatesList');

            if (templates.length === 0) {
                container.innerHTML = '<p class="text-gray-500 text-center py-4">暂无保存的模板</p>';
                return;
            }

            container.innerHTML = '';

            templates.forEach(tpl => {
                const item = document.createElement('div');
                item.className = 'border border-gray-200 rounded p-3 flex items-center justify-between hover:bg-gray-50';

                const info = document.createElement('div');
                info.className = 'flex-1';

                const title = document.createElement('div');
                title.className = 'font-semibold';
                title.textContent = tpl.template_name;
                if (tpl.is_default) {
                    const badge = document.createElement('span');
                    badge.className = 'ml-2 px-2 py-0.5 bg-blue-100 text-blue-800 text-xs rounded';
                    badge.textContent = '默认';
                    title.appendChild(badge);
                }

                const details = document.createElement('div');
                details.className = 'text-sm text-gray-600 mt-1';
                details.innerHTML = `
                    价格间隔: ${(tpl.price_interval * 100).toFixed(1)}% |
                    档位比例: ${(tpl.position_ratio * 100).toFixed(0)}% |
                    使用次数: ${tpl.usage_count || 0}
                `;

                if (tpl.description) {
                    const desc = document.createElement('div');
                    desc.className = 'text-sm text-gray-500 mt-1';
                    desc.textContent = tpl.description;
                    info.appendChild(title);
                    info.appendChild(details);
                    info.appendChild(desc);
                } else {
                    info.appendChild(title);
                    info.appendChild(details);
                }

                const actions = document.createElement('div');
                actions.className = 'flex gap-2';

                if (!tpl.is_default) {
                    const setDefaultBtn = document.createElement('button');
                    setDefaultBtn.className = 'px-3 py-1 text-sm bg-blue-600 text-white rounded hover:bg-blue-700';
                    setDefaultBtn.textContent = '设为默认';
                    setDefaultBtn.onclick = () => setDefaultTemplate(tpl.template_name);
                    actions.appendChild(setDefaultBtn);
                }

                const deleteBtn = document.createElement('button');
                deleteBtn.className = 'px-3 py-1 text-sm bg-red-600 text-white rounded hover:bg-red-700';
                deleteBtn.textContent = '删除';
                deleteBtn.onclick = () => deleteTemplate(tpl.template_name);
                actions.appendChild(deleteBtn);

                item.appendChild(info);
                item.appendChild(actions);
                container.appendChild(item);
            });
        } catch (error) {
            console.error('刷新模板列表失败:', error);
        }
    }

    /**
     * 设置默认模板
     */
    async function setDefaultTemplate(templateName) {
        try {
            const response = await fetch(`${API_BASE_URL}/api/grid/template/${templateName}/default`, {
                method: 'PUT'
            });

            if (!response.ok) throw new Error('设置默认模板失败');

            const result = await response.json();
            if (!result.success) throw new Error(result.error || '设置默认模板失败');

            showMessage(`已将"${templateName}"设为默认模板`, 'success');
            await refreshTemplatesList();
        } catch (error) {
            console.error('设置默认模板失败:', error);
            showMessage('设置默认模板失败: ' + error.message, 'error');
        }
    }

    /**
     * 删除模板
     */
    async function deleteTemplate(templateName) {
        if (!confirm(`确定删除模板"${templateName}"吗？`)) return;

        try {
            const response = await fetch(`${API_BASE_URL}/api/grid/template/${templateName}`, {
                method: 'DELETE'
            });

            if (!response.ok) throw new Error('删除模板失败');

            const result = await response.json();
            if (!result.success) throw new Error(result.error || '删除模板失败');

            showMessage(`模板"${templateName}"已删除`, 'success');
            await refreshTemplatesList();
        } catch (error) {
            console.error('删除模板失败:', error);
            showMessage('删除模板失败: ' + error.message, 'error');
        }
    }

    /**
     * 初始化模板管理事件监听器
     */
    function initGridTemplateListeners() {
        // 模板选择变化
        document.getElementById('gridTemplateSelect').addEventListener('change', (e) => {
            if (e.target.value) {
                applyGridTemplate(e.target.value);
            }
        });

        // 保存模板按钮
        document.getElementById('gridSaveTemplateBtn').addEventListener('click', showSaveTemplateDialog);

        // 管理模板按钮
        document.getElementById('gridManageTemplatesBtn').addEventListener('click', showManageTemplatesDialog);

        // 保存模板对话框
        document.getElementById('saveTemplateConfirm').addEventListener('click', saveGridTemplate);
        document.getElementById('saveTemplateCancel').addEventListener('click', () => {
            document.getElementById('saveTemplateDialog').classList.add('hidden');
        });

        // 关闭模板管理对话框
        document.getElementById('manageTemplatesClose').addEventListener('click', () => {
            document.getElementById('manageTemplatesDialog').classList.add('hidden');
        });
    }

    // 在页面加载时初始化模板功能
    document.addEventListener('DOMContentLoaded', () => {
        initGridTemplateListeners();
        loadGridTemplates(); // 加载模板列表到下拉框
    });

    // ======================= 网格模板管理功能结束 =======================

    console.log("Adding event listeners and fetching initial data...");
    fetchAllData(); // 脚本运行时加载初始数据
});

console.log("Script loaded");