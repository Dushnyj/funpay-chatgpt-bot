import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useOrders } from '../api/orders'
import { useRentals, useRetryRentalDelivery } from '../api/rentals'
import { Icon } from '../components/Icon'
import { EmptyState, ErrorState, LoadingState, PageHeader, StatusBadge, TableShell } from '../components/ui'
import { formatCurrency, formatDateTime } from '../utils/format'
import type { Rental } from '../types/api'

type DealTab = 'orders' | 'rentals'

export default function Orders() {
  const [tab, setTab] = useState<DealTab>('orders')
  const ordersQuery = useOrders()
  const rentalsQuery = useRentals()
  const activeRentals = rentalsQuery.data?.filter((rental) => rental.status === 'active').length ?? 0
  const completedOrders = ordersQuery.data?.filter((order) => order.status === 'completed').length ?? 0

  return (
    <div className="page-stack">
      <PageHeader eyebrow="Операции" title="Сделки" description="Заказы FunPay и созданные на их основе аренды аккаунтов." />
      <section className="summary-strip">
        <div><span>Заказов всего</span><strong>{ordersQuery.data?.length ?? '—'}</strong></div>
        <div><span className="summary-dot summary-dot--success" /><span>Завершены</span><strong>{completedOrders}</strong></div>
        <div><span>Аренд всего</span><strong>{rentalsQuery.data?.length ?? '—'}</strong></div>
        <div><span className="summary-dot summary-dot--success" /><span>Активны сейчас</span><strong>{activeRentals}</strong></div>
      </section>
      <div className="content-tabs" role="tablist" aria-label="Тип сделки">
        <button role="tab" aria-selected={tab === 'orders'} className={tab === 'orders' ? 'active' : ''} onClick={() => setTab('orders')}><Icon name="deals" />Заказы<span>{ordersQuery.data?.length ?? 0}</span></button>
        <button role="tab" aria-selected={tab === 'rentals'} className={tab === 'rentals' ? 'active' : ''} onClick={() => setTab('rentals')}><Icon name="clock" />Аренды<span>{rentalsQuery.data?.length ?? 0}</span></button>
      </div>
      {tab === 'orders' ? <OrdersTab query={ordersQuery} /> : <RentalsTab query={rentalsQuery} />}
    </div>
  )
}

