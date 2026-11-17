"""
Сборка админ-роутера из логических подмодулей.

Структура пакета:

* :mod:`handlers.admin.common` — общие константы, клавиатуры,
  форматтеры и хэлперы (без хэндлеров).
* :mod:`handlers.admin.start` — ``/start`` и ``/cancel``.
* :mod:`handlers.admin.admin_management` — ``/add_admin`` / ``/del_admin``.
* :mod:`handlers.admin.clients` — добавление/редактирование/удаление клиентов.
* :mod:`handlers.admin.records` — добавление записей ТО, последние записи,
  удаление записи, ``/test_notify``.
* :mod:`handlers.admin.search` — поиск клиента по имени/гос. номеру.
* :mod:`handlers.admin.requests` — журнал заявок на звонок.

Доступ к админ-функциям имеют только пользователи из таблицы ``admins``
(см. :mod:`db`). Попытки вызова ``/add_record`` не-админом логируются
как ``UNAUTHORIZED ACCESS ATTEMPT: [ID]``.
"""

from __future__ import annotations

from aiogram import Router

from . import (
    admin_management,
    clients,
    records,
    requests,
    search,
    start,
)

router = Router(name="admin-router")

# Порядок важен: специфичные команды (start/cancel, add_admin/del_admin)
# регистрируются ПЕРЕД хэндлерами, которые могут конкурировать (например,
# fallback-хэндлеры по StateFilter в clients/records/search).
router.include_router(start.router)
router.include_router(admin_management.router)
router.include_router(records.router)
router.include_router(clients.router)
router.include_router(search.router)
router.include_router(requests.router)

__all__ = ["router"]
