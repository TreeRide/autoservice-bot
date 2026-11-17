"""
Bootstrap-логика приложения, выполняемая один раз при старте бота:

1. Применить все pending Alembic-миграции (``alembic upgrade head``).
   Если БД ещё не отмечена в ``alembic_version`` (например, легаси-БД,
   созданная raw-SQL'ом из старого ``db.py``), но все нужные таблицы
   уже есть — проставить ``stamp head`` без выполнения миграции.
2. Засеять таблицу ``admins`` из ``ADMIN_BOOTSTRAP_IDS`` env (CSV),
   если она пуста.
3. Включить ``journal_mode=WAL`` (database-level, персистит сразу).

Эта логика когда-то жила в ``db.init_db()`` — теперь схема под управлением
Alembic, и легаси-`db.py` удалён.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text

from config import DB_PATH
from database.repositories import AdminRepository, invalidate_admin_cache
from database.session import async_session_factory, engine

logger = logging.getLogger(__name__)


_PROJECT_ROOT = Path(__file__).resolve().parent
_ALEMBIC_INI = _PROJECT_ROOT / "alembic.ini"
# Все таблицы, описанные в baseline-миграции. Если они уже есть в БД,
# а alembic_version отсутствует — БД легаси, её надо stamp'нуть.
_BASELINE_TABLES: frozenset[str] = frozenset(
    {"users", "users_history", "admins", "service_requests", "notifications_log"}
)


def _alembic_config() -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_PROJECT_ROOT / "alembic"))
    return cfg


def _sync_inspect_db(db_path: Path) -> tuple[set[str], str | None]:
    """Синхронно посмотреть на БД до запуска async-движка.

    Возвращает (set имён таблиц, текущая alembic-ревизия или None).
    """
    if not db_path.exists():
        return set(), None
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {r[0] for r in rows}
        revision: str | None = None
        if "alembic_version" in names:
            row = con.execute("SELECT version_num FROM alembic_version").fetchone()
            revision = row[0] if row else None
        return names, revision
    finally:
        con.close()


def _alembic_upgrade_or_stamp() -> None:
    """Применить миграции, аккуратно обработав три случая.

    1. **Свежая БД** (файла нет / в нём нет наших таблиц) — обычный
       ``upgrade head``: создаст всё с нуля.
    2. **Легаси-БД** (таблицы есть, но ``alembic_version`` нет) —
       структура совпадает с baseline, поэтому ``stamp head`` без
       выполнения миграций.
    3. **БД под управлением Alembic** (``alembic_version`` есть) —
       обычный ``upgrade head``.
    """
    cfg = _alembic_config()
    tables, revision = _sync_inspect_db(DB_PATH)
    has_legacy_schema = _BASELINE_TABLES.issubset(tables)

    if revision is not None:
        # Case 3.
        logger.info("Alembic: текущая ревизия %s, upgrade head", revision)
        command.upgrade(cfg, "head")
        return
    if has_legacy_schema:
        # Case 2.
        logger.info(
            "Alembic: легаси-БД с baseline-схемой, выполняю stamp head"
        )
        command.stamp(cfg, "head")
        return
    # Case 1.
    logger.info("Alembic: свежая БД, выполняю upgrade head с нуля")
    command.upgrade(cfg, "head")


def _parse_bootstrap_admin_ids() -> list[int]:
    """Прочитать ``ADMIN_BOOTSTRAP_IDS`` из env (csv int)."""
    raw = (os.getenv("ADMIN_BOOTSTRAP_IDS") or "").strip()
    if not raw:
        return []
    out: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            out.append(int(token))
        except ValueError:
            logger.warning(
                "ADMIN_BOOTSTRAP_IDS: пропускаю невалидный токен %r", token
            )
    return out


async def _seed_admins_if_empty() -> None:
    bootstrap = _parse_bootstrap_admin_ids()
    if not bootstrap:
        return
    async with async_session_factory() as session:
        repo = AdminRepository(session)
        added = await repo.seed_if_empty(bootstrap)
        if added:
            await session.commit()
            invalidate_admin_cache()
            logger.info("Bootstrapped admins table with IDs: %s", bootstrap)


async def _set_wal_mode() -> None:
    """Разово выставить ``journal_mode=WAL`` (database-level, персистит)."""
    async with engine.connect() as conn:
        result = await conn.execute(text("PRAGMA journal_mode=WAL"))
        mode = result.scalar()
        await conn.execute(text("PRAGMA synchronous=NORMAL"))
    logger.info("SQLite journal_mode = %s", mode)


async def bootstrap_database() -> None:
    """Async-bootstrap: WAL + seed админов.

    Миграции Alembic применяются ОТДЕЛЬНО, синхронно, ДО запуска
    основного event loop — см. ``main._run_alembic_migrations`` в
    ``main.py``. Это избавляет от deadlock'а между внешним event
    loop'ом aiogram и внутренним ``asyncio.run`` в ``alembic/env.py``,
    который через ``aiosqlite`` запускает ещё и helper-thread.
    """
    logger.info("bootstrap: WAL step")
    await _set_wal_mode()
    logger.info("bootstrap: seed admins step")
    await _seed_admins_if_empty()
    logger.info("Database bootstrap complete: %s", DB_PATH)
