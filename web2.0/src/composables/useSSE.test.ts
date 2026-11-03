import { describe, it, expect, vi, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { useSSE } from './useSSE'
import { useSystemStore } from '../stores/system'
import * as flaskApi from '../api/flask'

class FakeEventSource {
  static instances: FakeEventSource[] = []
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: (() => void) | null = null
  close = vi.fn()
  constructor() { FakeEventSource.instances.push(this) }
}

describe('资产轮询与 SSE 一致性', () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.useRealTimers() })
  it('轮询和 SSE 保留柜台原值，并共同展示行情估值', async () => {
    localStorage.clear()
    setActivePinia(createPinia())
    vi.useFakeTimers()
    vi.stubGlobal('EventSource', FakeEventSource)
    const wrapper = mount(defineComponent({
      setup() { useSSE().connect(); return () => h('div') },
    }))
    const system = useSystemStore()
    const valuation = {
      status: 'explained', quote_market_value: 62134, broker_identity_diff: -6909,
      reason: '行情估值解释柜台差额',
    }
    vi.spyOn(flaskApi, 'getStatus').mockResolvedValue({
      account: { id: 'TEST', availableBalance: 431612.83, totalAssets: 493746.83,
        maxHoldingValue: 69043, timestamp: '2026-11-02 14:09:44', valuation },
      settings: {}, isMonitoring: true,
    } as any)
    await system.fetchStatus()
    expect(system.valuationDisplay.value).toBe(62134)
    const stream = FakeEventSource.instances[FakeEventSource.instances.length - 1]!
    stream.onmessage!({ data: JSON.stringify({
      timestamp: '2026-11-02 14:09:45',
      account_info: { available: 431612.83, total_asset: 493746.83,
        market_value: 69043, valuation },
    }) } as MessageEvent)
    expect(system.account.maxHoldingValue).toBe(69043)
    expect(system.valuationDisplay.value).toBe(62134)
    expect(system.valuationDisplay.label).toBe('行情估值')
    wrapper.unmount()
  })
})
