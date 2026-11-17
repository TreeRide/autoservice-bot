"""Phase 37.6 · Master-centric scheduling.

Заменяем колонки расписания с post-id на master-id:

* ``assigned_master_id`` — FK-like ссылка на ``admins.telegram_id`` (INTEGER
  NULL). Это «канонический» идентификатор колонки матрицы. Имя мастера
  для отображения резолвится из ``admins`` JOIN'ом в endpoint'е и
  параллельно денормализуется в ``assigned_master`` (TEXT) — для лёгких
  выборок без JOIN'а и для совместимости со старым кодом.

Старая колонка ``assigned_post_id`` остаётся в таблице (для архивных
заявок), но новой логикой больше не пишется. Если в будущем понадобится
полностью убрать post-id — отдельная миграция её дропнет.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2f3a4b5c6d7"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("service_requests") as batch:
        batch.add_column(
            sa.Column(
                "assigned_master_id",
                sa.Integer(),
                nullable=True,
            )
        )
    op.create_index(
        "ix_service_requests_assigned_master_id",
        "service_requests",
        ["assigned_master_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_service_requests_assigned_master_id",
        table_name="service_requests",
    )
    with op.batch_alter_table("service_requests") as batch:
        batch.drop_column("assigned_master_id")
