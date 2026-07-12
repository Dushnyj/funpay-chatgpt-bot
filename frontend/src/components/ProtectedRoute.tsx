import type { ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, ApiError } from '../api/client'
import { Icon } from './Icon'

export default function ProtectedRoute({ children }: { children: ReactNode }) {
  const query = useQuery({
    queryKey: ['auth-check'],
    queryFn: () => api.get('/metrics'),
    retry: false,
  })

  if (query.isLoading) {
    return (
      <div className="auth-loading" role="status">
        <div className="brand__mark brand__mark--large"><span>F</span></div>
        <span className="spinner" />
        <p>Проверяем защищённую сессию…</p>
      </div>
    )
  }

  if (query.isError) {
    if (query.error instanceof ApiError && query.error.status === 401) return null
    return (
      <div className="auth-loading auth-loading--error" role="alert">
        <div className="empty-state__icon empty-state__icon--danger"><Icon name="warning" /></div>
        <h1>Панель временно недоступна</h1>
        <p>Сессия сохранена, но backend не ответил на проверку состояния.</p>
        <button className="button button--secondary" onClick={() => query.refetch()}><Icon name="refresh" />Повторить</button>
      </div>
    )
  }
  return <>{children}</>
}
