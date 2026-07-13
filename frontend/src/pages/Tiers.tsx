import { useState, type FormEvent } from 'react'
import {
  useDurations,
  useLimitScopes,
  useTiers,
  useCreateDuration,
  useUpdateDuration,
  useUpdateLimitScope,
  useUpdateTier,
} from '../api/catalog'
import { ApiError } from '../api/client'
import { Icon } from '../components/Icon'
import {
  EmptyState,
  ErrorState,
  LoadingState,
  ModalOverlay,
  PageHeader,
  StatusBadge,
  TableShell,
} from '../components/ui'
import type {
  Duration,
  DurationUpdate,
  LimitScope,
  LimitScopeUpdate,
  Tier,
  TierUpdate,
} from '../types/api'
import { durationUnit, parseCatalogSortOrder, validateDurationDays } from '../utils/catalogEditor'
import { isSupportedOfferScopeCode } from '../utils/offerScopes'

type CatalogTab = 'tiers' | 'durations' | 'scopes'
type UpdatingTier = { id: number; field: 'active' | 'sellable' }
type CatalogItemUpdate = DurationUpdate | LimitScopeUpdate

export default function Tiers() {
  const [tab, setTab] = useState<CatalogTab>('tiers')
  const tabs: Array<{ id: CatalogTab; label: string; description: string }> = [
    { id: 'tiers', label: 'Тарифы', description: 'Типы подписок ChatGPT' },
    { id: 'durations', label: 'Сроки', description: 'Доступные периоды аренды' },
    { id: 'scopes', label: 'Лимиты', description: 'Правила качества аккаунта' },
  ]

  return (
    <div className="page-stack">
      <PageHeader eyebrow="Товарная модель" title="Справочники" description="Базовые сущности, из которых строятся цены, лоты и правила выдачи." />
      <div className="catalog-tabs" role="tablist" aria-label="Разделы справочников">
        {tabs.map((item) => (
          <button key={item.id} type="button" role="tab" aria-selected={tab === item.id} className={tab === item.id ? 'active' : ''} onClick={() => setTab(item.id)}>
            <strong>{item.label}</strong><span>{item.description}</span>
          </button>
        ))}
      </div>
      {tab === 'tiers' && <TiersTab />}
      {tab === 'durations' && <DurationsTab />}
      {tab === 'scopes' && <ScopesTab />}
    </div>
  )
}

