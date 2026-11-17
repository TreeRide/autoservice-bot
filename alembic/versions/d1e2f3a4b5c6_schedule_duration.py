"""Phase 37.5 · Multi-slot booking duration.

Добавляет в ``service_requests`` одно поле:

* ``duration_hours`` — целое число часов, которое длится визит
  (``INTEGER NOT NULL DEFAULT 1``). Используется матрицей расписания,
  чтобы «растягивать» occupied-блок вниз по Y-оси, и сервером — для
  проверки перекрывающихся бронирований на один пост.

NOT NULL с server_default=1: легаси-заявкам и любым новым POST'ам
без duration_hours будет проставлена единица, что эквивалентно
прежнему поведению («одна ячейка = один час»).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d1e2f3a4b5c6"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("service_requests") as batch:
        batch.add_column(
            sa.Column(
                "duration_hours",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("service_requests") as batch:
        batch.drop_column("duration_hours")
