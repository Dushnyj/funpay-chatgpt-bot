import { useState } from 'react'
import type { FormEvent } from 'react'
import { useLogin } from '../api/auth'
import { ApiError } from '../api/client'
import { Icon } from '../components/Icon'

export default function Login() {
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const login = useLogin()

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault()
    setError('')
    try {
      await login.mutateAsync(password)
      window.location.href = '/'
    } catch (cause) {
      if (cause instanceof ApiError && cause.status >= 500) {
        setError('Сервер не готов к авторизации. Проверьте первоначальную настройку.')
      } else {
        setError('Пароль не подошёл. Проверьте раскладку и попробуйте ещё раз.')
      }
    }
  }

  return (
    <main className="login-page">
      <section className="login-visual" aria-label="Информация о системе">
        <div className="login-visual__glow" />
        <div className="login-brand">
          <div className="brand__mark brand__mark--large"><span>F</span></div>
          <div>
            <strong>FunPay Rental</strong>
            <span>Operations console</span>
          </div>
        </div>
        <div className="login-visual__content">
          <div className="eyebrow eyebrow--light">Единый центр управления</div>
          <h1>Контролируйте аренды, аккаунты и продажи в одном месте.</h1>
          <p>Статусы интеграций, лимиты аккаунтов и операционные события — без переключения между сервисами.</p>
        </div>
        <div className="login-features">
          <div><Icon name="activity" /><span>Мониторинг операций</span></div>
          <div><Icon name="shield" /><span>Защищённая сессия</span></div>
          <div><Icon name="database" /><span>Единый пул данных</span></div>
        </div>
      </section>

      <section className="login-panel">
        <form onSubmit={handleSubmit} className="login-form">
          <div className="login-form__header">
            <div className="mobile-login-brand">
              <div className="brand__mark"><span>F</span></div>
              <strong>FunPay Rental</strong>
            </div>
            <span className="eyebrow">Безопасный вход</span>
            <h2>Добро пожаловать</h2>
            <p>Введите пароль администратора, заданный при настройке сервера.</p>
          </div>

          {error && (
            <div className="form-alert form-alert--error" role="alert">
              <Icon name="warning" />
              <span>{error}</span>
            </div>
          )}

          <label className="field">
            <span className="field__label">Пароль администратора</span>
            <span className="input-with-action">
              <Icon name="key" className="input-leading-icon" />
              <input
                type={showPassword ? 'text' : 'password'}
                placeholder="Введите пароль"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="current-password"
                autoFocus
                required
              />
              <button type="button" onClick={() => setShowPassword((value) => !value)} aria-label={showPassword ? 'Скрыть пароль' : 'Показать пароль'}>
                <Icon name="eye" />
              </button>
            </span>
          </label>

          <button className="button button--primary button--large" type="submit" disabled={login.isPending || !password}>
            {login.isPending ? <><span className="spinner spinner--light" />Проверяем…</> : <>Войти в панель<Icon name="arrow-right" /></>}
          </button>

          <div className="login-security-note">
            <Icon name="shield" />
            <span>Пароль передаётся по защищённому HTTPS-соединению и не сохраняется в браузере.</span>
          </div>
        </form>
      </section>
    </main>
  )
}
