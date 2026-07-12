import { Routes, Route } from 'react-router-dom'
import Layout from './components/Layout'
import ProtectedRoute from './components/ProtectedRoute'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import Chats from './pages/Chats'
import Accounts from './pages/Accounts'
import Tiers from './pages/Tiers'
import Lots from './pages/Lots'
import Orders from './pages/Orders'
import Prices from './pages/Prices'
import Templates from './pages/Templates'
import Settings from './pages/Settings'
import NotFound from './pages/NotFound'

export default function App() {
  return (
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
  )
}
