<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { useSystemStore } from '../stores/system'
import { usePositionsStore } from '../stores/positions'
import { useSSE } from '../composables/useSSE'
import { isGatewayMode } from '../api/accounts'
import type { AccountEntry } from '../api/accounts'
import type { TriState } from '../types'
import ConnectionSettings from './ConnectionSettings.vue'
import StateBadge from './StateBadge.vue'
import { useAdviceTooltip } from '../composables/useAdviceTooltip'
import { formatAge, freshnessOf, freshnessClass } from '../utils/freshness'

const { show: showAdvice, hide: hideAdvice } = useAdviceTooltip()

const system = useSystemStore()
const assetMoney = (value: number | null | undefined) =>
  system.valuationDisplay.validAccount && typeof value === 'number' && Number.isFinite(value)
    ? value.toLocaleString('zh-CN', { minimumFractionDigits: 2 }) : '--'
const positions = usePositionsStore()
const { healthy: sseHealthy } = useSSE()

const showAccountDialog = ref(false)
const showConnSettings = ref(false)
const showDropdown = ref(false)
const editForm = ref<AccountEntry>({ id: '', label: '', flaskUrl: '' })
const dropdownRef = ref<HTMLElement | null>(null)
const gatewayMode = ref(isGatewayMode())

// 每秒重算一次，让"xx 秒前"真正走动
const now = ref(Date.now())
let clock: ReturnType<typeof setInterval> | null = null

const connectionAge = computed(() => formatAge(system.connectionUpdatedAt, now.value))
const connectionFreshness = computed(() => freshnessOf(system.connectionUpdatedAt, undefined, now.value))
const statusAge = computed(() => formatAge(system.statusUpdatedAt, now.value))
const statusFreshness = computed(() => freshnessOf(system.statusUpdatedAt, undefined, now.value))

/** 数据整体是否失联——任一核心数据块 dead 就该显眼提示 */
const dataStale = computed(() =>
  freshnessOf(system.statusUpdatedAt, undefined, now.value) === 'dead' ||
  freshnessOf(positions.positionsUpdatedAt, undefined, now.value) === 'dead'
)

const SWITCHES: { key: string; label: string; get: () => TriState }[] = [
  { key: 'auto',  label: '自动操作', get: () => system.isMonitoring },
  { key: 'buy',   label: '允许买',   get: () => system.allowBuy },
  { key: 'sell',  label: '允许卖',   get: () => system.allowSell },
  { key: 'sim',   label: '模拟交易', get: () => system.simulationMode },
  { key: 'stopP', label: '自动止盈', get: () => system.autoTrading },
  { key: 'grid',  label: '自动网格', get: () => system.gridTrading },
]

function toggleDropdown() { hideAdvice(); showDropdown.value = !showDropdown.value }
function closeDropdown() { showDropdown.value = false }
function onSwitchAccount(accId: string) { system.switchAccount(accId); closeDropdown() }
function openAdd() { editForm.value = { id: '', label: '', flaskUrl: '' }; showAccountDialog.value = true; closeDropdown() }
function openEdit(acc: AccountEntry) { editForm.value = { ...acc }; showAccountDialog.value = true; closeDropdown() }
function saveAccount() { if (!editForm.value.id || !editForm.value.label) return; system.addAccount({ ...editForm.value }); showAccountDialog.value = false }
function openMarketAdvice(event: Event) { closeDropdown(); showAdvice(event, '399001.SZ') }

async function onConnectionChanged() {
  gatewayMode.value = isGatewayMode()
  await system.syncAccountsFromGateway()
  system.fetchStatus()
  system.fetchConnection()
}
function onClickOutside(e: MouseEvent) { if (dropdownRef.value && !dropdownRef.value.contains(e.target as Node)) closeDropdown() }

onMounted(() => {
  document.addEventListener('click', onClickOutside)
  clock = setInterval(() => { now.value = Date.now() }, 1000)
})
onUnmounted(() => {
  document.removeEventListener('click', onClickOutside)
  if (clock) clearInterval(clock)
})
</script>

