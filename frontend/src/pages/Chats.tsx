import { useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { useChatMessages, useChats, useMarkChatRead, useSendChatMessage } from '../api/chats'
import { Icon } from '../components/Icon'
import { EmptyState, ErrorState, LoadingState, PageHeader } from '../components/ui'
import { formatDateTime } from '../utils/format'
import type { ChatMessage, ChatSummary } from '../types/api'

function chatTitle(chat: ChatSummary) {
  return chat.buyer_funpay_id ? `Покупатель #${chat.buyer_funpay_id}` : `Чат #${chat.funpay_chat_id}`
}

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
  const selectedId = parseConversationId(searchParams.get('chat'))
  const selectedChat = chatsQuery.data?.find((chat) => chat.id === selectedId) ?? null
  const messagesQuery = useChatMessages(selectedId)
  const markRead = useMarkChatRead()
  const markReadRef = useRef(markRead.mutate)
  markReadRef.current = markRead.mutate

  useEffect(() => {
    if (selectedChat && selectedChat.unread_count > 0) {
      markReadRef.current(selectedChat.id)
    }
  }, [selectedChat, messagesQuery.data?.length])

  const chats = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase('ru-RU')
    if (!needle) return chatsQuery.data ?? []
    return (chatsQuery.data ?? []).filter((chat) =>
      [chat.buyer_funpay_id, chat.funpay_order_id, chat.funpay_chat_id, chat.last_message_text]
        .some((value) => value?.toLocaleLowerCase('ru-RU').includes(needle)),
    )
  }, [chatsQuery.data, search])

  const selectChat = (conversation: ChatSummary) => {
    setSearchParams({ chat: String(conversation.id) })
    if (conversation.unread_count > 0) markRead.mutate(conversation.id)
  }

  return (
    <div className="page-stack chats-page">
      <PageHeader
        eyebrow="Коммуникации"
        title="Чаты"
        description="Входящие сообщения покупателей и ответы через подключённого FunPay-бота."
        actions={(
          <button className="button button--secondary" onClick={() => chatsQuery.refetch()} disabled={chatsQuery.isFetching}>
            <Icon name="refresh" />
            Обновить
          </button>
        )}
      />

      {chatsQuery.isLoading && <LoadingState label="Загружаем чаты" />}
      {chatsQuery.isError && <ErrorState message="Не удалось загрузить чаты" onRetry={() => chatsQuery.refetch()} />}
      {chatsQuery.data && chatsQuery.data.length === 0 && (
        <EmptyState
          icon="chat"
          title="Сообщений пока нет"
          description="Новая беседа появится здесь после первого входящего сообщения от покупателя в FunPay."
        />
      )}

      {chatsQuery.data && chatsQuery.data.length > 0 && (
        <div className={`chat-console ${selectedChat ? 'chat-console--thread-open' : ''}`}>
          <aside className="chat-inbox" aria-label="Список чатов">
            <div className="chat-inbox__toolbar">
              <label className="search-field chat-search">
                <Icon name="search" size={16} />
                <span className="sr-only">Найти чат</span>
                <input
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                  placeholder="Покупатель, заказ или сообщение"
                />
              </label>
              <span className="chat-inbox__count">{chats.length}</span>
            </div>

            <div className="chat-list">
              {chats.map((chat) => (
                <button
                  key={chat.id}
                  type="button"
                  className={`chat-list-item ${selectedId === chat.id ? 'chat-list-item--active' : ''}`}
                  onClick={() => selectChat(chat)}
                >
                  <span className="chat-list-item__avatar" aria-hidden="true">{chat.buyer_funpay_id?.slice(0, 1) ?? 'F'}</span>
                  <span className="chat-list-item__body">
                    <span className="chat-list-item__head">
                      <strong>{chatTitle(chat)}</strong>
                      <time dateTime={chat.last_message_at ?? undefined}>{shortTime(chat.last_message_at)}</time>
                    </span>
                    <span className="chat-list-item__meta">
                      {chat.funpay_order_id ? `Заказ #${chat.funpay_order_id}` : `FunPay chat ${chat.funpay_chat_id}`}
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
              ))}
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
                onBack={() => setSearchParams({})}
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
  const endRef = useRef<HTMLDivElement>(null)
  const lastMessageId = messages.at(-1)?.id

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' })
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
        <div className="chat-list-item__avatar" aria-hidden="true">{chat.buyer_funpay_id?.slice(0, 1) ?? 'F'}</div>
        <div>
          <strong>{chatTitle(chat)}</strong>
          <span>{chat.funpay_order_id ? `Заказ #${chat.funpay_order_id}` : `FunPay chat ${chat.funpay_chat_id}`}</span>
        </div>
      </header>

      <div className="chat-messages" role="log" aria-live="polite" aria-relevant="additions">
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
        <div ref={endRef} />
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
          rows={3}
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

function parseConversationId(value: string | null) {
  if (!value) return null
  const id = Number(value)
  return Number.isInteger(id) && id > 0 ? id : null
}
