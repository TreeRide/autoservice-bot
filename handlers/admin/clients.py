"""
Управление клиентами:

* просмотр карточки и истории ТО клиента,
* редактирование профиля (FSM ``EditClient``),
* удаление клиента.

Phase 15: пошаговое FSM-добавление клиента/авто (старая ``AddClient``)
удалено — теперь это форма CRM-панели Mini App
(POST ``/api/admin/create-client-and-vehicle``).
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from database.repositories import (
    RecordRepository,
    UserRepository,
)
from filters import IsAdmin
from states import EditClient

from .common import (
    _iso_to_human,
    _select_client_and_continue,
    _show_client_profile,
    delete_client_confirm_kb,
    edit_profile_menu_kb,
)

router = Router(name="admin-clients")


# ---------------------------------------------------------------------------
# Phase 15: FSM пошагового добавления клиента и автомобиля удалена.
# Создание клиента переехало в форму CRM-панели (POST /api/admin/
# create-client-and-vehicle, см. web_app/main.py). Здесь остаются
# только просмотр карточки, история ТО, редактирование (EditClient FSM)
# и удаление клиента.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Карточка профиля и история ТО
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("browse:"), IsAdmin())
async def browse_client(
    cb: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    if cb.data is None or cb.message is None:
        await cb.answer()
        return
    try:
        client_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        await cb.answer("Некорректный ID", show_alert=True)
        return
    profile = await UserRepository(session).get(client_id)
    if profile is None:
        await cb.answer("Клиент не найден", show_alert=True)
        return
    await state.clear()
    await cb.answer()
    await _show_client_profile(cb.message, profile, session)


@router.callback_query(F.data.startswith("hist:"), IsAdmin())
async def hist_view(cb: CallbackQuery, session: AsyncSession) -> None:
    if cb.data is None or cb.message is None:
        await cb.answer()
        return
    try:
        client_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        await cb.answer("Некорректный ID", show_alert=True)
        return
    history = await RecordRepository(session).get_user_history(client_id)
    profile = await UserRepository(session).get(client_id)
    name = profile.full_name if profile else f"id {client_id}"
    if not history:
        await cb.answer()
        await cb.message.answer(
            f"<b>История ТО клиента {name}:</b>\n\nИстория пуста.",
            parse_mode="HTML",
        )
        return
    await cb.answer()
    lines: list[str] = [f"<b>История ТО клиента {name}:</b>", ""]
    for idx, row in enumerate(history, 1):
        cur = row["current_mileage"]
        nxt = row["next_service_mileage"]
        nv = row["next_visit_date"]
        if nv is None and nxt is None:
            tail = "   <i>Разовая работа</i>"
        else:
            nv_str = _iso_to_human(nv) if nv else "—"
            nxt_str = f"{nxt} км" if nxt is not None else "—"
            tail = f"   Следующее ТО: <b>{nv_str}</b> или <b>{nxt_str}</b>"
        cur_line = (
            f"\n   🔢 Текущий пробег при визите: {cur} км" if cur is not None else ""
        )
        lines.append(
            f"<b>{idx}. {row['service_type']}</b>\n"
            f"   Выполнено: <b>{_iso_to_human(row['execution_date'])}</b>\n"
            f"{tail}"
            f"{cur_line}"
        )
        lines.append("")
    await cb.message.answer("\n".join(lines).strip(), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Меню «✏️ Редактировать профиль»
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("ep:menu:"), IsAdmin())
async def ep_menu(cb: CallbackQuery, session: AsyncSession) -> None:
    if cb.data is None or cb.message is None:
        await cb.answer()
        return
    parts = cb.data.split(":")
    try:
        client_id = int(parts[2])
    except (IndexError, ValueError):
        await cb.answer("Некорректный ID", show_alert=True)
        return
    profile = await UserRepository(session).get(client_id)
    if profile is None:
        await cb.answer("Клиент не найден", show_alert=True)
        return
    await cb.answer()
    await cb.message.answer(
        f"Что изменить у клиента <b>{profile.full_name}</b>?",
        parse_mode="HTML",
        reply_markup=edit_profile_menu_kb(client_id),
    )


def _ep_field_from_callback(callback_data: str) -> tuple[int, str] | None:
    """Распарсить ``ep:<field>:<id>`` → (id, db_field)."""
    parts = callback_data.split(":")
    if len(parts) != 3:
        return None
    field_key = parts[1]
    try:
        client_id = int(parts[2])
    except ValueError:
        return None
    db_field = {"name": "full_name", "brand": "car_brand", "plate": "car_plate"}.get(
        field_key
    )
    if db_field is None:
        return None
    return client_id, db_field


@router.callback_query(F.data.startswith("ep:name:"), IsAdmin())
async def ep_name_request(cb: CallbackQuery, state: FSMContext) -> None:
    if cb.data is None or cb.message is None:
        await cb.answer()
        return
    parsed = _ep_field_from_callback(cb.data)
    if parsed is None:
        await cb.answer("Ошибка", show_alert=True)
        return
    client_id, _ = parsed
    await state.set_state(EditClient.waiting_name)
    await state.update_data(target_id=client_id)
    await cb.answer()
    await cb.message.answer("Введите новое имя клиента:")


@router.callback_query(F.data.startswith("ep:brand:"), IsAdmin())
async def ep_brand_request(cb: CallbackQuery, state: FSMContext) -> None:
    if cb.data is None or cb.message is None:
        await cb.answer()
        return
    parsed = _ep_field_from_callback(cb.data)
    if parsed is None:
        await cb.answer("Ошибка", show_alert=True)
        return
    client_id, _ = parsed
    await state.set_state(EditClient.waiting_brand)
    await state.update_data(target_id=client_id)
    await cb.answer()
    await cb.message.answer("Введите новую марку и модель автомобиля:")


@router.callback_query(F.data.startswith("ep:plate:"), IsAdmin())
async def ep_plate_request(cb: CallbackQuery, state: FSMContext) -> None:
    if cb.data is None or cb.message is None:
        await cb.answer()
        return
    parsed = _ep_field_from_callback(cb.data)
    if parsed is None:
        await cb.answer("Ошибка", show_alert=True)
        return
    client_id, _ = parsed
    await state.set_state(EditClient.waiting_plate)
    await state.update_data(target_id=client_id)
    await cb.answer()
    await cb.message.answer("Введите новый гос. номер:")


async def _apply_edit_field(
    message: Message,
    state: FSMContext,
    db_field: str,
    value: str,
    label: str,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    target_id = int(data["target_id"])
    users = UserRepository(session)
    affected = await users.update_field(target_id, db_field, value)
    # Освобождаем SQLite-write-lock до FSM-записи (см. step_mileage).
    await session.commit()
    await state.clear()
    if not affected:
        await message.answer("⚠️ Клиент уже удалён, обновление не применено.")
        return
    profile = await users.get(target_id)
    await message.answer(f"✅ {label} обновлено.")
    if profile is not None:
        await _show_client_profile(message, profile, session)


@router.message(EditClient.waiting_name, F.text)
async def ep_name_input(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Имя не может быть пустым. Попробуйте ещё раз.")
        return
    await _apply_edit_field(message, state, "full_name", name, "Имя", session)


@router.message(EditClient.waiting_brand, F.text)
async def ep_brand_input(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    brand = (message.text or "").strip()
    if not brand:
        await message.answer("Марка не может быть пустой. Попробуйте ещё раз.")
        return
    await _apply_edit_field(
        message, state, "car_brand", brand, "Марка", session
    )


@router.message(EditClient.waiting_plate, F.text)
async def ep_plate_input(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    plate = (message.text or "").strip()
    if not plate:
        await message.answer("Гос. номер не может быть пустым. Попробуйте ещё раз.")
        return
    await _apply_edit_field(
        message, state, "car_plate", plate, "Гос. номер", session
    )


# ---------------------------------------------------------------------------
# Удаление клиента
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("delc:"), IsAdmin())
async def delc_confirm(cb: CallbackQuery, session: AsyncSession) -> None:
    if cb.data is None or cb.message is None:
        await cb.answer()
        return
    try:
        client_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        await cb.answer("Некорректный ID", show_alert=True)
        return
    profile = await UserRepository(session).get(client_id)
    if profile is None:
        await cb.answer("Клиент уже удалён", show_alert=True)
        return
    await cb.answer()
    await cb.message.answer(
        f"Удалить клиента <b>{profile.full_name}</b> "
        f"(<code>{client_id}</code>) и всю его историю ТО?",
        parse_mode="HTML",
        reply_markup=delete_client_confirm_kb(client_id),
    )


@router.callback_query(F.data.startswith("delcyes:"), IsAdmin())
async def delc_yes(cb: CallbackQuery, session: AsyncSession) -> None:
    if cb.data is None or cb.message is None:
        await cb.answer()
        return
    try:
        client_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        await cb.answer("Некорректный ID", show_alert=True)
        return
    affected = await UserRepository(session).delete_client(client_id)
    if not affected:
        await cb.answer("Клиент уже удалён", show_alert=True)
        return
    await cb.answer("Клиент удалён")
    await cb.message.answer(
        f"🗑 Клиент <code>{client_id}</code> и его история ТО удалены.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Fallback-хэндлеры для нетекстовых апдейтов в AddClient/EditClient.
# Регистрируются последними внутри модуля.
# ---------------------------------------------------------------------------

@router.message(StateFilter(EditClient), IsAdmin())
async def fallback_edit_client_state(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    await message.answer(
        f"Не понял ввод (текущий шаг: <code>{current}</code>). "
        "Введите запрошенное значение текстом или /cancel для отмены.",
        parse_mode="HTML",
    )
