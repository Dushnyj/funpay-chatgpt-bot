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
