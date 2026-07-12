# Дизайн: Бот аренды ChatGPT-аккаунтов через FunPay

**Дата:** 2026-07-11
**Статус:** Draft (rev. 2 — добавлены лимиты и OAuth-замеры)

## 1. Назначение

Автоматизация продажи доступа к ChatGPT-аккаунтам на площадке FunPay. Продавец загружает пул аккаунтов в админ-панель, бот автоматически создаёт лоты, выдаёт данные покупателям по оплате, управляет 2FA-кодами, контролирует срок доступа и лимиты, выбивает пользователей по истечении.

## 2. Бизнес-модель

- **Shared-модель аренды:** один аккаунт ChatGPT = несколько одновременных арендаторов (до `max_active_rentals`).
- **2FA как барьер доступа:** TOTP-код требуется для входа покупателем. Бот выдаёт код только активным арендаторам; истёкшим — отказывает.
- **Кик по таймеру:** при истечении аренды бот выполняет «Log out everywhere» через Playwright — рефреш-токен инвалидируется, для повторного входа требуется 2FA-код. Пароль НЕ меняется (общий для всех арендаторов).
- **Тарифы:** продаются связки «тип подписки × срок × лимит × порог лимита» (Plus × 7 дней × Codex ≥50%, и т.д.).
- **Лимиты общие:** лимиты ChatGPT/Codex (5h, weekly) разделяются между всеми арендаторами аккаунта. Бот замеряет их регулярно и снимает лоты, когда остаток ниже порога.

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
- `rate_limit.primary_window`: {`used_percent`, `limit_window_seconds`, `reset_at`} — 5-часовое окно
- `rate_limit.secondary_window`: те же поля — недельное окно
- `plan_type`: "plus" | "pro" | ...
- `credits`: остаток кредитов

