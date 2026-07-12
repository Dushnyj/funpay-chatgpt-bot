import { useMemo, useState } from 'react'
import { useOrders } from '../api/orders'
import { useRentals } from '../api/rentals'
import { Icon } from '../components/Icon'
import { EmptyState, ErrorState, LoadingState, PageHeader, StatusBadge, TableShell } from '../components/ui'
import { formatCurrency, formatDateTime } from '../utils/format'

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
      {orders.length === 0 ? <EmptyState icon="deals" title="Заказов пока нет" description="Новые события FunPay появятся здесь после подключения runner." /> : filtered.length === 0 ? <EmptyState icon="search" title="Заказы не найдены" description="Измените поиск или фильтр статуса." /> : <TableShell><table className="data-table"><thead><tr><th>Заказ</th><th>Покупатель</th><th>Создан</th><th>Лот</th><th>Сумма</th><th>Статус</th></tr></thead><tbody>{filtered.map((order) => <tr key={order.id}><td><div className="identity-cell"><span className="identity-avatar identity-avatar--blue"><Icon name="deals" size={16} /></span><span><strong>#{order.funpay_order_id}</strong><small>Внутренний ID {order.id}</small></span></div></td><td><strong>{order.buyer_funpay_id}</strong><small className="table-subline">Чат {order.funpay_chat_id}</small></td><td>{formatDateTime(order.created_at)}</td><td>{order.lot_id ? `#${order.lot_id}` : '—'}</td><td className="table-number">{formatCurrency(order.price)}</td><td><StatusBadge value={order.status} /></td></tr>)}</tbody></table></TableShell>}
    </section>
  )
}

function RentalsTab({ query }: { query: ReturnType<typeof useRentals> }) {
  const [status, setStatus] = useState('all')
  const rentals = query.data ?? []
  const filtered = rentals.filter((rental) => status === 'all' || rental.status === status)
  if (query.isLoading) return <LoadingState label="Загружаем аренды" />
  if (query.isError) return <ErrorState onRetry={() => query.refetch()} />
  return (
    <section className="panel panel--flush">
      <div className="toolbar"><div className="toolbar__title"><strong>Жизненный цикл аренд</strong><span>Выдача, срок доступа и замены аккаунтов</span></div><label className="select-field"><span>Статус</span><select value={status} onChange={(event) => setStatus(event.target.value)}><option value="all">Все</option><option value="active">Активные</option><option value="expired">Истекли</option><option value="revoked">Отозваны</option><option value="replaced">Заменены</option></select><Icon name="chevron-down" size={15} /></label><span className="toolbar__count">Показано: {filtered.length}</span></div>
      {rentals.length === 0 ? <EmptyState icon="clock" title="Аренд пока нет" description="Аренда создаётся автоматически после успешной обработки оплаченного заказа." /> : <TableShell><table className="data-table"><thead><tr><th>Аренда</th><th>Аккаунт</th><th>Покупатель</th><th>Начало</th><th>Окончание</th><th>Замены</th><th>Статус</th></tr></thead><tbody>{filtered.map((rental) => <tr key={rental.id}><td><strong>#{rental.id}</strong><small className="table-subline">Заказ #{rental.order_id}</small></td><td><span className="mono-chip">Account #{rental.account_id}</span></td><td><strong>{rental.buyer_funpay_id}</strong><small className="table-subline">Чат {rental.buyer_funpay_chat_id}</small></td><td>{formatDateTime(rental.started_at)}</td><td>{formatDateTime(rental.expires_at)}</td><td>{rental.replacement_count}</td><td><StatusBadge value={rental.status} /></td></tr>)}</tbody></table></TableShell>}
    </section>
  )
}
