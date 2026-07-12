import { useEffect, useMemo, useState } from 'react'
import { useDurations, useLimitScopes, useTiers } from '../api/catalog'
import { ApiError } from '../api/client'
import { usePrices, useUpdatePrices } from '../api/prices'
import { Icon } from '../components/Icon'
import { EmptyState, ErrorState, LoadingState, PageHeader, TableShell } from '../components/ui'
import type { PriceMatrixItem } from '../types/api'
import { formatCurrency } from '../utils/format'

const priceKey = (item: PriceMatrixItem) => `${item.tier_id}:${item.duration_id}:${item.limit_scope_id}:${item.min_limit_pct ?? ''}:${item.max_5h_pct ?? ''}:${item.max_weekly_pct ?? ''}`

export default function Prices() {
  const pricesQuery = usePrices()
  const tiersQuery = useTiers()
  const durationsQuery = useDurations()
  const scopesQuery = useLimitScopes()
  const updatePrices = useUpdatePrices()
  const [draft, setDraft] = useState<PriceMatrixItem[]>([])
  const [dirty, setDirty] = useState(false)
  const [tierFilter, setTierFilter] = useState('all')
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    if (!dirty && pricesQuery.data) setDraft(pricesQuery.data.map((item) => ({ ...item })))
  }, [pricesQuery.data, dirty])

  const tiers = tiersQuery.data ?? []
  const durations = durationsQuery.data ?? []
  const scopes = scopesQuery.data ?? []
  const visibleDraft = useMemo(() => draft.filter((item) => tierFilter === 'all' || item.tier_id === Number(tierFilter)), [draft, tierFilter])

  if (pricesQuery.isLoading) return <LoadingState label="Загружаем матрицу цен" />
  if (pricesQuery.isError) return <ErrorState onRetry={() => pricesQuery.refetch()} />

  const tierName = (id: number) => tiers.find((tier) => tier.id === id)?.name ?? `Тариф #${id}`
  const durationDays = (id: number) => durations.find((duration) => duration.id === id)?.days ?? '?'
  const scopeCode = (id: number) => scopes.find((scope) => scope.id === id)?.code ?? 'unknown'
  const averagePrice = draft.length ? Math.round(draft.reduce((sum, item) => sum + item.price, 0) / draft.length) : 0

  const updateItem = (target: PriceMatrixItem, field: keyof PriceMatrixItem, value: number | undefined) => {
    const key = priceKey(target)
    setDraft((items) => items.map((item) => priceKey(item) === key ? { ...item, [field]: value } : item))
    setDirty(true)
    setSaved(false)
    setError('')
  }

  const discard = () => {
    setDraft((pricesQuery.data ?? []).map((item) => ({ ...item })))
    setDirty(false)
    setError('')
    setSaved(false)
  }

  const save = async () => {
    setError('')
    const invalid = draft.some((item) => item.price <= 0 || [item.min_limit_pct, item.max_5h_pct, item.max_weekly_pct].some((value) => value !== undefined && (value < 0 || value > 100)))
    if (invalid) {
      setError('Цена должна быть больше нуля, а пороги лимитов — от 0 до 100%.')
      return
    }
    try {
      await updatePrices.mutateAsync(draft)
      setDirty(false)
      setSaved(true)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Не удалось сохранить матрицу цен')
    }
  }

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Коммерция"
        title="Матрица цен"
        description="Стоимость аренды по тарифу, сроку и требуемому остатку лимитов."
        actions={<div className="header-action-group"><button className="button button--secondary" onClick={discard} disabled={!dirty}>Отменить изменения</button><button className="button button--primary" onClick={save} disabled={!dirty || updatePrices.isPending}>{updatePrices.isPending ? <><span className="spinner spinner--light" />Сохраняем…</> : <><Icon name="check" />Сохранить всё</>}</button></div>}
      />

      <section className="summary-strip">
        <div><span>Ценовых правил</span><strong>{draft.length}</strong></div>
        <div><span>Тарифов в матрице</span><strong>{new Set(draft.map((item) => item.tier_id)).size}</strong></div>
        <div><span>Средняя цена</span><strong>{formatCurrency(averagePrice)}</strong></div>
        <div><span>Состояние</span><strong className={dirty ? 'text-warning' : 'text-success'}>{dirty ? 'Есть изменения' : 'Сохранено'}</strong></div>
      </section>

      {error && <div className="form-alert form-alert--error"><Icon name="warning" /><span>{error}</span></div>}
      {saved && <div className="form-alert form-alert--success"><Icon name="check" /><span>Матрица цен сохранена.</span></div>}
      <div className="form-alert form-alert--info"><Icon name="warning" /><span>Backend заменяет матрицу целиком, поэтому изменения отправляются одной атомарной операцией. Обновление опубликованных FunPay-лотов после сохранения пока не гарантировано: серверный reconcile требует доработки.</span></div>

      <section className="panel panel--flush">
        <div className="toolbar">
          <div className="toolbar__title"><strong>Правила продаж</strong><span>Для ANY задаётся максимум расхода, для CHAT/CODEX — минимальный остаток.</span></div>
          <label className="select-field"><span>Тариф</span><select value={tierFilter} onChange={(event) => setTierFilter(event.target.value)}><option value="all">Все тарифы</option>{tiers.map((tier) => <option value={tier.id} key={tier.id}>{tier.name}</option>)}</select><Icon name="chevron-down" size={15} /></label>
        </div>

        {draft.length === 0 ? <EmptyState icon="prices" title="Матрица цен пуста" description="Сначала создайте тарифы, сроки и типы лимитов, затем добавьте ценовые комбинации. API создания комбинаций пока требует доработки." /> : (
          <TableShell><table className="data-table pricing-table"><thead><tr><th>Тариф</th><th>Срок</th><th>Тип лимита</th><th>Порог 5ч</th><th>Порог недели</th><th>Цена</th></tr></thead><tbody>{visibleDraft.map((item) => {
            const scope = scopeCode(item.limit_scope_id)
            return <tr key={priceKey(item)}><td><strong>{tierName(item.tier_id)}</strong></td><td><span className="duration-pill">{durationDays(item.duration_id)} дн.</span></td><td><span className={`scope-badge scope-badge--${scope}`}>{scope.toUpperCase()}</span></td><td>{scope === 'any' ? <PercentInput value={item.max_5h_pct} label="Максимум 5ч" onChange={(value) => updateItem(item, 'max_5h_pct', value)} /> : <PercentInput value={item.min_limit_pct} label="Минимальный остаток" onChange={(value) => updateItem(item, 'min_limit_pct', value)} />}</td><td>{scope === 'any' ? <PercentInput value={item.max_weekly_pct} label="Максимум недели" onChange={(value) => updateItem(item, 'max_weekly_pct', value)} /> : <span className="muted">Общий минимум</span>}</td><td><label className="money-input"><input type="number" min="1" step="1" value={item.price} onChange={(event) => updateItem(item, 'price', Number(event.target.value))} aria-label={`Цена ${tierName(item.tier_id)} ${durationDays(item.duration_id)} дней`} /><span>₽</span></label></td></tr>
          })}</tbody></table></TableShell>
        )}
      </section>
      {dirty && <div className="unsaved-bar"><span><span className="status-dot status-dot--warning" />Есть несохранённые изменения</span><div><button className="button button--ghost" onClick={discard}>Отменить</button><button className="button button--primary" onClick={save} disabled={updatePrices.isPending}>Сохранить всё</button></div></div>}
    </div>
  )
}

function PercentInput({ value, label, onChange }: { value?: number | null; label: string; onChange: (value: number | undefined) => void }) {
  return <label className="percent-input"><input type="number" min="0" max="100" value={value ?? ''} onChange={(event) => onChange(event.target.value === '' ? undefined : Number(event.target.value))} aria-label={label} /><span>%</span></label>
}
