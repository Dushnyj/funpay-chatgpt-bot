import { useState } from 'react'
import type { FormEvent } from 'react'
import { useLogin } from '../api/auth'

export default function Login() {
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const login = useLogin()

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setError('')
    try {
      await login.mutateAsync(password)
      window.location.href = '/'
    } catch {
      setError('Неверный пароль')
    }
  }

  return (
    <div className="login-page">
      <form onSubmit={handleSubmit} className="login-form">
        <h1>Вход в админ-панель</h1>
        {error && <div className="error">{error}</div>}
        <input
          type="password"
          placeholder="Пароль"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoFocus
        />
        <button type="submit" disabled={login.isPending}>
          {login.isPending ? 'Вход...' : 'Войти'}
        </button>
      </form>
    </div>
  )
}
