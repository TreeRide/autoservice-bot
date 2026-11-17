"""Phase 22.1 · Local-TZ backfill (Europe/Minsk).

Сдвигаем все существующие naive-метки времени в БД из UTC в Europe/Minsk
(+3 часа, DST в Беларуси отменён с 2014). С этой миграции наружу из
БД уходят только локальные минские строки вида "YYYY-MM-DD HH:MM:SS",
а свежие inserts пишутся уже локализованным значением через
Python-side default ``config.now_local_sql`` (см. database/models.py).

Применяется:
  * service_requests.created_at
  * vehicles.created_at, vehicles.mileage_updated_at
  * health_reports.created_at, health_feedback.created_at
  * notifications_log.last_sent_at
  * admins.added_at
  * fsm_state.updated_at

Колонки, которые НЕ трогаем:
  * users_history.execution_date / next_visit_date — это даты (YYYY-MM-DD),
    введённые пользователем вручную в календаре, без TZ-семантики.

Offset +3ч применяется строго по SQLite-шному ``datetime(..., '+3 hours')``.
На SQLite это детерминированно: работает в любом локале сервера, не
зависит от внешних библиотек. Если мигрируется уже локализованная БД,
``downgrade`` симметрично сдвинет обратно на -3ч.

Это одноразовая data-миграция — схему не меняет.
"""

from __future__ import annotations

from alembic import op

# revision identifiers
revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


# Список (таблица, колонка) — строго те колонки, которые раньше писались
# серверным ``CURRENT_TIMESTAMP`` (SQLite → UTC) и теперь должны стать
# локальным минским временем.
_TIMESTAMP_COLUMNS: tuple[tuple[str, str], ...] = (
    ("service_requests", "created_at"),
    ("vehicles", "created_at"),
    ("vehicles", "mileage_updated_at"),
    ("health_reports", "created_at"),
    ("health_feedback", "created_at"),
    ("notifications_log", "last_sent_at"),
    ("admins", "added_at"),
    ("fsm_state", "updated_at"),
)

# Europe/Minsk = UTC+3, без DST (летнее время отменено в 2014).
_TZ_OFFSET_HOURS = 3


def _shift(direction: int) -> None:
    """direction=+1 → UTC→Minsk (+3h), direction=-1 → Minsk→UTC (-3h)."""
    sign = "+" if direction > 0 else "-"
    hours = abs(direction) * _TZ_OFFSET_HOURS
    bind = op.get_bind()
    inspector = op.get_context().bind.dialect.get_table_names(bind)
    for table, column in _TIMESTAMP_COLUMNS:
        if table not in inspector:
            # Таблица не существует (например свежая БД без fsm_state) —
            # нет смысла ругаться, просто пропускаем.
            continue
        # ``datetime(col, '+N hours')`` в SQLite ждёт ISO-наподобное
        # представление. Формат "YYYY-MM-DD HH:MM:SS" — валидный вход.
        # NULL-значения ``datetime`` пропускает (возвращает NULL).
        op.execute(
            f"""
            UPDATE {table}
               SET {column} = strftime(
                   '%Y-%m-%d %H:%M:%S',
                   datetime({column}, '{sign}{hours} hours')
               )
             WHERE {column} IS NOT NULL
            """
        )


def upgrade() -> None:
    _shift(+1)


def downgrade() -> None:
    _shift(-1)
