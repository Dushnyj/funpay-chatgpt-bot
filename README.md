# FunPay ChatGPT Rental Bot

Бот для аренды ChatGPT-аккаунтов через маркетплейс FunPay.

Автоматизирует весь цикл: загрузка пула аккаунтов → создание лотов → выдача после оплаты → контроль доступа через 2FA → кик при истечении аренды.

## Возможности

- **Пул аккаунтов**: загрузка ChatGPT-аккаунтов с автоматической валидацией через OAuth
- **Авто-включение 2FA**: бот сам включает TOTP через Playwright, читая QR-код. Нужен только логин + пароль + доступ к email (IMAP)
- **Замер лимитов**: мониторинг 5h/weekly rate limits через backend-api OpenAI
- **Авто-лоты**: динамическое создание/снятие лотов на FunPay в зависимости от доступности аккаунтов и лимитов
- **Команды в чатах сделок**: `!код`/`!code`, `!подписка`/`!sub`, `!замена`/`!replace`, `!продавец`/`!seller`, `!помощь`/`!help` (RU+EN)
- **Кик при истечении**: logout all sessions → 2FA-код как барьер доступа
- **Админ-панель**: React SPA (дашборд, аккаунты, лоты, заказы, настройки, шаблоны сообщений)
- **Telegram-уведомления**: 10 типов событий продавцу

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
FUNPAY_SESSION_KEY=<golden_key>
TELEGRAM_BOT_TOKEN=<опционально>
TELEGRAM_SELLER_CHAT_ID=<опционально>
POSTGRES_PASSWORD=<пароль БД>
```

## Документация

- [Спецификация](docs/superpowers/specs/2026-07-11-funpay-chatgpt-rental-bot-design.md) — полная архитектура, модели, flow
- [Deployment](docs/deployment.md) — детальная инструкция развёртывания

## Тесты

```bash
cd backend
pip install -e ".[dev]"
pytest                    # 268+ тестов
cd ../frontend && npm run build  # проверка сборки SPA
```