function TiersTab() {
  const tiersQuery = useTiers()
  const updateTier = useUpdateTier()
  const [updatingTier, setUpdatingTier] = useState<UpdatingTier | null>(null)
  const [error, setError] = useState('')

  if (tiersQuery.isLoading) return <LoadingState label="Загружаем тарифы" />
  if (tiersQuery.isError) return <ErrorState onRetry={() => tiersQuery.refetch()} />

  const tiers = [...(tiersQuery.data ?? [])].sort((left, right) =>
    (left.sort_order ?? left.id) - (right.sort_order ?? right.id),
  )

  const updateTierState = async (tier: Tier, field: UpdatingTier['field'], values: TierUpdate) => {
    setError('')
    setUpdatingTier({ id: tier.id, field })
    try {
      await updateTier.mutateAsync({ id: tier.id, ...values })
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Не удалось изменить тариф')
    } finally {
      setUpdatingTier(null)
    }
  }

  return (
    <section className="panel panel--flush">
      <div className="section-toolbar">
        <div><h2>Системный каталог тарифов</h2><p>Free, Go, Plus и варианты Pro распознаются автоматически по данным аккаунта.</p></div>
        <span className="soft-badge soft-badge--editable"><Icon name="settings" size={14} />Доступно редактирование</span>
      </div>
      <div className="form-alert form-alert--info catalog-system-note"><Icon name="activity" /><span>Названия тарифов синхронизируются системой. Здесь можно включить сам тариф и отдельно разрешить его продажу.</span></div>
      {error && <div className="form-alert form-alert--error catalog-system-note" role="alert"><Icon name="warning" /><span>{error}</span></div>}
      {tiers.length === 0 ? (
        <EmptyState icon="catalog" title="Системный каталог не инициализирован" description="Перезапустите bootstrap backend: тарифы создаются автоматически и не требуют ручного ввода." action={<button className="button button--secondary" onClick={() => tiersQuery.refetch()}><Icon name="refresh" />Обновить</button>} />
      ) : (
        <TableShell><table className="data-table tier-catalog-table"><thead><tr><th>Тариф</th><th>Описание</th><th>Коэффициент</th><th>Активность</th><th>Продажа</th></tr></thead><tbody>{tiers.map((tier) => {
          const tierName = displayTierName(tier)
          const pending = updatingTier?.id === tier.id
          return (
            <tr key={tier.id}>
              <td data-label="Тариф"><div className="identity-cell"><span className="identity-avatar identity-avatar--violet">{tierName.slice(0, 1).toUpperCase()}</span><span><strong>{tierName}</strong><small title={`Системный код: ${tier.code ?? `system-${tier.id}`}`}>Системный тариф</small></span></div></td>
              <td data-label="Описание">{displayTierDescription(tier)}</td>
              <td data-label="Коэффициент">{tier.usage_multiplier == null ? '—' : `×${tier.usage_multiplier}`}</td>
              <td data-label="Активность">
                <label className="switch-control">
                  <input
                    type="checkbox"
                    aria-label={`Тариф ${tierName} активен`}
                    checked={tier.is_active}
                    onChange={() => updateTierState(tier, 'active', {
                      is_active: !tier.is_active,
                      ...(!tier.is_active ? {} : { is_sellable: false }),
                    })}
                    disabled={pending}
                  />
                  <span aria-hidden="true" />
                  <strong>{tier.is_active ? 'Включён' : 'Выключен'}</strong>
                </label>
              </td>
              <td data-label="Продажа">
                <label className="switch-control">
                  <input
                    type="checkbox"
                    aria-label={`Продажа тарифа ${tierName}`}
                    checked={tier.is_sellable ?? tier.is_active}
                    onChange={() => updateTierState(tier, 'sellable', { is_sellable: !(tier.is_sellable ?? tier.is_active) })}
                    disabled={pending || !tier.is_active}
                  />
                  <span aria-hidden="true" />
                  <strong>{(tier.is_sellable ?? tier.is_active) ? 'Разрешена' : 'Запрещена'}</strong>
                </label>
              </td>
            </tr>
          )
        })}</tbody></table></TableShell>
      )}
    </section>
  )
}

function DurationsTab() {
  const query = useDurations()
  const updateDuration = useUpdateDuration()
  const [editing, setEditing] = useState<Duration | null>(null)
  const [createOpen, setCreateOpen] = useState(false)

  if (query.isLoading) return <LoadingState label="Загружаем сроки аренды" />
  if (query.isError) return <ErrorState onRetry={() => query.refetch()} />

  const durations = [...(query.data ?? [])].sort((left, right) => left.sort_order - right.sort_order || left.id - right.id)

  return (
    <section className="panel panel--flush">
      <div className="section-toolbar"><div><h2>Сроки аренды</h2><p>Включённые периоды участвуют в построении матрицы цен.</p></div><div className="catalog-toolbar-actions"><span className="soft-badge soft-badge--editable"><Icon name="settings" size={14} />Доступно редактирование</span><button type="button" className="button button--secondary" onClick={() => setCreateOpen(true)} title="Добавить пользовательский срок аренды"><Icon name="plus" size={16} />Добавить срок</button></div></div>
      {durations.length === 0 ? <EmptyState icon="clock" title="Сроки не настроены" description="Добавьте первый период аренды от 1 до 30 дней." action={<button type="button" className="button button--primary" onClick={() => setCreateOpen(true)}><Icon name="plus" />Добавить срок</button>} /> : (
        <div className="duration-grid">{durations.map((duration) => (
          <article className={`duration-card ${duration.is_enabled ? 'duration-card--active' : ''}`} key={duration.id}>
            <span>{duration.days}</span>
            <strong>{durationUnit(duration.days)}</strong>
            <div className="catalog-card__footer">
              <StatusBadge value={duration.is_enabled ? 'active' : 'paused'} label={duration.is_enabled ? 'Включён' : 'Выключен'} />
              <button type="button" className="catalog-card__edit" onClick={() => setEditing(duration)} aria-label={`Настроить срок ${duration.days} ${durationUnit(duration.days)}`} title="Настроить">
                <Icon name="settings" size={15} /><span>Настроить</span>
              </button>
            </div>
          </article>
        ))}</div>
      )}
      {editing && (
        <CatalogSettingsDialog
          title={`Срок ${editing.days} ${durationUnit(editing.days)}`}
          description="Количество дней зафиксировано после создания. Можно изменить доступность и положение в списках."
          enabled={editing.is_enabled}
          sortOrder={editing.sort_order}
          isPending={updateDuration.isPending}
          onClose={() => setEditing(null)}
          onSave={async (changes) => {
            await updateDuration.mutateAsync({ id: editing.id, ...changes })
            setEditing(null)
          }}
        />
      )}
      {createOpen && <CreateDurationDialog durations={durations} onClose={() => setCreateOpen(false)} />}
    </section>
  )
}

