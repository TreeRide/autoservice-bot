"""Phase 37 · Dynamic Scheduling Matrix.

Две независимые схемы изменения:

* ``app_settings`` — KV-стор для настроек CRM. Сейчас хранит
  ``active_posts_count`` (сколько подъёмников активно). Расширяем
  по мере появления глобальных настроек, не плодя колонок.

* ``service_requests`` — добавляем три nullable-поля для назначения
  заявки на конкретный слот в матрице расписания:
    * ``appointment_date`` — дата визита (TEXT 'YYYY-MM-DD');
    * ``appointment_time`` — время визита (TEXT 'HH:MM');
    * ``assigned_post_id`` — номер поста (1..N).
  Nullable, потому что у легаси-заявок этих полей нет, и не каждая
  заявка попадает в расписание (например, request_call без слота).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a8b9c0d1e2f3"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.Text(), primary_key=True, nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.Text(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
    )

    with op.batch_alter_table("service_requests") as batch:
        batch.add_column(
            sa.Column("appointment_date", sa.Text(), nullable=True)
        )
        batch.add_column(
            sa.Column("appointment_time", sa.Text(), nullable=True)
        )
        batch.add_column(
            sa.Column("assigned_post_id", sa.Integer(), nullable=True)
        )

    # Композитный индекс под основной запрос матрицы:
    # SELECT … WHERE appointment_date = ? — обращений к этому полю
    # будет много (по одной выборке на каждое открытие матрицы).
    op.create_index(
        "ix_service_requests_appointment_date",
        "service_requests",
        ["appointment_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_service_requests_appointment_date",
        table_name="service_requests",
    )
    with op.batch_alter_table("service_requests") as batch:
        batch.drop_column("assigned_post_id")
        batch.drop_column("appointment_time")
        batch.drop_column("appointment_date")

    op.drop_table("app_settings")
