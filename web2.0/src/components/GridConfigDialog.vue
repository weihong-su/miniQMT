<script setup lang="ts">
import { ref, onMounted } from 'vue'
import * as flaskApi from '../api/flask'
import type { RiskLevel } from '../types'

const props = defineProps<{ stockCode: string }>()
const emit = defineEmits<{ close: []; refresh: [] }>()

const loading = ref(true); const saving = ref(false)
const hasSession = ref(false); const sessionId = ref<number | null>(null)
const riskLevel = ref<RiskLevel>('moderate'); const riskTemplates = ref<Record<string, any>>({})
const marketPrice = ref(0); const existingCenter = ref(0)
const centerPrice = ref<number | string>('')

const form = ref({
  price_interval: 5, position_ratio: 25, callback_ratio: 0.5,
  max_investment: 10000, duration_days: 7, max_deviation: 15,
  target_profit: 10, stop_loss: 10,
})

const riskInfo: Record<string, { label: string; desc: string; color: string }> = {
  aggressive:  { label: '激进', desc: '高收益高风险', color: 'from-rose-500 to-orange-500' },
  moderate:    { label: '稳健', desc: '均衡收益风险', color: 'from-blue-500 to-indigo-500' },
  conservative:{ label: '保守', desc: '低风险稳定收益', color: 'from-emerald-500 to-teal-500' },
}

function applyTemplate(level: RiskLevel) {
  riskLevel.value = level
  const t = riskTemplates.value[level]; if (!t) return
  form.value.price_interval = t.price_interval * 100
  form.value.position_ratio = t.position_ratio * 100
  form.value.callback_ratio = t.callback_ratio * 100
  form.value.max_deviation = t.max_deviation * 100
  form.value.target_profit = t.target_profit * 100
  form.value.stop_loss = Math.abs(t.stop_loss) * 100
  form.value.duration_days = t.duration_days
}

function buildParams() {
  return {
    stock_code: props.stockCode, center_price: Number(centerPrice.value) || marketPrice.value,
    duration_days: form.value.duration_days, risk_level: riskLevel.value,
    price_interval: form.value.price_interval / 100, position_ratio: form.value.position_ratio / 100,
    callback_ratio: form.value.callback_ratio / 100, max_investment: form.value.max_investment,
    max_deviation: form.value.max_deviation / 100, target_profit: form.value.target_profit / 100,
    stop_loss: -form.value.stop_loss / 100,
  }
}

async function doStart() {
  saving.value = true; const r = await flaskApi.startGrid(buildParams()); saving.value = false
  if (r?.success) { emit('refresh'); emit('close') } else { alert('启动失败: ' + (r?.error || '未知错误')) }
}

async function doStop() {
  if (!sessionId.value) return; saving.value = true
  const r = await flaskApi.stopGrid(sessionId.value); saving.value = false
  if (r?.success) { emit('refresh'); emit('close') } else { alert('停止失败: ' + (r?.error || '未知错误')) }
}

onMounted(async () => {
  const [templates, session] = await Promise.all([flaskApi.getGridRiskTemplates(), flaskApi.getGridSession(props.stockCode)])
  riskTemplates.value = templates; if (templates.moderate) applyTemplate('moderate')
  if (session?.success && session.has_session) {
    hasSession.value = true; sessionId.value = session.session_id
    const cfg = session.config
    if (cfg) {
      centerPrice.value = cfg.center_price || ''; existingCenter.value = cfg.center_price || 0
      form.value.price_interval = (cfg.price_interval || 0.05) * 100
      form.value.position_ratio = (cfg.position_ratio || 0.25) * 100
      form.value.callback_ratio = (cfg.callback_ratio || 0.005) * 100
      form.value.max_investment = cfg.max_investment || 10000
      form.value.duration_days = cfg.duration_days || 7
      form.value.max_deviation = (cfg.max_deviation || 0.15) * 100
      form.value.target_profit = (cfg.target_profit || 0.10) * 100
      form.value.stop_loss = Math.abs(cfg.stop_loss || 0.10) * 100
    }
    if (session.risk_level) riskLevel.value = session.risk_level
    marketPrice.value = session.stats?.center_price || session.stats?.current_center_price || 0
  } else {
    hasSession.value = false
    if (session?.config) form.value.max_investment = session.config.max_investment || 10000
  }
  loading.value = false
})
</script>

