import { useState } from 'react'
import { useTiers, useCreateTier, useDeleteTier, useDurations, useLimitScopes } from '../api/catalog'

export default function Tiers() {
  const [tab, setTab] = useState<'tiers' | 'durations' | 'scopes'>('tiers')
  return (
    <div>
      <h1>Справочники</h1>
      <div className="tabs">
        <button className={tab === 'tiers' ? 'active' : ''} onClick={() => setTab('tiers')}>Тарифы</button>
        <button className={tab === 'durations' ? 'active' : ''} onClick={() => setTab('durations')}>Сроки</button>
        <button className={tab === 'scopes' ? 'active' : ''} onClick={() => setTab('scopes')}>Лимиты</button>
      </div>
      {tab === 'tiers' && <TiersTab />}
      {tab === 'durations' && <DurationsTab />}
      {tab === 'scopes' && <ScopesTab />}
    </div>
  )
}

function TiersTab() {
  const { data: tiers } = useTiers()
  const createTier = useCreateTier()
  const deleteTier = useDeleteTier()
  const [name, setName] = useState('')
  return (
    <div>
      <form
        onSubmit={(e) => { e.preventDefault(); createTier.mutate({ name }); setName('') }}
        className="inline-form"
      >
        <input placeholder="Название тарифа" value={name} onChange={(e) => setName(e.target.value)} required />
        <button type="submit">Добавить</button>
      </form>
      <table className="data-table">
        <thead><tr><th>ID</th><th>Название</th><th>Активен</th><th></th></tr></thead>
        <tbody>
          {tiers?.map((t) => (
            <tr key={t.id}>
              <td>{t.id}</td><td>{t.name}</td><td>{t.is_active ? '✅' : '❌'}</td>
              <td><button onClick={() => deleteTier.mutate(t.id)}>Удалить</button></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function DurationsTab() {
  const { data: durations } = useDurations()
  return (
    <table className="data-table">
      <thead><tr><th>ID</th><th>Дней</th><th>Включён</th><th>Порядок</th></tr></thead>
      <tbody>
        {durations?.map((d) => (
          <tr key={d.id}><td>{d.id}</td><td>{d.days}</td><td>{d.is_enabled ? '✅' : '❌'}</td><td>{d.sort_order}</td></tr>
        ))}
      </tbody>
    </table>
  )
}

function ScopesTab() {
  const { data: scopes } = useLimitScopes()
  return (
    <table className="data-table">
      <thead><tr><th>ID</th><th>Код</th><th>Название</th></tr></thead>
      <tbody>
        {scopes?.map((s) => (
          <tr key={s.id}><td>{s.id}</td><td>{s.code}</td><td>{s.name}</td></tr>
        ))}
      </tbody>
    </table>
  )
}
