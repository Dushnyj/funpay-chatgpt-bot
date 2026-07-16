import { useState } from 'react'
import { ApiError } from '../api/client'
import {
  useCreateHomeRelaySetup,
  useCreateLoginRoute,
  useDeleteLoginRoute,
  useLoginRoutes,
  useSetDefaultLoginRoute,
  useTestLoginRoute,
  useUpdateLoginRoute,
} from '../api/loginRoutes'
import type { HomeRelaySetup, LoginProxyType, LoginRoute, LoginRoutePatch, LoginRouteWrite } from '../types/api'
import { formatDateTime } from '../utils/format'
import {
  hasProxyRouteErrors,
  loginRouteErrorLabel,
  loginRouteEndpoint,
  loginRouteKindLabel,
  loginRouteStatusLabel,
  loginRouteStatusTone,
  proxyTypeLabel,
  validateProxyRouteDraft,
  type ProxyRouteDraft,
  type ProxyRouteErrors,
} from '../utils/loginRoutes'
import { Icon } from './Icon'
import { ModalOverlay } from './ui'

const EMPTY_PROXY: ProxyRouteDraft = {
  name: '',
  proxyType: 'socks5',
  host: '',
  port: '1080',
  username: '',
  password: '',
}

export function LoginRoutesSection() {
  const routesQuery = useLoginRoutes()
  const setDefault = useSetDefaultLoginRoute()
  const testRoute = useTestLoginRoute()
  const updateRoute = useUpdateLoginRoute()
  const deleteRoute = useDeleteLoginRoute()
  const [proxyEditor, setProxyEditor] = useState<LoginRoute | 'new' | null>(null)
  const [homeSetupOpen, setHomeSetupOpen] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<LoginRoute | null>(null)
  const [busyRouteId, setBusyRouteId] = useState<number | null>(null)
  const [feedback, setFeedback] = useState<{ tone: 'success' | 'error'; text: string } | null>(null)

  const run = async (route: LoginRoute) => {
    setFeedback(null)
    setBusyRouteId(route.id)
    try {
      const result = await testRoute.mutateAsync(route.id)
      setFeedback(result.status === 'online'
        ? { tone: 'success', text: `${route.name}: маршрут доступен${result.egress_ip ? `, внешний IP ${result.egress_ip}` : ''}${result.latency_ms !== null ? `, ${result.latency_ms} мс` : ''}.` }
        : { tone: 'error', text: `${route.name}: ${loginRouteErrorLabel(result.last_error) || 'маршрут недоступен.'}` })
    } catch (cause) {
      setFeedback({ tone: 'error', text: apiMessage(cause, 'Не удалось проверить маршрут') })
    } finally {
      setBusyRouteId(null)
    }
  }

  const makeDefault = async (routeId: number | null) => {
    setFeedback(null)
    setBusyRouteId(routeId ?? -1)
    try {
      await setDefault.mutateAsync(routeId)
      setFeedback({
        tone: 'success',
        text: routeId === null
          ? 'Для новых проверок выбран прямой выход сервера.'
          : 'Маршрут входа по умолчанию изменён.',
      })
    } catch (cause) {
      setFeedback({ tone: 'error', text: apiMessage(cause, 'Не удалось изменить маршрут по умолчанию') })
    } finally {
      setBusyRouteId(null)
    }
  }

  const toggle = async (route: LoginRoute) => {
    setFeedback(null)
    setBusyRouteId(route.id)
    try {
      await updateRoute.mutateAsync({ id: route.id, enabled: !route.enabled })
      setFeedback({ tone: 'success', text: `${route.name}: ${route.enabled ? 'маршрут отключён' : 'маршрут включён'}.` })
    } catch (cause) {
      setFeedback({ tone: 'error', text: apiMessage(cause, 'Не удалось изменить маршрут') })
    } finally {
      setBusyRouteId(null)
    }
  }

  const remove = async () => {
    if (!deleteTarget) return
    setFeedback(null)
    try {
      await deleteRoute.mutateAsync(deleteTarget.id)
      setFeedback({ tone: 'success', text: `Маршрут «${deleteTarget.name}» удалён.` })
      setDeleteTarget(null)
    } catch (cause) {
      setFeedback({ tone: 'error', text: apiMessage(cause, 'Не удалось удалить маршрут') })
      setDeleteTarget(null)
    }
  }

  const routes = routesQuery.data?.routes ?? []
  const defaultRouteId = routesQuery.data?.default_route_id ?? null

  const testHomeRelay = async () => {
    setHomeSetupOpen(false)
    const refreshed = await routesQuery.refetch()
    const homeRelay = refreshed.data?.routes.find((route) => route.mode === 'home_relay')
    if (!homeRelay) {
      setFeedback({ tone: 'error', text: 'Домашний маршрут ещё не появился. Проверьте результат команды PowerShell и повторите.' })
      return
    }
    await run(homeRelay)
  }

  return (
    <>
      <section className="settings-section login-routes-section">
        <div className="settings-section__intro">
          <div className="settings-section__icon"><Icon name="route" /></div>
          <div><h2>Маршрут входа</h2><p>Отдельный сетевой выход для OpenAI и почты, не затрагивающий сайты и FunPay.</p></div>
        </div>

        <div className="settings-card login-routes-card">
          <div className="login-routes-head">
            <div>
              <span className="eyebrow">Сеть авторизации</span>
              <h3>Прокси для входа в аккаунты</h3>
              <p>Применяется к входу OpenAI и чтению кодов из почты через Outlook Web или IMAP. Microsoft Graph, FunPay и остальной сервер продолжают работать напрямую.</p>
            </div>
            <div className="login-routes-head__actions">
              <button className="button button--secondary" type="button" onClick={() => setProxyEditor('new')}><Icon name="plus" />Добавить прокси</button>
              <button className="button button--primary" type="button" onClick={() => setHomeSetupOpen(true)}><Icon name="home" />Настроить домашний прокси</button>
            </div>
          </div>

          <div className="login-route-scope-note"><Icon name="shield" size={16} /><span><strong>Fail-closed:</strong> если выбранный маршрут недоступен, вход остановится и не переключится незаметно на IP сервера.</span></div>

          {feedback && <div className={`form-alert form-alert--${feedback.tone === 'success' ? 'success' : 'error'} login-route-feedback`} role={feedback.tone === 'error' ? 'alert' : 'status'}><Icon name={feedback.tone === 'success' ? 'check' : 'warning'} /><span>{feedback.text}</span></div>}

          {routesQuery.isLoading ? (
            <div className="login-routes-loading" role="status"><span className="spinner" />Загружаем маршруты…</div>
          ) : routesQuery.isError ? (
            <div className="login-routes-error" role="alert"><Icon name="warning" /><div><strong>Маршруты пока недоступны</strong><span>{apiMessage(routesQuery.error, 'Не удалось получить конфигурацию')}</span></div><button className="button button--secondary" type="button" onClick={() => routesQuery.refetch()}><Icon name="refresh" />Повторить</button></div>
          ) : (
            <div className="login-route-list">
              <article className={`login-route-row ${defaultRouteId === null ? 'login-route-row--default' : ''}`}>
                <div className="login-route-row__icon"><Icon name="database" /></div>
                <div className="login-route-row__identity"><div><strong>Прямой выход сервера</strong>{defaultRouteId === null && <span className="login-route-default">По умолчанию</span>}</div><span>Без прокси · IP дата-центра</span></div>
                <div className="login-route-row__health"><RouteStatus tone="warning" label="Без проверки" /><small>Резервное переключение отключено</small></div>
                <div className="login-route-row__actions">{defaultRouteId !== null && <button className="button button--secondary button--compact" type="button" onClick={() => makeDefault(null)} disabled={busyRouteId !== null}>Выбрать</button>}</div>
              </article>

              {routes.map((route) => (
                <article key={route.id} className={`login-route-row ${defaultRouteId === route.id || route.is_default ? 'login-route-row--default' : ''} ${!route.enabled ? 'login-route-row--disabled' : ''}`}>
                  <div className="login-route-row__icon"><Icon name={route.mode === 'home_relay' ? 'home' : 'route'} /></div>
                  <div className="login-route-row__identity">
                    <div><strong>{route.name}</strong>{(defaultRouteId === route.id || route.is_default) && <span className="login-route-default">По умолчанию</span>}</div>
                    <span>{loginRouteKindLabel(route)} · {loginRouteEndpoint(route)}</span>
                    {route.has_password && <small><Icon name="key" size={12} />Авторизация настроена, пароль скрыт</small>}
                  </div>
                  <div className="login-route-row__health">
                    <RouteStatus tone={loginRouteStatusTone(route.status)} label={route.enabled ? loginRouteStatusLabel(route.status) : 'Отключён'} />
                    <small>{route.egress_ip ? `IP ${route.egress_ip}` : 'IP ещё не определён'}{route.latency_ms !== null ? ` · ${route.latency_ms} мс` : ''}</small>
                    {route.last_checked_at && <small>Проверен {formatDateTime(route.last_checked_at)}</small>}
                    {route.status === 'offline' && route.last_error && <small className="login-route-row__error">{loginRouteErrorLabel(route.last_error)}</small>}
                  </div>
                  <div className="login-route-row__actions">
                    <button className="icon-button" type="button" onClick={() => run(route)} disabled={!route.enabled || busyRouteId !== null} title="Проверить маршрут" aria-label={`Проверить маршрут ${route.name}`}>{busyRouteId === route.id && testRoute.isPending ? <span className="spinner" /> : <Icon name="refresh" />}</button>
                    {defaultRouteId !== route.id && !route.is_default && <button className="icon-button" type="button" onClick={() => makeDefault(route.id)} disabled={!route.enabled || route.status !== 'online' || busyRouteId !== null} title={route.status === 'online' ? 'Использовать по умолчанию' : 'Сначала успешно проверьте маршрут'} aria-label={`Сделать маршрут ${route.name} основным`}><Icon name="check" /></button>}
                    {route.mode === 'custom_proxy' && <button className="icon-button" type="button" onClick={() => setProxyEditor(route)} disabled={busyRouteId !== null} title="Изменить" aria-label={`Изменить маршрут ${route.name}`}><Icon name="settings" /></button>}
                    <button className="icon-button" type="button" onClick={() => toggle(route)} disabled={busyRouteId !== null || (route.enabled && (defaultRouteId === route.id || route.is_default))} title={route.enabled && (defaultRouteId === route.id || route.is_default) ? 'Сначала выберите другой основной маршрут' : route.enabled ? 'Отключить' : 'Включить'} aria-label={`${route.enabled ? 'Отключить' : 'Включить'} маршрут ${route.name}`}><Icon name="power" /></button>
                    <button className="icon-button icon-button--danger" type="button" onClick={() => setDeleteTarget(route)} disabled={busyRouteId !== null || defaultRouteId === route.id || route.is_default} title={defaultRouteId === route.id || route.is_default ? 'Сначала выберите другой основной маршрут' : 'Удалить'} aria-label={`Удалить маршрут ${route.name}`}><Icon name="trash" /></button>
                  </div>
                </article>
              ))}
            </div>
          )}
        </div>
      </section>

      {proxyEditor && <ProxyRouteDialog route={proxyEditor === 'new' ? null : proxyEditor} onClose={() => setProxyEditor(null)} onSaved={(message) => { setProxyEditor(null); setFeedback({ tone: 'success', text: message }) }} />}
      {homeSetupOpen && <HomeRelayDialog onClose={() => { setHomeSetupOpen(false); void routesQuery.refetch() }} onReadyToTest={testHomeRelay} />}
      {deleteTarget && <ModalOverlay onClose={() => setDeleteTarget(null)}><div className="modal modal--compact" role="alertdialog" aria-modal="true" aria-labelledby="delete-login-route-title"><div className="modal__danger-icon"><Icon name="trash" /></div><h2 id="delete-login-route-title">Удалить «{deleteTarget.name}»?</h2><p>Сначала переназначьте привязанные аккаунты. Сохранённые данные прокси будут удалены{deleteTarget.mode === 'home_relay' ? ', а домашний SSH-ключ — отозван' : ''}.</p><div className="modal__actions"><button className="button button--secondary" type="button" onClick={() => setDeleteTarget(null)}>Отмена</button><button className="button button--danger" type="button" onClick={remove} disabled={deleteRoute.isPending}>{deleteRoute.isPending ? 'Удаляем…' : 'Удалить'}</button></div></div></ModalOverlay>}
    </>
  )
}

