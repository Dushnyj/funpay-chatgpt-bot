from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import MessageTemplate


async def render_message(session: AsyncSession, key: str, lang: str, **variables: object) -> str:
    """Рендерит шаблон сообщения с подстановкой переменных.

    Ищет шаблон по (key, lang). При отсутствии — fallback на ru,
    чтобы покупатели с нераспознанной локалью всё равно получали ответ.
    """
    template = await _find_template(session, key, lang)
    return template.content.format(**variables)


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
