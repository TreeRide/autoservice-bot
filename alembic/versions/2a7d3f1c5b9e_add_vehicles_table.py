"""add vehicles table + backfill primary vehicle from users.car_*

Phase 6 — виртуальный гараж: таблица ``vehicles`` (один клиент → N авто).
Backfill: для каждого пользователя с непустым ``car_brand`` создаём ОДНУ
запись с ``is_primary=1``. Старые поля ``users.car_brand / car_plate /
last_known_mileage`` НЕ дропаем в этой миграции (двухфазный
expand → contract): пока что часть кода (notifications.py, raw-SQL
report'ы) продолжает их читать, и хэндлеры пишут в обе стороны.
Дроп legacy-колонок поедет отдельной contract-миграцией после прод-проверки.

Revision ID: 2a7d3f1c5b9e
Revises: c1e94a352bb3
Create Date: 2026-04-27 23:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "2a7d3f1c5b9e"
down_revision: Union[str, Sequence[str], None] = "c1e94a352bb3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Создать ``vehicles`` и перенести данные первого автомобиля каждого
    клиента из legacy-полей ``users.car_*`` / ``last_known_mileage``."""
    op.create_table(
        "vehicles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("brand", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("vin", sa.Text(), nullable=True),
        sa.Column("plate_number", sa.Text(), nullable=False),
        sa.Column("current_mileage", sa.Integer(), nullable=True),
        sa.Column("avg_yearly_mileage", sa.Integer(), nullable=True),
        sa.Column(
            "is_primary",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.Text(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.telegram_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_vehicles_user_id", "vehicles", ["user_id"])

    # ---- Backfill ---------------------------------------------------------
    # Копируем car_brand «как есть» (не парсим на марку/модель). model = NULL.
    # Берём только клиентов с непустым car_brand — иначе NOT NULL brand
    # вылетит, и таких клиентов в новой схеме считаем «без машины».
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            INSERT INTO vehicles (
                user_id, brand, model, year, vin,
                plate_number, current_mileage, avg_yearly_mileage,
                is_primary
            )
            SELECT
                telegram_id,
                car_brand,
                NULL, NULL, NULL,
                COALESCE(car_plate, ''),
                last_known_mileage,
                NULL,
                1
            FROM users
            WHERE car_brand IS NOT NULL AND TRIM(car_brand) <> ''
            """
        )
    )


def downgrade() -> None:
    """Откатить: дропнуть ``vehicles`` (данные в legacy-полях остались)."""
    op.drop_index("ix_vehicles_user_id", table_name="vehicles")
    op.drop_table("vehicles")
