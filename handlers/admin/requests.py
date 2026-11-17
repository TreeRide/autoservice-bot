"""
📥 Заявки — журнал «новых» заявок на звонок и закрытие из уведомлений.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from database.repositories import UserRepository
from filters import IsAdmin

from .common import ADMIN_PANEL_KB, BTN_REQUESTS

router = Router(name="admin-requests")


def _format_request_card(
    *,
    full_name: str,
    username: str | None,
    phone: str,
    car_brand: str | None,
    car_plate: str | None,
    description: str | None,
) -> str:
    """Тот же шаблон, что и при уведомлении админа о новой заявке."""
    user_tag = f" (@{username})" if username else ""
    car = f"{car_brand or '—'} [{car_plate or '—'}]"
    desc = description.strip() if description and description.strip() else "Не указано"
    return (
        "⚡️ <b>НОВАЯ ЗАЯВКА НА ЗВОНОК!</b>\n"
        f"👤 Клиент: <b>{full_name}</b>{user_tag}\n"
        f"📱 Телефон: <code>{phone}</code>\n"
        f"🚗 Авто: {car}\n"
        f"📝 Проблема: {desc}"
    )


def _request_kb(request_id: int, telegram_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💬 Написать", url=f"tg://user?id={telegram_id}"
                ),
                InlineKeyboardButton(
                    text="✅ Обработано",
                    callback_data=f"req:close:{request_id}",
                ),
            ]
        ]
    )


@router.message(F.text == BTN_REQUESTS, IsAdmin())
async def btn_requests(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    await state.clear()
    users = UserRepository(session)
    requests = await users.list_new_service_requests()
    if not requests:
        await message.answer("🎉 Активных заявок нет.", reply_markup=ADMIN_PANEL_KB)
        return

    await message.answer(
        f"📥 Активных заявок: <b>{len(requests)}</b>",
        parse_mode="HTML",
        reply_markup=ADMIN_PANEL_KB,
    )
    for req in requests:
        profile = await users.get(int(req.telegram_id))
        if profile is None:
            full_name = f"id {req.telegram_id}"
            username = None
            car_brand = None
            car_plate = None
        else:
            full_name = profile.full_name
            username = profile.username
            car_brand = profile.car_brand
            car_plate = profile.car_plate
        text = _format_request_card(
            full_name=full_name,
            username=username,
            phone=req.phone,
            car_brand=car_brand,
            car_plate=car_plate,
            description=req.description,
        )
        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=_request_kb(int(req.id), int(req.telegram_id)),
        )


@router.callback_query(F.data.startswith("req:close:"), IsAdmin())
async def cb_close_request(cb: CallbackQuery, session: AsyncSession) -> None:
    if cb.data is None or cb.message is None:
        await cb.answer()
        return
    try:
        request_id = int(cb.data.split(":")[2])
    except (IndexError, ValueError):
        await cb.answer("Некорректный ID заявки", show_alert=True)
        return

    users = UserRepository(session)
    req = await users.get_service_request(request_id)
    if req is None:
        await cb.answer("Заявка не найдена", show_alert=True)
        await cb.message.edit_reply_markup(reply_markup=None)
        return
    # Phase 20: единый закрытый статус — 'completed'. Всё, что не
    # 'pending_dropoff', считаем «уже обработанной» с точки зрения
    # legacy bot-chat-flow — пользователю всё равно незачем
    # закрывать её повторно.
    if req.status != "pending_dropoff":
        await cb.answer("Заявка уже закрыта", show_alert=True)
        await cb.message.edit_reply_markup(reply_markup=None)
        return

    await users.close_service_request(request_id)
    await cb.answer("Закрыто")
    original = cb.message.html_text or ""
    new_text = f"[ЗАКРЫТО]\n{original}"
    try:
        await cb.message.edit_text(new_text, parse_mode="HTML")
    except TelegramBadRequest:
        # Если редактирование не удалось — хотя бы убираем кнопки.
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
