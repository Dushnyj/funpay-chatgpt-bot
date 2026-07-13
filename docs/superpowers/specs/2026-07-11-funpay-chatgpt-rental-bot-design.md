# Дизайн: Бот аренды ChatGPT-аккаунтов через FunPay

**Дата:** 2026-07-11
**Статус:** Draft (rev. 2 — добавлены лимиты и OAuth-замеры)

## 1. Назначение

Автоматизация продажи доступа к ChatGPT-аккаунтам на площадке FunPay. Продавец загружает пул аккаунтов в админ-панель, бот автоматически создаёт лоты, выдаёт данные покупателям по оплате, управляет 2FA-кодами, контролирует срок доступа и лимиты, выбивает пользователей по истечении.

## 2. Бизнес-модель

- **Безопасная модель аренды:** один аккаунт ChatGPT = не более одной активной аренды. Общие логин/пароль и глобальный «Log out everywhere» не позволяют независимо отозвать доступ одного из нескольких арендаторов.
- **2FA как барьер доступа:** TOTP-код требуется для входа покупателем. Бот выдаёт код только активным арендаторам; истёкшим — отказывает.
- **Кик по таймеру:** при истечении аренды бот выполняет «Log out everywhere» через Playwright — рефреш-токен инвалидируется, для повторного входа требуется 2FA-код. Пароль НЕ меняется (общий для всех арендаторов).
- **Тарифы:** продаются связки «тип подписки × срок × лимит × порог лимита» (Plus × 7 дней × Codex ≥50%, и т.д.).
- **Единый agentic-лимит:** Codex, Work, Workspace Agents и ChatGPT for Excel расходуют общий пул аккаунта; обычные разговоры Chat не учитываются. Бот сохраняет фактические окна OpenAI, регулярно их измеряет и снимает лоты, когда остаток ниже порога.

## 3. Стек

