from __future__ import annotations

from datetime import datetime, timezone
from string import Formatter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import AccountLimits
from app.models.message import MessageTemplate
from app.models.rental import Rental


SUPPORTED_TEMPLATE_LANGS = frozenset({"ru", "en"})

_USAGE_FIELDS = frozenset(
    {
        # Legacy variables remain available for operator templates created
        # before exact OpenAI usage windows were stored.
        "chat_5h",
        "chat_weekly",
        "codex_5h",
        "codex_weekly",
        # Exact, display-ready Codex observations.
        "codex_primary_limit",
        "codex_primary_window",
        "codex_primary_reset",
        "codex_secondary_limit",
        "codex_secondary_window",
        "codex_secondary_reset",
    }
)

TEMPLATE_FIELDS_BY_KEY: dict[str, frozenset[str]] = {
    "welcome": frozenset(
        {"login", "password", "tier", "days", "expires_at"}
    )
    | _USAGE_FIELDS,
    "code_success": frozenset({"code", "expires_in"}),
    "code_expired": frozenset(),
    "rental_ambiguous": frozenset(),
    "code_rate_limited": frozenset({"retry_in_sec"}),
    "email_code_success": frozenset({"email_code"}),
    "email_code_duplicate": frozenset(),
    "email_code_not_found": frozenset(),
    "email_code_unavailable": frozenset(),
    "subscription": frozenset({"tier", "expires_at", "expires_in"})
    | _USAGE_FIELDS,
    "replace_success": frozenset(
        {"login", "password", "tier", "days", "expires_at"}
    )
    | _USAGE_FIELDS,
    "replace_declined": frozenset(),
    "replace_no_account": frozenset(),
    "seller_called": frozenset(),
    "help": frozenset(),
    "order_confirmed": frozenset(),
    "expiry": frozenset({"tier", "days"}),
    "disconnect": frozenset({"expires_in"}),
    "no_account_available": frozenset({"retry_minutes"}),
}

REQUIRED_TEMPLATE_FIELDS_BY_KEY: dict[str, frozenset[str]] = {
    "welcome": frozenset({"login", "password"}),
    "replace_success": frozenset({"login", "password"}),
    "code_success": frozenset({"code"}),
    "email_code_success": frozenset({"email_code"}),
}

_FORMATTER = Formatter()


class TemplateValidationError(ValueError):
    """A message template is malformed or uses an unsafe placeholder."""


class TemplateRenderError(ValueError):
    """A valid template cannot be rendered with the supplied variables."""


def allowed_template_fields(key: str, lang: str) -> frozenset[str]:
    """Return the explicit placeholder whitelist for a template/language."""
    if lang not in SUPPORTED_TEMPLATE_LANGS:
        raise TemplateValidationError(
            f"Unsupported template language {lang!r}; allowed: en, ru"
        )
    fields = TEMPLATE_FIELDS_BY_KEY.get(key)
    if fields is None:
        supported = ", ".join(sorted(TEMPLATE_FIELDS_BY_KEY))
        raise TemplateValidationError(
            f"Unknown template key {key!r}; allowed: {supported}"
        )
    return fields


def validate_template_content(key: str, lang: str, content: str) -> frozenset[str]:
    """Validate ``str.format`` syntax and return placeholders used by content.

    Attribute/index access, conversions and format specifications are rejected:
    all values passed to templates are already display-ready strings, and these
    advanced format features can expose object internals or create fragile
    runtime-only failures.
    """
    allowed = allowed_template_fields(key, lang)
    used: set[str] = set()
    try:
        parts = list(_FORMATTER.parse(content))
    except ValueError as exc:
        raise TemplateValidationError(
            f"Invalid placeholder syntax in {key}/{lang}: {exc}"
        ) from exc

    for _literal, field_name, format_spec, conversion in parts:
        if field_name is None:
            continue
        if not field_name:
            raise TemplateValidationError(
                f"Positional placeholders are not allowed in {key}/{lang}"
            )
        if field_name not in allowed:
            allowed_display = ", ".join(f"{{{name}}}" for name in sorted(allowed))
            raise TemplateValidationError(
                f"Unknown placeholder {{{field_name}}} in {key}/{lang}. "
                f"Allowed: {allowed_display or 'none'}"
            )
        if conversion:
            raise TemplateValidationError(
                f"Placeholder conversion !{conversion} is not allowed in "
                f"{key}/{lang}"
            )
        if format_spec:
            raise TemplateValidationError(
                f"Format specification is not allowed for {{{field_name}}} "
                f"in {key}/{lang}"
            )
        used.add(field_name)
    required = REQUIRED_TEMPLATE_FIELDS_BY_KEY.get(key, frozenset())
    missing_required = required.difference(used)
    if missing_required:
        required_display = ", ".join(
            f"{{{name}}}" for name in sorted(missing_required)
        )
        raise TemplateValidationError(
            f"Template {key}/{lang} must include: {required_display}"
        )
    return frozenset(used)


