import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import type { GridLedgerDetail, GridSession, GridTrade, RiskTemplate } from '../types'
import * as flaskApi from '../api/flask'

export const useGridStore = defineStore('grid', () => {
  const sessions = ref<GridSession[]>([])
  const tradesBySession = ref<Record<number, GridTrade[]>>({})
  const tradeTotalsBySession = ref<Record<number, number>>({})
  const ledgerBySession = ref<Record<number, GridLedgerDetail>>({})
  const riskTemplates = ref<Record<string, RiskTemplate>>({})
  const templates = ref<any[]>([])
  const loading = ref(false)
  const tradesLoading = ref(false)
  const ledgerLoading = ref(false)
  const ledgerError = ref('')
  const activeSessions = computed(() => sessions.value.filter(s => s.status === 'active' || s.status === 'stopping'))

  const normalizeStockCode = (code: string) => String(code || '').split('.')[0]
  const activeStockCodes = computed(() =>
    new Set(activeSessions.value.map(s => normalizeStockCode(s.stock_code)))
  )

  function isActiveForStock(stockCode: string): boolean {
    return activeStockCodes.value.has(normalizeStockCode(stockCode))
  }

  async function fetchSessions() {
    sessions.value = await flaskApi.getAllGridSessions()
  }

  async function fetchRiskTemplates() {
    riskTemplates.value = await flaskApi.getGridRiskTemplates()
  }

  async function fetchTemplates() {
    templates.value = await flaskApi.getGridTemplates()
  }

  async function fetchAll() {
    loading.value = true
    await Promise.all([fetchSessions(), fetchRiskTemplates(), fetchTemplates()])
    loading.value = false
  }

  async function startSession(params: any) {
    const r = await flaskApi.startGrid(params)
    if (r?.success) await fetchSessions()
    return r
  }

  async function stopSession(sessionId: number) {
    const r = await flaskApi.stopGrid(sessionId)
    if (r?.success) await fetchSessions()
    return r
  }

  async function fetchTrades(sessionId: number, limit = 20, offset = 0) {
    tradesLoading.value = true
    try {
      const r = await flaskApi.getGridTrades(sessionId, limit, offset)
      tradesBySession.value = { ...tradesBySession.value, [sessionId]: r.trades }
      tradeTotalsBySession.value = { ...tradeTotalsBySession.value, [sessionId]: r.totalCount }
      return r
    } finally {
      tradesLoading.value = false
    }
  }

  async function fetchLedger(sessionId: number, limit = 50, offset = 0) {
    ledgerLoading.value = true
    ledgerError.value = ''
    try {
      const r = await flaskApi.getGridLedger(sessionId, limit, offset)
      if (r.error || !r.summary) {
        ledgerError.value = r.error || '账本数据不可用'
        return null
      }
      const detail = r as GridLedgerDetail
      ledgerBySession.value = { ...ledgerBySession.value, [sessionId]: detail }
      tradesBySession.value = { ...tradesBySession.value, [sessionId]: detail.trades }
      tradeTotalsBySession.value = { ...tradeTotalsBySession.value, [sessionId]: detail.totalCount }
      return detail
    } finally {
      ledgerLoading.value = false
    }
  }

  function getSessionByStock(stockCode: string): GridSession | undefined {
    const normalize = (code: string) => (code || '').split('.')[0]
    return sessions.value.find(s => s.stock_code === stockCode || normalize(s.stock_code) === normalize(stockCode))
  }

  return {
    sessions, activeSessions, activeStockCodes, tradesBySession, tradeTotalsBySession, ledgerBySession,
    riskTemplates, templates, loading, tradesLoading, ledgerLoading, ledgerError,
    fetchSessions, fetchRiskTemplates, fetchTemplates, fetchAll, startSession, stopSession, fetchTrades, fetchLedger, getSessionByStock, isActiveForStock,
  }
})