- **Backend:** Python 3.11+, asyncio, FastAPI, SQLAlchemy (async), asyncpg
- **Frontend:** React + TypeScript + Vite (SPA)
- **БД:** PostgreSQL
- **Браузерная автоматизация:** Playwright (Chromium, headless) — только для кика и первичной валидации
- **FunPay-интеграция:** [funpayhub/funpaybotengine](https://github.com/funpayhub/funpaybotengine) — за изолирующим интерфейсом
- **TOTP:** pyotp
- **HTTP-клиент для backend-api OpenAI:** httpx (замеры лимитов и подписки без браузера)
- **Шифрование секретов:** cryptography (Fernet)
- **Уведомления продавцу:** Telegram Bot API

## 4. Архитектура верхнего уровня

Один процесс, четыре asyncio-задачи в одном event loop:

1. **FunPay Engine** — WebSocket-слушатель чатов + обработчик событий заказов.
2. **Scheduler** — фоновые циклы: истечение аренд, авто-bump лотов, замеры лимитов/валидность аккаунтов, авто-создание/снятие лотов по квоте.
3. **Admin API** (FastAPI, uvicorn) — REST для SPA-панели.
4. **Browser automation** (Playwright) — запускается по требованию: кик (logout all), первичная валидация аккаунта (получение OAuth refresh_token).

Frontend (React SPA) — отдельная сборка, раздаётся как статика через FastAPI.

### Изоляция FunPay-слоя

Бизнес-логика ничего не знает про FunPay. Взаимодействие через интерфейс `ChatGateway` с двумя реализациями: `FunPayChatGateway` (прод) и `FakeChatGateway` (тесты). Замена FunPay-библиотеки = замена одной реализации.

### Изоляция OpenAI-слоя

Замеры лимитов и подписки — через интерфейс `OpenAIClient` с реализацией `ChatGPTBackendClient` (HTTP к backend-api). Playwright-операции (кик, валидация) — отдельный интерфейс `BrowserAutomation`.

## 5. Получение и обновление токенов (на основе codex-switcher)

Аккаунты в пуле хранят OAuth-токены для замеров лимитов, **отдельно** от логин:пароль+TOTP для доступа покупателей.

### Первичная валидация (при добавлении аккаунта)

Playwright OAuth device flow:
1. Логин на auth.openai.com (login:pass + TOTP из totp_secret)
2. Проход OAuth flow → получение `refresh_token`, `id_token`, `account_id`
3. `id_token` (JWT) парсится → извлекаются `email`, `plan_type`, `subscription_expires_at`
4. Сохранение: `refresh_token_encrypted`, `account_id_openai` в AccountLimits

### Обновление access_token

```
POST https://auth.openai.com/oauth/token
Content-Type: application/x-www-form-urlencoded
Body: grant_type=refresh_token&refresh_token={rt}&client_id=app_EMoamEEZ73f0CkXaXp7hrann
→ {access_token, refresh_token (новый, ротация), id_token}
```

Старый refresh_token инвалидируется (ротация). Новые токены сохраняются. Обновление перед каждым замером, если `access_token_expires_at < NOW() + 5 мин`.

### Протухание refresh_token и авто-восстановление

Refresh_token протухает регулярно — основная причина: наш кик (logout all) по таймеру. Это **штатный сценарий**, обрабатывается автоматическим перезаходом.

```
refresh_token протух (HTTP-замер вернул ошибку refresh)
    │
    ▼
AccountLimits.refresh_status = expired
account → maintenance (временно, лоты снимаются)
job priority='refresh_recover' в очередь
    │
    ▼
Воркер перезахода (отдельный пул, refresh_recover_concurrency):
  Playwright OAuth device flow:
    1. Логин на auth.openai.com (login:pass + TOTP из totp_secret)
    2. OAuth flow → новые refresh_token + access_token + id_token
    3. Парсинг id_token → plan_type, subscription_expires_at
    4. Сохранение токенов в AccountLimits
    │
    ├── успех: refresh_status=ok, account → active, свежий замер лимитов
    │
    └── провал (пароль сменён/бан/2FA сломана):
        refresh_recover_attempts++
        retry через refresh_retry_delay_minutes
        после refresh_max_attempts → refresh_status=failed, account → banned/maintenance
        уведомление продавцу «Аккаунт X требует вмешательства»
```

Перезаход создаёт **новую сессию** для бота в incognito-контексте — не вызывает logout all, активных арендаторов не затрагивает. Изолировано от KickService.

## 6. Замеры лимитов и подписки (без браузера)

### Эндпоинт лимитов

```
GET https://chatgpt.com/backend-api/wham/usage
Headers:
  Authorization: Bearer {access_token}
  chatgpt-account-id: {account_id_openai}
  User-Agent: codex-cli/1.0.0
```

JSON-ответ содержит:
- `rate_limit.primary_window`: {`used_percent`, `limit_window_seconds`, `reset_at`}
- `rate_limit.secondary_window`: те же поля; может отсутствовать
- `plan_type`: "plus" | "pro" | ...
- `credits`: остаток кредитов

**Остаток %** = `100 - used_percent`. Это единый agentic-лимит, общий для Codex, Work, Workspace Agents и ChatGPT for Excel; обычные разговоры Chat в него не входят. Отдельного измеримого Chat-лимита в модели нет. Длительность каждого окна берётся только из `limit_window_seconds`: Free в текущих наблюдениях может возвращать 30 дней, платные планы — одно или несколько фактических окон. Нельзя жёстко подписывать primary как «5 часов», а secondary как «неделя» без проверки ответа OpenAI.

### Эндпоинт подписки

```
GET https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27
Headers: те же
```

Возвращает `plan_type` и `entitlement.expires_at` (срок подписки).

### Частота замеров

- **По расписанию:** каждые 5–10 мин (настраивается, `limits_check_interval_minutes`) для аккаунтов `status=active`.
- **По событию (on-demand):** перед выдачей аренды — свежий замер для решения о выдаче.
- **По команде `!подписка`:** арендатор видит актуальные лимиты аккаунта.
- **При обновлении access_token:** попутный замер (бесплатный).

Лимиты динамические — OpenAI меняет их. Замеры кэшируются в `AccountLimits`, используются в SQL-запросах выдачи.

## 7. Модель данных

Все timestamp в UTC (`timestamptz`).

### Account — ChatGPT-аккаунт в пуле

```
Account
├── id
├── login                          (email ChatGPT)
├── password_encrypted             (Fernet) — для доступа покупателей
├── totp_secret_encrypted          (Fernet, base32) — для !код и первичной валидации
├── tier_id  ────────────────→ SubscriptionTier
├── subscription_expires_at        срок ChatGPT-подписки (UTC), обновляется из backend-api
├── max_active_rentals             совместимое поле; эффективное значение не выше 1
├── status                         pending_validation | active | maintenance | banned | expired
├── chatgpt_last_check_at          последний успешный health-check
└── notes
```

`status=pending_validation` — при загрузке, до успешной проверки. Не активен, лоты под него не создаются.

### SubscriptionTier — тип подписки ChatGPT

```
SubscriptionTier
├── id
├── name                           "Plus", "Pro", "Pro x5", "Team", "Enterprise", ...
├── description
└── is_active
```

Значения соответствуют `plan_type` из backend-api.

### Duration — срок аренды

```
Duration
├── id
├── minutes                        30..43200, кратно 30
└── is_enabled                     какие сроки продавец разрешил продавать
```

Админ задаёт срок в минутах, часах или днях; каноническое значение хранится в минутах. Сроки всегда показываются по возрастанию `minutes`; ручного порядка отображения нет.

### LimitScope — тип лимита лота

```
LimitScope
├── id
├── code                           any | codex
├── name                           "Любой" | "Codex"
└── is_enabled                     доступность системного типа для новых предложений
```

Два варианта:
- `any` — без проверки конкретного лимита
- `codex` — лот гарантирует остаток единого измеримого agentic-лимита

Устаревший `chat` может временно сохраняться только выключенной tombstone-записью ради внешних ключей исторических заказов/аренд. Он скрыт из новых цен, лотов и шаблонов и никогда не участвует в подборе аккаунта.

### AccountLimits — кэш замеров и OAuth-токены

```
AccountLimits
├── account_id  ──────────────→ Account (UNIQUE)
├── refresh_token_encrypted          долгоживущий, ротация при refresh
├── access_token_encrypted           короткоживущий (~1ч)
├── access_token_expires_at          UTC
├── account_id_openai                для заголовка chatgpt-account-id
├── codex_primary_remaining_pct      0-100
├── codex_primary_window_seconds     фактическая длительность primary
├── codex_primary_resets_at          UTC
├── codex_secondary_remaining_pct    0-100, NULL если окна нет
├── codex_secondary_window_seconds   фактическая длительность secondary
├── codex_secondary_resets_at        UTC
├── plan_type                        "plus" | "pro" | ... (из backend-api)
├── plan_window_status               unknown | ok | mismatch
├── expected_long_window_seconds     ожидаемое длинное окно тарифа
├── subscription_expires_at          UTC (из backend-api)
├── measured_at                      UTC
├── refresh_status                   ok | expired | recovering | failed
├── refresh_failed_at                UTC, когда протух (NULL = ок)
├── refresh_recover_attempts         счётчик попыток перезахода
└── refresh_last_recover_at          UTC, последний успешный перезаход
```

### PriceMatrix — цена связки (4 измерения)

```
PriceMatrix
├── id
├── tier_id  ────────────────→ SubscriptionTier
├── duration_id  ────────────→ Duration
├── limit_scope_id  ─────────→ LimitScope
├── min_limit_pct              нижний порог % (для codex)
├── max_5h_pct                 compatibility-name: потолок короткого 5ч окна (для any)
├── max_weekly_pct             compatibility-name: потолок длинного окна 7/30д (для any)
├── price                      рубли (brutto)
└── UNIQUE(tier_id, duration_id, limit_scope_id, min_limit_pct, max_5h_pct, max_weekly_pct)
```

Цена растёт с требованиями: низкий потолок (any) или высокий нижний порог (codex) → дороже. Задаётся продавцом per связку.

`max_5h_pct` и `max_weekly_pct` сохранены в БД/API как legacy-имена ради бесшовной миграции. Семантика определяется не позицией `primary/secondary`, а фактическим `limit_window_seconds`: короткое окно — наблюдаемое 5-часовое (если тариф его публикует), длинное — проверенное окно плана (сейчас 30 дней для Free и 7 дней для платных). Оба относятся к одному общему agentic-пулу, отдельного Chat-пула нет.

**Пример (Plus × 7 дней):**

| scope | пороги | price | комментарий |
|---|---|---|---|
| any | оба NULL | 299₽ | полный рандом |
| any | max_5h=30%, max_weekly=10% | 349₽ | рандом среди «плохих» аккаунтов |
| codex | min=30% | 449₽ | гарантия единого agentic-лимита ≥30% |
| codex | min=50% | 599₽ | гарантия единого agentic-лимита ≥50% |
| codex | min=80% | 799₽ | гарантия единого agentic-лимита ≥80% |

### Lot — лот на FunPay (один лот = RU+EN локализации)

Поля порогов зависят от scope:
- `scope=any`: используются `max_5h_pct` + `max_weekly_pct` (верхние пороги, потолок)
  - оба NULL → полный рандом (без учёта лимитов)
  - заданы → аккаунт подходит, если соответствующие фактически наблюдаемые окна единого Codex-пула ≤ порогов
- `scope=codex`: используется `min_limit_pct` (нижний порог, гарантия)
  - аккаунт подходит, если каждое фактически наблюдаемое окно единого пула ≥ порога

```
Lot
├── id
├── funpay_id                      ID лота на FunPay (после создания)
├── funpay_node_id                 категория FunPay
├── tier_id  ────────────────→ SubscriptionTier
├── duration_id  ────────────→ Duration
├── limit_scope_id  ─────────→ LimitScope
├── min_limit_pct                  нижний порог % (для codex; NULL для any)
├── max_5h_pct                     legacy-name: потолок фактического короткого окна
├── max_weekly_pct                 legacy-name: потолок фактического длинного окна
├── price                          из PriceMatrix
├── title_ru / title_en            заголовок
├── description_ru / description_en
├── status                         active | paused | deleted
├── paused_reason                  NULL | 'auto_no_quota' | 'auto_no_account' | 'auto_low_limit' | 'manual'
└── auto_created                   true | false
└── UNIQUE(tier_id, duration_id, limit_scope_id, min_limit_pct, max_5h_pct, max_weekly_pct)
```

`paused_reason='manual'` — бот не активирует обратно. `auto_*` — бот управляет автоматически.

### Rental — аренда

```
Rental
├── id
├── order_id  ────────────────→ Order (UNIQUE — одна аренда на заказ, идемпотентность)
├── account_id  ──────────────→ Account
├── buyer_funpay_id
├── buyer_funpay_chat_id
├── tier_id  ────────────────→ SubscriptionTier
├── duration_id  ────────────→ Duration
├── limit_scope_id  ─────────→ LimitScope
├── min_limit_pct                  нижний порог лота (для codex)
├── max_5h_pct                     legacy-name: потолок фактического короткого окна
├── max_weekly_pct                 legacy-name: потолок фактического длинного окна
├── lang                           ru | en (по локали покупателя)
├── started_at                     UTC
├── expires_at                     credentials_delivered_at + duration.minutes
├── status                         active | expiry_pending | expired | refunded | revoked
├── expiry_revoke_started_at       lease/token account-wide revoke/terminal notification
├── expiry_notified_at             durable marker отправки terminal expiry-сообщения
├── replacement_target_account_id  зарезервированный target до logout старого аккаунта
├── credentials_delivery_status    sending | failed | manual | sent
├── credentials_delivered_at       UTC; до sent доступ покупателю не авторизован
├── replacement_count              сколько раз account_id был заменён на этой аренде
├── last_code_request_at           анти-спам для !код
├── issued_codex_primary_pct        срез лимита на момент выдачи
├── issued_codex_primary_window_seconds
├── issued_codex_secondary_pct
└── issued_codex_secondary_window_seconds
```

### Order — заказ FunPay

```
Order
├── id
├── funpay_order_id                UNIQUE (идемпотентность)
├── funpay_chat_id
├── buyer_funpay_id
├── buyer_locale                   ru | en
├── lot_id  ──────────────────→ Lot
├── tier_id  ────────────────→ SubscriptionTier
├── duration_id  ────────────→ Duration
├── limit_scope_id  ─────────→ LimitScope
├── min_limit_pct                  порог лота (для codex)
├── max_5h_pct                     legacy-name: потолок фактического короткого окна
├── max_weekly_pct                 legacy-name: потолок фактического длинного окна
├── price
├── status                         pending | completed | refund_pending | refunded
└── created_at
```

### LotTemplate — шаблоны текстов лотов

```
LotTemplate
├── id
├── tier_id  ────────────────→ SubscriptionTier
├── limit_scope_id  ─────────→ LimitScope (NULL = шаблон для всех scope)
├── title_template_ru              "ChatGPT {tier} — {duration} ({scope_text})"
│                                  где {scope_text}: "Без проверки лимитов" для any,
│                                  "{scope} ≥{min_pct}%" для codex,
│                                  "Короткое ≤{max_5h}%/длинное ≤{max_weekly}%" для any с потолком
├── title_template_en
├── description_template_ru        многострочный шаблон
├── description_template_en
└── переменные: {tier}, {duration}, {duration_minutes}, {price}, {scope}, {min_pct}
```

### MessageTemplate — шаблоны сообщений покупателю в чат сделки

Все сообщения бота покупателю — настраиваемые шаблоны. Хранятся отдельно от SellerSettings (не разрастается).

```
MessageTemplate
├── id
├── key                    идентификатор шаблона (enum)
├── lang                   ru | en
├── content                текст с переменными {var}
├── updated_at
└── UNIQUE(key, lang)
```

Полный перечень ключей и переменных:

| key | Когда отправляется | Переменные |
|---|---|---|
| `welcome` | выдача данных после оплаты | {tier}, {duration}, {duration_minutes}, {login}, {password}, {expires_at}, {codex_primary_limit}, {codex_primary_window}, {codex_primary_reset}, {codex_secondary_limit}, {codex_secondary_window}, {codex_secondary_reset} |
| `code_success` | `!код`, аренда active | {code}, {expires_in} |
| `code_expiring` | `!код`, до конца доступа менее минуты | — |
| `account_unavailable` | `!код`, аккаунт остановлен оператором или на обслуживании | — |
| `delivery_pending` | команда покупателя до успешной доставки данных | — |
| `code_expired` | `!код`, аренда expired | — |
| `rental_ambiguous` | у покупателя несколько подходящих аренд | — |
| `code_rate_limited` | `!код`, анти-спам | {retry_in_sec} |
| `email_code_success` | получен новый одноразовый код из почты | {email_code} |
| `email_code_duplicate` | этот код из почты уже был отправлен | — |
| `email_code_not_found` | новое письмо с кодом ещё не найдено | — |
| `email_code_unavailable` | автоматическое чтение почты недоступно | — |
| `subscription` | `!подписка` | {tier}, {expires_at}, {access_expires_at}, {expires_in}, {codex_primary_limit}, {codex_primary_window}, {codex_primary_reset}, {codex_secondary_limit}, {codex_secondary_window}, {codex_secondary_reset} |
| `subscription_limits_unavailable` | `!подписка`, свежий замер не подтверждён | {tier}, {expires_at}, {access_expires_at}, {expires_in} |
| `replace_success` | `!замена` успешна | {login}, {password}, {tier}, {duration}, {duration_minutes}, {expires_at}, {access_expires_at}, {codex_primary_limit}, {codex_primary_window}, {codex_primary_reset}, {codex_secondary_limit}, {codex_secondary_window}, {codex_secondary_reset} |
| `replace_declined` | `!замена`, аккаунт работает | — |
| `replace_expiring` | для безопасной замены осталось менее двух минут | — |
| `replace_no_account` | `!замена`, нет свободного аккаунта | — |
| `seller_called` | `!продавец`, ответ покупателю | — |
| `help` | `!помощь` | — |
| `order_confirmed` | покупатель подтвердил сделку | — |
| `expiry` | истечение аренды | {tier}, {duration}, {duration_minutes} |
| `disconnect` | временный кик (logout all) | {expires_in} |
| `no_account_available` | нет аккаунта при заказе | {retry_minutes} |

Дефолтные значения зшиты в код, заполняют таблицу при первичной инициализации (миграция). Продавец редактирует через админку.

**Примеры RU:**

```
welcome:
  ✅ Заказ выполнен. ChatGPT {tier} на {duration}:
  Логин: {login}
  Пароль: {password}
  Тариф аккаунта до: {expires_at}
  Отсчёт доступа начнётся только после успешной доставки.
  📊 Codex: {codex_primary_window} — {codex_primary_limit}, сброс {codex_primary_reset}.
  📊 Доп. окно: {codex_secondary_window} — {codex_secondary_limit}, сброс {codex_secondary_reset}.
  ⚠️ Это общий agentic-лимит аккаунта; обычный Chat в него не входит.
  📱 Для входа: !код | Помощь: !помощь | Замена: !замена

code_success:
  🔑 Ваш код: {code}
  ⏱ Код обновляется каждые 30 секунд — используйте его сразу.
  Подписка активна ещё: {expires_in}

code_expired:
  ❌ Доступ закончился. Для продления — новый заказ.

replace_success:
  🔄 Замена выполнена. Новые данные:
  Логин: {login}
  Пароль: {password}
  ChatGPT {tier}, доступ ещё на {duration}.
  Тариф аккаунта до: {expires_at}. Доступ покупателя до: {access_expires_at}.
  📊 Codex: {codex_primary_window} — {codex_primary_limit}.
  📱 Для кода входа: !код

replace_declined:
  ✅ Аккаунт работает корректно.
  Уточните проблему: !продавец

order_confirmed:
  🙏 Спасибо за покупку! Помощь: !помощь

seller_called:
  📢 Продавец уведомлён. Ожидайте ответа.

disconnect:
  ⚠️ Временное отключение. Подписка активна ещё: {expires_in}.
  Для повторного входа: !код

expiry:
  ⏰ Ваш доступ ({tier}, {duration}) закончился.
  Для продления — новый заказ.

no_account_available:
  ⏳ Нет свободных аккаунтов. Ожидайте до {retry_minutes} мин.
```

Каждый ключ имеет RU и EN версию. Язык выбора — по `Rental.lang`. `{expires_at}` всегда означает срок тарифа самого аккаунта (`Без срока` для Free), а `{access_expires_at}` — отдельный дедлайн доступа покупателя. Для первичной выдачи абсолютный дедлайн нельзя обещать заранее: оплаченный срок начинается от фактического успешного `send_message`; при замене `{duration}` показывает точный оставшийся срок исходной аренды.

### AccountCheckJob — очередь проверок аккаунтов

```
AccountCheckJob
├── id
├── account_id  ──────────────→ Account
├── priority                       new | refresh_recover | manual | scheduled | limit_check
├── job_type                       full_validation | refresh_recover | limit_check
├── status                         pending | running | done | failed
├── created_at
├── started_at
├── finished_at
├── result                         ok | invalid_credentials | expired_sub | banned |
│                                  totp_fail | refresh_fail | refresh_recovered
└── error                          текст ошибки при failed
```

`new` и `refresh_recover` — высший приоритет (Playwright OAuth flow). `limit_check` — HTTP-замер, быстро.

### BumpLog, AuditLog — без изменений

```
BumpLog: id, lot_id, bumped_at, success, error
AuditLog: id, timestamp, event_type, account_id?, order_id?, rental_id?, chat_id?, message_text?, metadata (JSONB)
```

Очистка AuditLog по возрасту (настраивается, по умолчанию 90 дней).

### SellerSettings

```
SellerSettings
├── funpay_session_key             golden_key
├── funpay_session_valid           boolean (мониторинг протухания)
├── funpay_node_id                 категория для авто-лотов
├── telegram_bot_token
├── telegram_seller_chat_id
├── check_interval_minutes         интервал полного health-check (минимум 10)
├── limits_check_interval_minutes  интервал замеров лимитов (5-10)
├── refresh_recover_concurrency    воркеров перезахода Playwright (2-3)
├── refresh_max_attempts           максимум попыток перезахода (3)
├── refresh_retry_delay_minutes    пауза между попытками (5)
├── check_delay_seconds            пауза между Playwright-проверками
├── bump_interval_hours            кулдаун бесплатного поднятия лотов
├── auto_bump_enabled              boolean
├── default_max_active_rentals     совместимое поле; API принимает только 1
├── funpay_commission_percent      для расчёта netto (по умолчанию 15)
├── limits_warn_threshold_pct      порог уведомления продавцу о низких лимитах
└── admin_password_hash            (bcrypt)
```

## 8. Система команд (RU + EN)

Команды работают в чатах сделок FunPay. Матч по префиксу `!` + алиас (case-insensitive). Язык ответа — по `Rental.lang`.

| Назначение | RU | EN | Поведение |
|---|---|---|---|
| Получить 2FA-код | `!код` | `!code` | Проверка активной аренды → генерация TOTP → отправка. Истёкшим — отказ. Анти-спам: 1 код / 30 сек. |
| Проверить подписку и лимиты | `!подписка` | `!sub` | Показывает аккаунт, тариф, остаток времени и фактические окна единого лимита Codex. |
| Замена аккаунта | `!замена` | `!replace` | См. «Механика `!замена`» ниже. Автозамена только при бане/слете тарифа/неверных данных; иначе — вызов продавца. |

### Механика `!замена`

Бот логинится в проблемный аккаунт через Playwright и проверяет три условия:
1. **Логин работает** (login:pass + TOTP валидны)
2. **Аккаунт не забанен** (страница не показывает бан)
3. **Тариф активен** (`subscription_expires_at > NOW()`, tier совпадает с заявленным)

Если **хоть одно не выполняется** → автоматическая замена:
- Сначала в короткой транзакции подбирается новый аккаунт по тем же правилам и закрепляется в `replacement_target_account_id`. Зарезервированный target сразу исключается из обычной выдачи, автолотов и фоновых проверок
- Если подходящего target нет, старый аккаунт не отключается: покупатель получает редактируемое сообщение `replace_no_account`, продавец — уведомление
- Только после durable-резерва выполняется подтверждённый logout старого аккаунта. Затем та же `Rental` атомарно переключается на target, `replacement_count` увеличивается, а резерв очищается
- Новый аккаунт подбирается по тем же tier/scope/порогам и обязан покрывать только реальный остаток исходного срока; абсолютный `expires_at` не продлевается
- Welcome-сообщение с новыми данными
- Уведомление продавцу в Telegram «Замена: причина X, старый аккаунт Y → новый Z»

Незавершённый резерв замены старше пяти минут безопасно освобождается scheduler-ом, если аренда уже не находится в операции замены. Старый аккаунт при таком сбое остаётся в `maintenance` до явного успешного восстановления: он не возвращается в пул автоматически без доказанного состояния сессий.

Если **всё работает корректно** → отказ: «Аккаунт работает. Опишите проблему подробнее командой `!продавец`».

Автоматически выполняется не более одной замены на аренду; повторный инцидент переводится продавцу, чтобы исключить цепочку бесконтрольной выдачи аккаунтов. Все запросы и решения логируются в AuditLog.
| Вызвать продавца | `!продавец` | `!seller` | Бот пишет покупателю «Продавец уведомлён, ожидайте» и отправляет уведомление в Telegram. При недоступности Telegram — повтор с backoff, покупателю всё равно «уведомлён». |
| Помощь | `!помощь` | `!help` | Список команд на языке аренды. |

**Безопасность:** код выдаётся строго по `chat_id` аренды. Покупатель не может получить код чужого аккаунта — логин в команде игнорируется, привязка по чату сделки.

`!код` генерируется локально через pyotp от `totp_secret_encrypted` аккаунта. На первой и последней границе проверяются `Rental.status`, дедлайн, успешная доставка данных и доступность аккаунта. При `maintenance`, ручной паузе, возврате или оставшейся минуте код не раскрывается; покупателю предлагается безопасная замена/обращение к продавцу.

`!подписка` дополнительно триггерит ограниченный по времени свежий замер лимитов аккаунта через backend-api (on-demand), не удерживая блокировку аренды во время сети. После замера аренда и account_id проверяются повторно. Если свежесть не подтверждена, старые проценты не выдаются за актуальные: используется отдельный редактируемый шаблон `subscription_limits_unavailable` без значений лимита.

## 9. Жизненный цикл аренды

```
[Заказ оплачен на FunPay] → NEW_ORDER
    │
    ▼
1. Создать Order (идемпотентно по funpay_order_id)
   определить lot → tier/duration/scope/пороги, lang
   язык (buyer_locale → Rental.lang):
     - если FunPay передаёт локаль покупателя в событии — используем её
     - fallback: ru (русский по умолчанию)
    │
    ▼
2. Свежий замер лимитов потенциальных аккаунтов (on-demand)
   AccountPool.acquire(tier, duration, scope, пороги):
     base_filter:
       Account.status = 'active'
       Account.tier_id = tier.id
       Account.subscription_expires_at >= NOW() + duration.minutes + delivery_retry_headroom
       active_rentals_count < эффективный_лимит(account)
       AccountLimits.measured_at >= NOW() - freshness(duration)
         где freshness = min(60 мин, max(5 мин, duration / 2))
       AccountLimits.refresh_status = 'ok'
     
     if scope == 'any':
       # потолок: если пороги заданы — наблюдаемые agentic-окна ≤ порогов
       if max_5h_pct is not None:
         filter: короткое окно Codex ≤ max_5h_pct
       if max_weekly_pct is not None:
         filter: длинное окно Codex ≤ max_weekly_pct
       ORDER BY subscription_expires_at ASC   # FIFO по сроку подписки
     else:  # codex
       # гарантия: каждое фактически наблюдаемое окно ≥ min_limit_pct
       filter: primary >= min_limit_pct AND (secondary IS NULL OR secondary >= min_limit_pct)
       ORDER BY минимум_наблюдаемых_окон DESC  # наибольший запас
    │
    ├── нет аккаунта/ошибка доставки → не более 6 попыток с паузами
    │   1, 2, 4, 8 и 16 мин; каждая отправка ограничена 30 сек
    │   (в подборе заранее резервируется 40 мин headroom) → уведомление продавцу
    │
    ▼
3. Зарезервировать Rental для доставки данных.
   До успешной доставки она занимает capacity, но не авторизует !код/!подписка/!замена.
   До первой внешней попытки provisional timestamp не считается сроком покупателя.
   сохранить issued_*_pct (срез лимитов на момент выдачи)
    │
    ▼
4. Отправить в чат сделки welcome_message:
   рендеринг MessageTemplate['welcome'] с подстановкой переменных.
   Шаблон — в секции «MessageTemplate» (title/description/лимиты/инструкции).
    │
    ▼
5. Перед каждой попыткой доставки повторно проверить подписку, лимиты и доступность аккаунта.
   До сетевого send зафиксировать durable-границу возможного раскрытия: timeout/crash не
   означает, что FunPay не принял сообщение. После этой границы account_id неизменяем,
   а консервативный дедлайн гарантирует последующий revoke даже без локального ack.
   После успешной доставки установить started_at=delivered_at,
   expires_at=delivered_at+duration.minutes и только затем авторизовать команды.
   OTP не выдаётся, если до expires_at осталось меньше 60 секунд.
   Доставка данных через чат: бот отправляет welcome_message (логин/пароль/2FA-инструкции)
   FunPay НЕ имеет API complete_order() — подтверждение сделки = действие покупателя
   Order.status остаётся pending до SaleClosedEvent
   → триггер LotAutoManager (пересчёт квоты и лимитов, возможное снятие лотов)
    │
    ▼
[Активная аренда — пользователь пользуется]
   команды: !код / !подписка / !замена / !продавец / !помощь
    │
    ▼ (Scheduler, проверка истечений каждые 30 сек)
6. Rental.expires_at <= NOW() → status = expiry_pending; команды покупателя сразу запрещены
    │
    ▼
7. Короткая транзакция Order → Rental → Account фиксирует `expiry_pending`,
   account=maintenance и точный revoke lease/token; затем commit до браузерного I/O.
   KickService.kick(account):
   - PostgreSQL advisory lock сериализует logout одного аккаунта между процессами/контейнерами
   - browser operation имеет таймаут 240 сек, то есть меньше 5-минутного lease
   - Playwright: логин → Settings → «Log out everywhere»
   - Пароль НЕ меняем
   - После успешного logout создаётся `refresh_recover` job
    │
    ▼
8. Короткая финализация повторно проверяет Order/Rental/account/token. Только после
   успешного logout: status = expired. При ошибке остаётся expiry_pending и scheduler
   повторяет revoke. Отправка MessageTemplate['expiry'] имеет отдельный durable marker
   `expiry_notified_at`, 30-секундный timeout и повторяется после падения/ошибки;
   исторические terminal Rental при миграции помечаются уже обработанными.
    │
    ▼
9. Уведомление продавцу в Telegram (опционально)
   → триггер LotAutoManager (слот освободился)
```

### Эффективный лимит аккаунта

```
эффективный_лимит(account) = 1
```

Даже если историческая настройка содержит большее число, подбор аккаунта жёстко ограничивает capacity единицей. Увеличивать лимит можно только после появления действительно изолированных per-rental credentials и независимого механизма отзыва доступа без глобального logout.

### Защита доступа после истечения

| Кто пытается войти после logout all | Логин:пароль | 2FA-код | Результат |
|---|---|---|---|
| Истёкший арендатор | знает | бот отказывает (аренда expired) | не входит |

## 10. Управление лотами

### Авто-создание лотов (LotAutoManager)

Триггеры: событийные (выдача/истечение аренды, `!замена`, изменение пула/лимитов/замер) + периодические (раз в 1–2 мин, перестраховка).

```
for tier in SubscriptionTier WHERE is_active:
    for duration in Duration WHERE is_enabled:
        for (scope, пороги, price) in PriceMatrix WHERE tier, duration:
            has_capacity = EXISTS account WHERE
                base_filter(tier, duration)
                AND AccountLimits.measured_at >= NOW() - freshness(duration)
                AND (
                    # scope=any: потолок (если задан) — наблюдаемые окна ≤ порогов
                    (scope == 'any' AND max_пороги is NULL)
                    OR (scope == 'any'
                        AND codex_short ≤ max_5h
                        AND codex_long ≤ max_weekly)
                    # scope=codex: гарантия каждого наблюдаемого окна
                    OR (scope == 'codex'
                        AND primary >= min_limit_pct
                        AND (secondary IS NULL OR secondary >= min_limit_pct))
                )
            
            manage_lot(tier, duration, scope, пороги, price, has_capacity)
```

Каждая связка управляется **независимо**. Примеры:
- Plus × 7дн × codex ≥50% снимется, когда хотя бы одно фактически наблюдаемое окно единого agentic-пула на каждом Plus-аккаунте ниже 50%.
- Plus × 7дн × any (полный рандом) останется активным, пока есть аккаунт с нужным сроком.
- Plus × 7дн × any (max_5h=30%, max_weekly=10%) снимется, когда все аккаунты имеют хотя бы один замер выше потолка.

### Генерация текстов лотов

Из LotTemplate с подстановкой `{tier} {duration} {duration_minutes} {price} {scope} {min_pct}`.

**Пример title:** `ChatGPT Plus — 7 дн. (Codex ≥50%)`
**Пример description_template_ru:**
```
🎮 Доступ к ChatGPT {tier} на {duration}.

✅ Гарантированный лимит {scope}: не менее {min_pct}%
✅ Мгновенная выдача после оплаты
✅ Полноценный аккаунт, без ограничений
✅ Поддержка 24/7 в чате сделки

После оплаты: логин + пароль + команда !код для 2FA.
Остаток лимитов: !подписка. Замена при проблемах: !замена.
```

### Bump лотов

`funpaybotengine.raise_offers(category_id, *subcategory_ids)` бампит **категорию/подкатегорию** целиком (а не отдельный лот). Бесплатный bump имеет кулдаун на FunPay.

```
Scheduler, BumpService (если auto_bump_enabled):
for subcategory_id in distinct(Lot.funpay_node_id WHERE status = 'active'):
    last_bump = BumpLog.last_successful_for(subcategory)
    if last_bump is None or NOW() - last_bump >= bump_interval_hours:
        result = gateway.bump_category(category_id, subcategory_id)
        BumpLog.create(subcategory, result)
        if not result.ok: уведомление продавцу
```

Равномерное распределение: подкатегории поднимаются по очереди с интервалом, не разом. Один bump_category вызов поднимает все лоты продавца в подкатегории.

## 11. Обработка заказов FunPay

### События funpaybotengine

События приходят через WebSocket (Runner). Регистрация хэндлеров: `@dp.on_new_sale()`, `@dp.on_sale_closed()`, `@dp.on_sale_refunded()`, `@dp.on_new_message()`.

```
NewSaleEvent (новый оплаченный заказ)
├── OrderProcessor.process_new_sale(): создать Order (идемпотентно по funpay_order_id)
├── gateway.get_order(): определить subcategory_id → Lot matching (tier/duration/scope)
├── lang по buyer_locale
└── запустить жизненный цикл аренды (Фаза 4: AccountPool.acquire + Rental)

SaleClosedEvent (покупатель подтвердил получение)
├── OrderProcessor.process_sale_closed(): Order.status = completed
└── «Спасибо за покупку» (Фаза 4: MessageTemplate['thanks'])

SaleRefundedEvent (возврат / спор разрешён в пользу покупателя)
├── OrderProcessor.process_sale_refunded(): Order.status = refunded
├── Rental → revoked (Фаза 4)
└── кик покупателя (Фаза 4: KickService)

NewMessageEvent (в чате сделки)
├── CommandParser.parse(text) → ParsedCommand | None
├── команда распознана → CommandRouter.dispatch(ctx) → хэндлер
└── обычное сообщение → игнор
```

### Статусы FunPay

`funpayparsers.OrderStatus`: `PAID` (оплачен, ждёт подтверждения покупателем), `COMPLETED` (завершён), `REFUNDED` (возврат). Нет отдельного `dispute`/`cancelled` — спор на FunPay эскалируется в возврат.

### Идемпотентность

- `Order.funpay_order_id` — UNIQUE constraint.
- Каждое сообщение логируется с `message_id` → обработка только новых.
- `!код` — проверка `last_code_request_at`, отказ если < 30 сек назад.

### Доставка и завершение сделки

FunPay НЕ имеет API `complete_order()` — подтверждение сделки это действие покупателя. Продавец только доставляет данные (логин/пароль/2FA) через чат после выдачи аренды. Покупатель подтверждает → деньги продавцу (`SaleClosedEvent`). Если не подтверждает → поддержка FunPay подтверждает за него через 24–48ч при наличии лога в чате (вся выдача и команды пишутся в `AuditLog`).

## 12. Очередь проверок аккаунтов

### Триггеры постановки в очередь

| Триггер | Приоритет | job_type | Что проверяет |
|---|---|---|---|
| Добавление аккаунта | `new` | `full_validation` | Playwright OAuth flow → refresh_token + лимиты + подписка |
| Протухание refresh_token | `refresh_recover` | `refresh_recover` | Playwright перезаход → новые токены |
| Кнопка «Проверить сейчас» | `manual` | `full_validation` | Лимиты + подписка + валидность |
| Scheduler, раз в `check_interval` | `scheduled` | `full_validation` | Полная проверка для аккаунтов с устаревшим `last_check_at` |
| Scheduler, раз в `limits_check_interval` | `limit_check` | `limit_check` | Только замер лимитов (быстро, HTTP) |
| Перед выдачей аренды | `limit_check` | `limit_check` | Свежий замер лимитов кандидатов |
| `!замена` с проблемой | `manual` | `full_validation` | Полная проверка перед выдачей замены |

Приоритеты (высший → низший): `new` = `refresh_recover` > `manual` > `scheduled` > `limit_check`.

Дедупликация: не дублировать pending/running job того же `job_type` для аккаунта. Высший приоритет перебивает низший.

Проверка и выдача одного аккаунта взаимоисключаются на уровне БД. Producer берёт блокировку `Account` перед созданием job, worker выбирает пару `Account → AccountCheckJob` через `FOR UPDATE ... SKIP LOCKED`, а все пути расчёта capacity исключают аккаунты с job в `pending`/`running`, активной арендой или `replacement_target_account_id`. Поэтому аккаунт нельзя одновременно измерять, выдавать и резервировать для замены даже в нескольких процессах.

### Воркер-пулы (два раздельных)

**Пул перезаходов (Playwright)** — `refresh_recover_concurrency` воркеров (по умолчанию 2-3):
```
job = DB.fetch_next_pending_job(job_type IN ('full_validation', 'refresh_recover'))
if None: sleep(5s); continue
job.status = running
result = await playwright_oauth_flow(account)  # логин + OAuth → токены
if result.ok:
    measure_limits_and_subscription(account)   # HTTP-замер после получения токенов
update_account_and_limits(account, result)
await sleep(check_delay_seconds)                # анти-спам пауза
```

**Пул HTTP-замеров** — отдельный воркер (легче, без Playwright):
```
job = DB.fetch_next_pending_job(job_type='limit_check')
if None: sleep(2s); continue
job.status = running
result = await http_measure(account)            # обновление access_token + замер
update_limits(account, result)
# без паузы — HTTP-запросы лёгкие
```

При `refresh_recover_concurrency=3`, `check_delay_seconds=45`: 50 аккаунтов через Playwright ≈ 13 мин. HTTP-замеры — десятки ms на аккаунт, пауз не требуют.

### Массовая загрузка

CSV: `login,pass,totp,tier_id,expires_at`. Все → `status=pending_validation`, jobs с priority=`new`. Обрабатываются по очереди. Прогресс в админке: «Проверено X/50, активно Y, с ошибками Z».

**Валидация CSV перед постановкой в очередь:**
- обязательные поля: `login`, `pass`, `totp`, `tier_id` (`expires_at` опционально — бот замерит через backend-api)
- `tier_id` должен существовать в SubscriptionTier
- `totp` — валидный base32 (проверка формата)
- дубликаты `login` в файле и в БД — игнорируются с пометкой
- отчёт по строкам: «Строка N: пропущена (причина)» / «Строка N: принята»

## 13. Изоляция Playwright-операций

Каждый запуск Playwright — в свежем incognito-контексте. После операции контекст закрывается → cookies уничтожаются.

| Операция | Что делает | Logout all? |
|---|---|---|
| Первичная валидация | OAuth flow → refresh_token | никогда |
| KickService | Логин → Settings → «Log out everywhere» → закрытие | только здесь |
| LoginProbe | Генерация TOTP (без логина) | — |
| ReplaceService | Логин в проблемный аккаунт → проверка → закрытие | никогда |
| Refresh-восстановление | OAuth flow при протухшем refresh_token (fallback) | никогда |

**Защита:** валидация и замена не посещают страницу настроек — физически не могут нажать logout. KickService — единственный, кто целенаправленно идёт в logout everywhere.

## 14. Шифрование секретов

`password_encrypted`, `totp_secret_encrypted`, `refresh_token_encrypted`, `access_token_encrypted` — Fernet. Ключ `ENCRYPTION_KEY` в `.env`, не в БД/коде.

## 15. Админ-панель (React SPA)

### Аутентификация

Логин/пароль (один продавец), bcrypt-хеш. JWT в httpOnly cookie. Сессия с TTL.

### Разделы

**Дашборд:** метрики (активных аренд, свободных аккаунтов, заказов сегодня, выручка brutto/netto), последние события, статус бота (FunPay connected, Scheduler running, last error), **график лимитов по аккаунтам**.

**Аккаунты (пул):** компактная таблица (login, tier, единый лимит Codex с фактическими окнами/сбросами, expires_at, active/max rentals, status, действия-иконки), добавить (форма), массовая загрузка CSV, действия (edit, pause, delete, проверить сейчас), лимиты (глобальный + per-account override). Отдельный Chat-лимит не отображается.

**Тарифы (SubscriptionTier):** CRUD.

**Сроки (Duration):** список уникальных периодов от 30 минут до 30 дней с шагом 30 минут, ввод в минутах/часах/днях, автоматическая сортировка по продолжительности, включение/выключение и безопасное удаление неиспользуемых сроков.

**Лимиты (LimitScope):** системные any/codex — только включение/выключение использования в новых предложениях. Коды и названия не редактируются, пороговые проценты задаются в матрице цен. Legacy chat скрыт и fail-closed.

**Цены (PriceMatrix):** 4-мерная матрица (tier × duration × scope × пороги), inline-редактирование. Для scope=any — необязательные потолки фактического короткого и длинного окон (в API сохранены compatibility-имена `max_5h`/`max_weekly`), для codex — один минимальный остаток для всех наблюдаемых окон. Цена растёт с требованиями.

**Шаблоны лотов (LotTemplate):** title/description для RU/EN с переменными.

**Шаблоны сообщений (MessageTemplate):** таблица key × lang (RU/EN), полноценное редактирование всех сценариев выдачи, входа/кодов, поддержки, замены и жизненного цикла. Включены отдельные сообщения для ожидающей доставки, недоступного аккаунта, слишком поздней замены и всех состояний кода из почты. Предпросмотр использует те же разрешённые переменные, что и серверный рендер.

**Категории FunPay:** указать `funpay_node_id`.

**Лоты:** таблица (tier, duration, scope, min_pct, price, status, funpay_id), авто-созданные read-only, ручные edit/delete, действия (pause, activate, delete, bump).

**Заказы / Аренды:** как в rev.1.

**Настройки:** FunPay (golden_key + индикатор валидности, node_id), Telegram, Bump, Health-check, **Limits-check (интервал, порог уведомлений)**, комиссия, смена пароля. (Шаблоны лотов и сообщений — отдельные разделы.)

### API-дизайн (FastAPI)

```
POST   /api/auth/login
GET    /api/metrics
GET    /api/accounts | POST | POST /bulk | POST /{id}/check | PATCH /{id} | DELETE /{id}
GET    /api/tiers | POST | PATCH | DELETE
GET    /api/durations | POST | PATCH | DELETE /{id}
GET    /api/limit-scopes | PATCH
GET    /api/prices | PUT (matrix update)
GET    /api/templates | PUT                         шаблоны лотов (LotTemplate)
GET    /api/message-templates | PUT                 шаблоны сообщений (MessageTemplate)
GET    /api/lots | POST (manual) | PATCH /{id} | DELETE /{id} | POST /{id}/bump
GET    /api/orders | GET /{id}
GET    /api/rentals | PATCH /{id}
GET    /api/settings | PUT
```

## 16. Telegram-уведомления продавцу

Микро-бот (только отправка в `seller_chat_id`):

- «🆕 Новый заказ #123: Plus × 7дн × Codex ≥50%, 599₽»
- «✅ Заказ #123 подтверждён»
- «⚠️ СПОР по заказу #123!»
- «🔄 Замена: покупатель X запросил замену аккаунта Y»
- «⏰ Аренда истекла: аккаунт X, освободился слот»
- «🔴 Аккаунт X недоступен (бан/невалидный 2FA/refresh протух)»
- «📊 Лимит Codex аккаунта X упал ниже порога (длинное окно: 18%)»
- «📢 Покупатель X вызывает продавца в чат Z»
- «❌ Bump лота не удался»
- «🔴 FunPay дисконнект / golden_key протух»

## 17. Мониторинг golden_key и токенов

- **golden_key:** периодический HTTP-запрос к FunPay, проверка 401/403. При протухании → стоп FunPay-операций + уведомление + индикатор.
- **refresh_token:** при ошибке refresh (`refresh_failed_at`) → аккаунт → `maintenance`, уведомление продавцу, попытка восстановления через Playwright OAuth flow.

## 18. Развёртывание (Linux)

### Требования к серверу

- **OS:** Ubuntu 22.04+ / Debian 12+
- **RAM:** 2 ГБ минимум (Chromium ~500 МБ на контекст + Postgres + FastAPI)
- **Диск:** 10 ГБ
- **CPU:** 2 ядра

### Состав

```
backend/    Python 3.11+, FastAPI, SQLAlchemy, asyncpg, pyotp, httpx, playwright, cryptography
frontend/   React + TS + Vite → статик
postgres/   БД
```

### Запуск

Один процесс: FastAPI (uvicorn) обслуживает `/api` и раздаёт `frontend/dist`. В том же event loop: FunPay Engine, Scheduler. Playwright — по требованию.

Оркестрация: systemd unit или docker-compose.

### Секреты (.env)

```
DATABASE_URL
ENCRYPTION_KEY
SECRET_KEY (JWT)
ADMIN_PASSWORD_HASH
FUNPAY_SESSION_KEY
TELEGRAM_BOT_TOKEN
TELEGRAM_SELLER_CHAT_ID
```

### Backup

Periodic `pg_dump` по cron, хранение N дней.

## 19. Обработка ошибок и устойчивость

| Сбой | Поведение |
|---|---|
| FunPay WebSocket отвалился | Авто-реконнект с backoff, уведомление, лоты не снимаются |
| FunPay HTTP 429/403 | Backoff, пауза операций, уведомление |
| golden_key протух | Стоп FunPay-операций, индикатор, уведомление |
| refresh_token протух | `refresh_status=expired`, аккаунт → maintenance, job `refresh_recover` → Playwright перезаход; при успехе → active, при провале после max попыток → banned + уведомление |
| backend-api 401 (access_token) | Обновить через refresh_token, повторить запрос |
| backend-api 429 | Backoff, отложить замер |
| Playwright не смог залогиниться | Аккаунт → maintenance, уведомление, снять лоты tier |
| PostgreSQL недоступна | Стоп операций, ждёт восстановления |
| Истечение аренды во время сбоя Playwright | Retry кика с backoff; кик догоняет |
| Двойное событие заказа | Идемпотентность по `funpay_order_id` (UNIQUE) |
| Нет аккаунта при заказе | Retry 1/мин, таймаут 30 мин → уведомление |
| Замер лимитов устарел | Свежий on-demand замер перед выдачей |

## 20. Границы проекта (out of scope)

- Продажа API-доступа к ChatGPT (заглушка на будущее).
- Мост Telegram→FunPay для прямой переписки.
- Мультиязычность кроме RU/EN.
- Мультиселлерность.
- Платный bump лотов.

## 21. Риски

1. **funpaybotengine (15⭐, v0.7.0):** проверен по исходникам. API: `Bot(golden_key)`, `send_message`, `get_order_page`, `save_offer_fields` (create/update отдельного лота через `active=True/False`), `set_offer_active`, `get_my_offers_page`, `raise_offers(category_id, *subcategory_ids)` (bump категории), `listen_events(dp)`. События: `NewSaleEvent`/`SaleClosedEvent`/`SaleRefundedEvent`/`NewMessageEvent`. **Нет `complete_order()`** (подтверждение — действие покупателя). Изолирован за `ChatGateway` Protocol. Пауза/активация лота через `save_offer_fields` с `active=False/True` (НЕ `set_offers_hidden` — это глобальный переключатель). Риск: `save_offer_fields` не возвращает ID созданного лота — требуется `get_my_offers_page` для поиска по title после создания.
2. **Неофициальный backend-api OpenAI:** эндпоинты `/backend-api/wham/usage` и `/accounts/check/...` не документированы, могут меняться. Митигация: изоляция за `OpenAIClient`, обработка ошибок, fallback на Playwright-замеры.
3. **Ротация refresh_token при logout all:** logout all инвалидирует refresh_token — штатный сценарий при кике. Обрабатывается авто-перезаходом через Playwright OAuth flow (отдельный пул воркеров, до `refresh_max_attempts` попыток). Нагрузка: при активной аренде десятки перезаходов в час — требует достаточного `refresh_recover_concurrency`.
4. **Триггеры антибот-защиты OpenAI:** массовые логины/замеры с одного IP. Митигация: ограничение `check_concurrency`, паузы, при необходимости — прокси. HTTP-замеры легче Playwright.
5. **2FA как единственный барьер:** митигация — обязательное наличие 2FA при загрузке аккаунта, валидация при каждом `!код`.
6. **Споры на FunPay:** митигация — полный лог в чате сделки (AuditLog).
7. **Глобальный logout:** OpenAI отзывает сессии всего аккаунта, а не одного пользователя. Поэтому одновременная аренда одного аккаунта нескольким покупателям запрещена инвариантом capacity=1.
