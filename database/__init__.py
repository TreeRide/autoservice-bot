"""
Новый слой данных проекта на SQLAlchemy 2.0 (async) + Alembic.

Этот пакет существует **параллельно** старому модулю :mod:`db`.
В рантайме бот по-прежнему использует ``db.py``; переключение на
SQLAlchemy будет произведено отдельной фазой.

Состав:

* :mod:`database.models` — декларативные ORM-модели
  (``Base.metadata`` → описание схемы для Alembic).
* :mod:`database.session` — асинхронный engine и фабрика сессий.
* :mod:`database.repositories` — репозитории
  (``UserRepository``, ``RecordRepository``, ``AdminRepository`` и т.д.).
"""

from __future__ import annotations

from .models import (
    AdminRow,
    Base,
    NotificationLogRow,
    ServiceRequestRow,
    UserHistoryRow,
    UserRow,
)
from .session import (
    SQLITE_DATABASE_URL,
    async_session_factory,
    create_async_engine_for,
    engine,
    get_session,
)

__all__ = [
    "AdminRow",
    "Base",
    "NotificationLogRow",
    "ServiceRequestRow",
    "UserHistoryRow",
    "UserRow",
    "SQLITE_DATABASE_URL",
    "async_session_factory",
    "create_async_engine_for",
    "engine",
    "get_session",
]
