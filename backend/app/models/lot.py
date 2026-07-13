from datetime import datetime
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Сигнальное значение для нормализации NULL в порогах: NULL в стандартном SQL
# считается уникальным внутри UNIQUE-индекса, поэтому дублирующие конфиги с NULL-порогами
# не детектируются. Нормализуем NULL → -1 в отдельной сигнатуре.
_NULL_SENTINEL = -1


def _config_signature(
    tier_id: int,
    duration_id: int,
    limit_scope_id: int,
    min_limit_pct: int | None,
    max_5h_pct: int | None,
    max_weekly_pct: int | None,
) -> str:
    """Стабильное строковое представление конфигурации для уникальности.

    NULL-пороги нормализуются к сигналу, чтобы два одинаковых конфига
    (включая «без порогов») детектировались UNIQUE-индексом.
    """
    norm = lambda v: _NULL_SENTINEL if v is None else v  # noqa: E731
    return f"{tier_id}|{duration_id}|{limit_scope_id}|{norm(min_limit_pct)}|{norm(max_5h_pct)}|{norm(max_weekly_pct)}"


class Lot(Base):
    __tablename__ = "lots"

    id: Mapped[int] = mapped_column(primary_key=True)
    funpay_id: Mapped[str | None] = mapped_column(default=None)
    # Stable bot-only identity embedded into the published description.  It
    # survives title/price/status edits and lets an immutable order page prove
    # which local lot was actually sold when FunPay omits the offer id.
    provenance_token: Mapped[str] = mapped_column(
        String(32),
        unique=True,
        nullable=False,
        default=lambda: uuid.uuid4().hex,
    )
    provenance_marker_synced: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    funpay_node_id: Mapped[int | None] = mapped_column(default=None)
    tier_id: Mapped[int] = mapped_column(ForeignKey("subscription_tiers.id"))
    duration_id: Mapped[int] = mapped_column(ForeignKey("durations.id"))
    limit_scope_id: Mapped[int] = mapped_column(ForeignKey("limit_scopes.id"))
    min_limit_pct: Mapped[int | None] = mapped_column(default=None)
    max_5h_pct: Mapped[int | None] = mapped_column(default=None)
    max_weekly_pct: Mapped[int | None] = mapped_column(default=None)
    price: Mapped[int] = mapped_column(Integer)
    title_ru: Mapped[str] = mapped_column(String(255))
    title_en: Mapped[str] = mapped_column(String(255))
    description_ru: Mapped[str] = mapped_column(String(4000), default="")
    description_en: Mapped[str] = mapped_column(String(4000), default="")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | paused | deleted
    paused_reason: Mapped[str | None] = mapped_column(default=None)
    auto_created: Mapped[bool] = mapped_column(Boolean, default=False)
    # Гарантирует уникальность связки «тип × срок × scope × пороги» с учётом NULL-нормализации
    config_key: Mapped[str] = mapped_column(String(96), unique=True)

    def __init__(self, **kwargs):
        # Вычисляем сигнатуру до создания объекта, если она не передана явно
        if "config_key" not in kwargs:
            needed = ("tier_id", "duration_id", "limit_scope_id")
            if all(k in kwargs for k in needed):
                kwargs["config_key"] = _config_signature(
                    kwargs["tier_id"], kwargs["duration_id"], kwargs["limit_scope_id"],
                    kwargs.get("min_limit_pct"), kwargs.get("max_5h_pct"), kwargs.get("max_weekly_pct"),
                )
        super().__init__(**kwargs)


class PriceMatrix(Base):
    __tablename__ = "price_matrix"

    id: Mapped[int] = mapped_column(primary_key=True)
    tier_id: Mapped[int] = mapped_column(ForeignKey("subscription_tiers.id"))
    duration_id: Mapped[int] = mapped_column(ForeignKey("durations.id"))
    limit_scope_id: Mapped[int] = mapped_column(ForeignKey("limit_scopes.id"))
    min_limit_pct: Mapped[int | None] = mapped_column(default=None)
    max_5h_pct: Mapped[int | None] = mapped_column(default=None)
    max_weekly_pct: Mapped[int | None] = mapped_column(default=None)
    price: Mapped[int] = mapped_column(Integer)
    # Цена задаётся per связку; NULL-нормализация обеспечит детект дубликата
    config_key: Mapped[str] = mapped_column(String(96), unique=True)

    def __init__(self, **kwargs):
        if "config_key" not in kwargs:
            needed = ("tier_id", "duration_id", "limit_scope_id")
            if all(k in kwargs for k in needed):
                kwargs["config_key"] = _config_signature(
                    kwargs["tier_id"], kwargs["duration_id"], kwargs["limit_scope_id"],
                    kwargs.get("min_limit_pct"), kwargs.get("max_5h_pct"), kwargs.get("max_weekly_pct"),
                )
        super().__init__(**kwargs)


class LotTemplate(Base):
    __tablename__ = "lot_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stable API identity.  It is deliberately independent from localized
    # labels so an operator can rename a template without breaking links.
    key: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    tier_id: Mapped[int | None] = mapped_column(ForeignKey("subscription_tiers.id"), default=None)
    limit_scope_id: Mapped[int | None] = mapped_column(ForeignKey("limit_scopes.id"), default=None)
    title_template_ru: Mapped[str] = mapped_column(String(255))
    title_template_en: Mapped[str] = mapped_column(String(255))
    description_template_ru: Mapped[str] = mapped_column(String(4000), default="")
    description_template_en: Mapped[str] = mapped_column(String(4000), default="")
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    system_managed: Mapped[bool] = mapped_column(Boolean, default=False)


Index(
    "uq_lot_templates_enabled_custom_target",
    func.coalesce(LotTemplate.tier_id, 0),
    func.coalesce(LotTemplate.limit_scope_id, 0),
    unique=True,
    postgresql_where=text("system_managed = false AND is_enabled = true"),
    sqlite_where=text("system_managed = 0 AND is_enabled = 1"),
)


class BumpLog(Base):
    __tablename__ = "bump_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.id", ondelete="CASCADE"))
    bumped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    success: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(default=None)
