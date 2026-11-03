import { describe, it, expect } from 'vitest'
import { accountValuationDisplay } from './valuation'
import type { AccountInfo, AccountValuation } from '../types'

const raw: AccountInfo = {
  id: 'TEST', availableBalance: 431612.83, maxHoldingValue: 69043,
  totalAssets: 493746.83, timestamp: '2026-11-02 14:09:44',
}
const valuation: AccountValuation = {
  status: 'explained', reason: '行情估值解释柜台差额', quote_market_value: 62134,
  broker_identity_diff: -6909, quote_identity_diff: 0, quote_asof: '2026-11-02T14:09:44',
}

describe('账户估值口径', () => {
  it('行情估值与原始市值分别保留，并显示差额证据', () => {
    const account = { ...raw, valuation }
    const display = accountValuationDisplay(account)
    expect(display.value).toBe(62134)
    expect(display.label).toBe('行情估值')
    expect(display.detail).toContain('69,043')
    expect(display.detail).toContain('-6,909')
    expect(account.maxHoldingValue).toBe(69043)
  })
  it('旧后端缺少估值字段时显示柜台来源', () => {
    expect(accountValuationDisplay(raw)).toMatchObject({ label: '柜台市值', value: 69043 })
  })
  it('证据不足时返回柜台口径，不将缺失值转换为零', () => {
    expect(accountValuationDisplay({ ...raw, valuation: {
      status: 'unavailable', quote_market_value: null,
    } })).toMatchObject({ label: '柜台市值', value: 69043 })
  })
  it('盘外估值明确标记为静态', () => {
    expect(accountValuationDisplay({ ...raw, valuation: {
      ...valuation, status: 'static',
    } }).label).toBe('静态估值')
  })
  it('无效行情估值不覆盖柜台数据', () => {
    expect(accountValuationDisplay({ ...raw, valuation: {
      ...valuation, quote_market_value: Number.NaN,
    } }).value).toBe(69043)
  })
  it('资产未就绪时不展示默认零读数', () => {
    expect(accountValuationDisplay({ ...raw, totalAssets: 0, maxHoldingValue: 0 }).value).toBeNull()
  })
  it('HTTP 与 SSE 相同载荷采用相同的展示口径', () => {
    const first = accountValuationDisplay({ ...raw, valuation })
    const second = accountValuationDisplay(JSON.parse(JSON.stringify({ ...raw, valuation })))
    expect(second).toEqual(first)
  })
})
