import assert from 'node:assert/strict'
import test from 'node:test'

import { durationUnit, parseCatalogSortOrder } from '../src/utils/catalogEditor.ts'

test('catalog sort order accepts only non-negative safe integers', () => {
  assert.equal(parseCatalogSortOrder('0'), 0)
  assert.equal(parseCatalogSortOrder(' 25 '), 25)
  assert.equal(parseCatalogSortOrder('10000'), 10_000)
  assert.equal(parseCatalogSortOrder('10001'), null)
  assert.equal(parseCatalogSortOrder('-1'), null)
  assert.equal(parseCatalogSortOrder('1.5'), null)
  assert.equal(parseCatalogSortOrder(''), null)
  assert.equal(parseCatalogSortOrder('9007199254740992'), null)
})

test('duration labels use the correct Russian plural form', () => {
  assert.equal(durationUnit(1), 'день')
  assert.equal(durationUnit(2), 'дня')
  assert.equal(durationUnit(5), 'дней')
  assert.equal(durationUnit(21), 'день')
  assert.equal(durationUnit(23), 'дня')
  assert.equal(durationUnit(30), 'дней')
})
