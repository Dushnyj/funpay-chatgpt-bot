import { useEffect, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Icon, type IconName } from './Icon'

const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

let openModalCount = 0
let rootHadInert = false
let rootAriaHidden: string | null = null
let bodyOverflow = ''

export function ModalOverlay({
  children,
  onClose,
  canClose = true,
  closeOnBackdrop = true,
}: {
  children: ReactNode
  onClose: () => void
  canClose?: boolean
  closeOnBackdrop?: boolean
}) {
  const overlayRef = useRef<HTMLDivElement>(null)
  const onCloseRef = useRef(onClose)
  const canCloseRef = useRef(canClose)
  onCloseRef.current = onClose
  canCloseRef.current = canClose

  useEffect(() => {
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const root = document.getElementById('root')

    if (openModalCount === 0) {
      rootHadInert = root?.hasAttribute('inert') ?? false
      rootAriaHidden = root?.getAttribute('aria-hidden') ?? null
      bodyOverflow = document.body.style.overflow
      if (root) {
        root.setAttribute('inert', '')
        root.setAttribute('aria-hidden', 'true')
      }
      document.body.style.overflow = 'hidden'
    }
    openModalCount += 1

    const focusable = () => Array.from(overlayRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])
      .filter((item) => !item.hasAttribute('disabled') && item.getAttribute('aria-hidden') !== 'true')
    const preferred = overlayRef.current?.querySelector<HTMLElement>('[data-autofocus]')
    window.requestAnimationFrame(() => (preferred ?? focusable()[0] ?? overlayRef.current)?.focus())

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        if (canCloseRef.current) onCloseRef.current()
        return
      }
      if (event.key !== 'Tab') return
      const items = focusable()
      if (items.length === 0) {
        event.preventDefault()
        overlayRef.current?.focus()
        return
      }
      const first = items[0]
      const last = items[items.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', handleKeyDown)

    return () => {
      document.removeEventListener('keydown', handleKeyDown)
      openModalCount = Math.max(0, openModalCount - 1)
      if (openModalCount === 0) {
        if (root) {
          if (rootHadInert) root.setAttribute('inert', '')
          else root.removeAttribute('inert')
          if (rootAriaHidden === null) root.removeAttribute('aria-hidden')
          else root.setAttribute('aria-hidden', rootAriaHidden)
        }
        document.body.style.overflow = bodyOverflow
      }
      previousFocus?.focus()
    }
  }, [])

  return createPortal(
    <div
      ref={overlayRef}
      className="modal-overlay"
      role="presentation"
      tabIndex={-1}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && closeOnBackdrop && canClose) onClose()
      }}
    >
      {children}
    </div>,
    document.body,
  )
}

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
  const warning = ['pending', 'pending_validation', 'detecting', 'paused', 'maintenance', 'unknown'].includes(normalized)
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
