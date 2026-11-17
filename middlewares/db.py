"""
``DbSessionMiddleware`` — outer-middleware уровня dispatcher, которая
открывает :class:`AsyncSession` на каждый апдейт и кладёт её в
``data['session']``. Хэндлеры могут принимать сессию параметром:

.. code-block:: python

    async def my_handler(message: Message, session: AsyncSession) -> None:
        users = UserRepository(session)
        ...

Семантика транзакций::

    * Хэндлер выполнен без исключений → ``commit``.
    * Хэндлер бросил исключение → ``rollback`` и проброс наружу.
    * Сессия всегда закрывается.

Это снимает с хэндлеров обязанность явно коммитить — но при необходимости
можно вызвать ``await session.commit()`` внутри (например, чтобы зафиксировать
часть работы до отправки длинного сообщения, не блокируя транзакцией остальное).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import async_sessionmaker

from database.session import async_session_factory

logger = logging.getLogger(__name__)


class DbSessionMiddleware(BaseMiddleware):
    """Инжектит async-сессию SQLAlchemy в ``data['session']``."""

    def __init__(
        self,
        session_factory: async_sessionmaker | None = None,
    ) -> None:
        self._session_factory = session_factory or async_session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self._session_factory() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
            except Exception:
                await session.rollback()
                raise
            else:
                await session.commit()
                return result
