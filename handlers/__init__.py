"""
Подключение всех роутеров в один общий :data:`router`.

Порядок важен: сначала админский роутер (он перехватывает /add_record
и шаги FSM), затем клиентский (общедоступные команды).
"""

from __future__ import annotations

from aiogram import Router

from handlers.admin import router as admin_router
from handlers.user import router as user_router

router = Router(name="root-router")
router.include_router(admin_router)
router.include_router(user_router)

__all__ = ["router"]