**Остаток %** = `100 - used_percent`. Четыре значения: chat_5h, chat_weekly, codex_5h, codex_weekly (замеры для чата и Codex могут различаться — уточняется по поведению API; если одинаковы — дублируем).

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
├── max_active_rentals             индивидуальный лимит (NULL = глобальный)
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
├── days                           1, 3, 5, 7, 15, 30 (любое 1-30, на усмотрение продавца)
├── is_enabled                     какие сроки продавец разрешил продавать
└── sort_order
```

### LimitScope — тип лимита лота

```
LimitScope
├── id
├── code                           any | chat | codex
└── name                           "Любой" | "Чат (GPT-5)" | "Codex"
```

Три варианта:
- `any` — без проверки конкретного лимита
- `chat` — лот гарантирует лимит на чат (GPT-5)
- `codex` — лот гарантирует лимит на Codex

### AccountLimits — кэш замеров и OAuth-токены

```
AccountLimits
├── account_id  ──────────────→ Account (UNIQUE)
├── refresh_token_encrypted          долгоживущий, ротация при refresh
├── access_token_encrypted           короткоживущий (~1ч)
├── access_token_expires_at          UTC
├── account_id_openai                для заголовка chatgpt-account-id
├── chat_5h_remaining_pct            0-100
├── chat_weekly_remaining_pct        0-100
├── codex_5h_remaining_pct           0-100
├── codex_weekly_remaining_pct       0-100
├── plan_type                        "plus" | "pro" | ... (из backend-api)
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
├── min_limit_pct              нижний порог % (для chat/codex)
├── max_5h_pct                 верхний порог 5ч % (для any)
├── max_weekly_pct             верхний порог weekly % (для any)
├── price                      рубли (brutto)
└── UNIQUE(tier_id, duration_id, limit_scope_id, min_limit_pct, max_5h_pct, max_weekly_pct)
```

Цена растёт с требованиями: низкий потолок (any) или высокий нижний порог (chat/codex) → дороже. Задаётся продавцом per связку.

**Пример (Plus × 7 дней):**

| scope | пороги | price | комментарий |
|---|---|---|---|
| any | оба NULL | 299₽ | полный рандом |
| any | max_5h=30%, max_weekly=10% | 349₽ | рандом среди «плохих» аккаунтов |
| chat | min=30% | 449₽ | гарантия chat-лимита ≥30% |
| chat | min=50% | 499₽ | гарантия chat-лимита ≥50% |
| codex | min=50% | 599₽ | гарантия codex-лимита ≥50% |
| codex | min=80% | 799₽ | гарантия codex-лимита ≥80% |

### Lot — лот на FunPay (один лот = RU+EN локализации)

Поля порогов зависят от scope:
- `scope=any`: используются `max_5h_pct` + `max_weekly_pct` (верхние пороги, потолок)
  - оба NULL → полный рандом (без учёта лимитов)
  - заданы → аккаунт подходит, если все 4 замера ≤ соответствующих порогов
- `scope=chat`/`codex`: используется `min_limit_pct` (нижний порог, гарантия)
  - аккаунт подходит, если оба окна (5h И weekly) выбранного типа ≥ порога

```
Lot
├── id
├── funpay_id                      ID лота на FunPay (после создания)
├── funpay_node_id                 категория FunPay
├── tier_id  ────────────────→ SubscriptionTier
├── duration_id  ────────────→ Duration
├── limit_scope_id  ─────────→ LimitScope
├── min_limit_pct                  нижний порог % (для chat/codex; NULL для any)
├── max_5h_pct                     верхний порог 5ч % (для any; NULL = без потолка 5ч)
├── max_weekly_pct                 верхний порог weekly % (для any; NULL = без потолка weekly)
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
├── min_limit_pct                  нижний порог лота (для chat/codex)
├── max_5h_pct                     верхний порог 5ч лота (для any)
├── max_weekly_pct                 верхний порог weekly лота (для any)
├── lang                           ru | en (по локали покупателя)
├── started_at                     UTC
├── expires_at                     started_at + duration.days
├── status                         active | expired | replaced | revoked
├── replaced_by_rental_id          ссылка на новую аренду при !замена
├── replacement_count              сколько раз эта аренда была заменена (для лога/метрик)
├── last_code_request_at           анти-спам для !код
├── issued_chat_5h_pct             срез лимита на момент выдачи (информационно)
├── issued_chat_weekly_pct
├── issued_codex_5h_pct
└── issued_codex_weekly_pct
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
├── min_limit_pct                  порог лота (для chat/codex)
├── max_5h_pct                     верхний порог 5ч (для any)
├── max_weekly_pct                 верхний порог weekly (для any)
├── price
├── status                         pending | delivered | confirmed | dispute | cancelled
└── created_at
```

### LotTemplate — шаблоны текстов лотов

```
LotTemplate
├── id
├── tier_id  ────────────────→ SubscriptionTier
├── limit_scope_id  ─────────→ LimitScope (NULL = шаблон для всех scope)
├── title_template_ru              "ChatGPT {tier} — {days} дн. ({scope_text})"
│                                  где {scope_text}: "Без проверки лимитов" для any,
│                                  "{scope} ≥{min_pct}%" для chat/codex,
│                                  "Лимит ≤{max_5h}%/≤{max_weekly}%" для any с потолком
├── title_template_en
├── description_template_ru        многострочный шаблон
├── description_template_en
└── переменные: {tier}, {days}, {price}, {scope}, {min_pct}
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
| `welcome` | выдача данных после оплаты | {tier}, {days}, {login}, {password}, {expires_at}, {chat_5h}, {chat_weekly}, {codex_5h}, {codex_weekly} |
| `code_success` | `!код`, аренда active | {code}, {expires_in} |
| `code_expired` | `!код`, аренда expired | — |
| `code_rate_limited` | `!код`, анти-спам | {retry_in_sec} |
| `subscription` | `!подписка` | {tier}, {expires_at}, {expires_in}, {chat_5h}, {chat_weekly}, {codex_5h}, {codex_weekly} |
| `replace_success` | `!замена` успешна | {login}, {password}, {tier}, {days}, {expires_at}, {chat_5h}, {chat_weekly}, {codex_5h}, {codex_weekly} |
| `replace_declined` | `!замена`, аккаунт работает | — |
| `replace_no_account` | `!замена`, нет свободного аккаунта | — |
| `seller_called` | `!продавец`, ответ покупателю | — |
| `help` | `!помощь` | — |
| `order_confirmed` | покупатель подтвердил сделку | {tier}, {days} |
| `expiry` | истечение аренды | {tier}, {days} |
| `disconnect` | временный кик (logout all) | {expires_in} |
| `no_account_available` | нет аккаунта при заказе | {retry_minutes} |

