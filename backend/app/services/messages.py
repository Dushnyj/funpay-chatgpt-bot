from __future__ import annotations

from datetime import datetime, timezone
from string import Formatter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import AccountLimits
from app.models.audit import AuditLog
from app.models.message import MessageTemplate
from app.models.rental import Rental


SUPPORTED_TEMPLATE_LANGS = frozenset({"ru", "en"})

_USAGE_FIELDS = frozenset(
    {
        # Codex aliases remain available for operator templates created before
        # exact OpenAI usage windows were stored. They represent the same
        # agentic pool, not a separate product allowance.
        "codex_5h",
        "codex_weekly",
        # Exact, display-ready Codex observations.
        "codex_primary_limit",
        "codex_primary_window",
        "codex_primary_reset",
        "codex_secondary_limit",
        "codex_secondary_window",
        "codex_secondary_reset",
        # Compact localized block. Unlike the individual compatibility fields,
        # this omits an unobserved secondary window instead of printing dashes.
        "codex_usage_summary",
    }
)

# Hidden parser compatibility only. These placeholders are not returned in
# ``allowed_fields`` and always render as unavailable because no Chat allowance
# exists in the active model. This prevents an operator-edited legacy template
# from breaking credential delivery during the migration.
_DEPRECATED_CHAT_TEMPLATE_FIELDS = frozenset({"chat_5h", "chat_weekly"})
_USAGE_TEMPLATE_KEYS = frozenset({"welcome", "subscription", "replace_success"})

TEMPLATE_FIELDS_BY_KEY: dict[str, frozenset[str]] = {
    "welcome": frozenset(
        {
            "login",
            "password",
            "tier",
            "duration",
            "duration_minutes",
            "days",
            "expires_at",
        }
    )
    | _USAGE_FIELDS,
    "code_success": frozenset({"code", "expires_in"}),
    "code_expiring": frozenset(),
    "account_unavailable": frozenset(),
    "delivery_pending": frozenset(),
    "code_expired": frozenset(),
    "rental_ambiguous": frozenset({"active_orders"}),
    "code_rate_limited": frozenset({"retry_in_sec"}),
    "code_delivery_uncertain": frozenset(
        {"retry_in_sec", "retry_command"}
    ),
    "email_code_success": frozenset({"email_code"}),
    "email_code_duplicate": frozenset(),
    "email_code_not_found": frozenset(),
    "email_code_unavailable": frozenset(),
    "subscription": frozenset(
        {"tier", "expires_at", "access_expires_at", "expires_in"}
    )
    | _USAGE_FIELDS,
    "subscription_limits_unavailable": frozenset(
        {"tier", "expires_at", "access_expires_at", "expires_in"}
    ),
    "replace_success": frozenset(
        {
            "login",
            "password",
            "tier",
            "duration",
            "duration_minutes",
            "days",
            "expires_at",
            "access_expires_at",
        }
    )
    | _USAGE_FIELDS,
    "replace_declined": frozenset(),
    "replace_expiring": frozenset(),
    "replace_no_account": frozenset(),
    "seller_required": frozenset(),
    "seller_called": frozenset(),
    "help": frozenset(),
    "order_confirmed": frozenset(),
    "expiry": frozenset({"tier", "duration", "duration_minutes", "days"}),
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
    accepted = (
        allowed | _DEPRECATED_CHAT_TEMPLATE_FIELDS
        if key in _USAGE_TEMPLATE_KEYS
        else allowed
    )
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
        if field_name not in accepted:
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
    if "days" in used and "duration_minutes" in variables:
        duration_minutes = int(variables["duration_minutes"])
        if duration_minutes % (24 * 60) != 0:
            # Delivery must not fail after a buyer has paid merely because an
            # operator-edited legacy template still uses {days}. Render the
            # exact fraction and leave a durable warning for the operator.
            session.add(
                AuditLog(
                    event_type="deprecated_days_template_rendered",
                    metadata_={
                        "template": f"{template.key}/{template.lang}",
                        "duration_minutes": duration_minutes,
                    },
                )
            )
    return template.content.format_map(variables)


def usage_template_variables(
    limits: AccountLimits | None,
    *,
    lang: str,
) -> dict[str, str]:
    """Build compatible and exact agentic usage variables for templates.

    The window labels are derived solely from the observed ``window_seconds``.
    Thus a Free 30-day window is rendered as 30 days while a paid 7-day window
    is rendered as 7 days, without inferring either value from a plan name.
    """
    return {
        "chat_5h": "—",
        "chat_weekly": "—",
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
        "codex_usage_summary": _format_usage_summary(
            primary_remaining=(
                limits.codex_primary_remaining_pct if limits else None
            ),
            primary_window=(
                limits.codex_primary_window_seconds if limits else None
            ),
            primary_reset=(limits.codex_primary_resets_at if limits else None),
            secondary_remaining=(
                limits.codex_secondary_remaining_pct if limits else None
            ),
            secondary_window=(
                limits.codex_secondary_window_seconds if limits else None
            ),
            secondary_reset=(
                limits.codex_secondary_resets_at if limits else None
            ),
            lang=lang,
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
        "chat_5h": "—",
        "chat_weekly": "—",
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
        "codex_usage_summary": _format_usage_summary(
            primary_remaining=rental.issued_codex_primary_pct,
            primary_window=rental.issued_codex_primary_window_seconds,
            primary_reset=rental.issued_codex_primary_resets_at,
            secondary_remaining=rental.issued_codex_secondary_pct,
            secondary_window=rental.issued_codex_secondary_window_seconds,
            secondary_reset=rental.issued_codex_secondary_resets_at,
            lang=lang,
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


def _format_usage_summary(
    *,
    primary_remaining: int | None,
    primary_window: int | None,
    primary_reset: datetime | None,
    secondary_remaining: int | None,
    secondary_window: int | None,
    secondary_reset: datetime | None,
    lang: str,
) -> str:
    """Render only the OpenAI usage windows that were actually observed."""

    windows = (
        (
            "Основное окно" if lang != "en" else "Primary window",
            primary_remaining,
            primary_window,
            primary_reset,
        ),
        (
            "Дополнительное окно" if lang != "en" else "Secondary window",
            secondary_remaining,
            secondary_window,
            secondary_reset,
        ),
    )
    lines: list[str] = []
    for label, remaining, window, reset in windows:
        if remaining is None and not window and reset is None:
            continue
        limit_text = _exact_percentage(remaining)
        window_text = _format_window(window, lang)
        reset_text = _format_reset(reset, lang)
        reset_label = "resets" if lang == "en" else "сброс"
        lines.append(
            f"{label}: {limit_text} · {window_text}; {reset_label} {reset_text}"
        )
    if lines:
        return "\n".join(lines)
    return (
        "Data is currently unavailable."
        if lang == "en"
        else "Данные пока недоступны."
    )


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
