"""Phase 22 · Car binding in booking

Добавляем колонку ``service_requests.vehicle_id`` (Integer, nullable,
FK → vehicles.id ON DELETE SET NULL).

Клиент теперь записывается на сервис «из карточки авто», и в
service_requests сохраняется ссылка на конкретный автомобиль.
Legacy-заявки и заявки, поданные через глобальный sticky CTA
(без контекста авто), оставляют ``vehicle_id = NULL`` — это
валидное состояние. Бэкфилл не делаем: у старых заявок не
существует «правильного» vehicle_id, любое угадывание (например,
«привязать к is_primary машине клиента») дало бы искажение
статистики.

``ON DELETE SET NULL`` вместо CASCADE: если мастер удалит машину из
гаража, историю заявок терять нельзя — она остаётся в CRM как
«автомобиль не указан».

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-04 19:50:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite не умеет ADD COLUMN с REFERENCES + ON DELETE — приходится
    # делать через batch_alter_table, который пересоздаёт таблицу.
    # Заодно получаем честный FK-constraint в SQLite-схеме.
    with op.batch_alter_table(
        "service_requests",
        recreate="always",
    ) as batch:
        batch.add_column(
            sa.Column("vehicle_id", sa.Integer(), nullable=True)
        )
        batch.create_foreign_key(
            "fk_service_requests_vehicle_id",
            "vehicles",
            ["vehicle_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index(
            "ix_service_requests_vehicle_id",
            ["vehicle_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table(
        "service_requests",
        recreate="always",
    ) as batch:
        batch.drop_index("ix_service_requests_vehicle_id")
        batch.drop_constraint(
            "fk_service_requests_vehicle_id", type_="foreignkey"
        )
        batch.drop_column("vehicle_id")
