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
        <button
          className="logout-btn"
          onClick={() => logout.mutateAsync().then(() => { window.location.href = '/login' })}
        >
          Выйти
        </button>
      </aside>
      <main className="content">
        <Outlet />
      </main>
    </div>
  )
}
