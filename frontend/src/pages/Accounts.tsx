import { useState } from 'react'
import { useAccounts, useCreateAccount, useDeleteAccount } from '../api/accounts'
import { useTiers } from '../api/catalog'
import type { Tier } from '../types/api'

export default function Accounts() {
  const { data: accounts, isLoading } = useAccounts()
  const { data: tiers } = useTiers()
  const deleteAccount = useDeleteAccount()
  const [showForm, setShowForm] = useState(false)

  if (isLoading) return <div>Загрузка...</div>

  const tierName = (id: number) => tiers?.find((t) => t.id === id)?.name ?? `#${id}`

  return (
    <div>
      <h1>Аккаунты ({accounts?.length ?? 0})</h1>
      <button onClick={() => setShowForm(!showForm)}>Добавить</button>
      {showForm && <AddAccountForm tiers={tiers ?? []} />}
      <table className="data-table">
        <thead>
          <tr><th>ID</th><th>Логин</th><th>Tier</th><th>Статус</th><th>Действия</th></tr>
        </thead>
        <tbody>
          {accounts?.map((acc) => (
            <tr key={acc.id}>
              <td>{acc.id}</td>
              <td>{acc.login}</td>
              <td>{tierName(acc.tier_id)}</td>
              <td className={`status-${acc.status}`}>{acc.status}</td>
              <td><button onClick={() => deleteAccount.mutate(acc.id)}>Удалить</button></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function AddAccountForm({ tiers }: { tiers: Tier[] }) {
  const createAccount = useCreateAccount()
  const [form, setForm] = useState({
    login: '', password: '', totp_secret: '', tier_id: tiers[0]?.id ?? 0,
  })
  return (
    <form
      onSubmit={(e) => { e.preventDefault(); createAccount.mutate(form) }}
      className="inline-form"
    >
      <input placeholder="Логин" value={form.login} onChange={(e) => setForm({ ...form, login: e.target.value })} required />
      <input placeholder="Пароль" type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} required />
      <input placeholder="TOTP secret" value={form.totp_secret} onChange={(e) => setForm({ ...form, totp_secret: e.target.value })} required />
      <select value={form.tier_id} onChange={(e) => setForm({ ...form, tier_id: Number(e.target.value) })}>
        {tiers.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
      </select>
      <button type="submit">Создать</button>
    </form>
  )
}