function CreateDurationDialog({ durations, onClose }: { durations: Duration[]; onClose: () => void }) {
  const createDuration = useCreateDuration()
  const suggestedSortOrder = Math.min(
    10_000,
    Math.max(0, ...durations.map((duration) => duration.sort_order)) + 10,
  )
  const [daysInput, setDaysInput] = useState('')
  const [enabled, setEnabled] = useState(true)
  const [sortOrderInput, setSortOrderInput] = useState(String(suggestedSortOrder))
  const [attempted, setAttempted] = useState(false)
  const [serverError, setServerError] = useState('')
  const daysValidation = validateDurationDays(daysInput, durations.map((duration) => duration.days))
  const parsedSortOrder = parseCatalogSortOrder(sortOrderInput)
  const daysError = attempted && daysValidation.error
  const orderError = attempted && parsedSortOrder === null

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setAttempted(true)
    setServerError('')
    if (daysValidation.days === null || parsedSortOrder === null) return
    try {
      await createDuration.mutateAsync({
        days: daysValidation.days,
        is_enabled: enabled,
        sort_order: parsedSortOrder,
      })
      onClose()
    } catch (cause) {
      setServerError(cause instanceof ApiError
        ? cause.status === 409
          ? `Срок ${daysValidation.days} ${durationUnit(daysValidation.days)} уже существует. Обновите список и выберите другое значение.`
          : cause.message
        : 'Не удалось создать срок аренды')
    }
  }

  return (
    <ModalOverlay onClose={onClose} canClose={!createDuration.isPending}>
      <form className="modal catalog-settings-dialog" role="dialog" aria-modal="true" aria-labelledby="create-duration-title" aria-describedby="create-duration-description" onSubmit={submit}>
        <div className="modal__header">
          <div><span className="eyebrow">Справочник сроков</span><h2 id="create-duration-title">Новый срок аренды</h2><p id="create-duration-description">После создания количество дней изменить нельзя. Доступность и порядок можно настроить позже.</p></div>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Закрыть" title="Закрыть" disabled={createDuration.isPending}><Icon name="close" /></button>
        </div>
        {serverError && <div className="form-alert form-alert--error" role="alert"><Icon name="warning" /><span>{serverError}</span></div>}
        <div className="catalog-create-fields">
          <label className="field" htmlFor="create-duration-days">
            <span className="field__label">Количество дней</span>
            <input id="create-duration-days" data-autofocus type="number" min="1" max="30" step="1" inputMode="numeric" value={daysInput} onChange={(event) => { setDaysInput(event.target.value); setServerError('') }} onBlur={() => setAttempted(true)} disabled={createDuration.isPending} aria-invalid={Boolean(daysError)} aria-describedby="create-duration-days-hint" placeholder="Например, 8" />
            <small id="create-duration-days-hint" className={`field__hint ${daysError ? 'text-danger' : ''}`}>{daysError || 'Целое уникальное значение от 1 до 30.'}</small>
          </label>
          <label className="field" htmlFor="create-duration-order">
            <span className="field__label">Порядок отображения</span>
            <input id="create-duration-order" type="number" min="0" max="10000" step="1" inputMode="numeric" value={sortOrderInput} onChange={(event) => setSortOrderInput(event.target.value)} disabled={createDuration.isPending} aria-invalid={Boolean(orderError)} aria-describedby="create-duration-order-hint" />
            <small id="create-duration-order-hint" className={`field__hint ${orderError ? 'text-danger' : ''}`}>{orderError ? 'Введите целое число от 0 до 10 000.' : 'Меньшее число показывается раньше.'}</small>
          </label>
        </div>
        <div className="catalog-toggle-row catalog-create-toggle">
          <div><strong>Включить сразу</strong><small>Срок появится в создании цен и новых лотов.</small></div>
          <label className="switch-control">
            <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} disabled={createDuration.isPending} aria-label="Включить новый срок сразу" />
            <span aria-hidden="true" />
            <strong>{enabled ? 'Включён' : 'Выключен'}</strong>
          </label>
        </div>
        <div className="modal__actions">
          <button type="button" className="button button--secondary" onClick={onClose} disabled={createDuration.isPending}>Отмена</button>
          <button type="submit" className="button button--primary" disabled={createDuration.isPending}>{createDuration.isPending ? 'Создаём…' : 'Создать срок'}</button>
        </div>
      </form>
    </ModalOverlay>
  )
}

