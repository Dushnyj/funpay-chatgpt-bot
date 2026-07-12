# FunPay ChatGPT Rental Bot

Бот для аренды ChatGPT-аккаунтов через маркетплейс FunPay.

Автоматизирует весь цикл: загрузка пула аккаунтов → создание лотов → выдача после оплаты → контроль доступа через 2FA → кик при истечении аренды.

## Возможности

- **Пул аккаунтов**: загрузка ChatGPT-аккаунтов с проверкой через OAuth и точным статусом каждого этапа
- **Проверка через браузер**: рекомендуемый user-assisted device flow проходит Cloudflare/MFA в обычном браузере; headless Playwright остаётся диагностическим fallback
- **Системный каталог планов**: Free, Go, Plus, Pro 5x, Pro 20x, Business, Enterprise, Edu, Teachers, Healthcare, Clinicians и Gov; тариф аккаунта определяется автоматически
- **Замер лимитов**: мониторинг наблюдаемых 5h/weekly Codex rate limits через изолированный backend-api client; неизвестные ChatGPT-лимиты не подменяются копией Codex
- **Авто-лоты**: динамическое создание/снятие лотов на FunPay в зависимости от доступности аккаунтов и лимитов
- **Команды в чатах сделок**: `!код`/`!code`, `!подписка`/`!sub`, `!замена`/`!replace`, `!продавец`/`!seller`, `!помощь`/`!help` (RU+EN)
- **Кик при истечении**: logout all sessions → 2FA-код как барьер доступа
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
FUNPAY_SESSION_KEY=<опциональный env fallback для golden_key>
TELEGRAM_BOT_TOKEN=<опционально>
TELEGRAM_SELLER_CHAT_ID=<опционально>
POSTGRES_PASSWORD=<пароль БД>
```

После первого входа Golden Key и Telegram можно настроить в разделе **Настройки**. Секреты не возвращаются в браузер после сохранения.

При добавлении ChatGPT-аккаунта тариф выбирать не нужно. Откройте **Аккаунты → Проверить через браузер**, перейдите по HTTPS-ссылке OpenAI и введите одноразовый код. После OAuth система проверит, что подтверждён именно добавленный аккаунт, определит план и только затем сделает его доступным пулу. Неизвестный или конфликтующий `plan_type` остаётся заблокированным для выдачи.

Автоматическая работа с email через пароль приложения поддерживается только у провайдеров, где разрешён IMAP Basic Auth. Outlook/Hotmail требуют OAuth2 и поэтому сразу получают понятный статус `email_provider_unsupported`, а не бесконечное «Проверяется».

## Документация

- [Спецификация](docs/superpowers/specs/2026-07-11-funpay-chatgpt-rental-bot-design.md) — полная архитектура, модели, flow
- [Deployment](docs/deployment.md) — детальная инструкция развёртывания

## Тесты

```bash
cd backend
pip install -e ".[dev]"
pytest                    # 360+ тестов
cd ../frontend && npm run lint && npm run build
```
