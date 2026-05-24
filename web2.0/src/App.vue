<script setup lang="ts">
import { useSystemStore } from './stores/system'
import { useConfigStore } from './stores/config'
import { usePositionsStore } from './stores/positions'
import { useGridStore } from './stores/grid'
import { useSSE } from './composables/useSSE'
import { usePolling } from './composables/usePolling'
import { onMounted, onUnmounted, watch } from 'vue'

import HeaderBar from './components/HeaderBar.vue'
import SimulationBanner from './components/SimulationBanner.vue'
import ConfigPanel from './components/ConfigPanel.vue'
import HoldingsTable from './components/HoldingsTable.vue'
import OrderLog from './components/OrderLog.vue'

const system = useSystemStore()
const config = useConfigStore()
const positions = usePositionsStore()
const grid = useGridStore()
const { connect: sseConnect } = useSSE()
const { start: startPolling } = usePolling()

async function refreshAll() {
  await Promise.all([positions.fetchPositions(), positions.fetchTrades(), grid.fetchSessions()])
}

async function init() {
  config.fetchConfig()
  await Promise.all([system.fetchStatus(), positions.fetchAll(), grid.fetchAll()])
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
      <div class="grid grid-cols-1 lg:grid-cols-3 gap-3 md:gap-5">
        <div class="lg:col-span-2 space-y-3 md:space-y-5">
          <ConfigPanel />
          <HoldingsTable @refresh="refreshAll" />
        </div>
        <div class="space-y-3 md:space-y-5">
          <OrderLog />
        </div>
      </div>
    </main>
  </div>
</template>
