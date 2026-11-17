"""add health_reports and health_feedback (Phase 9: AI Health Score)

Phase 9 вводит фичу "AI-Диагностика": Gemini оценивает машину по
ТТХ+пробегу и возвращает score 0-100 + список рисков.

- health_reports: кэш отчётов на 7 дней per vehicle. Один отчёт на
  машину обычно хватает; если клиент отмечает риск как "resolved"/
  "hidden" — отчёт инвалидируется (см. POST /health/feedback) и
  пересоздаётся при следующем GET /health.
- health_feedback: список того, что клиент уже починил/скрыл.
  Этот список передаётся в промпт Gemini, чтобы модель не повторяла
  узлы, уже отмеченные клиентом.

Обе таблицы — CASCADE на удаление vehicle. Если машина удалена —
отчёт и фидбек тоже больше не нужны.

Revision ID: 6e3b7d1a4c04
Revises: 5d2a6f9c8b03
Create Date: 2026-04-28 21:35:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "6e3b7d1a4c04"
down_revision: Union[str, Sequence[str], None] = "5d2a6f9c8b03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "health_reports",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "vehicle_id",
            sa.Integer(),
            sa.ForeignKey("vehicles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("risks_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
    )
    op.create_index(
        "ix_health_reports_vehicle_created",
        "health_reports",
        ["vehicle_id", "created_at"],
    )

    op.create_table(
        "health_feedback",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "vehicle_id",
            sa.Integer(),
            sa.ForeignKey("vehicles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("risk_title", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
    )
    op.create_index(
        "ix_health_feedback_vehicle_status",
        "health_feedback",
        ["vehicle_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_health_feedback_vehicle_status", table_name="health_feedback"
    )
    op.drop_table("health_feedback")
    op.drop_index(
        "ix_health_reports_vehicle_created", table_name="health_reports"
    )
    op.drop_table("health_reports")
