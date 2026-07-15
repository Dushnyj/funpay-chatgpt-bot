import { useEffect, useMemo, useState } from 'react'
import { useDurations, useLimitScopes, useTiers } from '../api/catalog'
import { ApiError } from '../api/client'
import { usePrices, useUpdatePrices } from '../api/prices'
import { Icon } from '../components/Icon'
import { EmptyState, ErrorState, LoadingState, PageHeader, TableShell } from '../components/ui'
import type { PriceMatrixItem, Tier } from '../types/api'
import { compareDurationsByMinutes, formatDurationMinutes } from '../utils/catalogEditor'
import { formatCurrency } from '../utils/format'
import { normalizePriceRule, priceRuleSignature, priceRuleToWire } from '../utils/priceRules'
import {
  compareOfferScopes,
  isAvailableOfferScope,
  isSupportedOfferScopeCode,
  offerScopeDisplayCode,
  offerScopeDisplayName,
  offerScopeUnavailableReason,
} from '../utils/offerScopes'

type DraftRule = PriceMatrixItem & { draftId: string }
type BuilderState = {
  tierId: string
  durationId: string
  scopeId: string
  minLimit: string
  price: string
}

let draftSequence = 0
const nextDraftId = () => `price-rule-${++draftSequence}`

export default function Prices() {
  const pricesQuery = usePrices()
  const tiersQuery = useTiers()
  const durationsQuery = useDurations()
  const scopesQuery = useLimitScopes()
  const updatePrices = useUpdatePrices()
  const [draft, setDraft] = useState<DraftRule[]>([])
  const [dirty, setDirty] = useState(false)
  const [tierFilter, setTierFilter] = useState('all')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [saved, setSaved] = useState(false)
  const [builder, setBuilder] = useState<BuilderState>({
    tierId: '', durationId: '', scopeId: '', minLimit: '50', price: '',
  })

  const tiers = tiersQuery.data ?? []
  const durations = durationsQuery.data ?? []
  const scopes = scopesQuery.data ?? []
  const configurableScopes = scopes
    .filter(isAvailableOfferScope)
    .sort(compareOfferScopes)
  const sellableTiers = tiers.filter(isTierSellable)
  const enabledDurations = durations
    .filter((duration) => duration.is_enabled)
    .sort(compareDurationsByMinutes)
  const visibleDraft = useMemo(
    () => draft.filter((item) => tierFilter === 'all' || item.tier_id === Number(tierFilter)),
    [draft, tierFilter],
  )
  const unavailableTierRules = draft.filter((item) => !isTierSellable(tiers.find((tier) => tier.id === item.tier_id))).length
  const unavailableDurationRules = draft.filter(
    (item) => durations.find((duration) => duration.id === item.duration_id)?.is_enabled !== true,
  ).length
  const unavailableScopeRules = draft.filter((item) => {
    const scope = scopes.find((candidate) => candidate.id === item.limit_scope_id)
    return !isAvailableOfferScope(scope)
  }).length

  const tierName = (id: number) => tiers.find((tier) => tier.id === id)?.name ?? `Тариф #${id}`
  const durationLabel = (id: number) => {
    const duration = durations.find((candidate) => candidate.id === id)
    return duration ? formatDurationMinutes(duration.minutes) : 'неизвестный срок'
  }
  const scopeCode = (id: number) => scopes.find((scope) => scope.id === id)?.code.toLowerCase() ?? 'unknown'
  const scopeName = (id: number) => offerScopeDisplayName(scopes.find((scope) => scope.id === id))
  const averagePrice = draft.length ? Math.round(draft.reduce((sum, item) => sum + item.price, 0) / draft.length) : 0

  useEffect(() => {
    if (!dirty && pricesQuery.data && tiersQuery.data && scopesQuery.data) {
      setDraft(pricesQuery.data.map((item) => normalizePriceRule(
        { ...item, draftId: nextDraftId() },
        scopesQuery.data.find(
          (scope) => scope.id === item.limit_scope_id,
        )?.code.toLowerCase() ?? 'unknown',
      )))
    }
  }, [pricesQuery.data, tiersQuery.data, scopesQuery.data, dirty])

  const requestedBuilderTier = Number(builder.tierId)
  const resolvedBuilderTier = sellableTiers.some((tier) => tier.id === requestedBuilderTier)
    ? requestedBuilderTier
    : sellableTiers[0]?.id ?? 0
  const requestedBuilderDuration = Number(builder.durationId)
  const resolvedBuilderDuration = enabledDurations.some((duration) => duration.id === requestedBuilderDuration)
    ? requestedBuilderDuration
    : enabledDurations[0]?.id ?? 0
  const requestedBuilderScope = Number(builder.scopeId)
  const resolvedBuilderScope = configurableScopes.some((scope) => scope.id === requestedBuilderScope)
    ? requestedBuilderScope
    : configurableScopes.find((scope) => scope.code.toLowerCase() === 'any')?.id ?? configurableScopes[0]?.id ?? 0
  const builderScopeCode = scopeCode(resolvedBuilderScope)

  if (pricesQuery.isLoading || tiersQuery.isLoading || durationsQuery.isLoading || scopesQuery.isLoading) {
    return <LoadingState label="Загружаем матрицу цен" />
  }
  if (pricesQuery.isError || tiersQuery.isError || durationsQuery.isError || scopesQuery.isError) {
    return <ErrorState onRetry={() => void Promise.all([
      pricesQuery.refetch(), tiersQuery.refetch(), durationsQuery.refetch(), scopesQuery.refetch(),
    ])} />
  }

  const markChanged = () => {
    setDirty(true)
    setSaved(false)
    setError('')
  }

  const updateItem = (draftId: string, patch: Partial<PriceMatrixItem>) => {
    setDraft((items) => items.map((item) => {
      if (item.draftId !== draftId) return item
      const next = { ...item, ...patch }
      return normalizePriceRule(
        next,
        scopeCode(next.limit_scope_id),
      )
    }))
    markChanged()
    setNotice('')
  }

  const addRule = (presetScope?: 'any' | 'codex') => {
    const targetScope = presetScope
      ? configurableScopes.find((scope) => scope.code.toLowerCase() === presetScope)?.id ?? 0
      : resolvedBuilderScope
    const targetScopeCode = scopeCode(targetScope)
    const price = Number(builder.price)
    const minLimit = presetScope === 'codex' ? 50 : Number(builder.minLimit)

    if (!resolvedBuilderTier || !resolvedBuilderDuration || !targetScope) {
      setError('Справочники тарифов, сроков или типов лимита пусты. Сначала проверьте каталог.')
      return
    }
    if (!Number.isInteger(price) || price <= 0) {
      setError('Укажите цену правила целым числом больше нуля.')
      return
    }
    if (targetScopeCode !== 'any' && (!Number.isFinite(minLimit) || minLimit < 0 || minLimit > 100)) {
      setError('Для CODEX укажите минимальный остаток от 0 до 100%.')
      return
    }

    const candidate = normalizePriceRule({
      draftId: nextDraftId(),
      tier_id: resolvedBuilderTier,
      duration_id: resolvedBuilderDuration,
      limit_scope_id: targetScope,
      min_limit_pct: targetScopeCode === 'any' ? undefined : minLimit,
      price,
    }, targetScopeCode)
    if (draft.some((item) => priceRuleSignature(
      item,
      scopeCode(item.limit_scope_id),
    ) === priceRuleSignature(candidate, targetScopeCode))) {
      setError('Такое правило уже есть. Измените тариф, срок или условие по лимиту.')
      return
    }

    setDraft((items) => [...items, candidate])
    markChanged()
    setNotice(presetScope ? 'Правило добавлено из готового пресета.' : 'Новое правило добавлено в черновик.')
  }

  const duplicateRule = (item: DraftRule) => {
    setDraft((items) => [...items, normalizePriceRule(
      { ...item, draftId: nextDraftId() },
      scopeCode(item.limit_scope_id),
    )])
    markChanged()
    setNotice('Копия добавлена. Измените у неё тариф, срок или условие перед сохранением.')
  }

  const removeRule = (draftId: string) => {
    setDraft((items) => items.filter((item) => item.draftId !== draftId))
    markChanged()
    setNotice('Правило удалено из черновика.')
  }

  const discard = () => {
    setDraft((pricesQuery.data ?? []).map((item) => normalizePriceRule(
      { ...item, draftId: nextDraftId() },
      scopeCode(item.limit_scope_id),
    )))
    setDirty(false)
    setError('')
    setNotice('')
    setSaved(false)
  }

  const save = async () => {
    setError('')
    setNotice('')
    const normalized = draft.map((item) => normalizePriceRule(
      item,
      scopeCode(item.limit_scope_id),
    ))
    const invalid = normalized.some((item) => {
      const code = scopeCode(item.limit_scope_id)
      return item.price <= 0
        || !isSupportedOfferScopeCode(code)
        || (code === 'any' && item.max_weekly_pct != null && (item.max_weekly_pct < 0 || item.max_weekly_pct > 100))
        || (code !== 'any' && (item.min_limit_pct == null || item.min_limit_pct < 0 || item.min_limit_pct > 100))
    })
    if (invalid) {
      setError('Проверьте цену и условие: поддерживаются только типы ANY и CODEX. ANY продаётся без гарантии, CODEX требует минимум 0–100%.')
      return
    }
    const signatures = normalized.map((item) => priceRuleSignature(
      item,
      scopeCode(item.limit_scope_id),
    ))
    if (new Set(signatures).size !== signatures.length) {
      setError('В матрице есть одинаковые правила. Измените или удалите дубликат перед сохранением.')
      return
    }

    try {
      await updatePrices.mutateAsync(normalized.map((item) => priceRuleToWire(
        item,
        scopeCode(item.limit_scope_id),
      )))
      setDraft(normalized)
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
        description="Стоимость аренды по тарифу, сроку и честно сформулированному условию по лимиту."
        actions={<div className="header-action-group"><button className="button button--secondary" onClick={discard} disabled={!dirty}>Отменить изменения</button><button className="button button--primary" onClick={save} disabled={!dirty || updatePrices.isPending}>{updatePrices.isPending ? <><span className="spinner spinner--light" />Сохраняем…</> : <><Icon name="check" />Сохранить всё</>}</button></div>}
      />

      <section className="summary-strip">
        <div><span>Ценовых правил</span><strong>{draft.length}</strong></div>
        <div><span>Тарифов в матрице</span><strong>{new Set(draft.map((item) => item.tier_id)).size}</strong></div>
        <div><span>Средняя цена</span><strong>{formatCurrency(averagePrice)}</strong></div>
        <div><span>Состояние</span><strong className={dirty ? 'text-warning' : 'text-success'}>{dirty ? 'Есть изменения' : 'Сохранено'}</strong></div>
      </section>

      {error && <div className="form-alert form-alert--error" role="alert"><Icon name="warning" /><span>{error}</span></div>}
      {notice && <div className="form-alert form-alert--info" role="status"><Icon name="activity" /><span>{notice}</span></div>}
      {saved && <div className="form-alert form-alert--success" role="status"><Icon name="check" /><span>Матрица сохранена, автоматические лоты отправлены на сверку.</span></div>}

      <section className="panel price-rule-builder" aria-labelledby="price-builder-title">
        <div className="price-rule-builder__head">
          <div><span className="eyebrow">Конструктор</span><h2 id="price-builder-title">Добавить ценовое правило</h2><p>ANY не обещает остаток лимита. Для CODEX задаётся минимум единственного проверенного длинного окна: 30 дней для Free, 7 дней для платных тарифов.</p></div>
        </div>
        <div className="price-rule-builder__fields">
          <label className="field"><span className="field__label">Тариф</span><select value={String(resolvedBuilderTier || '')} onChange={(event) => setBuilder((current) => ({ ...current, tierId: event.target.value }))} disabled={sellableTiers.length === 0}>{sellableTiers.length === 0 && <option value="">Нет тарифов, разрешённых к продаже</option>}{sellableTiers.map((tier) => <option value={tier.id} key={tier.id}>{tier.name}</option>)}</select></label>
          <label className="field"><span className="field__label">Срок аренды</span><select value={String(resolvedBuilderDuration || '')} onChange={(event) => setBuilder((current) => ({ ...current, durationId: event.target.value }))} disabled={enabledDurations.length === 0}>{enabledDurations.length === 0 && <option value="">Нет включённых сроков</option>}{enabledDurations.map((duration) => <option value={duration.id} key={duration.id}>{formatDurationMinutes(duration.minutes)}</option>)}</select></label>
          <label className="field"><span className="field__label">Тип условия</span><select value={String(resolvedBuilderScope || '')} onChange={(event) => setBuilder((current) => ({ ...current, scopeId: event.target.value }))} disabled={configurableScopes.length === 0}>{configurableScopes.length === 0 && <option value="">Нет включённых типов лимита</option>}{configurableScopes.map((scope) => <option value={scope.id} key={scope.id}>{scope.name}</option>)}</select></label>
          {builderScopeCode === 'any' ? <div className="builder-condition-note"><Icon name="check" /><span><strong>Без гарантии остатка</strong><small>Подойдёт любой доступный аккаунт выбранного тарифа.</small></span></div> : <label className="field"><span className="field__label">Минимальный остаток</span><span className="percent-input percent-input--wide"><input type="number" min="0" max="100" value={builder.minLimit} onChange={(event) => setBuilder((current) => ({ ...current, minLimit: event.target.value }))} /><span>%</span></span></label>}
          <label className="field"><span className="field__label">Цена</span><span className="money-input money-input--wide"><input type="number" min="1" step="1" value={builder.price} onChange={(event) => setBuilder((current) => ({ ...current, price: event.target.value }))} placeholder="0" /><span>₽</span></span></label>
          <button className="button button--primary price-rule-builder__add" type="button" onClick={() => addRule()} disabled={sellableTiers.length === 0 || enabledDurations.length === 0 || configurableScopes.length === 0}><Icon name="plus" />Добавить правило</button>
        </div>
        <div className="price-presets" aria-label="Быстрые пресеты">
          <span>Быстрые пресеты для выбранного тарифа и срока:</span>
          <button type="button" onClick={() => addRule('any')} disabled={!configurableScopes.some((scope) => scope.code.toLowerCase() === 'any')}>ANY · без гарантии</button>
          <button type="button" onClick={() => addRule('codex')} disabled={!configurableScopes.some((scope) => scope.code.toLowerCase() === 'codex')}>CODEX · остаток ≥ 50%</button>
        </div>
      </section>

      <div className="form-alert form-alert--info"><Icon name="activity" /><span>Для обычного доступа используйте ANY без обещания остатка. CODEX проверяет только длинный лимит тарифа: 30 дней для Free или 7 дней для платных тарифов.</span></div>
      {unavailableTierRules > 0 && <div className="form-alert form-alert--warning"><Icon name="warning" /><span>Правил для тарифов с выключенной продажей: {unavailableTierRules}. Они остаются видимыми и сохраняются в матрице, но новые правила и автоматические лоты для таких тарифов не создаются. Правило можно перевести на доступный тариф или удалить.</span></div>}
      {unavailableDurationRules > 0 && <div className="form-alert form-alert--warning"><Icon name="warning" /><span>Правил для выключенных сроков: {unavailableDurationRules}. Они сохранены, но соответствующие автоматические лоты приостановлены. Выберите включённый срок или удалите правило.</span></div>}
      {unavailableScopeRules > 0 && <div className="form-alert form-alert--warning"><Icon name="warning" /><span>Правил с недоступным типом лимита: {unavailableScopeRules}. Они остаются в матрице, но не участвуют в создании новых лотов. Переведите правило на включённый ANY/CODEX или удалите его.</span></div>}

      <section className="panel panel--flush">
        <div className="toolbar">
          <div className="toolbar__title"><strong>Правила продаж</strong><span>После сохранения автоматические лоты сверяются с этой матрицей; ручные лоты остаются отдельными.</span></div>
          <label className="select-field"><span>Тариф</span><select value={tierFilter} onChange={(event) => setTierFilter(event.target.value)}><option value="all">Все тарифы</option>{tiers.map((tier) => <option value={tier.id} key={tier.id}>{tier.name}</option>)}</select><Icon name="chevron-down" size={15} /></label>
        </div>

        {draft.length === 0 ? <EmptyState icon="prices" title="Матрица цен пуста" description="Создайте первое правило в конструкторе выше: выберите тариф, срок, условие и цену." /> : visibleDraft.length === 0 ? <EmptyState icon="search" title="Для этого тарифа правил нет" description="Смените фильтр или добавьте первое правило для выбранного тарифа." /> : (
          <TableShell><table className="data-table pricing-table"><thead><tr><th>Тариф</th><th>Срок</th><th>Тип лимита</th><th>Условие выдачи</th><th>Цена</th><th><span className="sr-only">Действия</span></th></tr></thead><tbody>{visibleDraft.map((item) => {
            const scope = scopeCode(item.limit_scope_id)
            const currentTier = tiers.find((tier) => tier.id === item.tier_id)
            const currentDuration = durations.find((duration) => duration.id === item.duration_id)
            const currentScope = scopes.find((candidate) => candidate.id === item.limit_scope_id)
            const currentTierIsSellable = isTierSellable(currentTier)
            const rowTierOptions = currentTier && !sellableTiers.some((tier) => tier.id === currentTier.id)
              ? [currentTier, ...sellableTiers]
              : sellableTiers
            const rowDurationOptions = (currentDuration && !enabledDurations.some((duration) => duration.id === currentDuration.id)
              ? [currentDuration, ...enabledDurations]
              : [...enabledDurations])
              .sort(compareDurationsByMinutes)
            const rowScopeOptions = currentScope && !configurableScopes.some((candidate) => candidate.id === currentScope.id)
              ? [currentScope, ...configurableScopes]
              : configurableScopes
            const scopeUnavailableReason = offerScopeUnavailableReason(currentScope)
            const currentScopeIsEnabled = scopeUnavailableReason === null
            return <tr key={item.draftId}>
              <td><label className="sr-only" htmlFor={`${item.draftId}-tier`}>Тариф</label><select id={`${item.draftId}-tier`} className="table-select" value={item.tier_id} onChange={(event) => updateItem(item.draftId, { tier_id: Number(event.target.value) })}>{!currentTier && <option value={item.tier_id}>Тариф #{item.tier_id} · нет в каталоге</option>}{rowTierOptions.map((tier) => <option key={tier.id} value={tier.id} disabled={!isTierSellable(tier)}>{tier.name}{isTierSellable(tier) ? '' : ' · продажа выключена'}</option>)}</select>{!currentTierIsSellable && <small className="table-subline text-warning">Правило сохранено, публикация приостановлена</small>}</td>
              <td><label className="sr-only" htmlFor={`${item.draftId}-duration`}>Срок</label><select id={`${item.draftId}-duration`} className="table-select table-select--compact" value={item.duration_id} onChange={(event) => updateItem(item.draftId, { duration_id: Number(event.target.value) })}>{!currentDuration && <option value={item.duration_id}>Срок #{item.duration_id} · нет в каталоге</option>}{rowDurationOptions.map((duration) => <option key={duration.id} value={duration.id} disabled={!duration.is_enabled}>{formatDurationMinutes(duration.minutes)}{duration.is_enabled ? '' : ' · выключен'}</option>)}</select>{currentDuration?.is_enabled !== true && <small className="table-subline text-warning">Публикация приостановлена</small>}</td>
              <td><label className="sr-only" htmlFor={`${item.draftId}-scope`}>Тип лимита</label><select id={`${item.draftId}-scope`} className={`table-select scope-select scope-select--${scope}`} value={item.limit_scope_id} onChange={(event) => updateItem(item.draftId, { limit_scope_id: Number(event.target.value) })}>{!currentScope && <option value={item.limit_scope_id}>Тип #{item.limit_scope_id} · нет в каталоге</option>}{rowScopeOptions.map((candidate) => <option key={candidate.id} value={candidate.id} disabled={!isAvailableOfferScope(candidate)}>{offerScopeDisplayCode(candidate)}{offerScopeUnavailableReason(candidate) ? ` · ${offerScopeUnavailableReason(candidate)}` : ''}</option>)}</select><small className={`table-subline ${currentScopeIsEnabled ? '' : 'text-warning'}`}>{currentScopeIsEnabled ? scopeName(item.limit_scope_id) : `${scopeUnavailableReason}. Правило сохранено, публикация приостановлена`}</small></td>
              <td>{!currentScopeIsEnabled ? <div className="limit-condition limit-condition--unavailable"><Icon name="warning" size={15} /><span><strong>Тип лимита недоступен</strong>{scopeUnavailableReason}. Переведите правило на ANY/CODEX или удалите.</span></div> : scope === 'any' ? <div className="limit-condition limit-condition--ceilings"><span><strong className="no-guarantee"><Icon name="check" size={14} />Без гарантии</strong>Необязательный внутренний максимум длинного лимита Codex</span><PercentInput value={item.max_weekly_pct} label="Максимум длинного окна (7 или 30 дней)" onChange={(value) => updateItem(item.draftId, { max_weekly_pct: value })} /></div> : <div className="limit-condition"><span>Длинный лимит Codex не ниже</span><PercentInput value={item.min_limit_pct} label="Минимальный остаток длинного лимита Codex" onChange={(value) => updateItem(item.draftId, { min_limit_pct: value })} /></div>}</td>
              <td><label className="money-input"><input type="number" min="1" step="1" value={item.price} onChange={(event) => updateItem(item.draftId, { price: Number(event.target.value) })} aria-label={`Цена ${tierName(item.tier_id)}, срок ${durationLabel(item.duration_id)}`} /><span>₽</span></label></td>
              <td><div className="row-actions row-actions--compact"><button className="icon-button" type="button" onClick={() => duplicateRule(item)} disabled={!currentTierIsSellable || !currentScopeIsEnabled} title={currentTierIsSellable && currentScopeIsEnabled ? 'Дублировать правило' : 'Недоступное правило нельзя дублировать'} aria-label={`Дублировать правило ${tierName(item.tier_id)}`}><Icon name="copy" /></button><button className="icon-button icon-button--danger" type="button" onClick={() => removeRule(item.draftId)} title="Удалить правило" aria-label={`Удалить правило ${tierName(item.tier_id)}`}><Icon name="trash" /></button></div></td>
            </tr>
          })}</tbody></table></TableShell>
        )}
      </section>
      {dirty && <div className="unsaved-bar"><span><span className="status-dot status-dot--warning" />Есть несохранённые изменения</span><div><button className="button button--ghost" onClick={discard}>Отменить</button><button className="button button--primary" onClick={save} disabled={updatePrices.isPending}>Сохранить всё</button></div></div>}
    </div>
  )
}

function isTierSellable(tier: Tier | undefined) {
  return tier?.is_active === true && tier.is_sellable !== false
}

function PercentInput({ value, label, onChange }: { value?: number | null; label: string; onChange: (value: number | undefined) => void }) {
  return <label className="percent-input"><input type="number" min="0" max="100" value={value ?? ''} onChange={(event) => onChange(event.target.value === '' ? undefined : Number(event.target.value))} aria-label={label} /><span>%</span></label>
}
