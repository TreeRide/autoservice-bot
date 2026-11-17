"""Alembic environment-конфиг (async).

Использует :data:`database.models.Base.metadata` как ``target_metadata``
и :data:`database.session.SQLITE_DATABASE_URL` как URL подключения.
URL берётся из ``alembic.ini`` (sqlalchemy.url) только если он задан
(непустой и не равен placeholder-у); иначе подставляется значение из
конфига приложения. Это позволяет переопределять URL переменной
окружения ``SQLALCHEMY_URL`` без правки ini-файла.
"""

import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Делаем корень проекта импортируемым, чтобы видеть пакеты
# `database`, `config` без установки в site-packages.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from database.models import Base  # noqa: E402
from database.session import SQLITE_DATABASE_URL  # noqa: E402

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Если переменная окружения SQLALCHEMY_URL задана — используем её.
# Иначе берём URL приложения из database.session.
_url_override = os.environ.get("SQLALCHEMY_URL") or SQLITE_DATABASE_URL
config.set_main_option("sqlalchemy.url", _url_override)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Метаданные ORM-моделей. Используется `--autogenerate` для diff'а.
target_metadata = Base.metadata


# Таблицы, которыми Alembic управлять НЕ должен. Сейчас сюда попадает
# только `fsm_state` — она существует в БД, но управляется напрямую
# модулем `fsm_storage` (aiogram BaseStorage) через raw-SQL,
# создаётся в `db.init_db()` и в ORM сознательно не описывается.
_ALEMBIC_IGNORED_TABLES: frozenset[str] = frozenset({"fsm_state"})


def _include_object(object_, name, type_, reflected, compare_to):
    """Фильтр для autogenerate: пропускаем `fsm_state`."""
    if type_ == "table" and name in _ALEMBIC_IGNORED_TABLES:
        return False
    return True


def _process_revision_directives(context_, revision, directives):
    """Фильтр для autogenerate-вывода.

    SQLite-специфика: ``INTEGER PRIMARY KEY`` колонки в PRAGMA
    ``table_info`` отдают ``notnull=0``, потому что ``NULL`` для них
    означает «авто-rowid». Alembic считает это nullable, а наши модели
    объявляют PK как ``nullable=False`` → каждый autogenerate против
    легаси-БД генерирует «modify_nullable» на четырёх PK-колонках,
    хотя по факту изменений нет. Здесь мы вычищаем эти бенайн-диффы.
    """
    from alembic.operations import ops

    if not directives:
        return
    script = directives[0]
    if not hasattr(script, "upgrade_ops"):
        return

    def _is_pk_nullable_noop(op_) -> bool:
        if not isinstance(op_, ops.AlterColumnOp):
            return False
        # Изменяется ИМЕННО nullable (другие kwargs неизменны).
        if op_.modify_nullable is None:
            return False
        # Нас интересуют только PK-колонки — у них SQLite врёт про NULL.
        meta_table = target_metadata.tables.get(op_.table_name)
        if meta_table is None:
            return False
        col = meta_table.columns.get(op_.column_name)
        if col is None or not col.primary_key:
            return False
        return True

    def _filter(op_list):
        cleaned = []
        for op_ in op_list:
            # render_as_batch=True оборачивает операции в BatchAlterTableOp
            # с вложенным списком ``ops``. Рекурсивно чистим вложенные.
            inner = getattr(op_, "ops", None)
            if isinstance(inner, list):
                op_.ops = _filter(inner)
                if op_.ops:
                    cleaned.append(op_)
                # Пустой batch выбрасываем.
                continue
            if _is_pk_nullable_noop(op_):
                continue
            cleaned.append(op_)
        return cleaned

    for upgrade_ops in (script.upgrade_ops, script.downgrade_ops):
        upgrade_ops.ops = _filter(upgrade_ops.ops)

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite не поддерживает большую часть DDL-операций (DROP COLUMN,
        # ALTER COLUMN и т.д.) без `batch`-режима — он делает CREATE TABLE
        # _new + INSERT SELECT + DROP + RENAME под капотом.
        render_as_batch=True,
        include_object=_include_object,
        process_revision_directives=_process_revision_directives,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        include_object=_include_object,
        process_revision_directives=_process_revision_directives,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
