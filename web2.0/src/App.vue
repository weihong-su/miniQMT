<script setup lang="ts">
import { useSystemStore } from './stores/system'
import { useConfigStore } from './stores/config'
import { usePositionsStore } from './stores/positions'
import { useGridStore } from './stores/grid'
import { useSSE } from './composables/useSSE'
import { usePolling } from './composables/usePolling'
import { loadConnection } from './api/accounts'
import { computed, onMounted, onUnmounted, watch } from 'vue'

import HeaderBar from './components/HeaderBar.vue'
import SimulationBanner from './components/SimulationBanner.vue'
import ConfigPanel from './components/ConfigPanel.vue'
import HoldingsTable from './components/HoldingsTable.vue'
import GridStatusPanel from './components/GridStatusPanel.vue'
import OrderLog from './components/OrderLog.vue'

const system = useSystemStore()
const config = useConfigStore()
const positions = usePositionsStore()
const grid = useGridStore()
const { connect: sseConnect } = useSSE()
const { start: startPolling } = usePolling()

const mobilePnl = computed(() => positions.metrics.total_profit ?? 0)
const mobilePnlRatio = computed(() => positions.metrics.total_profit_ratio ?? 0)

async function refreshAll() {
  await grid.fetchSessions()
  await Promise.all([positions.fetchPositions(), positions.fetchTrades()])
}

async function init() {
  const conn = loadConnection()
  // xtquant 网关或 auto 模式：尝试从网关同步真实账号列表
  if (conn.mode === 'xtquant' || (conn.mode === 'auto' && conn.xtquantUrl && window.location.origin === conn.xtquantUrl)) {
    await system.syncAccountsFromGateway()
  }
  config.fetchConfig()
  await Promise.all([system.fetchStatus(), grid.fetchAll()])
  await positions.fetchAll()
  system.fetchConnection()
}

onMounted(() => {
  init(); setTimeout(() => sseConnect(), 1000); startPolling()
  window.addEventListener('refresh-data', refreshAll)
})
onUnmounted(() => window.removeEventListener('refresh-data', refreshAll))

watch(() => system.currentAccountId, () => {
  positions.dataVersion = 0; positions.positions = []; positions.trades = []; grid.sessions = []
  init(); sseConnect()
})
</script>

<template>
  <div class="min-h-screen flex flex-col">
    <HeaderBar />
    <SimulationBanner />

    <main class="flex-1 p-3 md:p-5 space-y-3 md:space-y-5 max-w-[1600px] mx-auto w-full">
      <section class="md:hidden grid grid-cols-2 gap-2">
        <div class="metric-tile col-span-2">
          <div class="flex items-center justify-between gap-3">
            <div class="min-w-0">
              <div class="metric-label">总资产</div>
              <div class="metric-value truncate text-base">¥{{ (system.account.totalAssets || 0).toLocaleString() }}</div>
            </div>
            <div :class="['min-w-0 text-right font-mono text-sm font-semibold', mobilePnl >= 0 ? 'text-red-600' : 'text-emerald-600']">
              <div class="truncate">{{ mobilePnl >= 0 ? '+' : '' }}¥{{ Math.abs(mobilePnl).toLocaleString(undefined, { maximumFractionDigits: 2 }) }}</div>
              <div class="text-xs">{{ mobilePnlRatio >= 0 ? '+' : '' }}{{ mobilePnlRatio.toFixed(2) }}%</div>
            </div>
          </div>
        </div>
        <div class="metric-tile">
          <div class="metric-label">持仓</div>
          <div class="metric-value">{{ positions.positions.length }} 只</div>
        </div>
        <div class="metric-tile">
          <div class="metric-label">网格</div>
          <div class="metric-value">{{ grid.activeSessions.length }} 个运行</div>
        </div>
      </section>
      <ConfigPanel />
      <HoldingsTable @refresh="refreshAll" />
      <GridStatusPanel />
      <OrderLog />
    </main>
  </div>
</template>
