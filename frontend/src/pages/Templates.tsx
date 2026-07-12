import { useState } from 'react'
import { useTemplates, useUpdateTemplates } from '../api/templates'

export default function Templates() {
  const { data: templates } = useTemplates()
  const update = useUpdateTemplates()
  const [edits, setEdits] = useState<Record<string, string>>({})

  const save = (key: string, lang: string) => {
    const editKey = `${key}:${lang}`
    const content = edits[editKey]
    if (content === undefined || !templates) return
    const updated = templates.map((t) =>
      t.key === key && t.lang === lang ? { ...t, content } : t,
    )
    update.mutate(updated)
    setEdits((prev) => {
      const next = { ...prev }
      delete next[editKey]
      return next
    })
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
