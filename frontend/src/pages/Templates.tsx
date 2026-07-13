import { useEffect, useMemo, useRef, useState } from 'react'
import { useBlocker } from 'react-router-dom'
import { ApiError } from '../api/client'
import { useLimitScopes, useTiers } from '../api/catalog'
import {
  useCreateLotTemplate,
  useDeleteLotTemplate,
  useLotTemplates,
  useResetLotTemplate,
  useResetTemplate,
  useTemplates,
  useUpdateLotTemplate,
  useUpdateTemplates,
} from '../api/templates'
import { Icon } from '../components/Icon'
import { EmptyState, ErrorState, LoadingState, ModalOverlay, PageHeader } from '../components/ui'
import type { LotTemplate, LotTemplateCreate, LotTemplateUpdate } from '../types/api'
import {
  extractTemplateFields,
  insertTemplateField,
  normalizeTemplateKey,
  renderTemplatePreview,
  templateKeyForName,
} from '../utils/templateEditor'
import '../styles/templates.css'

type TemplateSection = 'messages' | 'lots'
type Language = 'ru' | 'en'
type StateFilter = 'all' | 'custom' | 'system' | 'disabled'

type MessageMeta = {
  title: string
  description: string
  category: 'delivery' | 'access' | 'support' | 'replacement' | 'lifecycle'
}

const MESSAGE_META: Record<string, MessageMeta> = {
  welcome: { title: 'Выдача аккаунта', description: 'Данные после успешной оплаты', category: 'delivery' },
  order_confirmed: { title: 'Заказ подтверждён', description: 'Финальное сообщение покупателю', category: 'delivery' },
  no_account_available: { title: 'Нет свободного аккаунта', description: 'Выдача временно невозможна', category: 'delivery' },
  code_success: { title: 'Код входа', description: 'Ответ на команду получения кода', category: 'access' },
  code_expired: { title: 'Доступ завершён', description: 'Аренда истекла или не найдена', category: 'access' },
  code_rate_limited: { title: 'Запрос кода ограничен', description: 'Повторный запрос сделан слишком рано', category: 'access' },
  rental_ambiguous: { title: 'Несколько заказов', description: 'Нужно выбрать конкретный заказ', category: 'access' },
  email_code_success: { title: 'Код из почты', description: 'Код подтверждения получен из почты', category: 'access' },
  email_code_duplicate: { title: 'Код уже отправлен', description: 'Покупатель повторно запросил тот же код', category: 'access' },
  email_code_not_found: { title: 'Код не найден', description: 'Новое письмо с кодом пока не пришло', category: 'access' },
  email_code_unavailable: { title: 'Почта недоступна', description: 'Автоматическое чтение почты не настроено', category: 'access' },
  help: { title: 'Справка', description: 'Команды, доступные покупателю', category: 'support' },
  seller_called: { title: 'Продавец вызван', description: 'Подтверждение вызова продавца', category: 'support' },
  replace_success: { title: 'Замена выполнена', description: 'Новые данные после замены', category: 'replacement' },
  replace_declined: { title: 'Замена не нужна', description: 'Аккаунт работает корректно', category: 'replacement' },
  replace_no_account: { title: 'Нет аккаунта для замены', description: 'Свободная замена не найдена', category: 'replacement' },
  subscription: { title: 'Статус подписки', description: 'Тариф, срок и актуальные лимиты', category: 'lifecycle' },
  expiry: { title: 'Аренда завершена', description: 'Уведомление об окончании доступа', category: 'lifecycle' },
  disconnect: { title: 'Временное отключение', description: 'Сессия аккаунта завершена', category: 'lifecycle' },
}

const CATEGORY_LABELS: Record<string, string> = {
  all: 'Все сценарии',
  delivery: 'Выдача',
  access: 'Вход и код',
  support: 'Поддержка',
  replacement: 'Замена',
  lifecycle: 'Срок доступа',
  general: 'Общие',
  tier: 'По тарифу',
  scope: 'По лимиту',
  exact: 'Тариф + лимит',
}

const REQUIRED_FIELDS: Record<string, string[]> = {
  welcome: ['login', 'password'],
  replace_success: ['login', 'password'],
  code_success: ['code'],
  email_code_success: ['email_code'],
}

const SAMPLE_VALUES: Record<string, string> = {
  tier: 'Plus',
  plan: 'Plus',
  days: '7',
  duration: '7 дней',
  condition: 'Codex от 70%',
  long_window_days: '7',
  short_limit: '91%',
  long_limit: '79%',
  scope: 'Codex',
  limit_scope: 'Codex',
  price: '799 ₽',
  login: 'buyer@example.com',
  password: '••••••••••',
  expires_at: '20.07.2026 10:30',
  expires_in: '6 дн. 23 ч.',
  code: '482 913',
  email_code: '731 204',
  retry_in_sec: '24',
  retry_minutes: '5',
  chat_5h: '82',
  chat_weekly: '64',
  codex_5h: '82',
  codex_weekly: '64',
  codex_primary_limit: '79%',
  codex_primary_window: '7 дн.',
  codex_primary_reset: '17.07.2026 10:30 UTC',
  codex_secondary_limit: '91%',
  codex_secondary_window: '5 ч',
  codex_secondary_reset: '13.07.2026 15:30 UTC',
  min_limit: '70%',
  max_5h: '100%',
  max_weekly: '100%',
}

