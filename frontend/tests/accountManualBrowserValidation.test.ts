import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import {
  isCloudflareBrowserChallenge,
  isManualBrowserConfirmationAvailable,
} from '../src/utils/accountValidation.ts'


function cloudflareAccount(available: boolean) {
  return {
    status: 'validation_failed',
    operator_status_override: null,
    manual_browser_confirmation_available: available,
    manual_browser_confirmation_expires_at: available ? '2026-07-15T17:30:00Z' : null,
    validation_job: {
      id: 42,
      status: 'failed',
      job_type: 'full_validation',
      stage: 'login',
      error_code: 'cloudflare_challenge',
      error_detail: 'Cloudflare requested manual verification',
      created_at: '2026-07-15T17:00:00Z',
      started_at: '2026-07-15T17:00:01Z',
      finished_at: '2026-07-15T17:00:03Z',
    },
  }
}


test('account contract exposes backend-owned manual confirmation availability', async () => {
  const source = await readFile(
    new URL('../src/types/api.ts', import.meta.url),
    'utf8',
  )

  assert.match(source, /manual_browser_confirmation_available: boolean/)
  assert.match(source, /manual_browser_confirmation_expires_at: string \| null/)
})


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


test('manual confirmation requires both a Cloudflare failure and backend availability', () => {
  const unavailable = cloudflareAccount(false)
  const available = cloudflareAccount(true)

  assert.equal(isCloudflareBrowserChallenge(unavailable), true)
  assert.equal(isManualBrowserConfirmationAvailable(unavailable), false)
  assert.equal(isManualBrowserConfirmationAvailable(available), true)
  assert.equal(isManualBrowserConfirmationAvailable({
    ...available,
    validation_job: {
      ...available.validation_job,
      error_code: 'invalid_credentials',
    },
  }), false)
})


test('Cloudflare status is a warning and the green shield follows backend availability', async () => {
  const source = await readFile(
    new URL('../src/pages/Accounts.tsx', import.meta.url),
    'utf8',
  )
  const eligibility = source.match(
    /function needsManualBrowserConfirmation\(account: Account\) \{([\s\S]*?)\n\}/,
  )?.[1] ?? ''

  assert.match(eligibility, /isManualBrowserConfirmationAvailable\(account\)/)
  assert.match(source, /needsManualBrowserConfirmation\(account\)/)
  assert.match(source, /manualConfirmationAvailable[\s\S]*?'Подтвердите вход'[\s\S]*?'Нужен вход через браузер'/)
  assert.match(source, /statusValue = cloudflareChallenge \? 'pending' : state/)
  assert.match(source, /Войдите вручную: пароль \+ код из «Ключ»; затем нажмите зелёный щит/)
  assert.match(source, /aria-label=\{`Подтвердить ручной вход для \$\{account\.login\}`\}/)
})


test('icon action guidance separates Device Auth, auto validation, TOTP, and confirmation', async () => {
  const source = await readFile(
    new URL('../src/pages/Accounts.tsx', import.meta.url),
    'utf8',
  )

  assert.match(source, /Вход OpenAI \(Device Auth\): получить токены в браузере/)
  assert.match(source, /Повторить автоматическую проверку пароля и TOTP\. Device Auth не выполняется\./)
  assert.match(source, /Показать текущий код TOTP для ручного входа/)
  assert.match(source, /Подтвердить уже выполненный ручной вход/)
  assert.match(source, /account-icon-action account-icon-action--success/)
  assert.match(source, /!isValidationInProgress\(account\) && !isCloudflareBrowserChallenge\(account\)/)
  assert.match(source, /if \(isManualBrowserConfirmationAvailable\(account\)\) return false/)
})


test('confirmation modal explains the saved password and Key OTP requirement', async () => {
  const source = await readFile(
    new URL('../src/pages/Accounts.tsx', import.meta.url),
    'utf8',
  )

  assert.match(source, /Подтвердить ручной вход\?/)
  assert.match(source, /с сохранённым паролем и одноразовым кодом из кнопки «Ключ»/)
  assert.match(source, /https:\/\/chatgpt\.com\/auth\/login/)
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
