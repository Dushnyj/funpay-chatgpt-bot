import { useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { useChatMessages, useChats, useMarkChatRead, useSendChatMessage } from '../api/chats'
import { Icon } from '../components/Icon'
import { EmptyState, ErrorState, LoadingState, PageHeader } from '../components/ui'
import {
  buyerInitial,
  buyerPresence,
  chatMatchesSearch,
  chatTitle,
  funPayOrderUrl,
  orderLabel,
  orderListPresentation,
  parseConversationId,
  safeAvatarUrl,
  sortedSaleOrders,
} from '../utils/chatPresentation'
import { formatDateTime } from '../utils/format'
import type { ChatMessage, ChatSummary } from '../types/api'

function shortTime(value: string | null) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const now = new Date()
  if (date.toDateString() === now.toDateString()) {
    return new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit' }).format(date)
  }
  return new Intl.DateTimeFormat('ru-RU', { day: '2-digit', month: 'short' }).format(date)
}

export default function Chats() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [search, setSearch] = useState('')
  const chatsQuery = useChats()
  const requestedChat = searchParams.get('chat')
  const selectedId = parseConversationId(requestedChat)
  const buyerChats = useMemo(
    () => (chatsQuery.data ?? []).filter((chat) => chat.sale_orders.length > 0),
    [chatsQuery.data],
  )
  const selectedChat = buyerChats.find((chat) => chat.id === selectedId) ?? null
  const selectedChatId = selectedChat?.id ?? null
  const messagesQuery = useChatMessages(selectedChatId)
  const markRead = useMarkChatRead()
  const markReadRef = useRef(markRead.mutate)
  const markedUnreadRef = useRef<string | null>(null)
  markReadRef.current = markRead.mutate

  useEffect(() => {
    if (!requestedChat || chatsQuery.data === undefined || selectedChat) return
    const next = new URLSearchParams(searchParams)
    next.delete('chat')
    setSearchParams(next, { replace: true })
  }, [chatsQuery.data, requestedChat, searchParams, selectedChat, setSearchParams])

  const unreadKey = selectedChat && selectedChat.unread_count > 0
    ? `${selectedChat.id}:${selectedChat.unread_count}`
    : null

  useEffect(() => {
    if (!unreadKey || selectedChatId === null) {
      markedUnreadRef.current = null
      return
    }
    if (markedUnreadRef.current === unreadKey) return
    markedUnreadRef.current = unreadKey
    markReadRef.current(selectedChatId)
  }, [selectedChatId, unreadKey])

  const chats = useMemo(() => {
    return buyerChats.filter((chat) => chatMatchesSearch(chat, search))
  }, [buyerChats, search])

  const selectChat = (conversation: ChatSummary) => {
    const next = new URLSearchParams(searchParams)
    next.set('chat', String(conversation.id))
    setSearchParams(next)
  }

  return (
    <div className="page-stack chats-page">
      <PageHeader
        eyebrow="Коммуникации"
        title="Чаты"
        description="Только чаты покупателей, подтверждённые вашими продажами FunPay, и ответы через подключённого бота."
        actions={(
          <button className="button button--secondary" onClick={() => chatsQuery.refetch()} disabled={chatsQuery.isFetching}>
            <Icon name="refresh" />
            Обновить
          </button>
        )}
      />

      {chatsQuery.isLoading && <LoadingState label="Загружаем чаты" />}
      {chatsQuery.isError && <ErrorState message="Не удалось загрузить чаты" onRetry={() => chatsQuery.refetch()} />}
      {chatsQuery.data && buyerChats.length === 0 && (
        <EmptyState
          icon="chat"
          title="Чатов с покупателями пока нет"
          description="Здесь появятся только беседы пользователей, у которых есть подтверждённая покупка в ваших продажах FunPay."
        />
      )}

      {chatsQuery.data && buyerChats.length > 0 && (
        <div className={`chat-console ${selectedChat ? 'chat-console--thread-open' : ''}`}>
          <aside className="chat-inbox" aria-label="Список чатов">
            <div className="chat-inbox__toolbar">
              <label className="search-field chat-search">
                <Icon name="search" size={16} />
                <span className="sr-only">Найти чат</span>
                <input
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                  placeholder="Логин, ID, чат, заказ или сообщение"
                />
              </label>
              <span className="chat-inbox__count">{chats.length}</span>
            </div>

            <div className="chat-list">
              {chats.map((chat) => {
                const order = orderListPresentation(chat)
                return (
                  <button
                    key={chat.id}
                    type="button"
                    className={`chat-list-item ${selectedId === chat.id ? 'chat-list-item--active' : ''}`}
                    onClick={() => selectChat(chat)}
                  >
                    <BuyerAvatar chat={chat} />
                    <span className="chat-list-item__body">
                      <span className="chat-list-item__head">
                        <strong>{chatTitle(chat)}</strong>
                        <time dateTime={chat.last_message_at ?? undefined}>{shortTime(chat.last_message_at)}</time>
                      </span>
                      <span className="chat-list-item__meta">
                        <span className="chat-list-item__order">
                          {order.label}
                          {order.additionalCount > 0 && (
                            <span className="chat-list-item__order-more" aria-label={`Ещё заказов: ${order.additionalCount}`}>
                              +{order.additionalCount}
                            </span>
                          )}
                        </span>
                        <BuyerPresence chat={chat} />
                      </span>
                      <span className="chat-list-item__preview">
                        {chat.last_message_direction === 'outgoing' && <span aria-label="Ваш ответ">Вы: </span>}
                        {chat.last_message_text || 'Сообщение без текста'}
                      </span>
                    </span>
                    {chat.unread_count > 0 && (
                      <span className="unread-badge" aria-label={`Непрочитанных: ${chat.unread_count}`}>
                        {chat.unread_count > 99 ? '99+' : chat.unread_count}
                      </span>
                    )}
                  </button>
                )
              })}
              {chats.length === 0 && (
                <div className="chat-list-empty">Ничего не найдено</div>
              )}
            </div>
          </aside>

          <section className="chat-thread" aria-label="История переписки">
            {!selectedChat && (
              <EmptyState
                icon="chat"
                title="Выберите чат"
                description="Откройте беседу слева, чтобы прочитать историю и ответить покупателю."
              />
            )}
            {selectedChat && (
              <Conversation
                chat={selectedChat}
                messages={messagesQuery.data ?? []}
                isLoading={messagesQuery.isLoading}
                isError={messagesQuery.isError}
                onRetry={() => messagesQuery.refetch()}
                onBack={() => {
                  const next = new URLSearchParams(searchParams)
                  next.delete('chat')
                  setSearchParams(next)
                }}
              />
            )}
          </section>
        </div>
      )}
    </div>
  )
}

