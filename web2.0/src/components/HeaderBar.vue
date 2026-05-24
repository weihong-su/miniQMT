<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { useSystemStore } from '../stores/system'
import { useConfigStore } from '../stores/config'
import { useSSE } from '../composables/useSSE'
import { usePolling } from '../composables/usePolling'
import * as xqApi from '../api/xtquant'
import type { AccountEntry } from '../api/accounts'
import ConnectionSettings from './ConnectionSettings.vue'

const system = useSystemStore()
const config = useConfigStore()
const { healthy: sseHealthy, connect: sseConnect } = useSSE()
const { start: startPolling, stop: stopPolling } = usePolling()

const showAccountDialog = ref(false)
const showConnSettings = ref(false)
const showDropdown = ref(false)
const editForm = ref<AccountEntry>({ id: '', label: '', flaskUrl: '' })
const dropdownRef = ref<HTMLElement | null>(null)
const stopProfitEnabled = ref(false)
const stopProfitLoading = ref(false)

function toggleDropdown() { showDropdown.value = !showDropdown.value }
function closeDropdown() { showDropdown.value = false }
function onSwitchAccount(accId: string) { system.switchAccount(accId); closeDropdown() }
function openAdd() { editForm.value = { id: '', label: '', flaskUrl: '' }; showAccountDialog.value = true; closeDropdown() }
function openEdit(acc: AccountEntry) { editForm.value = { ...acc }; showAccountDialog.value = true; closeDropdown() }
function saveAccount() { if (!editForm.value.id || !editForm.value.label) return; system.addAccount({ ...editForm.value }); showAccountDialog.value = false }
function onConnectionChanged() { system.fetchStatus(); system.fetchConnection() }
function onClickOutside(e: MouseEvent) { if (dropdownRef.value && !dropdownRef.value.contains(e.target as Node)) closeDropdown() }

function toggleMonitoring() {
  const next = !system.isMonitoring
  system.toggleMonitor(next).then(() => { if (next) startPolling(); else stopPolling() })
}
async function toggleStopProfit() {
  stopProfitLoading.value = true; const next = !stopProfitEnabled.value
  await xqApi.toggleStopProfit(next); stopProfitEnabled.value = next; stopProfitLoading.value = false
}
function toggleConfigBool(key: string) {
  const val = !(config.config as any)[key]; (config.config as any)[key] = val
  config.saveConfig({ [key]: val } as any)
}
async function loadStopProfitStatus() { try { const d = await xqApi.getStopProfitStatus(); if (d?.config) stopProfitEnabled.value = d.config.enabled } catch {} }

onMounted(() => { document.addEventListener('click', onClickOutside); loadStopProfitStatus() })
onUnmounted(() => document.removeEventListener('click', onClickOutside))
</script>

