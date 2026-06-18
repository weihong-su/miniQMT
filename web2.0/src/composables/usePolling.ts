import { ref, onUnmounted } from 'vue'
import { useSystemStore } from '../stores/system'
import { usePositionsStore } from '../stores/positions'
import { useGridStore } from '../stores/grid'

export function usePolling() {
  const system = useSystemStore()
  const positions = usePositionsStore()
  const grid = useGridStore()
  const interval = ref(3000)
  let timer: ReturnType<typeof setInterval> | null = null
  let statusCounter = 0
  let logCounter = 0
  let holdingCounter = 0

  function start(customInterval = 3000) {
    stop()
    interval.value = customInterval
    statusCounter = logCounter = holdingCounter = 0
    timer = setInterval(poll, interval.value)
  }

  function stop() {
    if (timer) { clearInterval(timer); timer = null }
  }

  function adjustInterval(active: boolean) {
    interval.value = active ? 3000 : 10000
    if (timer) { stop(); start(interval.value) }
  }

  async function poll() {
    // Status: every 3 cycles (~9s when active)
    statusCounter++
    if (statusCounter >= 3) {
      statusCounter = 0
      system.fetchStatus().catch(() => {})
    }
    // Trades: every 6 cycles (~18s when active)
    logCounter++
    if (logCounter >= 6) {
      logCounter = 0
      positions.fetchTrades().catch(() => {})
    }
    // Positions: every 10 cycles (~30s when active) — SSE handles most updates
    holdingCounter++
    if (holdingCounter >= 10) {
      holdingCounter = 0
      grid.fetchSessions()
        .then(() => positions.fetchPositions())
        .catch(() => {})
    }
  }

  onUnmounted(stop)

  return { start, stop, adjustInterval }
}
