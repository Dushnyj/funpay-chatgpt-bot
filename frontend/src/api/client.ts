const BASE = '/api'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

type ValidationIssue = {
  loc?: Array<string | number>
  msg?: string
}

function validationMessage(issues: ValidationIssue[]) {
  return issues
    .map((issue) => {
      const field = issue.loc?.filter((part) => part !== 'body').join('.')
      return [field, issue.msg].filter(Boolean).join(': ')
    })
    .filter(Boolean)
    .join('; ')
}

export function parseApiError(raw: string, fallback: string) {
  if (!raw) return fallback
  try {
    const parsed = JSON.parse(raw) as {
      detail?: string | ValidationIssue[] | { message?: string }
      message?: string
      error?: string | { message?: string }
    }
    if (typeof parsed.detail === 'string') return parsed.detail
    if (Array.isArray(parsed.detail)) return validationMessage(parsed.detail) || fallback
    if (parsed.detail && typeof parsed.detail.message === 'string') return parsed.detail.message
    if (typeof parsed.message === 'string') return parsed.message
    if (typeof parsed.error === 'string') return parsed.error
    if (parsed.error && typeof parsed.error.message === 'string') return parsed.error.message
  } catch {
    return raw
  }
  return fallback
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  let resp: Response
  try {
    resp = await fetch(`${BASE}${path}`, {
      credentials: 'include',
      headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
      ...options,
    })
  } catch {
    throw new ApiError(0, 'Не удалось связаться с сервером. Проверьте подключение и повторите попытку.')
  }
  if (resp.status === 401 && window.location.pathname !== '/login') {
    window.location.href = '/login'
    throw new ApiError(401, 'Сессия истекла. Выполните вход повторно.')
  }
  if (!resp.ok) {
    const raw = await resp.text().catch(() => resp.statusText)
    throw new ApiError(resp.status, parseApiError(raw, resp.statusText || `Ошибка HTTP ${resp.status}`))
  }
  if (resp.status === 204) return undefined as T
  return resp.json()
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  patch: <T>(path: string, body: unknown) =>
    request<T>(path, { method: 'PATCH', body: JSON.stringify(body) }),
  put: <T>(path: string, body: unknown) =>
    request<T>(path, { method: 'PUT', body: JSON.stringify(body) }),
  delete: <T = void>(path: string) => request<T>(path, { method: 'DELETE' }),
}
