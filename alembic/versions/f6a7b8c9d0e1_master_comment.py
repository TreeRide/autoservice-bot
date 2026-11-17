"""Phase 31 · ``service_requests.master_comment`` — внутренний комментарий мастера.

Свободное TEXT-поле, заполняется только мастером в admin-CRM
карточке заявки. Видимо клиенту НИКОГДА не показывается — это
внутренний канал заметок («дозаказать ремень», «ждём запчасть от
поставщика», «клиент торопится» и т.п.).

Auto-save на blur с фронтенда: PATCH ``/api/admin/requests/{id}/master-comment``.
Nullable — у легаси-заявок и у новых заявок до первого редактирования
значение ``NULL`` (а не пустая строка), чтобы UI не путал «не трогали»
с «явно очистили». Backend-эндпоинт нормализует пустую строку → NULL.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("service_requests") as batch:
        batch.add_column(sa.Column("master_comment", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("service_requests") as batch:
        batch.drop_column("master_comment")
