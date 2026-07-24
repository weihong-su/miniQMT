import { apiGet, apiPost } from './adapter'

export async function getStatus() {
  const r = await apiGet('/api/status')
  if (!r || r.status !== 'success') return null
  return { account: r.account, settings: r.settings, isMonitoring: r.isMonitoring }
}

export async function getMacdAdvice(code: string) {
  return await apiGet(`/api/macd/advice?code=${encodeURIComponent(code)}`)
}

export async function getConfig() {
  const r = await apiGet('/api/config')
  if (!r || r.status !== 'success') return null
  return { data: r.data, ranges: r.ranges }
}

export async function getPositions(version = -1) {
  const r = await apiGet(`/api/positions?version=${version}`)
  if (!r || r.status !== 'success') return null
  return {
    positions: r.data?.positions || [],
    metrics: r.data?.metrics || {},
    positionsAll: r.data?.positions_all || [],
    version: r.data_version || 0,
    noChange: r.no_change || false,
  }
}

export async function getPositionsAll(version = 0) {
  const r = await apiGet(`/api/positions-all?version=${version}`)
  if (!r || r.status !== 'success') return null
  return { data: r.data || [], version: r.data_version || 0, noChange: r.no_change || false }
}

export async function getTradeRecords() {
  const r = await apiGet('/api/trade-records')
  if (!r || r.status !== 'success') return []
  return r.data || []
}

export async function getStockPool() {
  const r = await apiGet('/api/stock_pool/list')
  if (!r || r.status !== 'success') return []
  return r.data || []
}

export async function getConnectionStatus() {
  const r = await apiGet('/api/connection/status')
  if (!r || r.status !== 'success') return false
  return r.connected
}

export async function saveConfig(data: any) {
  const r = await apiPost('/api/config/save', data)
  return r?.status === 'success'
}

export async function toggleMonitor(start: boolean) {
  const r = await apiPost(start ? '/api/monitor/start' : '/api/monitor/stop')
  return r?.status === 'success'
}

export async function executeBuy(strategy: string, quantity: number, stocks: string[], configData?: any) {
  const r = await apiPost('/api/actions/execute_buy', { strategy, quantity, stocks, ...(configData || {}) })
  return { success: r?.status === 'success', message: r?.message || r?.error || '' }
}

export async function clearLogs() {
  const r = await apiPost('/api/logs/clear')
  return r?.status === 'success'
}

export async function clearBuySellData() {
  const r = await apiPost('/api/data/clear_buysell')
  return r?.status === 'success'
}

export async function importData() {
  const r = await apiPost('/api/data/import')
  return r?.status === 'success'
}

export async function initHoldings(configData?: any) {
  const r = await apiPost('/api/holdings/init', configData || {})
  return r?.status === 'success'
}

// ---- 网格交易 API ----

export async function getGridSession(stockCode: string) {
  return apiGet(`/api/grid/session/${stockCode}`)
}

export async function getAllGridSessions() {
  const r = await apiGet('/api/grid/sessions')
  if (!r?.success) return []
  return r.sessions || []
}

export async function getGridTrades(sessionId: number, limit = 20, offset = 0) {
  const r = await apiGet(`/api/grid/trades/${sessionId}?limit=${limit}&offset=${offset}`)
  if (!r?.success) return { trades: [], totalCount: 0, pagination: { limit, offset, has_more: false } }
  return {
    trades: r.trades || [],
    totalCount: r.total_count || 0,
    pagination: r.pagination || { limit, offset, has_more: false },
  }
}

export async function getGridLedger(sessionId: number, limit = 50, offset = 0) {
  const r = await apiGet(`/api/grid/ledger/${sessionId}?limit=${limit}&offset=${offset}`)
  if (!r?.success) {
    return {
      summary: null,
      lots: [],
      matches: [],
      trades: [],
      totalCount: 0,
      pagination: { limit, offset, has_more: false },
      error: r?.error || '账本数据不可用',
    }
  }
  return {
    session_id: r.session_id,
    session: r.session,
    current_price: r.current_price,
    summary: r.summary,
    lots: r.lots || [],
    matches: r.matches || [],
    trades: r.trades || [],
    totalCount: r.total_count || 0,
    pagination: r.pagination || { limit, offset, has_more: false },
  }
}

export async function startGrid(params: any) {
  const { stock_code, duration_days, risk_level, ...config } = params
  return apiPost('/api/grid/start', {
    stock_code,
    center_price: params.center_price,
    duration_days,
    risk_level: risk_level || 'moderate',
    config,
  })
}

export async function stopGrid(sessionId: number) {
  return apiPost(`/api/grid/stop/${sessionId}`)
}

export async function setGridSessionEnabled(sessionId: number, enabled: boolean) {
  return apiPost(`/api/grid/session/${sessionId}/enabled`, { enabled })
}

export async function getGridRiskTemplates() {
  const r = await apiGet('/api/grid/risk-templates')
  if (!r?.success) return {}
  return r.templates || {}
}

export async function getGridCheckboxState(stockCode: string) {
  return apiGet(`/api/grid/checkbox-state/${stockCode}`)
}

export async function getGridTemplates() {
  const r = await apiGet('/api/grid/templates')
  if (!r?.success) return []
  return r.templates || []
}
