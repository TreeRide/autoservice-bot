"""Phase 23.1 · ``service_requests.in_yard_at`` — момент приёмки на стоянку.

Колонка nullable-TEXT (naive-строка в Europe/Minsk, как остальные
timestamp'ы проекта — см. Phase 22.1). Взводится ровно один раз —
при первом переходе статуса заявки в ``in_yard`` (см.
``UserRepository.set_service_request_status``). Дальнейшие переходы
(in_progress → ready → completed) её НЕ перезаписывают: это момент
фактического поступления машины на территорию СТО.

Фронт использует значение для:
* yard-modal — в статусах ``in_yard | in_progress | ready`` заголовок
  «Окно заезда» заменяется на «Принята на стоянку» и ниже рендерится
  отформатированная ``in_yard_at`` («4 мая, 22:51»);
* возможных будущих аналитик «длительность ремонта = now - in_yard_at».

Backfill для legacy-записей: всем уже продвинутым заявкам
(``in_yard | in_progress | ready | completed``) копируем
``created_at`` — это лучшая оценка, которую мы можем дать без
исторических логов переходов. Alternative — оставить NULL, но тогда
яр-модалка на старых in_yard-карточках будет пустой; компромисс
в пользу UX.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("service_requests") as batch:
        batch.add_column(sa.Column("in_yard_at", sa.Text(), nullable=True))

    # Backfill: для уже «заехавших» заявок берём created_at (наилучшая
    # доступная оценка). Pending_dropoff оставляем NULL — заявка ещё
    # не приезжала.
    op.execute(
        """
        UPDATE service_requests
           SET in_yard_at = created_at
         WHERE status IN ('in_yard', 'in_progress', 'ready', 'completed')
           AND in_yard_at IS NULL
        """
    )


def downgrade() -> None:
    with op.batch_alter_table("service_requests") as batch:
        batch.drop_column("in_yard_at")