const VARIABLE_LABELS: Record<string, string> = {
  tier: 'тариф',
  plan: 'тариф',
  days: 'дней аренды',
  duration: 'срок аренды',
  condition: 'условия лота',
  long_window_days: 'дней в длинном окне',
  short_limit: 'лимит короткого окна',
  long_limit: 'лимит длинного окна',
  scope: 'тип лимита',
  limit_scope: 'тип лимита',
  price: 'цена',
  login: 'логин',
  password: 'пароль',
  expires_at: 'дата окончания',
  expires_in: 'осталось времени',
  code: 'одноразовый код',
  email_code: 'код из письма',
  retry_in_sec: 'повтор через, сек.',
  retry_minutes: 'ожидание, мин.',
  chat_5h: 'Chat, 5 ч',
  chat_weekly: 'Chat, 7 дней',
  codex_primary_limit: 'Codex, остаток',
  codex_primary_window: 'Codex, окно',
  codex_primary_reset: 'Codex, сброс',
  codex_secondary_limit: 'Codex доп., остаток',
  codex_secondary_window: 'Codex доп., окно',
  codex_secondary_reset: 'Codex доп., сброс',
}

const messageId = (key: string, lang: string) => `${key}:${lang}`

const toLotDraft = (template: LotTemplate): LotTemplateUpdate => ({
  title_ru: template.title_ru,
  title_en: template.title_en,
  description_ru: template.description_ru,
  description_en: template.description_en,
  enabled: template.enabled,
})

const lotCategory = (template: Pick<LotTemplate, 'tier_id' | 'limit_scope_id'>) => {
  if (template.tier_id && template.limit_scope_id) return 'exact'
  if (template.tier_id) return 'tier'
  if (template.limit_scope_id) return 'scope'
  return 'general'
}

function MutationAlert({ error, saved }: { error: string; saved: string }) {
  if (error) return <div className="form-alert form-alert--error" role="alert"><Icon name="warning" /><span>{error}</span></div>
  if (saved) return <div className="form-alert form-alert--success" role="status"><Icon name="check" /><span>{saved}</span></div>
  return null
}

function MessagePreview({ content, language }: { content: string; language: Language }) {
  return (
    <div className="templates-preview-card templates-preview-card--chat">
      <div className="templates-preview-card__top">
        <span className="templates-avatar">F</span>
        <span><strong>FunPay Rental</strong><small>только что · {language.toUpperCase()}</small></span>
      </div>
      <div className="templates-chat-bubble">
        {renderTemplatePreview(content, SAMPLE_VALUES) || <span className="muted">Введите текст сообщения</span>}
      </div>
    </div>
  )
}

function LotPreview({ title, description, enabled }: { title: string; description: string; enabled: boolean }) {
  return (
    <div className={`templates-preview-card templates-preview-card--lot ${enabled ? '' : 'is-disabled'}`}>
      <div className="templates-lot-cover"><img src="/app-icon.png" alt="Иконка сервиса FunPay Rental" /><small>Доступ к аккаунту</small></div>
      <div className="templates-lot-body">
        <div className="templates-lot-state"><span className={`status-dot ${enabled ? 'status-dot--success' : 'status-dot--warning'}`} />{enabled ? 'Будет опубликован' : 'Отключён'}</div>
        <strong>{renderTemplatePreview(title, SAMPLE_VALUES) || 'Название лота'}</strong>
        <p>{renderTemplatePreview(description, SAMPLE_VALUES) || 'Описание появится здесь.'}</p>
        <div><b>799 ₽</b><span>Моментальная выдача</span></div>
      </div>
    </div>
  )
}

