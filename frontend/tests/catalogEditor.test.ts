import assert from 'node:assert/strict'
import test from 'node:test'

import { durationUnit, parseCatalogSortOrder, validateDurationDays } from '../src/utils/catalogEditor.ts'

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

test('custom duration days must be a unique integer from 1 to 30', () => {
  assert.deepEqual(validateDurationDays('8', [1, 3, 7]), { days: 8, error: '' })
  assert.equal(validateDurationDays('', []).days, null)
  assert.equal(validateDurationDays('2.5', []).days, null)
  assert.equal(validateDurationDays('0', []).days, null)
  assert.equal(validateDurationDays('31', []).days, null)
  assert.equal(validateDurationDays('7', [1, 7, 30]).error, 'Срок 7 дней уже существует.')
})
