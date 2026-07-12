import { useLots, useDeleteLot } from '../api/lots'
import { useTiers, useDurations, useLimitScopes } from '../api/catalog'

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
