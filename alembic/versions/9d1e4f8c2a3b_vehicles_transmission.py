"""add vehicles.transmission (Phase 18: AI transmission awareness)

Phase 18 закрывает класс галлюцинаций ИИ для авто с АКПП — без знания
типа коробки модель часто советует «двухмассовый маховик», «выжимной
подшипник» и прочие узлы, которых на автомате/CVT попросту нет
(и наоборот, для механики предлагает соленоиды АКПП).

Колонка nullable: legacy-машины, заведённые до Phase 18, продолжат
жить с ``transmission=NULL``, что фронт показывает как «Не указано» и
ИИ интерпретирует как «тип неизвестен — не давать рекомендаций,
специфичных для конкретной коробки».

Допустимые значения проверяются на уровне Pydantic-схем
(``CreateVehicleRequest.transmission`` / ``AdminCreateVehicle.transmission``):
``{"АКПП", "МКПП", "Робот", "Вариатор", "Не указано"}``. На уровне БД
оставляем просто TEXT — без CHECK-констрейнта, потому что batch_alter
на SQLite их пересоздаёт неудобно, а enum'ы могут расширяться.

Revision ID: 9d1e4f8c2a3b
Revises: 7a8c2d1f5e06
Create Date: 2026-04-29 11:50:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9d1e4f8c2a3b"
down_revision: Union[str, Sequence[str], None] = "7a8c2d1f5e06"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch_alter_table — единственный надёжный способ ALTER TABLE на
    # SQLite (нативный ALTER не умеет добавлять CHECK/REFERENCES).
    with op.batch_alter_table("vehicles") as batch_op:
        batch_op.add_column(
            sa.Column("transmission", sa.Text(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("vehicles") as batch_op:
        batch_op.drop_column("transmission")
