import type { ChatSummary, ChatSaleOrder } from '../types/api'

type ChatIdentity = Pick<ChatSummary, 'buyer_funpay_id' | 'buyer_username' | 'funpay_chat_id'>
type ChatPresence = Pick<ChatSummary, 'buyer_is_online' | 'buyer_status_text'>

export interface PresencePresentation {
  label: string
  tone: 'online' | 'offline' | 'unknown'
}

export interface OrderListPresentation {
  label: string
  additionalCount: number
}

const SAFE_AVATAR_PROTOCOLS = new Set(['https:'])

export function chatTitle(chat: ChatIdentity): string {
  const username = chat.buyer_username?.trim()
  if (username) return username

  const buyerId = chat.buyer_funpay_id.trim()
  return `FunPay #${buyerId || chat.funpay_chat_id}`
}

export function buyerInitial(chat: ChatIdentity): string {
  const username = chat.buyer_username?.trim()
  if (!username) return 'F'
  return [...username][0]?.toLocaleUpperCase('ru-RU') ?? 'F'
}

export function buyerPresence(chat: ChatPresence): PresencePresentation {
  if (chat.buyer_is_online === true) {
    return { label: 'Онлайн', tone: 'online' }
  }

  const status = chat.buyer_status_text?.trim()
  if (status) {
    return { label: status, tone: 'offline' }
  }

  return { label: 'Статус не получен', tone: 'unknown' }
}

export function safeAvatarUrl(value: string | null): string | null {
  const candidate = value?.trim()
  if (!candidate) return null

  try {
    const normalized = candidate.startsWith('//') ? `https:${candidate}` : candidate
    const url = new URL(normalized)
    return SAFE_AVATAR_PROTOCOLS.has(url.protocol) ? url.toString() : null
  } catch {
    return null
  }
}

export function sortedSaleOrders(orders: readonly ChatSaleOrder[]): ChatSaleOrder[] {
  return [...orders].sort((left, right) => timestamp(right.created_at) - timestamp(left.created_at))
}

export function orderListPresentation(chat: Pick<ChatSummary, 'sale_orders' | 'funpay_chat_id'>): OrderListPresentation {
  const [primary, ...additional] = sortedSaleOrders(chat.sale_orders)
  if (!primary) {
    return { label: `Чат #${chat.funpay_chat_id}`, additionalCount: 0 }
  }

  return {
    label: orderLabel(primary.funpay_order_id),
    additionalCount: additional.length,
  }
}

export function orderLabel(funpayOrderId: string): string {
  return `Заказ #${normalizedOrderId(funpayOrderId)}`
}

export function funPayOrderUrl(funpayOrderId: string): string {
  return `https://funpay.com/orders/${encodeURIComponent(normalizedOrderId(funpayOrderId))}/`
}

export function chatMatchesSearch(chat: ChatSummary, rawSearch: string): boolean {
  const needle = normalizeSearch(rawSearch)
  if (!needle) return true

  const values = [
    chat.buyer_username,
    chat.buyer_funpay_id,
    chat.funpay_chat_id,
    chat.last_message_text,
    ...chat.sale_orders.flatMap((order) => [order.funpay_order_id, order.order_id?.toString() ?? null]),
  ]

  return values.some((value) => normalizeSearch(value).includes(needle))
}

export function parseConversationId(value: string | null): number | null {
  if (!value) return null
  const id = Number(value)
  return Number.isInteger(id) && id > 0 ? id : null
}

function normalizedOrderId(value: string): string {
  return value.trim().replace(/^#+/, '')
}

function normalizeSearch(value: string | null | undefined): string {
  return value?.trim().toLocaleLowerCase('ru-RU') ?? ''
}

function timestamp(value: string): number {
  const result = Date.parse(value)
  return Number.isNaN(result) ? 0 : result
}
