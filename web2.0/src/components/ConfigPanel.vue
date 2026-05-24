<script setup lang="ts">
import { useConfigStore } from '../stores/config'

const store = useConfigStore()

interface FieldDef {
  label: string
  key: string
  suffix?: string
  step?: number
  decimals?: number
  w?: string  // input width override
}

const NUMERIC_FIELDS: FieldDef[] = [
  { label: '单次买入金额', key: 'singleBuyAmount', suffix: '元', step: 1, decimals: 0, w: 'w-20' },
  { label: '首次止盈阈值', key: 'firstProfitSell', suffix: '%', step: 0.01, decimals: 2 },
  { label: '首次卖出比例', key: 'stockGainSellPencent', suffix: '%', step: 0.01, decimals: 2 },
  { label: '补仓跌幅阈值', key: 'stopLossBuy', suffix: '%', step: 0.01, decimals: 2 },
  { label: '止损比例', key: 'stockStopLoss', suffix: '%', step: 0.01, decimals: 2 },
  { label: '单股最大持仓', key: 'singleStockMaxPosition', suffix: '元', step: 1, decimals: 0, w: 'w-20' },
  { label: '最大总持仓', key: 'totalMaxPosition', suffix: '元', step: 1, decimals: 0, w: 'w-20' },
]

function displayValue(field: FieldDef): string | number {
  const raw = (store.config as any)[field.key]
  if (raw == null || isNaN(raw)) return ''
  return Number(raw).toFixed(field.decimals ?? 0)
}

function onFieldChange(field: FieldDef, raw: string) {
  const v = parseFloat(raw)
  ;(store.config as any)[field.key] = isNaN(v) ? 0 : v
}
</script>

<template>
  <div class="card">
    <div class="card-header flex items-center justify-between">
      <span>参数设置</span>
      <button class="btn-primary text-xs px-3 py-1.5" @click="store.saveConfig()" :disabled="store.saving">
        {{ store.saving ? '保存中...' : '保存配置' }}
      </button>
    </div>
    <div class="card-body !py-3">
      <div class="flex flex-wrap gap-x-5 gap-y-2">
        <div v-for="f in NUMERIC_FIELDS" :key="f.key" class="flex items-center gap-1.5 text-xs">
          <span class="text-slate-500 whitespace-nowrap">{{ f.label }}</span>
          <input
            type="number"
            :value="displayValue(f)"
            @input="onFieldChange(f, ($event.target as HTMLInputElement).value)"
            :step="f.step"
            class="input-field !py-1 !text-xs w-16"
            :class="f.w || 'w-14'"
          />
          <span v-if="f.suffix" class="text-[11px] text-slate-400">{{ f.suffix }}</span>
        </div>
      </div>
    </div>
  </div>
</template>
