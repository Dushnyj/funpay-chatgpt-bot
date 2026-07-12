import { useEffect, useMemo, useState } from 'react'
import { ApiError } from '../api/client'
import { useTemplates, useUpdateTemplates } from '../api/templates'
import { Icon } from '../components/Icon'
import { EmptyState, ErrorState, LoadingState, PageHeader } from '../components/ui'
import type { MessageTemplate } from '../types/api'

const TEMPLATE_META: Record<string, { title: string; description: string }> = {
  welcome: { title: 'Выдача аккаунта', description: 'Первое сообщение после успешной оплаты' },
  code_success: { title: 'Код входа', description: 'Ответ на команду !код' },
  code_expired: { title: 'Доступ завершён', description: 'Аренда истекла или не найдена' },
  code_rate_limited: { title: 'Лимит запросов кода', description: 'Слишком частый запрос TOTP' },
  subscription: { title: 'Статус подписки', description: 'Ответ на команду !подписка' },
  replace_success: { title: 'Успешная замена', description: 'Новые данные после замены аккаунта' },
  replace_declined: { title: 'Замена отклонена', description: 'Аккаунт работает корректно' },
  replace_no_account: { title: 'Нет аккаунта для замены', description: 'Свободный аккаунт не найден' },
  seller_called: { title: 'Продавец вызван', description: 'Подтверждение команды !продавец' },
  help: { title: 'Справка', description: 'Список доступных команд' },
  order_confirmed: { title: 'Заказ подтверждён', description: 'Финальное сообщение покупателю' },
  expiry: { title: 'Аренда истекла', description: 'Уведомление об окончании доступа' },
  disconnect: { title: 'Временное отключение', description: 'Сессия аккаунта завершена' },
  no_account_available: { title: 'Нет свободного аккаунта', description: 'Выдача временно невозможна' },
}

const SAMPLE_VALUES: Record<string, string> = {
  tier: 'Plus', days: '7', login: 'user@example.com', password: '••••••••', expires_at: '19.07.2026', expires_in: '6д 23ч',
  code: '482 913', retry_in_sec: '24', retry_minutes: '5', chat_5h: '82', chat_weekly: '64', codex_5h: '82', codex_weekly: '64',
}

const draftKey = (key: string, lang: string) => `${key}:${lang}`

