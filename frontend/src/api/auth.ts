import { useMutation } from '@tanstack/react-query'
import { api } from './client'

export function useLogin() {
  return useMutation({
    mutationFn: (password: string) => api.post('/auth/login', { password }),
  })
}

export function useLogout() {
  return useMutation({
    mutationFn: () => api.post('/auth/logout'),
  })
}
