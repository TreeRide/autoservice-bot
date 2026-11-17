"""
``/start`` и ``/cancel`` для администраторов.
"""

from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
    WebAppInfo,
)

from config import WEBAPP_URL
from filters import IsAdmin

# Phase 15: ADMIN_PANEL_KB больше не используется в /start /cancel.
# Reply-меню осталось в common.py только как legacy-константа на случай,
# если какой-то другой роутер по-прежнему ссылается на него.

logger = logging.getLogger(__name__)

router = Router(name="admin-start")


@router.message(CommandStart(), IsAdmin())
async def cmd_start_admin(message: Message, state: FSMContext, bot: Bot) -> None:
    """Старт для администратора.

    Phase 15: CRM-панель (Mini App) — единственный вход в админку.
    Reply-клавиатуры (legacy «👤 Добавить клиента», «📥 Заявки» и т.д.)
    больше не используются: добавление клиентов переехало в форму на
    одном экране WebApp, а заявки и поиск живут в дашборде там же.

    Поэтому /start отправляет всего ДВА сообщения:

    1. Заголовок + :class:`ReplyKeyboardRemove` — гасит у админа любые
       зависшие reply-кнопки от старых сборок (Phase 10/11/12) и
       очищает рабочий стол.
    2. Inline-кнопка ``💼 ОТКРЫТЬ CRM-ПАНЕЛЬ СТО`` со ссылкой на WebApp.
       Inline-кнопки кэшу клиента не подвержены и рендерятся всегда
       заново — это надёжнее, чем полагаться на ``MenuButtonWebApp``,
       который Telegram кэширует агрессивно.

    ``ADMIN_PANEL_KB`` намеренно НЕ возвращается: TЗ Phase 15 явно
    запрещает legacy reply-меню как «единственный вход» (см. PR-описание).
    """
    await state.clear()

    # ШАГ 1: заголовок + force-remove зависших reply-клавиатур.
    await message.answer(
        "🛠 <b>Панель управления СТО</b>",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )

    # ШАГ 2: единственный вход в CRM — inline web_app-кнопка.
    # Кнопка валидна только в приватном чате с HTTPS-URL — оба условия
    # выполняются конфигом WEBAPP_URL.
    if WEBAPP_URL:
        await message.answer(
            "Все операции — клиенты, заявки, записи ТО — здесь:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="💼 ОТКРЫТЬ CRM-ПАНЕЛЬ СТО",
                            web_app=WebAppInfo(url=WEBAPP_URL),
                        )
                    ]
                ]
            ),
        )
    else:
        # Боту явно не задан публичный URL — фолбэк, чтобы не крашить /start.
        # В этом сценарии админ должен сам поправить .env и /restart.
        logger.warning(
            "WEBAPP_URL не сконфигурирован — inline WebApp-кнопка пропущена."
        )
        await message.answer(
            "⚠️ WEBAPP_URL не сконфигурирован — CRM-панель временно "
            "недоступна. Свяжитесь с разработчиком.",
        )


@router.message(Command("cancel"), IsAdmin(), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Сбросить FSM из любого состояния и вернуть в главное меню."""
    current = await state.get_state()
    await state.clear()
    # Phase 15: возвращаем :class:`ReplyKeyboardRemove` вместо
    # legacy ADMIN_PANEL_KB. Если админу нужно вернуться в CRM —
    # пускай дёрнет /start, который пришлёт inline-кнопку в WebApp.
    if current is None:
        await message.answer(
            "Сейчас нет активного диалога. Откройте CRM через /start.",
            reply_markup=ReplyKeyboardRemove(),
        )
    else:
        await message.answer(
            "Действие отменено. Откройте CRM через /start.",
            reply_markup=ReplyKeyboardRemove(),
        )