function RouteStatus({ tone, label }: { tone: 'success' | 'warning' | 'danger'; label: string }) {
  return <span className={`status-badge status-badge--${tone}`}><span className={`status-dot status-dot--${tone}`} />{label}</span>
}

function ProxyRouteDialog({ route, onClose, onSaved }: { route: LoginRoute | null; onClose: () => void; onSaved: (message: string) => void }) {
  const createRoute = useCreateLoginRoute()
  const updateRoute = useUpdateLoginRoute()
  const [draft, setDraft] = useState<ProxyRouteDraft>(() => route ? {
    name: route.name,
    proxyType: route.proxy_type ?? 'socks5',
    host: route.host ?? '',
    port: route.port?.toString() ?? '1080',
    username: route.username ?? '',
    password: '',
    hasSavedPassword: route.has_password,
    savedUsername: route.username ?? '',
  } : EMPTY_PROXY)
  const [errors, setErrors] = useState<ProxyRouteErrors>({})
  const [submitError, setSubmitError] = useState('')
  const [clearCredentials, setClearCredentials] = useState(Boolean(route?.has_password && route.proxy_type === 'socks5'))

  const change = <K extends keyof ProxyRouteDraft>(field: K, value: ProxyRouteDraft[K]) => {
    setDraft((current) => ({ ...current, [field]: value }))
    setErrors((current) => ({ ...current, [field === 'proxyType' ? 'host' : field]: undefined, ...(field === 'username' || field === 'password' ? { credentials: undefined } : {}) }))
    setSubmitError('')
  }

  const changeProxyType = (proxyType: LoginProxyType) => {
    setDraft((current) => ({
      ...current,
      proxyType,
      ...(proxyType === 'socks5' ? { username: '', password: '' } : {}),
    }))
    setClearCredentials(Boolean(route?.has_password && proxyType === 'socks5'))
    setErrors((current) => ({ ...current, credentials: undefined }))
    setSubmitError('')
  }

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    const nextErrors = clearCredentials
      ? validateProxyRouteDraft({ ...draft, username: '', password: '', hasSavedPassword: false, savedUsername: '' })
      : validateProxyRouteDraft(draft)
    setErrors(nextErrors)
    setSubmitError('')
    if (hasProxyRouteErrors(nextErrors)) return

    const body: LoginRouteWrite = {
      name: draft.name.trim(),
      mode: 'custom_proxy',
      proxy_type: draft.proxyType,
      host: draft.host.trim(),
      port: Number(draft.port),
      enabled: route?.enabled ?? true,
      ...(!route || draft.password ? { username: draft.username.trim() || null } : {}),
      ...(draft.password ? { password: draft.password } : {}),
    }
    try {
      if (route) {
        const patch: LoginRoutePatch = {
          name: body.name,
          proxy_type: body.proxy_type,
          host: body.host,
          port: body.port,
          enabled: body.enabled,
          ...(clearCredentials ? { clear_credentials: true } : {}),
          ...(!clearCredentials && draft.password ? { username: draft.username.trim(), password: draft.password } : {}),
        }
        await updateRoute.mutateAsync({ id: route.id, ...patch })
      } else {
        await createRoute.mutateAsync(body)
      }
      onSaved(route ? `Маршрут «${body.name}» обновлён. Выполните тест перед использованием.` : `Маршрут «${body.name}» добавлен. Выполните тест и назначьте его основным.`)
    } catch (cause) {
      setSubmitError(apiMessage(cause, 'Не удалось сохранить прокси'))
    }
  }

  const pending = createRoute.isPending || updateRoute.isPending
  return (
    <ModalOverlay onClose={onClose} canClose={!pending}>
      <form className="modal modal--wide proxy-route-dialog" role="dialog" aria-modal="true" aria-labelledby="proxy-route-title" onSubmit={submit}>
        <div className="modal__header"><div><span className="eyebrow">Маршрут входа</span><h2 id="proxy-route-title">{route ? 'Изменить прокси' : 'Добавить прокси'}</h2><p>Поддерживаются HTTP, HTTPS CONNECT и SOCKS5, включая резидентские прокси.</p></div><button className="icon-button" type="button" onClick={onClose} disabled={pending} aria-label="Закрыть"><Icon name="close" /></button></div>
        <div className="form-stack">
          {submitError && <div className="form-alert form-alert--error" role="alert"><Icon name="warning" /><span>{submitError}</span></div>}
          <div className="form-alert form-alert--info"><Icon name="shield" /><span>Пароль шифруется на сервере и никогда не возвращается в браузер. Пустое поле при редактировании сохраняет текущий пароль.</span></div>
          <div className="form-grid">
            <label className="field"><span className="field__label">Название</span><input data-autofocus value={draft.name} onChange={(event) => change('name', event.target.value)} placeholder="Например, Residential NL" maxLength={80} aria-invalid={Boolean(errors.name)} />{errors.name ? <span className="field__error">{errors.name}</span> : <span className="field__hint">Понятное имя для списка и аккаунтов.</span>}</label>
            <label className="field"><span className="field__label">Тип</span><select value={draft.proxyType} onChange={(event) => changeProxyType(event.target.value as LoginProxyType)}><option value="socks5">SOCKS5 · без авторизации</option><option value="https">HTTPS CONNECT</option><option value="http">HTTP</option></select><span className="field__hint">Для логина и пароля выберите HTTP/HTTPS CONNECT.</span></label>
          </div>
          <div className="proxy-address-grid">
            <label className="field"><span className="field__label">Хост или IP</span><input value={draft.host} onChange={(event) => change('host', event.target.value)} placeholder="proxy.example.com" autoCapitalize="none" spellCheck={false} aria-invalid={Boolean(errors.host)} />{errors.host ? <span className="field__error">{errors.host}</span> : <span className="field__hint">Без {proxyTypeLabel(draft.proxyType).toLowerCase()}:// и пути.</span>}</label>
            <label className="field"><span className="field__label">Порт</span><input type="number" min="1" max="65535" value={draft.port} onChange={(event) => change('port', event.target.value)} aria-invalid={Boolean(errors.port)} />{errors.port && <span className="field__error">{errors.port}</span>}</label>
          </div>
          <div className="form-grid">
            <label className="field"><span className="field__label">Логин <small>необязательно</small></span><input value={draft.username} onChange={(event) => change('username', event.target.value)} autoComplete="off" placeholder="Без авторизации" disabled={clearCredentials || draft.proxyType === 'socks5'} /></label>
            <label className="field"><span className="field__label">Пароль <small>только запись</small></span><input type="password" value={draft.password} onChange={(event) => change('password', event.target.value)} autoComplete="new-password" placeholder={route?.has_password ? 'Сохранён · оставьте пустым' : 'Без авторизации'} disabled={clearCredentials || draft.proxyType === 'socks5'} /></label>
          </div>
          {route?.has_password && <label className="credential-clear"><input type="checkbox" checked={clearCredentials} onChange={(event) => { setClearCredentials(event.target.checked); setErrors((current) => ({ ...current, credentials: undefined })) }} /><span>Очистить сохранённые логин и пароль</span></label>}
          {errors.credentials && <div className="field__error proxy-credentials-error">{errors.credentials}</div>}
        </div>
        <div className="modal__actions"><button className="button button--secondary" type="button" onClick={onClose} disabled={pending}>Отмена</button><button className="button button--primary" type="submit" disabled={pending}>{pending ? <><span className="spinner spinner--light" />Сохраняем…</> : <><Icon name="check" />Сохранить</>}</button></div>
      </form>
    </ModalOverlay>
  )
}

