import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { getDeviceAuthStatus, startMicrosoftEmailOAuth, useAccounts, useCreateAccount, useDeleteAccount, useRecheckAccount, useStartDeviceAuth } from '../api/accounts'
import { useTiers } from '../api/catalog'
import { api, ApiError } from '../api/client'
import { Icon } from '../components/Icon'
import { EmptyState, ErrorState, LoadingState, PageHeader, StatusBadge, TableShell } from '../components/ui'
import type { Account, DeviceAuthSession, DeviceAuthStatus, TotpExport } from '../types/api'
import { formatDate, formatDateTime } from '../utils/format'

export default function Accounts() {
  const accountsQuery = useAccounts()
  const tiersQuery = useTiers()
  const deleteAccount = useDeleteAccount()
  const recheckAccount = useRecheckAccount()
  const startDeviceAuthMutation = useStartDeviceAuth()
  const refetchAccounts = accountsQuery.refetch
  const [showForm, setShowForm] = useState(false)
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState('all')
  const [totpModal, setTotpModal] = useState<{ account: Account; data: TotpExport } | null>(null)
  const [totpLoading, setTotpLoading] = useState<number | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<Account | null>(null)
  const [actionError, setActionError] = useState('')
  const [actionSuccess, setActionSuccess] = useState('')
  const [recheckTarget, setRecheckTarget] = useState<number | null>(null)
  const [deviceAuthTarget, setDeviceAuthTarget] = useState<number | null>(null)
  const [deviceAuthModal, setDeviceAuthModal] = useState<{ account: Account; session: DeviceAuthSession } | null>(null)
  const [emailOAuthTarget, setEmailOAuthTarget] = useState<number | null>(null)

  const accounts = useMemo(() => accountsQuery.data ?? [], [accountsQuery.data])
  const tiers = tiersQuery.data ?? []
  const filteredAccounts = useMemo(() => {
    const query = search.trim().toLowerCase()
    return accounts.filter((account) => {
      const matchesSearch = !query || account.login.toLowerCase().includes(query) || account.email?.toLowerCase().includes(query)
      const matchesStatus = status === 'all' || validationState(account) === status
      return matchesSearch && matchesStatus
    })
  }, [accounts, search, status])

  const completeDeviceAuth = useCallback(async () => {
    setDeviceAuthModal(null)
    setActionError('')
    setActionSuccess('Вход подтверждён. Аккаунт и его тариф успешно обновлены.')
    await refetchAccounts()
  }, [refetchAccounts])

  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const outcome = params.get('email_oauth')
    if (!outcome) return
    const reason = params.get('reason')
    if (outcome === 'connected') {
      setActionError('')
      setActionSuccess('Outlook подключён через Microsoft OAuth. Проверка аккаунта запущена повторно.')
      void refetchAccounts()
    } else {
      setActionSuccess('')
      setActionError(humanizeEmailOAuthError(reason))
    }
    params.delete('email_oauth')
    params.delete('reason')
    const query = params.toString()
    window.history.replaceState({}, '', `${window.location.pathname}${query ? `?${query}` : ''}${window.location.hash}`)
  }, [refetchAccounts])

  if (accountsQuery.isLoading) return <LoadingState label="Загружаем пул аккаунтов" />
  if (accountsQuery.isError) return <ErrorState onRetry={() => accountsQuery.refetch()} />

  const tierName = (id: number | null) => id === null
    ? 'Определяется'
    : tiers.find((tier) => tier.id === id)?.name ?? `Тариф #${id}`
  const activeCount = accounts.filter((account) => validationState(account) === 'active').length
  const attentionCount = accounts.filter((account) => validationState(account) === 'validation_failed').length
  const hasDeviceAuthCandidate = accounts.some(isDeviceAuthEligible)

  const exportTotp = async (account: Account) => {
    setActionError('')
    setTotpLoading(account.id)
    try {
      const data = await api.get<TotpExport>(`/accounts/${account.id}/totp-export`)
      setTotpModal({ account, data })
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : 'Не удалось получить TOTP-секрет')
    } finally {
      setTotpLoading(null)
    }
  }

  const confirmDelete = async () => {
    if (!deleteTarget) return
    setActionError('')
    try {
      await deleteAccount.mutateAsync(deleteTarget.id)
      setDeleteTarget(null)
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : 'Не удалось удалить аккаунт')
      setDeleteTarget(null)
    }
  }

  const recheck = async (account: Account) => {
    setActionError('')
    setActionSuccess('')
    setRecheckTarget(account.id)
    try {
      await recheckAccount.mutateAsync(account.id)
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : 'Не удалось повторно запустить проверку')
    } finally {
      setRecheckTarget(null)
    }
  }

  const startDeviceAuth = async (account: Account) => {
    setActionError('')
    setActionSuccess('')
    setDeviceAuthTarget(account.id)

    // Открываем вкладку прямо из пользовательского клика, иначе браузер может
    // заблокировать её как popup после завершения сетевого запроса.
    const authTab = window.open('about:blank', '_blank')
    if (authTab) authTab.opener = null

    try {
      const session = await startDeviceAuthMutation.mutateAsync(account.id)
      const verificationUrl = normalizeVerificationUrl(session.verification_url)
      if (authTab) {
        authTab.location.replace(verificationUrl)
      } else {
        window.open(verificationUrl, '_blank', 'noopener,noreferrer')
      }
      setDeviceAuthModal({ account, session: { ...session, verification_url: verificationUrl } })
    } catch (cause) {
      authTab?.close()
      setActionError(cause instanceof ApiError ? cause.message : 'Не удалось начать проверку через браузер')
    } finally {
      setDeviceAuthTarget(null)
    }
  }

  const connectOutlook = async (account: Account) => {
    setActionError('')
    setActionSuccess('')
    setEmailOAuthTarget(account.id)
    try {
      const oauth = await startMicrosoftEmailOAuth(account.id)
      window.location.assign(oauth.authorization_url)
    } catch (cause) {
      const message = cause instanceof ApiError && cause.status === 503
        ? 'Microsoft OAuth пока не настроен на сервере. Укажите Client ID, Client Secret и callback URL в окружении приложения.'
        : cause instanceof ApiError ? cause.message : 'Не удалось начать подключение Outlook'
      setActionError(message)
      setEmailOAuthTarget(null)
    }
  }

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Пул ресурсов"
        title="Аккаунты"
        description="ChatGPT-аккаунты, их готовность к выдаче и состояние проверки."
        actions={
          <button className="button button--primary" onClick={() => setShowForm(true)}>
            <Icon name="plus" />Добавить аккаунт
          </button>
        }
      />

      <section className="summary-strip" aria-label="Сводка по аккаунтам">
        <div><span>Всего в пуле</span><strong>{accounts.length}</strong></div>
        <div><span className="summary-dot summary-dot--success" /> <span>Активны</span><strong>{activeCount}</strong></div>
        <div><span className="summary-dot summary-dot--warning" /> <span>Определяются</span><strong>{accounts.filter((account) => validationState(account) === 'detecting').length}</strong></div>
        <div><span className="summary-dot summary-dot--danger" /> <span>Требуют внимания</span><strong>{attentionCount}</strong></div>
      </section>

      {actionError && <div className="form-alert form-alert--error" role="alert"><Icon name="warning" /><span>{actionError}</span></div>}
      {actionSuccess && <div className="form-alert form-alert--success" role="status"><Icon name="check" /><span>{actionSuccess}</span></div>}
      {hasDeviceAuthCandidate && !actionSuccess && (
        <div className="form-alert form-alert--info"><Icon name="activity" /><span>Проверка через браузер — основной способ. Перед первым входом включите в ChatGPT: <strong>Настройки → Безопасность и вход → Авторизация кода устройства для Codex</strong>. «Повторить автоматически» запускает headless-вход, который защита OpenAI может заблокировать.</span></div>
      )}

      <section className="panel panel--flush">
        <div className="toolbar">
          <label className="search-field">
            <Icon name="search" />
            <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Поиск по логину или email" aria-label="Поиск аккаунтов" />
          </label>
          <label className="select-field">
            <span>Статус</span>
            <select value={status} onChange={(event) => setStatus(event.target.value)}>
              <option value="all">Все статусы</option>
              <option value="active">Активные</option>
              <option value="detecting">Определяются</option>
              <option value="validation_failed">Ошибка проверки</option>
            </select>
            <Icon name="chevron-down" size={15} />
          </label>
          <span className="toolbar__count">Показано: {filteredAccounts.length}</span>
        </div>

        {accounts.length === 0 ? (
          <EmptyState
            icon="accounts"
            title="Пул аккаунтов пуст"
            description="Добавьте первый ChatGPT-аккаунт. После сохранения система должна поставить его в очередь безопасной проверки."
            action={<button className="button button--primary" onClick={() => setShowForm(true)}><Icon name="plus" />Добавить аккаунт</button>}
          />
        ) : filteredAccounts.length === 0 ? (
          <EmptyState icon="search" title="Ничего не найдено" description="Измените строку поиска или фильтр статуса." />
        ) : (
          <TableShell>
            <table className="data-table accounts-table">
              <thead><tr><th>Аккаунт</th><th>Определённый план</th><th>Подписка</th><th>Лимит аренд</th><th>Проверка</th><th><span className="sr-only">Действия</span></th></tr></thead>
              <tbody>
                {filteredAccounts.map((account) => {
                  const totpUnavailable = account.status !== 'active'
                  const totpHint = totpUnavailable
                    ? 'TOTP станет доступен после успешной проверки и активации аккаунта'
                    : 'Экспортировать TOTP'
                  return (
                    <tr key={account.id}>
                    <td>
                      <div className="identity-cell"><span className="identity-avatar">{account.login.slice(0, 1).toUpperCase()}</span><span><strong>{account.login}</strong><small>{account.email ?? 'Email для восстановления не задан'}</small>{isOutlookAccount(account) && <small className={account.email_oauth_connected ? 'text-success' : 'text-warning'}>{account.email_oauth_connected ? 'Outlook OAuth подключён' : 'Outlook OAuth не подключён'}</small>}</span></div>
                    </td>
                    <td><PlanDetection account={account} tierName={tierName(account.tier_id)} /></td>
                    <td>{account.subscription_expires_at ? formatDate(account.subscription_expires_at) : isFreePlan(account, tiers) ? 'Без срока' : '—'}</td>
                    <td>{account.max_active_rentals ?? 'По умолчанию'}</td>
                    <td><ValidationStatus account={account} /></td>
                    <td>
                      <div className="row-actions">
                        {isDeviceAuthEligible(account) && (
                          <button className="button button--primary button--compact" onClick={() => startDeviceAuth(account)} disabled={deviceAuthTarget === account.id} aria-label={`Проверить ${account.login} через браузер`}>
                            {deviceAuthTarget === account.id ? <span className="spinner spinner--light" /> : <Icon name="external" size={15} />}Через браузер
                          </button>
                        )}
                        {isOutlookAccount(account) && (
                          <button className={`button button--compact ${account.email_oauth_connected ? 'button--secondary' : 'button--primary'}`} onClick={() => connectOutlook(account)} disabled={emailOAuthTarget === account.id} aria-label={`${account.email_oauth_connected ? 'Переподключить' : 'Подключить'} Outlook для ${account.login}`} title="Безопасный доступ к кодам почты через Microsoft OAuth">
                            {emailOAuthTarget === account.id ? <span className="spinner spinner--light" /> : <Icon name={account.email_oauth_connected ? 'refresh' : 'shield'} size={15} />}{account.email_oauth_connected ? 'Outlook' : 'Почта OAuth'}
                          </button>
                        )}
                        {!isValidationInProgress(account) && (
                          <button className="icon-button" onClick={() => recheck(account)} disabled={recheckTarget === account.id} aria-label={`Повторить автоматическую проверку ${account.login}`} title="Повторить автоматически — защита OpenAI может заблокировать headless-вход">
                            {recheckTarget === account.id ? <span className="spinner" /> : <Icon name="refresh" size={15} />}
                          </button>
                        )}
                        <span className="action-help" title={totpHint} tabIndex={totpUnavailable ? 0 : undefined} aria-label={totpUnavailable ? totpHint : undefined}>
                          <button className="icon-button" onClick={() => exportTotp(account)} disabled={totpLoading === account.id || totpUnavailable} aria-label={totpUnavailable ? `Экспорт TOTP для ${account.login} недоступен: аккаунт должен пройти проверку` : `Экспорт TOTP для ${account.login}`}>
                            {totpLoading === account.id ? <span className="spinner" /> : <Icon name="key" />}
                          </button>
                        </span>
                        <button className="icon-button icon-button--danger" onClick={() => setDeleteTarget(account)} aria-label={`Удалить ${account.login}`} title="Удалить">
                          <Icon name="trash" />
                        </button>
                      </div>
                    </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </TableShell>
        )}
      </section>

      {showForm && <AddAccountDialog onClose={() => setShowForm(false)} />}
      {totpModal && <TotpDialog modal={totpModal} onClose={() => setTotpModal(null)} />}
      {deviceAuthModal && (
        <DeviceAuthDialog
          modal={deviceAuthModal}
          onClose={() => setDeviceAuthModal(null)}
          onCompleted={completeDeviceAuth}
        />
      )}
      {deleteTarget && (
        <div className="modal-overlay" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && setDeleteTarget(null)}>
          <div className="modal modal--compact" role="alertdialog" aria-modal="true" aria-labelledby="delete-account-title">
            <div className="modal__danger-icon"><Icon name="trash" size={22} /></div>
            <h2 id="delete-account-title">Удалить аккаунт?</h2>
            <p>Аккаунт <strong>{deleteTarget.login}</strong> будет удалён из пула. Это действие нельзя отменить.</p>
            <div className="modal__actions">
              <button className="button button--secondary" onClick={() => setDeleteTarget(null)}>Отмена</button>
              <button className="button button--danger" onClick={confirmDelete} disabled={deleteAccount.isPending}>{deleteAccount.isPending ? 'Удаляем…' : 'Удалить'}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function isDeviceAuthEligible(account: Account) {
  const state = validationState(account)
  return state === 'validation_failed' || state === 'detecting' || state === 'pending'
}

function normalizeVerificationUrl(value: string) {
  const url = new URL(value)
  if (url.protocol !== 'https:') throw new Error('OpenAI вернул небезопасный адрес страницы входа')
  return url.toString()
}

function isValidationInProgress(account: Account) {
  const jobStatus = account.validation_job?.status
  if (jobStatus) return jobStatus === 'pending'
    || jobStatus === 'running'
    || jobStatus === 'processing'
  return account.status === 'pending_validation'
}

function validationState(account: Account) {
  if (account.validation_job?.status === 'failed' || account.status === 'validation_failed') return 'validation_failed'
  if (isValidationInProgress(account)) return 'detecting'
  return account.status
}

function PlanDetection({ account, tierName }: { account: Account; tierName: string }) {
  const details = [
    account.plan_raw_type ? `raw: ${account.plan_raw_type}` : null,
    account.plan_source ? `источник: ${humanizePlanSource(account.plan_source)}` : null,
    account.plan_confidence != null ? `уверенность: ${formatConfidence(account.plan_confidence)}` : null,
  ].filter(Boolean)

  return (
    <div className="plan-detection">
      <span className={`soft-badge ${account.tier_id === null ? 'soft-badge--muted' : ''}`}>{tierName}</span>
      {details.length > 0 && <small>{details.join(' · ')}</small>}
      {account.plan_detected_at && <small>Определён {formatDate(account.plan_detected_at)}</small>}
      <ObservedLimits account={account} />
    </div>
  )
}

function ObservedLimits({ account }: { account: Account }) {
  const limits = account.limits
  if (!limits?.measured_at) return null

  const measuredAt = new Date(limits.measured_at)
  const stale = limits.refresh_status !== 'ok'
    || Number.isNaN(measuredAt.getTime())
    || Date.now() - measuredAt.getTime() > 15 * 60 * 1_000

  const windows = [
    {
      label: formatObservedWindow(limits.codex_primary_window_seconds),
      remaining: limits.codex_primary_remaining_pct,
      resetsAt: limits.codex_primary_resets_at,
    },
    {
      label: formatObservedWindow(limits.codex_secondary_window_seconds),
      remaining: limits.codex_secondary_remaining_pct,
      resetsAt: limits.codex_secondary_resets_at,
    },
  ].filter((window) => window.remaining != null || window.label !== '—')

  if (windows.length === 0) return <small>Лимиты Codex не опубликованы OpenAI</small>

  return (
    <div className={`observed-limits ${stale ? 'observed-limits--stale' : ''}`}>
      <small className="observed-limits__meta">Codex · замер {formatDateTime(limits.measured_at)}{stale ? ' · данные устарели' : ''}</small>
      {windows.map((window, index) => (
        <span key={`${window.label}-${index}`}>
          <strong>Окно {window.label}:</strong> {window.remaining == null ? 'остаток неизвестен' : `осталось ${window.remaining}%`}
          {window.resetsAt && <small>сброс {formatDateTime(window.resetsAt)}</small>}
        </span>
      ))}
    </div>
  )
}

function formatObservedWindow(seconds: number | null) {
  if (seconds == null) return '—'
  if (seconds % 86_400 === 0) {
    const days = seconds / 86_400
    return `${days} ${days === 1 ? 'день' : days >= 2 && days <= 4 ? 'дня' : 'дней'}`
  }
  if (seconds % 3_600 === 0) return `${seconds / 3_600} ч`
  if (seconds % 60 === 0) return `${seconds / 60} мин`
  return `${seconds} сек`
}

function humanizePlanSource(source: string) {
  const labels: Record<string, string> = {
    accounts_check: 'OpenAI account',
    wham_usage: 'OpenAI usage',
    access_token: 'OpenAI access token',
    id_token: 'OpenAI ID token',
    account_api: 'OpenAI API',
    usage_api: 'Usage API',
    heuristic: 'сопоставление',
  }
  return source.split('+').map((item) => labels[item] ?? item).join(' + ')
}

function formatConfidence(value: number) {
  const percent = value <= 1 ? value * 100 : value
  return `${Math.round(percent)}%`
}

function ValidationStatus({ account }: { account: Account }) {
  const job = account.validation_job
  const state = validationState(account)
  const label = state === 'detecting'
    ? 'Определяется'
    : state === 'validation_failed'
      ? 'Ошибка проверки'
      : undefined

  return (
    <div className="validation-state">
      <StatusBadge value={state} label={label} />
      {job?.stage && <small>Этап: {humanizeValidationStage(job.stage)}</small>}
      {state === 'validation_failed' && (job?.error_detail || job?.error_code) && (
        <small className="validation-state__error" title={job.error_detail ?? job.error_code ?? undefined}>
          {job.error_detail ?? humanizeValidationError(job.error_code ?? '')}
        </small>
      )}
    </div>
  )
}

function humanizeValidationStage(stage: string) {
  const labels: Record<string, string> = {
    queued: 'ожидание в очереди',
    input: 'проверка введённых данных',
    email_preflight: 'проверка доступа к почте',
    login: 'вход в ChatGPT',
    two_factor: 'двухфакторная защита',
    oauth: 'получение сессии',
    setup_2fa: 'настройка двухфакторной защиты',
    token_exchange: 'получение токенов OpenAI',
    limit_measurement: 'получение тарифа и лимитов',
    internal: 'внутренняя проверка',
    plan_detection: 'определение тарифа',
    limits: 'проверка лимитов',
    completed: 'завершено',
  }
  return labels[stage] ?? stage.replaceAll('_', ' ')
}

function humanizeValidationError(code: string) {
  const labels: Record<string, string> = {
    invalid_credentials: 'Неверный логин или пароль',
    invalid_totp: 'Неверный TOTP setup key',
    missing_2fa_data: 'Не указан TOTP setup key или доступ к почте',
    totp_failed: 'Не удалось подтвердить код 2FA',
    setup_2fa_failed: 'Не удалось настроить 2FA',
    setup_2fa_ui_timeout: 'ChatGPT не открыл настройки 2FA вовремя',
    setup_2fa_button_not_found: 'В ChatGPT не найдена кнопка настройки 2FA',
    setup_2fa_qr_not_found: 'ChatGPT не показал QR-код 2FA',
    setup_2fa_qr_invalid: 'Не удалось прочитать QR-код 2FA',
    email_code_failed: 'Не удалось получить код из почты',
    email_auth_failed: 'Почта отклонила вход',
    email_code_not_found: 'Новое письмо с кодом OpenAI не найдено',
    email_provider_unsupported: 'Для этой почты не настроен поддерживаемый способ входа',
    email_connection_failed: 'Не удалось подключиться к почте',
    email_security_challenge: 'Outlook запросил ручную проверку безопасности',
    email_timeout: 'Outlook Web не ответил вовремя',
    login_failed: 'Не удалось войти в ChatGPT',
    login_timeout: 'ChatGPT не завершил вход вовремя',
    oauth_rejected: 'OpenAI отклонил авторизацию',
    oauth_callback_invalid: 'OpenAI вернул некорректный результат авторизации',
    cloudflare_challenge: 'Cloudflare запросил ручную проверку в браузере',
    token_exchange_failed: 'Не удалось получить токены OpenAI',
    plan_detection_failed: 'Не удалось определить тариф',
    measure_failed: 'Вход выполнен, но OpenAI не вернул данные тарифа и лимитов',
    internal_error: 'Внутренняя ошибка проверки',
  }
  return labels[code] ?? code.replaceAll('_', ' ')
}

function humanizeEmailOAuthError(reason: string | null) {
  const labels: Record<string, string> = {
    invalid_state: 'Сессия подключения Outlook истекла или уже была использована. Начните подключение заново.',
    access_denied: 'Доступ к Outlook не был разрешён.',
    provider_error: 'Microsoft не завершил подключение почты.',
    missing_code: 'Microsoft не вернул код авторизации.',
    account_changed: 'Email аккаунта изменился во время подключения.',
    configuration_missing: 'Microsoft OAuth не настроен на сервере.',
    configuration_changed: 'Настройки Microsoft OAuth изменились во время подключения.',
    token_service_unavailable: 'Сервис авторизации Microsoft временно недоступен.',
    token_exchange_failed: 'Microsoft не выдал токены доступа к почте.',
    offline_access_missing: 'Microsoft не разрешил длительный доступ к почте.',
    profile_service_unavailable: 'Профиль Microsoft временно недоступен.',
    profile_lookup_failed: 'Не удалось проверить профиль Microsoft.',
    email_mismatch: 'Подтверждена другая почта Microsoft. Выберите email, указанный у аккаунта.',
    storage_failed: 'Не удалось безопасно сохранить подключение Outlook.',
  }
  return labels[reason ?? ''] ?? 'Не удалось подключить Outlook через Microsoft OAuth.'
}

function AddAccountDialog({ onClose }: { onClose: () => void }) {
  const createAccount = useCreateAccount()
  const [mode, setMode] = useState<'totp' | 'outlook' | 'email'>('totp')
  const [error, setError] = useState('')
  const [createdOutlookAccountId, setCreatedOutlookAccountId] = useState<number | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [form, setForm] = useState({
    login: '', password: '', totp_secret: '', email: '', email_password: '',
  })

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setError('')
    if (mode === 'totp' && !form.totp_secret.trim()) {
      setError('Укажите TOTP setup key или выберите настройку через email.')
      return
    }
    if (mode === 'email' && (!form.email.trim() || !form.email_password)) {
      setError('Для автоматической настройки 2FA нужны email и пароль почты.')
      return
    }
    if (mode === 'outlook' && !isOutlookAddress(form.email)) {
      setError('Для Microsoft OAuth укажите адрес Outlook, Hotmail, Live или MSN.')
      return
    }
    setSubmitting(true)
    try {
      const account = createdOutlookAccountId === null
        ? await createAccount.mutateAsync({
            ...form,
            totp_secret: mode === 'totp' ? form.totp_secret.trim() : '',
            email: form.email.trim() || undefined,
            email_password: mode === 'email' ? form.email_password || undefined : undefined,
          })
        : { id: createdOutlookAccountId }
      if (mode === 'outlook') {
        setCreatedOutlookAccountId(account.id)
        const oauth = await startMicrosoftEmailOAuth(account.id)
        window.location.assign(oauth.authorization_url)
        return
      }
      onClose()
    } catch (cause) {
      setError(
        createdOutlookAccountId !== null || (mode === 'outlook' && cause instanceof ApiError && cause.status === 503)
          ? 'Аккаунт сохранён, но Microsoft OAuth пока не настроен на сервере. Подключите Outlook позже кнопкой «Почта OAuth».'
          : cause instanceof ApiError ? cause.message : 'Не удалось добавить аккаунт',
      )
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="modal-overlay" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div className="modal modal--wide" role="dialog" aria-modal="true" aria-labelledby="add-account-title">
        <div className="modal__header">
          <div><span className="eyebrow">Новый ресурс</span><h2 id="add-account-title">Добавить ChatGPT-аккаунт</h2><p>Секреты будут зашифрованы перед сохранением.</p></div>
          <button className="icon-button" onClick={onClose} aria-label="Закрыть"><Icon name="close" /></button>
        </div>
        <form onSubmit={submit} className="form-stack">
          {error && <div className="form-alert form-alert--error" role="alert"><Icon name="warning" /><span>{error}</span></div>}
          <div className="form-grid">
            <label className="field"><span className="field__label">Логин ChatGPT</span><input value={form.login} onChange={(event) => setForm({ ...form, login: event.target.value })} placeholder="name@example.com" autoComplete="off" required /></label>
            <label className="field"><span className="field__label">Пароль ChatGPT</span><input type="password" value={form.password} onChange={(event) => setForm({ ...form, password: event.target.value })} placeholder="Пароль аккаунта" autoComplete="new-password" required /></label>
          </div>

          <div className="form-alert form-alert--info"><Icon name="activity" /><span>План назначать вручную не нужно: система определит Free, Go, Plus или вариант Pro по данным самого аккаунта во время проверки.</span></div>

          <fieldset className="segmented-fieldset">
            <legend>Как настроить двухфакторную защиту</legend>
            <div className="segmented-control">
              <button type="button" className={mode === 'totp' ? 'active' : ''} onClick={() => setMode('totp')}><Icon name="key" />TOTP уже включён</button>
              <button type="button" className={mode === 'outlook' ? 'active' : ''} onClick={() => setMode('outlook')}><Icon name="shield" />Outlook OAuth</button>
              <button type="button" className={mode === 'email' ? 'active' : ''} onClick={() => setMode('email')}><Icon name="shield" />Настроить через email</button>
            </div>
          </fieldset>

          {mode === 'totp' ? (
            <label className="field"><span className="field__label">TOTP setup key</span><input value={form.totp_secret} onChange={(event) => setForm({ ...form, totp_secret: event.target.value.toUpperCase().replaceAll(' ', '') })} placeholder="JBSWY3DPEHPK3PXP" autoComplete="off" /><span className="field__hint">Base32-ключ из настроек 2FA. Не QR-код и не одноразовый шестизначный код.</span></label>
          ) : mode === 'outlook' ? (
            <label className="field"><span className="field__label">Почта Outlook / Hotmail</span><input type="email" value={form.email} onChange={(event) => setForm({ ...form, email: event.target.value })} placeholder="mail@outlook.com" required /><span className="field__hint">После сохранения Microsoft откроет безопасное окно согласия только на чтение писем. Пароль почты бот не получает.</span></label>
          ) : (
            <div className="form-grid">
              <label className="field"><span className="field__label">Email для подтверждений</span><input type="email" value={form.email} onChange={(event) => setForm({ ...form, email: event.target.value })} placeholder="mail@example.com" required /></label>
              <label className="field"><span className="field__label">Пароль почты</span><input type="password" value={form.email_password} onChange={(event) => setForm({ ...form, email_password: event.target.value })} placeholder="Пароль или App Password" autoComplete="new-password" required /><span className="field__hint">Outlook/Hotmail проверяется через Outlook Web; Gmail, Yahoo и другие IMAP-провайдеры обычно требуют отдельный App Password.</span></label>
            </div>
          )}

          <div className="form-alert form-alert--info"><Icon name="activity" /><span>{mode === 'outlook' ? 'После сохранения подтвердите доступ Microsoft. Затем проверка аккаунта перезапустится автоматически.' : 'После сохранения аккаунт попадёт в очередь первичной проверки. Статус и этапы будут обновляться автоматически.'}</span></div>
          <div className="modal__actions"><button type="button" className="button button--secondary" onClick={onClose}>Отмена</button><button type="submit" className="button button--primary" disabled={submitting}>{submitting ? <><span className="spinner spinner--light" />Сохраняем…</> : <>{createdOutlookAccountId === null ? 'Добавить аккаунт' : 'Повторить OAuth'}<Icon name="arrow-right" /></>}</button></div>
        </form>
      </div>
    </div>
  )
}

function TotpDialog({ modal, onClose }: { modal: { account: Account; data: TotpExport }; onClose: () => void }) {
  const [copied, setCopied] = useState<'secret' | 'uri' | null>(null)

  useEffect(() => {
    const handler = (event: KeyboardEvent) => event.key === 'Escape' && onClose()
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose])

  const copy = async (kind: 'secret' | 'uri', value: string) => {
    await navigator.clipboard.writeText(value)
    setCopied(kind)
    window.setTimeout(() => setCopied(null), 1600)
  }

  return (
    <div className="modal-overlay" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div className="modal totp-dialog" role="dialog" aria-modal="true" aria-labelledby="totp-title">
        <div className="modal__header"><div><span className="eyebrow">Чувствительные данные</span><h2 id="totp-title">TOTP для {modal.account.login}</h2></div><button className="icon-button" onClick={onClose} aria-label="Закрыть"><Icon name="close" /></button></div>
        <div className="form-alert form-alert--warning"><Icon name="warning" /><span>Любой, кто получит этот ключ или QR-код, сможет генерировать коды входа. Не отправляйте его покупателю.</span></div>
        <img src={modal.data.qr_png_base64} alt={`QR-код TOTP для ${modal.account.login}`} className="qr-code" />
        <label className="field"><span className="field__label">Secret (base32)</span><span className="copy-field"><input readOnly value={modal.data.secret} /><button type="button" onClick={() => copy('secret', modal.data.secret)}><Icon name={copied === 'secret' ? 'check' : 'copy'} />{copied === 'secret' ? 'Скопировано' : 'Копировать'}</button></span></label>
        <label className="field"><span className="field__label">otpauth URI</span><span className="copy-field"><input readOnly value={modal.data.otpauth_uri} /><button type="button" onClick={() => copy('uri', modal.data.otpauth_uri)}><Icon name={copied === 'uri' ? 'check' : 'copy'} />{copied === 'uri' ? 'Скопировано' : 'Копировать'}</button></span></label>
        <div className="modal__actions"><button className="button button--primary" onClick={onClose}>Готово</button></div>
      </div>
    </div>
  )
}

function DeviceAuthDialog({
  modal,
  onClose,
  onCompleted,
}: {
  modal: { account: Account; session: DeviceAuthSession }
  onClose: () => void
  onCompleted: () => Promise<void>
}) {
  const [result, setResult] = useState<DeviceAuthStatus>({ status: 'pending' })
  const [pollError, setPollError] = useState('')
  const [copied, setCopied] = useState(false)
  const [copyError, setCopyError] = useState('')
  const dialogRef = useRef<HTMLDivElement>(null)
  const intervalMs = Math.max(1, modal.session.interval_seconds) * 1_000

  useEffect(() => {
    let cancelled = false
    let timer: number | undefined

    const poll = async () => {
      try {
        const next = await getDeviceAuthStatus(modal.account.id, modal.session.session_id)
        if (cancelled) return
        setPollError('')
        setResult(next)
        if (next.status === 'completed') {
          await onCompleted()
          return
        }
        if (next.status === 'pending') timer = window.setTimeout(poll, intervalMs)
      } catch (cause) {
        if (cancelled) return
        if (cause instanceof ApiError && cause.status === 404) {
          setResult({ status: 'failed', error_code: 'device_auth_state_lost', error_detail: cause.message })
          return
        }
        setPollError(cause instanceof ApiError ? cause.message : 'Не удалось получить состояние проверки')
        timer = window.setTimeout(poll, intervalMs)
      }
    }

    timer = window.setTimeout(poll, intervalMs)
    return () => {
      cancelled = true
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [intervalMs, modal.account.id, modal.session.session_id, onCompleted])

  useEffect(() => {
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const dialog = dialogRef.current
    const focusable = () => Array.from(dialog?.querySelectorAll<HTMLElement>('a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])') ?? [])
    focusable()[0]?.focus()

    const handler = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        if (result.status !== 'pending') onClose()
        return
      }
      if (event.key !== 'Tab') return
      const items = focusable()
      if (items.length === 0) return
      const first = items[0]
      const last = items[items.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    window.addEventListener('keydown', handler)
    return () => {
      window.removeEventListener('keydown', handler)
      previousFocus?.focus()
    }
  }, [onClose, result.status])

  const copyCode = async () => {
    try {
      await navigator.clipboard.writeText(modal.session.user_code)
      setCopyError('')
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1600)
    } catch {
      setCopyError('Не удалось скопировать код автоматически. Выделите его вручную.')
    }
  }

  const failed = result.status === 'failed' || result.status === 'expired'
  const errorText = result.status === 'expired'
    ? 'Срок действия кода истёк. Закройте окно и начните проверку заново.'
    : result.error_detail ?? humanizeDeviceAuthError(result.error_code ?? '')

  return (
    <div className="modal-overlay" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && result.status !== 'pending' && onClose()}>
      <div ref={dialogRef} className="modal device-auth-dialog" role="dialog" aria-modal="true" aria-labelledby="device-auth-title" aria-describedby="device-auth-description">
        <div className="modal__header">
          <div><span className="eyebrow">Проверка с вашим участием</span><h2 id="device-auth-title">Подтвердите вход в браузере</h2><p id="device-auth-description">Страница входа открыта в новой вкладке. Аккаунт обновится автоматически после подтверждения.</p></div>
          {result.status !== 'pending' && <button className="icon-button" onClick={onClose} aria-label="Закрыть"><Icon name="close" /></button>}
        </div>

        <div className="form-alert form-alert--info"><Icon name="shield" /><span><strong>Первый вход:</strong> в ChatGPT откройте «Настройки → Безопасность и вход» и включите «Авторизация кода устройства для Codex». Если OpenAI уже показал красное предупреждение, после включения закройте это окно и запустите «Через браузер» заново — старый код использовать нельзя.</span></div>

        <div className="device-auth-code" aria-label={`Одноразовый код ${modal.session.user_code}`}>
          <span>{modal.session.user_code}</span>
          <button type="button" onClick={copyCode}><Icon name={copied ? 'check' : 'copy'} />{copied ? 'Скопировано' : 'Копировать код'}</button>
        </div>

        <div className={`device-auth-status ${failed ? 'device-auth-status--error' : ''}`} role={failed ? 'alert' : 'status'}>
          {result.status === 'pending' && <span className="spinner" />}
          {failed && <Icon name="warning" />}
          <div>
            <strong>{result.status === 'pending' ? 'Ожидаем подтверждение' : result.status === 'expired' ? 'Код просрочен' : 'Проверка не завершена'}</strong>
            <p>{failed ? errorText : `Проверяем автоматически каждые ${Math.max(1, modal.session.interval_seconds)} сек. Код действует до ${formatDeviceAuthExpiry(modal.session.expires_at)}.`}</p>
          </div>
        </div>

        {pollError && <div className="form-alert form-alert--warning" role="status"><Icon name="warning" /><span>{pollError}. Повторим запрос автоматически.</span></div>}
        {copyError && <div className="form-alert form-alert--warning" role="status"><Icon name="warning" /><span>{copyError}</span></div>}

        <div className="modal__actions">
          {result.status === 'pending'
            ? <span className="device-auth-keep-open">Не закрывайте окно до завершения проверки</span>
            : <button className="button button--secondary" onClick={onClose}>Закрыть</button>}
          <a className="button button--primary" href={modal.session.verification_url} target="_blank" rel="noreferrer"><Icon name="external" />Открыть страницу входа</a>
        </div>
      </div>
    </div>
  )
}

function isFreePlan(account: Account, tiers: Array<{ id: number; code?: string }>) {
  if (tiers.some((tier) => tier.id === account.tier_id && tier.code === 'free')) return true
  const candidates = [account.plan_raw_type, account.limits?.plan_type]
  return candidates.some((value) => value != null && ['free', 'chatgpt_free', 'chatgptfreeplan'].includes(value.toLowerCase()))
}

function isOutlookAddress(value: string | null | undefined) {
  if (!value || !value.includes('@')) return false
  return ['outlook.com', 'hotmail.com', 'live.com', 'msn.com'].includes(value.trim().toLowerCase().split('@').pop() ?? '')
}

function isOutlookAccount(account: Account) {
  return isOutlookAddress(account.email)
}

function formatDeviceAuthExpiry(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return 'истечения сессии'
  return new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(date)
}

function humanizeDeviceAuthError(code: string) {
  const labels: Record<string, string> = {
    access_denied: 'Вход был отклонён на странице OpenAI.',
    authorization_declined: 'Подтверждение входа отменено.',
    expired_token: 'Одноразовый код больше не действует.',
    invalid_grant: 'OpenAI отклонил или уже использовал этот код.',
    login_failed: 'OpenAI не подтвердил вход в аккаунт.',
    plan_detection_failed: 'Вход выполнен, но тариф аккаунта определить не удалось.',
    measure_failed: 'Вход выполнен, но OpenAI не вернул данные тарифа и лимитов.',
    device_auth_state_lost: 'Состояние проверки потеряно. Начните вход заново.',
  }
  return labels[code] ?? (code ? code.replaceAll('_', ' ') : 'OpenAI не подтвердил вход. Начните проверку заново.')
}
