import assert from 'node:assert/strict'
import test from 'node:test'

import {
  normalizePriceRule,
  priceRuleSignature,
  priceRuleToWire,
} from '../src/utils/priceRules.ts'

const legacyAnyRule = {
  draftId: 'legacy',
  tier_id: 1,
  duration_id: 2,
  limit_scope_id: 3,
  min_limit_pct: null,
  max_5h_pct: 25,
  max_weekly_pct: 80,
  price: 599,
}

test('legacy short-window condition is hidden and cleared from the wire payload', () => {
  const normalized = normalizePriceRule(legacyAnyRule, 'any')
  const wire = priceRuleToWire(legacyAnyRule, 'any')

  assert.equal(normalized.max_5h_pct, undefined)
  assert.equal(wire.max_5h_pct, undefined)
  assert.equal(wire.max_weekly_pct, 80)
  assert.equal('draftId' in wire, false)
})

test('legacy short-window value cannot create a distinct price rule', () => {
  const changedShortWindow = { ...legacyAnyRule, max_5h_pct: 5 }

  assert.equal(
    priceRuleSignature(legacyAnyRule, 'any'),
    priceRuleSignature(changedShortWindow, 'any'),
  )
})

test('Codex rule keeps only its long-window minimum', () => {
  const wire = priceRuleToWire({
    ...legacyAnyRule,
    min_limit_pct: 70,
  }, 'codex')

  assert.equal(wire.min_limit_pct, 70)
  assert.equal(wire.max_5h_pct, undefined)
  assert.equal(wire.max_weekly_pct, undefined)
})
