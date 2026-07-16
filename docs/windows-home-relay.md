# Домашний маршрут входа OpenAI и почты

## Назначение

Home Relay даёт проверкам OpenAI и входам в почту домашний исходящий IP без
запуска Chromium на Windows-ПК. Это включает Outlook/Hotmail через Outlook Web
и обычные TLS IMAP-подключения (Gmail, Yahoo, iCloud и кастомные серверы).
Остальной backend, FunPay, админка и сайты продолжают использовать обычный
маршрут сервера.

Microsoft Graph после выданного OAuth-согласия остаётся прямым: это API-доступ
по токену, а не парольный вход. Пароли почты и IMAP-команды не видны relay:
внутри SSH-туннеля сохраняется сквозной TLS до почтового сервера.

Это транспорт, а не средство обхода CAPTCHA: если OpenAI или Microsoft всё равно
потребовали интерактивную проверку, backend должен вернуть точный challenge.

## Сетевой контракт

```text
Playwright (OpenAI / Outlook Web) и TLS IMAP
  -> socks5://home-relay:1080 (Docker internal network)
  -> OpenSSH sidecar sshd
  <- reverse dynamic forward from Windows: -R 0.0.0.0:1080
  -> домашний интернет
```

- `funpay-bot.duckdns.org:2222/tcp` — единственный публичный порт sidecar;
- `home-relay:1080` — только внутренняя Docker-сеть;
- у `1080` не должно быть host `ports:` mapping, nginx location или firewall
  allow-rule;
- auth и email fail closed: при недоступном relay нельзя незаметно повторять вход
  с IP VPS.

Ограничение «только вход OpenAI и почты» обеспечивается кодом backend и изоляцией
Docker-сети, а не протоколом SOCKS: технически reverse SOCKS способен достигать
адресов, доступных с домашнего ПК, включая локальную сеть. Поэтому к
`login_relay` подключён только доверенный backend, порт 1080 не публикуется, а
sidecar нельзя использовать как общий прокси. Для жёсткой сетевой сегментации
домашний relay следует запускать в гостевой VLAN или отдельной VM без доступа к
домашним устройствам.

Sidecar ожидает `GatewayPorts clientspecified`, `AllowTcpForwarding remote`,
`PermitTTY no`, `PasswordAuthentication no`, `KbdInteractiveAuthentication no`
и отдельного пользователя `relay` без sudo и обычной shell-сессии. Каждый ключ
ограничивается в `authorized_keys`, например:

```text
restrict,port-forwarding,permitlisten="0.0.0.0:1080" ssh-ed25519 AAAA... relay-id
```

Pairing API должен атомарно добавить только переданный public key, связать его с
route и удалить при удалении route. Root password/private key никогда не
передаются клиенту.

## Pairing API v1

Запрос `POST /api/proxy-routes/home-relay/enroll`:

```http
Authorization: Bearer <одноразовый setup token>
Content-Type: application/json
```

```json
{
  "schema_version": 1,
  "machine_name": "DESKTOP-01",
  "display_name": "Домашний ПК",
  "public_key": "ssh-ed25519 AAAA... funpay-home-relay:...",
  "client_version": "1.0.0"
}
```

Ответ:

```json
{
  "schema_version": 1,
  "relay_id": "relay-1-abcdef012345",
  "display_name": "Домашний ПК",
  "ssh_host": "funpay-bot.duckdns.org",
  "ssh_port": 2222,
  "ssh_user": "relay",
  "remote_socks_bind": "0.0.0.0",
  "remote_socks_port": 1080,
  "host_key": {
    "type": "ssh-ed25519",
    "data": "AAAA..."
  }
}
```

Setup token должен быть write-only, одноразовым, иметь короткий TTL и храниться
на сервере только в виде hash. Готовая команда содержит token и может попасть в
локальную историю PowerShell, поэтому UI предупреждает не пересылать её; после
успешной привязки или TTL token бесполезен. Рекомендуется возвращать `host_key`: fallback TOFU
оставлен лишь для совместимости клиента. UI возвращает готовые
`powershell_command`, `script_download_url` и `expires_at`; frontend не
конструирует секретную команду самостоятельно.

Новая setup-команда не считается простым repair: даже при существующем
`relay.json` Windows-клиент повторно отправляет сохранённый public key, принимает
новые route-параметры и host key, выполняет SSH probe и лишь затем запускает
tunnel. Запуск установщика без `PairingUrl`/`PairingCode` остаётся локальным
repair и не обращается к enrollment API.

Для `autostart=true` команда передаёт `-EnableAutoStart`, для ручного режима —
`-DisableAutoStart`. Task Scheduler запускает relay от `SYSTEM` при старте ОС,
до входа пользователя, и перезапускает при аварийном завершении; сам runner
дополнительно переподключает SSH с задержкой 2–60 секунд. Команду для boot-режима
нужно вставлять в PowerShell, заранее открытый через «Запуск от имени
администратора». Команда атомарно создаёт staging в `ProgramData` с ACL только
для `SYSTEM`/Administrators, закрепляет SHA-256 архива, очищает `PSModulePath` от
пользовательских модулей и запускает Windows PowerShell 5.1. Загруженный
установщик никогда не повышает себя сам.

Ручной режим выполняется только без повышения прав. Если встроенного OpenSSH
нет, административный проход устанавливает только Windows Capability и
завершается до enrollment; ту же ещё действующую команду затем повторяют в
обычном PowerShell. Переход на boot-режим не исполняет и не удаляет старые файлы
из `%LOCALAPPDATA%`: процессы останавливаются по фиксированным command-line
признакам, а каталог остаётся как неактивная резервная копия.

Ручная установка находится в `%LOCALAPPDATA%`; boot-установка — в
`%ProgramData%`. В boot-варианте системная задача исполняет только файлы, которые
обычный пользователь не может изменить. Приватный ключ читают только `SYSTEM` и
локальные администраторы; Start/Stop/Status поэтому показывают UAC.

## Эксплуатация

В ручном режиме Start/Stop/Status доступны в меню «Пуск» и, по выбору, на
рабочем столе. Boot-режим не пишет elevated-файлы в пользовательские Start
Menu/Desktop: relay стартует системной задачей, а ручное управление выполняется
скриптами из `%ProgramData%\FunPayHomeRelay` в административном PowerShell. ПК
потребляет ресурсы одного `powershell.exe` и одного `ssh.exe`; браузер там не
работает. Для постоянной доступности отключите сон, но не требуется держать
открытой админку.

В админке маршрут имеет три состояния: `unchecked`, `online` и `offline`.
Проверка запускает отдельный Chromium через SOCKS и читает внешний IP с
нейтрального HTTPS endpoint. Только `online`-маршрут разрешается назначить
основным или привязать к аккаунту. Пароли OpenAI/почты, OTP, cookies и
содержимое страниц в диагностический лог не записываются.
