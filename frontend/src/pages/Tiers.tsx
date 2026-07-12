import { useState } from 'react'
import { useCreateTier, useDeleteTier, useDurations, useLimitScopes, useTiers } from '../api/catalog'
import { ApiError } from '../api/client'
import { Icon } from '../components/Icon'
import { EmptyState, ErrorState, LoadingState, PageHeader, StatusBadge, TableShell } from '../components/ui'
import type { Tier } from '../types/api'

type CatalogTab = 'tiers' | 'durations' | 'scopes'

export default function Tiers() {
  const [tab, setTab] = useState<CatalogTab>('tiers')
  const tabs: Array<{ id: CatalogTab; label: string; description: string }> = [
    { id: 'tiers', label: 'Тарифы', description: 'Типы подписок ChatGPT' },
    { id: 'durations', label: 'Сроки', description: 'Доступные периоды аренды' },
    { id: 'scopes', label: 'Лимиты', description: 'Правила качества аккаунта' },
  ]

  return (
    <div className="page-stack">
      <PageHeader eyebrow="Товарная модель" title="Справочники" description="Базовые сущности, из которых строятся цены, лоты и правила выдачи." />
      <div className="catalog-tabs" role="tablist" aria-label="Разделы справочников">
        {tabs.map((item) => (
          <button key={item.id} type="button" role="tab" aria-selected={tab === item.id} className={tab === item.id ? 'active' : ''} onClick={() => setTab(item.id)}>
            <strong>{item.label}</strong><span>{item.description}</span>
          </button>
        ))}
      </div>
      {tab === 'tiers' && <TiersTab />}
      {tab === 'durations' && <DurationsTab />}
      {tab === 'scopes' && <ScopesTab />}
    </div>
  )
}

function TiersTab() {
  const tiersQuery = useTiers()
  const createTier = useCreateTier()
  const deleteTier = useDeleteTier()
  const [showForm, setShowForm] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<Tier | null>(null)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [error, setError] = useState('')

  if (tiersQuery.isLoading) return <LoadingState label="Загружаем тарифы" />
  if (tiersQuery.isError) return <ErrorState onRetry={() => tiersQuery.refetch()} />

  const tiers = tiersQuery.data ?? []

  const create = async (event: React.FormEvent) => {
    event.preventDefault()
    setError('')
    try {
      await createTier.mutateAsync({ name: name.trim(), description: description.trim() || undefined })
      setName('')
      setDescription('')
      setShowForm(false)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Не удалось создать тариф')
    }
  }

  const remove = async () => {
    if (!deleteTarget) return
    setError('')
    try {
      await deleteTier.mutateAsync(deleteTarget.id)
      setDeleteTarget(null)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Не удалось удалить тариф')
      setDeleteTarget(null)
    }
  }

  return (
    <section className="panel panel--flush">
      <div className="section-toolbar">
        <div><h2>Тарифы подписок</h2><p>Например Plus, Pro или Team. Используются в аккаунтах и ценовых правилах.</p></div>
        <button className="button button--primary" onClick={() => setShowForm((value) => !value)}><Icon name={showForm ? 'close' : 'plus'} />{showForm ? 'Закрыть форму' : 'Новый тариф'}</button>
      </div>
      {error && <div className="form-alert form-alert--error"><Icon name="warning" /><span>{error}</span></div>}
      {showForm && (
        <form className="inline-create-card" onSubmit={create}>
          <label className="field"><span className="field__label">Название</span><input value={name} onChange={(event) => setName(event.target.value)} placeholder="Например, Plus" autoFocus required /></label>
          <label className="field field--grow"><span className="field__label">Описание</span><input value={description} onChange={(event) => setDescription(event.target.value)} placeholder="Коротко опишите назначение тарифа" /></label>
          <button className="button button--primary" type="submit" disabled={createTier.isPending || !name.trim()}>{createTier.isPending ? 'Создаём…' : 'Создать'}</button>
        </form>
      )}
      {tiers.length === 0 ? (
        <EmptyState icon="catalog" title="Тарифов ещё нет" description="Создайте первый тариф, чтобы можно было добавлять аккаунты и настраивать цены." action={<button className="button button--primary" onClick={() => setShowForm(true)}><Icon name="plus" />Создать тариф</button>} />
      ) : (
        <TableShell><table className="data-table"><thead><tr><th>Название</th><th>Описание</th><th>Статус</th><th><span className="sr-only">Действия</span></th></tr></thead><tbody>{tiers.map((tier) => (
          <tr key={tier.id}><td><div className="identity-cell"><span className="identity-avatar identity-avatar--violet">{tier.name.slice(0, 1).toUpperCase()}</span><span><strong>{tier.name}</strong><small>ID {tier.id}</small></span></div></td><td>{tier.description || 'Без описания'}</td><td><StatusBadge value={tier.is_active ? 'active' : 'paused'} /></td><td><div className="row-actions"><button className="icon-button icon-button--danger" onClick={() => setDeleteTarget(tier)} aria-label={`Удалить тариф ${tier.name}`}><Icon name="trash" /></button></div></td></tr>
        ))}</tbody></table></TableShell>
      )}
      {deleteTarget && <ConfirmCatalogDelete name={deleteTarget.name} pending={deleteTier.isPending} onCancel={() => setDeleteTarget(null)} onConfirm={remove} />}
    </section>
  )
}

