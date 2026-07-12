import { useState } from 'react'
import { useDurations, useLimitScopes, useTiers, useUpdateTier } from '../api/catalog'
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
  const updateTier = useUpdateTier()
  const [updatingTier, setUpdatingTier] = useState<number | null>(null)
  const [error, setError] = useState('')

  if (tiersQuery.isLoading) return <LoadingState label="Загружаем тарифы" />
  if (tiersQuery.isError) return <ErrorState onRetry={() => tiersQuery.refetch()} />

  const tiers = [...(tiersQuery.data ?? [])].sort((left, right) =>
    (left.sort_order ?? left.id) - (right.sort_order ?? right.id),
  )

  const toggleSellable = async (tier: Tier) => {
    setError('')
    setUpdatingTier(tier.id)
    try {
      await updateTier.mutateAsync({ id: tier.id, is_sellable: !(tier.is_sellable ?? tier.is_active) })
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Не удалось изменить доступность тарифа')
    } finally {
      setUpdatingTier(null)
    }
  }

  return (
    <section className="panel panel--flush">
      <div className="section-toolbar">
        <div><h2>Системный каталог тарифов</h2><p>Free, Go, Plus и варианты Pro распознаются автоматически по данным аккаунта.</p></div>
        <span className="soft-badge"><Icon name="shield" size={14} />Синхронизируется системой</span>
      </div>
      <div className="form-alert form-alert--info catalog-system-note"><Icon name="activity" /><span>Названия и состав каталога защищены от ручного изменения. Вы можете только разрешить или запретить продажу конкретного тарифа.</span></div>
      {error && <div className="form-alert form-alert--error"><Icon name="warning" /><span>{error}</span></div>}
      {tiers.length === 0 ? (
        <EmptyState icon="catalog" title="Системный каталог не инициализирован" description="Перезапустите bootstrap backend: тарифы создаются автоматически и не требуют ручного ввода." action={<button className="button button--secondary" onClick={() => tiersQuery.refetch()}><Icon name="refresh" />Обновить</button>} />
      ) : (
        <TableShell><table className="data-table tier-catalog-table"><thead><tr><th>Тариф</th><th>Описание</th><th>Коэффициент</th><th>Состояние</th><th>Продажа</th></tr></thead><tbody>{tiers.map((tier) => (
          <tr key={tier.id}>
            <td><div className="identity-cell"><span className="identity-avatar identity-avatar--violet">{displayTierName(tier).slice(0, 1).toUpperCase()}</span><span><strong>{displayTierName(tier)}</strong><small title={`Системный код: ${tier.code ?? `system-${tier.id}`}`}>Системный тариф</small></span></div></td>
            <td>{displayTierDescription(tier)}</td>
            <td>{tier.usage_multiplier == null ? '—' : `×${tier.usage_multiplier}`}</td>
            <td><StatusBadge value={tier.is_active ? 'active' : 'paused'} /></td>
            <td>
              <label className="switch-control">
                <input type="checkbox" checked={tier.is_sellable ?? tier.is_active} onChange={() => toggleSellable(tier)} disabled={updatingTier === tier.id || !tier.is_active} />
                <span aria-hidden="true" />
                <strong>{(tier.is_sellable ?? tier.is_active) ? 'Разрешён' : 'Выключен'}</strong>
              </label>
            </td>
          </tr>
        ))}</tbody></table></TableShell>
      )}
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
      <div className="section-toolbar"><div><h2>Сроки аренды</h2><p>Включённые периоды участвуют в построении матрицы цен.</p></div><span className="soft-badge"><Icon name="shield" size={14} />Только просмотр</span></div>
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
      <div className="section-toolbar"><div><h2>Типы лимитов</h2><p>Определяют, какие показатели учитываются при подборе аккаунта.</p></div><span className="soft-badge"><Icon name="shield" size={14} />Только просмотр</span></div>
      {scopes.length === 0 ? <EmptyState icon="activity" title="Типы лимитов не инициализированы" description="Ожидаются системные значения any, chat и codex." /> : (
        <div className="scope-grid">{scopes.map((scope) => <article className="scope-card" key={scope.id}><div className="scope-card__icon"><Icon name={scope.code === 'codex' ? 'templates' : scope.code === 'chat' ? 'activity' : 'catalog'} /></div><div><span className="eyebrow">{scope.code}</span><h3>{scope.name}</h3><p>{descriptions[scope.code] ?? 'Системное правило подбора аккаунтов.'}</p></div></article>)}</div>
      )}
    </section>
  )
}

function displayTierName(tier: Tier) {
  return tier.name.replace(/\s*\/\s*usage-based\s*$/i, '')
}

function displayTierDescription(tier: Tier) {
  if (!tier.description) return 'Канонический план ChatGPT'
  return tier.description
    .replace(/\s*\(raw:\s*[^)]+\)/gi, '')
    .replace(/,\s*включая прежнее raw-имя\s+[^,.]+/gi, '')
    .replace(/с usage-based конфигурацией/gi, 'с оплатой по фактическому использованию')
    .trim()
}
