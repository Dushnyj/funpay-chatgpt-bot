import assert from 'node:assert/strict'
import test from 'node:test'

import { statusPresentation } from '../src/utils/statusPresentation.ts'

test('pending refund and expiry statuses are localized warnings', () => {
  assert.deepEqual(statusPresentation('refund_pending'), {
    normalized: 'refund_pending',
    tone: 'warning',
    label: 'Возврат обрабатывается',
  })
  assert.deepEqual(statusPresentation('expiry_pending'), {
    normalized: 'expiry_pending',
    tone: 'warning',
    label: 'Доступ завершается',
  })
})
