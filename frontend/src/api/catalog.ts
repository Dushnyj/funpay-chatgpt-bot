import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type {
  Duration,
  DurationUpdate,
  LimitScope,
  LimitScopeUpdate,
  Tier,
  TierCreate,
  TierUpdate,
} from '../types/api'

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
    mutationFn: ({ id, ...body }: TierUpdate & { id: number }) =>
      api.patch<Tier>(`/tiers/${id}`, body),
    onSuccess: (updated) => {
      qc.setQueryData<Tier[]>(['tiers'], (current) =>
        current?.map((tier) => tier.id === updated.id ? updated : tier),
      )
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['tiers'] }),
  })
}

export function useDurations() {
  return useQuery({ queryKey: ['durations'], queryFn: () => api.get<Duration[]>('/durations') })
}

export function useUpdateDuration() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, ...body }: DurationUpdate & { id: number }) =>
      api.patch<Duration>(`/durations/${id}`, body),
    onSuccess: (updated) => {
      qc.setQueryData<Duration[]>(['durations'], (current) =>
        current?.map((duration) => duration.id === updated.id ? updated : duration),
      )
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['durations'] }),
  })
}

export function useLimitScopes() {
  return useQuery({ queryKey: ['limit-scopes'], queryFn: () => api.get<LimitScope[]>('/limit-scopes') })
}

export function useUpdateLimitScope() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, ...body }: LimitScopeUpdate & { id: number }) =>
      api.patch<LimitScope>(`/limit-scopes/${id}`, body),
    onSuccess: (updated) => {
      qc.setQueryData<LimitScope[]>(['limit-scopes'], (current) =>
        current?.map((scope) => scope.id === updated.id ? updated : scope),
      )
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['limit-scopes'] }),
  })
}