Дефолтные значения зшиты в код, заполняют таблицу при первичной инициализации (миграция). Продавец редактирует через админку.

**Примеры RU:**

```
welcome:
  ✅ Заказ выполнен. ChatGPT {tier} на {days} дн.:
  Логин: {login}
  Пароль: {password}
  Подписка активна до: {expires_at}
  📊 Лимиты: Чат 5ч — {chat_5h}% / неделя — {chat_weekly}%
            Codex 5ч — {codex_5h}% / неделя — {codex_weekly}%
  ⚠️ Лимиты общие для аккаунта, обновляются динамически.
  📱 Для входа: !код | Помощь: !помощь | Замена: !замена

code_success:
  🔑 Ваш код: {code}
  ⏱ Действителен 30 секунд.
  Подписка активна ещё: {expires_in}

code_expired:
  ❌ Доступ закончился. Для продления — новый заказ.

replace_success:
  🔄 Замена выполнена. Новые данные:
  Логин: {login}
  Пароль: {password}
  ChatGPT {tier}, {days} дн. Подписка до {expires_at}.
  📊 Лимиты: Чат 5ч — {chat_5h}% / неделя — {chat_weekly}%
            Codex 5ч — {codex_5h}% / неделя — {codex_weekly}%
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
  ⏰ Ваш доступ ({tier}, {days} дн.) закончился.
  Для продления — новый заказ.

no_account_available:
  ⏳ Нет свободных аккаунтов. Ожидайте до {retry_minutes} мин.
```

