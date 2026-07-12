import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type { Tier, TierCreate, Duration, LimitScope } from '../types/api'

export function useTiers() {
  return useQuery({ queryKey: ['tiers'], queryFn: () => api.get<Tier[]>('/tiers') })
}

export function useCreateTier() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: TierCreate) => api.post<Tier>('/tiers', body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['tiers'] }),
  })
}

export function useDeleteTier() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => api.delete(`/tiers/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['tiers'] }),
  })
}

export function useUpdateTier() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, ...body }: { id: number; is_sellable?: boolean; is_active?: boolean }) =>
      api.patch<Tier>(`/tiers/${id}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['tiers'] }),
  })
}

export function useDurations() {
  return useQuery({ queryKey: ['durations'], queryFn: () => api.get<Duration[]>('/durations') })
}

export function useLimitScopes() {
  return useQuery({ queryKey: ['limit-scopes'], queryFn: () => api.get<LimitScope[]>('/limit-scopes') })
}