export default function Templates() {
  const templatesQuery = useTemplates()
  const updateTemplates = useUpdateTemplates()
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [selectedKey, setSelectedKey] = useState('')
  const [language, setLanguage] = useState<'ru' | 'en'>('ru')
  const [search, setSearch] = useState('')
  const [dirty, setDirty] = useState(false)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    if (!templatesQuery.data || dirty) return
    setDraft(Object.fromEntries(templatesQuery.data.map((template) => [draftKey(template.key, template.lang), template.content])))
    if (!selectedKey && templatesQuery.data[0]) setSelectedKey(templatesQuery.data[0].key)
  }, [templatesQuery.data, dirty, selectedKey])

  const templates = useMemo(() => templatesQuery.data ?? [], [templatesQuery.data])
  const keys = useMemo(() => [...new Set(templates.map((template) => template.key))].filter((key) => {
    const meta = TEMPLATE_META[key]
    const haystack = `${key} ${meta?.title ?? ''} ${meta?.description ?? ''}`.toLowerCase()
    return haystack.includes(search.trim().toLowerCase())
  }), [templates, search])

  if (templatesQuery.isLoading) return <LoadingState label="Загружаем шаблоны сообщений" />
  if (templatesQuery.isError) return <ErrorState onRetry={() => templatesQuery.refetch()} />

  const activeDraftKey = draftKey(selectedKey, language)
  const content = draft[activeDraftKey] ?? ''
  const variables = [...new Set([...content.matchAll(/\{([a-zA-Z0-9_]+)\}/g)].map((match) => match[1]))]
  const preview = content.replace(/\{([a-zA-Z0-9_]+)\}/g, (_, variable: string) => SAMPLE_VALUES[variable] ?? `{${variable}}`)
  const unknownVariables = variables.filter((variable) => !(variable in SAMPLE_VALUES))

  const changeContent = (value: string) => {
    setDraft((current) => ({ ...current, [activeDraftKey]: value }))
    setDirty(true)
    setSaved(false)
    setError('')
  }

  const save = async () => {
    setError('')
    const items: MessageTemplate[] = templates.map((template) => ({ ...template, content: draft[draftKey(template.key, template.lang)] ?? template.content }))
    try {
      await updateTemplates.mutateAsync(items)
      setDirty(false)
      setSaved(true)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Не удалось сохранить шаблоны')
    }
  }

  const discard = () => {
    setDraft(Object.fromEntries(templates.map((template) => [draftKey(template.key, template.lang), template.content])))
    setDirty(false)
    setSaved(false)
    setError('')
  }

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Коммуникации"
        title="Шаблоны"
        description="Сообщения покупателю на русском и английском языках с живым предпросмотром."
        actions={<div className="header-action-group"><button className="button button--secondary" onClick={discard} disabled={!dirty}>Отменить</button><button className="button button--primary" onClick={save} disabled={!dirty || updateTemplates.isPending}>{updateTemplates.isPending ? <><span className="spinner spinner--light" />Сохраняем…</> : <><Icon name="check" />Сохранить всё</>}</button></div>}
      />

      <div className="content-tabs content-tabs--compact" role="tablist">
        <button className="active" role="tab" aria-selected="true"><Icon name="templates" />Сообщения бота<span>{keys.length}</span></button>
        <button role="tab" aria-selected="false" disabled title="LotTemplate API отсутствует"><Icon name="lots" />Шаблоны лотов<span>API</span></button>
      </div>
      {error && <div className="form-alert form-alert--error"><Icon name="warning" /><span>{error}</span></div>}
      {saved && <div className="form-alert form-alert--success"><Icon name="check" /><span>Все изменения шаблонов сохранены.</span></div>}

      {templates.length === 0 ? <section className="panel"><EmptyState icon="templates" title="Шаблоны не инициализированы" description="Backend содержит набор стандартных сообщений, но пока не запускает его автоматическое заполнение при первом старте." /></section> : (
        <section className="template-workbench">
          <aside className="template-list">
            <div className="template-list__header"><h2>Сообщения</h2><span>{keys.length}</span></div>
            <label className="search-field search-field--compact"><Icon name="search" /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Найти шаблон" aria-label="Поиск шаблонов" /></label>
            <div className="template-list__items">
              {keys.map((key) => {
                const meta = TEMPLATE_META[key] ?? { title: key, description: 'Системное сообщение' }
                const hasBothLanguages = Boolean(draft[draftKey(key, 'ru')] && draft[draftKey(key, 'en')])
                return <button key={key} className={selectedKey === key ? 'active' : ''} onClick={() => setSelectedKey(key)}><span className="template-list__icon"><Icon name="templates" size={17} /></span><span><strong>{meta.title}</strong><small>{meta.description}</small></span><span className={`language-health ${hasBothLanguages ? 'language-health--ok' : ''}`}>{hasBothLanguages ? 'RU · EN' : 'Неполный'}</span></button>
              })}
            </div>
          </aside>

          <div className="template-editor">
            <div className="template-editor__header">
              <div><span className="eyebrow">{selectedKey}</span><h2>{TEMPLATE_META[selectedKey]?.title ?? selectedKey}</h2><p>{TEMPLATE_META[selectedKey]?.description}</p></div>
              <div className="language-toggle" role="tablist"><button className={language === 'ru' ? 'active' : ''} onClick={() => setLanguage('ru')} role="tab" aria-selected={language === 'ru'}>RU</button><button className={language === 'en' ? 'active' : ''} onClick={() => setLanguage('en')} role="tab" aria-selected={language === 'en'}>EN</button></div>
            </div>
            <div className="template-editor__body">
              <div className="editor-pane">
                <label className="field"><span className="field__label">Текст сообщения</span><textarea value={content} onChange={(event) => changeContent(event.target.value)} rows={13} spellCheck="true" /></label>
                <div className="variable-bar"><span>Переменные в сообщении:</span>{variables.length ? variables.map((variable) => <button key={variable} type="button" onClick={() => changeContent(`${content}{${variable}}`)}>{`{${variable}}`}</button>) : <small>Нет переменных</small>}</div>
                {unknownVariables.length > 0 && <div className="form-alert form-alert--warning"><Icon name="warning" /><span>Неизвестные переменные: {unknownVariables.join(', ')}. Проверьте, что backend передаёт их при рендеринге.</span></div>}
              </div>
              <div className="preview-pane">
                <div className="preview-pane__header"><span>Предпросмотр</span><span className="soft-badge">{language.toUpperCase()}</span></div>
                <div className="chat-preview">
                  <div className="chat-preview__meta"><span className="chat-avatar">F</span><span><strong>FunPay Rental</strong><small>только что</small></span></div>
                  <div className="chat-bubble">{preview || <span className="muted">Введите текст сообщения</span>}</div>
                </div>
                <p className="preview-hint"><Icon name="activity" size={15} />Предпросмотр использует тестовые значения и не отправляет сообщение в FunPay.</p>
              </div>
            </div>
          </div>
        </section>
      )}
      {dirty && <div className="unsaved-bar"><span><span className="status-dot status-dot--warning" />Шаблоны изменены локально</span><div><button className="button button--ghost" onClick={discard}>Отменить</button><button className="button button--primary" onClick={save} disabled={updateTemplates.isPending}>Сохранить всё</button></div></div>}
    </div>
  )
}
