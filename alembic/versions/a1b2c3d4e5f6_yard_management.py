"""Phase 20 · Yard Management & Drop-off Windows

Добавляем колонку ``service_requests.dropoff_window`` (String, nullable)
и нормализуем статусы заявок к строгому enum из 5 значений:

* ``pending_dropoff`` — клиент оставил заявку, ждём заезд.
* ``in_yard``         — машина физически на стоянке СТО.
* ``in_progress``     — машина поднята, идёт ремонт.
* ``ready``           — ремонт закончен, ждём, когда клиент заберёт.
* ``completed``       — выдано клиенту, заявка закрыта (уходит с доски).

Старые значения (``new``, ``pending``, ``active``, ``resolved``,
``closed``) маппим:

* ``new`` / ``pending`` / ``active`` → ``pending_dropoff``
  (заявки «ждут приёмщика» — по семантике ближе всего к
  «ожидается заезд»).
* ``resolved`` / ``closed`` → ``completed``.

На уровне БД по-прежнему TEXT, без CHECK-констрейнта — валидируем
Pydantic'ом; batch_alter на SQLite-CHECK работает криво, а enum
может расширяться в будущих фазах.

Revision ID: a1b2c3d4e5f6
Revises: 9d1e4f8c2a3b
Create Date: 2026-04-30 14:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "9d1e4f8c2a3b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. dropoff_window — новая колонка, nullable (legacy-заявки
    #    просто останутся без окна заезда).
    with op.batch_alter_table("service_requests") as batch_op:
        batch_op.add_column(
            sa.Column("dropoff_window", sa.Text(), nullable=True)
        )

    # 2. миграция значений status → новый enum.
    #    Делаем явные UPDATE'ы (а не CASE), чтобы при ошибке маппинга
    #    упасть громко, а не проглотить одним CASE/ELSE.
    op.execute(
        "UPDATE service_requests SET status = 'pending_dropoff' "
        "WHERE status IN ('new', 'pending', 'active')"
    )
    op.execute(
        "UPDATE service_requests SET status = 'completed' "
        "WHERE status IN ('resolved', 'closed')"
    )

    # 3. индекс по status — yard-board фильтрует по 3 статусам,
    #    запрос идёт часто (каждый рефреш), небольшой cost добавить.
    op.create_index(
        "ix_service_requests_status",
        "service_requests",
        ["status"],
    )


def downgrade() -> None:
    # Откат: возвращаем legacy-статусы, дропаем колонку и индекс.
    # Маппинг потерь: pending_dropoff → new, in_yard/in_progress/ready
    # сводим к ``active`` (т.к. legacy-логике они все одинаково «в
    # работе»), completed → closed.
    op.execute(
        "UPDATE service_requests SET status = 'new' "
        "WHERE status = 'pending_dropoff'"
    )
    op.execute(
        "UPDATE service_requests SET status = 'active' "
        "WHERE status IN ('in_yard', 'in_progress', 'ready')"
    )
    op.execute(
        "UPDATE service_requests SET status = 'closed' "
        "WHERE status = 'completed'"
    )

    op.drop_index(
        "ix_service_requests_status", table_name="service_requests"
    )
    with op.batch_alter_table("service_requests") as batch_op:
        batch_op.drop_column("dropoff_window")
