import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { useLogout } from '../api/auth'
import { useMetrics } from '../api/metrics'
import { Icon, type IconName } from './Icon'
import { StatusBadge } from './ui'

interface NavItem {
  to: string
  label: string
  icon: IconName
  end?: boolean
}

const NAV_GROUPS: Array<{ label: string; items: NavItem[] }> = [
  {
    label: 'Рабочее пространство',
    items: [
      { to: '/', label: 'Обзор', icon: 'dashboard', end: true },
      { to: '/accounts', label: 'Аккаунты', icon: 'accounts' },
      { to: '/chats', label: 'Чаты', icon: 'chat' },
      { to: '/orders', label: 'Сделки', icon: 'deals' },
      { to: '/lots', label: 'Лоты', icon: 'lots' },
    ],
  },
  {
    label: 'Управление',
    items: [
      { to: '/prices', label: 'Цены', icon: 'prices' },
      { to: '/catalog', label: 'Справочники', icon: 'catalog' },
      { to: '/templates', label: 'Шаблоны', icon: 'templates' },
    ],
  },
  {
    label: 'Система',
    items: [{ to: '/settings', label: 'Настройки', icon: 'settings' }],
  },
]

export default function Layout() {
  const [mobileOpen, setMobileOpen] = useState(false)
  const location = useLocation()
  const logout = useLogout()
  const { data: metrics } = useMetrics()

  useEffect(() => {
    setMobileOpen(false)
    window.scrollTo(0, 0)
  }, [location.pathname])

  const handleLogout = async () => {
    await logout.mutateAsync()
    window.location.href = '/login'
  }

  return (
    <div className="app-shell">
      <button
        className={`sidebar-backdrop ${mobileOpen ? 'sidebar-backdrop--visible' : ''}`}
        onClick={() => setMobileOpen(false)}
        aria-label="Закрыть меню"
      />
      <aside className={`sidebar ${mobileOpen ? 'sidebar--open' : ''}`}>
        <div className="brand">
          <div className="brand__mark"><span>F</span></div>
          <div>
            <strong>FunPay Rental</strong>
            <span>Operations console</span>
          </div>
        </div>

        <nav className="sidebar-nav" aria-label="Основная навигация">
          {NAV_GROUPS.map((group) => (
            <div className="nav-group" key={group.label}>
              <div className="nav-group__label">{group.label}</div>
              {group.items.map((item) => (
                <NavLink key={item.to} to={item.to} end={item.end} className="nav-link">
                  <Icon name={item.icon} size={19} />
                  <span>{item.label}</span>
                </NavLink>
              ))}
            </div>
          ))}
        </nav>

        <div className="sidebar-status">
          <div className="sidebar-status__head">
            <span>Состояние системы</span>
            <StatusBadge value={metrics?.bot_status ?? 'unknown'} />
          </div>
          <p>{metrics?.bot_status === 'connected' ? 'FunPay принимает события' : 'Интеграция требует настройки'}</p>
          <NavLink to="/settings">Открыть настройки <Icon name="arrow-right" size={15} /></NavLink>
        </div>

        <button className="logout-button" onClick={handleLogout} disabled={logout.isPending}>
          <Icon name="logout" />
          {logout.isPending ? 'Завершаем сессию…' : 'Выйти'}
        </button>
      </aside>

      <div className="workspace">
        <header className="topbar">
          <button className="icon-button topbar__menu" onClick={() => setMobileOpen(true)} aria-label="Открыть меню">
            <Icon name="menu" size={21} />
          </button>
          <div className="topbar__context">
            <span className={`connection-pulse ${metrics?.bot_status === 'connected' ? 'connection-pulse--online' : ''}`} />
            <span>{metrics?.bot_status === 'connected' ? 'Система работает' : 'Требуется настройка интеграции'}</span>
          </div>
          <div className="topbar__meta">
            <span>Admin</span>
            <div className="avatar" aria-hidden="true">A</div>
          </div>
        </header>
        <main className="content">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
