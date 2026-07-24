import { reactive } from 'vue'
import { getMacdAdvice } from '../api/flask'

// 模块级单例状态：全局共享一个悬浮窗
const state = reactive({
  visible: false,
  x: 0,
  y: 0,
  data: null as any,
})

const cache: Record<string, { data: any; ts: number }> = {}
const TTL = 300000 // 5 分钟

export function useAdviceTooltip() {
  async function show(event: MouseEvent, code: string) {
    if (!code) return
    const el = event.currentTarget as HTMLElement
    const rect = el.getBoundingClientRect()
    state.x = rect.left + window.scrollX
    state.y = rect.bottom + window.scrollY + 8

    const now = Date.now()
    const cached = cache[code]
    let data: any
    if (cached && now - cached.ts < TTL) {
      data = cached.data
    } else {
      data = await getMacdAdvice(code)
      cache[code] = { data, ts: now }
    }

    // 数据不足或网关模式降级：静默不显示
    if (!data || data.status !== 'success') {
      state.visible = false
      return
    }
    state.data = data
    state.visible = true
  }

  function hide() {
    state.visible = false
  }

  return { state, show, hide }
}
