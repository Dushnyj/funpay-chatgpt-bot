import { useState } from 'react'
import { useAccounts, useCreateAccount, useDeleteAccount } from '../api/accounts'
import { useTiers } from '../api/catalog'
import { api } from '../api/client'
import type { Tier, TotpExport } from '../types/api'

export default function Accounts() {
  const { data: accounts, isLoading } = useAccounts()
  const { data: tiers } = useTiers()
  const deleteAccount = useDeleteAccount()
  const [showForm, setShowForm] = useState(false)
  const [totpModal, setTotpModal] = useState<TotpExport | null>(null)
  const [totpLoading, setTotpLoading] = useState<number | null>(null)

  if (isLoading) return <div>Загрузка...</div>

  const tierName = (id: number) => tiers?.find((t) => t.id === id)?.name ?? `#${id}`

  const exportTotp = async (id: number) => {
    setTotpLoading(id)
    try {
      const data = await api.get<TotpExport>(`/accounts/${id}/totp-export`)
      setTotpModal(data)
    } catch {
      alert('Не удалось получить TOTP')
    } finally {
      setTotpLoading(null)
    }
  }

  return (
    <div>
      <h1>Аккаунты ({accounts?.length ?? 0})</h1>
      <button onClick={() => setShowForm(!showForm)}>Добавить</button>
      {showForm && <AddAccountForm tiers={tiers ?? []} />}
      <table className="data-table">
        <thead>
          <tr><th>ID</th><th>Логин</th><th>Email</th><th>Tier</th><th>Статус</th><th>Действия</th></tr>
        </thead>
        <tbody>
          {accounts?.map((acc) => (
            <tr key={acc.id}>
              <td>{acc.id}</td>
              <td>{acc.login}</td>
              <td>{acc.email ?? '—'}</td>
              <td>{tierName(acc.tier_id)}</td>
              <td className={`status-${acc.status}`}>{acc.status}</td>
              <td>
                {acc.status === 'active' && (
                  <button onClick={() => exportTotp(acc.id)} disabled={totpLoading === acc.id}>
                    {totpLoading === acc.id ? '...' : 'TOTP'}
                  </button>
                )}{' '}
                <button onClick={() => deleteAccount.mutate(acc.id)}>Удалить</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {totpModal && (
        <div className="modal-overlay" onClick={() => setTotpModal(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h2>Экспорт TOTP</h2>
            <img src={totpModal.qr_png_base64} alt="QR" className="qr-code" />
            <label>Secret (base32)
              <input readOnly value={totpModal.secret} onClick={(e) => (e.target as HTMLInputElement).select()} />
            </label>
            <label>otpauth:// URI
              <input readOnly value={totpModal.otpauth_uri} onClick={(e) => (e.target as HTMLInputElement).select()} />
            </label>
            <button onClick={() => setTotpModal(null)}>Закрыть</button>
          </div>
        </div>
      )}
    </div>
  )
}

function AddAccountForm({ tiers }: { tiers: Tier[] }) {
  const createAccount = useCreateAccount()
  const [form, setForm] = useState({
    login: '', password: '', totp_secret: '',
    email: '', email_password: '',
    tier_id: tiers[0]?.id ?? 0,
  })
  return (
    <form
      onSubmit={(e) => { e.preventDefault(); createAccount.mutate(form) }}
      className="account-form"
    >
      <div className="form-row">
        <input placeholder="Логин (email)" value={form.login} onChange={(e) => setForm({ ...form, login: e.target.value })} required />
        <input placeholder="Пароль ChatGPT" type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} required />
      </div>
      <div className="form-row">
        <input placeholder="Email (для IMAP)" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
        <input placeholder="Пароль почты (App Password)" type="password" value={form.email_password} onChange={(e) => setForm({ ...form, email_password: e.target.value })} />
      </div>
      <div className="form-row">
        <input placeholder="TOTP secret (если уже включена 2FA)" value={form.totp_secret} onChange={(e) => setForm({ ...form, totp_secret: e.target.value })} />
        <select value={form.tier_id} onChange={(e) => setForm({ ...form, tier_id: Number(e.target.value) })}>
          {tiers.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
        </select>
        <button type="submit">Создать</button>
      </div>
      <p className="form-hint">
        Если TOTP secret пустой — бот сам включит 2FA (нужен email + пароль почты).
        Если 2FA уже включена — вставьте setup key.
      </p>
    </form>
  )
}
