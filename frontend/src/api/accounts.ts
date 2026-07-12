import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type { Account, AccountCreate, DeviceAuthSession, DeviceAuthStatus } from '../types/api'

export function useAccounts() {
  return useQuery({
    queryKey: ['accounts'],
    queryFn: () => api.get<Account[]>('/accounts'),
    refetchInterval: (query) => {
      const accounts = query.state.data
      const hasValidationInProgress = accounts?.some((account) =>
        account.validation_job
          ? ['pending', 'running', 'processing'].includes(account.validation_job.status)
          : account.status === 'pending_validation',
      )
      return hasValidationInProgress ? 3_000 : false
    },
  })
}

export function useCreateAccount() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: AccountCreate) => api.post<Account>('/accounts', body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['accounts'] }),
  })
}

export function useDeleteAccount() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => api.delete(`/accounts/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['accounts'] }),
  })
}

export function useRecheckAccount() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => api.post<Account>(`/accounts/${id}/recheck`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['accounts'] }),
  })
}

export function useStartDeviceAuth() {
  return useMutation({
    mutationFn: (id: number) => api.post<DeviceAuthSession>(`/accounts/${id}/device-auth`),
  })
}

export function getDeviceAuthStatus(accountId: number, sessionId: string) {
  return api.get<DeviceAuthStatus>(`/accounts/${accountId}/device-auth/${encodeURIComponent(sessionId)}`)
}
