import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'


test('manual browser confirmation is wired to the guarded account endpoint', async () => {
  const source = await readFile(
    new URL('../src/api/accounts.ts', import.meta.url),
    'utf8',
  )

  assert.match(source, /export function useConfirmBrowserValidation\(\)/)
  assert.match(source, /`\/accounts\/\$\{id\}\/confirm-browser-validation`/)
  assert.match(source, /invalidateQueries\(\{ queryKey: \['accounts'\] \}\)/)
  assert.match(source, /invalidateQueries\(\{ queryKey: \['metrics'\] \}\)/)
})


test('manual confirmation is offered only for a blocking Cloudflare challenge', async () => {
  const source = await readFile(
    new URL('../src/pages/Accounts.tsx', import.meta.url),
    'utf8',
  )
  const eligibility = source.match(
    /function needsManualBrowserConfirmation\(account: Account\) \{([\s\S]*?)\n\}/,
  )?.[1] ?? ''

  assert.match(eligibility, /validationState\(account\) === 'validation_failed'/)
  assert.match(eligibility, /account\.validation_job\?\.error_code === 'cloudflare_challenge'/)
  assert.match(source, /needsManualBrowserConfirmation\(account\)/)
  assert.match(source, /aria-label=\{`Подтвердить ручной вход для \$\{account\.login\}`\}/)
})


test('confirmation modal explains the saved password and Key OTP requirement', async () => {
  const source = await readFile(
    new URL('../src/pages/Accounts.tsx', import.meta.url),
    'utf8',
  )

  assert.match(source, /Подтвердить ручной вход\?/)
  assert.match(source, /с сохранённым паролем и одноразовым кодом из кнопки «Ключ»/)
  assert.match(source, /confirmBrowserValidation\.mutateAsync\(browserValidationTarget\.id\)/)
  assert.match(source, /await refetchAccounts\(\)/)
  assert.match(source, /Ручной вход подтверждён/)
})


test('device authorization copy separates tokens from credential validation and permits closing', async () => {
  const source = await readFile(
    new URL('../src/pages/Accounts.tsx', import.meta.url),
    'utf8',
  )

  assert.match(source, /Токены OpenAI подтверждены\. Проверка сохранённого пароля и одноразового кода запущена\./)
  assert.match(source, /<ModalOverlay onClose=\{onClose\}>/)
  assert.match(source, /Окно можно закрыть — проверка продолжится в фоне/)
  assert.match(source, /<button className="icon-button" onClick=\{onClose\} aria-label="Закрыть">/)
  assert.doesNotMatch(source, /Не закрывайте окно до завершения проверки/)
})
