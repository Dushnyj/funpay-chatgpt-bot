import type { Account, AccountLimits } from '../types/api'

type AccountValidationSnapshot = Pick<
  Account,
  'status' | 'operator_status_override' | 'validation_job'
>

type ManualBrowserConfirmationSnapshot = AccountValidationSnapshot & Pick<
  Account,
  'manual_browser_confirmation_available' | 'manual_browser_confirmation_expires_at'
>

export function isValidationInProgress(account: AccountValidationSnapshot) {
  const jobStatus = account.validation_job?.status
  if (jobStatus) return jobStatus === 'pending'
    || jobStatus === 'running'
    || jobStatus === 'processing'
  return account.status === 'pending_validation'
}

export function validationState(account: AccountValidationSnapshot) {
  if (account.operator_status_override) return account.operator_status_override

  // Account.status is the worker's durable, fail-closed decision for a full
  // validation. A scheduled browser check may fail transiently (for example
  // on a Cloudflare challenge) while fresh tokens and limits still prove that
  // the account is usable. Keep that failed job as diagnostics without
  // overriding the worker's preserved "active" state. Other failed job types
  // (notably a refresh/limit check) continue to require operator attention.
  if (
    account.status === 'active'
    && account.validation_job?.status === 'failed'
    && account.validation_job.job_type === 'full_validation'
  ) return 'active'
  if (account.status === 'validation_failed') return 'validation_failed'

  if (account.validation_job?.status === 'failed') return 'validation_failed'
  // A periodic limit check refreshes usage data in the background. It may keep
  // row actions locked while it owns the account job slot, but it must not
  // visually demote an already classified account to "detecting".
  if (
    isValidationInProgress(account)
    && account.validation_job?.job_type !== 'limit_check'
  ) return 'detecting'
  return account.status
}

export function isCloudflareBrowserChallenge(account: AccountValidationSnapshot) {
  if (account.operator_status_override) return false
  const job = account.validation_job
  return job?.status === 'failed'
    && job.job_type === 'full_validation'
    && job.error_code === 'cloudflare_challenge'
}

export function isManualBrowserConfirmationAvailable(
  account: ManualBrowserConfirmationSnapshot,
) {
  return isCloudflareBrowserChallenge(account)
    && account.manual_browser_confirmation_available === true
}

export interface CompactUsageWindow {
  key: string
  windowSeconds: number
  remainingPct: number
  resetsAt: string | null
}

export interface ExpectedCodexUsageSnapshot {
  planWindowStatus: string | null
  expectedWindowSeconds: number | null
  primaryRemainingPct: number | null
  primaryWindowSeconds: number | null
  primaryResetsAt: string | null
  secondaryRemainingPct: number | null
  secondaryWindowSeconds: number | null
  secondaryResetsAt: string | null
}

const SUPPORTED_LONG_WINDOWS = new Set([7 * 86_400, 30 * 86_400])

/**
 * Возвращает единственный проверенный длинный лимит Codex: 30 дней для Free
 * или 7 дней для платного тарифа. Позиции provider primary/secondary и legacy
 * поля 5h/weekly не имеют самостоятельного продуктового смысла.
 */
export function compactCodexUsage(limits: AccountLimits | null | undefined): CompactUsageWindow[] {
  if (!limits) return []
  const selected = selectExpectedCodexUsage({
    planWindowStatus: limits.plan_window_status,
    expectedWindowSeconds: limits.expected_long_window_seconds,
    primaryRemainingPct: limits.codex_primary_remaining_pct,
    primaryWindowSeconds: limits.codex_primary_window_seconds,
    primaryResetsAt: limits.codex_primary_resets_at,
    secondaryRemainingPct: limits.codex_secondary_remaining_pct,
    secondaryWindowSeconds: limits.codex_secondary_window_seconds,
    secondaryResetsAt: limits.codex_secondary_resets_at,
  })
  return selected ? [selected] : []
}

export function selectExpectedCodexUsage(
  snapshot: ExpectedCodexUsageSnapshot,
): CompactUsageWindow | null {
  const expected = snapshot.expectedWindowSeconds
  if (snapshot.planWindowStatus !== 'ok' || expected === null || !SUPPORTED_LONG_WINDOWS.has(expected)) {
    return null
  }
  const matching = [
    {
      remainingPct: snapshot.primaryRemainingPct,
      windowSeconds: snapshot.primaryWindowSeconds,
      resetsAt: snapshot.primaryResetsAt,
    },
    {
      remainingPct: snapshot.secondaryRemainingPct,
      windowSeconds: snapshot.secondaryWindowSeconds,
      resetsAt: snapshot.secondaryResetsAt,
    },
  ].filter((window) => window.windowSeconds === expected)
  if (matching.length !== 1) return null
  const match = matching[0]
  return usageWindow('codex-long', match.windowSeconds, match.remainingPct, match.resetsAt)
}

export function formatUsageWindow(seconds: number) {
  if (seconds % 86_400 === 0) {
    const days = seconds / 86_400
    const lastTwo = Math.abs(days) % 100
    const last = lastTwo % 10
    const unit = lastTwo >= 11 && lastTwo <= 14
      ? 'дней'
      : last === 1
        ? 'день'
        : last >= 2 && last <= 4
          ? 'дня'
          : 'дней'
    return `${days} ${unit}`
  }
  if (seconds % 3_600 === 0) return `${seconds / 3_600} ч`
  if (seconds % 60 === 0) return `${seconds / 60} мин`
  return `${seconds} сек`
}

export function rentalCapacityLabel(
  activeRentals: number | null | undefined,
) {
  const used = Number.isInteger(activeRentals) && (activeRentals ?? -1) >= 0
    ? activeRentals
    : null
  return `${used ?? '—'} / 1`
}

function usageWindow(
  key: string,
  windowSeconds: number | null,
  remainingPct: number | null,
  resetsAt: string | null,
): CompactUsageWindow | null {
  if (
    windowSeconds == null
    || windowSeconds <= 0
    || remainingPct == null
    || !Number.isFinite(remainingPct)
    || remainingPct < 0
    || remainingPct > 100
  ) return null
  return { key, windowSeconds, remainingPct, resetsAt }
}
