import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import * as flaskApi from '../api/flask'
import { loadAccounts, saveAccounts, getCurrentAccountId, setCurrentAccountId } from '../api/accounts'
import type { AccountEntry } from '../api/accounts'

export const useSystemStore = defineStore('system', () => {
  const connected = ref(false)
  const accounts = ref<AccountEntry[]>(loadAccounts())
  const currentAccountId = ref(getCurrentAccountId())
  const account = ref({
    id: '--', availableBalance: 0, maxHoldingValue: 0, totalAssets: 0, timestamp: '--'
  })
  const isMonitoring = ref(false)
  const autoTrading = ref(false)
  const allowBuy = ref(true)
  const allowSell = ref(true)
  const simulationMode = ref(false)
  const positionMonitorRunning = ref(false)
  const lastUpdateTime = ref('')

  const currentAccount = computed(() =>
    accounts.value.find(a => a.id === currentAccountId.value) || accounts.value[0]
  )

  function switchAccount(accountId: string) {
    currentAccountId.value = accountId
    setCurrentAccountId(accountId)
    // reset stale data
    account.value = { id: accountId, availableBalance: 0, maxHoldingValue: 0, totalAssets: 0, timestamp: '--' }
  }

  function addAccount(entry: AccountEntry) {
    const exists = accounts.value.find(a => a.id === entry.id)
    if (exists) Object.assign(exists, entry)
    else accounts.value.push(entry)
    saveAccounts(accounts.value)
  }

  function removeAccount(accountId: string) {
    accounts.value = accounts.value.filter(a => a.id !== accountId)
    saveAccounts(accounts.value)
    if (currentAccountId.value === accountId && accounts.value.length > 0) {
      switchAccount(accounts.value[0].id)
    }
  }

  async function fetchStatus() {
    const r = await flaskApi.getStatus()
    if (!r) return
    account.value = r.account
    isMonitoring.value = r.settings.isMonitoring
    autoTrading.value = r.settings.enableAutoTrading
    allowBuy.value = r.settings.allowBuy
    allowSell.value = r.settings.allowSell
    simulationMode.value = r.settings.simulationMode
    positionMonitorRunning.value = r.settings.positionMonitorRunning
    lastUpdateTime.value = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  }

  async function fetchConnection() {
    connected.value = await flaskApi.getConnectionStatus()
  }

  async function toggleMonitor(on: boolean) {
    await flaskApi.toggleMonitor(on)
    isMonitoring.value = on
  }

  return {
    connected, accounts, currentAccountId, currentAccount, account,
    isMonitoring, autoTrading, allowBuy, allowSell,
    simulationMode, positionMonitorRunning, lastUpdateTime,
    switchAccount, addAccount, removeAccount,
    fetchStatus, fetchConnection, toggleMonitor,
  }
})
