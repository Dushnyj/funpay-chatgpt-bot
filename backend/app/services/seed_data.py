from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Duration, LimitScope, SubscriptionTier
from app.models.lot import LotTemplate
from app.models.message import MessageTemplate
from app.services.lot_templates import (
    DEFAULT_LOT_TEMPLATES,
    LEGACY_DAY_LOT_TEMPLATES,
    validate_lot_template_values,
)
from app.services.subscription_plans import SYSTEM_SUBSCRIPTION_PLANS


DEFAULT_TIERS: tuple[tuple[str, str], ...] = tuple(
    (plan.name, plan.description) for plan in SYSTEM_SUBSCRIPTION_PLANS
)
DEFAULT_DURATIONS: tuple[int, ...] = tuple(
    days * 24 * 60 for days in (1, 3, 5, 7, 15, 30)
)
DEFAULT_LIMIT_SCOPES: tuple[tuple[str, str], ...] = (
    ("any", "Без гарантии лимита"),
    ("codex", "Codex"),
)
DEFAULT_LIMIT_SCOPE_AVAILABILITY: dict[str, bool] = {
    "any": True,
    "codex": True,
}

# Полный перечень ключей из спеки (секция MessageTemplate).
# Каждый ключ имеет ru и en варианты с плейсхолдерами для str.format.
# These are the exact defaults shipped before exact OpenAI windows were added.
# On bootstrap only byte-for-byte matches are upgraded; operator-edited content
# is never overwritten.
LEGACY_LIMIT_MESSAGE_TEMPLATES: dict[str, dict[str, str]] = {
    "welcome": {
        "ru": (
            "✅ Заказ выполнен. ChatGPT {tier} на {days} дн.:\n\n"
            "Логин: {login}\n"
            "Пароль: {password}\n"
            "Подписка активна до: {expires_at}\n\n"
            "📊 Лимиты: Чат 5ч — {chat_5h}% / неделя — {chat_weekly}%\n"
            "            Codex 5ч — {codex_5h}% / неделя — {codex_weekly}%\n\n"
            "⚠️ Лимиты общие для аккаунта, обновляются динамически.\n\n"
            "📱 Для входа: !код | Помощь: !помощь | Замена: !замена"
        ),
        "en": (
            "✅ Order completed. ChatGPT {tier} for {days} days:\n\n"
            "Login: {login}\n"
            "Password: {password}\n"
            "Subscription active until: {expires_at}\n\n"
            "📊 Limits: Chat 5h — {chat_5h}% / weekly — {chat_weekly}%\n"
            "           Codex 5h — {codex_5h}% / weekly — {codex_weekly}%\n\n"
            "⚠️ Limits are shared for the account, updated dynamically.\n\n"
            "📱 To log in: !code | Help: !help | Replace: !replace"
        ),
    },
    "subscription": {
        "ru": (
            "📊 ChatGPT {tier}\n"
            "Подписка до: {expires_at}\n"
            "Осталось: {expires_in}\n\n"
            "Лимиты:\n"
            "• Чат: 5ч — {chat_5h}%, неделя — {chat_weekly}%\n"
            "• Codex: 5ч — {codex_5h}%, неделя — {codex_weekly}%"
        ),
        "en": (
            "📊 ChatGPT {tier}\n"
            "Subscription until: {expires_at}\n"
            "Remaining: {expires_in}\n\n"
            "Limits:\n"
            "• Chat: 5h — {chat_5h}%, weekly — {chat_weekly}%\n"
            "• Codex: 5h — {codex_5h}%, weekly — {codex_weekly}%"
        ),
    },
    "replace_success": {
        "ru": (
            "🔄 Замена выполнена. Новые данные:\n\n"
            "Логин: {login}\n"
            "Пароль: {password}\n"
            "ChatGPT {tier}, {days} дн. Подписка до {expires_at}.\n\n"
            "📊 Лимиты: Чат 5ч — {chat_5h}% / неделя — {chat_weekly}%\n"
            "            Codex 5ч — {codex_5h}% / неделя — {codex_weekly}%\n\n"
            "📱 Для кода входа: !код"
        ),
        "en": (
            "🔄 Replacement done. New credentials:\n\n"
            "Login: {login}\n"
            "Password: {password}\n"
            "ChatGPT {tier}, {days} days. Subscription until {expires_at}.\n\n"
            "📊 Limits: Chat 5h — {chat_5h}% / weekly — {chat_weekly}%\n"
            "           Codex 5h — {codex_5h}% / weekly — {codex_weekly}%\n\n"
            "📱 For login code: !code"
        ),
    },
}


