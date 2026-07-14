import { Component, lazy, Suspense, type ErrorInfo, type ReactNode } from 'react'
import { Routes, Route } from 'react-router-dom'
import Layout from './components/Layout'
import ProtectedRoute from './components/ProtectedRoute'
import { LoadingState } from './components/ui'

const CHUNK_RELOAD_PREFIX = 'funpay:chunk-reload:'

function lazyRoute<T extends { default: React.ComponentType<unknown> }>(
  key: string,
  importer: () => Promise<T>,
) {
  return lazy(async () => {
    const reloadKey = `${CHUNK_RELOAD_PREFIX}${key}`
    try {
      const module = await importer()
      sessionStorage.removeItem(reloadKey)
      return module
    } catch (error) {
      // An open admin tab can still reference the previous image's hashed
      // chunks immediately after a deploy. Refresh once to load the new
      // index.html; a persistent failure is handled by the boundary below.
      if (sessionStorage.getItem(reloadKey) !== '1') {
        sessionStorage.setItem(reloadKey, '1')
        window.location.reload()
        return new Promise<T>(() => undefined)
      }
      throw error
    }
  })
}

const Login = lazyRoute('login', () => import('./pages/Login'))
const Dashboard = lazyRoute('dashboard', () => import('./pages/Dashboard'))
const Chats = lazyRoute('chats', () => import('./pages/Chats'))
const Accounts = lazyRoute('accounts', () => import('./pages/Accounts'))
const Tiers = lazyRoute('catalog', () => import('./pages/Tiers'))
const Lots = lazyRoute('lots', () => import('./pages/Lots'))
const Orders = lazyRoute('orders', () => import('./pages/Orders'))
const Prices = lazyRoute('prices', () => import('./pages/Prices'))
const Templates = lazyRoute('templates', () => import('./pages/Templates'))
const Settings = lazyRoute('settings', () => import('./pages/Settings'))
const NotFound = lazyRoute('not-found', () => import('./pages/NotFound'))

class RouteErrorBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Failed to load admin route', error, info)
  }

  private retry = () => {
    for (let index = sessionStorage.length - 1; index >= 0; index -= 1) {
      const key = sessionStorage.key(index)
      if (key?.startsWith(CHUNK_RELOAD_PREFIX)) sessionStorage.removeItem(key)
    }
    window.location.reload()
  }

  render() {
    if (this.state.failed) {
      return (
        <main className="auth-loading auth-loading--error" role="alert">
          <h1>Обновление завершено</h1>
          <p>Загрузите актуальную версию панели.</p>
          <button className="button button--primary" type="button" onClick={this.retry}>
            Обновить панель
          </button>
        </main>
      )
    }
    return this.props.children
  }
}

export default function App() {
  return (
    <RouteErrorBoundary>
      <Suspense fallback={<LoadingState label="Открываем раздел" />}>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route element={<ProtectedRoute><Layout /></ProtectedRoute>}>
            <Route index element={<Dashboard />} />
            <Route path="accounts" element={<Accounts />} />
            <Route path="chats" element={<Chats />} />
            <Route path="catalog" element={<Tiers />} />
            <Route path="lots" element={<Lots />} />
            <Route path="orders" element={<Orders />} />
            <Route path="prices" element={<Prices />} />
            <Route path="templates" element={<Templates />} />
            <Route path="settings" element={<Settings />} />
            <Route path="*" element={<NotFound />} />
          </Route>
        </Routes>
      </Suspense>
    </RouteErrorBoundary>
  )
}
