# Фаза 6: Frontend SPA — План реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** React + TypeScript + Vite SPA — админ-панель продавца. Login, Dashboard, Accounts, Lots, Orders/Rentals, Settings. Общается с FastAPI (Фаза 5) через REST API. JWT в httpOnly cookie (браузер управляет автоматически).

**Architecture:** Vite SPA, React 18, TypeScript strict. Без UI-фреймворка (свои компоненты + CSS modules). TanStack Query (React Query) для server state. React Router для навигации. API-клиент — тонкая обёртка над fetch с обработкой 401 → redirect на /login.

**Tech Stack:** React 18, TypeScript 5, Vite 5, TanStack Query 5, React Router 6, Vitest для unit-тестов.

---

## Структура файлов

```
frontend/
├── package.json
├── tsconfig.json
├── vite.config.ts
├── index.html
├── src/
│   ├── main.tsx                 # точка входа, QueryClient, Router
│   ├── App.tsx                  # layout + routes
│   ├── api/
│   │   ├── client.ts            # fetch-обёртка, 401 handling
│   │   ├── accounts.ts          # useAccounts, useCreateAccount...
│   │   ├── catalog.ts           # useTiers, useDurations...
│   │   ├── lots.ts              # useLots, useCreateLot...
│   │   ├── orders.ts            # useOrders
│   │   ├── rentals.ts           # useRentals
│   │   ├── settings.ts          # useSettings, useUpdateSettings
│   │   ├── templates.ts         # useTemplates, useUpdateTemplates
│   │   ├── prices.ts            # usePrices, useUpdatePrices
│   │   └── metrics.ts           # useMetrics
│   ├── components/
│   │   ├── Layout.tsx           # sidebar + content area
│   │   ├── ProtectedRoute.tsx   # проверка auth
│   │   ├── DataTable.tsx        # переиспользуемая таблица
│   │   └── Modal.tsx            # переиспользуемое модальное окно
│   ├── pages/
│   │   ├── Login.tsx
│   │   ├── Dashboard.tsx
│   │   ├── Accounts.tsx
│   │   ├── Tiers.tsx            # catalog: tiers + durations + scopes (3 tab)
│   │   ├── Lots.tsx
│   │   ├── Orders.tsx           # orders + rentals (2 tab)
│   │   ├── Prices.tsx
│   │   ├── Templates.tsx        # message templates editor
│   │   └── Settings.tsx
│   ├── types/
│   │   └── api.ts               # TypeScript interfaces (зеркало backend schemas)
│   └── styles/
│       └── global.css
└── tests/                       # Vitest (опционально, минимальные smoke-тесты)
```

---

## Task 1: Инициализация проекта Vite + зависимости

**Files:**
- Create: `frontend/package.json`, `frontend/tsconfig.json`, `frontend/vite.config.ts`, `frontend/index.html`, `frontend/src/main.tsx`, `frontend/src/App.tsx`, `frontend/src/styles/global.css`

- [ ] **Step 1: Создать Vite проект**

```bash
cd /c/Source/funpay
npm create vite@latest frontend -- --template react-ts
cd frontend
npm install
```

- [ ] **Step 2: Установить зависимости**

```bash
cd /c/Source/funpay/frontend
npm install @tanstack/react-query @tanstack/react-query-devtools react-router-dom
npm install -D vitest @testing-library/react @testing-library/jest-dom jsdom
```

- [ ] **Step 3: Настроить vite.config.ts (proxy на backend + test)**

`frontend/vite.config.ts`:

```typescript
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: './tests/setup.ts',
  },
})
```

- [ ] **Step 4: Настроить tsconfig.json (strict)**

Прочитать созданный `frontend/tsconfig.json` и убедиться что `strict: true`. Добавить alias `@/` → `src/`:

```json
{
  "compilerOptions": {
    "target": "ES2020",
    "strict": true,
    "baseUrl": ".",
    "paths": { "@/*": ["src/*"] }
  }
}
```

- [ ] **Step 5: Минимальный main.tsx**

`frontend/src/main.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter } from 'react-router-dom'
import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './styles/global.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false },
  },
})

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
)
```

- [ ] **Step 6: Минимальный App.tsx (health-check)**

`frontend/src/App.tsx`:

```tsx
export default function App() {
  return (
    <div>
      <h1>FunPay ChatGPT Rental Bot</h1>
      <p>Admin panel (Phase 6)</p>
    </div>
  )
}
```

- [ ] **Step 7: Проверить сборку**

```bash
cd /c/Source/funpay/frontend
npm run build
```
Expected: успешная сборка без ошибок

- [ ] **Step 8: Commit**

```bash
cd /c/Source/funpay
git add frontend/
git commit -m "feat: initialize Vite React TS project with dependencies"
```

---

## Task 2: TypeScript API types + API client

Зеркало backend Pydantic schemas в TypeScript interfaces + fetch-обёртка с 401 handling.

**Files:**
- Create: `frontend/src/types/api.ts`
- Create: `frontend/src/api/client.ts`

- [ ] **Step 1: Создать types/api.ts**

`frontend/src/types/api.ts`:

