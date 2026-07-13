import { useState, type FormEvent } from 'react'
import {
  useDurations,
  useLimitScopes,
  useTiers,
  useCreateDuration,
  useDeleteDuration,
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
  LimitScope,
  Tier,
  TierUpdate,
} from '../types/api'
import {
  compareDurationsByMinutes,
  formatDurationMinutes,
  validateDurationInput,
  type DurationInputMode,
} from '../utils/catalogEditor'
import { compareOfferScopes, isSupportedOfferScopeCode } from '../utils/offerScopes'

type CatalogTab = 'tiers' | 'durations' | 'scopes'
type UpdatingTier = { id: number; field: 'active' | 'sellable' }

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
  const [editing, setEditing] = useState<Duration | null>(null)
  const [createOpen, setCreateOpen] = useState(false)

  if (query.isLoading) return <LoadingState label="Загружаем сроки аренды" />
  if (query.isError) return <ErrorState onRetry={() => query.refetch()} />

  const durations = [...(query.data ?? [])].sort(compareDurationsByMinutes)

  return (
    <section className="panel panel--flush">
      <div className="section-toolbar"><div><h2>Сроки аренды</h2><p>Периоды задаются с шагом 30 минут и всегда показаны по возрастанию продолжительности.</p></div><div className="catalog-toolbar-actions"><button type="button" className="button button--secondary" onClick={() => setCreateOpen(true)} title="Добавить пользовательский срок аренды"><Icon name="plus" size={16} />Добавить срок</button></div></div>
      {durations.length === 0 ? <EmptyState icon="clock" title="Сроки не настроены" description="Добавьте первый период аренды от 30 минут до 30 дней." action={<button type="button" className="button button--primary" onClick={() => setCreateOpen(true)}><Icon name="plus" />Добавить срок</button>} /> : (
        <div className="duration-grid">{durations.map((duration) => (
          <article className={`duration-card ${duration.is_enabled ? 'duration-card--active' : ''}`} key={duration.id}>
            <span title={`${duration.minutes} минут`}>{formatDurationMinutes(duration.minutes)}</span>
            <strong>срок аренды</strong>
            <div className="catalog-card__footer">
              <StatusBadge value={duration.is_enabled ? 'active' : 'paused'} label={duration.is_enabled ? 'Включён' : 'Выключен'} />
              <button type="button" className="catalog-card__edit" onClick={() => setEditing(duration)} aria-label={`Настроить срок ${formatDurationMinutes(duration.minutes)}`} title="Настроить">
                <Icon name="settings" size={15} /><span>Настроить</span>
              </button>
            </div>
          </article>
        ))}</div>
      )}
      {editing && (
        <DurationSettingsDialog duration={editing} onClose={() => setEditing(null)} />
      )}
      {createOpen && <CreateDurationDialog durations={durations} onClose={() => setCreateOpen(false)} />}
    </section>
  )
}

