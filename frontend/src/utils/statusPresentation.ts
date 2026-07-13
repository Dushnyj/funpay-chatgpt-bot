export type StatusTone = 'success' | 'warning' | 'danger'

const POSITIVE_STATUSES = new Set(['active', 'connected', 'completed', 'ok', 'healthy'])
const WARNING_STATUSES = new Set([
  'pending',
  'pending_validation',
  'detecting',
  'paused',
  'maintenance',
  'unknown',
  'refund_pending',
  'expiry_pending',
])

const STATUS_LABELS: Readonly<Record<string, string>> = {
  active: 'Активен',
  banned: 'Заблокирован',
  completed: 'Завершён',
  connected: 'Подключён',
  deleted: 'Удалён',
  disabled: 'Отключён',
  disconnected: 'Не подключён',
  error: 'Ошибка',
  expired: 'Истёк',
  expiry_pending: 'Доступ завершается',
  failed: 'Ошибка',
  maintenance: 'Обслуживание',
  paused: 'Приостановлен',
  pending: 'Ожидает',
  pending_validation: 'Проверяется',
  refund_pending: 'Возврат обрабатывается',
  refunded: 'Возврат',
  replaced: 'Заменён',
  revoked: 'Отозван',
  unknown: 'Неизвестно',
}

export function statusPresentation(value: string): { normalized: string; tone: StatusTone; label: string } {
  const normalized = value.toLowerCase().replaceAll(' ', '_')
  const tone = POSITIVE_STATUSES.has(normalized)
    ? 'success'
    : WARNING_STATUSES.has(normalized)
      ? 'warning'
      : 'danger'
  return {
    normalized,
    tone,
    label: STATUS_LABELS[normalized] ?? normalized.replaceAll('_', ' '),
  }
}
