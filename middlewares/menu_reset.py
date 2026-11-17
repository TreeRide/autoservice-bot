"""
Глобальные middlewares уровня диспетчера.

:class:`MenuResetMiddleware` — аварийный сброс FSM при нажатии любой
reply-кнопки главного меню. Регистрируется на ``dp.message.middleware()``
(см. :mod:`main`), поэтому отрабатывает раньше любых роутеров и
state-фильтров. Если текст входящего сообщения совпадает с одной из
известных reply-кнопок и у пользователя есть активный FSM — состояние
очищается, и управление пробрасывается в целевой хэндлер кнопки.
В противном случае событие проходит насквозь без изменений.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, TelegramObject

logger = logging.getLogger(__name__)


# Все reply-кнопки, которые должны действовать как «Домой».
# Импортируются по строке, чтобы не плодить circular imports с handlers/.
ADMIN_REPLY_BUTTONS: frozenset[str] = frozenset(
    {
        "👤 Добавить клиента",
        "📝 Добавить запись ТО",
        "🗂 База клиентов",
        "📋 Последние 5 записей",
        "📥 Заявки",
    }
)

CLIENT_REPLY_BUTTONS: frozenset[str] = frozenset(
    {
        "🔢 Обновить мой пробег",
        "📖 Моя сервисная книжка",
        "📞 Заказать звонок",
    }
)

ALL_REPLY_BUTTONS: frozenset[str] = ADMIN_REPLY_BUTTONS | CLIENT_REPLY_BUTTONS


class MenuResetMiddleware(BaseMiddleware):
    """Сбросить активный FSM при нажатии любой reply-кнопки меню.

    * Если ``event.text`` ∈ :data:`ALL_REPLY_BUTTONS` и у пользователя
      есть активное состояние — ``await state.clear()``.
    * Управление **всегда** передаётся дальше через
      ``await handler(event, data)`` — текст не «съедается».
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.text:
            text = event.text.strip()
            user_id = event.from_user.id if event.from_user else "?"
            if text in ALL_REPLY_BUTTONS:
                state: FSMContext | None = data.get("state")
                if state is not None:
                    current = await state.get_state()
                    if current is not None:
                        await state.clear()
                        # КРИТИЧНО: aiogram FSMContextMiddleware кэширует
                        # raw_state в data на старте обработки апдейта, и
                        # все последующие StateFilter сверяются именно с
                        # этим кэшем. После state.clear() надо вручную
                        # синхронизировать кэш, иначе FSM-хэндлеры всё
                        # равно сматчатся.
                        data["raw_state"] = None
                        logger.info(
                            "FSM CLEARED FOR BUTTON '%s' (was=%s, user=%s)",
                            text,
                            current,
                            user_id,
                        )
        return await handler(event, data)
