# Развёртывание FunPay ChatGPT Rental Bot

## Требования к серверу

- **OS:** Ubuntu 22.04+ / Debian 12+
- **RAM:** 2 ГБ минимум (Chromium ~500 МБ на контекст + Postgres + FastAPI)
- **Диск:** 10 ГБ
- **CPU:** 2 ядра
- **Docker Engine + Compose plugin**
- **nginx + certbot** на хосте

## Состав

```
backend/    Python 3.12, FastAPI, SQLAlchemy, Playwright, funpaybotengine
frontend/   React + TS + Vite → статик (собирается в dist/)
postgres/   БД
```

## Сборка

```bash
docker compose build --pull backend
```

Multi-stage образ сам собирает frontend, устанавливает Python-зависимости и совместимый Chromium. Backend запускается от непривилегированного пользователя.

## Секреты (.env)

Создай `.env` в корне репозитория:

```bash
POSTGRES_PASSWORD=<случайный пароль PostgreSQL>
POSTGRES_USER=funpay
POSTGRES_DB=funpay_bot
ENCRYPTION_KEY=<Fernet ключ>
SECRET_KEY=<случайная строка для JWT>
ADMIN_PASSWORD_HASH=<bcrypt хеш>
ADMIN_COOKIE_SECURE=true
FUNPAY_SESSION_KEY=<опциональный env fallback>
TELEGRAM_BOT_TOKEN=<опциональный env fallback>
TELEGRAM_SELLER_CHAT_ID=<опциональный env fallback>
MICROSOFT_GRAPH_CLIENT_ID=<Application (client) ID Microsoft Entra>
MICROSOFT_GRAPH_CLIENT_SECRET=<client secret приложения>
MICROSOFT_GRAPH_REDIRECT_URI=https://ваш-домен/api/email-oauth/microsoft/callback
```

Для личных Outlook/Hotmail зарегистрируйте web-приложение в Microsoft Entra с поддержкой personal Microsoft accounts. Добавьте delegated permissions `User.Read` и `Mail.Read`, затем web redirect URI, в точности совпадающий со значением выше. В админ-панели откройте аккаунт и нажмите **Почта OAuth**; после согласия Microsoft проверка аккаунта перезапустится автоматически. Пароль Outlook приложению не передаётся.

Генерация:
```bash
python3.12 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
python3.12 -c "from passlib.hash import bcrypt; print(bcrypt.hash('your_password'))"
python3.12 -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Запуск

FastAPI обслуживает `/api` и SPA. В том же event loop работают Scheduler и FunPay Runner. PostgreSQL доступен только внутри compose-сети, backend опубликован на `127.0.0.1:8000` для nginx.

```bash
docker compose pull postgres
docker compose up -d --build --remove-orphans
docker compose ps
curl -fsS http://127.0.0.1:8000/health
```

## Обновление

```bash
git pull --ff-only origin main
docker compose pull postgres
docker compose up -d --build --remove-orphans
docker compose ps
```

До обновления обязательно сделать `pg_dump` и архив `.env`/конфигурации nginx. Alembic запускается автоматически до старта lifecycle; при неполной legacy-схеме приложение останавливается вместо небезопасного stamp.

## Periodic задачи (Scheduler)

| Задача | Интервал | Что делает |
|---|---|---|
| `expire_overdue` | 30 сек | Помечает истёкшие аренды как expired, отправляет expiry message |
| `limits_check` | 5 мин | Замер наблюдаемых Codex-лимитов и повторное определение плана |
| `lot_auto_manager` | 10 мин | Пересчёт capacity, создание/пауза/активация лотов |
| `bump` | из настроек | Поднятие категории после настроенного cooldown |
| `refresh_recover` | 60 сек | Обработка одного refresh-recovery job (Playwright перезаход) |
| `refund_revoke` | 60 сек | Повтор отзыва доступа после refund при временной ошибке |

## Backup

Periodic `pg_dump` по cron:
```bash
# /etc/cron.d/funpay-backup
0 3 * * * postgres pg_dump -U funpay funpay_bot | gzip > /backup/funpay_$(date +\%Y\%m\%d).sql.gz
30 3 * * * find /backup -name "funpay_*.sql.gz" -mtime +14 -delete
```

## Первичная настройка

1. Запустить compose: Alembic создаст/обновит схему, bootstrap заполнит настройки и справочники
2. Открыть `http://server:8000/` → страница логина
3. Войти с паролем (из ADMIN_PASSWORD_HASH)
4. **Настройки:** сохранить Golden Key, `funpay_node_id` и при необходимости Telegram token/chat ID
5. **Аккаунты:** добавить ChatGPT-аккаунт без ручного выбора тарифа
6. В ChatGPT включить **Настройки → Безопасность и вход → Авторизация кода устройства для Codex**, затем нажать **Проверить через браузер** и подтвердить одноразовый код; после OAuth план определится автоматически
7. **Тарифы:** включить продажу только нужных системных планов; создавать/переименовывать планы вручную не нужно
8. **Цены:** настроить PriceMatrix (tier × duration × scope × пороги)
9. **Шаблоны:** при необходимости отредактировать RU/EN сообщения
10. Бот создаст лоты автоматически (LotAutoManager при наличии capacity)
11. При оплате заказа → автоматическая выдача аккаунта + welcome message с логином/паролем/2FA

## Мониторинг

- **/health** — состояние процесса, PostgreSQL, Scheduler и FunPay transport
- **/api/metrics** — метрики (требует auth): active_rentals, available_accounts, orders_today, revenue
- **логи:** `docker compose logs -f --tail=200 backend`

## Устранение неисправностей

| Симптом | Причина | Решение |
|---|---|---|
| FunPay не подключается | Неверный/протухший golden_key | Заменить Golden Key в админке и проверить статус |
| Аккаунт показывает `cloudflare_challenge` | OpenAI заблокировал headless Chromium | Использовать «Проверить через браузер» и device code flow |
| Outlook/Hotmail показывает, что OAuth не подключён | Microsoft Graph не настроен или согласие ещё не выдано | Заполнить три `MICROSOFT_GRAPH_*` значения, перезапустить backend и нажать «Почта OAuth» |
| Outlook показывает `email_security_challenge` | Microsoft потребовал дополнительную проверку нового серверного IP | Подключить Microsoft Graph OAuth; парольный Outlook Web оставить только fallback |
| Аккаунт показывает `validation_failed` | Ошибка содержит точные stage/code/detail | Исправить указанную причину и повторить проверку |
| План не определён | OpenAI вернул неизвестный или конфликтующий raw plan | Не выдавать аккаунт; проверить raw/source и повторить OAuth |
| Лимиты не замеряются | refresh_token протух | Бот попытается авто-перезаход; при неудаче → maintenance |
| Лоты не создаются | Нет capacity (нет активных аккаунтов) | Добавить/проверить аккаунты |
| Telegram-уведомления не приходят | Неверный token/chat ID | Использовать кнопку «Тест» в настройках |
