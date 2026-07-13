import { useQuery } from '@tanstack/react-query'
import { api } from './client'
import type { Order } from '../types/api'

export function useOrders() {
  return useQuery({
    queryKey: ['orders'],
    queryFn: () => api.get<Order[]>('/orders'),
    refetchInterval: 15_000,
  })
}