```typescript
// Зеркало backend Pydantic schemas (app/api/schemas.py)

export interface StatusResponse {
  status: string
}

// --- Catalog ---

export interface Tier {
  id: number
  name: string
  description: string | null
  is_active: boolean
}

export interface TierCreate {
  name: string
  description?: string
  is_active?: boolean
}

export interface Duration {
  id: number
  days: number
  is_enabled: boolean
  sort_order: number
}

export interface LimitScope {
  id: number
  code: string
  name: string
}

// --- Accounts ---

export interface AccountLimits {
  account_id: number
  chat_5h_remaining_pct: number | null
  chat_weekly_remaining_pct: number | null
  codex_5h_remaining_pct: number | null
  codex_weekly_remaining_pct: number | null
  refresh_status: string
  measured_at: string | null
}

export interface Account {
  id: number
  login: string
  tier_id: number
  subscription_expires_at: string | null
  max_active_rentals: number | null
  status: string
  notes: string | null
}

export interface AccountWithLimits extends Account {
  limits: AccountLimits | null
}

export interface AccountCreate {
  login: string
  password: string
  totp_secret: string
  tier_id: number
  subscription_expires_at?: string
  max_active_rentals?: number
  notes?: string
}

export interface BulkAccountItem {
  login: string
  password: string
  totp_secret: string
}

export interface BulkAccountRequest {
  tier_id: number
  accounts: BulkAccountItem[]
}

// --- Price Matrix ---

export interface PriceMatrixItem {
  tier_id: number
  duration_id: number
  limit_scope_id: number
  min_limit_pct?: number
  max_5h_pct?: number
  max_weekly_pct?: number
  price: number
}

// --- Templates ---

export interface MessageTemplate {
  key: string
  lang: string
  content: string
}

// --- Lots ---

export interface Lot {
  id: number
  funpay_id: string | null
  funpay_node_id: number | null
  tier_id: number
  duration_id: number
  limit_scope_id: number
  min_limit_pct: number | null
  max_5h_pct: number | null
  max_weekly_pct: number | null
  price: number
  title_ru: string
  title_en: string
  status: string
  auto_created: boolean
}

export interface LotCreate {
  funpay_node_id?: number
  tier_id: number
  duration_id: number
  limit_scope_id: number
  min_limit_pct?: number
  max_5h_pct?: number
  max_weekly_pct?: number
  price: number
  title_ru: string
  title_en: string
  description_ru?: string
  description_en?: string
}

// --- Orders / Rentals ---

export interface Order {
  id: number
  funpay_order_id: string
  funpay_chat_id: string
  buyer_funpay_id: string
  buyer_locale: string
  lot_id: number | null
  tier_id: number | null
  duration_id: number | null
  limit_scope_id: number | null
  price: number
  status: string
  created_at: string
}

export interface Rental {
  id: number
  order_id: number
  account_id: number
  buyer_funpay_id: string
  buyer_funpay_chat_id: string
  tier_id: number
  duration_id: number
  limit_scope_id: number
  lang: string
  started_at: string
  expires_at: string
  status: string
  replacement_count: number
}

// --- Settings ---

export interface Settings {
  funpay_node_id: number | null
  auto_bump_enabled: boolean
  bump_interval_hours: number
  default_max_active_rentals: number
  funpay_commission_percent: number
  check_interval_minutes: number
  limits_check_interval_minutes: number
  limits_warn_threshold_pct: number
}

// --- Metrics ---

export interface Metrics {
  active_rentals: number
  available_accounts: number
  orders_today: number
  revenue_brutto: number
  revenue_netto: number
  bot_status: string
}
```

- [ ] **Step 2: Создать api/client.ts**

`frontend/src/api/client.ts`:

```typescript
const BASE = '/api'

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (resp.status === 401) {
    window.location.href = '/login'
    throw new ApiError(401, 'Unauthorized')
  }
  if (!resp.ok) {
    const text = await resp.text().catch(() => resp.statusText)
    throw new ApiError(resp.status, text)
  }
  if (resp.status === 204) return undefined as T
  return resp.json()
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined }),
  patch: <T>(path: string, body: unknown) =>
    request<T>(path, { method: 'PATCH', body: JSON.stringify(body) }),
  put: <T>(path: string, body: unknown) =>
    request<T>(path, { method: 'PUT', body: JSON.stringify(body) }),
  delete: (path: string) => request<void>(path, { method: 'DELETE' }),
}
```

- [ ] **Step 3: Проверить типы**

```bash
cd /c/Source/funpay/frontend
npx tsc --noEmit
```
Expected: без ошибок

- [ ] **Step 4: Commit**

```bash
cd /c/Source/funpay
git add frontend/src/types/ frontend/src/api/
git commit -m "feat: add TypeScript API types and fetch client"
```

---

## Task 3: Auth + Layout + Routing

Login page, защищённые роуты, sidebar layout.

**Files:**
- Create: `frontend/src/pages/Login.tsx`
- Create: `frontend/src/components/Layout.tsx`
- Create: `frontend/src/components/ProtectedRoute.tsx`
- Create: `frontend/src/api/auth.ts`
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: Создать auth hooks**

`frontend/src/api/auth.ts`:

```typescript
import { useMutation } from '@tanstack/react-query'
import { api } from './client'

export function useLogin() {
  return useMutation({
    mutationFn: (password: string) =>
      api.post('/auth/login', { password }),
  })
}

export function useLogout() {
  return useMutation({
    mutationFn: () => api.post('/auth/logout'),
  })
}
```

- [ ] **Step 2: Создать Login page**

`frontend/src/pages/Login.tsx`:

```tsx
import { useState } from 'react'
import { useLogin } from '../api/auth'

export default function Login() {
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const login = useLogin()

  const handleSubmit = async (e: React.FormEvent) => {
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
```

