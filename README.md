# FunPay ChatGPT Rental Bot

Бот для аренды ChatGPT-аккаунтов через маркетплейс FunPay.

Автоматизирует весь цикл: загрузка пула аккаунтов → создание лотов → выдача после оплаты → контроль доступа через 2FA → кик при истечении аренды.

## Возможности

- **Пул аккаунтов**: загрузка ChatGPT-аккаунтов с проверкой через OAuth и точным статусом каждого этапа
- **Проверка через браузер**: рекомендуемый user-assisted device flow проходит Cloudflare/MFA в обычном браузере; headless Playwright остаётся диагностическим fallback
- **Системный каталог планов**: Free, Go, Plus, Pro 5x, Pro 20x, Business, Enterprise, Edu, Teachers, Healthcare, Clinicians и Gov; тариф аккаунта определяется автоматически
- **Единый лимит Codex**: бот продаёт и показывает одно проверенное длинное окно общего пула Codex/Work/Workspace Agents/Excel — 30 дней для Free и 7 дней для платных тарифов; отдельного лимита обычных Chat-разговоров в модели нет
- **Гибкие сроки**: аренда от 30 минут до 30 дней с шагом 30 минут; в панели срок вводится в минутах, часах или днях
- **Outlook OAuth**: чтение только новых кодов OpenAI через Microsoft Graph `Mail.Read`, без передачи пароля почты боту; Outlook Web остаётся диагностическим fallback
- **Авто-лоты**: динамическое создание/снятие лотов на FunPay в зависимости от доступности аккаунтов и лимитов
- **Команды в чатах сделок**: `!код`/`!code`, `!подписка`/`!sub`, `!замена`/`!replace`, `!продавец`/`!seller`, `!помощь`/`!help` (RU+EN)
- **Кик при истечении**: logout all sessions → 2FA-код как барьер доступа
- **Безопасная ёмкость**: не более одной активной аренды на аккаунт, поскольку OpenAI logout отзывает все сессии аккаунта целиком
- **Админ-панель**: адаптивная React SPA (дашборд, аккаунты, чаты, лоты, сделки, цены, настройки и шаблоны)
- **Чаты покупателей**: входящие FunPay-сообщения, непрочитанные, история и ответы продавца прямо из админ-панели
- **Telegram-уведомления**: 10 типов событий продавцу
- **Безопасная конфигурация**: Golden Key и Telegram token задаются write-only через панель и шифруются в PostgreSQL
- **Миграции и bootstrap**: Alembic обновляет существующую БД, а первый запуск создаёт настройки и справочники

## Технологии

| Слой | Стек |
|---|---|
| Backend | Python 3.12, FastAPI, SQLAlchemy 2.0 async, asyncpg |
| Browser automation | Playwright (Chromium) |
| FunPay | funpaybotengine (WebSocket events) |
| Frontend | React 19, TypeScript, Vite |
| БД | PostgreSQL 16 |
| Деплой | Docker Compose |

## Архитектура

```
Internet → nginx (HTTPS) → backend :8000
                              ├─ FastAPI: /api/* + SPA static
                              ├─ FunPayRunner: WebSocket events
                              └─ Scheduler: expire/limits/bump/refresh
                                    ↓
                              PostgreSQL (docker internal)
```

## Структура проекта

```
backend/          Python: FastAPI, сервисы, интеграции
  app/
    api/          REST routers (auth, accounts, lots, orders, ...)
    integrations/  funpay, openai, playwright, email
    models/       SQLAlchemy models
    services/     бизнес-логика
frontend/         React SPA (админ-панель)
docs/             спецификация, планы, deployment
```

## Быстрый старт

### Требования
- Docker + Docker Compose
- Домен с A-записью на сервер
- golden_key FunPay (из cookies браузера)

### Установка

```bash
# 1. Клонировать
git clone https://github.com/Dushnyj/funpay-chatgpt-bot.git
cd funpay-chatgpt-bot

# 2. Создать .env (см. .env.example)
cp .env.example .env
# Сгенерировать ключи:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Заполнить значения

# 3. Запустить
docker compose up -d --build

# 4. Настроить nginx reverse-proxy на порт 8000
# 5. Получить HTTPS сертификат: certbot --nginx -d ваш-домен
```

### .env

```bash
DATABASE_URL=postgresql+asyncpg://funpay:PASSWORD@postgres:5432/funpay_bot
ENCRYPTION_KEY=<Fernet ключ>
SECRET_KEY=<случайная строка>
ADMIN_PASSWORD_HASH=<bcrypt хеш>
BROWSER_CONCURRENCY_CAP=1  # предел фоновых Chromium-задач для сервера с 2 ГБ RAM
FUNPAY_SESSION_KEY=<опциональный env fallback для golden_key>
TELEGRAM_BOT_TOKEN=<опционально>
TELEGRAM_SELLER_CHAT_ID=<опционально>
MICROSOFT_GRAPH_CLIENT_ID=<Application (client) ID>
MICROSOFT_GRAPH_CLIENT_SECRET=<client secret приложения>
MICROSOFT_GRAPH_REDIRECT_URI=https://ваш-домен/api/email-oauth/microsoft/callback
POSTGRES_PASSWORD=<пароль БД>
```

После первого входа Golden Key и Telegram можно настроить в разделе **Настройки**. Секреты не возвращаются в браузер после сохранения.

При добавлении ChatGPT-аккаунта тариф выбирать не нужно. Перед первым входом включите в ChatGPT **Настройки → Безопасность и вход → Авторизация кода устройства для Codex**, затем откройте **Аккаунты → Проверить через браузер** и подтвердите одноразовый код. После OAuth система проверит, что подтверждён именно добавленный аккаунт, определит план и только затем сделает его доступным пулу. Неизвестный или конфликтующий `plan_type` остаётся заблокированным для выдачи.

Для Outlook/Hotmail используйте кнопку **Почта OAuth**. В Microsoft Entra приложение должно поддерживать личные Microsoft-аккаунты, иметь delegated permissions `User.Read` и `Mail.Read`, а redirect URI должен в точности совпадать с `MICROSOFT_GRAPH_REDIRECT_URI`. Refresh token хранится в БД зашифрованным. Если Graph не настроен, система может проверить обычный вход через Outlook Web, но дополнительный Microsoft security challenge требует ручного подтверждения и возвращается как понятная ошибка, а не бесконечное «Проверяется».

## Документация

- [Спецификация](docs/superpowers/specs/2026-07-11-funpay-chatgpt-rental-bot-design.md) — полная архитектура, модели, flow
- [Deployment](docs/deployment.md) — детальная инструкция развёртывания

## Тесты

```bash
cd backend
pip install -e ".[dev]"
pytest                    # 800+ backend-тестов
cd ../frontend && npm run lint && npm run test && npm run build
```
