import type { PriceMatrixItem } from '../types/api'

export function normalizePriceRule<T extends PriceMatrixItem>(
  item: T,
  scope: string,
): T {
  if (scope === 'any') {
    return {
      ...item,
      min_limit_pct: undefined,
      // Compatibility field only. The admin never creates or preserves a
      // short-window sales condition.
      max_5h_pct: undefined,
    }
  }
  return { ...item, max_5h_pct: undefined, max_weekly_pct: undefined }
}

export function priceRuleSignature(item: PriceMatrixItem, scope: string) {
  const normalized = normalizePriceRule(item, scope)
  return [
    normalized.tier_id,
    normalized.duration_id,
    normalized.limit_scope_id,
    normalized.min_limit_pct ?? '',
    normalized.max_weekly_pct ?? '',
  ].join(':')
}

export function priceRuleToWire<T extends PriceMatrixItem & { draftId?: string }>(
  item: T,
  scope: string,
): PriceMatrixItem {
  const normalized = normalizePriceRule(item, scope)
  const { draftId: _draftId, ...wire } = normalized
  return wire
}