- [ ] **Step 3: Создать Layout**

`frontend/src/components/Layout.tsx`:

```tsx
import { NavLink, Outlet } from 'react-router-dom'
import { useLogout } from '../api/auth'

const NAV = [
  { to: '/', label: 'Дашборд' },
  { to: '/accounts', label: 'Аккаунты' },
  { to: '/catalog', label: 'Справочники' },
  { to: '/lots', label: 'Лоты' },
  { to: '/orders', label: 'Заказы' },
  { to: '/prices', label: 'Цены' },
  { to: '/templates', label: 'Шаблоны' },
  { to: '/settings', label: 'Настройки' },
]

export default function Layout() {
  const logout = useLogout()
  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="logo">ChatGPT Rental</div>
        <nav>
          {NAV.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.to === '/'}>
              {item.label}
            </NavLink>
          ))}
        </nav>
        <button onClick={() => logout.mutateAsync().then(() => window.location.href = '/login')}>
          Выйти
        </button>
      </aside>
      <main className="content">
        <Outlet />
      </main>
    </div>
  )
}
```

- [ ] **Step 4: Создать ProtectedRoute**

`frontend/src/components/ProtectedRoute.tsx`:

```tsx
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'

export default function ProtectedRoute({ children }: { children: React.ReactNode }) {
  // Проверка auth: пробуем получить metrics (401 → редирект)
  const { isError } = useQuery({
    queryKey: ['auth-check'],
    queryFn: () => api.get('/metrics'),
    retry: false,
  })

  if (isError) {
    window.location.href = '/login'
    return null
  }

  return <>{children}</>
}
```

- [ ] **Step 5: Настроить App.tsx с роутингом**

`frontend/src/App.tsx`:

```tsx
import { Routes, Route } from 'react-router-dom'
import Layout from './components/Layout'
import ProtectedRoute from './components/ProtectedRoute'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import Accounts from './pages/Accounts'
import Tiers from './pages/Tiers'
import Lots from './pages/Lots'
import Orders from './pages/Orders'
import Prices from './pages/Prices'
import Templates from './pages/Templates'
import Settings from './pages/Settings'

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<ProtectedRoute><Layout /></ProtectedRoute>}>
        <Route index element={<Dashboard />} />
        <Route path="accounts" element={<Accounts />} />
        <Route path="catalog" element={<Tiers />} />
        <Route path="lots" element={<Lots />} />
        <Route path="orders" element={<Orders />} />
        <Route path="prices" element={<Prices />} />
        <Route path="templates" element={<Templates />} />
        <Route path="settings" element={<Settings />} />
      </Route>
    </Routes>
  )
}
```

- [ ] **Step 6: Создать заглушки страниц**

Создай минимальные заглушки для всех страниц (Dashboard, Accounts, Tiers, Lots, Orders, Prices, Templates, Settings). Каждая — просто `<h1>Название</h1>`.

Например `frontend/src/pages/Dashboard.tsx`:

```tsx
export default function Dashboard() {
  return <h1>Дашборд</h1>
}
```

Создай аналогичные для Accounts, Tiers, Lots, Orders, Prices, Templates, Settings.

- [ ] **Step 7: Проверить сборку**

```bash
cd /c/Source/funpay/frontend
npm run build
```
Expected: успешная сборка

- [ ] **Step 8: Commit**

```bash
cd /c/Source/funpay
git add frontend/src/
git commit -m "feat: add login page, layout, routing with protected routes"
```

---

## Task 4: Dashboard с метриками

**Files:**
- Create: `frontend/src/api/metrics.ts`
- Modify: `frontend/src/pages/Dashboard.tsx`

- [ ] **Step 1: Создать metrics hook**

`frontend/src/api/metrics.ts`:

```typescript
import { useQuery } from '@tanstack/react-query'
import { api } from './client'
import type { Metrics } from '../types/api'

export function useMetrics() {
  return useQuery({
    queryKey: ['metrics'],
    queryFn: () => api.get<Metrics>('/metrics'),
    refetchInterval: 30000,
  })
}
```

- [ ] **Step 2: Реализовать Dashboard**

`frontend/src/pages/Dashboard.tsx`:

```tsx
import { useMetrics } from '../api/metrics'

const METRIC_CARDS = [
  { key: 'active_rentals', label: 'Активных аренд' },
  { key: 'available_accounts', label: 'Свободных аккаунтов' },
  { key: 'orders_today', label: 'Заказов сегодня' },
  { key: 'revenue_brutto', label: 'Выручка brutto (₽)' },
  { key: 'revenue_netto', label: 'Выручка netto (₽)' },
] as const

export default function Dashboard() {
  const { data: metrics, isLoading } = useMetrics()

  if (isLoading) return <div>Загрузка...</div>

  return (
    <div>
      <h1>Дашборд</h1>
      <div className="metrics-grid">
        {METRIC_CARDS.map((card) => (
          <div key={card.key} className="metric-card">
            <div className="metric-value">
              {metrics ? metrics[card.key] : '—'}
            </div>
            <div className="metric-label">{card.label}</div>
          </div>
        ))}
      </div>
      <div className="bot-status">
        Статус бота:{' '}
        <span className={metrics?.bot_status === 'connected' ? 'status-ok' : 'status-error'}>
          {metrics?.bot_status || 'unknown'}
        </span>
      </div>
    </div>
  )
}
```

- [ ] **Step 3: Проверить сборку**

```bash
cd /c/Source/funpay/frontend
npm run build
```

