import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type { Rental } from '../types/api'

export function useRentals() {
  return useQuery({
    queryKey: ['rentals'],
    queryFn: () => api.get<Rental[]>('/rentals'),
    refetchInterval: 15_000,
  })
}

export function useRetryRentalDelivery() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (rentalId: number) => api.post<Rental>(`/rentals/${rentalId}/retry-delivery`),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['rentals'] }),
        queryClient.invalidateQueries({ queryKey: ['orders'] }),
      ])
    },
  })
}
