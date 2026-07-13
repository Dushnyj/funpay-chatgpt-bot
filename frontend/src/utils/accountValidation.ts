import type { Account } from '../types/api'

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