- [ ] **Step 4: Commit**

```bash
cd /c/Source/funpay
git add frontend/src/
git commit -m "feat: add Dashboard with metrics cards"
```

---

## Task 5: Accounts page — таблица + добавление + bulk

**Files:**
- Create: `frontend/src/api/accounts.ts`
- Modify: `frontend/src/pages/Accounts.tsx`

- [ ] **Step 1: Создать accounts hooks**

`frontend/src/api/accounts.ts`:

```typescript
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type { Account, AccountCreate, BulkAccountRequest } from '../types/api'

export function useAccounts() {
  return useQuery({
    queryKey: ['accounts'],
    queryFn: () => api.get<Account[]>('/accounts'),
  })
}

export function useCreateAccount() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: AccountCreate) => api.post<Account>('/accounts', body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['accounts'] }),
  })
}

export function useBulkAddAccounts() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: BulkAccountRequest) =>
      api.post<{ created: number }>('/accounts/bulk', body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['accounts'] }),
  })
}

export function useDeleteAccount() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => api.delete(`/accounts/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['accounts'] }),
  })
}

export function usePatchAccount() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, ...body }: { id: number } & Partial<Account>) =>
      api.patch<Account>(`/accounts/${id}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['accounts'] }),
  })
}
```

- [ ] **Step 2: Реализовать Accounts page**

`frontend/src/pages/Accounts.tsx`:

```tsx
import { useState } from 'react'
import { useAccounts, useCreateAccount, useDeleteAccount } from '../api/accounts'
import { useTiers } from '../api/catalog'

export default function Accounts() {
  const { data: accounts, isLoading } = useAccounts()
  const { data: tiers } = useTiers()
  const createAccount = useCreateAccount()
  const deleteAccount = useDeleteAccount()
  const [showForm, setShowForm] = useState(false)

  if (isLoading) return <div>Загрузка...</div>

  const tierName = (id: number) => tiers?.find((t) => t.id === id)?.name ?? `#${id}`

  return (
    <div>
      <h1>Аккаунты ({accounts?.length ?? 0})</h1>
      <button onClick={() => setShowForm(!showForm)}>Добавить</button>
      {showForm && <AddAccountForm tiers={tiers ?? []} onSubmit={async (v) => { await createAccount.mutateAsync(v); setShowForm(false) }} />}
      <table className="data-table">
        <thead>
          <tr>
            <th>ID</th><th>Логин</th><th>Tier</th><th>Статус</th><th>Действия</th>
          </tr>
        </thead>
        <tbody>
          {accounts?.map((acc) => (
            <tr key={acc.id}>
              <td>{acc.id}</td>
              <td>{acc.login}</td>
              <td>{tierName(acc.tier_id)}</td>
              <td className={`status-${acc.status}`}>{acc.status}</td>
              <td>
                <button onClick={() => deleteAccount.mutate(acc.id)}>Удалить</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function AddAccountForm({ tiers, onSubmit }: {
  tiers: { id: number; name: string }[]
  onSubmit: (v: { login: string; password: string; totp_secret: string; tier_id: number }) => Promise<void>
}) {
  const [form, setForm] = useState({ login: '', password: '', totp_secret: '', tier_id: tiers[0]?.id ?? 0 })
  return (
    <form onSubmit={(e) => { e.preventDefault(); onSubmit(form) }} className="inline-form">
      <input placeholder="Логин" value={form.login} onChange={(e) => setForm({ ...form, login: e.target.value })} required />
      <input placeholder="Пароль" type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} required />
      <input placeholder="TOTP secret" value={form.totp_secret} onChange={(e) => setForm({ ...form, totp_secret: e.target.value })} required />
      <select value={form.tier_id} onChange={(e) => setForm({ ...form, tier_id: Number(e.target.value) })}>
        {tiers.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
      </select>
      <button type="submit">Создать</button>
    </form>
  )
}
```

- [ ] **Step 3: Проверить сборку**

```bash
cd /c/Source/funpay/frontend
npm run build
```

- [ ] **Step 4: Commit**

```bash
cd /c/Source/funpay
git add frontend/src/
git commit -m "feat: add Accounts page with table, create form"
```

---

## Task 6: Catalog page (tiers/durations/scopes), Lots, Orders/Rentals

**Files:**
- Create: `frontend/src/api/catalog.ts`, `frontend/src/api/lots.ts`, `frontend/src/api/orders.ts`, `frontend/src/api/rentals.ts`
- Modify: `frontend/src/pages/Tiers.tsx`, `frontend/src/pages/Lots.tsx`, `frontend/src/pages/Orders.tsx`

- [ ] **Step 1: Создать catalog hooks**

`frontend/src/api/catalog.ts`:

```typescript
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type { Tier, TierCreate, Duration, LimitScope } from '../types/api'

export function useTiers() {
  return useQuery({ queryKey: ['tiers'], queryFn: () => api.get<Tier[]>('/tiers') })
}

export function useCreateTier() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: TierCreate) => api.post<Tier>('/tiers', body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['tiers'] }),
  })
}

