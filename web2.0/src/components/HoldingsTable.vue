<script setup lang="ts">
import { computed, ref } from 'vue'
import { usePositionsStore } from '../stores/positions'
import { useGridStore } from '../stores/grid'
import { fmtPrice, fmtPercent, fmtMoney, profitClass } from '../utils/format'
import GridConfigDialog from './GridConfigDialog.vue'

const positions = usePositionsStore()
const grid = useGridStore()
const sortKey = ref<string>('profit_ratio')
const sortDir = ref<-1 | 1>(-1)
const showGridDialog = ref(false)
const gridStockCode = ref('')

const sorted = computed(() => {
  const arr = [...positions.positions]
  arr.sort((a: any, b: any) => {
    const va = a[sortKey.value] ?? 0; const vb = b[sortKey.value] ?? 0
    return (va > vb ? 1 : va < vb ? -1 : 0) * sortDir.value
  })
  return arr
})

function toggleSort(key: string) {
  if (sortKey.value === key) sortDir.value = (sortDir.value * -1) as -1 | 1
  else { sortKey.value = key; sortDir.value = -1 }
}

const selectAll = computed({
  get: () => positions.positions.length > 0 && positions.selectedStocks.size === positions.positions.length,
  set: (v) => v ? positions.selectAll(positions.positions.map(p => p.stock_code)) : positions.deselectAll(),
})

function openGridConfig(code: string) { gridStockCode.value = code; showGridDialog.value = true }

const COLS = [
  { k: 'stock_code',   l: '代码',     s: true,  c: 'tabular-nums' },
  { k: 'stock_name',   l: '名称',     s: false, c: 'text-slate-500 truncate max-w-[60px]' },
  { k: 'current_price',l: '现价',     s: true,  c: 'text-right tabular-nums' },
  { k: 'cost_price',   l: '成本',     s: true,  c: 'text-right tabular-nums text-slate-500' },
  { k: 'profit_ratio', l: '盈亏',     s: true,  c: 'text-right tabular-nums font-semibold' },
  { k: 'market_value', l: '市值',     s: true,  c: 'text-right tabular-nums' },
  { k: 'volume',       l: '持仓',     s: true,  c: 'text-right tabular-nums' },
  { k: 'available',    l: '可用',     s: true,  c: 'text-right tabular-nums text-slate-500' },
  { k: 'profit_triggered', l: '止盈',s: false, c: 'text-center' },
  { k: 'highest_price',l: '最高',     s: false, c: 'text-right tabular-nums' },
  { k: 'stop_loss_price', l: '止损',s: false, c: 'text-right tabular-nums text-slate-500' },
  { k: 'open_date',    l: '建仓',     s: true,  c: 'text-slate-500 whitespace-nowrap' },
]

function cellValue(pos: any, col: typeof COLS[0]): string {
  const v = pos[col.k]
  if (col.k === 'profit_ratio') return fmtPercent(v)
  if (col.k === 'current_price' || col.k === 'cost_price' || col.k === 'highest_price' || col.k === 'stop_loss_price') return fmtPrice(v)
  if (col.k === 'market_value') return fmtMoney(v, 0)
  if (col.k === 'open_date') return (v || '').substring(0, 10) || '--'
  if (col.k === 'profit_triggered') return v ? '是' : '否'
  return v ?? '--'
}

function profitBg(v: number): string {
  if (v > 0) return 'bg-emerald-50/40'
  if (v < 0) return 'bg-red-50/40'
  return ''
}
</script>

<template>
  <div class="card">
    <div class="card-header">
      <div class="flex items-center gap-2">
        <svg class="w-4 h-4 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 10h16M4 14h16M4 18h16"/></svg>
        <span>持仓列表</span>
        <span class="badge-blue text-[10px]">{{ positions.positions.length }} 只</span>
      </div>
      <span class="text-xs text-slate-400">总市值 <strong class="text-slate-700">{{ fmtMoney(positions.totalMarketValue) }}</strong></span>
    </div>

    <div class="overflow-x-auto -mx-3 md:mx-0">
      <div class="min-w-[800px] md:min-w-0">
        <table class="w-full text-xs">
        <thead>
          <tr class="bg-slate-50/80 border-b border-slate-200">
            <th class="pl-5 pr-2 py-2.5 w-10"><input type="checkbox" v-model="selectAll" class="w-3.5 h-3.5 rounded border-slate-300 text-blue-600 focus:ring-blue-500" /></th>
            <th v-for="col in COLS" :key="col.k"
              :class="['px-2 py-2.5 font-semibold text-slate-500 whitespace-nowrap select-none', col.c,
                       col.s ? 'cursor-pointer hover:text-slate-800' : '']"
              @click="col.s && toggleSort(col.k)">
              <span class="inline-flex items-center gap-0.5">
                {{ col.l }}
                <span v-if="sortKey === col.k" class="text-blue-500 text-[10px]">{{ sortDir === -1 ? '▼' : '▲' }}</span>
              </span>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="sorted.length === 0">
            <td :colspan="COLS.length + 1" class="py-16 text-center">
              <div class="flex flex-col items-center gap-3">
                <svg class="w-12 h-12 text-slate-200" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4"/></svg>
                <p class="text-slate-400 font-medium">暂无持仓数据</p>
                <p class="text-slate-300 text-[11px]">等待 QMT 同步持仓信息...</p>
              </div>
            </td>
          </tr>
          <tr v-for="pos in sorted" :key="pos.stock_code"
            :class="['border-b border-slate-100 hover:bg-slate-50/70 transition-colors group', profitBg(pos.profit_ratio)]">
            <td class="pl-5 pr-2 py-2">
              <input type="checkbox" :checked="positions.selectedStocks.has(pos.stock_code)"
                @change="positions.toggleSelect(pos.stock_code, ($event.target as HTMLInputElement).checked)"
                class="w-3.5 h-3.5 rounded border-slate-300 text-blue-600 cursor-pointer opacity-60 group-hover:opacity-100 transition-opacity" />
            </td>
            <td class="px-2 py-2 font-semibold font-mono">
              <button @click="openGridConfig(pos.stock_code)" class="text-blue-700 hover:text-blue-500 hover:underline cursor-pointer inline-flex items-center gap-1">
                {{ pos.stock_code }}
                <span v-if="pos.grid_session_active" class="badge-green !text-[9px] !px-1 !py-0 leading-none">网格</span>
              </button>
            </td>
            <td v-for="col in COLS.slice(1)" :key="col.k"
              :class="['px-2 py-2 whitespace-nowrap', col.c,
                       col.k === 'profit_ratio' ? (pos.profit_ratio > 0 ? 'text-emerald-600' : pos.profit_ratio < 0 ? 'text-red-600' : 'text-slate-400') : '']">
              <span v-if="col.k === 'profit_triggered'">
                <span :class="pos.profit_triggered ? 'badge-green !text-[9px]' : 'badge-slate !text-[9px]'">{{ pos.profit_triggered ? '是' : '否' }}</span>
              </span>
              <span v-else>{{ cellValue(pos, col) }}</span>
            </td>
          </tr>
        </tbody>
      </table>
      </div>
    </div>

    <GridConfigDialog v-if="showGridDialog" :stock-code="gridStockCode" @close="showGridDialog = false; $emit('refresh')" />
  </div>
</template>
