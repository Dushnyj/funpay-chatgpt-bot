import assert from 'node:assert/strict'
import test from 'node:test'

import {
  compactCodexUsage,
  formatUsageWindow,
  rentalCapacityLabel,
  validationState,
} from '../src/utils/accountValidation.ts'

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

test('compact usage keeps the exact 30-day Free window instead of relabelling it weekly', () => {
  const usage = compactCodexUsage({
    account_id: 1,
    plan_window_status: 'ok',
    expected_long_window_seconds: 30 * 86_400,
    codex_5h_remaining_pct: null,
    codex_weekly_remaining_pct: null,
    codex_primary_remaining_pct: 95,
    codex_primary_window_seconds: 30 * 86_400,
    codex_primary_resets_at: '2026-08-12T10:30:00Z',
    codex_secondary_remaining_pct: null,
    codex_secondary_window_seconds: null,
    codex_secondary_resets_at: null,
    refresh_status: 'ok',
    measured_at: '2026-07-13T10:30:00Z',
  })

  assert.deepEqual(usage, [{
    key: 'codex-long',
    windowSeconds: 30 * 86_400,
    remainingPct: 95,
    resetsAt: '2026-08-12T10:30:00Z',
  }])
  assert.equal(formatUsageWindow(usage[0].windowSeconds), '30 дней')
})

test('compact usage selects only the paid seven-day window by duration', () => {
  const usage = compactCodexUsage({
    account_id: 2,
    plan_window_status: 'ok',
    expected_long_window_seconds: 7 * 86_400,
    codex_5h_remaining_pct: 79,
    codex_weekly_remaining_pct: 66,
    codex_primary_remaining_pct: 79,
    codex_primary_window_seconds: 5 * 3_600,
    codex_primary_resets_at: '2026-07-13T15:00:00Z',
    codex_secondary_remaining_pct: 66,
    codex_secondary_window_seconds: 7 * 86_400,
    codex_secondary_resets_at: '2026-07-20T10:30:00Z',
    refresh_status: 'ok',
    measured_at: '2026-07-13T10:30:00Z',
  })

  assert.deepEqual(usage.map((item) => [formatUsageWindow(item.windowSeconds), item.remainingPct]), [
    ['7 дней', 66],
  ])
})

test('compact usage never falls back to legacy Codex aliases', () => {
  const usage = compactCodexUsage({
    account_id: 3,
    plan_window_status: 'ok',
    expected_long_window_seconds: 7 * 86_400,
    codex_5h_remaining_pct: 88,
    codex_weekly_remaining_pct: 71,
    codex_primary_remaining_pct: null,
    codex_primary_window_seconds: null,
    codex_primary_resets_at: null,
    codex_secondary_remaining_pct: null,
    codex_secondary_window_seconds: null,
    codex_secondary_resets_at: null,
    refresh_status: 'ok',
    measured_at: '2026-07-13T10:30:00Z',
  })

  assert.deepEqual(usage, [])
})

test('compact usage fails closed on a mismatched plan contract', () => {
  const usage = compactCodexUsage({
    account_id: 4,
    plan_window_status: 'mismatch',
    expected_long_window_seconds: 7 * 86_400,
    codex_5h_remaining_pct: null,
    codex_weekly_remaining_pct: 71,
    codex_primary_remaining_pct: 71,
    codex_primary_window_seconds: 7 * 86_400,
    codex_primary_resets_at: '2026-07-20T10:30:00Z',
    codex_secondary_remaining_pct: null,
    codex_secondary_window_seconds: null,
    codex_secondary_resets_at: null,
    refresh_status: 'ok',
    measured_at: '2026-07-13T10:30:00Z',
  })

  assert.deepEqual(usage, [])
})

test('compact usage fails closed when both provider positions claim the long window', () => {
  const usage = compactCodexUsage({
    account_id: 5,
    plan_window_status: 'ok',
    expected_long_window_seconds: 7 * 86_400,
    codex_5h_remaining_pct: null,
    codex_weekly_remaining_pct: 71,
    codex_primary_remaining_pct: 71,
    codex_primary_window_seconds: 7 * 86_400,
    codex_primary_resets_at: '2026-07-20T10:30:00Z',
    codex_secondary_remaining_pct: 70,
    codex_secondary_window_seconds: 7 * 86_400,
    codex_secondary_resets_at: '2026-07-20T10:30:00Z',
    refresh_status: 'ok',
    measured_at: '2026-07-13T10:30:00Z',
  })

  assert.deepEqual(usage, [])
})

test('rental capacity always uses the safe single-renter maximum', () => {
  assert.equal(rentalCapacityLabel(2), '2 / 1')
  assert.equal(rentalCapacityLabel(0), '0 / 1')
  assert.equal(rentalCapacityLabel(undefined), '— / 1')
})

test('day windows use correct Russian plural forms', () => {
  assert.equal(formatUsageWindow(1 * 86_400), '1 день')
  assert.equal(formatUsageWindow(2 * 86_400), '2 дня')
  assert.equal(formatUsageWindow(11 * 86_400), '11 дней')
  assert.equal(formatUsageWindow(14 * 86_400), '14 дней')
  assert.equal(formatUsageWindow(21 * 86_400), '21 день')
  assert.equal(formatUsageWindow(22 * 86_400), '22 дня')
  assert.equal(formatUsageWindow(25 * 86_400), '25 дней')
})