export function useDeleteTier() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => api.delete(`/tiers/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['tiers'] }),
  })
}

export function useDurations() {
  return useQuery({ queryKey: ['durations'], queryFn: () => api.get<Duration[]>('/durations') })
}

export function useLimitScopes() {
  return useQuery({ queryKey: ['limit-scopes'], queryFn: () => api.get<LimitScope[]>('/limit-scopes') })
}
```

- [ ] **Step 2: Создать lots hooks**

`frontend/src/api/lots.ts`:

```typescript
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type { Lot, LotCreate } from '../types/api'

export function useLots() {
  return useQuery({ queryKey: ['lots'], queryFn: () => api.get<Lot[]>('/lots') })
}

export function useCreateLot() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: LotCreate) => api.post<Lot>('/lots', body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['lots'] }),
  })
}

export function useDeleteLot() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => api.delete(`/lots/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['lots'] }),
  })
}
```

- [ ] **Step 3: Создать orders/rentals hooks**

`frontend/src/api/orders.ts`:

```typescript
import { useQuery } from '@tanstack/react-query'
import { api } from './client'
import type { Order } from '../types/api'

export function useOrders() {
  return useQuery({ queryKey: ['orders'], queryFn: () => api.get<Order[]>('/orders') })
}
```

`frontend/src/api/rentals.ts`:

```typescript
import { useQuery } from '@tanstack/react-query'
import { api } from './client'
import type { Rental } from '../types/api'

export function useRentals() {
  return useQuery({ queryKey: ['rentals'], queryFn: () => api.get<Rental[]>('/rentals') })
}
```

- [ ] **Step 4: Реализовать Tiers page (с табами durations/scopes)**

`frontend/src/pages/Tiers.tsx`:

```tsx
import { useState } from 'react'
import { useTiers, useCreateTier, useDeleteTier, useDurations, useLimitScopes } from '../api/catalog'

export default function Tiers() {
  const [tab, setTab] = useState<'tiers' | 'durations' | 'scopes'>('tiers')
  return (
    <div>
      <h1>Справочники</h1>
      <div className="tabs">
        <button className={tab === 'tiers' ? 'active' : ''} onClick={() => setTab('tiers')}>Тарифы</button>
        <button className={tab === 'durations' ? 'active' : ''} onClick={() => setTab('durations')}>Сроки</button>
        <button className={tab === 'scopes' ? 'active' : ''} onClick={() => setTab('scopes')}>Лимиты</button>
      </div>
      {tab === 'tiers' && <TiersTab />}
      {tab === 'durations' && <DurationsTab />}
      {tab === 'scopes' && <ScopesTab />}
    </div>
  )
}