function HomeRelayDialog({ onClose, onReadyToTest }: { onClose: () => void; onReadyToTest: () => Promise<void> }) {
  const createSetup = useCreateHomeRelaySetup()
  const [name, setName] = useState('Домашний ПК')
  const [autostart, setAutostart] = useState(true)
  const [setup, setSetup] = useState<Omit<HomeRelaySetup, 'setup_token'> | null>(null)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)

  const generate = async () => {
    setError('')
    if (name.trim().length < 2 || name.trim().length > 80) {
      setError('Название должно содержать от 2 до 80 символов.')
      return
    }
    try {
      const response = await createSetup.mutateAsync({ name: name.trim(), autostart })
      setSetup({
        expires_at: response.expires_at,
        powershell_command: response.powershell_command,
        script_download_url: response.script_download_url,
        installer_sha256: response.installer_sha256,
      })
    } catch (cause) {
      setError(apiMessage(cause, 'Не удалось подготовить установку'))
    }
  }

  const copy = async () => {
    if (!setup) return
    try {
      await navigator.clipboard.writeText(setup.powershell_command)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2_000)
    } catch {
      setError('Не удалось скопировать команду. Выделите её вручную.')
    }
  }

  return (
    <ModalOverlay onClose={onClose} canClose={!createSetup.isPending}>
      <div className="modal modal--wide home-relay-dialog" role="dialog" aria-modal="true" aria-labelledby="home-relay-title">
        <div className="modal__header"><div><span className="eyebrow">Домашний прокси</span><h2 id="home-relay-title">{setup ? 'Запустите установку на ПК' : 'Настроить домашний шлюз'}</h2><p>Лёгкий SSH-туннель направит входы OpenAI и почты через домашний интернет.</p></div><button className="icon-button" type="button" onClick={onClose} disabled={createSetup.isPending} aria-label="Закрыть"><Icon name="close" /></button></div>

        {!setup ? (
          <div className="form-stack">
            {error && <div className="form-alert form-alert--error" role="alert"><Icon name="warning" /><span>{error}</span></div>}
            <div className="home-relay-benefits">
              <div><Icon name="home" /><span><strong>Выход через ваш Wi-Fi</strong><small>Браузер работает на сервере; на ПК браузер не запускается.</small></span></div>
              <div><Icon name="activity" /><span><strong>Минимальная нагрузка</strong><small>Один фоновый ssh.exe и трафик только во время входа.</small></span></div>
              <div><Icon name="shield" /><span><strong>Закрытый доступ</strong><small>SOCKS-порт не публикуется наружу; его видит только backend в изолированной Docker-сети.</small></span></div>
            </div>
            <div className="form-alert form-alert--warning"><Icon name="warning" /><span>Для автоматической проверки ПК должен быть включён и не находиться в спящем режиме. При недоступности шлюза вход остановится без перехода на IP сервера.</span></div>
            <label className="field"><span className="field__label">Название этого ПК</span><input data-autofocus value={name} onChange={(event) => { setName(event.target.value); setError('') }} maxLength={80} placeholder="Домашний ПК" /><span className="field__hint">Будет видно только администратору.</span></label>
            <label className="switch-row home-relay-autostart"><span><strong>Запускать при включении Windows</strong><small>{autostart ? 'Команду нужно выполнить в PowerShell от имени администратора; она создаст защищённую системную задачу. Управляющие скрипты останутся в ProgramData.' : 'Для новой установки — ручной запуск без UAC через ярлык. Если boot-режим уже установлен, он останется защищённым в ProgramData и попросит права администратора.'}</small></span><input type="checkbox" checked={autostart} onChange={(event) => setAutostart(event.target.checked)} /><span className="switch" /></label>
          </div>
        ) : (
          <div className="form-stack">
            {error && <div className="form-alert form-alert--error" role="alert"><Icon name="warning" /><span>{error}</span></div>}
            <ol className="home-relay-steps"><li><span>1</span><div><strong>Откройте PowerShell{autostart ? ' от имени администратора' : ''}</strong><small>{autostart ? 'Права нужны для автозапуска при включении Windows.' : 'Начните в обычном окне. Если уже установлен boot-режим или нет OpenSSH, команда попросит безопасно повторить её от администратора.'}</small></div></li><li><span>2</span><div><strong>Скопируйте и выполните команду</strong><small>Одноразовая привязка действует до {formatDateTime(setup.expires_at)}.</small></div></li><li><span>3</span><div><strong>Проверьте маршрут</strong><small>Оставьте ПК включённым; после статуса «Онлайн» маршрут готов к входам OpenAI и почты.</small></div></li></ol>
            <div className="home-relay-command"><code>{setup.powershell_command}</code><button className="button button--secondary" type="button" onClick={copy}><Icon name={copied ? 'check' : 'copy'} />{copied ? 'Скопировано' : 'Копировать'}</button></div>
            <div className="home-relay-download"><div><Icon name="download" /><span><strong>Установочный пакет</strong><small>Для ручной установки; одноразовые параметры находятся только в команде выше.</small></span></div><a className="button button--secondary" href={setup.script_download_url} download><Icon name="download" />Скачать .zip</a></div>
            <div className="form-alert form-alert--info"><Icon name="shield" /><span>Приватный SSH-ключ создаётся локально и не покидает ПК. Команда содержит одноразовый код и может остаться в истории PowerShell: не пересылайте её; после привязки или окончания срока код больше не действует.</span></div>
          </div>
        )}

        <div className="modal__actions"><button className="button button--secondary" type="button" onClick={onClose} disabled={createSetup.isPending}>{setup ? 'Закрыть' : 'Отмена'}</button>{setup ? <button className="button button--primary" type="button" onClick={() => void onReadyToTest()}><Icon name="refresh" />Проверить маршрут</button> : <button className="button button--primary" type="button" onClick={generate} disabled={createSetup.isPending}>{createSetup.isPending ? <><span className="spinner spinner--light" />Готовим…</> : <><Icon name="arrow-right" />Получить установку</>}</button>}</div>
      </div>
    </ModalOverlay>
  )
}

