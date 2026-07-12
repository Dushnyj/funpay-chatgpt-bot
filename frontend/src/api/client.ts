const BASE = '/api'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (resp.status === 401 && window.location.pathname !== '/login') {
    window.location.href = '/login'
    throw new ApiError(401, 'Unauthorized')
  }
  if (!resp.ok) {
    const raw = await resp.text().catch(() => resp.statusText)
    let message = raw || resp.statusText
    try {
      const parsed = JSON.parse(raw) as { detail?: string }
      message = parsed.detail ?? message
    } catch {
      // Ответ не в JSON — сохраняем исходный текст.
    }
    throw new ApiError(resp.status, message)
  }
  if (resp.status === 204) return undefined as T
  return resp.json()
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined }),
  patch: <T>(path: string, body: unknown) =>
    request<T>(path, { method: 'PATCH', body: JSON.stringify(body) }),
  put: <T>(path: string, body: unknown) =>
    request<T>(path, { method: 'PUT', body: JSON.stringify(body) }),
  delete: <T = void>(path: string) => request<T>(path, { method: 'DELETE' }),
}