function TiersTab() {
  const { data: tiers } = useTiers()
  const createTier = useCreateTier()
  const deleteTier = useDeleteTier()
  const [name, setName] = useState('')
  return (
    <div>
      <form onSubmit={(e) => { e.preventDefault(); createTier.mutate({ name }); setName('') }} className="inline-form">
        <input placeholder="Название тарифа" value={name} onChange={(e) => setName(e.target.value)} required />
        <button type="submit">Добавить</button>
      </form>
      <table className="data-table">
        <thead><tr><th>ID</th><th>Название</th><th>Активен</th><th></th></tr></thead>
        <tbody>
          {tiers?.map((t) => (
            <tr key={t.id}>
              <td>{t.id}</td><td>{t.name}</td><td>{t.is_active ? '✅' : '❌'}</td>
              <td><button onClick={() => deleteTier.mutate(t.id)}>Удалить</button></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function DurationsTab() {
  const { data: durations } = useDurations()
  return (
    <table className="data-table">
      <thead><tr><th>ID</th><th>Дней</th><th>Включён</th><th>Порядок</th></tr></thead>
      <tbody>
        {durations?.map((d) => (
          <tr key={d.id}><td>{d.id}</td><td>{d.days}</td><td>{d.is_enabled ? '✅' : '❌'}</td><td>{d.sort_order}</td></tr>
        ))}
      </tbody>
    </table>
  )
}

function ScopesTab() {
  const { data: scopes } = useLimitScopes()
  return (
    <table className="data-table">
      <thead><tr><th>ID</th><th>Код</th><th>Название</th></tr></thead>
      <tbody>
        {scopes?.map((s) => (
          <tr key={s.id}><td>{s.id}</td><td>{s.code}</td><td>{s.name}</td></tr>
        ))}
      </tbody>
    </table>
  )
}
```

- [ ] **Step 5: Реализовать Lots page**

`frontend/src/pages/Lots.tsx`:

```tsx
import { useLots, useDeleteLot } from '../api/lots'
import { useTiers } from '../api/catalog'
import { useDurations } from '../api/catalog'
import { useLimitScopes } from '../api/catalog'

export default function Lots() {
  const { data: lots, isLoading } = useLots()
  const { data: tiers } = useTiers()
  const { data: durations } = useDurations()
  const { data: scopes } = useLimitScopes()
  const deleteLot = useDeleteLot()

  if (isLoading) return <div>Загрузка...</div>

  const tierName = (id: number) => tiers?.find((t) => t.id === id)?.name ?? `#${id}`
  const durDays = (id: number) => durations?.find((d) => d.id === id)?.days ?? '?'
  const scopeName = (id: number) => scopes?.find((s) => s.id === id)?.code ?? '?'

  return (
    <div>
      <h1>Лоты ({lots?.length ?? 0})</h1>
      <table className="data-table">
        <thead>
          <tr><th>ID</th><th>Tier</th><th>Дней</th><th>Scope</th><th>Цена</th><th>Статус</th><th>Авто</th><th></th></tr>
        </thead>
        <tbody>
          {lots?.map((lot) => (
            <tr key={lot.id}>
              <td>{lot.id}</td>
              <td>{tierName(lot.tier_id)}</td>
              <td>{durDays(lot.duration_id)}</td>
              <td>{scopeName(lot.limit_scope_id)}</td>
              <td>{lot.price}₽</td>
              <td className={`status-${lot.status}`}>{lot.status}</td>
              <td>{lot.auto_created ? '✅' : '❌'}</td>
              <td><button onClick={() => deleteLot.mutate(lot.id)}>Удалить</button></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
```

- [ ] **Step 6: Реализовать Orders page (с табой Rentals)**

`frontend/src/pages/Orders.tsx`:

```tsx
import { useState } from 'react'
import { useOrders } from '../api/orders'
import { useRentals } from '../api/rentals'

export default function Orders() {
  const [tab, setTab] = useState<'orders' | 'rentals'>('orders')
  return (
    <div>
      <h1>Сделки</h1>
      <div className="tabs">
        <button className={tab === 'orders' ? 'active' : ''} onClick={() => setTab('orders')}>Заказы</button>
        <button className={tab === 'rentals' ? 'active' : ''} onClick={() => setTab('rentals')}>Аренды</button>
      </div>
      {tab === 'orders' && <OrdersTab />}
      {tab === 'rentals' && <RentalsTab />}
    </div>
  )
}

function OrdersTab() {
  const { data: orders, isLoading } = useOrders()
  if (isLoading) return <div>Загрузка...</div>
  return (
    <table className="data-table">
      <thead><tr><th>ID</th><th>FunPay ID</th><th>Покупатель</th><th>Цена</th><th>Статус</th><th>Создан</th></tr></thead>
      <tbody>
        {orders?.map((o) => (
          <tr key={o.id}>
            <td>{o.id}</td><td>{o.funpay_order_id}</td><td>{o.buyer_funpay_id}</td>
            <td>{o.price}₽</td><td className={`status-${o.status}`}>{o.status}</td>
            <td>{new Date(o.created_at).toLocaleString('ru')}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function RentalsTab() {
  const { data: rentals, isLoading } = useRentals()
  if (isLoading) return <div>Загрузка...</div>
  return (
    <table className="data-table">
      <thead><tr><th>ID</th><th>Аккаунт</th><th>Покупатель</th><th>Начало</th><th>Истекает</th><th>Статус</th></tr></thead>
      <tbody>
        {rentals?.map((r) => (
          <tr key={r.id}>
            <td>{r.id}</td><td>#{r.account_id}</td><td>{r.buyer_funpay_id}</td>
            <td>{new Date(r.started_at).toLocaleString('ru')}</td>
            <td>{new Date(r.expires_at).toLocaleString('ru')}</td>
            <td className={`status-${r.status}`}>{r.status}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
```

- [ ] **Step 7: Проверить сборку**

```bash
cd /c/Source/funpay/frontend
npm run build
```

- [ ] **Step 8: Commit**

```bash
cd /c/Source/funpay
git add frontend/src/
git commit -m "feat: add catalog, lots, orders pages"
```

---

## Task 7: Settings, Prices, Templates pages

**Files:**
- Create: `frontend/src/api/settings.ts`, `frontend/src/api/prices.ts`, `frontend/src/api/templates.ts`
- Modify: `frontend/src/pages/Settings.tsx`, `frontend/src/pages/Prices.tsx`, `frontend/src/pages/Templates.tsx`

- [ ] **Step 1: Создать hooks**

`frontend/src/api/settings.ts`:

```typescript
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type { Settings } from '../types/api'

export function useSettings() {
  return useQuery({ queryKey: ['settings'], queryFn: () => api.get<Settings>('/settings') })
}

export function useUpdateSettings() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: Partial<Settings>) => api.put<Settings>('/settings', body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['settings'] }),
  })
}
```

`frontend/src/api/prices.ts`:

```typescript
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type { PriceMatrixItem } from '../types/api'

export function usePrices() {
  return useQuery({ queryKey: ['prices'], queryFn: () => api.get<PriceMatrixItem[]>('/prices') })
}

export function useUpdatePrices() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (items: PriceMatrixItem[]) =>
      api.put<{ updated: number }>('/prices', { items }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['prices'] }),
  })
}
```

`frontend/src/api/templates.ts`:

```typescript
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type { MessageTemplate } from '../types/api'

export function useTemplates() {
  return useQuery({ queryKey: ['templates'], queryFn: () => api.get<MessageTemplate[]>('/templates') })
}

export function useUpdateTemplates() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (items: MessageTemplate[]) =>
      api.put<{ updated: number }>('/templates', { items }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['templates'] }),
  })
}
```

- [ ] **Step 2: Реализовать Settings page**

`frontend/src/pages/Settings.tsx`:

```tsx
import { useState } from 'react'
import { useSettings, useUpdateSettings } from '../api/settings'
import type { Settings as SettingsType } from '../types/api'

export default function Settings() {
  const { data, isLoading } = useSettings()
  const update = useUpdateSettings()
  const [form, setForm] = useState<Partial<SettingsType>>({})

  if (isLoading) return <div>Загрузка...</div>
  if (!data) return <div>Настройки не сконфигурированы</div>

  const current = { ...data, ...form }

  return (
    <div>
      <h1>Настройки</h1>
      <form onSubmit={(e) => { e.preventDefault(); update.mutate(form) }} className="settings-form">
        <label>FunPay Node ID<input type="number" value={current.funpay_node_id ?? ''} onChange={(e) => setForm({ ...form, funpay_node_id: Number(e.target.value) })} /></label>
        <label>Лимит аренд по умолчанию<input type="number" value={current.default_max_active_rentals} onChange={(e) => setForm({ ...form, default_max_active_rentals: Number(e.target.value) })} /></label>
        <label>Комиссия FunPay (%)<input type="number" value={current.funpay_commission_percent} onChange={(e) => setForm({ ...form, funpay_commission_percent: Number(e.target.value) })} /></label>
        <label>Интервал проверки (мин)<input type="number" value={current.check_interval_minutes} onChange={(e) => setForm({ ...form, check_interval_minutes: Number(e.target.value) })} /></label>
        <label>Интервал замеров лимитов (мин)<input type="number" value={current.limits_check_interval_minutes} onChange={(e) => setForm({ ...form, limits_check_interval_minutes: Number(e.target.value) })} /></label>
        <label>Bump интервал (часы)<input type="number" value={current.bump_interval_hours} onChange={(e) => setForm({ ...form, bump_interval_hours: Number(e.target.value) })} /></label>
        <label>Авто-bump<input type="checkbox" checked={current.auto_bump_enabled} onChange={(e) => setForm({ ...form, auto_bump_enabled: e.target.checked })} /></label>
        <label>Порог уведомления о лимитах (%)<input type="number" value={current.limits_warn_threshold_pct} onChange={(e) => setForm({ ...form, limits_warn_threshold_pct: Number(e.target.value) })} /></label>
        <button type="submit">Сохранить</button>
      </form>
    </div>
  )
}
```

- [ ] **Step 3: Реализовать Prices page (упрощённо — список + редактирование цены)**

`frontend/src/pages/Prices.tsx`:

```tsx
import { usePrices, useUpdatePrices } from '../api/prices'
import { useTiers, useDurations, useLimitScopes } from '../api/catalog'

export default function Prices() {
  const { data: prices } = usePrices()
  const { data: tiers } = useTiers()
  const { data: durations } = useDurations()
  const { data: scopes } = useLimitScopes()
  const update = useUpdatePrices()

  const tierName = (id: number) => tiers?.find((t) => t.id === id)?.name ?? `#${id}`
  const durDays = (id: number) => durations?.find((d) => d.id === id)?.days ?? '?'
  const scopeName = (id: number) => scopes?.find((s) => s.id === id)?.code ?? '?'

  return (
    <div>
      <h1>Цены</h1>
      <table className="data-table">
        <thead><tr><th>Tier</th><th>Дней</th><th>Scope</th><th>min%</th><th>max5h%</th><th>maxW%</th><th>Цена</th></tr></thead>
        <tbody>
          {prices?.map((p, i) => (
            <tr key={i}>
              <td>{tierName(p.tier_id)}</td>
              <td>{durDays(p.duration_id)}</td>
              <td>{scopeName(p.limit_scope_id)}</td>
              <td>{p.min_limit_pct ?? '—'}</td>
              <td>{p.max_5h_pct ?? '—'}</td>
              <td>{p.max_weekly_pct ?? '—'}</td>
              <td>
                <input
                  type="number"
                  defaultValue={p.price}
                  onBlur={(e) => {
                    const newPrice = Number(e.target.value)
                    if (newPrice !== p.price && prices) {
                      const updated = [...prices]
                      updated[i] = { ...p, price: newPrice }
                      update.mutate(updated)
                    }
                  }}
                />₽
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
```

- [ ] **Step 4: Реализовать Templates page (редактор)**

`frontend/src/pages/Templates.tsx`:

```tsx
import { useState } from 'react'
import { useTemplates, useUpdateTemplates } from '../api/templates'
import type { MessageTemplate } from '../types/api'

export default function Templates() {
  const { data: templates } = useTemplates()
  const update = useUpdateTemplates()
  const [edits, setEdits] = useState<Record<string, string>>({})

  const save = (key: string, lang: string) => {
    const editKey = `${key}:${lang}`
    const content = edits[editKey]
    if (content === undefined) return
    if (templates) {
      const updated = templates.map((t) =>
        t.key === key && t.lang === lang ? { ...t, content } : t,
      )
      update.mutate(updated)
      setEdits({ ...edits, [editKey]: undefined })
    }
  }

  return (
    <div>
      <h1>Шаблоны сообщений</h1>
      <table className="data-table">
        <thead><tr><th>Ключ</th><th>Язык</th><th>Содержание</th><th></th></tr></thead>
        <tbody>
          {templates?.map((t) => {
            const editKey = `${t.key}:${t.lang}`
            return (
              <tr key={editKey}>
                <td>{t.key}</td>
                <td>{t.lang}</td>
                <td>
                  <textarea
                    defaultValue={t.content}
                    value={edits[editKey] ?? t.content}
                    onChange={(e) => setEdits({ ...edits, [editKey]: e.target.value })}
                    rows={3}
                  />
                </td>
                <td><button onClick={() => save(t.key, t.lang)}>Сохранить</button></td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
```

- [ ] **Step 5: Проверить сборку**

```bash
cd /c/Source/funpay/frontend
npm run build
```

- [ ] **Step 6: Commit**

```bash
cd /c/Source/funpay
git add frontend/src/
git commit -m "feat: add settings, prices, templates pages"
```

---

## Task 8: Глобальные стили + финальная проверка

**Files:**
- Create/Modify: `frontend/src/styles/global.css`
- Modify: `backend/app/main.py` — раздача статики frontend/dist (опционально)

- [ ] **Step 1: Базовые стили**

`frontend/src/styles/global.css`:

```css
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: system-ui, -apple-system, sans-serif; background: #f5f5f5; color: #333; }

.layout { display: flex; min-height: 100vh; }
.sidebar { width: 220px; background: #1a1a2e; padding: 20px 0; display: flex; flex-direction: column; }
.logo { color: #fff; font-size: 18px; padding: 0 20px 20px; font-weight: bold; }
.sidebar nav { display: flex; flex-direction: column; flex: 1; }
.sidebar nav a { color: #ccc; padding: 10px 20px; text-decoration: none; }
.sidebar nav a:hover, .sidebar nav a.active { background: #16213e; color: #fff; }
.sidebar button { margin: 0 20px; padding: 8px; background: transparent; color: #ff6b6b; border: 1px solid #ff6b6b; cursor: pointer; border-radius: 4px; }
.content { flex: 1; padding: 24px; overflow-y: auto; }
.content h1 { margin-bottom: 16px; }

.login-page { display: flex; justify-content: center; align-items: center; min-height: 100vh; }
.login-form { background: #fff; padding: 32px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); display: flex; flex-direction: column; gap: 12px; min-width: 320px; }
.login-form input { padding: 10px; border: 1px solid #ddd; border-radius: 4px; font-size: 14px; }
.login-form button { padding: 10px; background: #4f46e5; color: #fff; border: none; border-radius: 4px; cursor: pointer; }
.error { color: #dc2626; font-size: 14px; }

.metrics-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 16px; margin-bottom: 20px; }
.metric-card { background: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
.metric-value { font-size: 28px; font-weight: bold; color: #4f46e5; }
.metric-label { color: #666; font-size: 14px; margin-top: 4px; }
.bot-status { padding: 12px; background: #fff; border-radius: 8px; }
.status-ok { color: #16a34a; }
.status-error { color: #dc2626; }

.data-table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
.data-table th, .data-table td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #eee; font-size: 14px; }
.data-table th { background: #f9fafb; font-weight: 600; }
.data-table tr:hover { background: #f9fafb; }

.tabs { display: flex; gap: 4px; margin-bottom: 16px; }
.tabs button { padding: 8px 16px; border: 1px solid #ddd; background: #fff; cursor: pointer; border-radius: 4px; }
.tabs button.active { background: #4f46e5; color: #fff; border-color: #4f46e5; }

.inline-form { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
.inline-form input, .inline-form select { padding: 8px; border: 1px solid #ddd; border-radius: 4px; }
.inline-form button { padding: 8px 16px; background: #4f46e5; color: #fff; border: none; border-radius: 4px; cursor: pointer; }

.settings-form { display: flex; flex-direction: column; gap: 12px; max-width: 480px; }
.settings-form label { display: flex; flex-direction: column; gap: 4px; font-size: 14px; }
.settings-form input { padding: 8px; border: 1px solid #ddd; border-radius: 4px; }
.settings-form button { padding: 10px; background: #4f46e5; color: #fff; border: none; border-radius: 4px; cursor: pointer; margin-top: 8px; }

.status-active { color: #16a34a; }
.status-paused { color: #ca8a04; }
.status-deleted { color: #dc2626; }
.status-pending_validation { color: #ca8a04; }
.status-maintenance { color: #dc2626; }
```

- [ ] **Step 2: Добавить раздачу статики в backend (опционально)**

В `backend/app/main.py` добавить (после routers):

```python
import os
from fastapi.staticfiles import StaticFiles

# Раздача собранной SPA (frontend/dist) — только если директория существует
_frontend_dist = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "dist")
if os.path.isdir(_frontend_dist):
    app.mount("/", StaticFiles(directory=_frontend_dist, html=True), name="frontend")
```

- [ ] **Step 3: Финальная сборка**

```bash
cd /c/Source/funpay/frontend
npm run build
```
Expected: `dist/` создана без ошибок

- [ ] **Step 4: Проверить что backend тесты не сломались**

```bash
cd /c/Source/funpay/backend
py -3.12 -m pytest 2>&1 | tail -5
```
Expected: ALL PASS (195 тестов)

- [ ] **Step 5: Commit**

```bash
cd /c/Source/funpay
git add frontend/src/styles/ backend/app/main.py
git commit -m "feat: add global styles and static file serving"
```

---

## Замечания

### Разработка

- **Vite proxy**: `server.proxy['/api']` направляет API-запросы на `localhost:8000` (backend). При разработке: запускаешь backend (`uvicorn`) + frontend (`npm run dev`) одновременно.
- **httpOnly cookie**: браузер автоматически отправляет cookie с `credentials: 'include'`. Vite proxy проксирует cookie между портами.
- **ProtectedRoute**: использует `useQuery` с `/metrics` для проверки auth. 401 → `window.location.href = '/login'`. Это не идеально (полная перезагрузка страницы), но надёжно.

### Что НЕ делает Фаза 6

- **CSV bulk upload UI** — только API готов (`/accounts/bulk`), UI можно добавить позже
- **Inline price matrix editing** — упрощённый вариант (input onBlur), без 4-мерного матричного редактора
- **Charts** — дашборд без графика лимитов (просто метрики)
- **i18n** — интерфейс на русском
- **Тёмная тема** — только светлая
- **Мобильная адаптация** — только десктоп
- **Unit-тесты фронтенда** — Vitest установлен, но smoke-тесты не написаны (Фаза 6 фокус на функциональности)
