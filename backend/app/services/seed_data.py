from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Duration, LimitScope, SubscriptionTier
from app.models.message import MessageTemplate
from app.services.subscription_plans import SYSTEM_SUBSCRIPTION_PLANS


DEFAULT_TIERS: tuple[tuple[str, str], ...] = tuple(
    (plan.name, plan.description) for plan in SYSTEM_SUBSCRIPTION_PLANS
)
DEFAULT_DURATIONS: tuple[int, ...] = (1, 3, 5, 7, 15, 30)
DEFAULT_LIMIT_SCOPES: tuple[tuple[str, str], ...] = (
    ("any", "Без гарантии лимита"),
    ("chat", "Chat"),
    ("codex", "Codex"),
)

# Полный перечень ключей из спеки (секция MessageTemplate).
# Каждый ключ имеет ru и en варианты с плейсхолдерами для str.format.
DEFAULT_MESSAGE_TEMPLATES: dict[str, dict[str, str]] = {
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
    "code_success": {
        "ru": "🔑 Ваш код: {code}\n⏱ Действителен 30 секунд.\nПодписка активна ещё: {expires_in}",
        "en": "🔑 Your code: {code}\n⏱ Valid for 30 seconds.\nSubscription active: {expires_in}",
    },
    "code_expired": {
        "ru": "❌ Доступ закончился. Для продления — новый заказ.",
        "en": "❌ Access expired. To extend — place a new order.",
    },
    "code_rate_limited": {
        "ru": "⏳ Подождите {retry_in_sec} сек. перед запросом нового кода.",
        "en": "⏳ Wait {retry_in_sec} sec. before requesting a new code.",
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


async def seed_catalog(session: AsyncSession, *, commit: bool = True) -> None:
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

    existing_durations = set(
        (await session.execute(select(Duration.days))).scalars().all()
    )
    for sort_order, days in enumerate(DEFAULT_DURATIONS):
        if days not in existing_durations:
            session.add(Duration(days=days, is_enabled=True, sort_order=sort_order))

    existing_scopes = set(
        (await session.execute(select(LimitScope.code))).scalars().all()
    )
    for code, name in DEFAULT_LIMIT_SCOPES:
        if code not in existing_scopes:
            session.add(LimitScope(code=code, name=name))

    if commit:
        await session.commit()
    else:
        await session.flush()


async def seed_message_templates(
    session: AsyncSession, *, commit: bool = True
) -> None:
    """Заполняет таблицу MessageTemplate дефолтными значениями, если их нет.

    Идемпотентна: существующие шаблоны не перезаписываются, чтобы
    ручные правки оператора сохранялись при повторном запуске.
    """
    for key, translations in DEFAULT_MESSAGE_TEMPLATES.items():
        for lang, content in translations.items():
            existing = await session.execute(
                select(MessageTemplate).where(
                    MessageTemplate.key == key,
                    MessageTemplate.lang == lang,
                )
            )
            if existing.scalar_one_or_none() is None:
                session.add(MessageTemplate(key=key, lang=lang, content=content))
    if commit:
        await session.commit()
    else:
        await session.flush()
