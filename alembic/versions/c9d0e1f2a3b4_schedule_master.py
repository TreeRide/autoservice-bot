"""Phase 37.2 · Assigned master per booking.

Добавляет одно nullable-поле в ``service_requests``:

* ``assigned_master`` — имя/идентификатор мастера, ответственного за
  визит (TEXT, например «Олег», «Сергей»). Свободный ввод — отдельной
  таблицы «мастеров» пока нет, важна только привязка имени к заявке.
  Nullable, потому что у легаси-заявок и у заявок без записи в матрицу
  этого поля нет.

Это узкое расширение Phase 37 — никаких других колонок, никаких
индексов: имя мастера не используется в фильтрах ``WHERE``,
а только показывается в карточке заявки.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c9d0e1f2a3b4"
down_revision = "a8b9c0d1e2f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("service_requests") as batch:
        batch.add_column(
            sa.Column("assigned_master", sa.Text(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("service_requests") as batch:
        batch.drop_column("assigned_master")
