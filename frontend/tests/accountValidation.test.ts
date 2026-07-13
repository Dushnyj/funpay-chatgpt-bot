import assert from 'node:assert/strict'
import test from 'node:test'

import { validationState } from '../src/utils/accountValidation.ts'

test('a transient failed background job does not demote a proven active account', () => {
  assert.equal(validationState({
    status: 'active',
    operator_status_override: null,
    validation_job: {
      id: 12,
      status: 'failed',
      job_type: 'full_validation',
      stage: 'authorize',
      error_code: 'cloudflare_challenge',
      error_detail: 'Cloudflare requested manual verification',
      created_at: '2026-07-13T10:21:23Z',
      started_at: '2026-07-13T10:21:23Z',
      finished_at: '2026-07-13T10:21:26Z',
    },
  }), 'active')
})

test('a never-validated account still fails closed', () => {
  assert.equal(validationState({
    status: 'validation_failed',
    operator_status_override: null,
    validation_job: {
      id: 1,
      status: 'failed',
      job_type: 'full_validation',
      error_code: 'invalid_credentials',
      created_at: '2026-07-13T10:00:00Z',
      started_at: '2026-07-13T10:00:00Z',
      finished_at: '2026-07-13T10:00:01Z',
    },
  }), 'validation_failed')
})

test('a failed refresh or limit check still requires attention', () => {
  assert.equal(validationState({
    status: 'active',
    operator_status_override: null,
    validation_job: {
      id: 13,
      status: 'failed',
      job_type: 'limit_check',
      stage: 'limit_measurement',
      error_code: 'refresh_failed',
      created_at: '2026-07-13T10:26:23Z',
      started_at: '2026-07-13T10:26:42Z',
      finished_at: '2026-07-13T10:26:43Z',
    },
  }), 'validation_failed')
})

test('manual revalidation remains pending after the account is removed from the pool', () => {
  assert.equal(validationState({
    status: 'pending_validation',
    operator_status_override: null,
    validation_job: {
      id: 2,
      status: 'running',
      job_type: 'full_validation',
      created_at: '2026-07-13T10:00:00Z',
      started_at: '2026-07-13T10:00:01Z',
      finished_at: null,
    },
  }), 'detecting')
})

test('operator pause always wins over a background job', () => {
  assert.equal(validationState({
    status: 'active',
    operator_status_override: 'maintenance',
    validation_job: {
      id: 3,
      status: 'done',
      job_type: 'limit_check',
      created_at: '2026-07-13T10:00:00Z',
      started_at: '2026-07-13T10:00:01Z',
      finished_at: '2026-07-13T10:00:02Z',
    },
  }), 'maintenance')
})
