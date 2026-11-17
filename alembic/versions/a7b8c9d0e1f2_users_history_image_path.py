"""Phase 35 · ``users_history.image_path`` — путь к фото для записи ТО.

Добавляем nullable TEXT-поле для прямой загрузки фото
(не Telegram file_id, а путь к файлу в /uploads/maintenance/<uuid>/).
Существующее поле ``photo_file_id`` остаётся для обратной совместимости
со старыми записями ТО, прикрепленными через бота. Новый flow:

    POST /api/admin/maintenance/upload (multipart) → {image_path}
    POST /api/admin/service-record body.image_path → users_history.image_path
    GET  /api/history → возвращает image_path в каждой записи
    Frontend → <img src="{image_path}"> в карточке записи.

Path хранится как абсолютный URL-путь от корня сайта
(например ``/uploads/maintenance/<uuid>/<file>.jpg``).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users_history") as batch:
        batch.add_column(sa.Column("image_path", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users_history") as batch:
        batch.drop_column("image_path")