PRE_AGENTIC_MESSAGE_TEMPLATES: dict[str, dict[str, str]] = {
    "welcome": {
        "ru": (
            "✅ Заказ выполнен. ChatGPT {tier} на {days} дн.:\n\n"
            "Логин: {login}\n"
            "Пароль: {password}\n"
            "Подписка активна до: {expires_at}\n\n"
            "📊 Лимиты:\n"
            "• Chat: 5 ч — {chat_5h}% / 7 дн. — {chat_weekly}%\n"
            "• Codex: {codex_primary_limit}; окно {codex_primary_window}; "
            "сброс {codex_primary_reset}\n"
            "• Codex доп.: {codex_secondary_limit}; окно "
            "{codex_secondary_window}; сброс {codex_secondary_reset}\n\n"
            "⚠️ Лимиты общие для аккаунта, обновляются динамически.\n\n"
            "📱 Для входа: !код | Помощь: !помощь | Замена: !замена"
        ),
        "en": (
            "✅ Order completed. ChatGPT {tier} for {days} days:\n\n"
            "Login: {login}\n"
            "Password: {password}\n"
            "Subscription active until: {expires_at}\n\n"
            "📊 Limits:\n"
            "• Chat: 5h — {chat_5h}% / 7d — {chat_weekly}%\n"
            "• Codex: {codex_primary_limit}; window {codex_primary_window}; "
            "resets {codex_primary_reset}\n"
            "• Codex secondary: {codex_secondary_limit}; window "
            "{codex_secondary_window}; resets {codex_secondary_reset}\n\n"
            "⚠️ Limits are shared for the account, updated dynamically.\n\n"
            "📱 To log in: !code | Help: !help | Replace: !replace"
        ),
    },
    "code_success": {
        "ru": "🔑 Ваш код: {code}\n⏱ Действителен 30 секунд.\nПодписка активна ещё: {expires_in}",
        "en": "🔑 Your code: {code}\n⏱ Valid for 30 seconds.\nSubscription active: {expires_in}",
    },
    "code_expired": {
        "ru": "❌ Доступ закончился. Для продления — новый заказ.",
        "en": "❌ Access expired. To extend — place a new order.",
    },
    "rental_ambiguous": {
        "ru": (
            "⚠️ В этом чате несколько активных заказов. "
            "Откройте нужный заказ на FunPay и повторите команду из него."
        ),
        "en": (
            "⚠️ This chat has multiple active orders. Open the required "
            "FunPay order and repeat the command from that order."
        ),
    },
    "code_rate_limited": {
        "ru": "⏳ Подождите {retry_in_sec} сек. перед запросом нового кода.",
        "en": "⏳ Wait {retry_in_sec} sec. before requesting a new code.",
    },
    "email_code_success": {
        "ru": "📧 Email OTP OpenAI: {email_code}",
        "en": "📧 OpenAI email OTP: {email_code}",
    },
    "email_code_duplicate": {
        "ru": (
            "📧 Последний email-код уже выдавался. Запросите новый код в "
            "OpenAI и повторите !код."
        ),
        "en": (
            "📧 The latest email code was already delivered. Request a new "
            "one in OpenAI, then use !code again."
        ),
    },
    "email_code_not_found": {
        "ru": (
            "📧 Свежий email-код не найден. Если OpenAI просит код из письма — "
            "вызовите продавца: !продавец."
        ),
        "en": (
            "📧 No fresh email code was found. If OpenAI asks for one, contact "
            "the seller with !seller."
        ),
    },
    "email_code_unavailable": {
        "ru": "📧 Для доступа к почте нужен продавец: !продавец.",
        "en": "📧 Mailbox access needs seller assistance: !seller.",
    },
    "subscription": {
        "ru": (
            "📊 ChatGPT {tier}\n"
            "Подписка до: {expires_at}\n"
            "Осталось: {expires_in}\n\n"
            "Лимиты:\n"
            "• Chat: 5 ч — {chat_5h}%, 7 дн. — {chat_weekly}%\n"
            "• Codex: {codex_primary_limit}; окно {codex_primary_window}; "
            "сброс {codex_primary_reset}\n"
            "• Codex доп.: {codex_secondary_limit}; окно "
            "{codex_secondary_window}; сброс {codex_secondary_reset}"
        ),
        "en": (
            "📊 ChatGPT {tier}\n"
            "Subscription until: {expires_at}\n"
            "Remaining: {expires_in}\n\n"
            "Limits:\n"
            "• Chat: 5h — {chat_5h}%, 7d — {chat_weekly}%\n"
            "• Codex: {codex_primary_limit}; window {codex_primary_window}; "
            "resets {codex_primary_reset}\n"
            "• Codex secondary: {codex_secondary_limit}; window "
            "{codex_secondary_window}; resets {codex_secondary_reset}"
        ),
    },
    "replace_success": {
        "ru": (
            "🔄 Замена выполнена. Новые данные:\n\n"
            "Логин: {login}\n"
            "Пароль: {password}\n"
            "ChatGPT {tier}, {days} дн. Подписка до {expires_at}.\n\n"
            "📊 Лимиты:\n"
            "• Chat: 5 ч — {chat_5h}% / 7 дн. — {chat_weekly}%\n"
            "• Codex: {codex_primary_limit}; окно {codex_primary_window}; "
            "сброс {codex_primary_reset}\n"
            "• Codex доп.: {codex_secondary_limit}; окно "
            "{codex_secondary_window}; сброс {codex_secondary_reset}\n\n"
            "📱 Для кода входа: !код"
        ),
        "en": (
            "🔄 Replacement done. New credentials:\n\n"
            "Login: {login}\n"
            "Password: {password}\n"
            "ChatGPT {tier}, {days} days. Subscription until {expires_at}.\n\n"
            "📊 Limits:\n"
            "• Chat: 5h — {chat_5h}% / 7d — {chat_weekly}%\n"
            "• Codex: {codex_primary_limit}; window {codex_primary_window}; "
            "resets {codex_primary_reset}\n"
            "• Codex secondary: {codex_secondary_limit}; window "
            "{codex_secondary_window}; resets {codex_secondary_reset}\n\n"
            "📱 For login code: !code"
        ),
    },
    "replace_declined": {
        "ru": "✅ Аккаунт работает корректно.\nУточните проблему: !продавец",
        "en": "✅ Account works correctly.\nDescribe the issue: !seller",
    },
    "replace_no_account": {
        "ru": "⏳ Нет свободных аккаунтов для замены. Ожидайте.",
        "en": "⏳ No free accounts for replacement. Please wait.",
    },
    "seller_called": {
        "ru": "📢 Продавец уведомлён. Ожидайте ответа.",
        "en": "📢 Seller notified. Please wait for a response.",
    },
    "help": {
        "ru": (
            "📖 Команды:\n"
            "!код — получить код входа\n"
            "!подписка — статус подписки и лимиты\n"
            "!замена — заменить аккаунт при проблемах\n"
            "!продавец — вызвать продавца\n"
            "!помощь — эта справка"
        ),
        "en": (
            "📖 Commands:\n"
            "!code — get login code\n"
            "!sub — subscription status and limits\n"
            "!replace — replace account if issues\n"
            "!seller — call the seller\n"
            "!help — this help"
        ),
    },
    "order_confirmed": {
        "ru": "🙏 Спасибо за покупку! Если понадобится помощь — !помощь.",
        "en": "🙏 Thank you for your purchase! If you need help — !help.",
    },
    "expiry": {
        "ru": "⏰ Ваш доступ ({tier}, {days} дн.) закончился.\nДля продления — новый заказ.",
        "en": "⏰ Your access ({tier}, {days} days) has expired.\nTo extend — new order.",
    },
    "disconnect": {
        "ru": "⚠️ Временное отключение. Подписка активна ещё: {expires_in}.\nДля повторного входа: !код",
        "en": "⚠️ Temporary disconnect. Subscription active: {expires_in}.\nTo log back in: !code",
    },
    "no_account_available": {
        "ru": "⏳ Нет свободных аккаунтов. Ожидайте до {retry_minutes} мин.",
        "en": "⏳ No free accounts available. Wait up to {retry_minutes} min.",
    },
}


