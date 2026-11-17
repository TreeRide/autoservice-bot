"""
Глобальные middlewares уровня диспетчера.

* :class:`MenuResetMiddleware` — аварийный сброс FSM при нажатии
  reply-кнопок меню.
* :class:`DbSessionMiddleware` — инжектит ``AsyncSession`` (из
  :data:`database.session.async_session_factory`) в ``data['session']``
  каждого апдейта; после хэндлера коммитит транзакцию, при исключении —
  rollback.
"""

from .db import DbSessionMiddleware
from .menu_reset import (
    ADMIN_REPLY_BUTTONS,
    ALL_REPLY_BUTTONS,
    CLIENT_REPLY_BUTTONS,
    MenuResetMiddleware,
)

__all__ = [
    "ADMIN_REPLY_BUTTONS",
    "ALL_REPLY_BUTTONS",
    "CLIENT_REPLY_BUTTONS",
    "DbSessionMiddleware",
    "MenuResetMiddleware",
]
