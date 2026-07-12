import { useMemo, useState } from 'react'
import { useDurations, useLimitScopes, useTiers } from '../api/catalog'
import { ApiError } from '../api/client'
import { useDeleteLot, useLots } from '../api/lots'
import { Icon } from '../components/Icon'
import { EmptyState, ErrorState, LoadingState, PageHeader, StatusBadge, TableShell } from '../components/ui'
import type { Lot } from '../types/api'
import { formatCurrency } from '../utils/format'

export default function Lots() {
  const lotsQuery = useLots()
  const tiersQuery = useTiers()
  const durationsQuery = useDurations()
  const scopesQuery = useLimitScopes()
  const deleteLot = useDeleteLot()
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState('all')
  const [deleteTarget, setDeleteTarget] = useState<Lot | null>(null)
  const [error, setError] = useState('')

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

  if (lotsQuery.isLoading) return <LoadingState label="Синхронизируем список лотов" />
  if (lotsQuery.isError) return <ErrorState onRetry={() => lotsQuery.refetch()} />

  const tierName = (id: number) => tiers.find((tier) => tier.id === id)?.name ?? `Тариф #${id}`
  const durationDays = (id: number) => durations.find((duration) => duration.id === id)?.days
  const scopeName = (id: number) => scopes.find((scope) => scope.id === id)?.code ?? 'unknown'
  const active = lots.filter((lot) => lot.status === 'active').length

  const remove = async () => {
    if (!deleteTarget) return
    setError('')
    try {
      await deleteLot.mutateAsync(deleteTarget.id)
      setDeleteTarget(null)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Не удалось удалить лот')
      setDeleteTarget(null)
    }
  }

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Витрина FunPay"
        title="Лоты"
        description="Предложения, опубликованные вручную или созданные автоматикой по матрице цен."
        actions={<button className="button button--primary" disabled title="Создание ручного лота ещё не подключено к интерфейсу"><Icon name="plus" />Новый лот</button>}
      />

      <section className="summary-strip">
        <div><span>Всего</span><strong>{lots.length}</strong></div>
        <div><span className="summary-dot summary-dot--success" /><span>Активны</span><strong>{active}</strong></div>
        <div><span className="summary-dot summary-dot--warning" /><span>На паузе</span><strong>{lots.filter((lot) => lot.status === 'paused').length}</strong></div>
        <div><span>Автоматические</span><strong>{lots.filter((lot) => lot.auto_created).length}</strong></div>
      </section>

      {error && <div className="form-alert form-alert--error"><Icon name="warning" /><span>{error}</span></div>}
      <div className="form-alert form-alert--info"><Icon name="activity" /><span>Пауза, активация и поднятие категории предусмотрены спецификацией, но соответствующие API-действия пока не реализованы полностью.</span></div>

      <section className="panel panel--flush">
        <div className="toolbar">
          <label className="search-field"><Icon name="search" /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Название или FunPay ID" aria-label="Поиск лотов" /></label>
          <label className="select-field"><span>Статус</span><select value={status} onChange={(event) => setStatus(event.target.value)}><option value="all">Все статусы</option><option value="active">Активные</option><option value="paused">На паузе</option><option value="deleted">Удалённые</option></select><Icon name="chevron-down" size={15} /></label>
          <span className="toolbar__count">Показано: {filteredLots.length}</span>
        </div>

        {lots.length === 0 ? <EmptyState icon="lots" title="Лотов пока нет" description="После настройки FunPay, пула аккаунтов и матрицы цен автоматические лоты появятся здесь." /> : filteredLots.length === 0 ? <EmptyState icon="search" title="Лоты не найдены" description="Измените фильтры или строку поиска." /> : (
          <TableShell>
            <table className="data-table lots-table">
              <thead><tr><th>Предложение</th><th>Конфигурация</th><th>Требование</th><th>Цена</th><th>FunPay</th><th>Статус</th><th><span className="sr-only">Действия</span></th></tr></thead>
              <tbody>{filteredLots.map((lot) => {
                const scope = scopeName(lot.limit_scope_id)
                const threshold = scope === 'any'
                  ? `5ч ≤ ${lot.max_5h_pct ?? '—'}% · нед. ≤ ${lot.max_weekly_pct ?? '—'}%`
                  : `остаток ≥ ${lot.min_limit_pct ?? '—'}%`
                return (
                  <tr key={lot.id}>
                    <td><div className="lot-title-cell"><strong>{lot.title_ru}</strong><small>{lot.auto_created ? 'Автоматический лот' : 'Ручной лот'} · ID {lot.id}</small></div></td>
                    <td><strong>{tierName(lot.tier_id)}</strong><small className="table-subline">{durationDays(lot.duration_id) ?? '?'} дн. · {scope.toUpperCase()}</small></td>
                    <td>{threshold}</td>
                    <td className="table-number">{formatCurrency(lot.price)}</td>
                    <td>{lot.funpay_id ? <span className="mono-chip">#{lot.funpay_id}</span> : <span className="muted">Не опубликован</span>}</td>
                    <td><StatusBadge value={lot.status} /></td>
                    <td><div className="row-actions"><button className="icon-button" disabled title="API паузы/активации требует доработки" aria-label="Пауза или активация"><Icon name={lot.status === 'active' ? 'clock' : 'refresh'} /></button><button className="icon-button icon-button--danger" disabled={lot.auto_created} onClick={() => setDeleteTarget(lot)} title={lot.auto_created ? 'Автоматические лоты управляются матрицей цен' : 'Удалить'} aria-label={`Удалить лот ${lot.title_ru}`}><Icon name="trash" /></button></div></td>
                  </tr>
                )
              })}</tbody>
            </table>
          </TableShell>
        )}
      </section>

      {deleteTarget && <div className="modal-overlay" role="presentation"><div className="modal modal--compact" role="alertdialog" aria-modal="true"><div className="modal__danger-icon"><Icon name="trash" /></div><h2>Удалить ручной лот?</h2><p>Лот «{deleteTarget.title_ru}» будет удалён из панели. Проверьте, что на FunPay он также снят с публикации.</p><div className="modal__actions"><button className="button button--secondary" onClick={() => setDeleteTarget(null)}>Отмена</button><button className="button button--danger" onClick={remove} disabled={deleteLot.isPending}>{deleteLot.isPending ? 'Удаляем…' : 'Удалить'}</button></div></div></div>}
    </div>
  )
}
