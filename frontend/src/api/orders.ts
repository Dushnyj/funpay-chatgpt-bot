import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type { Order } from '../types/api'

export function useOrders() {
  return useQuery({
    queryKey: ['orders'],
    queryFn: () => api.get<Order[]>('/orders'),
    refetchInterval: 15_000,
  })
}

export function useRetryOrderConfirmation() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (orderId: number) => api.post<Order>(`/orders/${orderId}/retry-confirmation`),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['orders'] })
    },
  })
}
