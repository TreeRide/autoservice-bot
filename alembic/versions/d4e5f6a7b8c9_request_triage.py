"""Phase 23 · Request triage (is_viewed / is_confirmed / resolved).

Три новых булевых флага на ``service_requests``:

* ``is_viewed``     — мастер открывал карточку этой заявки (любая деталь-модалка
                      в CRM ставит флаг атомарно при первом GET).
* ``is_confirmed``  — мастер подтвердил заявку («✅ Подтвердить», раньше было
                      «Обработано»). Перенос из bucket «Просмотрено» в
                      bucket «Подтверждено».
* ``resolved``      — заявка ушла из воронки ожидания. Взводится автоматически
                      при смене статуса на ``in_yard`` / ``in_progress`` /
                      ``ready`` / ``completed``.

Legacy-дисциплина: ``status`` остаётся источником истины yard-board
(pending_dropoff | in_yard | in_progress | ready | completed);
тройка новых флагов — ортогональная (можно одновременно is_viewed=1 и
resolved=1, напр. для in_yard-заявок со старого дэша).

Backfill существующих записей:

    status                                      → is_viewed is_confirmed resolved
    ---------------------------------------------- --------- ------------ --------
    pending_dropoff                                0         0            0
    in_yard | in_progress | ready | completed      1         1            1
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch_alter_table используется из-за SQLite — она не умеет ALTER в
    # общем смысле, Alembic эмулирует через rebuild таблицы.
    with op.batch_alter_table("service_requests") as batch:
        batch.add_column(
            sa.Column(
                "is_viewed",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch.add_column(
            sa.Column(
                "is_confirmed",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch.add_column(
            sa.Column(
                "resolved",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )

    # Backfill: все не-pending заявки → ушли из воронки ожидания.
    # Считаем их ещё и ``is_viewed`` + ``is_confirmed``, т.к.:
    #   * мастер их не просто видел, он уже принял машину на подъёмник;
    #   * визуально в новом CRM они не попадают в «Новые / Просмотрено /
    #     Подтверждено» (resolved=1 выкидывает из всей воронки). Флаги
    #     нужны только чтобы legacy-записи при будущих фильтрах
    #     «показать историю подтверждений» корректно попали в выборку.
    op.execute(
        """
        UPDATE service_requests
           SET is_viewed = 1,
               is_confirmed = 1,
               resolved = 1
         WHERE status != 'pending_dropoff'
        """
    )


def downgrade() -> None:
    with op.batch_alter_table("service_requests") as batch:
        batch.drop_column("resolved")
        batch.drop_column("is_confirmed")
        batch.drop_column("is_viewed")
