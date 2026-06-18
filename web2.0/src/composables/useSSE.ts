import { ref, onMounted, onUnmounted } from 'vue'
import { useSystemStore } from '../stores/system'
import { usePositionsStore } from '../stores/positions'
import { useGridStore } from '../stores/grid'
import { loadConnection } from '../api/accounts'
import type { SSEMessage } from '../types'

export function useSSE() {
  const system = useSystemStore()
  const positions = usePositionsStore()
  const grid = useGridStore()
  const healthy = ref(false)
  let eventSource: EventSource | null = null
  let heartbeatTimer: ReturnType<typeof setInterval> | null = null
  let lastMessageTime = 0
  const HEARTBEAT_INTERVAL = 10000
  const HEARTBEAT_TIMEOUT = 15000

  function connect() {
    const conn = loadConnection()
    // xtquant 网关模式：没有 SSE 端点，依赖轮询
    if (conn.mode === 'xtquant') return
    // auto 模式在同源 xtquant_manager 下也无 SSE 端点
    if (conn.mode === 'auto' && conn.xtquantUrl && window.location.origin === conn.xtquantUrl) return
    disconnect()
    healthy.value = false
    try {
      eventSource = new EventSource('/api/sse')
      eventSource.onmessage = (e) => {
        lastMessageTime = Date.now()
        healthy.value = true
        try {
          const msg: SSEMessage = JSON.parse(e.data)
          if (msg.account_info) {
            system.account.availableBalance = msg.account_info.available
            system.account.maxHoldingValue = msg.account_info.market_value
            system.account.totalAssets = msg.account_info.total_asset
          }
          if (msg.monitoring) {
            system.autoTrading = msg.monitoring.autoTradingEnabled
            system.allowBuy = msg.monitoring.allowBuy
            system.allowSell = msg.monitoring.allowSell
            system.simulationMode = msg.monitoring.simulationMode
          }
          system.lastUpdateTime = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
          if (msg.positions_update?.changed) {
            setTimeout(() => {
              grid.fetchSessions()
                .then(() => positions.fetchPositions())
                .catch(() => {})
            }, 100)
          }
        } catch { /* ignore parse errors */ }
      }
      eventSource.onerror = () => {
        healthy.value = false
        disconnect()
        setTimeout(connect, 5000)
      }
      startHeartbeat()
    } catch { /* SSE not supported */ }
  }

  function disconnect() {
    if (eventSource) { eventSource.close(); eventSource = null }
    stopHeartbeat()
    healthy.value = false
  }

  function startHeartbeat() {
    stopHeartbeat()
    heartbeatTimer = setInterval(() => {
      if (Date.now() - lastMessageTime > HEARTBEAT_TIMEOUT) {
        healthy.value = false
        disconnect()
        setTimeout(connect, 1000)
      }
    }, HEARTBEAT_INTERVAL)
  }

  function stopHeartbeat() {
    if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null }
  }

  onUnmounted(disconnect)

  return { healthy, connect, disconnect }
}
