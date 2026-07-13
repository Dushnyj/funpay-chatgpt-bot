import assert from 'node:assert/strict'
import test from 'node:test'

import {
  classifyTemplateFields,
  DEPRECATED_DURATION_TEMPLATE_FIELDS,
  DEPRECATED_MESSAGE_TEMPLATE_FIELDS,
  extractTemplateFields,
  insertTemplateField,
  normalizeTemplateKey,
  renderTemplatePreview,
  templateKeyForName,
} from '../src/utils/templateEditor.ts'

test('extracts unique placeholders in source order', () => {
  assert.deepEqual(extractTemplateFields('Привет, {login}. Код {code}, снова {login}.'), ['login', 'code'])
})

test('renders known samples and leaves unsupported placeholders visible', () => {
  assert.equal(
    renderTemplatePreview('{tier}: {limit}; {unknown}', { tier: 'Plus', limit: '79%' }),
    'Plus: 79%; {unknown}',
  )
})

test('classifies supported legacy fields as deprecated instead of unknown', () => {
  assert.deepEqual(
    classifyTemplateFields(
      ['chat_5h', 'chat_weekly', 'days', 'unknown'],
      ['days'],
      DEPRECATED_MESSAGE_TEMPLATE_FIELDS,
    ),
    {
      deprecated: ['chat_5h', 'chat_weekly', 'days'],
      unknown: ['unknown'],
    },
  )
  assert.deepEqual(
    classifyTemplateFields(['days', 'chat_5h'], [], DEPRECATED_DURATION_TEMPLATE_FIELDS),
    { deprecated: ['days'], unknown: ['chat_5h'] },
  )
})

test('inserts a placeholder at the current selection and returns the next cursor', () => {
  assert.deepEqual(insertTemplateField('Код: сейчас', 'code', 5, 11), {
    value: 'Код: {code}',
    cursor: 11,
  })
})

test('normalizes a readable name to an API-safe lot-template key', () => {
  assert.equal(normalizeTemplateKey('  Plus / Codex 7 days  '), 'plus-codex-7-days')
})

test('keeps the generated key in sync until the operator edits it', () => {
  assert.equal(templateKeyForName('P', '', false), 'p')
  assert.equal(templateKeyForName('Plus · Codex', 'p', false), 'plus-codex')
  assert.equal(templateKeyForName('Plus · Codex', 'custom-key', true), 'custom-key')
})
