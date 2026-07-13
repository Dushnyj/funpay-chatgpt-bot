import { useMemo, useState } from 'react'
import { useDurations, useLimitScopes, useTiers } from '../api/catalog'
import { ApiError } from '../api/client'
import { useCreateLot, useDeleteLot, useLots, useSyncLots, useUpdateLotStatus } from '../api/lots'
import { useSettings } from '../api/settings'
import { Icon } from '../components/Icon'
import { EmptyState, ErrorState, LoadingState, ModalOverlay, PageHeader, StatusBadge, TableShell } from '../components/ui'
import type { Duration, LimitScope, Lot, LotCreate, Tier } from '../types/api'
import { compareDurationsByMinutes, formatDurationMinutes } from '../utils/catalogEditor'
import { formatCurrency } from '../utils/format'
import { getLotCatalogAvailability } from '../utils/lotAvailability'
import { compareOfferScopes, isAvailableOfferScope, offerScopeDisplayCode } from '../utils/offerScopes'

export default function Lots() {
  const lotsQuery = useLots()
  const tiersQuery = useTiers()
  const durationsQuery = useDurations()
  const scopesQuery = useLimitScopes()
  const settingsQuery = useSettings()
  const deleteLot = useDeleteLot()
  const updateStatus = useUpdateLotStatus()
  const syncLots = useSyncLots()
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState('all')
  const [deleteTarget, setDeleteTarget] = useState<Lot | null>(null)
  const [showCreate, setShowCreate] = useState(false)
  const [statusTarget, setStatusTarget] = useState<number | null>(null)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  const lots = useMemo(() => lotsQuery.data ?? [], [lotsQuery.data])
  const tiers = tiersQuery.data ?? []
  const durations = durationsQuery.data ?? []
  const scopes = scopesQuery.data ?? []
  const filteredLots = useMemo(() => {
    const query = search.trim().toLowerCase()
    return lots.filter((lot) => {
      const title = `${lot.title_ru} ${lot.title_en} ${lot.funpay_id ?? ''}`.toLowerCase()
      return (!query || title.includes(query)) && (status === 'all' || lot.status === status)
    })
  }, [lots, search, status])

  if (lotsQuery.isLoading || tiersQuery.isLoading || durationsQuery.isLoading || scopesQuery.isLoading) {
    return <LoadingState label="Синхронизируем список лотов" />
  }
  if (lotsQuery.isError || tiersQuery.isError || durationsQuery.isError || scopesQuery.isError) {
    return <ErrorState onRetry={() => void Promise.all([
      lotsQuery.refetch(), tiersQuery.refetch(), durationsQuery.refetch(), scopesQuery.refetch(),
    ])} />
  }

  const tierName = (id: number) => tiers.find((tier) => tier.id === id)?.name ?? `Тариф #${id}`
  const durationLabel = (id: number) => {
    const duration = durations.find((candidate) => candidate.id === id)
    return duration ? formatDurationMinutes(duration.minutes) : 'неизвестный срок'
  }
  const active = lots.filter((lot) => lot.status === 'active').length
  const unavailableCatalogLots = lots.filter(
    (lot) => !getLotCatalogAvailability(lot, tiers, durations, scopes).available,
  ).length

  const clearFeedback = () => {
    setError('')
    setSuccess('')
  }

  const remove = async () => {
    if (!deleteTarget) return
    clearFeedback()
    try {
      await deleteLot.mutateAsync(deleteTarget.id)
      setSuccess('Ручной лот удалён из панели и снят с публикации на FunPay.')
      setDeleteTarget(null)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Не удалось удалить лот')
      setDeleteTarget(null)
    }
  }

  const toggleStatus = async (lot: Lot) => {
    clearFeedback()
    setStatusTarget(lot.id)
    const nextStatus = lot.status === 'active' ? 'paused' : 'active'
    try {
      if (nextStatus === 'active') {
        const [latestLots, latestTiers, latestDurations, latestScopes] = await Promise.all([
          lotsQuery.refetch(),
          tiersQuery.refetch(),
          durationsQuery.refetch(),
          scopesQuery.refetch(),
        ])
        if (latestLots.isError || latestTiers.isError || latestDurations.isError || latestScopes.isError) {
          setError('Не удалось проверить актуальную конфигурацию лота. Обновите страницу и повторите попытку.')
          return
        }
        const latestLot = latestLots.data?.find((item) => item.id === lot.id)
        if (!latestLot) {
          setError('Лот больше не существует. Список обновлён.')
          return
        }
        if (latestLot.status === 'active') {
          setSuccess('Лот уже активирован в другой вкладке. Список обновлён.')
          return
        }
        if (latestLot.status !== 'paused') {
          setError(`Лот нельзя активировать из статуса «${latestLot.status}». Список обновлён.`)
          return
        }
        const availability = getLotCatalogAvailability(
          latestLot,
          latestTiers.data ?? [],
          latestDurations.data ?? [],
          latestScopes.data ?? [],
        )
        if (!availability.available) {
          setError(`Лот не активирован: ${availability.reasons.join('; ')}. Исправьте справочники и повторите попытку.`)
          return
        }
      }
      await updateStatus.mutateAsync({ id: lot.id, status: nextStatus })
      setSuccess(nextStatus === 'active' ? 'Лот активирован.' : 'Лот поставлен на паузу.')
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Не удалось изменить статус лота')
    } finally {
      setStatusTarget(null)
    }
  }

  const synchronize = async () => {
    clearFeedback()
    try {
      await syncLots.mutateAsync()
      setSuccess('Сверка завершена: список и статусы лотов обновлены по матрице цен и FunPay.')
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Не удалось синхронизировать лоты')
    }
  }

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Витрина FunPay"
        title="Лоты"
        description="Предложения, опубликованные вручную или созданные автоматикой по матрице цен."
        actions={<div className="header-action-group"><button className="button button--secondary" onClick={synchronize} disabled={syncLots.isPending}>{syncLots.isPending ? <span className="spinner" /> : <Icon name="refresh" />}Синхронизировать</button><button className="button button--primary" onClick={() => setShowCreate(true)}><Icon name="plus" />Новый лот</button></div>}
      />

      <section className="summary-strip">
        <div><span>Всего</span><strong>{lots.length}</strong></div>
        <div><span className="summary-dot summary-dot--success" /><span>Активны</span><strong>{active}</strong></div>
        <div><span className="summary-dot summary-dot--warning" /><span>На паузе</span><strong>{lots.filter((lot) => lot.status === 'paused').length}</strong></div>
        <div><span>Автоматические</span><strong>{lots.filter((lot) => lot.auto_created).length}</strong></div>
      </section>

      {error && <div className="form-alert form-alert--error" role="alert"><Icon name="warning" /><span>{error}</span></div>}
      {success && <div className="form-alert form-alert--success" role="status"><Icon name="check" /><span>{success}</span></div>}
      <div className="form-alert form-alert--info"><Icon name="activity" /><span>Автоматические лоты следуют матрице цен. Ручные можно создавать отдельно, ставить на паузу и активировать; удаление автоматических правил выполняется через матрицу.</span></div>
      {unavailableCatalogLots > 0 && <div className="form-alert form-alert--warning"><Icon name="warning" /><span>Лотов с недоступной конфигурацией: {unavailableCatalogLots}. Они сохранены, но активация заблокирована. Конкретная причина указана в строке каждого лота.</span></div>}

      <section className="panel panel--flush">
        <div className="toolbar">
          <label className="search-field"><Icon name="search" /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Название или FunPay ID" aria-label="Поиск лотов" /></label>
          <label className="select-field"><span>Статус</span><select value={status} onChange={(event) => setStatus(event.target.value)}><option value="all">Все статусы</option><option value="active">Активные</option><option value="paused">На паузе</option><option value="deleted">Удалённые</option></select><Icon name="chevron-down" size={15} /></label>
          <span className="toolbar__count">Показано: {filteredLots.length}</span>
        </div>

        {lots.length === 0 ? <EmptyState icon="lots" title="Лотов пока нет" description="Создайте ручной лот или настройте матрицу цен и запустите синхронизацию автоматической витрины." action={<button className="button button--primary" onClick={() => setShowCreate(true)}><Icon name="plus" />Создать первый лот</button>} /> : filteredLots.length === 0 ? <EmptyState icon="search" title="Лоты не найдены" description="Измените фильтры или строку поиска." /> : (
          <TableShell>
            <table className="data-table lots-table">
              <thead><tr><th>Предложение</th><th>Конфигурация</th><th>Условие выдачи</th><th>Цена</th><th>FunPay</th><th>Статус</th><th><span className="sr-only">Действия</span></th></tr></thead>
              <tbody>{filteredLots.map((lot) => {
                const scopeItem = scopes.find((candidate) => candidate.id === lot.limit_scope_id)
                const scope = scopeItem?.code.toLowerCase() ?? 'unknown'
                const scopeLabel = offerScopeDisplayCode(scopeItem)
                const availability = getLotCatalogAvailability(lot, tiers, durations, scopes)
                const threshold = scope === 'any'
                  ? 'Без гарантии остатка лимита'
                  : scope === 'codex'
                    ? `Codex: остаток в наблюдаемом окне ≥ ${lot.min_limit_pct ?? '—'}%`
                    : 'Устаревшее условие недоступно'
                const canToggle = lot.status === 'active' || (lot.status === 'paused' && availability.available)
                const activationBlockReason = availability.reasons.join('; ')
                const availabilityDescriptionId = `lot-${lot.id}-availability`
                const toggleTitle = canToggle
                  ? (lot.status === 'active' ? 'Поставить на паузу' : 'Активировать')
                  : lot.status === 'paused' && activationBlockReason
                    ? `Нельзя активировать: ${activationBlockReason}`
                    : 'Статус этого лота изменить нельзя'
                return (
                  <tr key={lot.id}>
                    <td><div className="lot-title-cell"><strong>{lot.title_ru}</strong><small>{lot.auto_created ? 'Автоматический лот' : 'Ручной лот'} · ID {lot.id}</small></div></td>
                    <td><strong>{tierName(lot.tier_id)}</strong><small className="table-subline">{durationLabel(lot.duration_id)} · {scopeLabel}</small>{!availability.available && <small id={availabilityDescriptionId} className="table-subline text-warning">{availability.reasons.join(' · ')}</small>}</td>
                    <td>{threshold}</td>
                    <td className="table-number">{formatCurrency(lot.price)}</td>
                    <td>{lot.funpay_id ? <span className="mono-chip">#{lot.funpay_id}</span> : <span className="muted">Не опубликован</span>}</td>
                    <td><StatusBadge value={lot.status} /></td>
                    <td><div className="row-actions"><button className="icon-button" disabled={!canToggle || statusTarget === lot.id} onClick={() => toggleStatus(lot)} title={toggleTitle} aria-label={lot.status === 'active' ? `Поставить на паузу ${lot.title_ru}` : `Активировать ${lot.title_ru}`} aria-describedby={!availability.available ? availabilityDescriptionId : undefined}>{statusTarget === lot.id ? <span className="spinner" /> : <Icon name={lot.status === 'active' ? 'clock' : 'refresh'} />}</button><button className="icon-button icon-button--danger" disabled={lot.auto_created || lot.status === 'deleted'} onClick={() => setDeleteTarget(lot)} title={lot.auto_created ? 'Автоматические лоты управляются матрицей цен' : 'Удалить'} aria-label={`Удалить лот ${lot.title_ru}`}><Icon name="trash" /></button></div></td>
                  </tr>
                )
              })}</tbody>
            </table>
          </TableShell>
        )}
      </section>

      {showCreate && <ManualLotDialog tiers={tiers} durations={durations} scopes={scopes} defaultNodeId={settingsQuery.data?.funpay_node_id ?? undefined} onClose={() => setShowCreate(false)} onCreated={() => { setShowCreate(false); setSuccess('Ручной лот создан и опубликован на FunPay.') }} />}
      {deleteTarget && <ModalOverlay onClose={() => setDeleteTarget(null)}><div className="modal modal--compact" role="alertdialog" aria-modal="true" aria-labelledby="delete-lot-title"><div className="modal__danger-icon"><Icon name="trash" /></div><h2 id="delete-lot-title">Удалить ручной лот?</h2><p>Лот «{deleteTarget.title_ru}» будет удалён из панели и снят с публикации на FunPay.</p><div className="modal__actions"><button className="button button--secondary" onClick={() => setDeleteTarget(null)}>Отмена</button><button className="button button--danger" onClick={remove} disabled={deleteLot.isPending}>{deleteLot.isPending ? 'Удаляем…' : 'Удалить'}</button></div></div></ModalOverlay>}
    </div>
  )
}

