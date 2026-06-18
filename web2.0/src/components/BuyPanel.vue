<script setup lang="ts">
import { ref } from 'vue'
import { useConfigStore } from '../stores/config'
import { usePositionsStore } from '../stores/positions'
import * as flaskApi from '../api/flask'
import type { BuyStrategy } from '../types'

const emit = defineEmits<{ refresh: [] }>()
const config = useConfigStore()
const positions = usePositionsStore()

const strategy = ref<BuyStrategy>('random_pool')
const quantity = ref(1)
const executing = ref(false)
const clearing = ref(false)
const importing = ref(false)
const initializing = ref(false)
const showDialog = ref(false)
const dialogTitle = ref('')
const dialogStockList = ref('')
const isRandomPool = ref(true)

async function handleBuy() {
  if (strategy.value === 'random_pool') {
    const pool = await flaskApi.getStockPool()
    if (pool.length === 0) { alert('备选池为空'); return }
    isRandomPool.value = true; dialogTitle.value = `从备选池随机买入 ${quantity.value} 只`; dialogStockList.value = pool.join(', ')
  } else {
    isRandomPool.value = false; dialogTitle.value = `自定义股票买入 ${quantity.value} 只`; dialogStockList.value = ''
  }
  showDialog.value = true
}

async function doConfirm() {
  executing.value = true
  const stocks = dialogStockList.value.split(/[,，\s]+/).filter(s => s.trim())
  const r = await flaskApi.executeBuy(strategy.value, quantity.value, stocks, config.configData)
  executing.value = false; showDialog.value = false
  if (r.success) { positions.dataVersion = 0; emit('refresh') } else { alert(r.message || '买入失败') }
}

async function doClear() { clearing.value = true; await flaskApi.clearLogs(); clearing.value = false; emit('refresh') }
async function doImport() { importing.value = true; await flaskApi.importData(); importing.value = false; emit('refresh') }
async function doInit() {
  if (!confirm('确定要重新初始化持仓数据？此操作将从 QMT 重新同步。')) return
  initializing.value = true
  await flaskApi.initHoldings(config.configData)
  initializing.value = false; positions.dataVersion = 0; emit('refresh')
}
</script>

<template>
  <div class="card">
    <div class="card-header">
      <div class="flex items-center gap-2">
        <svg class="w-4 h-4 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
        <span>交易操作</span>
      </div>
    </div>
    <div class="card-body">
      <div class="flex flex-wrap items-end gap-4">
        <div>
          <label class="label-text">策略</label>
          <select v-model="strategy" class="input-field w-44">
            <option value="random_pool">从备选池随机买入</option>
            <option value="custom_stock">自定义股票买入</option>
          </select>
        </div>
        <div>
          <label class="label-text">数量</label>
          <input v-model.number="quantity" type="number" min="1" max="100" class="input-field w-16 text-center" />
        </div>
        <button @click="handleBuy" :disabled="executing" class="btn-primary">⚡ 一键买入</button>

        <div class="h-8 w-px bg-slate-200 mx-1 self-center"></div>

        <button @click="doClear" :disabled="clearing" class="btn-outline btn-xs">{{ clearing ? '...' : '清空今日日志' }}</button>
        <button @click="doImport" :disabled="importing" class="btn-outline btn-xs">{{ importing ? '...' : '导入配置' }}</button>
        <button @click="doInit" :disabled="initializing" class="btn-danger btn-xs">{{ initializing ? '同步中...' : '初始化持仓' }}</button>
      </div>
    </div>

    <!-- Dialog -->
    <Teleport to="body">
      <div v-if="showDialog" class="modal-overlay" @click.self="showDialog = false">
        <div class="modal-content w-[520px] max-w-[96vw]">
          <div class="px-6 py-4 border-b border-slate-100"><h3 class="text-lg font-semibold text-slate-800">{{ dialogTitle }}</h3></div>
          <div class="p-6">
            <label class="label-text">{{ isRandomPool ? '股票列表（可编辑）' : '股票代码（逗号或换行分隔）' }}</label>
            <textarea v-model="dialogStockList" rows="6" class="input-field font-mono text-sm" :placeholder="isRandomPool ? '' : '000001.SZ, 600036.SH'"></textarea>
          </div>
          <div class="px-6 py-3 bg-slate-50/80 rounded-b-lg flex justify-end gap-2">
            <button @click="showDialog = false" class="btn-ghost">取消</button>
            <button @click="doConfirm" :disabled="executing" class="btn-primary">{{ executing ? '提交中...' : '确定买入' }}</button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>
