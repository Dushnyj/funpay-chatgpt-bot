import { useQuery } from '@tanstack/react-query'
import { api } from './client'
import type { Metrics } from '../types/api'

export function useMetrics() {
  return useQuery({
    queryKey: ['metrics'],
    queryFn: () => api.get<Metrics>('/metrics'),
    refetchInterval: 30000,
  })
}
