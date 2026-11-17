"""
Асинхронный SQLAlchemy engine и фабрика сессий для SQLite-базы проекта.

URL формируется из :data:`config.DB_PATH` — то есть указывает на тот же
файл ``service_bot.db``, с которым сейчас работает легаси-модуль
:mod:`db`. Это позволит будущим репозиториям (см.
:mod:`database.repositories`) читать/писать те же данные.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config import DB_PATH


def _sqlite_url_for(db_path: Path | str) -> str:
    """Сформировать ``sqlite+aiosqlite://...`` URL для абсолютного пути."""
    abs_path = Path(db_path).resolve()
    return f"sqlite+aiosqlite:///{abs_path}"


SQLITE_DATABASE_URL: str = _sqlite_url_for(DB_PATH)


def _attach_sqlite_pragmas(engine_: AsyncEngine) -> None:
    """Включить FK-enforcement и нужный busy_timeout на каждом коннекте.

    SQLite по умолчанию игнорирует объявленные FOREIGN KEY и не ждёт
    при locked-DB — ставим эти PRAGMA на каждое новое соединение.
    """
    @event.listens_for(engine_.sync_engine, "connect")
    def _on_connect(dbapi_conn, _record):  # noqa: ANN001 - SQLAlchemy hook signature
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=10000")
        finally:
            cursor.close()
        # Phase 12: SQLite-вшитые `LOWER`/`COLLATE NOCASE` работают
        # ТОЛЬКО для ASCII — кириллицу не трогают, и поиск по
        # «Марина» / «volvo» в одном запросе с case-insensitive
        # сравнением ломается. Регистрируем питоновский ``py_lower``,
        # который зовёт ``str.lower()`` (Unicode-aware). Используем
        # его в WHERE: ``py_lower(field) LIKE py_lower(:pattern)``.
        try:
            dbapi_conn.create_function(
                "py_lower", 1, lambda v: v.lower() if isinstance(v, str) else v,
                deterministic=True,
            )
        except TypeError:
            # `deterministic=True` доступен с Python 3.8 + sqlite >= 3.8.3.
            # Fallback на старый API без флага — функционально идентично.
            dbapi_conn.create_function(
                "py_lower", 1, lambda v: v.lower() if isinstance(v, str) else v,
            )


def create_async_engine_for(db_path: Path | str) -> AsyncEngine:
    """Создать новый ``AsyncEngine`` для указанного файла БД.

    Удобно для тестов / миграций на временных БД, чтобы не делить
    глобальный движок.
    """
    eng = create_async_engine(_sqlite_url_for(db_path), future=True)
    _attach_sqlite_pragmas(eng)
    return eng


# Глобальный engine, привязанный к конфигурационному ``DB_PATH``.
# SQLite в WAL поддерживает множество читателей и одного писателя.
engine: AsyncEngine = create_async_engine(SQLITE_DATABASE_URL, future=True)
_attach_sqlite_pragmas(engine)


# ``expire_on_commit=False`` важен для async-кода: после commit-а атрибуты
# не «протухают», что даёт читать значения объекта без повторного round-trip.
async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    expire_on_commit=False,
    autoflush=False,
)


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Async-контекст-менеджер с транзакционной семантикой.

    Внутри ``async with``:

    * успешный выход → ``commit``;
    * исключение → ``rollback`` и проброс наружу;
    * сессия всегда закрывается.

    Пример::

        async with get_session() as session:
            user = await UserRepository(session).get(telegram_id=123)
    """
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
