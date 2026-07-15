import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type { Account, AccountCreate, AccountCredentialsUpdate, AccountUpdate, DeviceAuthSession, DeviceAuthStatus, EmailOAuthStart } from '../types/api'

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
    refetchIntervalInBackground: true,
  })
}

export function useCreateAccount() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: AccountCreate) => api.post<Account>('/accounts', body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['accounts'] })
      void qc.invalidateQueries({ queryKey: ['metrics'] })
    },
  })
}

export function useDeleteAccount() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => api.delete(`/accounts/${id}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['accounts'] })
      void qc.invalidateQueries({ queryKey: ['metrics'] })
    },
  })
}

export function useRecheckAccount() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => api.post<Account>(`/accounts/${id}/recheck`),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['accounts'] }),
  })
}

export function useConfirmBrowserValidation() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => api.post<Account>(`/accounts/${id}/confirm-browser-validation`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['accounts'] })
      void qc.invalidateQueries({ queryKey: ['metrics'] })
    },
  })
}

export function useUpdateAccount() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, ...body }: { id: number } & AccountUpdate) =>
      api.patch<Account>(`/accounts/${id}`, body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['accounts'] })
      void qc.invalidateQueries({ queryKey: ['metrics'] })
    },
  })
}

export function useRepairAccountCredentials() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, ...body }: { id: number } & AccountCredentialsUpdate) =>
      api.patch<Account>(`/accounts/${id}/credentials`, body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['accounts'] })
      void qc.invalidateQueries({ queryKey: ['metrics'] })
    },
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

export function startMicrosoftEmailOAuth(accountId: number) {
  return api.post<EmailOAuthStart>(`/accounts/${accountId}/email-oauth/microsoft`)
}