export default function Templates() {
  const templatesQuery = useTemplates()
  const lotsQuery = useLotTemplates()
  const tiersQuery = useTiers()
  const scopesQuery = useLimitScopes()
  const updateTemplates = useUpdateTemplates()
  const resetTemplate = useResetTemplate()
  const updateLot = useUpdateLotTemplate()
  const resetLot = useResetLotTemplate()
  const createLot = useCreateLotTemplate()
  const deleteLot = useDeleteLotTemplate()

  const [section, setSection] = useState<TemplateSection>('messages')
  const [language, setLanguage] = useState<Language>('ru')
  const [search, setSearch] = useState('')
  const [category, setCategory] = useState('all')
  const [stateFilter, setStateFilter] = useState<StateFilter>('all')
  const [selectedMessageKey, setSelectedMessageKey] = useState('')
  const [selectedLotKey, setSelectedLotKey] = useState('')
  const [messageDrafts, setMessageDrafts] = useState<Record<string, string>>({})
  const [messageDirty, setMessageDirty] = useState<Set<string>>(() => new Set())
  const [lotDrafts, setLotDrafts] = useState<Record<string, LotTemplateUpdate>>({})
  const [lotDirty, setLotDirty] = useState<Set<string>>(() => new Set())
  const [lotInsertTarget, setLotInsertTarget] = useState<'title' | 'description'>('description')
  const [error, setError] = useState('')
  const [saved, setSaved] = useState('')
  const [batchSaving, setBatchSaving] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [createKeyTouched, setCreateKeyTouched] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [pendingSection, setPendingSection] = useState<TemplateSection | null>(null)
  const [createDraft, setCreateDraft] = useState<LotTemplateCreate>({
    key: '',
    name: '',
    tier_id: null,
    limit_scope_id: null,
    title_ru: 'ChatGPT {plan} на {days} дн. — {condition}',
    title_en: 'ChatGPT {plan} for {days} days — {condition}',
    description_ru: 'Доступ к ChatGPT {plan}. Длинное окно: {long_window_days} дн., остаток от {min_limit}. Моментальная выдача.',
    description_en: 'ChatGPT {plan} access. Long window: {long_window_days} days, at least {min_limit} remaining. Instant delivery.',
    enabled: true,
  })
  const messageEditorRef = useRef<HTMLTextAreaElement>(null)
  const lotTitleRef = useRef<HTMLInputElement>(null)
  const lotDescriptionRef = useRef<HTMLTextAreaElement>(null)

  const templates = useMemo(() => templatesQuery.data ?? [], [templatesQuery.data])
  const lots = useMemo(() => lotsQuery.data ?? [], [lotsQuery.data])

  useEffect(() => {
    if (templates.length === 0) return
    setMessageDrafts((current) => {
      const next = { ...current }
      templates.forEach((template) => {
        const id = messageId(template.key, template.lang)
        if (!messageDirty.has(id)) next[id] = template.content
      })
      return next
    })
    if (!selectedMessageKey) setSelectedMessageKey(templates[0].key)
  }, [messageDirty, selectedMessageKey, templates])

  useEffect(() => {
    if (lots.length === 0) return
    setLotDrafts((current) => {
      const next = { ...current }
      lots.forEach((template) => {
        if (!lotDirty.has(template.key)) next[template.key] = toLotDraft(template)
      })
      return next
    })
    if (!selectedLotKey) setSelectedLotKey(lots[0].key)
  }, [lotDirty, lots, selectedLotKey])

  const messageKeys = useMemo(() => [...new Set(templates.map((template) => template.key))], [templates])
  const filteredMessageKeys = useMemo(() => messageKeys.filter((key) => {
    const meta = MESSAGE_META[key] ?? { title: key, description: 'Системное сообщение', category: 'support' as const }
    const variants = templates.filter((template) => template.key === key)
    const normalizedSearch = search.trim().toLowerCase()
    const matchesSearch = `${key} ${meta.title} ${meta.description}`.toLowerCase().includes(normalizedSearch)
    const matchesCategory = category === 'all' || meta.category === category
    const matchesState = stateFilter === 'all'
      || (stateFilter === 'custom' && variants.some((item) => item.is_custom))
      || (stateFilter === 'system' && variants.every((item) => !item.is_custom))
    return matchesSearch && matchesCategory && matchesState
  }), [category, messageKeys, search, stateFilter, templates])

  const filteredLots = useMemo(() => lots.filter((template) => {
    const normalizedSearch = search.trim().toLowerCase()
    const matchesSearch = `${template.key} ${template.name}`.toLowerCase().includes(normalizedSearch)
    const matchesCategory = category === 'all' || lotCategory(template) === category
    const matchesState = stateFilter === 'all'
      || (stateFilter === 'custom' && (template.is_custom || !template.system_managed))
      || (stateFilter === 'system' && template.system_managed && !template.is_custom)
      || (stateFilter === 'disabled' && !template.enabled)
    return matchesSearch && matchesCategory && matchesState
  }), [category, lots, search, stateFilter])

  const activeMessage = templates.find((item) => item.key === selectedMessageKey && item.lang === language)
    ?? templates.find((item) => item.key === selectedMessageKey)
  const activeMessageId = activeMessage ? messageId(activeMessage.key, activeMessage.lang) : ''
  const messageContent = activeMessage ? messageDrafts[activeMessageId] ?? activeMessage.content : ''
  const messageFields = activeMessage?.allowed_fields ?? []
  const messageUsedFields = extractTemplateFields(messageContent)
  const messageUnknownFields = messageUsedFields.filter((field) => !messageFields.includes(field))
  const messageMissingFields = (REQUIRED_FIELDS[selectedMessageKey] ?? []).filter((field) => !messageUsedFields.includes(field))

  const activeLot = lots.find((item) => item.key === selectedLotKey)
  const activeLotDraft = activeLot ? lotDrafts[activeLot.key] ?? toLotDraft(activeLot) : null
  const lotTitleField = language === 'ru' ? 'title_ru' : 'title_en'
  const lotDescriptionField = language === 'ru' ? 'description_ru' : 'description_en'
  const lotTitle = activeLotDraft?.[lotTitleField] ?? ''
  const lotDescription = activeLotDraft?.[lotDescriptionField] ?? ''
  const lotUsedFields = extractTemplateFields(`${lotTitle}\n${lotDescription}`)
  const lotTitleFields = extractTemplateFields(lotTitle)
  const lotMissingTitleFields = ['plan', 'days', 'condition'].filter((field) => !lotTitleFields.includes(field))
  const lotUnknownFields = lotUsedFields.filter((field) => !(activeLot?.allowed_fields ?? []).includes(field))

  const activeDirtyCount = section === 'messages' ? messageDirty.size : lotDirty.size
  const totalDirtyCount = messageDirty.size + lotDirty.size
  const navigationBlocker = useBlocker(totalDirtyCount > 0)
  const isSaving = batchSaving
    || (section === 'messages' ? updateTemplates.isPending : updateLot.isPending)
  const editorBusy = isSaving || resetTemplate.isPending || resetLot.isPending || deleteLot.isPending

  const clearFeedback = () => {
    setError('')
    setSaved('')
  }

  const changeMessage = (value: string) => {
    if (!activeMessageId) return
    setMessageDrafts((current) => ({ ...current, [activeMessageId]: value }))
    setMessageDirty((current) => new Set(current).add(activeMessageId))
    clearFeedback()
  }

  const changeLot = <K extends keyof LotTemplateUpdate>(field: K, value: LotTemplateUpdate[K]) => {
    if (!activeLot) return
    setLotDrafts((current) => ({ ...current, [activeLot.key]: { ...(current[activeLot.key] ?? toLotDraft(activeLot)), [field]: value } }))
    setLotDirty((current) => new Set(current).add(activeLot.key))
    clearFeedback()
  }

  const insertMessageVariable = (field: string) => {
    const textarea = messageEditorRef.current
    const insertion = insertTemplateField(messageContent, field, textarea?.selectionStart, textarea?.selectionEnd)
    changeMessage(insertion.value)
    requestAnimationFrame(() => {
      textarea?.focus()
      textarea?.setSelectionRange(insertion.cursor, insertion.cursor)
    })
  }

  const insertLotVariable = (field: string) => {
    const input = lotInsertTarget === 'title' ? lotTitleRef.current : lotDescriptionRef.current
    const source = lotInsertTarget === 'title' ? lotTitle : lotDescription
    const insertion = insertTemplateField(source, field, input?.selectionStart ?? undefined, input?.selectionEnd ?? undefined)
    changeLot(lotInsertTarget === 'title' ? lotTitleField : lotDescriptionField, insertion.value)
    requestAnimationFrame(() => {
      input?.focus()
      input?.setSelectionRange(insertion.cursor, insertion.cursor)
    })
  }

  const saveActiveSection = async (): Promise<boolean> => {
    if (batchSaving) return false
    clearFeedback()
    setBatchSaving(true)
    try {
      if (section === 'messages') {
        const items = templates
          .filter((template) => messageDirty.has(messageId(template.key, template.lang)))
          .map((template) => ({
            key: template.key,
            lang: template.lang,
            content: messageDrafts[messageId(template.key, template.lang)] ?? template.content,
          }))
        await updateTemplates.mutateAsync(items)
        setMessageDirty(new Set())
        setSaved(`Сохранено сообщений: ${items.length}.`)
      } else {
        const changed = lots.filter((template) => lotDirty.has(template.key))
        // One mutation observer cannot reliably expose the pending state of
        // several concurrent mutateAsync calls. Keep saves sequential so the
        // editor remains locked until every requested template is durable.
        for (const template of changed) {
          await updateLot.mutateAsync({
            key: template.key,
            body: lotDrafts[template.key] ?? toLotDraft(template),
          })
        }
        setLotDirty(new Set())
        setSaved(`Сохранено шаблонов лотов: ${changed.length}. Публикации обновятся при ближайшей синхронизации.`)
      }
      return true
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Не удалось сохранить изменения')
      return false
    } finally {
      setBatchSaving(false)
    }
  }

  const discardActiveSection = () => {
    clearFeedback()
    if (section === 'messages') {
      setMessageDrafts(Object.fromEntries(templates.map((template) => [messageId(template.key, template.lang), template.content])))
      setMessageDirty(new Set())
    } else {
      setLotDrafts(Object.fromEntries(lots.map((template) => [template.key, toLotDraft(template)])))
      setLotDirty(new Set())
    }
  }

  const resetCurrent = async () => {
    clearFeedback()
    try {
      if (section === 'messages' && activeMessage) {
        const reset = await resetTemplate.mutateAsync({ key: activeMessage.key, lang: activeMessage.lang })
        setMessageDrafts((current) => ({ ...current, [activeMessageId]: reset.content }))
        setMessageDirty((current) => {
          const next = new Set(current)
          next.delete(activeMessageId)
          return next
        })
        setSaved('Вернули стандартный текст сообщения.')
      } else if (section === 'lots' && activeLot) {
        const reset = await resetLot.mutateAsync(activeLot.key)
        setLotDrafts((current) => ({ ...current, [activeLot.key]: toLotDraft(reset) }))
        setLotDirty((current) => {
          const next = new Set(current)
          next.delete(activeLot.key)
          return next
        })
        setSaved('Вернули стандартный шаблон лота.')
      }
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Не удалось сбросить шаблон')
    }
  }

  const submitCreateLot = async () => {
    clearFeedback()
    const body = { ...createDraft, key: normalizeTemplateKey(createDraft.key || createDraft.name) }
    if (!/^[a-z0-9][a-z0-9_-]{1,63}$/.test(body.key) || !body.name.trim() || !body.title_ru.trim() || !body.title_en.trim()) {
      setError('Укажите ключ, название и заголовки на двух языках.')
      return
    }
    const targetExists = lots.some((template) => !template.system_managed
      && template.tier_id === body.tier_id
      && template.limit_scope_id === body.limit_scope_id)
    if (targetExists) {
      setError('Для выбранного тарифа и типа лимита уже есть пользовательский шаблон.')
      return
    }
    try {
      const created = await createLot.mutateAsync(body)
      setSelectedLotKey(created.key)
      setCreateOpen(false)
      setCreateKeyTouched(false)
      setCreateDraft((current) => ({ ...current, key: '', name: '', tier_id: null, limit_scope_id: null }))
      setSaved(`Шаблон «${created.name}» создан.`)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Не удалось создать шаблон лота')
    }
  }

  const confirmDeleteLot = async () => {
    if (!activeLot) return
    clearFeedback()
    try {
      await deleteLot.mutateAsync(activeLot.key)
      setLotDirty((current) => {
        const next = new Set(current)
        next.delete(activeLot.key)
        return next
      })
      setSelectedLotKey(lots.find((item) => item.key !== activeLot.key)?.key ?? '')
      setDeleteOpen(false)
      setSaved(`Шаблон «${activeLot.name}» удалён.`)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Не удалось удалить шаблон лота')
    }
  }

  const switchSection = (next: TemplateSection) => {
    setSection(next)
    setSearch('')
    setCategory('all')
    setStateFilter('all')
    clearFeedback()
  }

  const requestSectionSwitch = (next: TemplateSection) => {
    if (next === section) return
    if (activeDirtyCount > 0) {
      setPendingSection(next)
      return
    }
    switchSection(next)
  }

  useEffect(() => {
    if (totalDirtyCount === 0) return
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warnBeforeUnload)
    return () => {
      window.removeEventListener('beforeunload', warnBeforeUnload)
    }
  }, [totalDirtyCount])

  const cancelDeparture = () => {
    setPendingSection(null)
    if (navigationBlocker.state === 'blocked') navigationBlocker.reset()
  }

  const continueDeparture = () => {
    const sectionTarget = pendingSection
    setPendingSection(null)
    if (navigationBlocker.state === 'blocked') navigationBlocker.proceed()
    else if (sectionTarget) switchSection(sectionTarget)
  }

  const saveAndContinue = async () => {
    if (await saveActiveSection()) {
      continueDeparture()
    }
  }

  const discardAndContinue = () => {
    discardActiveSection()
    continueDeparture()
  }

  const activeQuery = section === 'messages' ? templatesQuery : lotsQuery
  if (activeQuery.isLoading) return <LoadingState label={section === 'messages' ? 'Загружаем сообщения' : 'Загружаем шаблоны лотов'} />
  if (activeQuery.isError) return <ErrorState onRetry={() => activeQuery.refetch()} />

  const sectionIsEmpty = section === 'messages' ? templates.length === 0 : lots.length === 0
  const currentCanReset = section === 'messages'
    ? Boolean(activeMessage?.default_content != null && (activeMessage.is_custom || messageDirty.has(activeMessageId)))
    : Boolean(activeLot?.system_managed && (activeLot.is_custom || lotDirty.has(activeLot.key)))

  return (
    <div className="page-stack templates-page">
      <PageHeader
        eyebrow="Коммуникации"
        title="Шаблоны"
        description="Единая точка управления сообщениями покупателю и оформлением автоматически создаваемых лотов."
        actions={(
          <div className="header-action-group">
            <button className="button button--secondary" onClick={discardActiveSection} disabled={activeDirtyCount === 0 || editorBusy}>Отменить</button>
            <button className="button button--primary" onClick={saveActiveSection} disabled={activeDirtyCount === 0 || editorBusy}>
              {isSaving ? <><span className="spinner spinner--light" />Сохраняем…</> : <><Icon name="check" />Сохранить{activeDirtyCount > 0 ? ` · ${activeDirtyCount}` : ''}</>}
            </button>
          </div>
        )}
      />

      <div className="templates-section-bar">
        <div className="templates-tabs" role="tablist" aria-label="Типы шаблонов">
          <button className={section === 'messages' ? 'active' : ''} role="tab" aria-selected={section === 'messages'} onClick={() => requestSectionSwitch('messages')} disabled={editorBusy}>
            <Icon name="templates" />Сообщения<span>{messageKeys.length}</span>
          </button>
          <button className={section === 'lots' ? 'active' : ''} role="tab" aria-selected={section === 'lots'} onClick={() => requestSectionSwitch('lots')} disabled={editorBusy}>
            <Icon name="lots" />Лоты<span>{lots.length}</span>
          </button>
        </div>
        {section === 'lots' && <button className="button button--secondary" onClick={() => setCreateOpen(true)} disabled={editorBusy}><Icon name="plus" />Новый шаблон</button>}
      </div>

      <MutationAlert error={error} saved={saved} />

      {sectionIsEmpty ? (
        <section className="panel">
          <EmptyState
            icon={section === 'messages' ? 'templates' : 'lots'}
            title={section === 'messages' ? 'Сообщения не инициализированы' : 'Шаблонов лотов пока нет'}
            description={section === 'messages' ? 'Перезапустите инициализацию стандартных данных на сервере.' : 'Создайте первый шаблон для автоматического оформления объявлений.'}
            action={section === 'lots' ? <button className="button button--primary" onClick={() => setCreateOpen(true)}><Icon name="plus" />Создать шаблон</button> : undefined}
          />
        </section>
      ) : (
        <section className="templates-workbench">
          <aside className="templates-library" aria-label="Библиотека шаблонов">
            <div className="templates-library__heading">
              <div><span>{section === 'messages' ? 'Сценарии' : 'Оформление'}</span><strong>{section === 'messages' ? filteredMessageKeys.length : filteredLots.length}</strong></div>
              <small>{section === 'messages' ? 'RU · EN' : 'По приоритету'}</small>
            </div>
            <label className="templates-search"><Icon name="search" size={16} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Найти шаблон" aria-label="Поиск шаблонов" /></label>
            <div className="templates-filters">
              <label><span className="sr-only">Категория</span><select value={category} onChange={(event) => setCategory(event.target.value)}>
                <option value="all">Все категории</option>
                {(section === 'messages'
                  ? ['delivery', 'access', 'support', 'replacement', 'lifecycle']
                  : ['general', 'tier', 'scope', 'exact'])
                  .map((value) => <option key={value} value={value}>{CATEGORY_LABELS[value]}</option>)}
              </select></label>
              <label><span className="sr-only">Состояние</span><select value={stateFilter} onChange={(event) => setStateFilter(event.target.value as StateFilter)}>
                <option value="all">Любое состояние</option>
                <option value="custom">Изменённые</option>
                <option value="system">Стандартные</option>
                {section === 'lots' && <option value="disabled">Отключённые</option>}
              </select></label>
            </div>
            <div className="templates-library__items">
              {section === 'messages' ? filteredMessageKeys.map((key) => {
                const meta = MESSAGE_META[key] ?? { title: key, description: 'Системное сообщение', category: 'support' as const }
                const variants = templates.filter((template) => template.key === key)
                const customized = variants.some((template) => template.is_custom || messageDirty.has(messageId(template.key, template.lang)))
                const complete = variants.some((template) => template.lang === 'ru') && variants.some((template) => template.lang === 'en')
                return (
                  <button key={key} className={selectedMessageKey === key ? 'active' : ''} onClick={() => setSelectedMessageKey(key)} disabled={editorBusy}>
                    <span className="templates-library__icon"><Icon name="chat" size={16} /></span>
                    <span><strong>{meta.title}</strong><small>{meta.description}</small></span>
                    <span className={`templates-state-mark ${customized ? 'is-custom' : ''}`}>{customized ? 'Изм.' : complete ? 'RU · EN' : 'Неполный'}</span>
                  </button>
                )
              }) : filteredLots.map((template) => (
                <button key={template.key} className={selectedLotKey === template.key ? 'active' : ''} onClick={() => setSelectedLotKey(template.key)} disabled={editorBusy}>
                  <span className="templates-library__icon"><Icon name="lots" size={16} /></span>
                  <span><strong>{template.name}</strong><small>{CATEGORY_LABELS[lotCategory(template)]} · {template.key}</small></span>
                  <span className={`templates-state-mark ${!template.enabled ? 'is-disabled' : template.is_custom || lotDirty.has(template.key) ? 'is-custom' : ''}`}>
                    {!template.enabled ? 'Выкл.' : !template.system_managed ? 'Польз.' : template.is_custom || lotDirty.has(template.key) ? 'Изм.' : 'Станд.'}
                  </span>
                </button>
              ))}
              {(section === 'messages' ? filteredMessageKeys.length : filteredLots.length) === 0 && <div className="templates-library__empty">По выбранным фильтрам ничего не найдено.</div>}
            </div>
          </aside>

          <div className="templates-editor">
            <div className="templates-editor__header">
              <div>
                <span className="eyebrow">{section === 'messages' ? selectedMessageKey : activeLot?.key}</span>
                <div className="templates-editor__title-row">
                  <h2>{section === 'messages' ? (MESSAGE_META[selectedMessageKey]?.title ?? selectedMessageKey) : activeLot?.name}</h2>
                  {section === 'lots' && activeLot && <span className={`templates-inline-state ${activeLotDraft?.enabled ? 'is-on' : ''}`}>{activeLotDraft?.enabled ? 'Активен' : 'Отключён'}</span>}
                </div>
                <p>{section === 'messages' ? MESSAGE_META[selectedMessageKey]?.description : activeLot ? `${CATEGORY_LABELS[lotCategory(activeLot)]}. Более точный шаблон применяется раньше общего.` : ''}</p>
              </div>
              <div className="templates-editor__head-actions">
                <button className="templates-icon-button" onClick={resetCurrent} disabled={!currentCanReset || editorBusy} title="Вернуть стандартный вариант"><Icon name="refresh" size={16} /><span>Сбросить</span></button>
                {section === 'lots' && activeLot && !activeLot.system_managed && <button className="templates-icon-button templates-icon-button--danger" onClick={() => setDeleteOpen(true)} disabled={editorBusy} title="Удалить пользовательский шаблон"><Icon name="trash" size={16} /><span>Удалить</span></button>}
                <div className="templates-language" role="tablist" aria-label="Язык шаблона">
                  <button className={language === 'ru' ? 'active' : ''} onClick={() => setLanguage('ru')} role="tab" aria-selected={language === 'ru'} disabled={editorBusy}>RU</button>
                  <button className={language === 'en' ? 'active' : ''} onClick={() => setLanguage('en')} role="tab" aria-selected={language === 'en'} disabled={editorBusy}>EN</button>
                </div>
              </div>
            </div>

            <div className="templates-editor__body">
              <div className="templates-edit-pane">
                {section === 'messages' ? (
                  <>
                    <label className="templates-field">
                      <span><b>Текст сообщения</b><small>{messageContent.length} / 4000</small></span>
                      <textarea ref={messageEditorRef} value={messageContent} onChange={(event) => changeMessage(event.target.value)} rows={14} maxLength={4000} spellCheck="true" disabled={editorBusy} />
                    </label>
                    <div className="templates-variables">
                      <div><strong>Переменные</strong><span>Нажмите, чтобы вставить в позицию курсора</span></div>
                      <div>{messageFields.length > 0 ? messageFields.map((field) => <button key={field} className={messageUsedFields.includes(field) ? 'is-used' : ''} onClick={() => insertMessageVariable(field)} disabled={editorBusy} title={VARIABLE_LABELS[field] ?? field}><code>{`{${field}}`}</code><span>{VARIABLE_LABELS[field] ?? field}</span></button>) : <small>Для этого сообщения переменные не нужны.</small>}</div>
                    </div>
                    {messageMissingFields.length > 0 && <div className="templates-validation is-error"><Icon name="warning" size={15} /><span>Обязательные переменные: {messageMissingFields.map((field) => `{${field}}`).join(', ')}</span></div>}
                    {messageUnknownFields.length > 0 && <div className="templates-validation is-error"><Icon name="warning" size={15} /><span>Не поддерживаются: {messageUnknownFields.map((field) => `{${field}}`).join(', ')}</span></div>}
                  </>
                ) : activeLot && activeLotDraft ? (
                  <>
                    <div className="templates-lot-settings">
                      {activeLot.system_managed
                        ? <span className="templates-system-required"><Icon name="shield" size={14} />Базовый шаблон всегда активен</span>
                        : <label className="templates-switch"><input type="checkbox" checked={activeLotDraft.enabled} onChange={(event) => changeLot('enabled', event.target.checked)} disabled={editorBusy} /><span /><b>Использовать при генерации лотов</b></label>}
                      <span>{CATEGORY_LABELS[lotCategory(activeLot)]}</span>
                    </div>
                    <label className="templates-field">
                      <span><b>Название лота</b><small>{lotTitle.length} / 255</small></span>
                      <input ref={lotTitleRef} value={lotTitle} onFocus={() => setLotInsertTarget('title')} onChange={(event) => changeLot(lotTitleField, event.target.value)} maxLength={255} disabled={editorBusy} />
                    </label>
                    <label className="templates-field templates-field--description">
                      <span><b>Описание</b><small>{lotDescription.length} / 4000</small></span>
                      <textarea ref={lotDescriptionRef} value={lotDescription} onFocus={() => setLotInsertTarget('description')} onChange={(event) => changeLot(lotDescriptionField, event.target.value)} rows={10} maxLength={4000} spellCheck="true" disabled={editorBusy} />
                    </label>
                    <div className="templates-variables">
                      <div><strong>Переменные</strong><span>Вставка в поле «{lotInsertTarget === 'title' ? 'Название' : 'Описание'}»</span></div>
                      <div>{activeLot.allowed_fields.map((field) => <button key={field} className={lotUsedFields.includes(field) ? 'is-used' : ''} onClick={() => insertLotVariable(field)} disabled={editorBusy} title={VARIABLE_LABELS[field] ?? field}><code>{`{${field}}`}</code><span>{VARIABLE_LABELS[field] ?? field}</span></button>)}</div>
                    </div>
                    {lotUnknownFields.length > 0 && <div className="templates-validation is-error"><Icon name="warning" size={15} /><span>Не поддерживаются: {lotUnknownFields.map((field) => `{${field}}`).join(', ')}</span></div>}
                    {lotMissingTitleFields.length > 0 && <div className="templates-validation is-error"><Icon name="warning" size={15} /><span>В названии обязательны: {lotMissingTitleFields.map((field) => `{${field}}`).join(', ')}</span></div>}
                  </>
                ) : null}
              </div>

              <aside className="templates-preview-pane">
                <div className="templates-preview-pane__header"><span><Icon name="eye" size={15} />Предпросмотр</span><small>Тестовые данные · не публикуется</small></div>
                {section === 'messages'
                  ? <MessagePreview content={messageContent} language={language} />
                  : <LotPreview title={lotTitle} description={lotDescription} enabled={activeLotDraft?.enabled ?? false} />}
                <div className="templates-preview-note"><Icon name="activity" size={15} /><span>Фигурные скобки заменяются актуальными данными заказа только при отправке или публикации.</span></div>
              </aside>
            </div>
          </div>
        </section>
      )}

      {activeDirtyCount > 0 && <div className="templates-unsaved"><span><span className="status-dot status-dot--warning" />Не сохранено: {activeDirtyCount}</span><div><button className="button button--ghost" onClick={discardActiveSection} disabled={editorBusy}>Отменить</button><button className="button button--primary" onClick={saveActiveSection} disabled={editorBusy}>Сохранить</button></div></div>}

      {createOpen && (
        <ModalOverlay onClose={() => setCreateOpen(false)} canClose={!createLot.isPending}>
          <div className="modal-card templates-create-modal" role="dialog" aria-modal="true" aria-labelledby="create-lot-template-title">
            <div className="modal-card__header"><div><span className="eyebrow">Шаблоны лотов</span><h2 id="create-lot-template-title">Новый шаблон</h2><p>Точный тариф и тип лимита имеют приоритет над общим шаблоном.</p></div><button className="icon-button" onClick={() => setCreateOpen(false)} disabled={createLot.isPending} aria-label="Закрыть"><Icon name="close" /></button></div>
            <div className="modal-card__body templates-create-form">
              {error && <MutationAlert error={error} saved="" />}
              <div className="form-grid">
                <label className="field"><span className="field__label">Название</span><input data-autofocus value={createDraft.name} onChange={(event) => { const name = event.target.value; setCreateDraft((current) => ({ ...current, name, key: templateKeyForName(name, current.key, createKeyTouched) })) }} placeholder="Plus · Codex" maxLength={120} /></label>
                <label className="field"><span className="field__label">Системный ключ</span><input value={createDraft.key} onChange={(event) => { setCreateKeyTouched(true); setCreateDraft((current) => ({ ...current, key: normalizeTemplateKey(event.target.value) })) }} placeholder="plus-codex" maxLength={64} /><span className="field__hint">2–64 символа: латиница, цифры, дефис или подчёркивание</span></label>
              </div>
              <div className="form-grid">
                <label className="field"><span className="field__label">Тариф</span><select value={createDraft.tier_id ?? ''} onChange={(event) => setCreateDraft((current) => ({ ...current, tier_id: event.target.value ? Number(event.target.value) : null }))}><option value="">Любой тариф</option>{(tiersQuery.data ?? []).map((tier) => <option key={tier.id} value={tier.id}>{tier.name}</option>)}</select></label>
                <label className="field"><span className="field__label">Тип лимита</span><select value={createDraft.limit_scope_id ?? ''} onChange={(event) => setCreateDraft((current) => ({ ...current, limit_scope_id: event.target.value ? Number(event.target.value) : null }))}><option value="">Любой лимит</option>{(scopesQuery.data ?? []).map((scope) => <option key={scope.id} value={scope.id}>{scope.name}</option>)}</select></label>
              </div>
              <div className="form-grid">
                <label className="field"><span className="field__label">Название RU</span><input value={createDraft.title_ru} onChange={(event) => setCreateDraft((current) => ({ ...current, title_ru: event.target.value }))} maxLength={255} /></label>
                <label className="field"><span className="field__label">Название EN</span><input value={createDraft.title_en} onChange={(event) => setCreateDraft((current) => ({ ...current, title_en: event.target.value }))} maxLength={255} /></label>
              </div>
              <label className="templates-switch"><input type="checkbox" checked={createDraft.enabled} onChange={(event) => setCreateDraft((current) => ({ ...current, enabled: event.target.checked }))} /><span /><b>Активировать сразу после создания</b></label>
            </div>
            <div className="modal-card__footer"><button className="button button--secondary" onClick={() => setCreateOpen(false)} disabled={createLot.isPending}>Отмена</button><button className="button button--primary" onClick={submitCreateLot} disabled={createLot.isPending}>{createLot.isPending ? <><span className="spinner spinner--light" />Создаём…</> : <><Icon name="plus" />Создать</>}</button></div>
          </div>
        </ModalOverlay>
      )}

      {deleteOpen && activeLot && (
        <ModalOverlay onClose={() => setDeleteOpen(false)} canClose={!deleteLot.isPending}>
          <div className="modal-card modal-card--small templates-delete-modal" role="alertdialog" aria-modal="true" aria-labelledby="delete-lot-template-title">
            <div className="modal-card__header"><div><span className="eyebrow">Удаление</span><h2 id="delete-lot-template-title">Удалить «{activeLot.name}»?</h2><p>При ближайшей синхронизации новые и существующие автолоты перейдут на следующий подходящий шаблон.</p></div></div>
            {error && <div className="templates-delete-modal__alert"><MutationAlert error={error} saved="" /></div>}
            <div className="modal-card__footer"><button className="button button--secondary" onClick={() => setDeleteOpen(false)} disabled={deleteLot.isPending}>Отмена</button><button className="button button--danger" onClick={confirmDeleteLot} disabled={deleteLot.isPending}><Icon name="trash" />Удалить</button></div>
          </div>
        </ModalOverlay>
      )}

      {(pendingSection || navigationBlocker.state === 'blocked') && (
        <ModalOverlay onClose={cancelDeparture} canClose={!editorBusy}>
          <div className="modal-card modal-card--small templates-delete-modal" role="alertdialog" aria-modal="true" aria-labelledby="unsaved-templates-title">
            <div className="modal-card__header"><div><span className="eyebrow">Несохранённые изменения</span><h2 id="unsaved-templates-title">Сохранить перед переходом?</h2><p>Изменено шаблонов: {activeDirtyCount}. Можно сохранить правки, отменить их или остаться в редакторе.</p></div></div>
            {error && <div className="templates-delete-modal__alert"><MutationAlert error={error} saved="" /></div>}
            <div className="modal-card__footer"><button className="button button--secondary" onClick={cancelDeparture} disabled={editorBusy}>Остаться</button><button className="button button--ghost" onClick={discardAndContinue} disabled={editorBusy}>Не сохранять</button><button className="button button--primary" onClick={saveAndContinue} disabled={editorBusy}>{editorBusy ? <><span className="spinner spinner--light" />Сохраняем…</> : 'Сохранить и перейти'}</button></div>
          </div>
        </ModalOverlay>
      )}
    </div>
  )
}