# Active defaults describe the single measured agentic pool and use a
# display-ready duration so sub-day rentals remain truthful in every locale.
DEFAULT_MESSAGE_TEMPLATES: dict[str, dict[str, str]] = {
    **PRE_AGENTIC_MESSAGE_TEMPLATES,
    "code_success": {
        "ru": (
            "🔑 Ваш код: {code}\n"
            "⏱ Код обновляется каждые 30 секунд — используйте его сразу.\n"
            "Доступ активен ещё: {expires_in}"
        ),
        "en": (
            "🔑 Your code: {code}\n"
            "⏱ The code refreshes every 30 seconds — use it immediately.\n"
            "Access remains active for: {expires_in}"
        ),
    },
    "code_expiring": {
        "ru": (
            "⏳ До окончания доступа осталось меньше минуты. "
            "Новый код входа уже не выдаётся."
        ),
        "en": (
            "⏳ Less than one minute remains. A new login code can no "
            "longer be issued."
        ),
    },
    "account_unavailable": {
        "ru": (
            "⚠️ Аккаунт временно недоступен. Используйте !замена или "
            "!продавец."
        ),
        "en": (
            "⚠️ The account is temporarily unavailable. Use !replace or "
            "!seller."
        ),
    },
    "delivery_pending": {
        "ru": (
            "⏳ Данные аккаунта ещё доставляются. Повторите команду после "
            "сообщения об успешной выдаче."
        ),
        "en": (
            "⏳ Account credentials are still being delivered. Retry after "
            "the successful delivery message."
        ),
    },
    "replace_expiring": {
        "ru": (
            "⏳ До окончания доступа меньше 2 минут. "
            "Безопасная замена аккаунта уже невозможна."
        ),
        "en": (
            "⏳ Less than 2 minutes of access remain. "
            "A safe account replacement is no longer possible."
        ),
    },
    "welcome": {
        "ru": (
            "✅ Заказ выполнен. ChatGPT {tier} на {duration}:\n\n"
            "Логин: {login}\n"
            "Пароль: {password}\n"
            "Тариф аккаунта до: {expires_at}\n"
            "Отсчёт доступа начнётся после доставки; точное окончание — "
            "!подписка.\n\n"
            "📊 Лимит Codex (общий для Work, Workspace Agents и Excel; "
            "обычный Chat не расходует его):\n"
            "• Основное окно: {codex_primary_limit}; {codex_primary_window}; "
            "сброс {codex_primary_reset}\n"
            "• Дополнительное окно: {codex_secondary_limit}; "
            "{codex_secondary_window}; сброс {codex_secondary_reset}\n\n"
            "📱 Для входа: !код | Помощь: !помощь | Замена: !замена"
        ),
        "en": (
            "✅ Order completed. ChatGPT {tier} for {duration}:\n\n"
            "Login: {login}\n"
            "Password: {password}\n"
            "Account plan until: {expires_at}\n"
            "Access starts after delivery; use !sub for the exact end time.\n\n"
            "📊 Shared agentic allowance:\n"
            "• Primary window: {codex_primary_limit}; {codex_primary_window}; "
            "resets {codex_primary_reset}\n"
            "• Secondary window: {codex_secondary_limit}; "
            "{codex_secondary_window}; resets {codex_secondary_reset}\n\n"
            "⚠️ This allowance is shared by Codex, Work, Workspace Agents and Excel.\n\n"
            "📱 To log in: !code | Help: !help | Replace: !replace"
        ),
    },
    "subscription": {
        "ru": (
            "📊 ChatGPT {tier}\n"
            "Тариф аккаунта до: {expires_at}\n"
            "Доступ до: {access_expires_at}\n"
            "Осталось: {expires_in}\n\n"
            "Лимит Codex (общий для Work, Workspace Agents и Excel; "
            "обычный Chat не расходует его):\n"
            "• Основное окно: {codex_primary_limit}; {codex_primary_window}; "
            "сброс {codex_primary_reset}\n"
            "• Дополнительное окно: {codex_secondary_limit}; "
            "{codex_secondary_window}; сброс {codex_secondary_reset}"
        ),
        "en": (
            "📊 ChatGPT {tier}\n"
            "Account plan until: {expires_at}\n"
            "Access until: {access_expires_at}\n"
            "Remaining: {expires_in}\n\n"
            "Shared agentic allowance:\n"
            "• Primary window: {codex_primary_limit}; {codex_primary_window}; "
            "resets {codex_primary_reset}\n"
            "• Secondary window: {codex_secondary_limit}; "
            "{codex_secondary_window}; resets {codex_secondary_reset}"
        ),
    },
    "subscription_limits_unavailable": {
        "ru": (
            "📊 ChatGPT {tier}\n"
            "Тариф аккаунта до: {expires_at}\n"
            "Доступ до: {access_expires_at}\n"
            "Осталось: {expires_in}\n\n"
            "⚠️ Лимиты сейчас не удалось обновить, поэтому устаревшие "
            "значения не показываются. Повторите команду позже или вызовите "
            "!продавец."
        ),
        "en": (
            "📊 ChatGPT {tier}\n"
            "Account plan until: {expires_at}\n"
            "Access until: {access_expires_at}\n"
            "Remaining: {expires_in}\n\n"
            "⚠️ Limits could not be refreshed, so stale values are hidden. "
            "Try again later or use !seller."
        ),
    },
    "replace_success": {
        "ru": (
            "🔄 Замена выполнена. Новые данные:\n\n"
            "Логин: {login}\n"
            "Пароль: {password}\n"
            "ChatGPT {tier}, доступ ещё на {duration}.\n"
            "Тариф аккаунта до: {expires_at}\n"
            "Доступ до: {access_expires_at}\n\n"
            "📊 Лимит Codex (общий для Work, Workspace Agents и Excel; "
            "обычный Chat не расходует его):\n"
            "• Основное окно: {codex_primary_limit}; {codex_primary_window}; "
            "сброс {codex_primary_reset}\n"
            "• Дополнительное окно: {codex_secondary_limit}; "
            "{codex_secondary_window}; сброс {codex_secondary_reset}\n\n"
            "📱 Для кода входа: !код"
        ),
        "en": (
            "🔄 Replacement done. New credentials:\n\n"
            "Login: {login}\n"
            "Password: {password}\n"
            "ChatGPT {tier}, access for another {duration}.\n"
            "Account plan until: {expires_at}\n"
            "Access until: {access_expires_at}\n\n"
            "📊 Shared agentic allowance:\n"
            "• Primary window: {codex_primary_limit}; {codex_primary_window}; "
            "resets {codex_primary_reset}\n"
            "• Secondary window: {codex_secondary_limit}; "
            "{codex_secondary_window}; resets {codex_secondary_reset}\n\n"
            "📱 For login code: !code"
        ),
    },
    "expiry": {
        "ru": (
            "⏰ Ваш доступ ({tier}, {duration}) закончился.\n"
            "Для продления — новый заказ."
        ),
        "en": (
            "⏰ Your access ({tier}, {duration}) has expired.\n"
            "To extend — new order."
        ),
    },
}


