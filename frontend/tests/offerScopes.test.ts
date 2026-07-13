import assert from 'node:assert/strict'
import test from 'node:test'

import { compareOfferScopes } from '../src/utils/offerScopes.ts'

test('offer scopes keep canonical system order and place unknown codes last', () => {
  const scopes = [
    { id: 8, code: 'legacy-z' },
    { id: 5, code: 'codex' },
    { id: 3, code: 'chat' },
    { id: 2, code: 'any' },
    { id: 7, code: 'alpha' },
    { id: 4, code: 'CODEX' },
  ]

  assert.deepEqual(
    scopes.sort(compareOfferScopes).map((scope) => `${scope.code.toLowerCase()}:${scope.id}`),
    ['any:2', 'chat:3', 'codex:4', 'codex:5', 'alpha:7', 'legacy-z:8'],
  )
})
