import assert from 'node:assert/strict'
import test from 'node:test'

import {
  hasProxyRouteErrors,
  loginRouteEndpoint,
  loginRouteErrorLabel,
  loginRouteKindLabel,
  loginRouteStatusLabel,
  validateProxyRouteDraft,
} from '../src/utils/loginRoutes.ts'

test('custom proxy validation accepts unauthenticated and authenticated routes', () => {
  const publicProxy = validateProxyRouteDraft({
    name: 'Резидентский EU', proxyType: 'socks5', host: 'nl.example.net', port: '1080', username: '', password: '',
  })
  const authenticatedProxy = validateProxyRouteDraft({
    name: 'Residential #1', proxyType: 'https', host: '192.0.2.10', port: '8443', username: 'tenant', password: 'secret',
  })

  assert.equal(hasProxyRouteErrors(publicProxy), false)
  assert.equal(hasProxyRouteErrors(authenticatedProxy), false)
})

test('custom proxy validation rejects URLs, unsafe ports and incomplete credentials', () => {
  const errors = validateProxyRouteDraft({
    name: 'x', proxyType: 'http', host: 'https://proxy.example/path', port: '70000', username: '', password: 'secret',
  })

  assert.equal(errors.name, 'Название должно содержать от 2 до 80 символов.')
  assert.equal(errors.host, 'Укажите только домен или IP без протокола и пути.')
  assert.equal(errors.port, 'Порт должен быть целым числом от 1 до 65535.')
  assert.equal(errors.credentials, 'Для пароля укажите логин прокси.')
})

test('saved write-only password can be retained while editing a proxy', () => {
  const errors = validateProxyRouteDraft({
    name: 'Домашний резерв', proxyType: 'https', host: 'proxy.example', port: '1080', username: 'relay', savedUsername: 'relay', password: '', hasSavedPassword: true,
  })
  assert.equal(hasProxyRouteErrors(errors), false)
})

test('changing a saved username requires writing the new password again', () => {
  const errors = validateProxyRouteDraft({
    name: 'Residential', proxyType: 'https', host: 'proxy.example', port: '443', username: 'new-user', savedUsername: 'old-user', password: '', hasSavedPassword: true,
  })
  assert.equal(errors.credentials, 'Чтобы изменить логин, введите новый пароль повторно.')
})

test('SOCKS5 credentials are rejected because Playwright only supports proxy auth over HTTP', () => {
  const errors = validateProxyRouteDraft({
    name: 'SOCKS', proxyType: 'socks5', host: 'proxy.example', port: '1080', username: 'user', password: 'secret',
  })
  assert.equal(errors.credentials, 'SOCKS5 используется без авторизации. Для логина и пароля выберите HTTP или HTTPS CONNECT.')
})

test('route presentation does not expose credentials', () => {
  const route = {
    mode: 'custom_proxy' as const,
    proxy_type: 'socks5' as const,
    host: 'proxy.example',
    port: 1080,
  }
  assert.equal(loginRouteKindLabel(route), 'SOCKS5-прокси')
  assert.equal(loginRouteEndpoint(route), 'proxy.example:1080')
  assert.equal(loginRouteStatusLabel('online'), 'Онлайн')
  assert.equal(loginRouteStatusLabel('offline'), 'Недоступен')
  assert.equal(loginRouteStatusLabel('unchecked'), 'Не проверен')
  assert.equal(loginRouteErrorLabel('proxy_unavailable'), 'Прокси не отвечает или отклонил соединение.')
  assert.equal(loginRouteErrorLabel('proxy_test_failed'), 'Не удалось завершить проверку внешнего IP.')
  assert.equal(loginRouteErrorLabel('proxy_check_stale'), 'Нет свежего heartbeat. Проверьте домашний ПК или прокси.')
  assert.equal(loginRouteErrorLabel('runtime_proxy_unavailable'), 'Маршрут перестал отвечать во время входа.')
})