function DurationsTab() {
  const query = useDurations()
  if (query.isLoading) return <LoadingState label="Загружаем сроки аренды" />
  if (query.isError) return <ErrorState onRetry={() => query.refetch()} />
  const durations = query.data ?? []
  return (
    <section className="panel panel--flush">
      <div className="section-toolbar"><div><h2>Сроки аренды</h2><p>Включённые периоды участвуют в построении матрицы цен.</p></div><span className="soft-badge"><Icon name="warning" size={14} />Редактирование требует API</span></div>
      {durations.length === 0 ? <EmptyState icon="clock" title="Сроки не инициализированы" description="Backend должен создать набор сроков от 1 до 30 дней при первоначальном запуске." /> : (
        <div className="duration-grid">{durations.map((duration) => <article className={`duration-card ${duration.is_enabled ? 'duration-card--active' : ''}`} key={duration.id}><span>{duration.days}</span><strong>{duration.days === 1 ? 'день' : duration.days < 5 ? 'дня' : 'дней'}</strong><StatusBadge value={duration.is_enabled ? 'active' : 'paused'} /></article>)}</div>
      )}
    </section>
  )
}

function ScopesTab() {
  const query = useLimitScopes()
  if (query.isLoading) return <LoadingState label="Загружаем типы лимитов" />
  if (query.isError) return <ErrorState onRetry={() => query.refetch()} />
  const scopes = query.data ?? []
  const descriptions: Record<string, string> = {
    any: 'Без гарантии остатка конкретного лимита. Подходит для базовых предложений.',
    chat: 'Гарантированный остаток лимитов ChatGPT для диалогов.',
    codex: 'Гарантированный остаток лимитов Codex для задач разработки.',
  }
  return (
    <section className="panel panel--flush">
      <div className="section-toolbar"><div><h2>Типы лимитов</h2><p>Определяют, какие показатели учитываются при подборе аккаунта.</p></div><span className="soft-badge"><Icon name="warning" size={14} />Редактирование требует API</span></div>
      {scopes.length === 0 ? <EmptyState icon="activity" title="Типы лимитов не инициализированы" description="Ожидаются системные значения any, chat и codex." /> : (
        <div className="scope-grid">{scopes.map((scope) => <article className="scope-card" key={scope.id}><div className="scope-card__icon"><Icon name={scope.code === 'codex' ? 'templates' : scope.code === 'chat' ? 'activity' : 'catalog'} /></div><div><span className="eyebrow">{scope.code}</span><h3>{scope.name}</h3><p>{descriptions[scope.code] ?? 'Системное правило подбора аккаунтов.'}</p></div></article>)}</div>
      )}
    </section>
  )
}

function ConfirmCatalogDelete({ name, pending, onCancel, onConfirm }: { name: string; pending: boolean; onCancel: () => void; onConfirm: () => void }) {
  return <div className="modal-overlay" role="presentation"><div className="modal modal--compact" role="alertdialog" aria-modal="true"><div className="modal__danger-icon"><Icon name="trash" /></div><h2>Удалить тариф «{name}»?</h2><p>Удаление невозможно, если тариф уже используется аккаунтами, лотами или ценовыми правилами.</p><div className="modal__actions"><button className="button button--secondary" onClick={onCancel}>Отмена</button><button className="button button--danger" onClick={onConfirm} disabled={pending}>{pending ? 'Удаляем…' : 'Удалить'}</button></div></div></div>
}
