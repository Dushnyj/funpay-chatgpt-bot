import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { getDeviceAuthStatus, startMicrosoftEmailOAuth, useAccounts, useCreateAccount, useDeleteAccount, useRecheckAccount, useRepairAccountCredentials, useStartDeviceAuth, useUpdateAccount } from '../api/accounts'
import { useTiers } from '../api/catalog'
import { api, ApiError } from '../api/client'
import { useSettings } from '../api/settings'
import { Icon } from '../components/Icon'
import { EmptyState, ErrorState, LoadingState, ModalOverlay, PageHeader, StatusBadge, TableShell } from '../components/ui'
import type { Account, AccountCredentialsUpdate, DeviceAuthSession, DeviceAuthStatus, TotpCode, TotpExport } from '../types/api'
import { compactCodexUsage, formatUsageWindow, isValidationInProgress, rentalCapacityLabel, validationState } from '../utils/accountValidation'
import { formatDateTime } from '../utils/format'

export default function Accounts() {
  const accountsQuery = useAccounts()
  const tiersQuery = useTiers()
  const settingsQuery = useSettings()
  const deleteAccount = useDeleteAccount()
  const recheckAccount = useRecheckAccount()
  const startDeviceAuthMutation = useStartDeviceAuth()
  const refetchAccounts = accountsQuery.refetch
  const [showForm, setShowForm] = useState(false)
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState('all')
  const [totpModal, setTotpModal] = useState<{ account: Account; data: TotpCode } | null>(null)
  const [totpLoading, setTotpLoading] = useState<number | null>(null)
  const [editTarget, setEditTarget] = useState<Account | null>(null)
  const [credentialTarget, setCredentialTarget] = useState<Account | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<Account | null>(null)
  const [actionError, setActionError] = useState('')
  const [actionSuccess, setActionSuccess] = useState('')
  const [recheckTarget, setRecheckTarget] = useState<number | null>(null)
  const [deviceAuthTarget, setDeviceAuthTarget] = useState<number | null>(null)
  const [deviceAuthModal, setDeviceAuthModal] = useState<{ account: Account; session: DeviceAuthSession } | null>(null)
  const [emailOAuthTarget, setEmailOAuthTarget] = useState<number | null>(null)

  const accounts = useMemo(() => accountsQuery.data ?? [], [accountsQuery.data])
  const tiers = tiersQuery.data ?? []
  const graphConfigured = settingsQuery.data?.graph_configured === true
  const filteredAccounts = useMemo(() => {
    const query = search.trim().toLowerCase()
    return accounts.filter((account) => {
      const matchesSearch = !query || account.login.toLowerCase().includes(query)
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
  const attentionCount = accounts.filter(accountNeedsAttention).length
  const hasDeviceAuthCandidate = accounts.some(isDeviceAuthEligible)

  const openTotpCode = async (account: Account) => {
    setActionError('')
    setTotpLoading(account.id)
    try {
      const data = await api.get<TotpCode>(`/accounts/${account.id}/totp-code`)
      setTotpModal({ account, data })
    } catch (cause) {
      setActionError(cause instanceof ApiError && cause.status === 400
        ? 'У аккаунта нет рабочего TOTP setup key. Обновите данные входа.'
        : cause instanceof ApiError ? cause.message : 'Не удалось получить одноразовый код')
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
    if (!graphConfigured) {
      setActionError('Microsoft Graph не настроен на сервере. Добавьте Client ID, Client Secret и callback URL, затем перезапустите приложение.')
      return
    }
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
      {!settingsQuery.isLoading && !graphConfigured && accounts.some(isOutlookAccount) && (
        <div className="form-alert form-alert--warning" role="status">
          <Icon name="warning" />
          <span>Microsoft Graph не настроен — «Почта OAuth» недоступна.</span>
        </div>
      )}
      {hasDeviceAuthCandidate && !actionSuccess && (
        <div className="form-alert form-alert--info"><Icon name="activity" /><span>Для кнопки «Вход» включите в ChatGPT: <strong>Настройки → Безопасность и вход → Авторизация кода устройства для Codex</strong>.</span></div>
      )}

      <section className="panel panel--flush">
        <div className="toolbar">
          <label className="search-field">
            <Icon name="search" />
            <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Поиск по аккаунту" aria-label="Поиск аккаунтов" />
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
            <table className="data-table accounts-table accounts-table--operator">
              <thead><tr><th>Аккаунт</th><th>План</th><th>Лимит Codex</th><th>Подписка</th><th>Аренды</th><th>Проверка</th><th><span className="sr-only">Действия</span></th></tr></thead>
              <tbody>
                {filteredAccounts.map((account) => {
                  const activeRentals = account.active_rentals_count
                  const accountOccupied = (activeRentals ?? 0) > 0 || account.replacement_reserved
                  const totpHint = 'Получить текущий одноразовый код или открыть setup key'
                  return (
                    <tr key={account.id}>
                    <td data-label="Аккаунт">
                      <strong className="account-login" title={account.login}>{account.login}</strong>
                    </td>
                    <td data-label="План"><span className={`soft-badge account-plan ${account.tier_id === null ? 'soft-badge--muted' : ''}`}>{tierName(account.tier_id)}</span></td>
                    <td data-label="Лимит Codex"><CompactLimits account={account} /></td>
                    <td data-label="Подписка"><span className="account-subscription">{subscriptionLabel(account, tiers)}</span></td>
                    <td data-label="Аренды"><span className="rental-capacity" title={activeRentals == null ? 'Фактическое число аренд ещё не загружено; безопасный максимум — 1' : `Фактически продано: ${activeRentals}; безопасный максимум: 1`}>{rentalCapacityLabel(activeRentals)}</span></td>
                    <td data-label="Проверка"><ValidationStatus account={account} /></td>
                    <td data-label="Действия">
                      <div className="row-actions account-actions">
                        {isDeviceAuthEligible(account) && (
                          <button type="button" className="icon-button account-icon-action account-icon-action--primary" onClick={() => startDeviceAuth(account)} disabled={accountOccupied || deviceAuthTarget === account.id} aria-label={`Войти в ${account.login} через браузер`} title={accountOccupied ? 'Нельзя запускать вход, пока аккаунт занят арендой или заменой' : 'Ручная проверка через браузер'}>
                            {deviceAuthTarget === account.id ? <span className="spinner spinner--light" /> : <Icon name="external" size={15} />}
                          </button>
                        )}
                        {isOutlookAccount(account) && (
                          <span className="action-help" title={graphConfigured ? undefined : 'Microsoft Graph не настроен на сервере'} tabIndex={graphConfigured ? undefined : 0} aria-label={graphConfigured ? undefined : 'Почта OAuth недоступна: Microsoft Graph не настроен'}>
                            <button type="button" className={`icon-button account-icon-action ${account.email_oauth_connected ? 'account-icon-action--success' : 'account-icon-action--primary'}`} onClick={() => connectOutlook(account)} disabled={!graphConfigured || accountOccupied || emailOAuthTarget === account.id} aria-label={`${account.email_oauth_connected ? 'Переподключить' : 'Подключить'} почту Outlook для ${account.login} через OAuth`} title={!graphConfigured ? 'Почта OAuth недоступна: Microsoft Graph не настроен' : accountOccupied ? 'Нельзя менять OAuth почты во время активной аренды' : `${account.email_oauth_connected ? 'Переподключить' : 'Подключить'} почту Outlook через OAuth`}>
                              {emailOAuthTarget === account.id ? <span className={`spinner ${account.email_oauth_connected ? '' : 'spinner--light'}`} /> : <Icon name={account.email_oauth_connected ? 'check' : 'shield'} size={15} />}
                            </button>
                          </span>
                        )}
                        {!isValidationInProgress(account) && (
                          <button type="button" className="icon-button account-icon-action" onClick={() => recheck(account)} disabled={accountOccupied || recheckTarget === account.id} aria-label={`Повторить автоматическую проверку ${account.login}`} title={accountOccupied ? 'Нельзя перезапускать проверку во время активной аренды' : 'Повторить автоматическую проверку'}>
                            {recheckTarget === account.id ? <span className="spinner" /> : <Icon name="refresh" size={15} />}
                          </button>
                        )}
                        <span className="action-help" title={totpHint}>
                          <button type="button" className="icon-button account-icon-action" onClick={() => openTotpCode(account)} disabled={totpLoading === account.id} aria-label={`Получить одноразовый ключ для ${account.login}`} title={totpHint}>
                            {totpLoading === account.id ? <span className="spinner" /> : <Icon name="key" size={15} />}
                          </button>
                        </span>
                        <span className="action-help" title={accountOccupied ? 'Нельзя менять данные входа, пока аккаунт занят арендой или заменой' : undefined} tabIndex={accountOccupied ? 0 : undefined}>
                          <button type="button" className="icon-button account-icon-action" onClick={() => setCredentialTarget(account)} disabled={accountOccupied} aria-label={`Изменить данные входа ${account.login}`} title={accountOccupied ? 'Данные входа защищены на время аренды или замены' : 'Изменить логин, пароль, TOTP и почту'}>
                            <Icon name="eye" size={15} />
                          </button>
                        </span>
                        <button type="button" className="icon-button account-icon-action" onClick={() => setEditTarget(account)} aria-label={`Изменить параметры ${account.login}`} title="Изменить ёмкость, статус и заметку">
                          <Icon name="settings" size={15} />
                        </button>
                        <span className="action-help" title={accountOccupied ? 'Сначала завершите аренду или замену' : undefined} tabIndex={accountOccupied ? 0 : undefined} aria-label={accountOccupied ? `Удаление ${account.login} недоступно: аккаунт занят арендой или заменой` : undefined}>
                          <button type="button" className="icon-button account-icon-action account-icon-action--danger" onClick={() => setDeleteTarget(account)} disabled={accountOccupied} aria-label={`Удалить ${account.login}`} title={accountOccupied ? 'Удаление недоступно: аккаунт занят арендой или заменой' : 'Удалить аккаунт'}>
                            <Icon name="trash" size={15} />
                          </button>
                        </span>
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

      {showForm && <AddAccountDialog graphConfigured={graphConfigured} onClose={() => setShowForm(false)} />}
      {totpModal && <TotpDialog modal={totpModal} onClose={() => setTotpModal(null)} />}
      {deviceAuthModal && (
        <DeviceAuthDialog
          modal={deviceAuthModal}
          onClose={() => setDeviceAuthModal(null)}
          onCompleted={completeDeviceAuth}
        />
      )}
      {credentialTarget && <RepairCredentialsDialog account={credentialTarget} onClose={() => setCredentialTarget(null)} onSaved={() => { setCredentialTarget(null); setActionError(''); setActionSuccess('Данные входа обновлены. Аккаунт поставлен на обязательную повторную проверку.') }} />}
      {editTarget && <EditAccountDialog account={editTarget} onClose={() => setEditTarget(null)} onSaved={() => { setEditTarget(null); setActionError(''); setActionSuccess('Операторские параметры аккаунта сохранены.') }} />}
      {deleteTarget && (
        <ModalOverlay onClose={() => setDeleteTarget(null)}>
          <div className="modal modal--compact" role="alertdialog" aria-modal="true" aria-labelledby="delete-account-title">
            <div className="modal__danger-icon"><Icon name="trash" size={22} /></div>
            <h2 id="delete-account-title">Удалить аккаунт?</h2>
            <p>Аккаунт <strong>{deleteTarget.login}</strong> будет удалён из пула. Это действие нельзя отменить.</p>
            <div className="modal__actions">
              <button className="button button--secondary" onClick={() => setDeleteTarget(null)}>Отмена</button>
              <button className="button button--danger" onClick={confirmDelete} disabled={deleteAccount.isPending}>{deleteAccount.isPending ? 'Удаляем…' : 'Удалить'}</button>
            </div>
          </div>
        </ModalOverlay>
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

function CompactLimits({ account }: { account: Account }) {
  const limits = account.limits
  const windows = compactCodexUsage(limits)
  const stale = areLimitsStale(limits)
  const windowStatus = limits?.plan_window_status ?? 'unknown'

  return (
    <div className={`compact-limits ${stale ? 'compact-limits--stale' : ''}`}>
      <CompactLimitGroup windows={windows} />
      {windowStatus === 'mismatch' && <small className="compact-limits__alert compact-limits__alert--danger" title="Аккаунт исключён из автоматической выдачи">Окно тарифа не совпало</small>}
      {windowStatus === 'unknown' && <small className="compact-limits__alert" title="До свежего замера аккаунт не должен участвовать в автоматической выдаче">Окно не проверено</small>}
      {stale && <small className="compact-limits__alert" title={limits?.measured_at ? `Последний замер: ${formatDateTime(limits.measured_at)}` : 'Свежего замера ещё нет'}>Лимиты устарели</small>}
    </div>
  )
}

function CompactLimitGroup({ windows }: { windows: ReturnType<typeof compactCodexUsage> }) {
  return (
    <div className="compact-limit-group">
      <span>
        {windows.length === 0
          ? <small>—</small>
          : windows.map((window) => {
              const label = `${formatUsageWindow(window.windowSeconds)} — ${window.remainingPct}%${window.resetsAt ? ` · сброс ${formatCompactDateTime(window.resetsAt)}` : ''}`
              return <small key={window.key} title={label}>{label}</small>
            })}
      </span>
    </div>
  )
}

function formatCompactDateTime(value: string) {
  return formatDateTime(value).replace(' г.', '')
}

function areLimitsStale(limits: Account['limits']) {
  if (!limits) return false
  if (limits.refresh_status !== 'ok' || !limits.measured_at) return true
  const measuredAt = new Date(limits.measured_at)
  return Number.isNaN(measuredAt.getTime()) || Date.now() - measuredAt.getTime() > 60 * 60 * 1_000
}

function accountNeedsAttention(account: Account) {
  const state = validationState(account)
  if (state === 'validation_failed') return true
  if (state !== 'active') return false
  return !account.limits
    || account.limits.plan_window_status !== 'ok'
    || areLimitsStale(account.limits)
}

function ValidationStatus({ account }: { account: Account }) {
  const job = account.validation_job
  const state = validationState(account)
  const nonBlockingFailure = state === 'active' && job?.status === 'failed'
  const label = state === 'detecting'
    ? 'Определяется'
    : state === 'validation_failed'
      ? 'Ошибка проверки'
      : undefined

  return (
    <div className="validation-state">
      <StatusBadge value={state} label={label} />
      {state === 'detecting' && job?.stage && <small>{humanizeValidationStage(job.stage)}</small>}
      {state === 'validation_failed' && (job?.error_detail || job?.error_code) && (
        <small className="validation-state__error" title={job.error_detail ?? job.error_code ?? undefined}>
          {humanizeValidationError(job.error_code ?? '') || 'Проверка не пройдена'}
        </small>
      )}
      {nonBlockingFailure && (
        <small className="validation-state__warning" title={job.error_detail ?? job.error_code ?? undefined}>
          Фоновая ошибка
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

function accountStatusLabel(status: string) {
  const labels: Record<string, string> = {
    active: 'активен',
    disabled: 'отключён',
    maintenance: 'обслуживание',
    pending_validation: 'проверяется',
    validation_failed: 'ошибка проверки',
  }
  return labels[status] ?? status.replaceAll('_', ' ')
}

function RepairCredentialsDialog({ account, onClose, onSaved }: { account: Account; onClose: () => void; onSaved: () => void }) {
  const repairCredentials = useRepairAccountCredentials()
  const [error, setError] = useState('')
  const [form, setForm] = useState({
    login: '',
    password: '',
    totpSecret: '',
    email: '',
    emailPassword: '',
    clearTotp: false,
    clearEmail: false,
    clearEmailPassword: false,
  })

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setError('')
    const body: AccountCredentialsUpdate = {}
    const login = form.login.trim()
    const email = form.email.trim()
    const totpSecret = form.totpSecret.trim().replaceAll(' ', '')

    if (login) body.login = login
    if (form.password) body.password = form.password
    if (form.clearTotp) body.totp_secret = null
    else if (totpSecret) body.totp_secret = totpSecret
    if (form.clearEmail) {
      body.email = null
      body.email_password = null
    } else {
      if (email) body.email = email
      if (form.clearEmailPassword) body.email_password = null
      else if (form.emailPassword) body.email_password = form.emailPassword
    }

    if (Object.keys(body).length === 0) {
      setError('Заполните хотя бы одно новое значение или явно выберите, что нужно очистить.')
      return
    }

    try {
      await repairCredentials.mutateAsync({ id: account.id, ...body })
      onSaved()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Не удалось обновить данные входа')
    }
  }

  return (
    <ModalOverlay onClose={onClose}>
      <div className="modal modal--wide" role="dialog" aria-modal="true" aria-labelledby="repair-credentials-title">
        <div className="modal__header">
          <div><span className="eyebrow">Безопасное исправление</span><h2 id="repair-credentials-title">Данные входа {account.login}</h2><p>Заполните только то, что нужно заменить. Сохранённые пароли и TOTP никогда не подставляются обратно в форму.</p></div>
          <button className="icon-button" onClick={onClose} aria-label="Закрыть"><Icon name="close" /></button>
        </div>
        <form className="form-stack" onSubmit={submit}>
          {error && <div className="form-alert form-alert--error" role="alert"><Icon name="warning" /><span>{error}</span></div>}
          <div className="form-alert form-alert--info"><Icon name="activity" /><span>Пустое поле означает «не менять». После сохранения аккаунт станет недоступен для выдачи до полной повторной проверки.</span></div>
          {account.email_oauth_connected && <div className="form-alert form-alert--warning"><Icon name="warning" /><span>При смене или удалении email текущее подключение Microsoft OAuth будет сброшено. Новый Outlook потребуется подключить заново.</span></div>}

          <div className="form-grid">
            <label className="field"><span className="field__label">Новый логин ChatGPT</span><input data-autofocus value={form.login} onChange={(event) => setForm((current) => ({ ...current, login: event.target.value }))} placeholder={`Сейчас: ${account.login}`} autoComplete="off" /><span className="field__hint">Оставьте пустым, чтобы не менять.</span></label>
            <label className="field"><span className="field__label">Новый пароль ChatGPT</span><input type="password" value={form.password} onChange={(event) => setForm((current) => ({ ...current, password: event.target.value }))} placeholder="Не изменять" autoComplete="new-password" /><span className="field__hint">Текущее значение скрыто и недоступно через API.</span></label>
          </div>

          <div className="form-grid">
            <div className="credential-repair-field">
              <label className="field"><span className="field__label">Новый TOTP setup key</span><input value={form.totpSecret} onChange={(event) => setForm((current) => ({ ...current, totpSecret: event.target.value.toUpperCase() }))} placeholder="Не изменять" autoComplete="off" disabled={form.clearTotp} /><span className="field__hint">Base32-ключ, не одноразовый шестизначный код.</span></label>
              <label className="credential-clear"><input type="checkbox" checked={form.clearTotp} onChange={(event) => setForm((current) => ({ ...current, clearTotp: event.target.checked }))} /><span>Очистить сохранённый TOTP</span></label>
            </div>
            <div className="credential-repair-field">
              <label className="field"><span className="field__label">Новый email</span><input type="email" value={form.email} onChange={(event) => setForm((current) => ({ ...current, email: event.target.value }))} placeholder={account.email ? `Сейчас: ${account.email}` : 'Не задан'} autoComplete="off" disabled={form.clearEmail} /></label>
              <label className="credential-clear"><input type="checkbox" checked={form.clearEmail} onChange={(event) => setForm((current) => ({ ...current, clearEmail: event.target.checked }))} /><span>Удалить email и доступ к почте</span></label>
            </div>
          </div>

          <div className="credential-repair-field">
            <label className="field"><span className="field__label">Новый пароль почты или пароль приложения</span><input type="password" value={form.emailPassword} onChange={(event) => setForm((current) => ({ ...current, emailPassword: event.target.value }))} placeholder="Не изменять" autoComplete="new-password" disabled={form.clearEmail || form.clearEmailPassword} /><span className="field__hint">Для Outlook предпочтительнее Microsoft OAuth; обычный пароль не подставляется и не отображается.</span></label>
            <label className="credential-clear"><input type="checkbox" checked={form.clearEmail || form.clearEmailPassword} disabled={form.clearEmail} onChange={(event) => setForm((current) => ({ ...current, clearEmailPassword: event.target.checked }))} /><span>Очистить сохранённый пароль почты</span></label>
          </div>

          <div className="modal__actions"><button className="button button--secondary" type="button" onClick={onClose}>Отмена</button><button className="button button--primary" type="submit" disabled={repairCredentials.isPending}>{repairCredentials.isPending ? <><span className="spinner spinner--light" />Сохраняем…</> : <><Icon name="shield" />Сохранить и перепроверить</>}</button></div>
        </form>
      </div>
    </ModalOverlay>
  )
}

function EditAccountDialog({ account, onClose, onSaved }: { account: Account; onClose: () => void; onSaved: () => void }) {
  const updateAccount = useUpdateAccount()
  const [error, setError] = useState('')
  const accountOccupied = (account.active_rentals_count ?? 0) > 0 || account.replacement_reserved
  const [form, setForm] = useState({
    maxRentals: account.max_active_rentals == null ? '' : '1',
    operatorStatus: (account.operator_status_override ?? '') as '' | 'maintenance' | 'disabled',
    notes: account.notes ?? '',
  })

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setError('')
    const maxRentals = form.maxRentals === '' ? null : Number(form.maxRentals)
    if (maxRentals !== null && maxRentals !== 1) {
      setError('Безопасный лимит — только 1 активная аренда на аккаунт или системное значение.')
      return
    }
    try {
      const safeStatus = accountOccupied ? '' : form.operatorStatus
      await updateAccount.mutateAsync({
        id: account.id,
        ...(!accountOccupied ? {
          max_active_rentals: maxRentals,
          ...(safeStatus ? { status: safeStatus } : {}),
        } : {}),
        notes: form.notes.trim() || null,
      })
      onSaved()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Не удалось сохранить параметры аккаунта')
    }
  }

  return (
    <ModalOverlay onClose={onClose}>
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="edit-account-title">
        <div className="modal__header"><div><span className="eyebrow">Операторские параметры</span><h2 id="edit-account-title">Настроить {account.login}</h2><p>Тариф здесь не меняется: Free, Go, Plus и Pro определяются только автоматической проверкой OpenAI.</p></div><button className="icon-button" onClick={onClose} aria-label="Закрыть"><Icon name="close" /></button></div>
        <form className="form-stack" onSubmit={submit}>
          {error && <div className="form-alert form-alert--error" role="alert"><Icon name="warning" /><span>{error}</span></div>}
          <div className="form-alert form-alert--info"><Icon name="activity" /><span>Тариф и срок подписки определяются только автоматической проверкой OpenAI. Ручное изменение отключено, чтобы аккаунт с неподтверждённой подпиской не попал в выдачу.</span></div>
          {accountOccupied && <div className="form-alert form-alert--warning"><Icon name="warning" /><span>Аккаунт занят арендой или зарезервирован для замены. До освобождения можно сохранить только заметку; ёмкость и приостановка защищены.</span></div>}
          <div className="form-grid">
            <label className="field"><span className="field__label">Лимит активных аренд</span><select value={form.maxRentals} disabled={accountOccupied} onChange={(event) => setForm((current) => ({ ...current, maxRentals: event.target.value }))}><option value="">Системный лимит — 1</option><option value="1">Явный лимит — 1</option></select><span className="field__hint">Аккаунт нельзя одновременно выдать нескольким покупателям: завершение аренды закрывает его общую сессию.</span></label>
            <label className="field"><span className="field__label">Ручная приостановка</span><select value={form.operatorStatus} disabled={accountOccupied} onChange={(event) => setForm((current) => ({ ...current, operatorStatus: event.target.value as '' | 'maintenance' | 'disabled' }))}><option value="">Не менять текущий статус</option><option value="maintenance">Перевести на обслуживание</option><option value="disabled">Отключить аккаунт</option></select><span className="field__hint">{accountOccupied ? 'Недоступно, пока аккаунт занят арендой или заменой.' : `Сейчас: ${accountStatusLabel(account.status)}. Возврат в работу выполняется только повторной проверкой аккаунта.`}</span></label>
          </div>
          <label className="field"><span className="field__label">Заметка оператора</span><textarea value={form.notes} onChange={(event) => setForm((current) => ({ ...current, notes: event.target.value }))} maxLength={4000} placeholder="Обслуживание или другой важный контекст" /></label>
          <div className="modal__actions"><button className="button button--secondary" type="button" onClick={onClose}>Отмена</button><button className="button button--primary" type="submit" disabled={updateAccount.isPending}>{updateAccount.isPending ? <><span className="spinner spinner--light" />Сохраняем…</> : <><Icon name="check" />Сохранить</>}</button></div>
        </form>
      </div>
    </ModalOverlay>
  )
}

function AddAccountDialog({ graphConfigured, onClose }: { graphConfigured: boolean; onClose: () => void }) {
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
    if (mode === 'outlook' && !graphConfigured) {
      setError('Microsoft Graph не настроен на сервере. Выберите TOTP или настройку через email.')
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
    <ModalOverlay onClose={onClose}>
      <div className="modal modal--wide" role="dialog" aria-modal="true" aria-labelledby="add-account-title">
        <div className="modal__header">
          <div><span className="eyebrow">Новый ресурс</span><h2 id="add-account-title">Добавить ChatGPT-аккаунт</h2><p>Секреты будут зашифрованы перед сохранением.</p></div>
          <button className="icon-button" onClick={onClose} aria-label="Закрыть"><Icon name="close" /></button>
        </div>
        <form onSubmit={submit} className="form-stack">
          {error && <div className="form-alert form-alert--error" role="alert"><Icon name="warning" /><span>{error}</span></div>}
          <div className="form-grid">
            <label className="field"><span className="field__label">Логин ChatGPT</span><input data-autofocus value={form.login} onChange={(event) => setForm({ ...form, login: event.target.value })} placeholder="name@example.com" autoComplete="off" required /></label>
            <label className="field"><span className="field__label">Пароль ChatGPT</span><input type="password" value={form.password} onChange={(event) => setForm({ ...form, password: event.target.value })} placeholder="Пароль аккаунта" autoComplete="new-password" required /></label>
          </div>

          <div className="form-alert form-alert--info"><Icon name="activity" /><span>План назначать вручную не нужно: система определит Free, Go, Plus или вариант Pro по данным самого аккаунта во время проверки.</span></div>

          <fieldset className="segmented-fieldset">
            <legend>Как настроить двухфакторную защиту</legend>
            <div className="segmented-control">
              <button type="button" className={mode === 'totp' ? 'active' : ''} onClick={() => setMode('totp')}><Icon name="key" />TOTP уже включён</button>
              <button type="button" className={mode === 'outlook' ? 'active' : ''} onClick={() => setMode('outlook')} disabled={!graphConfigured} title={graphConfigured ? 'Подключить Outlook через Microsoft Graph' : 'Microsoft Graph не настроен'}><Icon name="shield" />Outlook OAuth</button>
              <button type="button" className={mode === 'email' ? 'active' : ''} onClick={() => setMode('email')}><Icon name="shield" />Настроить через email</button>
            </div>
          </fieldset>

          {!graphConfigured && <div className="form-alert form-alert--warning"><Icon name="warning" /><span>Outlook OAuth появится после настройки Microsoft Graph на сервере. До этого используйте TOTP setup key или другой поддерживаемый способ доступа к почте.</span></div>}

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
    </ModalOverlay>
  )
}

function TotpDialog({ modal, onClose }: { modal: { account: Account; data: TotpCode }; onClose: () => void }) {
  const [currentCode, setCurrentCode] = useState(modal.data.code)
  const [expiresAt, setExpiresAt] = useState(() => Date.now() + Math.max(1, modal.data.seconds_remaining) * 1_000)
  const [secondsRemaining, setSecondsRemaining] = useState(modal.data.seconds_remaining)
  const [codeValid, setCodeValid] = useState(true)
  const [codeLoading, setCodeLoading] = useState(false)
  const [setupData, setSetupData] = useState<TotpExport | null>(null)
  const [setupLoading, setSetupLoading] = useState(false)
  const [copied, setCopied] = useState<'code' | 'secret' | 'uri' | null>(null)
  const [error, setError] = useState('')
  const refreshingRef = useRef(false)
  const retryAtRef = useRef(0)
  const retryDelayRef = useRef(5_000)

  const refreshCode = useCallback(async () => {
    if (refreshingRef.current) return
    refreshingRef.current = true
    setCodeLoading(true)
    try {
      const data = await api.get<TotpCode>(`/accounts/${modal.account.id}/totp-code`)
      setCurrentCode(data.code)
      setSecondsRemaining(data.seconds_remaining)
      setExpiresAt(Date.now() + Math.max(1, data.seconds_remaining) * 1_000)
      setCodeValid(true)
      retryAtRef.current = 0
      retryDelayRef.current = 5_000
      setError('')
    } catch (cause) {
      retryAtRef.current = Date.now() + retryDelayRef.current
      retryDelayRef.current = Math.min(retryDelayRef.current * 2, 30_000)
      setError(cause instanceof ApiError && cause.status === 400
        ? 'У аккаунта нет рабочего TOTP setup key.'
        : cause instanceof ApiError ? cause.message : 'Не удалось обновить одноразовый код')
    } finally {
      refreshingRef.current = false
      setCodeLoading(false)
    }
  }, [modal.account.id])

  useEffect(() => {
    const timer = window.setInterval(() => {
      const now = Date.now()
      const remaining = Math.max(0, Math.ceil((expiresAt - now) / 1_000))
      setSecondsRemaining(remaining)
      if (remaining === 0) {
        setCodeValid(false)
        if (now >= retryAtRef.current) void refreshCode()
      }
    }, 1_000)
    return () => window.clearInterval(timer)
  }, [expiresAt, refreshCode])

  const copy = async (kind: 'code' | 'secret' | 'uri', value: string) => {
    try {
      await navigator.clipboard.writeText(value)
      setError('')
      setCopied(kind)
      window.setTimeout(() => setCopied(null), 1_600)
    } catch {
      setError('Не удалось скопировать автоматически. Выделите значение вручную.')
    }
  }

  const toggleSetupKey = async () => {
    if (setupData) {
      setSetupData(null)
      return
    }
    setSetupLoading(true)
    setError('')
    try {
      const data = await api.get<TotpExport>(`/accounts/${modal.account.id}/totp-export`)
      setSetupData(data)
    } catch (cause) {
      setError(cause instanceof ApiError && cause.status === 400
        ? 'У аккаунта нет рабочего TOTP setup key.'
        : cause instanceof ApiError ? cause.message : 'Не удалось получить setup key')
    } finally {
      setSetupLoading(false)
    }
  }

  return (
    <ModalOverlay onClose={onClose}>
      <div className="modal totp-dialog" role="dialog" aria-modal="true" aria-labelledby="totp-title">
        <div className="modal__header">
          <div><span className="eyebrow">Доступ к аккаунту</span><h2 id="totp-title">Одноразовый код</h2><p>{modal.account.login}</p></div>
          <button className="icon-button" onClick={onClose} aria-label="Закрыть"><Icon name="close" /></button>
        </div>

        {error && <div className="form-alert form-alert--error" role="alert"><Icon name="warning" /><span>{error}</span></div>}

        <div className="totp-current-code" aria-live="polite">
          <div>
            <span>{codeValid && secondsRemaining > 0 ? currentCode : '••••••'}</span>
            <small>{codeValid && secondsRemaining > 0 ? `Действует ещё ${secondsRemaining} с` : codeLoading ? 'Обновляем код…' : 'Повторим автоматически'}</small>
          </div>
          <button type="button" onClick={() => copy('code', currentCode)} disabled={!codeValid || secondsRemaining <= 0 || codeLoading} aria-label="Скопировать одноразовый код">
            <Icon name={copied === 'code' ? 'check' : 'copy'} />{copied === 'code' ? 'Скопировано' : 'Копировать'}
          </button>
        </div>

        <div className="totp-secondary-actions">
          <button type="button" className="button button--secondary button--compact" onClick={refreshCode} disabled={codeLoading}>{codeLoading ? <span className="spinner" /> : <Icon name="refresh" size={14} />}Обновить код</button>
          <button type="button" className="button button--ghost button--compact" onClick={toggleSetupKey} disabled={setupLoading} aria-expanded={setupData !== null}>
            {setupLoading ? <span className="spinner" /> : <Icon name="key" size={14} />}{setupData ? 'Скрыть setup key' : 'Показать setup key и QR'}
          </button>
        </div>

        {setupData && (
          <div className="totp-setup-export">
            <div className="form-alert form-alert--warning"><Icon name="warning" /><span>Setup key даёт постоянный доступ к кодам. Не передавайте его покупателю.</span></div>
            <img src={setupData.qr_png_base64} alt={`QR-код TOTP для ${modal.account.login}`} className="qr-code" />
            <label className="field"><span className="field__label">Setup key (base32)</span><span className="copy-field"><input readOnly value={setupData.secret} /><button type="button" onClick={() => copy('secret', setupData.secret)}><Icon name={copied === 'secret' ? 'check' : 'copy'} />{copied === 'secret' ? 'Скопировано' : 'Копировать'}</button></span></label>
            <label className="field"><span className="field__label">otpauth URI</span><span className="copy-field"><input readOnly value={setupData.otpauth_uri} /><button type="button" onClick={() => copy('uri', setupData.otpauth_uri)}><Icon name={copied === 'uri' ? 'check' : 'copy'} />{copied === 'uri' ? 'Скопировано' : 'Копировать'}</button></span></label>
          </div>
        )}

        <div className="modal__actions"><button className="button button--primary" onClick={onClose}>Готово</button></div>
      </div>
    </ModalOverlay>
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
    <ModalOverlay onClose={onClose} canClose={result.status !== 'pending'} closeOnBackdrop={result.status !== 'pending'}>
      <div className="modal device-auth-dialog" role="dialog" aria-modal="true" aria-labelledby="device-auth-title" aria-describedby="device-auth-description">
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
    </ModalOverlay>
  )
}

function isFreePlan(account: Account, tiers: Array<{ id: number; code?: string }>) {
  if (tiers.some((tier) => tier.id === account.tier_id && tier.code === 'free')) return true
  const candidates = [account.plan_raw_type, account.limits?.plan_type]
  return candidates.some((value) => value != null && normalizePlanCode(value) === 'free')
}

function subscriptionLabel(account: Account, tiers: Array<{ id: number; code?: string }>) {
  if (isFreePlan(account, tiers)) return 'Без срока'
  if (!['accounts_check', 'id_token'].includes(account.subscription_expiry_source ?? '')) {
    return 'Не подтверждена'
  }
  return formatDateTime(account.subscription_expires_at)
}

function normalizePlanCode(value: string) {
  return value
    .trim()
    .toLowerCase()
    .replace(/^chat[_-]?gpt[_-]?/, '')
    .replace(/[_-]?plan$/, '')
    .replace(/[^a-z0-9]+/g, '_')
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
