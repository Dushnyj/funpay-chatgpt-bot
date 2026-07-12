import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type { Lot } from '../types/api'

export function useLots() {
  return useQuery({ queryKey: ['lots'], queryFn: () => api.get<Lot[]>('/lots') })
}

export function useDeleteLot() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => api.delete(`/lots/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['lots'] }),
  })
}
