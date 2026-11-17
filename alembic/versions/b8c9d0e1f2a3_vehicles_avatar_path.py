"""Phase 42 · vehicles.avatar_path."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vehicles") as batch:
        batch.add_column(sa.Column("avatar_path", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("vehicles") as batch:
        batch.drop_column("avatar_path")
