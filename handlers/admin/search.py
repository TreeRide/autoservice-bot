"""
🗂 База клиентов — поиск клиентов по имени/гос. номеру/ID.

Это «вход» во flow «найти клиента → посмотреть профиль».
Сами действия над профилем (редактирование, удаление, история ТО)
живут в :mod:`handlers.admin.clients`.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from database.repositories import UserRepository
from filters import IsAdmin
from states import FindClient

from .common import (
    ADMIN_PANEL_KB,
    BTN_CLIENTS_DB,
    _show_client_profile,
    search_results_kb,
)

logger = logging.getLogger(__name__)

router = Router(name="admin-search")


@router.message(F.text == BTN_CLIENTS_DB, IsAdmin())
async def btn_clients_db(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(FindClient.waiting_search)
    await message.answer(
        "Введите имя или гос. номер для поиска (или /cancel):"
    )


@router.message(FindClient.waiting_search, F.text)
async def find_client_input(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Введите имя или номер.")
        return

    users = UserRepository(session)
    try:
        if text.isdigit() and 9 <= len(text) <= 10:
            results = await users.search_by_id(int(text))
        else:
            results = await users.search_by_text(text)

        await state.clear()

        if not results:
            await message.answer(f"По запросу «{text}» ничего не найдено.")
            return
        if len(results) == 1:
            await _show_client_profile(message, results[0], session)
            return
        await message.answer(
            f"Найдено {len(results)} клиентов — выберите:",
            reply_markup=search_results_kb(results, prefix="browse"),
        )
    except Exception:
        logger.exception("Ошибка при обработке поиска клиента (text=%r)", text)
        await state.clear()
        await message.answer(
            "Произошла внутренняя ошибка при поиске. Попробуйте ещё раз "
            "или нажмите /cancel.",
            reply_markup=ADMIN_PANEL_KB,
        )


# Fallback для нетекстовых апдейтов в FindClient — регистрируется
# в :mod:`handlers.admin.__init__` как часть последнего include_router.
@router.message(StateFilter(FindClient), IsAdmin())
async def fallback_find_client_state(message: Message) -> None:
    await message.answer(
        "Введите имя или гос. номер для поиска — текстом. "
        "Для отмены — /cancel."
    )
