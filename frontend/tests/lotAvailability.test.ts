import assert from 'node:assert/strict'
import test from 'node:test'

import { getLotCatalogAvailability } from '../src/utils/lotAvailability.ts'

const lot = { tier_id: 1, duration_id: 2, limit_scope_id: 3 }

test('lot is available only when every catalog component can be sold', () => {
  assert.deepEqual(getLotCatalogAvailability(
    lot,
    [{ id: 1, is_active: true, is_sellable: true }],
    [{ id: 2, is_enabled: true }],
    [{ id: 3, code: 'codex', is_enabled: true }],
  ), { available: true, reasons: [] })
})

test('lot availability reports every disabled catalog component', () => {
  assert.deepEqual(getLotCatalogAvailability(
    lot,
    [{ id: 1, is_active: false, is_sellable: false }],
    [{ id: 2, is_enabled: false }],
    [{ id: 3, code: 'any', is_enabled: false }],
  ), {
    available: false,
    reasons: ['тариф выключен', 'продажа тарифа запрещена', 'срок выключен', 'тип лимита выключен'],
  })
})

test('missing references and Chat scope fail closed', () => {
  assert.deepEqual(getLotCatalogAvailability(lot, [], [], []), {
    available: false,
    reasons: [
      'тариф отсутствует в справочнике',
      'срок отсутствует в справочнике',
      'тип лимита отсутствует в справочнике',
    ],
  })
  assert.deepEqual(getLotCatalogAvailability(
    lot,
    [{ id: 1, is_active: true, is_sellable: true }],
    [{ id: 2, is_enabled: true }],
    [{ id: 3, code: 'chat', is_enabled: true }],
  ).reasons, ['лимит Chat недоступен'])
})

test('unknown enabled legacy scope is not available for sale', () => {
  assert.deepEqual(getLotCatalogAvailability(
    lot,
    [{ id: 1, is_active: true, is_sellable: true }],
    [{ id: 2, is_enabled: true }],
    [{ id: 3, code: 'legacy-premium', is_enabled: true }],
  ), {
    available: false,
    reasons: ['тип лимита «legacy-premium» не поддерживается'],
  })
})