Каждый ключ имеет RU и EN версию. Язык выбора — по `Rental.lang`.

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
├── default_max_active_rentals     глобальный лимит по умолчанию
├── funpay_commission_percent      для расчёта netto (по умолчанию 15)
├── limits_warn_threshold_pct      порог уведомления продавцу о низких лимитах
└── admin_password_hash            (bcrypt)
```

## 8. Система команд (RU + EN)

Команды работают в чатах сделок FunPay. Матч по префиксу `!` + алиас (case-insensitive). Язык ответа — по `Rental.lang`.

| Назначение | RU | EN | Поведение |
|---|---|---|---|
| Получить 2FA-код | `!код` | `!code` | Проверка активной аренды → генерация TOTP → отправка. Истёкшим — отказ. Анти-спам: 1 код / 30 сек. |
| Проверить подписку и лимиты | `!подписка` | `!sub` | Показывает аккаунт, тариф, остаток времени, **актуальные лимиты** (5h/weekly для chat/codex). |
| Замена аккаунта | `!замена` | `!replace` | См. «Механика `!замена`» ниже. Автозамена только при бане/слете тарифа/неверных данных; иначе — вызов продавца. |

### Механика `!замена`

Бот логинится в проблемный аккаунт через Playwright и проверяет три условия:
1. **Логин работает** (login:pass + TOTP валидны)
2. **Аккаунт не забанен** (страница не показывает бан)
3. **Тариф активен** (`subscription_expires_at > NOW()`, tier совпадает с заявленным)

Если **хоть одно не выполняется** → автоматическая замена:
- Старая аренда → `status=replaced`, `replaced_by_rental_id` = новой
- Выдаётся новый аккаунт по тем же правилам `AccountPool.acquire` (тот же tier/duration/scope/пороги)
- Welcome-сообщение с новыми данными
- Уведомление продавцу в Telegram «Замена: причина X, старый аккаунт Y → новый Z»

Если **всё работает корректно** → отказ: «Аккаунт работает. Опишите проблему подробнее командой `!продавец`».

Лимита на количество замен нет, но все запросы логируются в AuditLog (видны продавцу).
| Вызвать продавца | `!продавец` | `!seller` | Бот пишет покупателю «Продавец уведомлён, ожидайте» и отправляет уведомление в Telegram. При недоступности Telegram — повтор с backoff, покупателю всё равно «уведомлён». |
| Помощь | `!помощь` | `!help` | Список команд на языке аренды. |

**Безопасность:** код выдаётся строго по `chat_id` аренды. Покупатель не может получить код чужого аккаунта — логин в команде игнорируется, привязка по чату сделки.

`!код` генерируется локально через pyotp от `totp_secret_encrypted` аккаунта. Не зависит от `refresh_status` и `Account.status` — работает всегда, пока `Rental.status = 'active'`. Если аренда `expired` → отказ.

`!подписка` дополнительно триггерит свежий замер лимитов аккаунта через backend-api (on-demand), чтобы арендатор видел актуальные %. Если аккаунт в `maintenance` (`refresh_status != 'ok'`) или замер устарел (`measured_at < NOW() - 1 hour`) — показываются последние кэшированные значения с пометкой «обновляется».

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
       Account.subscription_expires_at >= NOW() + duration.days
       active_rentals_count < эффективный_лимит(account)
       AccountLimits.measured_at >= NOW() - 1 hour  (свежий замер)
       AccountLimits.refresh_status = 'ok'
     
     if scope == 'any':
       # потолок: если max_5h_pct/max_weekly_pct заданы — все 4 замера ≤ порогов
       if max_5h_pct is not None:
         filter: chat_5h ≤ max_5h_pct AND codex_5h ≤ max_5h_pct
       if max_weekly_pct is not None:
         filter: chat_weekly ≤ max_weekly_pct AND codex_weekly ≤ max_weekly_pct
       ORDER BY subscription_expires_at ASC   # FIFO по сроку подписки
     else:  # chat / codex
       # гарантия: оба окна выбранного типа ≥ min_limit_pct
       filter: оба окна(scope) >= min_limit_pct
       ORDER BY LEAST(5h_pct, weekly_pct) DESC  # наибольший запас
    │
    ├── нет аккаунта → retry каждые 1 мин, таймаут 30 мин → уведомление продавцу
    │
    ▼
3. Создать Rental (started_at=now, expires_at=now+duration, status=active)
   сохранить issued_*_pct (срез лимитов на момент выдачи)
    │
    ▼
4. Отправить в чат сделки welcome_message:
   рендеринг MessageTemplate['welcome'] с подстановкой переменных.
   Шаблон — в секции «MessageTemplate» (title/description/лимиты/инструкции).
    │
    ▼
5. Доставка данных через чат: бот отправляет welcome_message (логин/пароль/2FA-инструкции)
   FunPay НЕ имеет API complete_order() — подтверждение сделки = действие покупателя
   Order.status остаётся pending до SaleClosedEvent
   → триггер LotAutoManager (пересчёт квоты и лимитов, возможное снятие лотов)
    │
    ▼
[Активная аренда — пользователь пользуется]
   команды: !код / !подписка / !замена / !продавец / !помощь
    │
    ▼ (Scheduler, проверка истечений каждые 30 сек)
6. Rental.expires_at <= NOW() → status = expired
    │
    ▼
7. KickService.kick(account):
   - Дедупликация: logout all один раз за 60 сек на аккаунт
     (5 одновременных истечений на одном аккаунте = 1 logout all)
   - Playwright: логин → Settings → «Log out everywhere»
   - Пароль НЕ меняем
   - Уведомить активных арендаторов (MessageTemplate['disconnect']): «Для повторного входа: !код»
   - Logout all инвалидирует refresh_token → одно `refresh_recover` job на аккаунт
     (не 5 по числу истечений — дедупликация на уровне AccountCheckJob)
    │
    ▼
8. Уведомление в чат истёкшей аренды (MessageTemplate['expiry'])
    │
    ▼
9. Уведомление продавцу в Telegram (опционально)
   → триггер LotAutoManager (слот освободился)
```

