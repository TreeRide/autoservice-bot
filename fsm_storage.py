"""
Persistent FSM-storage поверх SQLite (aiosqlite).

Реализация :class:`aiogram.fsm.storage.base.BaseStorage`, которая
хранит state и data в той же БД, что и остальные данные сервиса
(см. :data:`config.DB_PATH`). Цель — пережить рестарт бота: если
админ или клиент в середине диалога, их состояние не теряется.

Таблица ``fsm_state`` создаётся в :func:`db.init_db` (см.
``CREATE_FSM_STATE_SQL``). Ключ записи — кортеж
``(chat_id, user_id, thread_id, business_connection_id, destiny)``,
совпадающий с :class:`aiogram.fsm.storage.base.StorageKey`.

``state`` хранится как ``TEXT`` (строковое представление State или
``NULL`` для «без состояния»). ``data_json`` — JSON-сериализованный
словарь. Все file_id фото и пр. лежат внутри JSON.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

import aiosqlite
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey

from config import DB_PATH

logger = logging.getLogger(__name__)


# Таблица fsm_state не описана в SQLAlchemy-моделях (управляется
# напрямую этим модулем) — поэтому Alembic её не создаёт. Поднимаем
# её один раз на старте приложения через init_fsm_table().
_CREATE_FSM_STATE_SQL = """
CREATE TABLE IF NOT EXISTS fsm_state (
    chat_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    thread_id  INTEGER NOT NULL DEFAULT 0,
    business_connection_id TEXT NOT NULL DEFAULT '',
    destiny    TEXT    NOT NULL DEFAULT 'default',
    state      TEXT,
    data_json  TEXT    NOT NULL DEFAULT '{}',
    updated_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chat_id, user_id, thread_id, business_connection_id, destiny)
);
"""


@asynccontextmanager
async def _open_db(db_path: Path = DB_PATH) -> AsyncIterator[aiosqlite.Connection]:
    """Открыть aiosqlite-соединение с нужными PRAGMA.

    SQLAlchemy-движок параллельно держит свои соединения; SQLite в WAL
    разруливает множество читателей и одного писателя без конфликтов.
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA busy_timeout=10000")
        yield db


async def init_fsm_table(db_path: Path = DB_PATH) -> None:
    """Создать таблицу ``fsm_state`` если её ещё нет."""
    async with _open_db(db_path) as db:
        await db.execute(_CREATE_FSM_STATE_SQL)
        await db.commit()


def _state_to_str(state: str | State | None) -> str | None:
    if state is None:
        return None
    if isinstance(state, State):
        return state.state
    return str(state)


class SQLiteStorage(BaseStorage):
    """FSM-storage поверх существующей SQLite-БД сервиса."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._db_path = db_path

    @staticmethod
    def _key_tuple(key: StorageKey) -> tuple[int, int, int, str, str]:
        return (
            key.chat_id,
            key.user_id,
            key.thread_id or 0,
            key.business_connection_id or "",
            key.destiny or "default",
        )

    async def set_state(self, key: StorageKey, state: str | State | None = None) -> None:
        chat_id, user_id, thread_id, biz_id, destiny = self._key_tuple(key)
        state_str = _state_to_str(state)
        async with _open_db(self._db_path) as db:
            # UPSERT через ON CONFLICT: при обновлении state не трогаем data.
            await db.execute(
                """
                INSERT INTO fsm_state
                    (chat_id, user_id, thread_id, business_connection_id,
                     destiny, state, data_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, '{}', CURRENT_TIMESTAMP)
                ON CONFLICT(chat_id, user_id, thread_id, business_connection_id, destiny)
                DO UPDATE SET state = excluded.state, updated_at = CURRENT_TIMESTAMP
                """,
                (chat_id, user_id, thread_id, biz_id, destiny, state_str),
            )
            # Если новое состояние None И data пустой → можно подчистить строку,
            # чтобы не плодить пустышки. Делаем отдельным DELETE по факту.
            if state_str is None:
                await db.execute(
                    """
                    DELETE FROM fsm_state
                    WHERE chat_id = ? AND user_id = ? AND thread_id = ?
                      AND business_connection_id = ? AND destiny = ?
                      AND state IS NULL
                      AND (data_json = '{}' OR data_json IS NULL)
                    """,
                    (chat_id, user_id, thread_id, biz_id, destiny),
                )
            await db.commit()

    async def get_state(self, key: StorageKey) -> str | None:
        chat_id, user_id, thread_id, biz_id, destiny = self._key_tuple(key)
        async with _open_db(self._db_path) as db:
            async with db.execute(
                """
                SELECT state FROM fsm_state
                WHERE chat_id = ? AND user_id = ? AND thread_id = ?
                  AND business_connection_id = ? AND destiny = ?
                """,
                (chat_id, user_id, thread_id, biz_id, destiny),
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        chat_id, user_id, thread_id, biz_id, destiny = self._key_tuple(key)
        try:
            payload = json.dumps(dict(data), ensure_ascii=False)
        except (TypeError, ValueError):
            logger.exception("FSM data is not JSON-serializable, key=%s", key)
            payload = "{}"
        async with _open_db(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO fsm_state
                    (chat_id, user_id, thread_id, business_connection_id,
                     destiny, state, data_json, updated_at)
                VALUES (?, ?, ?, ?, ?, NULL, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(chat_id, user_id, thread_id, business_connection_id, destiny)
                DO UPDATE SET data_json = excluded.data_json, updated_at = CURRENT_TIMESTAMP
                """,
                (chat_id, user_id, thread_id, biz_id, destiny, payload),
            )
            if payload == "{}":
                await db.execute(
                    """
                    DELETE FROM fsm_state
                    WHERE chat_id = ? AND user_id = ? AND thread_id = ?
                      AND business_connection_id = ? AND destiny = ?
                      AND state IS NULL
                      AND (data_json = '{}' OR data_json IS NULL)
                    """,
                    (chat_id, user_id, thread_id, biz_id, destiny),
                )
            await db.commit()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        chat_id, user_id, thread_id, biz_id, destiny = self._key_tuple(key)
        async with _open_db(self._db_path) as db:
            async with db.execute(
                """
                SELECT data_json FROM fsm_state
                WHERE chat_id = ? AND user_id = ? AND thread_id = ?
                  AND business_connection_id = ? AND destiny = ?
                """,
                (chat_id, user_id, thread_id, biz_id, destiny),
            ) as cur:
                row = await cur.fetchone()
        if not row or not row[0]:
            return {}
        try:
            value = json.loads(row[0])
        except (TypeError, ValueError):
            logger.exception("FSM data corrupted in DB, key=%s", key)
            return {}
        if not isinstance(value, dict):
            return {}
        return value

    async def close(self) -> None:
        # Ничего не держим долгоживущего — все соединения закрываются по async-with.
        return None
