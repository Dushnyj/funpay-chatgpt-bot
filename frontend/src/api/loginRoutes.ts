import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type {
  HomeRelaySetup,
  HomeRelaySetupRequest,
  LoginRoute,
  LoginRoutePatch,
  LoginRoutesResponse,
  LoginRouteWrite,
} from '../types/api'

const LOGIN_ROUTES_KEY = ['settings', 'login-routes'] as const

export function useLoginRoutes() {
  return useQuery({
    queryKey: LOGIN_ROUTES_KEY,
    queryFn: () => api.get<LoginRoutesResponse>('/proxy-routes'),
    refetchInterval: (query) => query.state.data?.routes.some((route) => route.status === 'online') ? 30_000 : false,
  })
}

export function useCreateLoginRoute() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: LoginRouteWrite) => api.post<LoginRoute>('/proxy-routes', body),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: LOGIN_ROUTES_KEY }),
    gcTime: 0,
  })
}

export function useUpdateLoginRoute() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, ...body }: { id: number } & LoginRoutePatch) =>
      api.patch<LoginRoute>(`/proxy-routes/${id}`, body),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: LOGIN_ROUTES_KEY }),
    gcTime: 0,
  })
}

export function useDeleteLoginRoute() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => api.delete(`/proxy-routes/${id}`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: LOGIN_ROUTES_KEY }),
  })
}

export function useSetDefaultLoginRoute() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (routeId: number | null) =>
      api.put<LoginRoutesResponse>('/proxy-routes/default', { route_id: routeId }),
    onSuccess: (response) => queryClient.setQueryData(LOGIN_ROUTES_KEY, response),
  })
}

export function useTestLoginRoute() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => api.post<LoginRoute>(`/proxy-routes/${id}/test`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: LOGIN_ROUTES_KEY }),
  })
}

export function useCreateHomeRelaySetup() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: HomeRelaySetupRequest) =>
      api.post<HomeRelaySetup>('/proxy-routes/home-relay/setup', body),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: LOGIN_ROUTES_KEY }),
    gcTime: 0,
  })
}
