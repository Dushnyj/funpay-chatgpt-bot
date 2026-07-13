import type { LimitScope } from '../types/api'

type OfferScope = Pick<LimitScope, 'code' | 'is_enabled'>

export function isSupportedOfferScopeCode(code: string) {
  const normalized = code.toLowerCase()
  return normalized === 'any' || normalized === 'codex'
}

export function offerScopeUnavailableReason(scope: OfferScope | undefined | null): string | null {
  if (!scope) return 'тип лимита отсутствует в справочнике'
  const code = scope.code.toLowerCase()
  if (code === 'chat') return 'лимит Chat недоступен'
  if (!isSupportedOfferScopeCode(code)) return `тип лимита «${scope.code}» не поддерживается`
  if (!scope.is_enabled) return 'тип лимита выключен'
  return null
}

export function isAvailableOfferScope(scope: OfferScope | undefined | null) {
  return offerScopeUnavailableReason(scope) === null
}