### Эффективный лимит аккаунта

```
эффективный_лимит(account) = COALESCE(
    account.max_active_rentals,
    SellerSettings.default_max_active_rentals
)
```

### Защита доступа после истечения

| Кто пытается войти после logout all | Логин:пароль | 2FA-код | Результат |
|---|---|---|---|
| Активный арендатор | знает | бот выдаёт (аренда active) | входит |
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
                AND AccountLimits.measured_at >= NOW() - 1 hour
                AND (
                    # scope=any: потолок (если задан) — все 4 замера ≤ порогов
                    (scope == 'any' AND max_пороги is NULL)
                    OR (scope == 'any'
                        AND chat_5h ≤ max_5h AND codex_5h ≤ max_5h
                        AND chat_weekly ≤ max_weekly AND codex_weekly ≤ max_weekly)
                    # scope=chat/codex: гарантия — оба окна типа ≥ min_limit_pct
                    OR (scope IN ('chat','codex')
                        AND оба окна(scope) >= min_limit_pct)
                )
            
            manage_lot(tier, duration, scope, пороги, price, has_capacity)
```

Каждая связка управляется **независимо**. Примеры:
- Plus × 7дн × codex ≥50% снимется, когда на всех Plus-аккаунтах codex weekly/5h <50%.
- Plus × 7дн × any (полный рандом) останется активным, пока есть аккаунт с нужным сроком.
- Plus × 7дн × any (max_5h=30%, max_weekly=10%) снимется, когда все аккаунты имеют хотя бы один замер выше потолка.
- Plus × 7дн × chat ≥30% останется активным, если chat-лимит ≥30%.

### Генерация текстов лотов

Из LotTemplate с подстановкой `{tier} {days} {price} {scope} {min_pct}`.

**Пример title:** `ChatGPT Plus — 7 дн. (Codex ≥50%)`
**Пример description_template_ru:**
```
🎮 Доступ к ChatGPT {tier} на {days} дней.

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

**Аккаунты (пул):** таблица (login, tier, expires_at, active/total rentals, **4 лимита %**, status, last_check), добавить (форма), массовая загрузка CSV, действия (edit, pause, delete, проверить сейчас), лимиты (глобальный + per-account override). **Вид лимитов: 4 колонки (chat 5h/weekly, codex 5h/weekly) с цветовой индикацией порогов.**

**Тарифы (SubscriptionTier):** CRUD.

**Сроки (Duration):** чекбоксы (1-30), sort_order.

**Лимиты (LimitScope):** any/chat/codex — включение/выключение использования в лотах.

**Цены (PriceMatrix):** 4-мерная матрица (tier × duration × scope × пороги), inline-редактирование. Для scope=any — два ползунка (max_5h, max_weekly). Для chat/codex — один ползунок (min_limit). Цена растёт с требованиями.

**Шаблоны лотов (LotTemplate):** title/description для RU/EN с переменными.

**Шаблоны сообщений (MessageTemplate):** таблица key × lang (RU/EN), inline-редактирование. Все 14 ключей (welcome, code_success, code_expired, subscription, replace_success/declined/no_account, seller_called, help, order_confirmed, expiry, disconnect, no_account_available, code_rate_limited). Предпросмотр с подставленными переменными.

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
GET    /api/durations | PATCH
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
- «📊 Лимиты аккаунта X упали ниже порога (chat weekly: 18%)»
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
7. **Лимиты общие (shared):** арендаторы расходуют лимит вместе. Лимитный лот может исчезнуть после выдачи. Митигация — частые замеры, on-demand замер перед выдачей, `min_limit_pct` с запасом.
