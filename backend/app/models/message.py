from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MessageTemplate(Base):
    __tablename__ = "message_templates"
    # Каждый шаблон уникален в пределах связки «ключ × язык»: ru/en версии одного ключа
    __table_args__ = (
        UniqueConstraint("key", "lang", name="uq_message_key_lang"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(32))
    lang: Mapped[str] = mapped_column(String(8))
    content: Mapped[str] = mapped_column(String(4000))
