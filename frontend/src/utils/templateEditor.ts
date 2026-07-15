export const PLACEHOLDER_PATTERN = /\{([a-zA-Z0-9_]+)\}/g

export const DEPRECATED_DURATION_TEMPLATE_FIELDS: ReadonlySet<string> = new Set(['days'])
export const DEPRECATED_LIMIT_TEMPLATE_FIELDS: ReadonlySet<string> = new Set([
  'chat_5h',
  'chat_weekly',
  'codex_5h',
  'codex_weekly',
  'codex_primary_limit',
  'codex_primary_window',
  'codex_primary_reset',
  'codex_secondary_limit',
  'codex_secondary_window',
  'codex_secondary_reset',
])
export const DEPRECATED_LOT_LIMIT_TEMPLATE_FIELDS: ReadonlySet<string> = new Set(['short_limit'])
export const DEPRECATED_MESSAGE_TEMPLATE_FIELDS: ReadonlySet<string> = new Set([
  ...DEPRECATED_DURATION_TEMPLATE_FIELDS,
  ...DEPRECATED_LIMIT_TEMPLATE_FIELDS,
])
export const DEPRECATED_LOT_TEMPLATE_FIELDS: ReadonlySet<string> = new Set([
  ...DEPRECATED_DURATION_TEMPLATE_FIELDS,
  ...DEPRECATED_LOT_LIMIT_TEMPLATE_FIELDS,
])

export function classifyTemplateFields(
  usedFields: string[],
  allowedFields: string[],
  deprecatedFields: ReadonlySet<string>,
) {
  const allowed = new Set(allowedFields)
  return {
    deprecated: usedFields.filter((field) => deprecatedFields.has(field)),
    unknown: usedFields.filter((field) => !allowed.has(field) && !deprecatedFields.has(field)),
  }
}

export function extractTemplateFields(value: string) {
  return [...new Set([...value.matchAll(PLACEHOLDER_PATTERN)].map((match) => match[1]))]
}

export function renderTemplatePreview(value: string, samples: Readonly<Record<string, string>>) {
  return value.replace(PLACEHOLDER_PATTERN, (_, field: string) => samples[field] ?? `{${field}}`)
}

export function insertTemplateField(value: string, field: string, start = value.length, end = start) {
  const placeholder = `{${field}}`
  return {
    value: `${value.slice(0, start)}${placeholder}${value.slice(end)}`,
    cursor: start + placeholder.length,
  }
}

export function normalizeTemplateKey(value: string) {
  return value
    .trim()
    .toLowerCase()
    .replaceAll(/[^a-z0-9_-]+/g, '-')
    .replaceAll(/-{2,}/g, '-')
    .replaceAll(/^-|-$/g, '')
}

export function templateKeyForName(
  name: string,
  currentKey: string,
  manuallyEdited: boolean,
) {
  return manuallyEdited ? currentKey : normalizeTemplateKey(name)
}
