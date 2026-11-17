"""
Кастомные фильтры aiogram.

:class:`IsAdmin` пропускает апдейты только от пользователей,
чей ``telegram_id`` присутствует в таблице ``admins``. Источник истины —
:class:`database.repositories.AdminRepository`, на TTL-кэше 5 минут,
поэтому фильтр дёргает SELECT не чаще раза в 5 минут на процесс.

Сессия SQLAlchemy достаётся из ``data['session']`` (инжектится
:class:`middlewares.DbSessionMiddleware`). Если сессии в data нет —
фильтр fallback'ом открывает короткую через ``async_session_factory``,
чтобы корректно работать в местах, куда middleware не дотянулся
(например, ручные тесты).
"""

from __future__ import annotations

from typing import Any

from aiogram.filters import BaseFilter
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from database.repositories import AdminRepository
from database.session import async_session_factory


class IsAdmin(BaseFilter):
    """Истина, если автор сообщения — администратор."""

    async def __call__(
        self, message: Message, **data: Any
    ) -> bool:
        if message.from_user is None:
            return False
        session: AsyncSession | None = data.get("session")
        if session is not None:
            return await AdminRepository(session).is_admin(message.from_user.id)
        # Fallback: middleware не доехал — открываем короткую сессию.
        async with async_session_factory() as fallback_session:
            return await AdminRepository(fallback_session).is_admin(
                message.from_user.id
            )