function apiMessage(cause: unknown, fallback: string) {
  if (!(cause instanceof ApiError)) return fallback
  const normalized = cause.message.toLowerCase()
  if (normalized.includes('proxy route name already exists')) return 'Маршрут с таким названием уже существует.'
  if (normalized.includes('disabled route cannot be default')) return 'Сначала включите и успешно проверьте маршрут.'
  if (normalized.includes('choose direct or another default route')) return 'Сначала выберите Direct или другой основной маршрут.'
  if (normalized.includes('reassign accounts before deleting')) return 'Сначала переназначьте аккаунты, использующие этот маршрут.'
  if (normalized.includes('unassign this route from the default')) return 'Сначала уберите маршрут из настроек по умолчанию и у всех привязанных аккаунтов.'
  if (normalized.includes('changed while it was being tested')) return 'Маршрут изменился во время проверки. Запустите проверку ещё раз.'
  if (normalized.includes('home relay key could not be revoked')) return 'Не удалось безопасно остановить и отозвать домашний туннель. Повторите попытку.'
  if (normalized.includes('home relay is unavailable')) return 'Сервер домашнего туннеля пока недоступен. Проверьте sidecar и повторите установку.'
  if (normalized.includes('home relay setup conflict')) return 'Настройка уже выполняется. Закройте мастер и повторите попытку.'
  return cause.message
}
