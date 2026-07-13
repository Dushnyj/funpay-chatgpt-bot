export function parseCatalogSortOrder(value: string): number | null {
  const normalized = value.trim()
  if (!/^\d+$/.test(normalized)) return null
  const parsed = Number(normalized)
  return Number.isSafeInteger(parsed) && parsed <= 10_000 ? parsed : null
}

export function durationUnit(days: number) {
  const mod100 = Math.abs(days) % 100
  const mod10 = mod100 % 10
  if (mod100 >= 11 && mod100 <= 14) return 'дней'
  if (mod10 === 1) return 'день'
  if (mod10 >= 2 && mod10 <= 4) return 'дня'
  return 'дней'
}

export type DurationDaysValidation = {
  days: number | null
  error: string
}

export function validateDurationDays(value: string, existingDays: number[]): DurationDaysValidation {
  const normalized = value.trim()
  if (!/^\d+$/.test(normalized)) {
    return { days: null, error: 'Введите целое число дней от 1 до 30.' }
  }
  const days = Number(normalized)
  if (!Number.isSafeInteger(days) || days < 1 || days > 30) {
    return { days: null, error: 'Срок должен быть целым числом от 1 до 30 дней.' }
  }
  if (existingDays.includes(days)) {
    return { days: null, error: `Срок ${days} ${durationUnit(days)} уже существует.` }
  }
  return { days, error: '' }
}
