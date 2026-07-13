import { useEffect, useState } from 'react'
import { useMetrics } from '../api/metrics'
import { ApiError } from '../api/client'
import {
  useChangeAdminPassword,
  useClearFunPayKey,
  useClearTelegramConfig,
  useFunPayKeyStatus,
  useSetFunPayKey,
  useSettings,
  useTelegramConfig,
  useTestTelegramConfig,
  useUpdateSettings,
  useUpdateTelegramConfig,
} from '../api/settings'
import { Icon } from '../components/Icon'
import { ErrorState, LoadingState, PageHeader, StatusBadge } from '../components/ui'
import type { Settings as SettingsType } from '../types/api'

const DEFAULT_SETTINGS: SettingsType = {
  funpay_node_id: null,
  auto_bump_enabled: true,
  bump_interval_hours: 4,
  default_max_active_rentals: 1,
  funpay_commission_percent: 15,
  check_interval_minutes: 1440,
  limits_check_interval_minutes: 5,
  refresh_recover_concurrency: 3,
  refresh_max_attempts: 3,
  refresh_retry_delay_minutes: 5,
  check_delay_seconds: 45,
  limits_warn_threshold_pct: 20,
}

export default function Settings() {
  const settingsQuery = useSettings()
  const metricsQuery = useMetrics()
  const funPayKeyQuery = useFunPayKeyStatus()
  const telegramQuery = useTelegramConfig()
  const update = useUpdateSettings()
  const setFunPayKey = useSetFunPayKey()
  const clearFunPayKey = useClearFunPayKey()
  const updateTelegram = useUpdateTelegramConfig()
  const clearTelegram = useClearTelegramConfig()
  const testTelegram = useTestTelegramConfig()
  const changePassword = useChangeAdminPassword()
  const [draft, setDraft] = useState<SettingsType>(DEFAULT_SETTINGS)
  const [dirty, setDirty] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [goldenKey, setGoldenKey] = useState('')
  const [telegramToken, setTelegramToken] = useState('')
  const [telegramChatId, setTelegramChatId] = useState('')
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')

  const missingSettings = settingsQuery.error instanceof ApiError && settingsQuery.error.status === 404

  useEffect(() => {
    if (settingsQuery.data && !dirty) setDraft(settingsQuery.data)
  }, [settingsQuery.data, dirty])

  useEffect(() => {
    if (telegramQuery.data) setTelegramChatId(telegramQuery.data.seller_chat_id ?? '')
  }, [telegramQuery.data])

  if (settingsQuery.isLoading) return <LoadingState label="Загружаем конфигурацию" />
  if (settingsQuery.isError && !missingSettings) return <ErrorState message="Не удалось загрузить настройки" onRetry={() => settingsQuery.refetch()} />

  const change = <K extends keyof SettingsType>(field: K, value: SettingsType[K]) => {
    setDraft((current) => ({ ...current, [field]: value }))
    setDirty(true)
    setSuccess('')
    setError('')
  }

  const save = async () => {
    setError('')
    if (draft.funpay_node_id !== null && draft.funpay_node_id <= 0) {
      setError('FunPay Node ID должен быть положительным числом или пустым.')
      return
    }
    if (draft.limits_check_interval_minutes < 1 || draft.limits_check_interval_minutes > 55) {
      setError('Замер лимитов должен выполняться каждые 1–55 минут: пятиминутный запас оставляет время очереди до часовой границы свежести.')
      return
    }
    if (
      draft.default_max_active_rentals < 1
      || draft.funpay_commission_percent < 0
      || draft.funpay_commission_percent > 100
      || draft.check_interval_minutes < 1
      || draft.check_interval_minutes > 10_080
      || draft.bump_interval_hours < 1
      || draft.bump_interval_hours > 168
      || draft.limits_warn_threshold_pct < 0
      || draft.limits_warn_threshold_pct > 100
    ) {
      setError('Проверьте основные интервалы и лимит аренд; проценты должны быть от 0 до 100.')
      return
    }
    if (
      draft.refresh_recover_concurrency < 1
      || draft.refresh_recover_concurrency > 20
      || draft.refresh_max_attempts < 1
      || draft.refresh_max_attempts > 20
      || draft.refresh_retry_delay_minutes < 1
      || draft.refresh_retry_delay_minutes > 1_440
      || draft.check_delay_seconds < 30
      || draft.check_delay_seconds > 3_600
    ) {
      setError('Восстановление проверок: параллельность и попытки — от 1 до 20, пауза — до 1440 минут, опрос очереди — от 30 до 3600 секунд.')
      return
    }
    try {
      const canonical = await update.mutateAsync(draft)
      setDraft(canonical)
      setDirty(false)
      setSuccess('Настройки сохранены.')
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Не удалось сохранить настройки')
    }
  }

  const discard = () => {
    setDraft(settingsQuery.data ?? DEFAULT_SETTINGS)
    setDirty(false)
    setError('')
    setSuccess('')
  }

  const saveFunPayKey = async () => {
    setError('')
    setSuccess('')
    if (goldenKey.trim().length < 16) {
      setError('Golden key должен содержать не менее 16 символов.')
      return
    }
    try {
      await setFunPayKey.mutateAsync(goldenKey.trim())
      setGoldenKey('')
      setFunPayKey.reset()
      setSuccess('Golden key проверен, сохранён и подключён. Предыдущее соединение заменено без простоя.')
    } catch (cause) {
      setError(errorMessage(cause, 'Не удалось сохранить Golden key'))
    }
  }

  const removeFunPayKey = async () => {
    setError('')
    try {
      await clearFunPayKey.mutateAsync()
      setGoldenKey('')
      setSuccess('Сохранённый Golden key удалён.')
    } catch (cause) {
      setError(errorMessage(cause, 'Не удалось удалить Golden key'))
    }
  }

  const saveTelegram = async () => {
    setError('')
    setSuccess('')
    const body: { token?: string; seller_chat_id?: string } = {
      seller_chat_id: telegramChatId.trim(),
    }
    if (telegramToken.trim()) body.token = telegramToken.trim()
    try {
      await updateTelegram.mutateAsync(body)
      setTelegramToken('')
      updateTelegram.reset()
      setSuccess('Настройки Telegram сохранены.')
    } catch (cause) {
      setError(errorMessage(cause, 'Не удалось сохранить Telegram'))
    }
  }

  const sendTelegramTest = async () => {
    setError('')
    try {
      await testTelegram.mutateAsync()
      setSuccess('Тестовое сообщение отправлено в Telegram.')
    } catch (cause) {
      setError(errorMessage(cause, 'Тест Telegram не прошёл'))
    }
  }

  const removeTelegram = async () => {
    setError('')
    try {
      const status = await clearTelegram.mutateAsync()
      setTelegramToken('')
      setTelegramChatId(status.seller_chat_id ?? '')
      setSuccess('Сохранённая конфигурация Telegram очищена.')
    } catch (cause) {
      setError(errorMessage(cause, 'Не удалось очистить Telegram'))
    }
  }

  const savePassword = async () => {
    setError('')
    setSuccess('')
    if (newPassword.length < 12) {
      setError('Новый пароль должен содержать не менее 12 символов.')
      return
    }
    if (newPassword !== confirmPassword) {
      setError('Подтверждение нового пароля не совпадает.')
      return
    }
    try {
      await changePassword.mutateAsync({ current_password: currentPassword, new_password: newPassword })
      setCurrentPassword('')
      setNewPassword('')
      setConfirmPassword('')
      changePassword.reset()
      setSuccess('Пароль изменён. Все остальные админ-сессии отозваны.')
    } catch (cause) {
      setError(errorMessage(cause, 'Не удалось изменить пароль'))
    }
  }

  return (
    <div className="page-stack settings-page">
      <PageHeader
        eyebrow="Система"
        title="Настройки"
        description="Интеграции, автоматические задачи, ограничения и финансовые параметры."
        actions={<div className="header-action-group"><button className="button button--secondary" onClick={discard} disabled={!dirty}>Отменить</button><button className="button button--primary" onClick={save} disabled={!dirty || update.isPending}>{update.isPending ? <><span className="spinner spinner--light" />Сохраняем…</> : <><Icon name="check" />Сохранить настройки</>}</button></div>}
      />

      {missingSettings && <div className="form-alert form-alert--warning"><Icon name="warning" /><span>Это первый запуск: строка системных настроек ещё не создана. Заполните форму и сохраните её.</span></div>}
      {error && <div className="form-alert form-alert--error"><Icon name="warning" /><span>{error}</span></div>}
      {success && <div className="form-alert form-alert--success"><Icon name="check" /><span>{success}</span></div>}

      <section className="settings-section">
        <div className="settings-section__intro"><div className="settings-section__icon"><Icon name="activity" /></div><div><h2>FunPay</h2><p>Подключение продавца, категория лотов и состояние событий.</p></div></div>
        <div className="settings-card settings-card--featured">
          <div className="integration-head">
            <div className="integration-logo">FP</div>
            <div><strong>FunPay Seller API</strong><span>События заказов, сообщения и управление лотами</span></div>
            <StatusBadge value={metricsQuery.data?.bot_status ?? 'unknown'} />
          </div>
          <div className="integration-grid">
            <label className="field"><span className="field__label">Golden key</span><input type="password" autoComplete="off" value={goldenKey} onChange={(event) => setGoldenKey(event.target.value)} placeholder={funPayKeyQuery.data?.configured ? `${funPayKeyQuery.data.connected ? 'Подключён' : 'Настроен, но не подключён'} ••••${funPayKeyQuery.data.last4 ?? ''}` : 'Вставьте golden_key из cookies FunPay'} /><span className="field__hint">Новый ключ сначала проверяется; неверный ключ не отключит текущее рабочее соединение. Значение хранится зашифрованно и не возвращается в браузер.</span></label>
            <label className="field"><span className="field__label">FunPay Node ID</span><input type="number" min="1" value={draft.funpay_node_id ?? ''} onChange={(event) => change('funpay_node_id', event.target.value === '' ? null : Number(event.target.value))} placeholder="Например, 1234" /><span className="field__hint">Категория, в которой бот создаёт и обновляет лоты.</span></label>
          </div>
          <div className="integration-actions"><button className="button button--primary" onClick={saveFunPayKey} disabled={!goldenKey.trim() || setFunPayKey.isPending}><Icon name="key" />{setFunPayKey.isPending ? 'Подключаем…' : 'Сохранить ключ'}</button>{funPayKeyQuery.data?.configured && <button className="button button--secondary" onClick={removeFunPayKey} disabled={clearFunPayKey.isPending}>Удалить ключ</button>}<span className={metricsQuery.data?.bot_status === 'connected' ? 'integration-note--ok' : ''}><Icon name={metricsQuery.data?.bot_status === 'connected' ? 'check' : 'warning'} size={15} />{metricsQuery.data?.bot_status === 'connected' ? 'FunPay принимает события' : 'После сохранения проверяется подключение'}</span></div>
        </div>
      </section>

      <section className="settings-section">
        <div className="settings-section__intro"><div className="settings-section__icon"><Icon name="clock" /></div><div><h2>Автоматизация</h2><p>Частота проверок, замеров лимитов и поднятия лотов.</p></div></div>
        <div className="settings-card">
          <div className="form-grid form-grid--3">
            <NumberField label="Проверка аккаунтов" suffix="мин" min={1} max={10_080} value={draft.check_interval_minutes} onChange={(value) => change('check_interval_minutes', value)} hint="Полный вход; по умолчанию раз в сутки" />
            <NumberField label="Замер лимитов" suffix="мин" min={1} max={55} value={draft.limits_check_interval_minutes} onChange={(value) => change('limits_check_interval_minutes', value)} hint="1–55 мин; запас 5 минут до часовой границы свежести" />
            <NumberField label="Интервал bump" suffix="ч" min={1} max={168} value={draft.bump_interval_hours} onChange={(value) => change('bump_interval_hours', value)} hint="Cooldown поднятия категории" />
          </div>
          <div className="settings-card__subsection">
            <div className="settings-card__subsection-head"><strong>Восстановление фоновых проверок</strong><span>Ограничивает нагрузку и повторяет временно неудачные проверки без ручного вмешательства.</span></div>
            <div className="form-grid form-grid--3">
              <NumberField label="Параллельных задач" suffix="шт" min={1} max={20} value={draft.refresh_recover_concurrency} onChange={(value) => change('refresh_recover_concurrency', value)} hint="Одновременные проверки восстановления" />
              <NumberField label="Попыток на задачу" suffix="шт" min={1} max={20} value={draft.refresh_max_attempts} onChange={(value) => change('refresh_max_attempts', value)} hint="После лимита задача помечается ошибкой" />
              <NumberField label="Пауза между попытками" suffix="мин" min={1} max={1_440} value={draft.refresh_retry_delay_minutes} onChange={(value) => change('refresh_retry_delay_minutes', value)} hint="Задержка перед повторным запуском" />
              <NumberField label="Опрос очереди" suffix="сек" min={30} max={3_600} value={draft.check_delay_seconds} onChange={(value) => change('check_delay_seconds', value)} hint="Как часто запускать обработчик проверок" />
            </div>
          </div>
          <label className="switch-row"><span><strong>Автоматически поднимать лоты</strong><small>Пытаться выполнять бесплатный bump после истечения cooldown</small></span><input type="checkbox" checked={draft.auto_bump_enabled} onChange={(event) => change('auto_bump_enabled', event.target.checked)} /><span className="switch" /></label>
        </div>
      </section>

      <section className="settings-section">
        <div className="settings-section__intro"><div className="settings-section__icon"><Icon name="prices" /></div><div><h2>Продажи и ёмкость</h2><p>Общие ограничения пула и расчёт чистой выручки.</p></div></div>
        <div className="settings-card">
          <div className="form-grid form-grid--3">
            <NumberField label="Аренд на аккаунт" suffix="шт" min={1} value={draft.default_max_active_rentals} onChange={(value) => change('default_max_active_rentals', value)} hint="Если у аккаунта нет override" />
            <NumberField label="Комиссия FunPay" suffix="%" min={0} max={100} value={draft.funpay_commission_percent} onChange={(value) => change('funpay_commission_percent', value)} hint="Для расчёта netto" />
            <NumberField label="Порог предупреждения" suffix="%" min={0} max={100} value={draft.limits_warn_threshold_pct} onChange={(value) => change('limits_warn_threshold_pct', value)} hint="Уведомление о низком остатке" />
          </div>
        </div>
      </section>

      <section className="settings-section">
        <div className="settings-section__intro"><div className="settings-section__icon"><Icon name="shield" /></div><div><h2>Telegram и безопасность</h2><p>Уведомления продавцу и управление доступом.</p></div></div>
        <div className="settings-security-grid">
          <div className="settings-card">
            <div className="security-card__head"><div><Icon name="deals" /><span><strong>Telegram-уведомления</strong><small>{telegramQuery.data?.configured ? `Настроено ••••${telegramQuery.data.token_last4 ?? ''}` : 'Не настроено'}</small></span></div><StatusBadge value={telegramQuery.data?.configured ? 'active' : 'disabled'} /></div>
            <div className="form-stack compact-form-stack">
              <label className="field"><span className="field__label">Bot token</span><input type="password" autoComplete="off" value={telegramToken} onChange={(event) => setTelegramToken(event.target.value)} placeholder={telegramQuery.data?.configured ? 'Оставьте пустым, чтобы не менять' : '123456:ABC…'} /></label>
              <label className="field"><span className="field__label">Seller chat ID</span><input value={telegramChatId} onChange={(event) => setTelegramChatId(event.target.value)} placeholder="Например, 123456789" /></label>
            </div>
            <div className="security-card__actions"><button className="button button--primary" onClick={saveTelegram} disabled={updateTelegram.isPending || (!telegramToken.trim() && !telegramChatId.trim())}>Сохранить</button><button className="button button--secondary" onClick={sendTelegramTest} disabled={!telegramQuery.data?.configured || testTelegram.isPending}>Тест</button>{telegramQuery.data?.configured && <button className="button button--ghost" onClick={removeTelegram} disabled={clearTelegram.isPending}>Очистить</button>}</div>
          </div>
          <div className="settings-card">
            <div className="security-card__head"><div><Icon name="key" /><span><strong>Пароль администратора</strong><small>После смены остальные сессии будут отозваны</small></span></div><StatusBadge value="active" /></div>
            <div className="form-stack compact-form-stack">
              <label className="field"><span className="field__label">Текущий пароль</span><input type="password" autoComplete="current-password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} /></label>
              <label className="field"><span className="field__label">Новый пароль</span><input type="password" autoComplete="new-password" minLength={12} value={newPassword} onChange={(event) => setNewPassword(event.target.value)} /></label>
              <label className="field"><span className="field__label">Повторите пароль</span><input type="password" autoComplete="new-password" minLength={12} value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} /></label>
            </div>
            <div className="security-card__actions"><button className="button button--primary" onClick={savePassword} disabled={changePassword.isPending || !currentPassword || !newPassword || !confirmPassword}>Изменить пароль</button></div>
          </div>
        </div>
      </section>

      {dirty && <div className="unsaved-bar"><span><span className="status-dot status-dot--warning" />Настройки изменены локально</span><div><button className="button button--ghost" onClick={discard}>Отменить</button><button className="button button--primary" onClick={save} disabled={update.isPending}>Сохранить настройки</button></div></div>}
    </div>
  )
}

function errorMessage(cause: unknown, fallback: string) {
  return cause instanceof ApiError ? cause.message : fallback
}

function NumberField({ label, suffix, value, min, max, hint, onChange }: { label: string; suffix: string; value: number; min: number; max?: number; hint: string; onChange: (value: number) => void }) {
  return <label className="field"><span className="field__label">{label}</span><span className="number-with-suffix"><input type="number" min={min} max={max} value={value} onChange={(event) => onChange(Number(event.target.value))} /><span>{suffix}</span></span><span className="field__hint">{hint}</span></label>
}