async def seed_catalog(
    session: AsyncSession,
    *,
    commit: bool = True,
    initialize_durations: bool | None = None,
) -> None:
    """Create the stable catalog defaults without overwriting operator data."""
    existing_tiers = (
        (await session.execute(select(SubscriptionTier))).scalars().all()
    )
    tiers_by_code = {tier.code: tier for tier in existing_tiers if tier.code}
    tiers_by_name = {tier.name: tier for tier in existing_tiers}
    for plan in SYSTEM_SUBSCRIPTION_PLANS:
        tier = tiers_by_code.get(plan.code) or tiers_by_name.get(plan.name)
        # Upgrade the old two-tier catalog without creating a second Pro row.
        if tier is None and plan.code == "pro_20x":
            tier = tiers_by_name.get("Pro")
            if tier is not None:
                tier.name = plan.name
        initialize_sellable = (
            tier is None or not tier.system_managed or not tier.code
        )
        if tier is None:
            tier = SubscriptionTier(
                code=plan.code,
                name=plan.name,
                description=plan.description,
                is_active=True,
                system_managed=True,
                is_sellable=plan.is_sellable,
                sort_order=plan.sort_order,
                usage_multiplier=plan.usage_multiplier,
            )
            session.add(tier)
        tier.code = plan.code
        tier.system_managed = True
        if initialize_sellable:
            tier.is_sellable = plan.is_sellable
        tier.sort_order = plan.sort_order
        tier.usage_multiplier = plan.usage_multiplier

    existing_durations = (
        (await session.execute(select(Duration))).scalars().all()
    )
    if initialize_durations is None:
        # Direct callers retain the original first-run behavior. Production
        # bootstrap passes an explicit flag based on whether SellerSettings
        # existed before startup, so a deliberately emptied catalog stays empty.
        initialize_durations = not existing_durations
    if initialize_durations and not existing_durations:
        for minutes in DEFAULT_DURATIONS:
            session.add(
                Duration(
                    minutes=minutes,
                    is_enabled=True,
                    sort_order=minutes,
                )
            )
    else:
        for duration in existing_durations:
            duration.sort_order = duration.minutes

    existing_scopes = (
        (await session.execute(select(LimitScope))).scalars().all()
    )
    scopes_by_code = {scope.code: scope for scope in existing_scopes}
    canonical_scope_order = {"any": 10, "codex": 20}
    for code, name in DEFAULT_LIMIT_SCOPES:
        scope = scopes_by_code.get(code)
        if scope is None:
            session.add(
                LimitScope(
                    code=code,
                    name=name,
                    is_enabled=DEFAULT_LIMIT_SCOPE_AVAILABILITY[code],
                    sort_order=canonical_scope_order[code],
                )
            )
        else:
            scope.sort_order = canonical_scope_order[code]
    # Any pre-migration or manually inserted non-canonical scope is never
    # eligible for a new offer. Historical rows remain readable through FKs.
    for scope in existing_scopes:
        if scope.code not in canonical_scope_order:
            scope.is_enabled = False
            scope.sort_order = 100

    if commit:
        await session.commit()
    else:
        await session.flush()


