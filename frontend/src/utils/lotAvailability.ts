import type { Duration, LimitScope, Lot, Tier } from '../types/api'
import { offerScopeUnavailableReason } from './offerScopes.ts'

type LotCatalogKey = Pick<Lot, 'tier_id' | 'duration_id' | 'limit_scope_id'>
type AvailabilityTier = Pick<Tier, 'id' | 'is_active' | 'is_sellable'>
type AvailabilityDuration = Pick<Duration, 'id' | 'is_enabled'>
type AvailabilityScope = Pick<LimitScope, 'id' | 'code' | 'is_enabled'>

export type LotCatalogAvailability = {
  available: boolean
  reasons: string[]
}

export function getLotCatalogAvailability(
  lot: LotCatalogKey,
  tiers: AvailabilityTier[],
  durations: AvailabilityDuration[],
  scopes: AvailabilityScope[],
): LotCatalogAvailability {
  const reasons: string[] = []
  const tier = tiers.find((item) => item.id === lot.tier_id)
  const duration = durations.find((item) => item.id === lot.duration_id)
  const scope = scopes.find((item) => item.id === lot.limit_scope_id)

  if (!tier) reasons.push('тариф отсутствует в справочнике')
  else {
    if (!tier.is_active) reasons.push('тариф выключен')
    if (tier.is_sellable === false) reasons.push('продажа тарифа запрещена')
  }

  if (!duration) reasons.push('срок отсутствует в справочнике')
  else if (!duration.is_enabled) reasons.push('срок выключен')

  const scopeReason = offerScopeUnavailableReason(scope)
  if (scopeReason) reasons.push(scopeReason)

  return { available: reasons.length === 0, reasons }
}