function DurationSettingsDialog({ duration, onClose }: { duration: Duration; onClose: () => void }) {
  const updateDuration = useUpdateDuration()
  const deleteDuration = useDeleteDuration()
  const [nextEnabled, setNextEnabled] = useState(duration.is_enabled)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [saveError, setSaveError] = useState('')
  const [deleteError, setDeleteError] = useState('')
  const hasChanges = nextEnabled !== duration.is_enabled
  const busy = updateDuration.isPending || deleteDuration.isPending
  const durationLabel = formatDurationMinutes(duration.minutes)

  const save = async (event: FormEvent) => {
    event.preventDefault()
    if (!hasChanges) return
    setSaveError('')
    try {
      await updateDuration.mutateAsync({ id: duration.id, is_enabled: nextEnabled })
      onClose()
    } catch (cause) {
      setSaveError(cause instanceof ApiError ? cause.message : 'Не удалось изменить доступность срока')
    }
  }

  const remove = async () => {
    setDeleteError('')
    try {
      await deleteDuration.mutateAsync(duration.id)
      onClose()
    } catch (cause) {
      setDeleteError(cause instanceof ApiError
        ? cause.status === 409
          ? 'Этот срок уже используется в ценах, лотах, сделках/заказах или арендах, поэтому удалить его нельзя. Вернитесь назад, выключите срок и сохраните — существующие данные останутся.'
          : cause.message
        : 'Не удалось удалить срок аренды')
    }
  }

  if (confirmDelete) {
    return (
      <ModalOverlay key="duration-delete-confirm" onClose={() => setConfirmDelete(false)} canClose={!deleteDuration.isPending}>
        <div className="modal modal--compact catalog-delete-confirm" role="alertdialog" aria-modal="true" aria-busy={deleteDuration.isPending} aria-labelledby="delete-duration-title" aria-describedby="delete-duration-description">
          <div className="modal__danger-icon"><Icon name="trash" size={22} /></div>
          <h2 id="delete-duration-title">Удалить срок {durationLabel}?</h2>
          <p id="delete-duration-description">Срок исчезнет из справочника. Если он уже связан с ценами, лотами, сделками/заказами или арендами, сервер не даст его удалить — тогда безопасно выключите срок в настройках.</p>
          {deleteError && <div className="form-alert form-alert--error catalog-delete-confirm__alert" role="alert"><Icon name="warning" /><span>{deleteError}</span></div>}
          <div className="modal__actions">
            <button type="button" className="button button--secondary" onClick={() => { setConfirmDelete(false); setDeleteError('') }} disabled={deleteDuration.isPending} autoFocus>Назад</button>
            <button type="button" className="button button--danger" onClick={remove} disabled={deleteDuration.isPending}>{deleteDuration.isPending ? 'Удаляем…' : 'Удалить'}</button>
          </div>
        </div>
      </ModalOverlay>
    )
  }

  return (
    <ModalOverlay key="duration-settings" onClose={onClose} canClose={!busy}>
      <form className="modal catalog-settings-dialog" role="dialog" aria-modal="true" aria-busy={busy} aria-labelledby="duration-settings-title" aria-describedby="duration-settings-description" onSubmit={save}>
        <div className="modal__header">
          <div><span className="eyebrow">Настройка срока</span><h2 id="duration-settings-title">Срок {durationLabel}</h2><p id="duration-settings-description">Продолжительность зафиксирована после создания. В списках сроки автоматически располагаются по возрастанию минут.</p></div>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Закрыть" title="Закрыть" disabled={busy}><Icon name="close" /></button>
        </div>
        {saveError && <div className="form-alert form-alert--error" role="alert"><Icon name="warning" /><span>{saveError}</span></div>}
        <div className="catalog-toggle-row">
          <div><strong>Использовать в продажах</strong><small>Выключенный срок исчезнет из создания цен и новых лотов, не затрагивая существующие данные.</small></div>
          <label className="switch-control">
            <input data-autofocus type="checkbox" checked={nextEnabled} onChange={(event) => { setNextEnabled(event.target.checked); setSaveError('') }} disabled={busy} aria-label={`Использовать срок ${durationLabel} в продажах`} />
            <span aria-hidden="true" />
            <strong>{nextEnabled ? 'Включён' : 'Выключен'}</strong>
          </label>
        </div>
        <div className="modal__actions catalog-settings-actions">
          <button type="button" className="button button--secondary catalog-delete-trigger" onClick={() => { setConfirmDelete(true); setDeleteError('') }} disabled={busy}><Icon name="trash" size={16} />Удалить срок</button>
          <div className="catalog-settings-actions__primary">
            <button type="button" className="button button--secondary" onClick={onClose} disabled={busy}>Отмена</button>
            <button type="submit" className="button button--primary" disabled={busy || !hasChanges}>{updateDuration.isPending ? 'Сохраняем…' : 'Сохранить'}</button>
          </div>
        </div>
      </form>
    </ModalOverlay>
  )
}

