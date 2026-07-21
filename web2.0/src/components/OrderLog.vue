<script setup lang="ts">
import { usePositionsStore } from '../stores/positions'
const positions = usePositionsStore()

const strategyLabels: Record<string, string> = {
  simu: '模拟',
  auto_partial: '浮盈',
  auto_full: '止盈',
  stop_loss: '止损',
  grid: '网格',
  manual: '手动',
  external: '外部',
  default: '默认'
}

function strategyLabel(t: any) {
  return t.strategy_label || strategyLabels[t.strategy] || t.strategy || '--'
}
</script>

<template>
  <div class="card">
    <div class="card-header">
      <div class="flex items-center gap-2">
        <svg class="w-4 h-4 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
        <span>交易日志</span>
        <span v-if="positions.trades.length" class="badge-slate text-[10px]">{{ positions.trades.length }} 条</span>
      </div>
    </div>
    <div class="p-0">
      <div v-if="positions.trades.length === 0" class="py-16 text-center">
        <svg class="w-10 h-10 text-slate-200 mx-auto mb-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>
        <p class="text-slate-400 font-medium text-sm">暂无交易记录</p>
      </div>
      <div v-else class="md:hidden max-h-[420px] overflow-y-auto p-3 space-y-2">
        <article v-for="(t, i) in positions.trades" :key="i" class="mobile-card !p-3">
          <div class="flex items-start justify-between gap-3">
            <div>
              <div class="flex items-center gap-2">
                <span :class="['inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-bold ring-1',
                  t.trade_type === 'BUY' ? 'bg-red-50 text-red-600 ring-red-200' : 'bg-emerald-50 text-emerald-600 ring-emerald-200']">
                  {{ t.trade_type === 'BUY' ? '买入' : '卖出' }}
                </span>
                <span class="font-mono text-sm font-semibold text-slate-700">{{ t.stock_code }}</span>
              </div>
              <div class="mt-1 text-xs text-slate-400">{{ t.stock_name || '--' }} · {{ strategyLabel(t) }}</div>
            </div>
            <div class="text-right">
              <div class="font-mono text-sm text-slate-700">¥{{ (t.price || 0).toFixed(2) }}</div>
              <div class="mt-1 text-xs text-slate-400">{{ t.volume }} 股</div>
            </div>
          </div>
          <div class="mt-2 flex items-center justify-between text-xs text-slate-400">
            <span>{{ (t.trade_time || '').substring(0, 19).replace('T', ' ') }}</span>
            <span class="font-mono">¥{{ ((t.price || 0) * (t.volume || 0)).toLocaleString(undefined, {minimumFractionDigits:0, maximumFractionDigits:0}) }}</span>
          </div>
        </article>
      </div>
      <div v-if="positions.trades.length > 0" class="hidden md:block max-h-[420px] overflow-y-auto overflow-x-auto">
        <div class="min-w-[560px] md:min-w-0 divide-y divide-slate-50">
        <div v-for="(t, i) in positions.trades" :key="i"
          class="flex items-center gap-2 md:gap-3 px-3 md:px-4 py-2.5 hover:bg-slate-50/60 transition-colors text-xs">
          <!-- time -->
          <span class="text-[11px] text-slate-400 font-mono w-16 flex-shrink-0">{{ (t.trade_time || '').substring(11, 19) }}</span>
          <!-- badge -->
          <span :class="['inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-bold w-10 justify-center flex-shrink-0',
            t.trade_type === 'BUY' ? 'bg-red-50 text-red-600 ring-1 ring-red-200' : 'bg-emerald-50 text-emerald-600 ring-1 ring-emerald-200']">
            {{ t.trade_type === 'BUY' ? '买入' : '卖出' }}
          </span>
          <!-- stock -->
          <span class="font-mono font-medium text-slate-700 w-16 flex-shrink-0">{{ t.stock_code }}</span>
          <!-- name -->
          <span class="text-slate-500 truncate w-16 flex-shrink-0">{{ t.stock_name || '--' }}</span>
          <!-- price -->
          <span class="font-mono text-slate-600 w-14 text-right flex-shrink-0">{{ (t.price || 0).toFixed(2) }}</span>
          <!-- volume -->
          <span class="text-slate-400 w-16 text-right flex-shrink-0">{{ t.volume }} 股</span>
          <!-- amount -->
          <span class="font-mono text-slate-500 w-24 text-right flex-shrink-0">¥{{ ((t.price || 0) * (t.volume || 0)).toLocaleString(undefined, {minimumFractionDigits:0, maximumFractionDigits:0}) }}</span>
          <!-- strategy tag -->
          <span class="badge-slate !text-[9px] flex-shrink-0">{{ strategyLabel(t) }}</span>
        </div>
        </div>
      </div>
    </div>
  </div>
</template>
