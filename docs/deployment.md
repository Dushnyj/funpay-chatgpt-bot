# Развёртывание FunPay ChatGPT Rental Bot

## Требования к серверу

- **OS:** Ubuntu 22.04+ / Debian 12+
- **RAM:** 2 ГБ минимум (Chromium ~500 МБ на контекст + Postgres + FastAPI)
- **Диск:** 10 ГБ
- **CPU:** 2 ядра
- **Python:** 3.12+
- **Node.js:** 20+ (для сборки frontend)
- **PostgreSQL:** 15+
- **Chromium:** для Playwright

## Состав

```
backend/    Python 3.12, FastAPI, SQLAlchemy, Playwright, funpaybotengine
frontend/   React + TS + Vite → статик (собирается в dist/)
postgres/   БД
```

## Сборка

### Backend
```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
```

### Frontend
```bash
cd frontend
npm install
npm run build  # → frontend/dist/
```

## Секреты (.env)

Создай `backend/.env`:

```bash
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/funpay_bot
ENCRYPTION_KEY=<Fernet ключ>
SECRET_KEY=<случайная строка для JWT>
ADMIN_PASSWORD_HASH=<bcrypt хеш>
FUNPAY_SESSION_KEY=<golden_key из FunPay>
TELEGRAM_BOT_TOKEN=<токен бота>
TELEGRAM_SELLER_CHAT_ID=<chat_id продавца>
```

Генерация:
```bash
python3.12 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
python3.12 -c "from passlib.hash import bcrypt; print(bcrypt.hash('your_password'))"
python3.12 -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Запуск

Один процесс: FastAPI (uvicorn) обслуживает `/api` и раздаёт `frontend/dist`.
В том же event loop: Scheduler (периодические задачи), FunPay Runner (если golden_key).

```bash
cd backend
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Systemd unit

`/etc/systemd/system/funpay-bot.service`:

```ini
[Unit]
Description=FunPay ChatGPT Rental Bot
After=network.target postgresql.service

[Service]
Type=simple
User=funpay
WorkingDirectory=/opt/funpay/backend
EnvironmentFile=/opt/funpay/backend/.env
ExecStart=/opt/funpay/backend/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable funpay-bot
sudo systemctl start funpay-bot
sudo journalctl -u funpay-bot -f  # логи
```

## Periodic задачи (Scheduler)

| Задача | Интервал | Что делает |
|---|---|---|
| `expire_overdue` | 30 сек | Помечает истёкшие аренды как expired, отправляет expiry message |
| `limits_check` | 5 мин | Замер лимитов аккаунтов с устаревшим measured_at |
| `lot_auto_manager` | 2 мин | Пересчёт capacity, создание/пауза/активация лотов |
| `bump` | 1 час | Поднятие лотов с истёкшим кулдауном (бесплатный bump) |
| `refresh_recover` | 60 сек | Обработка одного refresh-recovery job (Playwright перезаход) |

## Backup

Periodic `pg_dump` по cron:
```bash
# /etc/cron.d/funpay-backup
0 3 * * * postgres pg_dump -U funpay funpay_bot | gzip > /backup/funpay_$(date +\%Y\%m\%d).sql.gz
30 3 * * * find /backup -name "funpay_*.sql.gz" -mtime +14 -delete
```

## Первичная настройка

1. Установить и запустить backend (создаст таблицы через `Base.metadata.create_all`)
2. Открыть `http://server:8000/` → страница логина
3. Войти с паролем (из ADMIN_PASSWORD_HASH)
4. **Справочники:** создать SubscriptionTier (Plus/Pro), Duration (1-30 дней), LimitScope (any/chat/codex)
5. **Аккаунты:** добавить ChatGPT аккаунты (проверятся автоматически через Playwright OAuth flow)
6. **Цены:** настроить PriceMatrix (tier × duration × scope × пороги)
7. **Шаблоны:** при необходимости отредактировать MessageTemplate (14 ключей × ru/en)
8. **Настройки:** указать `funpay_node_id` (категория FunPay для лотов)
9. Бот создаст лоты автоматически (LotAutoManager при наличии capacity)
10. При оплате заказа → автоматическая выдача аккаунта + welcome message с логином/паролем/2FA

## Мониторинг

- **/health** endpoint — базовая проверка (`{"status": "ok"}`)
- **/api/metrics** — метрики (требует auth): active_rentals, available_accounts, orders_today, revenue
- **systemd journal:** `journalctl -u funpay-bot -f`
- **Логи Python:** `logging` module, уровень INFO по умолчанию

## Устранение неисправностей

| Симптом | Причина | Решение |
|---|---|---|
| FunPay не подключается | Неверный/протухший golden_key | Обновить FUNPAY_SESSION_KEY в .env, перезапустить |
| Аккаунты не валидируются | Playwright/Chromium не установлен | `playwright install chromium` |
| Лимиты не замеряются | refresh_token протух | Бот попытается авто-перезаход; при неудаче → maintenance |
| Лоты не создаются | Нет capacity (нет активных аккаунтов) | Добавить/проверить аккаунты |
| Telegram-уведомления не приходят | Неверный токен/chat_id | Проверить TELEGRAM_BOT_TOKEN и TELEGRAM_SELLER_CHAT_ID |
