"""add vehicle_id FK to users_history (Phase 7)

Привязываем каждую запись о ТО к конкретной машине из таблицы vehicles.
Все существующие записи бэкфиллим: vehicle_id = id is_primary-машины
владельца записи. Если у владельца нет ни одной машины (теоретический
edge-case), запись остаётся с vehicle_id=NULL — это допустимо, FK
объявлен как ON DELETE SET NULL.

Revision ID: 4c1f5e8a9b02
Revises: 3b8e4d2c7a01
Create Date: 2026-04-27 23:55:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "4c1f5e8a9b02"
down_revision: Union[str, Sequence[str], None] = "3b8e4d2c7a01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch_alter_table нужен из-за SQLite (он не поддерживает ALTER ADD
    # COLUMN с FK напрямую — alembic пересоздаёт таблицу под капотом).
    with op.batch_alter_table("users_history") as batch_op:
        batch_op.add_column(
            sa.Column("vehicle_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_users_history_vehicle_id",
            "vehicles",
            ["vehicle_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_users_history_vehicle_id", ["vehicle_id"], unique=False
        )

    # Бэкфилл: каждой существующей записи проставить vehicle_id главной
    # машины владельца (is_primary=1). Update по join'у в SQLite не
    # поддерживается напрямую — используем коррелированный subquery.
    op.execute(
        """
        UPDATE users_history
        SET vehicle_id = (
            SELECT v.id FROM vehicles v
            WHERE v.user_id = users_history.user_id
              AND v.is_primary = 1
            ORDER BY v.id
            LIMIT 1
        )
        WHERE vehicle_id IS NULL
        """
    )


def downgrade() -> None:
    with op.batch_alter_table("users_history") as batch_op:
        batch_op.drop_index("ix_users_history_vehicle_id")
        batch_op.drop_constraint(
            "fk_users_history_vehicle_id", type_="foreignkey"
        )
        batch_op.drop_column("vehicle_id")