function ScopesTab() {
  const query = useLimitScopes()
  const updateScope = useUpdateLimitScope()
  const [editing, setEditing] = useState<LimitScope | null>(null)

  if (query.isLoading) return <LoadingState label="Загружаем типы лимитов" />
  if (query.isError) return <ErrorState onRetry={() => query.refetch()} />

  const scopes = [...(query.data ?? [])].sort((left, right) => left.sort_order - right.sort_order || left.id - right.id)
  const descriptions: Record<string, string> = {
    any: 'Без гарантии остатка конкретного лимита. Подходит для базовых предложений.',
    chat: 'Недоступно для продажи с гарантией: OpenAI не публикует достоверный остаток сообщений ChatGPT.',
    codex: 'Гарантированный остаток измеримых лимитов Codex в фактическом окне OpenAI.',
  }

  return (
    <section className="panel panel--flush">
      <div className="section-toolbar"><div><h2>Типы лимитов</h2><p>Определяют, какие показатели учитываются при подборе аккаунта.</p></div><span className="soft-badge soft-badge--editable"><Icon name="settings" size={14} />Доступно редактирование</span></div>
      {scopes.length === 0 ? <EmptyState icon="activity" title="Типы лимитов не инициализированы" description="Ожидаются системные значения any, chat и codex." /> : (
        <div className="scope-grid">{scopes.map((scope) => {
          const code = scope.code.toLowerCase()
          const unavailable = !isSupportedOfferScopeCode(code)
          return (
            <article className={`scope-card ${unavailable ? 'scope-card--unavailable' : ''} ${scope.is_enabled ? '' : 'scope-card--disabled'}`} key={scope.id} data-state={scope.is_enabled ? 'enabled' : 'disabled'}>
              <div className="scope-card__main">
                <div className="scope-card__icon"><Icon name={code === 'codex' ? 'templates' : unavailable ? 'activity' : 'catalog'} /></div>
                <div className="scope-card__body"><span className="eyebrow">{code}</span><h3>{scope.name}</h3>{unavailable && <StatusBadge value="disabled" label={code === 'chat' ? 'Недоступно · не измеряется' : 'Недоступно · не поддерживается'} />}<p>{descriptions[code] ?? 'Неизвестный системный тип не используется в новых предложениях.'}</p></div>
              </div>
              <div className="catalog-card__footer">
                {!unavailable && <StatusBadge value={scope.is_enabled ? 'active' : 'paused'} label={scope.is_enabled ? 'Включён' : 'Выключен'} />}
                <button type="button" className="catalog-card__edit" onClick={() => setEditing(scope)} aria-label={`Настроить тип лимита ${scope.name}`} title="Настроить">
                  <Icon name="settings" size={15} /><span>Настроить</span>
                </button>
              </div>
            </article>
          )
        })}</div>
      )}
      {editing && (
        <CatalogSettingsDialog
          title={`Тип лимита «${editing.name}»`}
          description={`Системный код ${editing.code} не меняется. Управляйте доступностью и положением в списках.`}
          enabled={editing.is_enabled}
          sortOrder={editing.sort_order}
          enabledLocked={!isSupportedOfferScopeCode(editing.code)}
          lockedHint={editing.code.toLowerCase() === 'chat'
            ? 'Chat нельзя включить: OpenAI не предоставляет измеримый остаток этого лимита.'
            : `Тип «${editing.code}» не поддерживается и не может участвовать в новых предложениях.`}
          isPending={updateScope.isPending}
          onClose={() => setEditing(null)}
          onSave={async (changes) => {
            await updateScope.mutateAsync({ id: editing.id, ...changes })
            setEditing(null)
          }}
        />
      )}
    </section>
  )
}

