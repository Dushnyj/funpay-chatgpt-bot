import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type { PriceMatrixItem } from '../types/api'

export function usePrices() {
  return useQuery({ queryKey: ['prices'], queryFn: () => api.get<PriceMatrixItem[]>('/prices') })
}

export function useUpdatePrices() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (items: PriceMatrixItem[]) =>
      api.put<{ updated: number }>('/prices', { items }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['prices'] }),
  })
}
