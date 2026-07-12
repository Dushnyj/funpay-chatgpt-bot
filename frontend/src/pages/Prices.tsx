import { usePrices, useUpdatePrices } from '../api/prices'
import { useTiers, useDurations, useLimitScopes } from '../api/catalog'
import type { PriceMatrixItem } from '../types/api'

export default function Prices() {
  const { data: prices } = usePrices()
  const { data: tiers } = useTiers()
  const { data: durations } = useDurations()
  const { data: scopes } = useLimitScopes()
  const update = useUpdatePrices()

  const tierName = (id: number) => tiers?.find((t) => t.id === id)?.name ?? `#${id}`
  const durDays = (id: number) => durations?.find((d) => d.id === id)?.days ?? '?'
  const scopeName = (id: number) => scopes?.find((s) => s.id === id)?.code ?? '?'

  const updatePrice = (index: number, newPrice: number) => {
    if (!prices) return
    const updated: PriceMatrixItem[] = prices.map((p, i) =>
      i === index ? { ...p, price: newPrice } : p,
    )
    update.mutate(updated)
  }

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
                    const v = Number(e.target.value)
                    if (v !== p.price) updatePrice(i, v)
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
