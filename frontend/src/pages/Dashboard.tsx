import { useMetrics } from '../api/metrics'

const METRIC_CARDS = [
  { key: 'active_rentals', label: 'Активных аренд' },
  { key: 'available_accounts', label: 'Свободных аккаунтов' },
  { key: 'orders_today', label: 'Заказов сегодня' },
  { key: 'revenue_brutto', label: 'Выручка brutto (₽)' },
  { key: 'revenue_netto', label: 'Выручка netto (₽)' },
] as const

export default function Dashboard() {
  const { data: metrics, isLoading } = useMetrics()

  if (isLoading) return <div>Загрузка...</div>

  return (
    <div>
      <h1>Дашборд</h1>
      <div className="metrics-grid">
        {METRIC_CARDS.map((card) => (
          <div key={card.key} className="metric-card">
            <div className="metric-value">{metrics ? metrics[card.key] : '—'}</div>
            <div className="metric-label">{card.label}</div>
          </div>
        ))}
      </div>
      <div className="bot-status">
        Статус бота:{' '}
        <span className={metrics?.bot_status === 'connected' ? 'status-ok' : 'status-error'}>
          {metrics?.bot_status || 'unknown'}
        </span>
      </div>
    </div>
  )
}
