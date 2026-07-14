# Развёртывание FunPay ChatGPT Rental Bot

## Требования к серверу

- **OS:** Ubuntu 22.04+ / Debian 12+
- **RAM:** 2 ГБ минимум при `BROWSER_CONCURRENCY_CAP=1` (Chromium ~500 МБ на контекст + Postgres + FastAPI)
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
BROWSER_CONCURRENCY_CAP=1
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
python3.12 -c "import bcrypt; print(bcrypt.hashpw(b'your_password', bcrypt.gensalt()).decode())"
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

До обновления обязательно запустите проверяемый backup ниже и сохраните конфигурацию nginx. Alembic запускается автоматически до старта lifecycle; при неполной legacy-схеме приложение останавливается вместо небезопасного stamp. Миграция сроков переводит `Duration.days` в минуты с сохранением ID и внешних ключей, нормализует исторические настройки capacity и добавляет durable state для replacement/expiry. Ревизия `20260713_0016` отделяет подтверждённые продажи от произвольных личных чатов: старые диалоги не удаляются, но остаются скрытыми, пока их покупатель и chat node не подтверждены через точный sale-order. Ревизии `20260714_0017`–`20260714_0018` добавляют ограниченный cursor истории и разрешают выдачу только для заказов лотов с неизменяемым bot-marker. Ревизия `20260714_0019` хранит seller-history в неавторитетной recovery-очереди: только точное совпадение offer-id/marker переводит запись в `Order`/`FunPaySale`; очередь сама ограничена по попыткам и возрасту. Ревизия `20260714_0020` удаляет неподтверждённые сроки платных подписок и ставит аккаунты на повторную проверку. Исторические terminal-аренды и подтверждённые заказы не создают повторных сообщений после обновления. Поэтому **любой rollback с ревизий `20260713_0015`–`20260714_0020` выполняется только восстановлением pre-upgrade dump**: `alembic downgrade` возвращает форму схемы, но не восстанавливает очищенные или переинтерпретированные данные.

### Полный rollback из проверенного backup

Команды ниже останавливают backend, проверяют backup, временно запускают код из
сохранённой ветки `main`, пересоздают рабочую БД и только затем включают сервис.
Укажите фактический каталог backup, созданный **до** обновления:

```bash
cd /opt/funpay-chatgpt-bot
BACKUP=/opt/backups/funpay/predeploy/YYYYMMDDTHHMMSSZ

test -s "$BACKUP/database.dump"
test -s "$BACKUP/repository.bundle"
test -s "$BACKUP/environment.backup"
(cd "$BACKUP" && sha256sum --check SHA256SUMS)
git bundle verify "$BACKUP/repository.bundle"

docker compose stop backend
install -m 0600 "$BACKUP/environment.backup" .env
git fetch "$BACKUP/repository.bundle" refs/heads/main
git switch --detach FETCH_HEAD

docker compose exec -T postgres sh -eu -c \
  'dropdb --if-exists --force --username="$POSTGRES_USER" "$POSTGRES_DB"; createdb --username="$POSTGRES_USER" "$POSTGRES_DB"'
docker compose exec -T postgres sh -eu -c \
  'pg_restore --exit-on-error --single-transaction --no-owner --no-privileges --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"' \
  < "$BACKUP/database.dump"

docker compose up -d --build --remove-orphans
docker compose exec -T postgres sh -eu -c \
  'psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --command="TABLE alembic_version"'
curl -fsS http://127.0.0.1:8000/health
docker compose logs --tail=200 backend
```

Не запускайте новый backend поверх восстановленной старой БД. После исправления
релиза верните рабочую копию на `main` обычным `git switch main && git pull
--ff-only`, соберите контейнер и снова проверьте миграцию на копии backup.

Account-wide logout сериализуется PostgreSQL advisory lock и ограничен 240 секундами; это защищает от позднего revoke при нескольких uvicorn workers/контейнерах. Не обходите `KickService` прямым вызовом Playwright и не увеличивайте timeout выше 5-минутного revoke lease.

## Periodic задачи (Scheduler)

