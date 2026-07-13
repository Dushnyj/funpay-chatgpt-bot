export const PLACEHOLDER_PATTERN = /\{([a-zA-Z0-9_]+)\}/g

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
