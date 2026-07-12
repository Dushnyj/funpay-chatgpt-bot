import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type { FunPayKeyStatus, Settings, TelegramConfigStatus } from '../types/api'

export function useSettings() {
  return useQuery({ queryKey: ['settings'], queryFn: () => api.get<Settings>('/settings') })
}

export function useUpdateSettings() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: Partial<Settings>) => api.put<Settings>('/settings', body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['settings'] }),
  })
}

export function useFunPayKeyStatus() {
  return useQuery({
    queryKey: ['settings', 'funpay-key'],
    queryFn: () => api.get<FunPayKeyStatus>('/settings/funpay-key'),
  })
}

export function useSetFunPayKey() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (key: string) => api.put<FunPayKeyStatus>('/settings/funpay-key', { key }),
    onSuccess: (status) => queryClient.setQueryData(['settings', 'funpay-key'], status),
    gcTime: 0,
  })
}

export function useClearFunPayKey() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.delete<FunPayKeyStatus>('/settings/funpay-key'),
    onSuccess: (status) => queryClient.setQueryData(['settings', 'funpay-key'], status),
  })
}

export function useTelegramConfig() {
  return useQuery({
    queryKey: ['settings', 'telegram'],
    queryFn: () => api.get<TelegramConfigStatus>('/settings/telegram'),
  })
}

export function useUpdateTelegramConfig() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: { token?: string; seller_chat_id?: string }) =>
      api.put<TelegramConfigStatus>('/settings/telegram', body),
    onSuccess: (status) => queryClient.setQueryData(['settings', 'telegram'], status),
    gcTime: 0,
  })
}

export function useClearTelegramConfig() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.delete<TelegramConfigStatus>('/settings/telegram'),
    onSuccess: (status) => queryClient.setQueryData(['settings', 'telegram'], status),
  })
}

export function useTestTelegramConfig() {
  return useMutation({ mutationFn: () => api.post<{ status: string }>('/settings/telegram/test') })
}

export function useChangeAdminPassword() {
  return useMutation({
    mutationFn: (body: { current_password: string; new_password: string }) =>
      api.post<{ status: string }>('/auth/change-password', body),
    gcTime: 0,
  })
}
