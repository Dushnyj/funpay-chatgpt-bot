import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type { MessageTemplate } from '../types/api'

export function useTemplates() {
  return useQuery({ queryKey: ['templates'], queryFn: () => api.get<MessageTemplate[]>('/templates') })
}

export function useUpdateTemplates() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (items: MessageTemplate[]) =>
      api.put<{ updated: number }>('/templates', { items }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['templates'] }),
  })
}
