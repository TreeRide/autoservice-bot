"""add fuel_type to vehicles (Phase 7 add-on)

Запчасти/расходники зависят от типа топлива (масло, фильтры,
свечи / форсунки). Поле опциональное — у legacy-записей остаётся
NULL, мастер дозаполняет вручную или клиент через WebApp.

VIN уже существует в схеме (добавлен в 2a7d3f1c5b9e) — миграция
его не трогает, только проверьте что колонка vehicles.vin есть.

Revision ID: 5d2a6f9c8b03
Revises: 4c1f5e8a9b02
Create Date: 2026-04-28 00:10:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "5d2a6f9c8b03"
down_revision: Union[str, Sequence[str], None] = "4c1f5e8a9b02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("vehicles") as batch_op:
        batch_op.add_column(
            sa.Column("fuel_type", sa.Text(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("vehicles") as batch_op:
        batch_op.drop_column("fuel_type")
