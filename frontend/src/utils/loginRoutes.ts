import type { LoginProxyType, LoginRoute, LoginRouteStatus } from '../types/api'

export interface ProxyRouteDraft {
  name: string
  proxyType: LoginProxyType
  host: string
  port: string
  username: string
  password: string
  hasSavedPassword?: boolean
  savedUsername?: string
}

export interface ProxyRouteErrors {
  name?: string
  host?: string
  port?: string
  credentials?: string
}

const HOST_FORBIDDEN = /[\s/?#@]/

export function validateProxyRouteDraft(draft: ProxyRouteDraft): ProxyRouteErrors {
  const errors: ProxyRouteErrors = {}
  const name = draft.name.trim()
  const host = draft.host.trim()
  const port = Number(draft.port)
  const username = draft.username.trim()

  if (name.length < 2 || name.length > 80) {
    errors.name = 'Название должно содержать от 2 до 80 символов.'
  }
  if (!host || host.includes('://') || HOST_FORBIDDEN.test(host)) {
    errors.host = 'Укажите только домен или IP без протокола и пути.'
  }
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    errors.port = 'Порт должен быть целым числом от 1 до 65535.'
  }
  if (draft.proxyType === 'socks5' && (username || draft.password || draft.hasSavedPassword)) {
    errors.credentials = 'SOCKS5 используется без авторизации. Для логина и пароля выберите HTTP или HTTPS CONNECT.'
  } else if (draft.password && !username) {
    errors.credentials = 'Для пароля укажите логин прокси.'
  } else if (draft.hasSavedPassword && username !== (draft.savedUsername ?? '').trim() && !draft.password) {
    errors.credentials = 'Чтобы изменить логин, введите новый пароль повторно.'
  } else if (username && !draft.password && !draft.hasSavedPassword) {
    errors.credentials = 'Укажите пароль или очистите логин для прокси без авторизации.'
  }
  return errors
}

export function hasProxyRouteErrors(errors: ProxyRouteErrors) {
  return Object.values(errors).some(Boolean)
}

export function proxyTypeLabel(type: LoginProxyType | null) {
  if (type === 'socks5') return 'SOCKS5'
  if (type === 'https') return 'HTTPS'
  if (type === 'http') return 'HTTP'
  return 'Внутренний туннель'
}

export function loginRouteKindLabel(route: Pick<LoginRoute, 'mode' | 'proxy_type'>) {
  return route.mode === 'home_relay' ? 'Домашний шлюз' : `${proxyTypeLabel(route.proxy_type)}-прокси`
}

export function loginRouteEndpoint(route: Pick<LoginRoute, 'mode' | 'host' | 'port'>) {
  if (route.mode === 'home_relay') return 'Защищённый обратный туннель'
  if (!route.host || !route.port) return 'Адрес не задан'
  return `${route.host}:${route.port}`
}

export function loginRouteStatusLabel(status: LoginRouteStatus) {
  if (status === 'online') return 'Онлайн'
  if (status === 'offline') return 'Недоступен'
  return 'Не проверен'
}

export function loginRouteStatusTone(status: LoginRouteStatus) {
  if (status === 'online') return 'success'
  if (status === 'offline') return 'danger'
  return 'warning'
}

export function loginRouteErrorLabel(error: string | null | undefined) {
  if (error === 'proxy_check_stale') return 'Нет свежего heartbeat. Проверьте домашний ПК или прокси.'
  if (error === 'runtime_proxy_unavailable') return 'Маршрут перестал отвечать во время входа.'
  if (error === 'proxy_unavailable') return 'Прокси не отвечает или отклонил соединение.'
  if (error === 'proxy_test_failed') return 'Не удалось завершить проверку внешнего IP.'
  if (error === 'proxy_disabled') return 'Маршрут отключён.'
  return error ? 'Маршрут недоступен.' : ''
}
