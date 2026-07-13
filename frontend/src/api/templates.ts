import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type { LotTemplate, LotTemplateCreate, LotTemplateUpdate, MessageTemplate } from '../types/api'

const messageTemplatesKey = ['templates', 'messages'] as const
const lotTemplatesKey = ['templates', 'lots'] as const

export function useTemplates() {
  return useQuery({ queryKey: messageTemplatesKey, queryFn: () => api.get<MessageTemplate[]>('/templates') })
}

export function useUpdateTemplates() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (items: Array<Pick<MessageTemplate, 'key' | 'lang' | 'content'>>) =>
      api.put<{ updated: number }>('/templates', { items }),
    onSuccess: () => qc.invalidateQueries({ queryKey: messageTemplatesKey }),
  })
}

export function useResetTemplate() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ key, lang }: Pick<MessageTemplate, 'key' | 'lang'>) =>
      api.post<MessageTemplate>(`/templates/messages/${encodeURIComponent(key)}/${encodeURIComponent(lang)}/reset`),
    onSuccess: () => qc.invalidateQueries({ queryKey: messageTemplatesKey }),
  })
}

export function useLotTemplates() {
  return useQuery({ queryKey: lotTemplatesKey, queryFn: () => api.get<LotTemplate[]>('/templates/lot') })
}

export function useUpdateLotTemplate() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ key, body }: { key: string; body: LotTemplateUpdate }) =>
      api.put<LotTemplate>(`/templates/lot/${encodeURIComponent(key)}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: lotTemplatesKey }),
  })
}

export function useResetLotTemplate() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (key: string) => api.post<LotTemplate>(`/templates/lot/${encodeURIComponent(key)}/reset`),
    onSuccess: () => qc.invalidateQueries({ queryKey: lotTemplatesKey }),
  })
}

export function useCreateLotTemplate() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: LotTemplateCreate) => api.post<LotTemplate>('/templates/lot', body),
    onSuccess: () => qc.invalidateQueries({ queryKey: lotTemplatesKey }),
  })
}

export function useDeleteLotTemplate() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (key: string) => api.delete(`/templates/lot/${encodeURIComponent(key)}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: lotTemplatesKey }),
  })
}