function CatalogSettingsDialog({
  title,
  description,
  enabled,
  sortOrder,
  enabledLocked = false,
  lockedHint,
  isPending,
  onClose,
  onSave,
}: {
  title: string
  description: string
  enabled: boolean
  sortOrder: number
  enabledLocked?: boolean
  lockedHint?: string
  isPending: boolean
  onClose: () => void
  onSave: (changes: CatalogItemUpdate) => Promise<void>
}) {
  const [nextEnabled, setNextEnabled] = useState(enabled)
  const [sortOrderInput, setSortOrderInput] = useState(String(sortOrder))
  const [error, setError] = useState('')
  const parsedSortOrder = parseCatalogSortOrder(sortOrderInput)
  const hasChanges = nextEnabled !== enabled || parsedSortOrder !== sortOrder

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (parsedSortOrder === null || !hasChanges) return
    const changes: CatalogItemUpdate = {}
    if (nextEnabled !== enabled) changes.is_enabled = nextEnabled
    if (parsedSortOrder !== sortOrder) changes.sort_order = parsedSortOrder
    setError('')
    try {
      await onSave(changes)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Не удалось сохранить изменения')
    }
  }

  return (
    <ModalOverlay onClose={onClose} canClose={!isPending}>
      <form className="modal catalog-settings-dialog" role="dialog" aria-modal="true" aria-labelledby="catalog-edit-title" onSubmit={submit}>
        <div className="modal__header">
          <div><span className="eyebrow">Настройка справочника</span><h2 id="catalog-edit-title">{title}</h2><p>{description}</p></div>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Закрыть" disabled={isPending}><Icon name="close" /></button>
        </div>
        {error && <div className="form-alert form-alert--error" role="alert"><Icon name="warning" /><span>{error}</span></div>}
        <div className="catalog-toggle-row">
          <div><strong>Использовать в продажах</strong><small>Выключенный элемент исчезнет из создания цен и новых лотов.</small></div>
          <label className="switch-control">
            <input type="checkbox" checked={nextEnabled} onChange={(event) => setNextEnabled(event.target.checked)} disabled={enabledLocked || isPending} aria-label="Использовать элемент в продажах" />
            <span aria-hidden="true" />
            <strong>{nextEnabled ? 'Включён' : 'Выключен'}</strong>
          </label>
        </div>
        {enabledLocked && lockedHint && <div className="form-alert form-alert--warning catalog-lock-note"><Icon name="warning" /><span>{lockedHint}</span></div>}
        <label className="field catalog-order-field">
          <span className="field__label">Порядок отображения</span>
          <input data-autofocus type="number" min="0" max="10000" step="1" inputMode="numeric" value={sortOrderInput} onChange={(event) => setSortOrderInput(event.target.value)} disabled={isPending} aria-invalid={parsedSortOrder === null} />
          <small className="field__hint">{parsedSortOrder === null ? 'Введите целое число от 0 до 10 000.' : 'Меньшее число показывается раньше.'}</small>
        </label>
        <div className="modal__actions">
          <button type="button" className="button button--secondary" onClick={onClose} disabled={isPending}>Отмена</button>
          <button type="submit" className="button button--primary" disabled={isPending || parsedSortOrder === null || !hasChanges}>{isPending ? 'Сохраняем…' : 'Сохранить'}</button>
        </div>
      </form>
    </ModalOverlay>
  )
}

function displayTierName(tier: Tier) {
  return tier.name.replace(/\s*\/\s*usage-based\s*$/i, '')
}

function displayTierDescription(tier: Tier) {
  if (!tier.description) return 'Канонический план ChatGPT'
  return tier.description
    .replace(/\s*\(raw:\s*[^)]+\)/gi, '')
    .replace(/,\s*включая прежнее raw-имя\s+[^,.]+/gi, '')
    .replace(/с usage-based конфигурацией/gi, 'с оплатой по фактическому использованию')
    .trim()
}