<template>
  <header class="bg-white/90 backdrop-blur-md border-b border-slate-200/70 sticky top-0 z-40">
    <!-- Row 1: Brand + Account + Settings + Assets -->
    <div class="px-3 md:px-6 py-2.5 flex items-center justify-between gap-2">
      <div class="flex items-center gap-2 md:gap-3 min-w-0">
        <div class="flex items-center gap-2 flex-shrink-0">
          <div class="w-8 h-8 rounded-lg bg-blue-600 flex items-center justify-center shadow-md shadow-blue-100 flex-shrink-0">
            <span class="text-white font-black text-[10px]">MQ</span>
          </div>
          <h1 class="text-sm md:text-base font-bold text-slate-800 leading-tight">
            miniQMT<span class="text-slate-400 font-normal text-[10px] ml-0.5">2.0</span>
          </h1>
          <span class="badge-slate !text-[9px] !px-1.5 hidden sm:inline" title="web2.0 为只读监控端，所有写操作请使用 web1.0">只读监控</span>
        </div>

        <!-- Account switcher -->
        <div class="relative" ref="dropdownRef">
          <button @click="toggleDropdown" class="flex min-h-9 max-w-[150px] sm:max-w-none items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-semibold bg-blue-50 text-blue-700 border border-blue-200/60 hover:bg-blue-100 transition-colors">
            <span class="dot-green"></span>
            <span class="truncate">{{ system.currentAccount.label || system.currentAccount.id }}</span>
            <svg class="w-3 h-3 opacity-40 transition-transform" :class="showDropdown ? 'rotate-180' : ''" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>
          </button>
          <div v-show="showDropdown" class="absolute top-full left-0 mt-1.5 w-72 max-w-[calc(100vw-24px)] bg-white rounded-lg shadow-lg border border-slate-200/80 z-50">
            <div class="p-1.5">
              <button v-for="acc in system.accounts" :key="acc.id" @click="onSwitchAccount(acc.id)"
                :class="['w-full text-left px-3 py-2 rounded-lg text-sm transition-all flex items-center justify-between', acc.id === system.currentAccountId ? 'bg-blue-50 text-blue-700' : 'hover:bg-slate-50 text-slate-600']">
                <span class="min-w-0 truncate">{{ acc.label }}</span>
                <span class="text-[10px] text-slate-400 font-mono">{{ acc.id.slice(0,4) }}***</span>
                <span @click.stop="openEdit(acc)" class="text-slate-300 hover:text-slate-500 cursor-pointer p-0.5"><svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/></svg></span>
              </button>
            </div>
            <div class="border-t border-slate-100 px-1.5 py-1"><button @click="openAdd" class="w-full text-left px-3 py-1.5 rounded-lg text-xs text-blue-600 hover:bg-blue-50">+ 添加账户</button></div>
          </div>
        </div>
        <button
          type="button"
          class="inline-flex h-5 flex-shrink-0 items-center rounded-full border border-amber-200 bg-amber-50 px-1.5 text-[10px] font-semibold leading-none text-amber-700 hover:bg-amber-100 focus:outline-none focus:ring-2 focus:ring-amber-300"
          title="MACD 操作建议"
          @mouseenter="openMarketAdvice($event)"
          @mouseleave="hideAdvice()"
          @focus="openMarketAdvice($event)"
          @blur="hideAdvice()"
          @click.stop="openMarketAdvice($event)"
        >操作建议</button>

        <button @click="showConnSettings = true" class="w-9 h-9 flex items-center justify-center rounded-lg text-slate-400 hover:text-slate-600 hover:bg-slate-100 transition-colors flex-shrink-0" title="连接设置">
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
        </button>
      </div>

      <!-- Assets (right) -->
      <div class="hidden sm:flex items-center gap-2 md:gap-3 ml-auto">
        <div class="text-[10px] md:text-xs text-slate-500"><span class="text-slate-400">可用</span> <strong class="text-slate-700">¥{{ assetMoney(system.account.availableBalance) }}</strong></div>
        <div class="text-[10px] md:text-xs text-slate-500"><span class="text-slate-400" :title="system.valuationDisplay.detail">{{ system.valuationDisplay.label }}</span> <strong class="text-slate-700">¥{{ assetMoney(system.valuationDisplay.value) }}</strong></div>
        <div class="text-[10px] md:text-xs text-slate-500"><span class="text-slate-400">总资产</span> <strong class="text-slate-700">¥{{ assetMoney(system.account.totalAssets) }}</strong></div>
      </div>
    </div>

    <!-- Mobile asset bar -->
    <div class="sm:hidden touch-strip no-scrollbar px-3 pb-1 text-[11px] text-slate-500">
      <span class="metric-tile !min-w-[112px] !px-2.5 !py-1.5">可用 <strong class="block truncate text-slate-700">¥{{ assetMoney(system.account.availableBalance) }}</strong></span>
      <span class="metric-tile !min-w-[112px] !px-2.5 !py-1.5" :title="system.valuationDisplay.detail">{{ system.valuationDisplay.label }} <strong class="block truncate text-slate-700">¥{{ assetMoney(system.valuationDisplay.value) }}</strong></span>
      <span class="metric-tile !min-w-[112px] !px-2.5 !py-1.5">总资产 <strong class="block truncate text-slate-700">¥{{ assetMoney(system.account.totalAssets) }}</strong></span>
    </div>

    <div v-if="system.account.valuation?.broker_identity_diff" class="px-3 md:px-6 pb-1 text-[11px] text-amber-700" :title="system.valuationDisplay.detail">
      原始资产差额 ¥{{ assetMoney(system.account.valuation.broker_identity_diff) }} · {{ system.account.valuation.reason }}
    </div>

    <!-- Row 2: 只读状态徽章 + 连接/新鲜度指示 -->
    <div class="px-3 md:px-6 pb-2 flex items-center justify-between gap-2 flex-wrap">
      <div class="touch-strip no-scrollbar flex-1 min-w-0 gap-1.5">
        <StateBadge v-for="s in SWITCHES" :key="s.key" :label="s.label" :value="s.get()" />
      </div>

      <div class="flex items-center gap-1.5 ml-auto flex-shrink-0">
        <span :class="['badge text-[10px]', system.connected ? 'badge-green' : 'badge-red']"
          :title="`QMT 连接状态 · 最后检查 ${connectionAge}`">
          <span :class="system.connected ? 'dot-green' : 'dot-red'"></span>QMT{{ system.connected ? '·OK' : '·断' }}
        </span>
        <span class="hidden sm:inline" :class="['badge text-[10px]', sseHealthy ? 'badge-green' : 'badge-slate']"
          :title="sseHealthy ? 'SSE 实时推送正常' : 'SSE 不可用，依赖轮询（数据仍在更新）'">SSE</span>
        <span :class="['text-[10px] font-mono hidden sm:inline', freshnessClass(statusFreshness)]"
          :title="`系统状态最后更新 ${statusAge}`">{{ statusAge }}</span>
        <span v-if="connectionFreshness === 'dead'" class="badge badge-red text-[10px]" title="连接状态长时间未刷新">连接检查停滞</span>
      </div>
    </div>

    <!-- 数据失联横幅：监控端最重要的告警 -->
    <div v-if="dataStale" class="px-3 md:px-6 pb-2">
      <div class="rounded-md bg-red-50 border border-red-200 px-3 py-2 text-[11px] text-red-700">
        ⚠ 数据已超过 90 秒未更新，界面显示的可能是陈旧快照。请检查后端服务与网络连接。
      </div>
    </div>
  </header>

  <!-- Account edit dialog -->
  <Teleport to="body">
    <div v-if="showAccountDialog" class="modal-overlay" @click.self="showAccountDialog = false">
      <div class="modal-content w-[420px] max-w-[96vw]">
        <div class="px-6 py-4 border-b border-slate-100"><h3 class="text-lg font-semibold text-slate-800">{{ system.accounts.some(a => a.id === editForm.id) ? '编辑账户' : '添加账户' }}</h3></div>
        <div class="p-6 space-y-4">
          <div><label class="label-text">账户 ID <span class="text-red-400">*</span></label><input v-model="editForm.id" placeholder="如 TEST_ACC_1" class="input-field" :disabled="system.accounts.some(a => a.id === editForm.id)" /></div>
          <div><label class="label-text">显示名称 <span class="text-red-400">*</span></label><input v-model="editForm.label" placeholder="如 账户A" class="input-field" /></div>
          <div><label class="label-text">Flask 直连地址 <span class="text-slate-400 font-normal">(可选)</span></label><input v-model="editForm.flaskUrl" placeholder="http://127.0.0.1:5000" class="input-field" /><p class="text-[10px] text-slate-400 mt-1">使用 Flask 直连模式时单独指定地址</p></div>
        </div>
        <div class="px-6 py-3 bg-slate-50/80 rounded-b-lg flex justify-between">
          <button v-if="system.accounts.some(a => a.id === editForm.id) && system.accounts.length > 1" @click="system.removeAccount(editForm.id); showAccountDialog = false" class="btn-ghost !text-red-500 text-xs">删除</button><span v-else></span>
          <div class="flex gap-2"><button @click="showAccountDialog = false" class="btn-ghost">取消</button><button @click="saveAccount" :disabled="!editForm.id || !editForm.label" class="btn-primary">保存</button></div>
        </div>
      </div>
    </div>
  </Teleport>
  <ConnectionSettings v-if="showConnSettings" @close="showConnSettings = false" @changed="onConnectionChanged" />
</template>
