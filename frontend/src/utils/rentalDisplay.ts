import type { Rental } from '../types/api'

type RentalDeliveryState = Pick<
  Rental,
  'credentials_delivery_attempts' | 'credentials_delivery_status' | 'credentials_delivery_template'
>

export function isInitialRentalDeliveryPending(rental: RentalDeliveryState) {
  return rental.credentials_delivery_template === 'welcome'
    && rental.credentials_delivery_status !== 'sent'
    && rental.credentials_delivery_attempts === 0
}

export function isOccupyingRentalStatus(status: string) {
  return status === 'active' || status === 'expiry_pending'
}
