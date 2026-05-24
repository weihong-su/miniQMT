export function fmtMoney(v: number | null | undefined, digits = 2): string {
  if (v == null || isNaN(v)) return '--'
  return '¥' + v.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

export function fmtPercent(v: number | null | undefined, digits = 2): string {
  if (v == null || isNaN(v)) return '--'
  return (v >= 0 ? '+' : '') + v.toFixed(digits) + '%'
}

export function fmtNumber(v: number | null | undefined, digits = 0): string {
  if (v == null || isNaN(v)) return '--'
  return v.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

export function fmtPrice(v: number | null | undefined): string {
  if (v == null || isNaN(v) || v === 0) return '--'
  return v.toFixed(2)
}

export function fmtTime(ts: string | null | undefined): string {
  if (!ts) return '--'
  return ts.replace('T', ' ').substring(0, 19)
}

export function profitClass(v: number): string {
  if (v > 0) return 'text-profit'
  if (v < 0) return 'text-loss'
  return 'text-slate-500'
}

export function riskLabel(level: string): string {
  const map: Record<string, string> = { aggressive: '激进型', moderate: '稳健型', conservative: '保守型' }
  return map[level] || level
}
