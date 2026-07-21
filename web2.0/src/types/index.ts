// ===== 持仓相关 =====
export interface Position {
  stock_code: string
  stock_name?: string
  volume: number
  available: number
  cost_price: number
  current_price: number
  market_value: number
  profit_ratio: number
  profit_amount?: number
  change_percentage?: number
  profit_triggered: boolean
  highest_price: number
  stop_loss_price: number
  open_date: string
  grid_session_active: boolean
}

export interface PositionMetrics {
  total_market_value: number
  total_profit: number
  total_profit_ratio: number
  position_count: number
  stock_count: number
}

// ===== 账户相关 =====
export interface AccountInfo {
  id: string
  availableBalance: number
  maxHoldingValue: number
  totalAssets: number
  timestamp: string
}

// ===== 系统状态 =====
export interface SystemStatus {
  isMonitoring: boolean
  enableAutoTrading: boolean
  enableGridTrading: boolean
  positionMonitorRunning: boolean
  allowBuy: boolean
  allowSell: boolean
  simulationMode: boolean
  connected: boolean
}

// ===== 配置 =====
export interface ConfigData {
  singleBuyAmount: number
  firstProfitSell: number
  firstProfitSellEnabled: boolean
  stockGainSellPencent: number
  firstProfitSellPencent: boolean
  allowBuy: boolean
  allowSell: boolean
  stopLossBuy: number
  stopLossBuyEnabled: boolean
  stockStopLoss: number
  StopLossEnabled: boolean
  singleStockMaxPosition: number
  totalMaxPosition: number
  globalAllowBuySell: boolean
  globalAllowGridTrading: boolean
  simulationMode: boolean
}

export interface ParamRange {
  min: number
  max: number
}

// ===== 交易记录 =====
export interface TradeRecord {
  stock_code: string
  stock_name: string
  trade_type: 'BUY' | 'SELL'
  price: number
  volume: number
  trade_time: string
  trade_id: string
  strategy: string
  strategy_label?: string
}

// ===== 买卖操作 =====
export type BuyStrategy = 'random_pool' | 'custom_stock'

export interface BuyRequest {
  strategy: BuyStrategy
  quantity: number
  stocks: string[]
}

// ===== 网格交易 =====
export type RiskLevel = 'aggressive' | 'moderate' | 'conservative'

export interface GridConfig {
  stock_code: string
  center_price: number
  price_interval: number
  position_ratio: number
  callback_ratio: number
  max_investment: number
  max_deviation: number
  target_profit: number
  stop_loss: number
  duration_days: number
}

export interface GridSession {
  session_id: number
  stock_code: string
  status: 'active' | 'stopping' | 'stopped' | 'completed' | 'paused'
  enabled?: boolean
  center_price: number
  current_center_price: number
  current_investment?: number
  max_investment?: number
  trade_count: number
  buy_count: number
  sell_count: number
  profit_ratio: number
  grid_profit?: number
  pnl_snapshot?: GridPnlSnapshot
  deviation_ratio: number
  start_time: string
  end_time: string
  stop_time?: string
  stop_reason?: string
}

export interface GridPnlSnapshot {
  profit_ratio: number
  total_pnl_ratio?: number
  total_pnl: number
  realized_pnl: number
  unrealized_pnl: number
  cash_flow_profit?: number
  cash_flow_ratio?: number
  method: string
  method_detail?: string
  has_ledger?: boolean
  is_degraded?: boolean
  open_volume?: number
  denominator?: number
  denominator_type?: string
}

export interface GridTrade {
  session_id: number
  stock_code?: string
  trade_type: 'BUY' | 'SELL' | string
  grid_level?: number
  trigger_price?: number
  volume: number
  amount: number
  trade_id?: string
  trade_time?: string
}

export interface GridLot {
  id: number
  session_id: number
  stock_code: string
  buy_trade_id: string
  buy_order_id?: string | null
  buy_price: number
  original_volume: number
  remaining_volume: number
  realized_volume: number
  buy_amount: number
  opened_at: string
  updated_at?: string
  status: 'open' | 'closed' | string
}

export interface GridLotMatch {
  id: number
  session_id: number
  stock_code: string
  buy_lot_id?: number | null
  sell_trade_id: string
  sell_order_id?: string | null
  match_type: 'matched' | 'unmatched' | string
  volume: number
  buy_price?: number | null
  sell_price: number
  buy_amount: number
  sell_amount: number
  realized_pnl: number
  matched_at: string
}

export interface GridLedgerSummary {
  has_ledger: boolean
  lot_count: number
  match_count: number
  bought_volume: number
  open_volume: number
  matched_volume: number
  unmatched_volume: number
  open_cost: number
  open_market_value: number
  realized_pnl: number
  unrealized_pnl: number
  true_pnl: number
}

export interface GridLedgerDetail {
  session_id: number
  session?: Record<string, any>
  current_price?: number | null
  summary: GridLedgerSummary
  lots: GridLot[]
  matches: GridLotMatch[]
  trades: GridTrade[]
  totalCount: number
  pagination: {
    limit: number
    offset: number
    has_more: boolean
  }
}

export interface RiskTemplate {
  template_name: string
  price_interval: number
  position_ratio: number
  callback_ratio: number
  max_deviation: number
  target_profit: number
  stop_loss: number
  duration_days: number
  max_investment_ratio: number
  description: string
}

// ===== SSE 事件 =====
export interface SSEMessage {
  timestamp: string
  account_info?: {
    available: number
    market_value: number
    total_asset: number
  }
  monitoring?: {
    isMonitoring: boolean
    autoTradingEnabled: boolean
    gridTradingEnabled: boolean
    allowBuy: boolean
    allowSell: boolean
    simulationMode: boolean
  }
  positions_update?: {
    version: number
    changed: boolean
  }
  error?: string
}

// ===== API 响应包装 =====
export interface ApiResponse<T = any> {
  status?: string
  success?: boolean
  data?: T
  message?: string
  error?: string
  no_change?: boolean
  data_version?: number
}

// ===== 后端类型 =====
export type BackendType = 'flask' | 'xtquant' | 'auto'