function ManualLotDialog({
  tiers,
  durations,
  scopes,
  defaultNodeId,
  onClose,
  onCreated,
}: {
  tiers: Tier[]
  durations: Duration[]
  scopes: LimitScope[]
  defaultNodeId?: number
  onClose: () => void
  onCreated: () => void
}) {
  const createLot = useCreateLot()
  const availableTiers = tiers.filter((tier) => tier.is_active && tier.is_sellable !== false)
  const availableDurations = durations.filter((duration) => duration.is_enabled).sort(compareDurationsByMinutes)
  const availableScopes = scopes
    .filter(isAvailableOfferScope)
    .sort(compareOfferScopes)
  const defaultScope = availableScopes.find((scope) => scope.code.toLowerCase() === 'any') ?? availableScopes[0]
  const [error, setError] = useState('')
  const [form, setForm] = useState({
    tierId: String(availableTiers[0]?.id ?? ''),
    durationId: String(availableDurations[0]?.id ?? ''),
    scopeId: String(defaultScope?.id ?? ''),
    minLimit: '50',
    price: '',
    nodeId: defaultNodeId ? String(defaultNodeId) : '',
    titleRu: '',
    titleEn: '',
    descriptionRu: '',
    descriptionEn: '',
  })
  const selectedScope = availableScopes.find((scope) => scope.id === Number(form.scopeId))?.code.toLowerCase() ?? 'unknown'

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setError('')
    const price = Number(form.price)
    const minLimit = form.minLimit === '' ? Number.NaN : Number(form.minLimit)
    if (!Number(form.tierId) || !Number(form.durationId) || !Number(form.scopeId)) {
      setError('Выберите тариф, срок и тип условия.')
      return
    }
    if (!Number.isInteger(price) || price <= 0) {
      setError('Укажите цену целым числом больше нуля.')
      return
    }
    if (selectedScope === 'codex' && (!Number.isFinite(minLimit) || minLimit < 0 || minLimit > 100)) {
      setError('Для CODEX нужен минимальный остаток от 0 до 100%.')
      return
    }

    const payload: LotCreate = {
      tier_id: Number(form.tierId),
      duration_id: Number(form.durationId),
      limit_scope_id: Number(form.scopeId),
      min_limit_pct: selectedScope === 'codex' ? minLimit : undefined,
      price,
      title_ru: form.titleRu.trim(),
      title_en: form.titleEn.trim(),
      description_ru: form.descriptionRu.trim(),
      description_en: form.descriptionEn.trim(),
      funpay_node_id: form.nodeId ? Number(form.nodeId) : undefined,
    }
    try {
      await createLot.mutateAsync(payload)
      onCreated()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Не удалось создать ручной лот')
    }
  }

  return (
    <ModalOverlay onClose={onClose}>
      <div className="modal modal--wide manual-lot-dialog" role="dialog" aria-modal="true" aria-labelledby="manual-lot-title">
        <div className="modal__header"><div><span className="eyebrow">Ручная витрина</span><h2 id="manual-lot-title">Новый лот</h2><p>Лот сохранится отдельно от автоматической матрицы цен.</p></div><button className="icon-button" onClick={onClose} aria-label="Закрыть"><Icon name="close" /></button></div>
        <form className="form-stack" onSubmit={submit}>
          {error && <div className="form-alert form-alert--error" role="alert"><Icon name="warning" /><span>{error}</span></div>}
          <div className="form-alert form-alert--info"><Icon name="activity" /><span>ANY выдаёт доступ без обещания остатка. CODEX гарантирует минимальный остаток единого измеримого лимита по фактическим окнам OpenAI.</span></div>
          <div className="form-grid form-grid--3">
            <label className="field"><span className="field__label">Тариф</span><select data-autofocus value={form.tierId} onChange={(event) => setForm((current) => ({ ...current, tierId: event.target.value }))} required disabled={availableTiers.length === 0}>{availableTiers.length === 0 && <option value="">Нет тарифов, разрешённых к продаже</option>}{availableTiers.map((tier) => <option key={tier.id} value={tier.id}>{tier.name}</option>)}</select></label>
            <label className="field"><span className="field__label">Срок</span><select value={form.durationId} onChange={(event) => setForm((current) => ({ ...current, durationId: event.target.value }))} required disabled={availableDurations.length === 0}>{availableDurations.length === 0 && <option value="">Нет включённых сроков</option>}{availableDurations.map((duration) => <option key={duration.id} value={duration.id}>{formatDurationMinutes(duration.minutes)}</option>)}</select></label>
            <label className="field"><span className="field__label">Условие лимита</span><select value={form.scopeId} onChange={(event) => setForm((current) => ({ ...current, scopeId: event.target.value }))} required disabled={availableScopes.length === 0}>{availableScopes.length === 0 && <option value="">Нет включённых типов лимита</option>}{availableScopes.map((scope) => <option key={scope.id} value={scope.id}>{scope.name}</option>)}</select></label>
          </div>
          <div className="form-grid form-grid--3">
            {selectedScope === 'codex' ? <label className="field"><span className="field__label">Минимальный остаток CODEX</span><div className="number-with-suffix"><input type="number" min="0" max="100" value={form.minLimit} onChange={(event) => setForm((current) => ({ ...current, minLimit: event.target.value }))} required /><span>%</span></div><span className="field__hint">Остаток в наблюдаемом окне OpenAI.</span></label> : <div className="builder-condition-note"><Icon name="check" /><span><strong>Без гарантии остатка</strong><small>Для ANY порог лимита не задаётся.</small></span></div>}
            <label className="field"><span className="field__label">Цена</span><div className="number-with-suffix"><input type="number" min="1" step="1" value={form.price} onChange={(event) => setForm((current) => ({ ...current, price: event.target.value }))} required /><span>₽</span></div></label>
            <label className="field"><span className="field__label">FunPay Node ID</span><input type="number" min="1" value={form.nodeId} onChange={(event) => setForm((current) => ({ ...current, nodeId: event.target.value }))} placeholder="Из настроек" /><span className="field__hint">Можно оставить пустым, если категория задана в настройках.</span></label>
          </div>
          <div className="form-grid">
            <label className="field"><span className="field__label">Название на русском</span><input value={form.titleRu} onChange={(event) => setForm((current) => ({ ...current, titleRu: event.target.value }))} maxLength={255} required /></label>
            <label className="field"><span className="field__label">Название на английском</span><input value={form.titleEn} onChange={(event) => setForm((current) => ({ ...current, titleEn: event.target.value }))} maxLength={255} required /></label>
          </div>
          <div className="form-grid">
            <label className="field"><span className="field__label">Описание на русском</span><textarea value={form.descriptionRu} onChange={(event) => setForm((current) => ({ ...current, descriptionRu: event.target.value }))} maxLength={4000} /></label>
            <label className="field"><span className="field__label">Описание на английском</span><textarea value={form.descriptionEn} onChange={(event) => setForm((current) => ({ ...current, descriptionEn: event.target.value }))} maxLength={4000} /></label>
          </div>
          <div className="modal__actions"><button className="button button--secondary" type="button" onClick={onClose}>Отмена</button><button className="button button--primary" type="submit" disabled={createLot.isPending || availableTiers.length === 0 || availableDurations.length === 0 || availableScopes.length === 0}>{createLot.isPending ? <><span className="spinner spinner--light" />Создаём…</> : <><Icon name="plus" />Создать лот</>}</button></div>
        </form>
      </div>
    </ModalOverlay>
  )
}
