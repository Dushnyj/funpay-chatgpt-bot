import type { LimitScope } from '../types/api'

type OfferScope = Pick<LimitScope, 'code' | 'is_enabled'>
type OrderedOfferScope = Pick<LimitScope, 'id' | 'code'>

const OFFER_SCOPE_ORDER: Record<string, number> = {
  any: 10,
  codex: 20,
}

export function compareOfferScopes(left: OrderedOfferScope, right: OrderedOfferScope) {
  const leftCode = left.code.toLowerCase()
  const rightCode = right.code.toLowerCase()
  const rankDifference = (OFFER_SCOPE_ORDER[leftCode] ?? Number.MAX_SAFE_INTEGER)
    - (OFFER_SCOPE_ORDER[rightCode] ?? Number.MAX_SAFE_INTEGER)
  if (rankDifference !== 0) return rankDifference
  const codeDifference = leftCode.localeCompare(rightCode, 'en')
  return codeDifference || left.id - right.id
}

export function isSupportedOfferScopeCode(code: string) {
  const normalized = code.toLowerCase()
  return normalized === 'any' || normalized === 'codex'
}

export function offerScopeUnavailableReason(scope: OfferScope | undefined | null): string | null {
  if (!scope) return 'тип лимита отсутствует в справочнике'
  const code = scope.code.toLowerCase()
  if (code === 'chat') return 'устаревший тип лимита недоступен'
  if (!isSupportedOfferScopeCode(code)) return `тип лимита «${scope.code}» не поддерживается`
  if (!scope.is_enabled) return 'тип лимита выключен'
  return null
}

export function isAvailableOfferScope(scope: OfferScope | undefined | null) {
  return offerScopeUnavailableReason(scope) === null
}

export function offerScopeDisplayName(scope: Pick<LimitScope, 'code' | 'name'> | undefined | null) {
  if (!scope) return 'Неизвестный тип'
  return scope.code.toLowerCase() === 'chat' ? 'Устаревший тип' : scope.name
}

export function offerScopeDisplayCode(scope: Pick<LimitScope, 'code'> | undefined | null) {
  if (!scope) return 'UNKNOWN'
  return scope.code.toLowerCase() === 'chat' ? 'LEGACY' : scope.code.toUpperCase()
}