| Задача | Интервал | Что делает |
|---|---|---|
| `expire_overdue` | 30 сек | Durable revoke истёкших аренд и повтор неотправленных expiry message |
| `limits_check` | 5 мин | Замер наблюдаемых Codex-лимитов и повторное определение плана |
| `scheduled_validation` | из настроек | Полная периодическая проверка входа без снятия доказанно активного аккаунта при временном Cloudflare-сбое |
| `lot_auto_manager` | 10 мин | Пересчёт capacity, создание/пауза/активация лотов |
| `bump` | из настроек | Поднятие категории после настроенного cooldown |
| `refresh_recover` | 60 сек | Обработка одного refresh-recovery job (Playwright перезаход) |
| `refund_revoke` | 60 сек | Повтор отзыва доступа после refund при временной ошибке |
| `pending_order_retry` | 30 сек | Повтор безопасной выдачи оплаченного заказа с bounded backoff |
| `funpay_sale_sync` | 2 мин | Импорт только продаж, обновление профилей покупателей и ограниченное догружание chat node |
| `order_confirmed_notify` | 60 сек | Идемпотентный повтор buyer-уведомления после подтверждения заказа |

## Backup

В репозитории есть проверяемый backup-скрипт: он создаёт PostgreSQL custom
dump, восстанавливает его во временную изолированную БД и выполняет smoke-query,
сохраняет Git bundle и закрытую копию `.env`, затем атомарно публикует каталог
с SHA-256. Перед началом он проверяет свободное место для dump и временной
restore-БД; `flock` не допускает пересечения ручного запуска с таймером, а
systemd завершает зависший запуск через один час.
Установите таймер:

```bash
sudo install -m 0755 ops/backup.sh /opt/funpay-chatgpt-bot/ops/backup.sh
sudo install -m 0644 ops/systemd/funpay-backup.service /etc/systemd/system/
sudo install -m 0644 ops/systemd/funpay-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now funpay-backup.timer
sudo systemctl start funpay-backup.service
sudo systemctl status funpay-backup.service --no-pager
```

По умолчанию хранятся 14 суток в `/opt/backups/funpay/daily`. Это защищает от
ошибки приложения, но не от потери диска: добавьте шифрованную off-site копию
каталога и оповещение по `systemctl --failed`/возрасту последнего backup.

## Точное время для OTP и аренды

TOTP, сроки аренды и reset-время лимитов требуют синхронизированных системных
часов. Для Ubuntu установите проверенную конфигурацию `systemd-timesyncd`:

```bash
sudo install -d -m 0755 /etc/systemd/timesyncd.conf.d
sudo install -m 0644 ops/systemd/funpay-timesyncd.conf \
  /etc/systemd/timesyncd.conf.d/funpay.conf
sudo systemctl restart systemd-timesyncd
timedatectl show -p NTPSynchronized
timedatectl timesync-status
```

Перед продажами значение `NTPSynchronized` должно быть `yes`.

## Ограничение браузерных процессов

`BROWSER_CONCURRENCY_CAP` — жёсткий верхний предел одновременных фоновых
Chromium-задач проверки/восстановления аккаунтов и отзыва сессий. Значение из БД
`refresh_recover_concurrency` дополнительно ограничивается этим пределом, поэтому
старое сохранённое значение `3` не запустит три браузера при cap `1`. Для сервера
с 2 ГБ RAM оставьте `BROWSER_CONCURRENCY_CAP=1`; повышайте его только после
нагрузочного теста и увеличения памяти.

## Первичная настройка

1. Запустить compose: Alembic создаст/обновит схему, bootstrap заполнит настройки и справочники
2. Открыть `http://server:8000/` → страница логина
3. Войти с паролем (из ADMIN_PASSWORD_HASH)
4. **Настройки:** сохранить Golden Key, `funpay_node_id` и при необходимости Telegram token/chat ID
5. **Аккаунты:** добавить ChatGPT-аккаунт без ручного выбора тарифа
6. В ChatGPT включить **Настройки → Безопасность и вход → Авторизация кода устройства для Codex**, затем нажать **Проверить через браузер** и подтвердить одноразовый код; после OAuth план определится автоматически
7. **Тарифы:** включить продажу только нужных системных планов; создавать/переименовывать планы вручную не нужно
8. **Сроки:** создать нужные периоды от 30 минут до 30 дней; ввод доступен в минутах, часах и днях
9. **Цены:** настроить PriceMatrix (tier × duration × scope × пороги), где scope — `any` или единый измеримый `codex`
10. **Шаблоны:** при необходимости отредактировать RU/EN сообщения
11. Бот создаст лоты автоматически (LotAutoManager при наличии capacity)
12. При оплате заказа → автоматическая выдача аккаунта + welcome message с логином/паролем/2FA

## Мониторинг

- **/health** — состояние процесса, PostgreSQL, Scheduler и FunPay transport
- **/api/metrics** — метрики (требует auth): active_rentals, available_accounts, orders_today, revenue
- **логи:** `docker compose logs -f --tail=200 backend` (`APP_LOG_LEVEL=INFO` по умолчанию)

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
