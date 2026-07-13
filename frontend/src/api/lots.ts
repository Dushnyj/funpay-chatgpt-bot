import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type { Lot, LotCreate, LotStatusUpdate, LotSyncResult } from '../types/api'

const lotKeys = {
  all: ['lots'] as const,
}

function invalidateCommerce(qc: ReturnType<typeof useQueryClient>) {
  void qc.invalidateQueries({ queryKey: lotKeys.all })
  void qc.invalidateQueries({ queryKey: ['metrics'] })
}

export function useLots() {
  return useQuery({
    queryKey: lotKeys.all,
    queryFn: () => api.get<Lot[]>('/lots'),
    refetchInterval: 15_000,
  })
}

export function useCreateLot() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: LotCreate) => api.post<Lot>('/lots', body),
    onSuccess: () => invalidateCommerce(qc),
  })
}

export function useUpdateLotStatus() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, ...body }: { id: number } & LotStatusUpdate) =>
      api.patch<Lot>(`/lots/${id}`, body),
    onSuccess: () => invalidateCommerce(qc),
  })
}

export function useSyncLots() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => api.post<LotSyncResult>('/lots/sync'),
    onSuccess: () => {
      invalidateCommerce(qc)
      void qc.invalidateQueries({ queryKey: ['prices'] })
    },
  })
}

export function useDeleteLot() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => api.delete(`/lots/${id}`),
    onSuccess: () => invalidateCommerce(qc),
  })
}
