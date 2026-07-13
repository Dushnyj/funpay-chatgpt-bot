import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buyerInitial,
  buyerPresence,
  chatMatchesSearch,
  chatTitle,
  funPayOrderUrl,
  orderListPresentation,
  parseConversationId,
  safeAvatarUrl,
  sortedSaleOrders,
} from '../src/utils/chatPresentation.ts'
import type { ChatSummary } from '../src/types/api.ts'

function makeChat(overrides: Partial<ChatSummary> = {}): ChatSummary {
  return {
    id: 12,
    funpay_chat_id: '268814469',
    buyer_funpay_id: '16395457',
    buyer_username: 'BuyerLogin',
    buyer_avatar_url: 'https://cdn.example.test/avatar.png',
    buyer_is_online: false,
    buyer_status_text: 'Был в сети 5 минут назад',
    profile_checked_at: '2026-07-13T10:05:00Z',
    sale_orders: [
      { order_id: 31, funpay_order_id: 'HHHGNZ4N', status: 'paid', created_at: '2026-07-13T10:00:00Z' },
    ],
    unread_count: 2,
    last_message_text: 'Нужен новый код',
    last_message_direction: 'incoming',
    last_message_at: '2026-07-13T10:06:00Z',
    ...overrides,
  }
}

test('buyer title prefers a clean username and otherwise uses the FunPay id', () => {
  assert.equal(chatTitle(makeChat({ buyer_username: '  BuyerLogin  ' })), 'BuyerLogin')
  assert.equal(chatTitle(makeChat({ buyer_username: null })), 'FunPay #16395457')
  assert.equal(buyerInitial(makeChat({ buyer_username: 'ёжик' })), 'Ё')
  assert.equal(buyerInitial(makeChat({ buyer_username: null })), 'F')
})

test('presence distinguishes online, last seen and unavailable states', () => {
  assert.deepEqual(buyerPresence(makeChat({ buyer_is_online: true })), { label: 'Онлайн', tone: 'online' })
  assert.deepEqual(
    buyerPresence(makeChat({ buyer_is_online: false, buyer_status_text: '  Был вчера  ' })),
    { label: 'Был вчера', tone: 'offline' },
  )
  assert.deepEqual(
    buyerPresence(makeChat({ buyer_is_online: null, buyer_status_text: null })),
    { label: 'Статус не получен', tone: 'unknown' },
  )
})

test('avatar presentation only accepts HTTPS resources', () => {
  assert.equal(safeAvatarUrl('https://cdn.example.test/avatar.png'), 'https://cdn.example.test/avatar.png')
  assert.equal(safeAvatarUrl('//cdn.example.test/avatar.png'), 'https://cdn.example.test/avatar.png')
  assert.equal(safeAvatarUrl('http://cdn.example.test/avatar.png'), null)
  assert.equal(safeAvatarUrl('data:image/svg+xml;base64,broken'), null)
  assert.equal(safeAvatarUrl('javascript:alert(1)'), null)
})

test('orders are presented newest first without mutating the API payload', () => {
  const older = { order_id: 30, funpay_order_id: 'OLD111', status: 'closed', created_at: '2026-07-11T10:00:00Z' }
  const newer = { order_id: 31, funpay_order_id: 'NEW222', status: 'paid', created_at: '2026-07-13T10:00:00Z' }
  const chat = makeChat({ sale_orders: [older, newer] })

  assert.deepEqual(sortedSaleOrders(chat.sale_orders).map((order) => order.funpay_order_id), ['NEW222', 'OLD111'])
  assert.deepEqual(chat.sale_orders.map((order) => order.funpay_order_id), ['OLD111', 'NEW222'])
  assert.deepEqual(orderListPresentation(chat), { label: 'Заказ #NEW222', additionalCount: 1 })
  assert.equal(funPayOrderUrl('#NEW/222'), 'https://funpay.com/orders/NEW%2F222/')
})

test('chat search covers login, buyer id, chat id, sale ids and message text', () => {
  const chat = makeChat()

  for (const query of ['buyerlogin', '16395457', '268814469', 'hhhgnz4n', '31', 'НОВЫЙ КОД']) {
    assert.equal(chatMatchesSearch(chat, query), true, query)
  }
  assert.equal(chatMatchesSearch(chat, 'продавец'), false)
})

test('conversation query parameter accepts only positive integer ids', () => {
  assert.equal(parseConversationId('12'), 12)
  assert.equal(parseConversationId(null), null)
  assert.equal(parseConversationId('0'), null)
  assert.equal(parseConversationId('-1'), null)
  assert.equal(parseConversationId('1.5'), null)
  assert.equal(parseConversationId('not-a-number'), null)
})