function Conversation({
  chat,
  messages,
  isLoading,
  isError,
  onRetry,
  onBack,
}: {
  chat: ChatSummary
  messages: ChatMessage[]
  isLoading: boolean
  isError: boolean
  onRetry: () => void
  onBack: () => void
}) {
  const [draft, setDraft] = useState('')
  const [sendError, setSendError] = useState('')
  const sendMessage = useSendChatMessage(chat.id)
  const messagesRef = useRef<HTMLDivElement>(null)
  const lastMessageId = messages.at(-1)?.id

  useEffect(() => {
    const container = messagesRef.current
    if (container) container.scrollTop = container.scrollHeight
  }, [chat.id, lastMessageId])

  useEffect(() => {
    setDraft('')
    setSendError('')
  }, [chat.id])

  const submit = async () => {
    const text = draft.trim()
    if (!text || sendMessage.isPending) return
    setSendError('')
    try {
      await sendMessage.mutateAsync(text)
      setDraft('')
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 503) {
        setSendError('FunPay-бот сейчас не подключён. Запустите интеграцию и повторите отправку.')
      } else {
        setSendError(cause instanceof ApiError ? cause.message : 'Не удалось отправить сообщение')
      }
    }
  }

  return (
    <>
      <header className="chat-thread__header">
        <button type="button" className="icon-button chat-thread__back" onClick={onBack} aria-label="Вернуться к списку чатов">
          <Icon name="arrow-right" className="chat-thread__back-icon" />
        </button>
        <BuyerAvatar chat={chat} />
        <div className="chat-thread__summary">
          <div className="chat-thread__buyer">
            <strong>{chatTitle(chat)}</strong>
            <BuyerPresence chat={chat} />
          </div>
          <nav className="chat-thread__orders" aria-label="Продажи покупателя">
            {sortedSaleOrders(chat.sale_orders).map((order) => (
              <a
                key={`${order.funpay_order_id}:${order.created_at}`}
                className="chat-order-chip"
                href={funPayOrderUrl(order.funpay_order_id)}
                target="_blank"
                rel="noopener noreferrer"
                title={`Открыть ${orderLabel(order.funpay_order_id)} в FunPay`}
              >
                {orderLabel(order.funpay_order_id)}
              </a>
            ))}
          </nav>
        </div>
      </header>

      <div ref={messagesRef} className="chat-messages" role="log" aria-live="polite" aria-relevant="additions">
        {isLoading && <LoadingState label="Загружаем историю" />}
        {isError && <ErrorState message="Не удалось загрузить историю" onRetry={onRetry} />}
        {!isLoading && !isError && messages.length === 0 && (
          <EmptyState icon="chat" title="История пуста" description="В этой беседе ещё нет сохранённых сообщений." />
        )}
        {messages.map((message) => (
          <article
            key={message.id}
            className={`message-row message-row--${message.direction}`}
            aria-label={message.direction === 'incoming' ? 'Сообщение покупателя' : 'Ваш ответ'}
          >
            <div className={`message-bubble message-bubble--${message.direction} ${message.delivery_status === 'failed' ? 'message-bubble--failed' : ''}`}>
              <p>{message.text || 'Сообщение без текста'}</p>
              <footer>
                <time dateTime={message.created_at}>{formatDateTime(message.created_at)}</time>
                {message.direction === 'outgoing' && <DeliveryStatus status={message.delivery_status} />}
              </footer>
            </div>
          </article>
        ))}
      </div>

      <form
        className="chat-composer"
        onSubmit={(event) => { event.preventDefault(); void submit() }}
      >
        {sendError && <div className="form-alert form-alert--error" role="alert">{sendError}</div>}
        <label htmlFor="chat-reply" className="sr-only">Ответ покупателю</label>
        <textarea
          id="chat-reply"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault()
              void submit()
            }
          }}
          rows={2}
          maxLength={4000}
          placeholder="Напишите ответ… Enter — отправить, Shift+Enter — новая строка"
        />
        <div className="chat-composer__footer">
          <span>{draft.length} / 4000</span>
          <button className="button button--primary" type="submit" disabled={!draft.trim() || sendMessage.isPending}>
            <Icon name="send" />
            {sendMessage.isPending ? 'Отправляем…' : 'Отправить'}
          </button>
        </div>
      </form>
    </>
  )
}

