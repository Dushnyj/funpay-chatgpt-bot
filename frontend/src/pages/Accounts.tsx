import { useEffect, useMemo, useState } from 'react'
import { useAccounts, useCreateAccount, useDeleteAccount } from '../api/accounts'
import { useTiers } from '../api/catalog'
import { api, ApiError } from '../api/client'
import { Icon } from '../components/Icon'
import { EmptyState, ErrorState, LoadingState, PageHeader, StatusBadge, TableShell } from '../components/ui'
import type { Account, Tier, TotpExport } from '../types/api'
import { formatDate } from '../utils/format'

export default function Accounts() {
  const accountsQuery = useAccounts()
  const tiersQuery = useTiers()
  const deleteAccount = useDeleteAccount()
  const [showForm, setShowForm] = useState(false)
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState('all')
  const [totpModal, setTotpModal] = useState<{ account: Account; data: TotpExport } | null>(null)
  const [totpLoading, setTotpLoading] = useState<number | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<Account | null>(null)
  const [actionError, setActionError] = useState('')

  const accounts = useMemo(() => accountsQuery.data ?? [], [accountsQuery.data])
  const tiers = tiersQuery.data ?? []
  const filteredAccounts = useMemo(() => {
    const query = search.trim().toLowerCase()
    return accounts.filter((account) => {
      const matchesSearch = !query || account.login.toLowerCase().includes(query) || account.email?.toLowerCase().includes(query)
      const matchesStatus = status === 'all' || account.status === status
      return matchesSearch && matchesStatus
    })
  }, [accounts, search, status])

  if (accountsQuery.isLoading) return <LoadingState label="Загружаем пул аккаунтов" />
  if (accountsQuery.isError) return <ErrorState onRetry={() => accountsQuery.refetch()} />

  const tierName = (id: number) => tiers.find((tier) => tier.id === id)?.name ?? `Тариф #${id}`
  const activeCount = accounts.filter((account) => account.status === 'active').length
  const attentionCount = accounts.filter((account) => ['maintenance', 'banned'].includes(account.status)).length

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

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Пул ресурсов"
        title="Аккаунты"
        description="ChatGPT-аккаунты, их готовность к выдаче и состояние проверки."
        actions={
          <button className="button button--primary" onClick={() => setShowForm(true)} disabled={!tiers.length} title={!tiers.length ? 'Сначала создайте тариф' : undefined}>
            <Icon name="plus" />Добавить аккаунт
          </button>
        }
      />

      <section className="summary-strip" aria-label="Сводка по аккаунтам">
        <div><span>Всего в пуле</span><strong>{accounts.length}</strong></div>
        <div><span className="summary-dot summary-dot--success" /> <span>Активны</span><strong>{activeCount}</strong></div>
        <div><span className="summary-dot summary-dot--warning" /> <span>Проверяются</span><strong>{accounts.filter((account) => account.status === 'pending_validation').length}</strong></div>
        <div><span className="summary-dot summary-dot--danger" /> <span>Требуют внимания</span><strong>{attentionCount}</strong></div>
      </section>

      {!tiers.length && (
        <div className="form-alert form-alert--warning"><Icon name="warning" /><span>Перед добавлением аккаунта создайте хотя бы один тариф в разделе «Справочники».</span></div>
      )}
      {actionError && <div className="form-alert form-alert--error" role="alert"><Icon name="warning" /><span>{actionError}</span></div>}

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
              <option value="pending_validation">Проверяются</option>
              <option value="maintenance">Обслуживание</option>
              <option value="banned">Заблокированные</option>
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
            action={tiers.length ? <button className="button button--primary" onClick={() => setShowForm(true)}><Icon name="plus" />Добавить аккаунт</button> : undefined}
          />
        ) : filteredAccounts.length === 0 ? (
          <EmptyState icon="search" title="Ничего не найдено" description="Измените строку поиска или фильтр статуса." />
        ) : (
          <TableShell>
            <table className="data-table accounts-table">
              <thead><tr><th>Аккаунт</th><th>Тариф</th><th>Подписка</th><th>Лимит аренд</th><th>Состояние</th><th><span className="sr-only">Действия</span></th></tr></thead>
              <tbody>
                {filteredAccounts.map((account) => (
                  <tr key={account.id}>
                    <td>
                      <div className="identity-cell"><span className="identity-avatar">{account.login.slice(0, 1).toUpperCase()}</span><span><strong>{account.login}</strong><small>{account.email ?? 'Email для восстановления не задан'}</small></span></div>
                    </td>
                    <td><span className="soft-badge">{tierName(account.tier_id)}</span></td>
                    <td>{formatDate(account.subscription_expires_at)}</td>
                    <td>{account.max_active_rentals ?? 'По умолчанию'}</td>
                    <td><StatusBadge value={account.status} /></td>
                    <td>
                      <div className="row-actions">
                        <button className="icon-button" onClick={() => exportTotp(account)} disabled={totpLoading === account.id || account.status !== 'active'} aria-label={`Экспорт TOTP для ${account.login}`} title="Экспорт TOTP">
                          {totpLoading === account.id ? <span className="spinner" /> : <Icon name="key" />}
                        </button>
                        <button className="icon-button icon-button--danger" onClick={() => setDeleteTarget(account)} aria-label={`Удалить ${account.login}`} title="Удалить">
                          <Icon name="trash" />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableShell>
        )}
      </section>

      {showForm && <AddAccountDialog tiers={tiers} onClose={() => setShowForm(false)} />}
      {totpModal && <TotpDialog modal={totpModal} onClose={() => setTotpModal(null)} />}
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

function AddAccountDialog({ tiers, onClose }: { tiers: Tier[]; onClose: () => void }) {
  const createAccount = useCreateAccount()
  const [mode, setMode] = useState<'totp' | 'email'>('totp')
  const [error, setError] = useState('')
  const [form, setForm] = useState({
    login: '', password: '', totp_secret: '', email: '', email_password: '', tier_id: tiers[0]?.id ?? 0,
  })

  useEffect(() => {
    if (!form.tier_id && tiers[0]) setForm((current) => ({ ...current, tier_id: tiers[0].id }))
  }, [tiers, form.tier_id])

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setError('')
    if (!form.tier_id) {
      setError('Сначала выберите тариф.')
      return
    }
    if (mode === 'totp' && !form.totp_secret.trim()) {
      setError('Укажите TOTP setup key или выберите настройку через email.')
      return
    }
    if (mode === 'email' && (!form.email.trim() || !form.email_password)) {
      setError('Для автоматической настройки 2FA нужны email и App Password.')
      return
    }
    try {
      await createAccount.mutateAsync({
        ...form,
        totp_secret: mode === 'totp' ? form.totp_secret.trim() : '',
        email: form.email.trim() || undefined,
        email_password: form.email_password || undefined,
      })
      onClose()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Не удалось добавить аккаунт')
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
          <div className="form-grid form-grid--3">
            <label className="field"><span className="field__label">Логин ChatGPT</span><input value={form.login} onChange={(event) => setForm({ ...form, login: event.target.value })} placeholder="name@example.com" autoComplete="off" required /></label>
            <label className="field"><span className="field__label">Пароль ChatGPT</span><input type="password" value={form.password} onChange={(event) => setForm({ ...form, password: event.target.value })} placeholder="Пароль аккаунта" autoComplete="new-password" required /></label>
            <label className="field"><span className="field__label">Тариф</span><select value={form.tier_id} onChange={(event) => setForm({ ...form, tier_id: Number(event.target.value) })} required><option value={0} disabled>Выберите тариф</option>{tiers.map((tier) => <option key={tier.id} value={tier.id}>{tier.name}</option>)}</select></label>
          </div>

          <fieldset className="segmented-fieldset">
            <legend>Как настроить двухфакторную защиту</legend>
            <div className="segmented-control">
              <button type="button" className={mode === 'totp' ? 'active' : ''} onClick={() => setMode('totp')}><Icon name="key" />TOTP уже включён</button>
              <button type="button" className={mode === 'email' ? 'active' : ''} onClick={() => setMode('email')}><Icon name="shield" />Настроить через email</button>
            </div>
          </fieldset>

          {mode === 'totp' ? (
            <label className="field"><span className="field__label">TOTP setup key</span><input value={form.totp_secret} onChange={(event) => setForm({ ...form, totp_secret: event.target.value.toUpperCase().replaceAll(' ', '') })} placeholder="JBSWY3DPEHPK3PXP" autoComplete="off" /><span className="field__hint">Base32-ключ из настроек 2FA. Не QR-код и не одноразовый шестизначный код.</span></label>
          ) : (
            <div className="form-grid">
              <label className="field"><span className="field__label">Email для подтверждений</span><input type="email" value={form.email} onChange={(event) => setForm({ ...form, email: event.target.value })} placeholder="mail@example.com" required /></label>
              <label className="field"><span className="field__label">App Password почты</span><input type="password" value={form.email_password} onChange={(event) => setForm({ ...form, email_password: event.target.value })} placeholder="Пароль приложения" autoComplete="new-password" required /></label>
            </div>
          )}

          <div className="form-alert form-alert--info"><Icon name="activity" /><span>После сохранения аккаунт должен попасть в очередь первичной проверки. Сейчас этот backend-trigger требует доработки и отмечен в ревью.</span></div>
          <div className="modal__actions"><button type="button" className="button button--secondary" onClick={onClose}>Отмена</button><button type="submit" className="button button--primary" disabled={createAccount.isPending}>{createAccount.isPending ? <><span className="spinner spinner--light" />Сохраняем…</> : <>Добавить аккаунт<Icon name="arrow-right" /></>}</button></div>
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
