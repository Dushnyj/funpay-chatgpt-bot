import type { ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'

export default function ProtectedRoute({ children }: { children: ReactNode }) {
  const { isError, isLoading } = useQuery({
    queryKey: ['auth-check'],
    queryFn: () => api.get('/metrics'),
    retry: false,
  })

  if (isLoading) return <div>Проверка авторизации...</div>
  if (isError) {
    window.location.href = '/login'
    return null
  }
  return <>{children}</>
}
