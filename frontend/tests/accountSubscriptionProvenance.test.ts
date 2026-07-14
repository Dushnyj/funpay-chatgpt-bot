import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'


test('account editor cannot submit an operator-controlled subscription deadline', async () => {
  const source = await readFile(
    new URL('../src/pages/Accounts.tsx', import.meta.url),
    'utf8',
  )

  assert.doesNotMatch(source, /type="datetime-local"/)
  assert.doesNotMatch(source, /subscription_expires_at\s*:/)
  assert.match(
    source,
    /срок подписки определяются только автоматической проверкой OpenAI/,
  )
  assert.match(source, /Не подтверждена/)
})


test('write payload types do not expose subscription expiry', async () => {
  const source = await readFile(
    new URL('../src/types/api.ts', import.meta.url),
    'utf8',
  )
  const createBlock = source.match(
    /export interface AccountCreate \{([\s\S]*?)\n\}/,
  )?.[1] ?? ''
  const updateBlock = source.match(
    /export interface AccountUpdate \{([\s\S]*?)\n\}/,
  )?.[1] ?? ''

  assert.ok(createBlock)
  assert.ok(updateBlock)
  assert.doesNotMatch(createBlock, /subscription_expires_at/)
  assert.doesNotMatch(updateBlock, /subscription_expires_at/)
})
