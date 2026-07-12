import { useQuery } from '@tanstack/react-query'
import { api } from './client'
import type { Rental } from '../types/api'

export function useRentals() {
  return useQuery({ queryKey: ['rentals'], queryFn: () => api.get<Rental[]>('/rentals') })
}
