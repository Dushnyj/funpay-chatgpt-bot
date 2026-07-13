import assert from 'node:assert/strict'
import test from 'node:test'

import { isInitialRentalDeliveryPending, isOccupyingRentalStatus } from '../src/utils/rentalDisplay.ts'

test('unsent initial welcome with no delivery attempt keeps the paid term hidden', () => {
  for (const status of ['sending', 'failed', 'manual'] as const) {
    assert.equal(isInitialRentalDeliveryPending({
      credentials_delivery_attempts: 0,
      credentials_delivery_template: 'welcome',
      credentials_delivery_status: status,
    }), true)
  }
})

test('the first potential welcome send makes the conservative term visible', () => {
  for (const status of ['sending', 'failed', 'manual'] as const) {
    assert.equal(isInitialRentalDeliveryPending({
      credentials_delivery_attempts: 1,
      credentials_delivery_template: 'welcome',
      credentials_delivery_status: status,
    }), false)
  }
})

test('sent welcome and pending replacement keep the real rental term visible', () => {
  assert.equal(isInitialRentalDeliveryPending({
    credentials_delivery_attempts: 0,
    credentials_delivery_template: 'welcome',
    credentials_delivery_status: 'sent',
  }), false)
  assert.equal(isInitialRentalDeliveryPending({
    credentials_delivery_attempts: 0,
    credentials_delivery_template: 'replace_success',
    credentials_delivery_status: 'sending',
  }), false)
})

test('active and expiry-pending rentals both occupy an account', () => {
  assert.equal(isOccupyingRentalStatus('active'), true)
  assert.equal(isOccupyingRentalStatus('expiry_pending'), true)
  assert.equal(isOccupyingRentalStatus('expired'), false)
  assert.equal(isOccupyingRentalStatus('refunded'), false)
})
