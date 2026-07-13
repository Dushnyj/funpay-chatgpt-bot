import { Link } from 'react-router-dom'
import { useAccounts } from '../api/accounts'
import { useMetrics } from '../api/metrics'
import { useOrders } from '../api/orders'
import { usePrices } from '../api/prices'
import { useSettings } from '../api/settings'
import { Icon, type IconName } from '../components/Icon'
import { EmptyState, LoadingState, PageHeader, StatusBadge, TableShell } from '../components/ui'
import { formatCurrency, formatDateTime } from '../utils/format'

const METRIC_CARDS: Array<{
  key: 'active_rentals' | 'available_accounts' | 'orders_today' | 'revenue_netto'
  label: string
  hint: string
  icon: IconName
  currency?: boolean
}> = [
  { key: 'active_rentals', label: 'Активные аренды', hint: 'Сейчас обслуживаются', icon: 'clock' },
  { key: 'available_accounts', label: 'Свободная ёмкость', hint: 'Доступные слоты аренды', icon: 'accounts' },
  { key: 'orders_today', label: 'Заказы сегодня', hint: 'С начала суток', icon: 'deals' },
  { key: 'revenue_netto', label: 'Выручка netto', hint: 'После комиссии', icon: 'prices', currency: true },
]

export default function Dashboard() {
  const metricsQuery = useMetrics()
  const accountsQuery = useAccounts()
  const ordersQuery = useOrders()
  const pricesQuery = usePrices()
  const settingsQuery = useSettings()

  if (metricsQuery.isLoading) return <LoadingState label="Собираем операционные показатели" />

  const metrics = metricsQuery.data
  const connected = metrics?.bot_status === 'connected'
  const accounts = accountsQuery.data ?? []
  const recentOrders = [...(ordersQuery.data ?? [])]
    .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
    .slice(0, 5)

  const setupSteps = [
    { label: 'Доступ администратора', complete: true, detail: 'Защищённая сессия активна', to: '/settings' },
    { label: 'Категория FunPay', complete: Boolean(settingsQuery.data?.funpay_node_id), detail: settingsQuery.data?.funpay_node_id ? `Node ID ${settingsQuery.data.funpay_node_id}` : 'Укажите Node ID', to: '/settings' },
    { label: 'Подключение FunPay', complete: connected, detail: connected ? 'События принимаются' : 'Golden key и runner не активны', to: '/settings' },
    { label: 'Пул аккаунтов', complete: accounts.length > 0, detail: accounts.length ? `${accounts.length} аккаунтов добавлено` : 'Добавьте первый аккаунт', to: '/accounts' },
    { label: 'Матрица цен', complete: Boolean(pricesQuery.data?.length), detail: pricesQuery.data?.length ? `${pricesQuery.data.length} правил цены` : 'Настройте правила продаж', to: '/prices' },
  ]
  const completedSteps = setupSteps.filter((step) => step.complete).length

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Операционный центр"
        title="Обзор"
        description="Ключевые показатели, готовность системы и последние сделки."
        actions={<Link className="button button--secondary" to="/settings"><Icon name="settings" />Настройки системы</Link>}
      />

      <section className={`health-banner ${connected ? 'health-banner--ok' : ''}`}>
        <div className="health-banner__icon"><Icon name={connected ? 'check' : 'activity'} size={25} /></div>
        <div className="health-banner__content">
          <div className="health-banner__title-row">
            <h2>{connected ? 'FunPay подключён и принимает события' : 'Интеграция FunPay пока не активна'}</h2>
            <StatusBadge value={metrics?.bot_status ?? 'unknown'} />
          </div>
          <p>{connected ? 'Продажи, сообщения и синхронизация лотов доступны.' : 'Панель и база данных доступны, но автоматизация продаж не запущена. Завершите настройку перед добавлением реальных аккаунтов.'}</p>
        </div>
        {!connected && <Link className="button button--primary" to="/settings">Проверить настройку<Icon name="arrow-right" /></Link>}
      </section>

      <section className="metrics-grid" aria-label="Ключевые показатели">
        {METRIC_CARDS.map((card) => (
          <article className="metric-card" key={card.key}>
            <div className="metric-card__top">
              <div className="metric-card__icon"><Icon name={card.icon} /></div>
              <span className="metric-card__period">Сегодня</span>
            </div>
            <div className="metric-value">
              {metrics ? (card.currency ? formatCurrency(metrics[card.key]) : metrics[card.key]) : '—'}
            </div>
            <div className="metric-label">{card.label}</div>
            <div className="metric-hint">{card.hint}</div>
          </article>
        ))}
      </section>

      <div className="dashboard-grid">
        <section className="panel setup-panel">
          <div className="panel__header">
            <div>
              <span className="eyebrow">Первичный запуск</span>
              <h2>Готовность к работе</h2>
            </div>
            <strong className="setup-score">{completedSteps}/{setupSteps.length}</strong>
          </div>
          <div className="progress-track"><span style={{ width: `${(completedSteps / setupSteps.length) * 100}%` }} /></div>
          <div className="setup-list">
            {setupSteps.map((step) => (
              <Link to={step.to} className="setup-step" key={step.label}>
                <span className={`setup-step__status ${step.complete ? 'setup-step__status--done' : ''}`}>
                  <Icon name={step.complete ? 'check' : 'arrow-right'} size={15} />
                </span>
                <span><strong>{step.label}</strong><small>{step.detail}</small></span>
                <Icon name="arrow-right" size={16} className="setup-step__arrow" />
              </Link>
            ))}
          </div>
        </section>

        <section className="panel system-panel">
          <div className="panel__header">
            <div>
              <span className="eyebrow">Состояние</span>
              <h2>Контур системы</h2>
            </div>
          </div>
          <div className="system-list">
            <div className="system-row"><span><Icon name="database" />Backend и PostgreSQL</span><StatusBadge value="healthy" label="Доступны" /></div>
            <div className="system-row"><span><Icon name="activity" />FunPay events</span><StatusBadge value={metrics?.bot_status ?? 'unknown'} /></div>
            <div className="system-row"><span><Icon name="accounts" />Пул аккаунтов</span><StatusBadge value={accounts.length ? 'active' : 'unknown'} label={accounts.length ? 'Настроен' : 'Пуст'} /></div>
            <div className="system-row"><span><Icon name="prices" />Правила цен</span><StatusBadge value={pricesQuery.data?.length ? 'active' : 'unknown'} label={pricesQuery.data?.length ? 'Настроены' : 'Не заданы'} /></div>
          </div>
          <div className="system-note"><Icon name="warning" /><p>Health endpoint сейчас проверяет только доступность web-процесса. Состояние scheduler и внешних интеграций требует отдельной backend-метрики.</p></div>
        </section>
      </div>

      <section className="panel">
        <div className="panel__header">
          <div><span className="eyebrow">Продажи</span><h2>Последние заказы</h2></div>
          <Link className="text-link" to="/orders">Все сделки <Icon name="arrow-right" size={15} /></Link>
        </div>
        {ordersQuery.isLoading ? <LoadingState label="Загружаем заказы" /> : recentOrders.length === 0 ? (
          <EmptyState
            icon="deals"
            title="Заказов пока нет"
            description={connected
              ? 'FunPay подключён. Проверьте, что хотя бы один лот активен; первый оплаченный заказ появится здесь автоматически.'
              : 'Сначала подключите FunPay и опубликуйте лоты. До этого панель не сможет получать реальные заказы.'}
            action={<Link className="button button--secondary" to={connected ? '/lots' : '/settings'}>{connected ? 'Проверить лоты' : 'Настроить FunPay'}<Icon name="arrow-right" /></Link>}
          />
        ) : (
          <TableShell>
            <table className="data-table">
              <thead><tr><th>Заказ</th><th>Покупатель</th><th>Создан</th><th>Сумма</th><th>Статус</th></tr></thead>
              <tbody>{recentOrders.map((order) => (
                <tr key={order.id}>
                  <td><strong>#{order.funpay_order_id}</strong></td>
                  <td>{order.buyer_funpay_id}</td>
                  <td>{formatDateTime(order.created_at)}</td>
                  <td className="table-number">{formatCurrency(order.price)}</td>
                  <td><StatusBadge value={order.status} /></td>
                </tr>
              ))}</tbody>
            </table>
          </TableShell>
        )}
      </section>
    </div>
  )
}
