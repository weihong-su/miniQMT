import type { AccountInfo } from '../types'

/** 统一账户估值展示；柜台原始字段始终保留在账户对象中。 */
export function accountValuationDisplay(account: AccountInfo) {
  const valuation = account.valuation
  const quote = valuation?.quote_market_value
  const validAccount = Number.isFinite(account.totalAssets) && account.totalAssets > 0
  const useQuote = valuation && valuation.status !== 'unavailable'
    && typeof quote === 'number' && Number.isFinite(quote) && quote >= 0
  const raw = account.maxHoldingValue
  const value = useQuote ? quote : Number.isFinite(raw) ? raw : null
  const label = useQuote ? valuation.status === 'static' ? '静态估值' : '行情估值' : '柜台市值'
  const money = (v: number | null | undefined) =>
    typeof v === 'number' && Number.isFinite(v) ? v.toLocaleString('zh-CN', { minimumFractionDigits: 2 }) : '--'
  const detail = validAccount
    ? [
        valuation?.reason,
        '柜台市值 ' + money(raw),
        useQuote ? '行情估值 ' + money(quote) : '',
        valuation?.broker_identity_diff != null ? '原始资产差额 ' + money(valuation.broker_identity_diff) : '',
        valuation?.quote_asof ? '行情时间 ' + valuation.quote_asof : '',
        valuation?.asset_asof ? '资金取数时间 ' + valuation.asset_asof : '',
      ].filter(Boolean).join('；')
    : '账户信息待更新'
  return { value: validAccount ? value : null, label, detail, validAccount }
}