function CreateDurationDialog({ durations, onClose }: { durations: Duration[]; onClose: () => void }) {
  const createDuration = useCreateDuration()
  const [mode, setMode] = useState<DurationInputMode>('minutes')
  const [amountInput, setAmountInput] = useState('30')
  const [enabled, setEnabled] = useState(true)
  const [attempted, setAttempted] = useState(false)
  const [serverError, setServerError] = useState('')
  const durationValidation = validateDurationInput(mode, amountInput, durations.map((duration) => duration.minutes))
  const durationError = attempted ? durationValidation.error : ''

  const selectMode = (nextMode: DurationInputMode) => {
    setMode(nextMode)
    setAmountInput(nextMode === 'minutes' ? '30' : '1')
    setAttempted(false)
    setServerError('')
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setAttempted(true)
    setServerError('')
    if (durationValidation.minutes === null || durationValidation.error) return
    try {
      await createDuration.mutateAsync({
        minutes: durationValidation.minutes,
        is_enabled: enabled,
      })
      onClose()
    } catch (cause) {
      setServerError(cause instanceof ApiError
        ? cause.status === 409
          ? `Срок «${formatDurationMinutes(durationValidation.minutes)}» уже существует. Обновите список и выберите другое значение.`
          : cause.message
        : 'Не удалось создать срок аренды')
    }
  }

  return (
    <ModalOverlay onClose={onClose} canClose={!createDuration.isPending}>
      <form className="modal catalog-settings-dialog" role="dialog" aria-modal="true" aria-labelledby="create-duration-title" aria-describedby="create-duration-description" onSubmit={submit}>
        <div className="modal__header">
          <div><span className="eyebrow">Справочник сроков</span><h2 id="create-duration-title">Новый срок аренды</h2><p id="create-duration-description">Укажите срок в минутах, часах или днях. Значение сохраняется в минутах с шагом 30 минут.</p></div>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Закрыть" title="Закрыть" disabled={createDuration.isPending}><Icon name="close" /></button>
        </div>
        {serverError && <div className="form-alert form-alert--error" role="alert"><Icon name="warning" /><span>{serverError}</span></div>}
        <fieldset className="catalog-duration-mode">
          <legend>Единица срока</legend>
          <div role="radiogroup" aria-label="Единица срока аренды">
            {([
              ['minutes', 'Минуты'],
              ['hours', 'Часы'],
              ['days', 'Дни'],
            ] as Array<[DurationInputMode, string]>).map(([value, label]) => (
              <label key={value}>
                <input data-autofocus={value === 'minutes' ? true : undefined} type="radio" name="duration-mode" value={value} checked={mode === value} onChange={() => selectMode(value)} disabled={createDuration.isPending} />
                <span>{label}</span>
              </label>
            ))}
          </div>
        </fieldset>
        <label className="field" htmlFor="create-duration-amount">
          <span className="field__label">Количество {mode === 'minutes' ? 'минут' : mode === 'hours' ? 'часов' : 'дней'}</span>
          <input id="create-duration-amount" type="number" min={mode === 'minutes' ? '30' : mode === 'hours' ? '0.5' : '1'} max={mode === 'minutes' ? '43200' : mode === 'hours' ? '720' : '30'} step={mode === 'minutes' ? '30' : mode === 'hours' ? '0.5' : '1'} inputMode="decimal" value={amountInput} onChange={(event) => { setAmountInput(event.target.value); setServerError('') }} onBlur={() => setAttempted(true)} disabled={createDuration.isPending} aria-invalid={Boolean(durationError)} aria-describedby="create-duration-amount-hint" />
          <small id="create-duration-amount-hint" className={`field__hint ${durationError ? 'text-danger' : ''}`}>{durationError || (mode === 'minutes' ? 'От 30 до 43 200, шаг 30 минут.' : mode === 'hours' ? 'От 0,5 до 720, шаг 0,5 часа.' : 'Целое число от 1 до 30.')}</small>
        </label>
        <div className="catalog-duration-summary" aria-live="polite"><span>Итоговый срок</span><strong>{durationValidation.minutes === null ? 'Укажите корректное значение' : formatDurationMinutes(durationValidation.minutes)}</strong></div>
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
  const [editing, setEditing] = useState<LimitScope | null>(null)

  if (query.isLoading) return <LoadingState label="Загружаем типы лимитов" />
  if (query.isError) return <ErrorState onRetry={() => query.refetch()} />

  const scopes = [...(query.data ?? [])]
    .filter((scope) => scope.code.toLowerCase() !== 'chat')
    .sort(compareOfferScopes)
  const descriptions: Record<string, string> = {
    any: 'Без гарантии остатка конкретного лимита. Подходит для базовых предложений.',
    codex: 'Единый измеримый лимит Codex со всеми фактическими окнами OpenAI.',
  }

  return (
    <section className="panel panel--flush">
      <div className="section-toolbar"><div><h2>Типы лимитов</h2><p>Определяют, какие показатели учитываются при подборе аккаунта.</p></div></div>
      <div className="form-alert form-alert--info catalog-system-note"><Icon name="activity" /><span>Коды и названия типов лимитов системные: по ним работает подбор аккаунта. Здесь управляется только доступность, а пороговые проценты настраиваются в разделе «Цены».</span></div>
      {scopes.length === 0 ? <EmptyState icon="activity" title="Типы лимитов не инициализированы" description="Ожидаются системные значения any и codex." /> : (
        <div className="scope-grid">{scopes.map((scope) => {
          const code = scope.code.toLowerCase()
          const unavailable = !isSupportedOfferScopeCode(code)
          return (
            <article className={`scope-card ${unavailable ? 'scope-card--unavailable' : ''} ${scope.is_enabled ? '' : 'scope-card--disabled'}`} key={scope.id} data-state={scope.is_enabled ? 'enabled' : 'disabled'}>
              <div className="scope-card__main">
                <div className="scope-card__icon"><Icon name={code === 'codex' ? 'templates' : unavailable ? 'activity' : 'catalog'} /></div>
                <div className="scope-card__body"><span className="eyebrow">{code}</span><h3>{scope.name}</h3>{unavailable && <StatusBadge value="disabled" label="Недоступно · не поддерживается" />}<p>{descriptions[code] ?? 'Неизвестный системный тип не используется в новых предложениях.'}</p></div>
              </div>
              <div className="catalog-card__footer">
                {unavailable ? (
                  <span className="catalog-card__locked" title="Неизвестный системный тип не поддерживается"><Icon name="shield" size={14} />Системное ограничение</span>
                ) : (
                  <>
                    <StatusBadge value={scope.is_enabled ? 'active' : 'paused'} label={scope.is_enabled ? 'Включён' : 'Выключен'} />
                    <button type="button" className="catalog-card__edit" onClick={() => setEditing(scope)} aria-label={`Настроить доступность типа лимита ${scope.name}`} title="Настроить доступность">
                      <Icon name="settings" size={15} /><span>Настроить</span>
                    </button>
                  </>
                )}
              </div>
            </article>
          )
        })}</div>
      )}
      {editing && (
        <ScopeAvailabilityDialog scope={editing} onClose={() => setEditing(null)} />
      )}
    </section>
  )
}

function ScopeAvailabilityDialog({ scope, onClose }: { scope: LimitScope; onClose: () => void }) {
  const updateScope = useUpdateLimitScope()
  const [nextEnabled, setNextEnabled] = useState(scope.is_enabled)
  const [error, setError] = useState('')
  const hasChanges = nextEnabled !== scope.is_enabled

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!hasChanges) return
    setError('')
    try {
      await updateScope.mutateAsync({ id: scope.id, is_enabled: nextEnabled })
      onClose()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Не удалось изменить доступность типа лимита')
    }
  }

  return (
    <ModalOverlay onClose={onClose} canClose={!updateScope.isPending}>
      <form className="modal catalog-settings-dialog" role="dialog" aria-modal="true" aria-busy={updateScope.isPending} aria-labelledby="scope-settings-title" aria-describedby="scope-settings-description" onSubmit={submit}>
        <div className="modal__header">
          <div><span className="eyebrow">Доступность типа лимита</span><h2 id="scope-settings-title">{scope.name}</h2><p id="scope-settings-description">Название «{scope.name}» и системный код {scope.code} используются логикой подбора аккаунта и не редактируются. Здесь меняется только доступность.</p></div>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Закрыть" title="Закрыть" disabled={updateScope.isPending}><Icon name="close" /></button>
        </div>
        {error && <div className="form-alert form-alert--error" role="alert"><Icon name="warning" /><span>{error}</span></div>}
        <div className="catalog-toggle-row">
          <div><strong>Использовать в новых предложениях</strong><small>Выключенный тип исчезнет из создания цен, лотов и шаблонов. Пороговые проценты задаются в разделе «Цены».</small></div>
          <label className="switch-control">
            <input data-autofocus type="checkbox" checked={nextEnabled} onChange={(event) => { setNextEnabled(event.target.checked); setError('') }} disabled={updateScope.isPending} aria-label={`Использовать тип лимита ${scope.name} в новых предложениях`} />
            <span aria-hidden="true" />
            <strong>{nextEnabled ? 'Включён' : 'Выключен'}</strong>
          </label>
        </div>
        <div className="modal__actions">
          <button type="button" className="button button--secondary" onClick={onClose} disabled={updateScope.isPending}>Отмена</button>
          <button type="submit" className="button button--primary" disabled={updateScope.isPending || !hasChanges}>{updateScope.isPending ? 'Сохраняем…' : 'Сохранить'}</button>
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
