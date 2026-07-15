type OrderedDuration = { id: number; minutes: number }

type TierSaleInput = {
  is_active: boolean
  is_sellable?: boolean
  funpay_supported: boolean
}

export type TierSaleControl = {
  checked: boolean
  disabled: boolean
  label: string
}

export type DurationInputMode = 'minutes' | 'hours' | 'days'

export type DurationMinutesValidation = {
  minutes: number | null
  error: string
}

const MAX_DURATION_MINUTES = 30 * 24 * 60

export function tierSaleControl(tier: TierSaleInput): TierSaleControl {
  if (!tier.funpay_supported) {
    return {
      checked: false,
      disabled: true,
      label: 'Не поддерживается FunPay',
    }
  }
  const checked = tier.is_sellable === true
  return {
    checked,
    disabled: !tier.is_active,
    label: checked ? 'Разрешена' : 'Запрещена',
  }
}

export function compareDurationsByMinutes(left: OrderedDuration, right: OrderedDuration) {
  return left.minutes - right.minutes || left.id - right.id
}

export function formatDurationMinutes(minutes: number) {
  if (!Number.isSafeInteger(minutes) || minutes <= 0) return 'Неизвестный срок'

  const days = Math.floor(minutes / (24 * 60))
  const hours = Math.floor((minutes % (24 * 60)) / 60)
  const remainingMinutes = minutes % 60
  const parts: string[] = []

  if (days > 0) parts.push(`${days} ${plural(days, 'день', 'дня', 'дней')}`)
  if (hours > 0) parts.push(`${hours} ${plural(hours, 'час', 'часа', 'часов')}`)
  if (remainingMinutes > 0) parts.push(`${remainingMinutes} ${plural(remainingMinutes, 'минута', 'минуты', 'минут')}`)

  return parts.join(' ')
}

export function validateDurationInput(
  mode: DurationInputMode,
  value: string,
  existingMinutes: number[],
): DurationMinutesValidation {
  const normalized = value.trim().replace(',', '.')
  if (!/^\d+(?:\.\d+)?$/.test(normalized)) {
    const unit = mode === 'minutes' ? 'минут' : mode === 'hours' ? 'часов' : 'дней'
    return { minutes: null, error: `Введите количество ${unit}.` }
  }
  const amount = Number(normalized)
  if (!Number.isFinite(amount)) return { minutes: null, error: 'Введите корректный срок.' }
  if ((mode === 'minutes' || mode === 'days') && !Number.isInteger(amount)) {
    return {
      minutes: null,
      error: mode === 'minutes'
        ? 'Минуты указываются целым числом с шагом 30.'
        : 'Для дней укажите целое число. Для более точного срока выберите часы.',
    }
  }
  const minutes = amount * (mode === 'minutes' ? 1 : mode === 'hours' ? 60 : 24 * 60)

  if (!Number.isSafeInteger(minutes) || minutes < 30 || minutes > MAX_DURATION_MINUTES) {
    return { minutes: null, error: 'Срок должен быть от 30 минут до 30 дней.' }
  }
  if (minutes % 30 !== 0) {
    return { minutes: null, error: 'Срок задаётся с шагом 30 минут. Для часов используйте целое значение или половину часа.' }
  }
  if (existingMinutes.includes(minutes)) {
    return { minutes, error: `Срок «${formatDurationMinutes(minutes)}» уже существует.` }
  }
  return { minutes, error: '' }
}

function plural(value: number, one: string, few: string, many: string) {
  const mod100 = Math.abs(value) % 100
  const mod10 = mod100 % 10
  if (mod100 >= 11 && mod100 <= 14) return many
  if (mod10 === 1) return one
  if (mod10 >= 2 && mod10 <= 4) return few
  return many
}