function DeliveryStatus({ status }: { status: ChatMessage['delivery_status'] }) {
  const labels: Record<ChatMessage['delivery_status'], string> = {
    received: 'Получено',
    pending: 'Отправляется',
    sent: 'Отправлено',
    failed: 'Не доставлено',
  }
  return <span className={`delivery-status delivery-status--${status}`}>{labels[status]}</span>
}

function BuyerAvatar({ chat }: { chat: ChatSummary }) {
  const avatarUrl = safeAvatarUrl(chat.buyer_avatar_url)
  const presence = buyerPresence(chat)
  return (
    <span className="chat-list-item__avatar buyer-avatar" aria-hidden="true">
      <span className="buyer-avatar__initial">{buyerInitial(chat)}</span>
      {avatarUrl && (
        <img
          key={avatarUrl}
          src={avatarUrl}
          alt=""
          loading="lazy"
          decoding="async"
          referrerPolicy="no-referrer"
          onError={(event) => { event.currentTarget.hidden = true }}
        />
      )}
      <span className={`buyer-avatar__presence buyer-avatar__presence--${presence.tone}`} />
    </span>
  )
}

function BuyerPresence({ chat }: { chat: ChatSummary }) {
  const presence = buyerPresence(chat)
  const checkedAt = chat.profile_checked_at ? `Профиль проверен: ${formatDateTime(chat.profile_checked_at)}` : undefined
  return (
    <span className={`buyer-presence buyer-presence--${presence.tone}`} title={checkedAt}>
      {presence.label}
    </span>
  )
}