async def render_message(
    session: AsyncSession,
    key: str,
    lang: str,
    **variables: object,
) -> str:
    """Рендерит шаблон сообщения с подстановкой переменных.

    Ищет шаблон по (key, lang). При отсутствии — fallback на ru,
    чтобы покупатели с нераспознанной локалью всё равно получали ответ.
    """
    template = await _find_template(session, key, lang)
    used = validate_template_content(template.key, template.lang, template.content)
    missing = used.difference(variables)
    if missing:
        missing_display = ", ".join(f"{{{name}}}" for name in sorted(missing))
        raise TemplateRenderError(
            f"Missing variables for {template.key}/{template.lang}: "
            f"{missing_display}"
        )
    return template.content.format_map(variables)


def usage_template_variables(
    limits: AccountLimits | None,
    *,
    lang: str,
) -> dict[str, str]:
    """Build legacy and exact usage variables for buyer-facing templates.

    The window labels are derived solely from the observed ``window_seconds``.
    Thus a Free 30-day window is rendered as 30 days while a paid 7-day window
    is rendered as 7 days, without inferring either value from a plan name.
    """
    return {
        "chat_5h": _legacy_percentage(limits, "chat_5h"),
        "chat_weekly": _legacy_percentage(limits, "chat_weekly"),
        "codex_5h": _legacy_percentage(limits, "codex_5h"),
        "codex_weekly": _legacy_percentage(limits, "codex_weekly"),
        "codex_primary_limit": _exact_percentage(
            limits.codex_primary_remaining_pct if limits else None
        ),
        "codex_primary_window": _format_window(
            limits.codex_primary_window_seconds if limits else None, lang
        ),
        "codex_primary_reset": _format_reset(
            limits.codex_primary_resets_at if limits else None, lang
        ),
        "codex_secondary_limit": _exact_percentage(
            limits.codex_secondary_remaining_pct if limits else None
        ),
        "codex_secondary_window": _format_window(
            limits.codex_secondary_window_seconds if limits else None, lang
        ),
        "codex_secondary_reset": _format_reset(
            limits.codex_secondary_resets_at if limits else None, lang
        ),
    }


def issued_usage_template_variables(
    rental: Rental,
    *,
    lang: str,
) -> dict[str, str]:
    """Build buyer-facing variables from the rental's durable limit snapshot.

    Credential delivery can be retried after the live usage row changes. The
    message must still match the exact values claimed and shown in the admin
    panel for this issuance or replacement.
    """
    return {
        "chat_5h": _legacy_value(rental.issued_chat_5h_pct),
        "chat_weekly": _legacy_value(rental.issued_chat_weekly_pct),
        "codex_5h": _legacy_value(rental.issued_codex_5h_pct),
        "codex_weekly": _legacy_value(rental.issued_codex_weekly_pct),
        "codex_primary_limit": _exact_percentage(
            rental.issued_codex_primary_pct
        ),
        "codex_primary_window": _format_window(
            rental.issued_codex_primary_window_seconds, lang
        ),
        "codex_primary_reset": _format_reset(
            rental.issued_codex_primary_resets_at, lang
        ),
        "codex_secondary_limit": _exact_percentage(
            rental.issued_codex_secondary_pct
        ),
        "codex_secondary_window": _format_window(
            rental.issued_codex_secondary_window_seconds, lang
        ),
        "codex_secondary_reset": _format_reset(
            rental.issued_codex_secondary_resets_at, lang
        ),
    }


def _legacy_percentage(limits: AccountLimits | None, field: str) -> str:
    if limits is None:
        return "—"
    value = getattr(limits, f"{field}_remaining_pct", None)
    return _legacy_value(value)


def _legacy_value(value: int | None) -> str:
    # Legacy templates already contain a trailing percent sign.
    return str(value) if value is not None else "—"


def _exact_percentage(value: int | None) -> str:
    return f"{value}%" if value is not None else "—"


def _format_window(seconds: int | None, lang: str) -> str:
    if seconds is None or seconds <= 0:
        return "—"
    units = (
        (86_400, "дн.", "days"),
        (3_600, "ч", "hours"),
        (60, "мин.", "minutes"),
    )
    for unit_seconds, ru_label, en_label in units:
        if seconds % unit_seconds == 0:
            value = seconds // unit_seconds
            # _find_template falls back to Russian for an unknown locale.
            label = en_label if lang == "en" else ru_label
            return f"{value} {label}"
    label = "seconds" if lang == "en" else "сек."
    return f"{seconds} {label}"


def _format_reset(value: datetime | None, lang: str) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    if lang == "en":
        return value.strftime("%Y-%m-%d %H:%M UTC")
    return value.strftime("%d.%m.%Y %H:%M UTC")


async def _find_template(session: AsyncSession, key: str, lang: str) -> MessageTemplate:
    result = await session.execute(
        select(MessageTemplate).where(
            MessageTemplate.key == key,
            MessageTemplate.lang == lang,
        )
    )
    template = result.scalar_one_or_none()
    if template is not None:
        return template

    # Fallback на ru — базовый язык, на котором существуют все шаблоны
    if lang != "ru":
        result = await session.execute(
            select(MessageTemplate).where(
                MessageTemplate.key == key,
                MessageTemplate.lang == "ru",
            )
        )
        template = result.scalar_one_or_none()
        if template is not None:
            return template

    raise ValueError(f"MessageTemplate not found: key={key}, lang={lang}")
