import type { ReactNode } from 'react'
import { Icon, type IconName } from './Icon'

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow?: string
  title: string
  description?: string
  actions?: ReactNode
}) {
  return (
    <header className="page-header">
      <div>
        {eyebrow && <div className="eyebrow">{eyebrow}</div>}
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      {actions && <div className="page-header__actions">{actions}</div>}
    </header>
  )
}

export function StatusBadge({ value, label }: { value: string; label?: string }) {
  const normalized = value.toLowerCase().replaceAll(' ', '_')
  const positive = ['active', 'connected', 'completed', 'ok', 'healthy'].includes(normalized)
  const warning = ['pending', 'pending_validation', 'paused', 'maintenance', 'unknown'].includes(normalized)
  const tone = positive ? 'success' : warning ? 'warning' : 'danger'
  const dot = tone === 'success' ? 'status-dot--success' : tone === 'warning' ? 'status-dot--warning' : 'status-dot--danger'
  return (
    <span className={`status-badge status-badge--${tone}`}>
      <span className={`status-dot ${dot}`} />
      {label ?? humanizeStatus(value)}
    </span>
  )
}

export function EmptyState({
  icon = 'database',
  title,
  description,
  action,
}: {
  icon?: IconName
  title: string
  description: string
  action?: ReactNode
}) {
  return (
    <div className="empty-state">
      <div className="empty-state__icon"><Icon name={icon} size={24} /></div>
      <h3>{title}</h3>
      <p>{description}</p>
      {action && <div className="empty-state__action">{action}</div>}
    </div>
  )
}

export function LoadingState({ label = 'Загружаем данные' }: { label?: string }) {
  return (
    <div className="loading-state" role="status">
      <span className="spinner" />
      <span>{label}</span>
    </div>
  )
}

export function ErrorState({ message = 'Не удалось загрузить данные', onRetry }: { message?: string; onRetry?: () => void }) {
  return (
    <div className="error-state" role="alert">
      <div className="empty-state__icon empty-state__icon--danger"><Icon name="warning" size={22} /></div>
      <div>
        <strong>{message}</strong>
        <p>Проверьте соединение с сервером и повторите попытку.</p>
      </div>
      {onRetry && <button className="button button--secondary" onClick={onRetry}><Icon name="refresh" />Повторить</button>}
    </div>
  )
}

export function TableShell({ children }: { children: ReactNode }) {
  return <div className="table-shell">{children}</div>
}

function humanizeStatus(value: string) {
  const labels: Record<string, string> = {
    active: 'Активен',
    banned: 'Заблокирован',
    completed: 'Завершён',
    connected: 'Подключён',
    deleted: 'Удалён',
    disabled: 'Отключён',
    disconnected: 'Не подключён',
    error: 'Ошибка',
    expired: 'Истёк',
    failed: 'Ошибка',
    maintenance: 'Обслуживание',
    paused: 'Приостановлен',
    pending: 'Ожидает',
    pending_validation: 'Проверяется',
    refunded: 'Возврат',
    replaced: 'Заменён',
    revoked: 'Отозван',
    unknown: 'Неизвестно',
  }
  return labels[value] ?? value.replaceAll('_', ' ')
}