async def seed_message_templates(
    session: AsyncSession, *, commit: bool = True
) -> None:
    """Create missing templates and safely upgrade exact legacy defaults.

    Operator-edited content is preserved. A stored template is upgraded only
    when it is byte-for-byte equal to the old bundled default that described
    Codex limits as fixed 5-hour/weekly windows.
    """
    for key, translations in DEFAULT_MESSAGE_TEMPLATES.items():
        for lang, content in translations.items():
            result = await session.execute(
                select(MessageTemplate).where(
                    MessageTemplate.key == key,
                    MessageTemplate.lang == lang,
                )
            )
            existing = result.scalar_one_or_none()
            if existing is None:
                session.add(MessageTemplate(key=key, lang=lang, content=content))
                continue
            upgrade_sources = {
                value
                for value in (
                    LEGACY_LIMIT_MESSAGE_TEMPLATES.get(key, {}).get(lang),
                    PRE_AGENTIC_MESSAGE_TEMPLATES.get(key, {}).get(lang),
                )
                if value is not None
            }
            if existing.content in upgrade_sources:
                existing.content = content
    if commit:
        await session.commit()
    else:
        await session.flush()


async def seed_lot_templates(
    session: AsyncSession, *, commit: bool = True
) -> None:
    """Seed system lot templates without overwriting operator customizations."""
    for key, default in DEFAULT_LOT_TEMPLATES.items():
        validate_lot_template_values(
            title_ru=default.title_ru,
            title_en=default.title_en,
            description_ru=default.description_ru,
            description_en=default.description_en,
        )
        existing = (
            await session.execute(
                select(LotTemplate).where(LotTemplate.key == key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            # Identity metadata and availability are system invariants;
            # editable content remains entirely operator-owned.
            existing.name = default.name
            existing.system_managed = True
            existing.is_enabled = True
            legacy = LEGACY_DAY_LOT_TEMPLATES.get(key)
            if legacy is not None and all(
                (
                    existing.title_template_ru == legacy.title_ru,
                    existing.title_template_en == legacy.title_en,
                    existing.description_template_ru == legacy.description_ru,
                    existing.description_template_en == legacy.description_en,
                )
            ):
                existing.title_template_ru = default.title_ru
                existing.title_template_en = default.title_en
                existing.description_template_ru = default.description_ru
                existing.description_template_en = default.description_en
            continue
        session.add(
            LotTemplate(
                key=key,
                name=default.name,
                tier_id=None,
                limit_scope_id=None,
                title_template_ru=default.title_ru,
                title_template_en=default.title_en,
                description_template_ru=default.description_ru,
                description_template_en=default.description_en,
                is_enabled=True,
                system_managed=True,
            )
        )
    if commit:
        await session.commit()
    else:
        await session.flush()
