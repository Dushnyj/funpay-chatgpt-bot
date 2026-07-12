import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type { ChatMessage, ChatSummary } from '../types/api'

const chatKeys = {
  all: ['chats'] as const,
  messages: (conversationId: number | null) => ['chats', conversationId, 'messages'] as const,
}

export function useChats() {
  return useQuery({
    queryKey: chatKeys.all,
    queryFn: () => api.get<ChatSummary[]>('/chats'),
    refetchInterval: 5000,
  })
}

export function useChatMessages(conversationId: number | null) {
  return useQuery({
    queryKey: chatKeys.messages(conversationId),
    queryFn: () => api.get<ChatMessage[]>(`/chats/${conversationId}/messages`),
    enabled: conversationId !== null,
    refetchInterval: conversationId === null ? false : 3000,
  })
}

export function useMarkChatRead() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (conversationId: number) =>
      api.post<{ status: string; unread_count: number }>(`/chats/${conversationId}/read`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: chatKeys.all }),
  })
}

export function useSendChatMessage(conversationId: number | null) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (text: string) => {
      if (conversationId === null) throw new Error('Chat is not selected')
      return api.post<ChatMessage>(`/chats/${conversationId}/messages`, { text })
    },
    onSuccess: async (message) => {
      queryClient.setQueryData<ChatMessage[]>(chatKeys.messages(conversationId), (current) => {
        if (!current || current.some((item) => item.id === message.id)) return current
        return [...current, message]
      })
      await queryClient.invalidateQueries({ queryKey: chatKeys.all })
    },
  })
}
