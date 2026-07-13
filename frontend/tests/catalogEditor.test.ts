import assert from 'node:assert/strict'
import test from 'node:test'

import {
  compareDurationsByMinutes,
  formatDurationMinutes,
  validateDurationInput,
} from '../src/utils/catalogEditor.ts'

test('durations are ordered by minutes with id as a stable tie breaker', () => {
  const durations = [
    { id: 9, minutes: 7 * 24 * 60 },
    { id: 4, minutes: 90 },
    { id: 2, minutes: 30 },
    { id: 3, minutes: 90 },
  ]

  assert.deepEqual(durations.sort(compareDurationsByMinutes).map((duration) => duration.id), [2, 3, 4, 9])
})

test('duration labels use readable Russian units and mixed values', () => {
  assert.equal(formatDurationMinutes(30), '30 минут')
  assert.equal(formatDurationMinutes(60), '1 час')
  assert.equal(formatDurationMinutes(90), '1 час 30 минут')
  assert.equal(formatDurationMinutes(120), '2 часа')
  assert.equal(formatDurationMinutes(300), '5 часов')
  assert.equal(formatDurationMinutes(24 * 60), '1 день')
  assert.equal(formatDurationMinutes(2 * 24 * 60), '2 дня')
  assert.equal(formatDurationMinutes(5 * 24 * 60), '5 дней')
  assert.equal(formatDurationMinutes(24 * 60 + 30), '1 день 30 минут')
})

test('duration input accepts minutes, half-hours and whole days', () => {
  assert.deepEqual(validateDurationInput('minutes', '30', []), { minutes: 30, error: '' })
  assert.deepEqual(validateDurationInput('hours', '1.5', []), { minutes: 90, error: '' })
  assert.deepEqual(validateDurationInput('hours', '2,5', []), { minutes: 150, error: '' })
  assert.deepEqual(validateDurationInput('days', '7', []), { minutes: 10_080, error: '' })
})

test('duration input enforces 30-minute steps, range and uniqueness', () => {
  assert.equal(validateDurationInput('minutes', '45', []).minutes, null)
  assert.equal(validateDurationInput('hours', '1.25', []).minutes, null)
  assert.equal(validateDurationInput('days', '1.5', []).minutes, null)
  assert.equal(validateDurationInput('minutes', '0', []).minutes, null)
  assert.equal(validateDurationInput('days', '31', []).minutes, null)
  assert.deepEqual(validateDurationInput('hours', '1.5', [90]), {
    minutes: 90,
    error: 'Срок «1 час 30 минут» уже существует.',
  })
})
