import assert from 'node:assert/strict'
import test from 'node:test'

import { compareDurationsByDays, durationUnit, validateDurationDays } from '../src/utils/catalogEditor.ts'

test('durations are ordered by days with id as a stable tie breaker', () => {
  const durations = [
    { id: 9, days: 30 },
    { id: 4, days: 7 },
    { id: 2, days: 1 },
    { id: 3, days: 7 },
  ]

  assert.deepEqual(durations.sort(compareDurationsByDays).map((duration) => duration.id), [2, 3, 4, 9])
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