function OrdersTab({ query }: { query: ReturnType<typeof useOrders> }) {
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState('all')
  const orders = useMemo(() => query.data ?? [], [query.data])
  const filtered = useMemo(() => {
    const text = search.trim().toLowerCase()
    return orders.filter((order) => (!text || `${order.funpay_order_id} ${order.buyer_funpay_id}`.toLowerCase().includes(text)) && (status === 'all' || order.status === status))
  }, [orders, search, status])
  if (query.isLoading) return <LoadingState label="Загружаем заказы" />
  if (query.isError) return <ErrorState onRetry={() => query.refetch()} />
  return (
    <section className="panel panel--flush">
      <div className="toolbar"><label className="search-field"><Icon name="search" /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Заказ или покупатель" aria-label="Поиск заказов" /></label><label className="select-field"><span>Статус</span><select value={status} onChange={(event) => setStatus(event.target.value)}><option value="all">Все</option><option value="pending">Ожидают</option><option value="completed">Завершены</option><option value="refunded">Возвраты</option></select><Icon name="chevron-down" size={15} /></label><span className="toolbar__count">Показано: {filtered.length}</span></div>
      {orders.length === 0 ? <EmptyState icon="deals" title="Заказов пока нет" description="Здесь появятся оплаченные заказы, полученные от FunPay. Убедитесь, что интеграция подключена и на витрине есть активный лот." action={<Link className="button button--secondary" to="/lots">Проверить лоты<Icon name="arrow-right" /></Link>} /> : filtered.length === 0 ? <EmptyState icon="search" title="Заказы не найдены" description="Измените поиск или фильтр статуса." /> : <TableShell><table className="data-table"><thead><tr><th>Заказ</th><th>Покупатель</th><th>Создан</th><th>Лот и условие</th><th>Сумма</th><th>Статус</th></tr></thead><tbody>{filtered.map((order) => <tr key={order.id}><td><div className="identity-cell"><span className="identity-avatar identity-avatar--blue"><Icon name="deals" size={16} /></span><span><strong>#{order.funpay_order_id}</strong><small>Внутренний ID {order.id}</small></span></div></td><td><strong>{order.buyer_funpay_id}</strong><small className="table-subline">Чат {order.funpay_chat_id}</small></td><td>{formatDateTime(order.created_at)}</td><td>{order.lot_id ? `#${order.lot_id}` : '—'}<small className="table-subline">{purchasedLimitCondition(order)}</small></td><td className="table-number">{formatCurrency(order.price)}</td><td><div className="credential-delivery-cell"><StatusBadge value={order.status} />{order.fulfillment_attempts > 0 && <small>Попыток: {order.fulfillment_attempts}</small>}{order.fulfillment_next_attempt_at && <small>Следующая: {formatDateTime(order.fulfillment_next_attempt_at)}</small>}{safeOrderRetryError(order.fulfillment_last_error) && <small className="credential-delivery-error">{safeOrderRetryError(order.fulfillment_last_error)}</small>}</div></td></tr>)}</tbody></table></TableShell>}
    </section>
  )
}

function RentalsTab({ query }: { query: ReturnType<typeof useRentals> }) {
  const [status, setStatus] = useState('all')
  const retryDelivery = useRetryRentalDelivery()
  const rentals = query.data ?? []
  const filtered = rentals.filter((rental) => status === 'all' || rental.status === status)
  if (query.isLoading) return <LoadingState label="Загружаем аренды" />
  if (query.isError) return <ErrorState onRetry={() => query.refetch()} />
  return (
    <section className="panel panel--flush">
      <div className="toolbar"><div className="toolbar__title"><strong>Жизненный цикл аренд</strong><span>Выдача, срок доступа и замены аккаунтов</span></div><label className="select-field"><span>Статус</span><select value={status} onChange={(event) => setStatus(event.target.value)}><option value="all">Все</option><option value="active">Активные</option><option value="expired">Истекли</option><option value="revoked">Отозваны</option><option value="replaced">Заменены</option></select><Icon name="chevron-down" size={15} /></label><span className="toolbar__count">Показано: {filtered.length}</span></div>
      {rentals.length === 0 ? <EmptyState icon="clock" title="Аренд пока нет" description="Аренда создаётся после оплаченного заказа, когда система находит подходящий проверенный аккаунт. Проверьте пул, если заказ уже есть, а аренда не появилась." action={<Link className="button button--secondary" to="/accounts">Проверить аккаунты<Icon name="arrow-right" /></Link>} /> : filtered.length === 0 ? <EmptyState icon="search" title="Аренды не найдены" description="Для выбранного статуса записей нет. Смените фильтр." /> : <TableShell><table className="data-table"><thead><tr><th>Аренда</th><th>Аккаунт и лимиты</th><th>Покупатель</th><th>Начало</th><th>Окончание</th><th>Замены</th><th>Выдача данных</th><th>Статус</th></tr></thead><tbody>{filtered.map((rental) => {
        const deliveryError = safeDeliveryError(rental.credentials_delivery_last_error)
        return <tr key={rental.id}><td><strong>#{rental.id}</strong><small className="table-subline">Заказ #{rental.order_id}</small></td><td><span className="mono-chip">Account #{rental.account_id}</span><small className="table-subline">{issuedLimitsSnapshot(rental)}</small><small className="table-subline">{issuedWindowContract(rental)}</small><small className="table-subline">{purchasedLimitCondition(rental)}</small>{rental.issued_limits_measured_at && <small className="table-subline">Замер: {formatDateTime(rental.issued_limits_measured_at)}</small>}</td><td><strong>{rental.buyer_funpay_id}</strong><small className="table-subline">Чат {rental.buyer_funpay_chat_id}</small></td><td>{formatDateTime(rental.started_at)}</td><td>{formatDateTime(rental.expires_at)}</td><td>{rental.replacement_count}</td><td><div className="credential-delivery-cell"><StatusBadge value={deliveryTone(rental.credentials_delivery_status)} label={deliveryLabel(rental.credentials_delivery_status)} /><small>Попыток: {rental.credentials_delivery_attempts}</small>{rental.credentials_delivery_next_attempt_at && <small>Следующая: {formatDateTime(rental.credentials_delivery_next_attempt_at)}</small>}{rental.credentials_delivery_status === 'manual' && <><small className="credential-delivery-error">Требуется вмешательство оператора</small><button className="button button--ghost button--compact" disabled={retryDelivery.isPending} onClick={() => retryDelivery.mutate(rental.id)}>{retryDelivery.isPending ? 'Повторяем…' : 'Повторить выдачу'}</button></>}{deliveryError && <small className="credential-delivery-error">{deliveryError}</small>}</div></td><td><StatusBadge value={rental.status} /></td></tr>
      })}</tbody></table></TableShell>}
    </section>
  )
}

function issuedLimitsSnapshot(rental: Rental) {
  const windows = [
    formatIssuedWindow(rental.issued_codex_primary_pct, rental.issued_codex_primary_window_seconds, rental.issued_codex_primary_resets_at),
    formatIssuedWindow(rental.issued_codex_secondary_pct, rental.issued_codex_secondary_window_seconds, rental.issued_codex_secondary_resets_at),
  ].filter(Boolean)
  return windows.length > 0 ? `При выдаче: ${windows.join(' · ')}` : 'Снимок лимитов отсутствует'
}

function formatIssuedWindow(remaining: number | null, seconds: number | null, resetsAt: string | null) {
  if (remaining === null || seconds === null) return ''
  const reset = resetsAt ? `, сброс ${formatDateTime(resetsAt)}` : ''
  return `${formatWindow(seconds)}: ${remaining}%${reset}`
}

function issuedWindowContract(rental: Rental) {
  const expected = rental.issued_expected_long_window_seconds
  const expectedLabel = expected === null ? 'не определено' : formatWindow(expected)
  const status = rental.issued_plan_window_status === 'ok'
    ? 'подтверждён'
    : rental.issued_plan_window_status === 'mismatch'
      ? 'не совпал'
      : 'не зафиксирован'
  return `Контракт окна: ${status} · ожидалось ${expectedLabel}`
}

function formatWindow(seconds: number) {
  return seconds === 18_000 ? '5 ч' : seconds === 604_800 ? '7 д' : seconds === 2_592_000 ? '30 д' : `${Math.round(seconds / 3_600)} ч`
}

function purchasedLimitCondition(item: Pick<Rental, 'min_limit_pct' | 'max_5h_pct' | 'max_weekly_pct'>) {
  if (item.min_limit_pct !== null) return `Условие: не ниже ${item.min_limit_pct}%`
  const ceilings = [
    item.max_5h_pct !== null ? `5 ч ≤ ${item.max_5h_pct}%` : '',
    item.max_weekly_pct !== null ? `длинное ≤ ${item.max_weekly_pct}%` : '',
  ].filter(Boolean)
  return ceilings.length > 0 ? `Условие: ${ceilings.join(', ')}` : 'Условие: без гарантии лимита'
}

function deliveryTone(status: string) {
  if (status === 'sent') return 'active'
  if (status === 'sending') return 'pending'
  return 'failed'
}

function deliveryLabel(status: string) {
  const labels: Record<string, string> = {
    sending: 'Отправляется',
    sent: 'Отправлено',
    failed: 'Ошибка выдачи',
    manual: 'Нужен оператор',
  }
  return labels[status] ?? 'Неизвестно'
}

function safeDeliveryError(error: string | null) {
  if (!error) return null
  if (error === 'order_not_fulfillable') return 'Заказ больше нельзя исполнить'
  if (error === 'delivery_data_missing') return 'Не хватает данных для выдачи'
  if (error.startsWith('delivery_failed:')) return 'Сбой отправки в чат FunPay'
  return 'Сбой выдачи; подробности доступны в журнале сервера'
}

function safeOrderRetryError(error: string | null) {
  if (!error) return null
  if (error === 'no_account_available') return 'Нет подходящего проверенного аккаунта'
  if (error.startsWith('remote_status:')) return 'FunPay ещё не подтвердил оплату'
  if (error.startsWith('retry_failed:')) return 'Временный сбой повторной обработки'
  if (error === 'credential_delivery_manual_required' || error === 'delivery_data_missing') return 'Нужно вмешательство оператора'
  if (error.startsWith('delivery_failed:')) return 'Сбой отправки в чат FunPay'
  return 'Заказ ожидает повторной обработки'
}