<template>
  <Teleport to="body">
    <div class="modal-overlay" @click.self="emit('close')">
      <div class="modal-content w-[620px] max-w-[96vw]">
        <div class="px-6 py-4 border-b border-slate-100 flex items-center justify-between">
          <div>
            <h3 class="text-lg font-semibold text-slate-800">网格交易配置</h3>
            <p class="text-xs text-slate-400 mt-0.5 font-mono">{{ stockCode }}</p>
          </div>
          <button @click="emit('close')" class="w-8 h-8 flex items-center justify-center rounded-lg text-slate-400 hover:text-slate-600 hover:bg-slate-100 transition-colors text-xl">&times;</button>
        </div>

        <div v-if="loading" class="p-16 text-center">
          <div class="w-10 h-10 border-2 border-slate-200 border-t-blue-500 rounded-full animate-spin mx-auto mb-3"></div>
          <p class="text-sm text-slate-400">加载配置中...</p>
        </div>

        <template v-else>
          <div class="p-6 space-y-5">
            <!-- Risk Level -->
            <div>
              <label class="label-text mb-2">风险等级</label>
              <div class="grid grid-cols-1 sm:grid-cols-3 gap-2">
                <button v-for="lv in (['aggressive','moderate','conservative'] as RiskLevel[])" :key="lv"
                  @click="applyTemplate(lv)"
                  :class="['relative flex flex-col items-center py-3 px-2 rounded-lg border-2 transition-all duration-150',
                    riskLevel === lv ? 'border-blue-400 bg-blue-50 shadow-sm' : 'border-slate-200 bg-white hover:border-slate-300']">
                  <span :class="['text-sm font-bold', riskLevel === lv ? 'text-blue-700' : 'text-slate-500']">{{ riskInfo[lv].label }}</span>
                  <span class="text-[10px] text-slate-400 mt-0.5">{{ riskInfo[lv].desc }}</span>
                  <span v-if="riskLevel === lv" class="absolute -top-1.5 -right-1.5 w-4 h-4 bg-blue-500 rounded-full flex items-center justify-center">
                    <svg class="w-2.5 h-2.5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7"/></svg>
                  </span>
                </button>
              </div>
              <p class="text-[11px] text-slate-400 mt-2">{{ riskTemplates[riskLevel]?.description || '' }}</p>
            </div>

            <!-- Market info -->
            <div class="bg-blue-50/60 rounded-lg p-3 flex flex-col sm:flex-row sm:items-center gap-2 sm:gap-6 text-sm">
              <span class="text-slate-500">参考价 <strong class="text-blue-700 font-mono ml-1">{{ marketPrice || '--' }}</strong></span>
              <span v-if="hasSession" class="text-slate-500">原中心价 <strong class="text-blue-700 font-mono ml-1">{{ existingCenter }}</strong></span>
            </div>

            <!-- Center price -->
            <div>
              <label class="label-text">网格中心价格 <span class="text-red-400">*</span></label>
              <input v-model="centerPrice" type="number" step="0.001" placeholder="输入中心价格" class="input-field text-lg font-mono" />
            </div>

            <!-- Params grid -->
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-4">
              <div v-for="f in [
                {k:'price_interval',l:'价格间隔 (%)',h:'相邻网格档位间距'},
                {k:'position_ratio',l:'每档比例 (%)',h:'每次买卖持仓占比'},
                {k:'callback_ratio',l:'回调触发 (%)',h:'价格回调超此触发'},
                {k:'max_investment',l:'最大投入 (元)',h:'网格总投入上限'},
                {k:'max_deviation',l:'最大偏离 (%)',h:'超此自动停止'},
                {k:'target_profit',l:'目标盈利 (%)',h:'达到后自动平仓'},
                {k:'stop_loss',l:'止损比例 (%)',h:'亏损超此自动止损'},
                {k:'duration_days',l:'运行时长 (天)',h:'最长持续天数'},
              ]" :key="f.k">
                <label class="label-text">{{ f.l }}</label>
                <input v-model.number="(form as any)[f.k]" type="number" :step="f.k === 'callback_ratio' ? 0.01 : f.k === 'duration_days' ? 1 : 0.1" class="input-field" />
                <p class="text-[10px] text-slate-400 mt-0.5">{{ f.h }}</p>
              </div>
            </div>
          </div>

          <div class="px-6 py-4 bg-slate-50/80 rounded-b-lg flex justify-end gap-2">
            <button @click="emit('close')" class="btn-ghost">取消</button>
            <button v-if="!hasSession" @click="doStart" :disabled="saving" class="btn-primary">{{ saving ? '启动中...' : '启动网格交易' }}</button>
            <button v-else @click="doStop" :disabled="saving" class="btn-danger">{{ saving ? '停止中...' : '停止网格交易' }}</button>
          </div>
        </template>
      </div>
    </div>
  </Teleport>
</template>
