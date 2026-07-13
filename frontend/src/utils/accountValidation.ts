import type { Account, AccountLimits } from '../types/api'

type AccountValidationSnapshot = Pick<
  Account,
  'status' | 'operator_status_override' | 'validation_job'
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
  if (isValidationInProgress(account)) return 'detecting'
  return account.status
}

export interface CompactUsageWindow {
  key: string
  windowSeconds: number
  remainingPct: number
  resetsAt: string | null
}

/**
 * Возвращает единый измеримый лимит Codex. Точные provider-окна
 * primary/secondary имеют приоритет над legacy-полями 5h/weekly, чтобы UI не
 * подписывал 30-дневное окно Free как недельное и не смешивал Codex с Chat.
 */
export function compactCodexUsage(limits: AccountLimits | null | undefined): CompactUsageWindow[] {
  if (!limits) return []
  const exactCodex = [
    usageWindow(
      'codex-primary',
      limits.codex_primary_window_seconds,
      limits.codex_primary_remaining_pct,
      limits.codex_primary_resets_at,
    ),
    usageWindow(
      'codex-secondary',
      limits.codex_secondary_window_seconds,
      limits.codex_secondary_remaining_pct,
      limits.codex_secondary_resets_at,
    ),
  ].filter(isUsageWindow)

  const codex = exactCodex.length > 0
    ? exactCodex
    : [
        usageWindow('codex-5h', 5 * 3_600, limits.codex_5h_remaining_pct, null),
        usageWindow('codex-7d', 7 * 86_400, limits.codex_weekly_remaining_pct, null),
      ].filter(isUsageWindow)

  return codex.sort((left, right) => left.windowSeconds - right.windowSeconds || left.key.localeCompare(right.key))
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
  if (windowSeconds == null || remainingPct == null) return null
  return { key, windowSeconds, remainingPct, resetsAt }
}

function isUsageWindow(value: CompactUsageWindow | null): value is CompactUsageWindow {
  return value !== null
}
