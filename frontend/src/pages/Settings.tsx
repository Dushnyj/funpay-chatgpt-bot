import { useState } from 'react'
import type { FormEvent } from 'react'
import { useSettings, useUpdateSettings } from '../api/settings'
import type { Settings as SettingsType } from '../types/api'

export default function Settings() {
  const { data, isLoading } = useSettings()
  const update = useUpdateSettings()
  const [form, setForm] = useState<Partial<SettingsType>>({})

  if (isLoading) return <div>Загрузка...</div>
  if (!data) return <div>Настройки не сконфигурированы</div>

  const current = { ...data, ...form }

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault()
    update.mutate(form)
  }

  return (
    <div>
      <h1>Настройки</h1>
      <form onSubmit={handleSubmit} className="settings-form">
        <label>FunPay Node ID
          <input type="number" value={current.funpay_node_id ?? ''} onChange={(e) => setForm({ ...form, funpay_node_id: Number(e.target.value) })} />
        </label>
        <label>Лимит аренд по умолчанию
          <input type="number" value={current.default_max_active_rentals} onChange={(e) => setForm({ ...form, default_max_active_rentals: Number(e.target.value) })} />
        </label>
        <label>Комиссия FunPay (%)
          <input type="number" value={current.funpay_commission_percent} onChange={(e) => setForm({ ...form, funpay_commission_percent: Number(e.target.value) })} />
        </label>
        <label>Интервал проверки (мин)
          <input type="number" value={current.check_interval_minutes} onChange={(e) => setForm({ ...form, check_interval_minutes: Number(e.target.value) })} />
        </label>
        <label>Интервал замеров лимитов (мин)
          <input type="number" value={current.limits_check_interval_minutes} onChange={(e) => setForm({ ...form, limits_check_interval_minutes: Number(e.target.value) })} />
        </label>
        <label>Bump интервал (часы)
          <input type="number" value={current.bump_interval_hours} onChange={(e) => setForm({ ...form, bump_interval_hours: Number(e.target.value) })} />
        </label>
        <label>Авто-bump
          <input type="checkbox" checked={current.auto_bump_enabled} onChange={(e) => setForm({ ...form, auto_bump_enabled: e.target.checked })} />
        </label>
        <label>Порог уведомления о лимитах (%)
          <input type="number" value={current.limits_warn_threshold_pct} onChange={(e) => setForm({ ...form, limits_warn_threshold_pct: Number(e.target.value) })} />
        </label>
        <button type="submit">Сохранить</button>
      </form>
    </div>
  )
}