<template>
  <header class="bg-white/80 backdrop-blur-md border-b border-slate-200/60 sticky top-0 z-40">
    <!-- Row 1: Brand + Account + Settings + Assets -->
    <div class="px-4 md:px-6 py-2 flex items-center justify-between gap-2 flex-wrap">
      <div class="flex items-center gap-2 md:gap-3 flex-wrap">
        <div class="flex items-center gap-2">
          <div class="w-7 h-7 rounded-lg bg-gradient-to-br from-blue-500 to-blue-700 flex items-center justify-center shadow-md shadow-blue-200 flex-shrink-0">
            <span class="text-white font-black text-[10px]">MQ</span>
          </div>
          <h1 class="text-sm md:text-base font-bold text-slate-800 leading-tight hidden sm:block">miniQMT<span class="text-slate-400 font-normal text-[10px] ml-0.5">2.0</span></h1>
        </div>

        <!-- Account switcher -->
        <div class="relative" ref="dropdownRef">
          <button @click="toggleDropdown" class="flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-semibold bg-blue-50 text-blue-700 border border-blue-200/60 hover:bg-blue-100 transition-colors">
            <span class="dot-green"></span> {{ system.currentAccount.label || system.currentAccount.id }}
            <svg class="w-3 h-3 opacity-40 transition-transform" :class="showDropdown ? 'rotate-180' : ''" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>
          </button>
          <div v-show="showDropdown" class="absolute top-full left-0 mt-1.5 w-64 bg-white rounded-xl shadow-lg border border-slate-200/80 z-50">
            <div class="p-1.5">
              <button v-for="acc in system.accounts" :key="acc.id" @click="onSwitchAccount(acc.id)"
                :class="['w-full text-left px-3 py-2 rounded-lg text-sm transition-all flex items-center justify-between', acc.id === system.currentAccountId ? 'bg-blue-50 text-blue-700' : 'hover:bg-slate-50 text-slate-600']">
                <span>{{ acc.label }}</span>
                <span class="text-[10px] text-slate-400 font-mono">{{ acc.id.slice(0,4) }}***</span>
                <span @click.stop="openEdit(acc)" class="text-slate-300 hover:text-slate-500 cursor-pointer p-0.5"><svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/></svg></span>
              </button>
            </div>
            <div class="border-t border-slate-100 px-1.5 py-1"><button @click="openAdd" class="w-full text-left px-3 py-1.5 rounded-lg text-xs text-blue-600 hover:bg-blue-50">+ 添加账户</button></div>
          </div>
        </div>

        <button @click="showConnSettings = true" class="p-1.5 rounded-lg text-slate-400 hover:text-slate-600 hover:bg-slate-100 transition-colors flex-shrink-0" title="连接设置">
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
        </button>
      </div>

      <!-- Assets (right) -->
      <div class="flex items-center gap-2 md:gap-3 ml-auto">
        <div class="text-[10px] md:text-xs text-slate-500"><span class="text-slate-400">可用</span> <strong class="text-slate-700">¥{{ system.account.availableBalance.toLocaleString() }}</strong></div>
        <div class="hidden sm:block text-[10px] md:text-xs text-slate-500"><span class="text-slate-400">市值</span> <strong class="text-slate-700">¥{{ system.account.maxHoldingValue.toLocaleString() }}</strong></div>
        <div class="text-[10px] md:text-xs text-slate-500"><span class="text-slate-400">总资产</span> <strong class="text-slate-700">¥{{ system.account.totalAssets.toLocaleString() }}</strong></div>
      </div>
    </div>

    <!-- Row 2: Controls (left) + Status badges (right) -->
    <div class="px-4 md:px-6 pb-2 flex items-center justify-between gap-1.5 flex-wrap text-[11px]">
      <!-- Control toggles -->
      <div class="flex items-center gap-1 flex-wrap">
        <button @click="toggleMonitoring" :class="['px-2 py-1 rounded-md font-semibold transition-colors', system.isMonitoring ? 'bg-red-50 text-red-600' : 'bg-emerald-50 text-emerald-600']">{{ system.isMonitoring ? '停止监控' : '开始监控' }}</button>
        <button @click="toggleStopProfit" :disabled="stopProfitLoading" :class="['px-2 py-1 rounded-md font-semibold transition-colors', stopProfitEnabled ? 'bg-amber-50 text-amber-600' : 'bg-emerald-50 text-emerald-600']">{{ stopProfitLoading ? '...' : (stopProfitEnabled ? '禁用动态止盈' : '开启动态止盈') }}</button>
        <span class="text-slate-300 hidden sm:inline">|</span>
        <label class="flex items-center gap-1 px-1.5 py-1 rounded cursor-pointer hover:bg-slate-100 transition-colors"><input type="checkbox" :checked="config.config.allowBuy" @change="toggleConfigBool('allowBuy')" class="w-3 h-3 rounded accent-blue-600" /><span :class="config.config.allowBuy ? 'text-slate-700' : 'text-slate-400'">买</span></label>
        <label class="flex items-center gap-1 px-1.5 py-1 rounded cursor-pointer hover:bg-slate-100 transition-colors"><input type="checkbox" :checked="config.config.allowSell" @change="toggleConfigBool('allowSell')" class="w-3 h-3 rounded accent-blue-600" /><span :class="config.config.allowSell ? 'text-slate-700' : 'text-slate-400'">卖</span></label>
        <label class="flex items-center gap-1 px-1.5 py-1 rounded cursor-pointer hover:bg-slate-100 transition-colors"><input type="checkbox" :checked="config.config.simulationMode" @change="toggleConfigBool('simulationMode')" class="w-3 h-3 rounded accent-amber-500" /><span :class="config.config.simulationMode ? 'text-amber-600 font-medium' : 'text-slate-400'">模拟</span></label>
        <label class="flex items-center gap-1 px-1.5 py-1 rounded cursor-pointer hover:bg-slate-100 transition-colors"><input type="checkbox" :checked="config.config.globalAllowBuySell" @change="toggleConfigBool('globalAllowBuySell')" class="w-3 h-3 rounded accent-blue-600" /><span :class="config.config.globalAllowBuySell ? 'text-slate-700' : 'text-slate-400'">总开关</span></label>
      </div>

      <!-- Status badges (right) -->
      <div class="flex items-center gap-1.5 ml-auto flex-shrink-0">
        <span :class="['badge text-[10px]', system.isMonitoring ? 'badge-green' : 'badge-red']"><span :class="system.isMonitoring ? 'dot-green' : 'dot-red'"></span>监控:{{ system.isMonitoring ? 'ON' : 'OFF' }}</span>
        <span :class="['badge text-[10px]', system.connected ? 'badge-green' : 'badge-amber']"><span :class="system.connected ? 'dot-green' : 'dot-amber'"></span>QMT:{{ system.connected ? 'OK' : '未连接' }}</span>
        <span class="hidden sm:inline" :class="['badge text-[10px]', sseHealthy ? 'badge-green' : 'badge-amber']" :title="sseHealthy ? 'SSE实时推送正常' : 'SSE断开, 使用轮询(功能不受影响)'">SSE</span>
        <span v-if="system.lastUpdateTime" class="text-[10px] text-slate-400 font-mono hidden sm:inline">{{ system.lastUpdateTime }}</span>
      </div>
    </div>
  </header>

  <!-- Account edit dialog -->
  <Teleport to="body">
    <div v-if="showAccountDialog" class="modal-overlay" @click.self="showAccountDialog = false">
      <div class="modal-content w-[420px]">
        <div class="px-6 py-4 border-b border-slate-100"><h3 class="text-lg font-semibold text-slate-800">{{ system.accounts.some(a => a.id === editForm.id) ? '编辑账户' : '添加账户' }}</h3></div>
        <div class="p-6 space-y-4">
          <div><label class="label-text">账户 ID <span class="text-red-400">*</span></label><input v-model="editForm.id" placeholder="如 25105132" class="input-field" :disabled="system.accounts.some(a => a.id === editForm.id)" /></div>
          <div><label class="label-text">显示名称 <span class="text-red-400">*</span></label><input v-model="editForm.label" placeholder="如 账户A" class="input-field" /></div>
          <div><label class="label-text">Flask 直连地址 <span class="text-slate-400 font-normal">(可选)</span></label><input v-model="editForm.flaskUrl" placeholder="http://127.0.0.1:5000" class="input-field" /><p class="text-[10px] text-slate-400 mt-1">使用 Flask 直连模式时单独指定地址</p></div>
        </div>
        <div class="px-6 py-3 bg-slate-50/80 rounded-b-2xl flex justify-between">
          <button v-if="system.accounts.some(a => a.id === editForm.id) && system.accounts.length > 1" @click="system.removeAccount(editForm.id); showAccountDialog = false" class="btn-ghost !text-red-500 text-xs">删除</button><span v-else></span>
          <div class="flex gap-2"><button @click="showAccountDialog = false" class="btn-ghost">取消</button><button @click="saveAccount" :disabled="!editForm.id || !editForm.label" class="btn-primary">保存</button></div>
        </div>
      </div>
    </div>
  </Teleport>
  <ConnectionSettings v-if="showConnSettings" @close="showConnSettings = false" @changed="onConnectionChanged" />
</template>
