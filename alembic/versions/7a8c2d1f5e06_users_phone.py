"""add users.phone (Phase 15: WebApp-only client creation)

Phase 15 переносит создание клиента из Telegram-FSM в форму Mini App.
Админская анкета принимает телефон руками (приёмщик звонит и спрашивает,
а не дёргает Telegram contact-share), поэтому хранить его нужно на
уровне ``users``, а не как раньше — только в каждом
``service_requests`` ряду.

Поле nullable: legacy-юзеры, заведённые в Phase 1-14, останутся без
телефона и продолжат жить (UI рендерит «телефон не указан»). При
эволюции UI можно будет добавить отдельный «дозвон-флоу», но это уже
другая задача.

Revision ID: 7a8c2d1f5e06
Revises: 6e3b7d1a4c04
Create Date: 2026-04-29 10:50:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7a8c2d1f5e06"
down_revision: Union[str, Sequence[str], None] = "6e3b7d1a4c04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch_alter_table — единственный надёжный способ ALTER TABLE на
    # SQLite (нативный ALTER не умеет добавлять CHECK/REFERENCES, и
    # alembic подстраховывается через recreate).
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("phone", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("phone")
