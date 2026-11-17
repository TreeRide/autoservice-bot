"""add engine_volume and mileage_updated_at to vehicles

Phase 6B — техническая характеристика двигателя + timestamp последнего
обновления пробега (нужен фронту для расчёта «живого пробега» по формуле
current_mileage + days_since_update * avg_yearly_mileage / 365).

Бэкфилл существующей записи (Volvo Марины) оставляет оба поля NULL —
это ожидаемое поведение: фронт без timestamp/avg_yearly_mileage
показывает просто current_mileage без прогноза.

Revision ID: 3b8e4d2c7a01
Revises: 2a7d3f1c5b9e
Create Date: 2026-04-27 23:45:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "3b8e4d2c7a01"
down_revision: Union[str, Sequence[str], None] = "2a7d3f1c5b9e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("vehicles") as batch_op:
        batch_op.add_column(
            sa.Column("engine_volume", sa.Float(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("mileage_updated_at", sa.Text(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("vehicles") as batch_op:
        batch_op.drop_column("mileage_updated_at")
        batch_op.drop_column("engine_volume")
