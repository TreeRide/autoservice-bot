"""
Управление записями ТО:

* добавление записи (FSM ``AddRecord`` со «умным поиском» клиента,
  выбором категории / подкатегории, датой выполнения, фотографиями
  и экраном подтверждения),
* «📋 Последние 5 записей» с inline-удалением,
* команда ``/test_notify`` для ручного запуска ежедневных уведомлений.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)
from sqlalchemy.ext.asyncio import AsyncSession

from database.repositories import (
    RecordRepository,
    UserRepository,
    VehicleRepository,
)
from filters import IsAdmin
from notifications import run_notifications
from states import AddClient, AddRecord

from .common import (
    BTN_ADD_RECORD,
    BTN_LAST_RECORDS,
    CATEGORY_NAMES,
    PHOTO_LIMIT,
    PHOTO_PROMPT_KB,
    SKIP_PHOTO_TEXT,
    SUBCATEGORY_NAMES,
    _ask_search,
    _back_button,
    _back_kb,
    _format_record_with_profile,
    _iso_to_human,
    _parse_mileage,
    _safe_send,
    _select_client_and_continue,
    _show_review,
    _ask_for_photo,
    cancel_to_review_kb,
    categories_kb,
    create_new_client_kb,
    delete_kb,
    edit_categories_kb,
    edit_exec_date_kb,
    exec_date_kb,
    next_choice_kb,
    photo_done_kb,
    recent_clients_kb,
    review_kb,
    search_results_kb,
    subcategories_kb,
)

logger = logging.getLogger(__name__)

router = Router(name="admin-records")


# ---------------------------------------------------------------------------
# 📝 Добавить запись ТО (умный поиск)
# ---------------------------------------------------------------------------

@router.message(F.text == BTN_ADD_RECORD, IsAdmin())
async def btn_add_record(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    await state.clear()
    await _ask_search(message, state, session)


@router.message(Command("add_record"), IsAdmin())
async def cmd_add_record(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    await state.clear()
    await _ask_search(message, state, session)


@router.message(Command("add_record"))
async def cmd_add_record_denied(message: Message) -> None:
    if message.from_user:
        logger.warning("UNAUTHORIZED ACCESS ATTEMPT: %s", message.from_user.id)
        print(f"UNAUTHORIZED ACCESS ATTEMPT: {message.from_user.id}")


@router.message(AddRecord.waiting_search, F.text)
async def search_input(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Введите ID, часть номера или имени.")
        return

    users = UserRepository(session)
    if text.isdigit() and 9 <= len(text) <= 10:
        results = await users.search_by_id(int(text))
    else:
        results = await users.search_by_text(text)

    await _process_search_results(
        message, state, results, query=text, session=session
    )


@router.callback_query(AddRecord.waiting_search, F.data.startswith("pick:"))
async def search_recent_pick(
    cb: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    await _handle_pick_callback(cb, state, session)


@router.callback_query(AddRecord.waiting_pick, F.data.startswith("pick:"))
async def pick_from_results(
    cb: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    await _handle_pick_callback(cb, state, session)


async def _handle_pick_callback(
    cb: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    if cb.data is None or cb.message is None:
        await cb.answer()
        return
    try:
        client_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        await cb.answer("Некорректный выбор", show_alert=True)
        return

    profile = await UserRepository(session).get(client_id)
    if profile is None:
        await cb.answer("Клиент не найден", show_alert=True)
        return
    await cb.answer()
    await _select_client_and_continue(cb.message, state, profile, session)


@router.callback_query(
    AddRecord.waiting_vehicle, F.data.startswith("ar:veh:")
)
async def ar_pick_vehicle(
    cb: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Phase 7: мастер выбирает машину для записи (если у клиента >1)."""
    if cb.data is None or cb.message is None:
        await cb.answer()
        return
    try:
        vehicle_id = int(cb.data.split(":", 2)[2])
    except ValueError:
        await cb.answer("Некорректный выбор", show_alert=True)
        return

    data = await state.get_data()
    client_id = data.get("client_id")
    repo = VehicleRepository(session)
    vehicle = await repo.get(vehicle_id)
    if vehicle is None or vehicle.user_id != client_id:
        await cb.answer("Машина не найдена", show_alert=True)
        return

    await state.update_data(
        vehicle_id=vehicle.id,
        vehicle_label=f"{vehicle.brand} [{vehicle.plate_number}]",
    )
    await state.set_state(AddRecord.waiting_service_type)
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        # Сообщение могло быть уже изменено, не критично.
        pass
    await cb.message.answer(
        f"🚗 Выбрано авто: <b>{vehicle.brand}</b> "
        f"[<code>{vehicle.plate_number}</code>]\n\n"
        "Что было сделано? Выберите категорию:",
        parse_mode="HTML",
        reply_markup=categories_kb(),
    )


@router.callback_query(F.data == "search:new", IsAdmin())
async def search_create_new(cb: CallbackQuery, state: FSMContext) -> None:
    if cb.message is None:
        await cb.answer()
        return
    data = await state.get_data()
    edit_mode_for_record = bool(data.get("edit_mode"))
    await state.set_data(
        {"from_record": True, "edit_mode_for_record": edit_mode_for_record}
    )
    await state.set_state(AddClient.waiting_id)
    await cb.answer()
    await cb.message.answer(
        "Введите Telegram ID нового клиента:\n(или /cancel для отмены)"
    )


async def _process_search_results(
    message: Message,
    state: FSMContext,
    results,
    query: str,
    session: AsyncSession,
) -> None:
    if not results:
        await message.answer(
            f"По запросу «{query}» ничего не найдено.",
            reply_markup=create_new_client_kb(),
        )
        return

    if len(results) == 1:
        await _select_client_and_continue(
            message, state, results[0], session
        )
        return

    await state.set_state(AddRecord.waiting_pick)
    await message.answer(
        f"Найдено {len(results)} клиентов — выберите:",
        reply_markup=search_results_kb(results, prefix="pick"),
    )


# ---------------------------------------------------------------------------
# Шаги после выбора клиента (категория, дата, подтверждение)
# ---------------------------------------------------------------------------

@router.callback_query(AddRecord.waiting_service_type, F.data.startswith("cat:"))
async def step_main_category(cb: CallbackQuery, state: FSMContext) -> None:
    """Шаг 1: выбор главной категории. Дальше — подкатегории или ручной ввод."""
    if cb.data is None or cb.message is None:
        await cb.answer()
        return
    key = cb.data.split(":", 1)[1]
    label = CATEGORY_NAMES.get(key)
    if not label:
        await cb.answer("Неизвестная категория", show_alert=True)
        return

    # Сохраняем выбранную главную категорию и НЕ трогаем edit_mode —
    # ревью покажем только после выбора подкатегории / ввода текста.
    data = await state.get_data()
    edit_mode = bool(data.get("edit_mode"))
    await state.update_data(category_key=key, category_label=label)
    await cb.answer()

    if key == "other":
        await state.set_state(AddRecord.waiting_other_text)
        if edit_mode:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [_back_button("⬅️ Назад к категориям", "back:to_categories")],
                    [_back_button("⬅️ Назад к проверке", "back:to_review")],
                ]
            )
        else:
            kb = _back_kb("⬅️ Назад к категориям", "back:to_categories")
        await cb.message.answer(
            f"Категория: {label}\n\nОпишите услугу текстом или /cancel.",
            reply_markup=kb,
        )
        return

    await state.set_state(AddRecord.waiting_subcategory)
    await cb.message.answer(
        f"Категория: {label}\n\nВыберите подкатегорию:",
        reply_markup=subcategories_kb(key),
    )


@router.callback_query(
    AddRecord.waiting_subcategory, F.data.startswith("subcat:")
)
async def step_subcategory(cb: CallbackQuery, state: FSMContext) -> None:
    """Шаг 2: выбор подкатегории. Собираем финальный ``service_type``."""
    if cb.data is None or cb.message is None:
        await cb.answer()
        return
    sub_key = cb.data.split(":", 1)[1]
    data = await state.get_data()
    cat_key = data.get("category_key")
    cat_label = data.get("category_label")
    if not cat_key or not cat_label:
        await cb.answer("Сначала выберите категорию", show_alert=True)
        return
    sub_label = SUBCATEGORY_NAMES.get((cat_key, sub_key))
    if not sub_label:
        await cb.answer("Неизвестная подкатегория", show_alert=True)
        return

    service_type = f"{cat_label} — {sub_label}"
    edit_mode = bool(data.get("edit_mode"))
    await state.update_data(service_type=service_type, edit_mode=False)
    await cb.answer()

    if edit_mode:
        await _show_review(cb.message, state)
        return

    await _ask_current_mileage(cb.message, state)


@router.message(AddRecord.waiting_other_text, F.text)
async def step_other_text(message: Message, state: FSMContext) -> None:
    """Шаг 2 для «📁 Другое» — ручной ввод описания."""
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        await message.answer("Опишите услугу текстом или /cancel.")
        return
    data = await state.get_data()
    cat_label = data.get("category_label", "📁 Другое")
    service_type = f"{cat_label} — {text}"
    edit_mode = bool(data.get("edit_mode"))
    await state.update_data(service_type=service_type, edit_mode=False)

    if edit_mode:
        await _show_review(message, state)
        return

    await _ask_current_mileage(message, state)


# ---------------------------------------------------------------------------
# Шаг 3: текущий пробег → Дата выполнения → Назначить след. ТО?
# ---------------------------------------------------------------------------

async def _ask_current_mileage(message: Message, state: FSMContext) -> None:
    await state.set_state(AddRecord.waiting_current_mileage)
    await message.answer(
        "Введите актуальный пробег авто (в км):",
        reply_markup=_back_kb("⬅️ Назад к категориям", "back:to_categories"),
    )


async def _ask_execution_date(message: Message, state: FSMContext) -> None:
    await state.set_state(AddRecord.waiting_execution_date)
    await message.answer(
        "Укажите дату выполнения работы. Нажмите «Сегодня» или введите дату в формате ДД.ММ.ГГГГ.",
        reply_markup=exec_date_kb(),
    )


async def _ask_next_choice(message: Message, state: FSMContext) -> None:
    await state.set_state(AddRecord.waiting_next_choice)
    await message.answer(
        "Нужно ли назначить следующее ТО для этой работы?",
        reply_markup=next_choice_kb(),
    )


@router.message(AddRecord.waiting_current_mileage, F.text)
async def step_current_mileage(message: Message, state: FSMContext) -> None:
    value = _parse_mileage((message.text or "").strip())
    if value is None:
        await message.answer(
            "Пробег должен быть целым неотрицательным числом (например, 125000). Попробуйте ещё раз."
        )
        return
    data = await state.get_data()
    edit_mode = bool(data.get("edit_mode"))
    await state.update_data(current_mileage=value, edit_mode=False)
    if edit_mode:
        await _show_review(message, state)
        return
    await _ask_execution_date(message, state)


@router.callback_query(AddRecord.waiting_execution_date, F.data == "exec:today")
async def step_execution_today(cb: CallbackQuery, state: FSMContext) -> None:
    if cb.message is None:
        await cb.answer()
        return
    today_iso = date.today().isoformat()
    data = await state.get_data()
    edit_mode = bool(data.get("edit_mode"))
    await state.update_data(execution_date_iso=today_iso, edit_mode=False)
    await cb.answer("Сегодня")
    if edit_mode:
        await _show_review(cb.message, state)
        return
    await _ask_next_choice(cb.message, state)


@router.message(AddRecord.waiting_execution_date, F.text)
async def step_execution_date_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    try:
        exec_date = datetime.strptime(text, "%d.%m.%Y").date()
    except ValueError:
        await message.answer(
            "Дата должна быть в формате ДД.ММ.ГГГГ или нажмите «Сегодня». Попробуйте ещё раз.",
            reply_markup=exec_date_kb(),
        )
        return
    # Sanity-check: дату выполнения нельзя выставлять в будущем —
    # это всегда опечатка (нельзя «уже выполнить» работу, которая ещё не сделана).
    today = date.today()
    if exec_date > today:
        # Используем edit_categories_kb / edit_exec_date_kb для edit-режима,
        # обычный exec_date_kb — для линейного флоу.
        data_for_kb = await state.get_data()
        kb = edit_exec_date_kb() if data_for_kb.get("edit_mode") else exec_date_kb()
        await message.answer(
            f"Дата выполнения не может быть в будущем "
            f"(сегодня {today.strftime('%d.%m.%Y')}). "
            "Введите корректную дату или нажмите «📅 Сегодня».",
            reply_markup=kb,
        )
        return
    data = await state.get_data()
    edit_mode = bool(data.get("edit_mode"))
    await state.update_data(
        execution_date_iso=exec_date.isoformat(), edit_mode=False
    )
    if edit_mode:
        await _show_review(message, state)
        return
    await _ask_next_choice(message, state)


@router.callback_query(AddRecord.waiting_next_choice, F.data == "nxt:no")
async def step_next_choice_no(cb: CallbackQuery, state: FSMContext) -> None:
    if cb.message is None:
        await cb.answer()
        return
    await state.update_data(
        next_visit_iso=None, next_service_mileage=None, edit_mode=False
    )
    await cb.answer("Разовая работа")
    await _ask_for_photo(cb.message, state)


@router.callback_query(AddRecord.waiting_next_choice, F.data == "nxt:yes")
async def step_next_choice_yes(cb: CallbackQuery, state: FSMContext) -> None:
    if cb.message is None:
        await cb.answer()
        return
    await cb.answer()
    await state.set_state(AddRecord.waiting_next_visit)
    await cb.message.answer(
        "Введите дату следующего ТО (ДД.ММ.ГГГГ):",
        reply_markup=_back_kb("⬅️ Назад", "back:to_next_choice"),
    )


@router.message(AddRecord.waiting_next_visit, F.text)
async def step_next_visit(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    try:
        next_visit = datetime.strptime(text, "%d.%m.%Y").date()
    except ValueError:
        await message.answer(
            "Дата должна быть в формате ДД.ММ.ГГГГ (например, 25.04.2027). "
            "Попробуйте ещё раз или /cancel."
        )
        return
    data = await state.get_data()
    edit_mode = bool(data.get("edit_mode"))
    await state.update_data(next_visit_iso=next_visit.isoformat(), edit_mode=False)
    if edit_mode:
        await _show_review(message, state)
        return
    await state.set_state(AddRecord.waiting_next_mileage)
    await message.answer(
        "Введите пробег для следующего ТО (в км):",
        reply_markup=_back_kb("⬅️ Назад", "back:to_next_visit"),
    )


@router.message(AddRecord.waiting_next_mileage, F.text)
async def step_next_mileage(message: Message, state: FSMContext) -> None:
    value = _parse_mileage((message.text or "").strip())
    if value is None:
        await message.answer(
            "Пробег должен быть целым неотрицательным числом. Попробуйте ещё раз."
        )
        return
    data = await state.get_data()
    edit_mode = bool(data.get("edit_mode"))
    await state.update_data(next_service_mileage=value, edit_mode=False)
    if edit_mode:
        await _show_review(message, state)
        return
    await _ask_for_photo(message, state)


# ---------------------------------------------------------------------------
# Шаг waiting_for_photo: фото / пропуск.
# ---------------------------------------------------------------------------

@router.message(AddRecord.waiting_for_photo, F.photo)
async def step_photo_received(message: Message, state: FSMContext) -> None:
    if not message.photo:
        return
    data = await state.get_data()
    photo_ids: list[str] = list(data.get("photo_file_ids") or [])
    if len(photo_ids) >= PHOTO_LIMIT:
        # TG send_media_group лимит = 10. Дальше не принимаем — клиент
        # всё равно не увидит сверх 10 в книжке.
        await message.answer(
            f"⚠️ Достигнут лимит <b>{PHOTO_LIMIT}</b> фото на запись. "
            "Нажмите «✅ Завершить добавление фото» или удалите часть.",
            parse_mode="HTML",
            reply_markup=photo_done_kb(),
        )
        return
    photo_file_id = message.photo[-1].file_id
    photo_ids.append(photo_file_id)
    await state.update_data(photo_file_ids=photo_ids)

    # На первом фото убираем reply-кб «Пропустить» (теперь только «Завершить»).
    if len(photo_ids) == 1:
        await message.answer(
            f"📸 Фото 1 добавлено. Можно отправить ещё.",
            reply_markup=ReplyKeyboardRemove(),
        )
    await message.answer(
        f"Фото в этой записи: <b>{len(photo_ids)}</b>. "
        "Когда закончите — нажмите кнопку ниже.",
        parse_mode="HTML",
        reply_markup=photo_done_kb(),
    )


@router.callback_query(AddRecord.waiting_for_photo, F.data == "photo:done")
async def step_photo_done(cb: CallbackQuery, state: FSMContext) -> None:
    if cb.message is None:
        await cb.answer()
        return
    await cb.answer("Готово")
    await _show_review(cb.message, state)


@router.message(
    AddRecord.waiting_for_photo,
    F.text.in_({SKIP_PHOTO_TEXT, "/skip"}),
)
async def step_photo_skip(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    photo_ids: list[str] = list(data.get("photo_file_ids") or [])
    msg = "Сохраню без фото." if not photo_ids else "Закончил с фото."
    await message.answer(msg, reply_markup=ReplyKeyboardRemove())
    await _show_review(message, state)


@router.message(AddRecord.waiting_for_photo)
async def step_photo_other(message: Message, state: FSMContext) -> None:
    await message.answer(
        "Пришлите фото или нажмите «✅ Завершить добавление фото» / "
        "«➡️ Пропустить фото» (либо /skip).",
    )


# ---------------------------------------------------------------------------
# Кнопки «⬅️ Назад» внутри AddRecord — позволяют мастеру откатиться на
# предыдущий шаг, не теряя весь FSM.
# ---------------------------------------------------------------------------

@router.callback_query(
    F.data == "back:to_client_search", StateFilter(AddRecord)
)
async def back_to_client_search(
    cb: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    if cb.message is None:
        await cb.answer()
        return
    await state.set_state(AddRecord.waiting_search)
    recent = await UserRepository(session).get_recent(3)
    prompt = (
        "Введите ID, часть гос. номера или имени клиента."
        + ("\nИли выберите из недавних:" if recent else "")
    )
    await _safe_send(cb, prompt, recent_clients_kb(recent))
    await cb.answer()


@router.callback_query(
    F.data == "back:to_categories", StateFilter(AddRecord)
)
async def back_to_categories(cb: CallbackQuery, state: FSMContext) -> None:
    if cb.message is None:
        await cb.answer()
        return
    await state.set_state(AddRecord.waiting_service_type)
    data = await state.get_data()
    name = data.get("client_name") or "—"
    cid = data.get("client_id") or "—"
    brand = data.get("client_car_brand") or "—"
    plate = data.get("client_car_plate") or "—"
    text = (
        f"Клиент: <b>{name}</b> (<code>{cid}</code>)\n"
        f"🚗 {brand} [<code>{plate}</code>]\n\n"
        "Что было сделано? Выберите категорию:"
    )
    await _safe_send(cb, text, categories_kb())
    await cb.answer()


@router.callback_query(
    F.data == "back:to_cur_mile", StateFilter(AddRecord)
)
async def back_to_cur_mile(cb: CallbackQuery, state: FSMContext) -> None:
    if cb.message is None:
        await cb.answer()
        return
    await state.set_state(AddRecord.waiting_current_mileage)
    await _safe_send(
        cb,
        "Введите актуальный пробег авто (в км):",
        _back_kb("⬅️ Назад к категориям", "back:to_categories"),
    )
    await cb.answer()


@router.callback_query(
    F.data == "back:to_exec_date", StateFilter(AddRecord)
)
async def back_to_exec_date(cb: CallbackQuery, state: FSMContext) -> None:
    if cb.message is None:
        await cb.answer()
        return
    await state.set_state(AddRecord.waiting_execution_date)
    await _safe_send(
        cb,
        "Укажите дату выполнения работы. Нажмите «Сегодня» или введите дату в формате ДД.ММ.ГГГГ.",
        exec_date_kb(),
    )
    await cb.answer()


@router.callback_query(
    F.data == "back:to_next_choice", StateFilter(AddRecord)
)
async def back_to_next_choice(cb: CallbackQuery, state: FSMContext) -> None:
    if cb.message is None:
        await cb.answer()
        return
    await state.set_state(AddRecord.waiting_next_choice)
    await _safe_send(
        cb,
        "Нужно ли назначить следующее ТО для этой работы?",
        next_choice_kb(),
    )
    await cb.answer()


@router.callback_query(
    F.data == "back:to_next_visit", StateFilter(AddRecord)
)
async def back_to_next_visit(cb: CallbackQuery, state: FSMContext) -> None:
    if cb.message is None:
        await cb.answer()
        return
    await state.set_state(AddRecord.waiting_next_visit)
    await _safe_send(
        cb,
        "Введите дату следующего ТО (ДД.ММ.ГГГГ):",
        _back_kb("⬅️ Назад", "back:to_next_choice"),
    )
    await cb.answer()


@router.callback_query(AddRecord.waiting_confirmation, F.data == "confirm:save")
async def confirm_save(
    cb: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    if cb.message is None:
        await cb.answer()
        return
    data = await state.get_data()
    client_id: int = data["client_id"]
    service_type: str = data["service_type"]
    next_visit_iso: str | None = data.get("next_visit_iso")
    current_mileage: int = data["current_mileage"]
    next_service_mileage: int | None = data.get("next_service_mileage")
    execution_iso: str = data["execution_date_iso"]
    photo_ids: list[str] = list(data.get("photo_file_ids") or [])
    photo_payload: str | None = json.dumps(photo_ids) if photo_ids else None

    # Phase 7: vehicle_id мог быть зафиксирован в FSM либо после
    # _select_client_and_continue (если у клиента 1 машина), либо после
    # callback ar:veh:<id> (если >1). Если клиент без машин — None.
    vehicle_id: int | None = data.get("vehicle_id")

    await RecordRepository(session).add(
        user_id=client_id,
        vehicle_id=vehicle_id,
        service_type=service_type,
        execution_date=execution_iso,
        next_visit_date=next_visit_iso,
        current_mileage=current_mileage,
        next_service_mileage=next_service_mileage,
        photo_file_id=photo_payload,
    )
    # Освобождаем SQLite-write-lock до FSM-записи (см. step_mileage).
    await session.commit()
    await state.clear()
    await cb.answer("Сохранено")

    if next_visit_iso is None and next_service_mileage is None:
        next_block = "След. ТО: <i>разовая работа</i>"
    else:
        next_visit_str = _iso_to_human(next_visit_iso) if next_visit_iso else "—"
        next_mile_str = (
            f"{next_service_mileage}" if next_service_mileage is not None else "—"
        )
        next_block = (
            f"След. визит: <b>{next_visit_str}</b>\n"
            f"🔜 Пробег след. ТО: <b>{next_mile_str}</b> км"
        )
    await cb.message.answer(
        "✅ Запись добавлена.\n\n"
        f"Клиент: <b>{data['client_name']}</b> (<code>{client_id}</code>)\n"
        f"🚗 {data['client_car_brand']} [<code>{data['client_car_plate']}</code>]\n"
        f"Услуга: {service_type}\n"
        f"📅 Выполнено: <b>{_iso_to_human(execution_iso)}</b>\n"
        f"🔢 Текущий пробег: <b>{current_mileage}</b> км\n"
        f"{next_block}",
        parse_mode="HTML",
    )


@router.callback_query(AddRecord.waiting_confirmation, F.data == "edit:service")
async def edit_service(cb: CallbackQuery, state: FSMContext) -> None:
    if cb.message is None:
        await cb.answer()
        return
    await state.update_data(edit_mode=True)
    await state.set_state(AddRecord.waiting_service_type)
    await cb.answer()
    await cb.message.answer(
        "Выберите новую категорию:", reply_markup=edit_categories_kb()
    )


@router.callback_query(AddRecord.waiting_confirmation, F.data == "edit:exec_date")
async def edit_exec_date(cb: CallbackQuery, state: FSMContext) -> None:
    """Точечная правка даты выполнения работ."""
    if cb.message is None:
        await cb.answer()
        return
    await state.update_data(edit_mode=True)
    await state.set_state(AddRecord.waiting_execution_date)
    await cb.answer()
    await cb.message.answer(
        "Введите новую дату выполнения (ДД.ММ.ГГГГ) или нажмите «Сегодня»:",
        reply_markup=edit_exec_date_kb(),
    )


@router.callback_query(AddRecord.waiting_confirmation, F.data == "edit:next_visit")
async def edit_next_visit(cb: CallbackQuery, state: FSMContext) -> None:
    """Точечная правка даты следующего визита."""
    if cb.message is None:
        await cb.answer()
        return
    await state.update_data(edit_mode=True)
    await state.set_state(AddRecord.waiting_next_visit)
    await cb.answer()
    await cb.message.answer(
        "Введите новую дату следующего ТО (ДД.ММ.ГГГГ):",
        reply_markup=cancel_to_review_kb(),
    )


@router.callback_query(AddRecord.waiting_confirmation, F.data == "edit:client")
async def edit_client_in_record(
    cb: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    if cb.message is None:
        await cb.answer()
        return
    await state.update_data(edit_mode=True)
    await cb.answer()
    await _ask_search(cb.message, state, session)


@router.callback_query(AddRecord.waiting_confirmation, F.data == "edit:cur_mile")
async def edit_current_mileage(cb: CallbackQuery, state: FSMContext) -> None:
    if cb.message is None:
        await cb.answer()
        return
    await state.update_data(edit_mode=True)
    await state.set_state(AddRecord.waiting_current_mileage)
    await cb.answer()
    await cb.message.answer(
        "Введите актуальный пробег авто (в км):",
        reply_markup=cancel_to_review_kb(),
    )


@router.callback_query(AddRecord.waiting_confirmation, F.data == "edit:next_mile")
async def edit_next_mileage_cb(cb: CallbackQuery, state: FSMContext) -> None:
    if cb.message is None:
        await cb.answer()
        return
    await state.update_data(edit_mode=True)
    await state.set_state(AddRecord.waiting_next_mileage)
    await cb.answer()
    await cb.message.answer(
        "Введите пробег для следующего ТО (в км):",
        reply_markup=cancel_to_review_kb(),
    )


@router.callback_query(AddRecord.waiting_confirmation, F.data == "photo:clear")
async def review_photo_clear(cb: CallbackQuery, state: FSMContext) -> None:
    """Сбросить массив фотографий и перерисовать ревью."""
    if cb.message is None:
        await cb.answer()
        return
    await state.update_data(photo_file_ids=[])
    await cb.answer("Фото очищены")
    await _show_review(cb.message, state)


@router.callback_query(AddRecord.waiting_confirmation, F.data == "photo:add")
async def review_photo_add(cb: CallbackQuery, state: FSMContext) -> None:
    """Вернуться на шаг добавления фото — сохраняя уже прикреплённые."""
    if cb.message is None:
        await cb.answer()
        return
    data = await state.get_data()
    photo_ids: list[str] = list(data.get("photo_file_ids") or [])
    await state.set_state(AddRecord.waiting_for_photo)
    await cb.answer()
    if photo_ids:
        await cb.message.answer(
            f"Уже прикреплено: <b>{len(photo_ids)}</b>. "
            "Пришлите ещё фото или нажмите кнопку ниже.",
            parse_mode="HTML",
            reply_markup=photo_done_kb(),
        )
    else:
        await cb.message.answer(
            "📸 Отправьте фото — можно несколько. "
            "Чтобы вернуться без фото — нажмите кнопку ниже.",
            reply_markup=PHOTO_PROMPT_KB,
        )


@router.callback_query(StateFilter(AddRecord), F.data == "back:to_review")
async def back_to_review(cb: CallbackQuery, state: FSMContext) -> None:
    """Возврат в карточку «Проверьте данные» из любого edit-промпта."""
    if cb.message is None:
        await cb.answer()
        return
    data = await state.get_data()
    if "client_id" not in data or "execution_date_iso" not in data:
        await cb.answer("Нечего подтверждать", show_alert=False)
        return
    await state.update_data(edit_mode=False)
    await cb.answer()
    await _show_review(cb.message, state)


# ---------------------------------------------------------------------------
# /test_notify — для ручной проверки ежедневных уведомлений
# ---------------------------------------------------------------------------

@router.message(Command("test_notify"), IsAdmin())
async def cmd_test_notify(message: Message) -> None:
    """Ручной запуск логики ежедневных уведомлений (обычно в 10:00)."""
    if message.bot is None:
        return
    sent = await run_notifications(message.bot)
    await message.answer(
        f"Проверка уведомлений выполнена. Отправлено сообщений: <b>{sent}</b>.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# 📋 Последние 5 записей и удаление одной записи
# ---------------------------------------------------------------------------

@router.message(F.text == BTN_LAST_RECORDS, IsAdmin())
async def btn_last_records(message: Message, session: AsyncSession) -> None:
    rows = await RecordRepository(session).get_last_with_profile(5)
    if not rows:
        await message.answer("В базе пока нет записей.")
        return
    await message.answer(f"Последние {len(rows)} записей:")
    for row in rows:
        await message.answer(
            _format_record_with_profile(row),
            parse_mode="HTML",
            reply_markup=delete_kb(row["id"]),
        )


@router.callback_query(F.data.startswith("del:"), IsAdmin())
async def cb_delete_record(cb: CallbackQuery, session: AsyncSession) -> None:
    if cb.data is None:
        await cb.answer()
        return
    try:
        record_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        await cb.answer("Некорректный ID записи", show_alert=True)
        return

    deleted = await RecordRepository(session).delete(record_id)
    if not deleted:
        await cb.answer("Запись уже удалена", show_alert=True)
        if cb.message is not None:
            await cb.message.edit_reply_markup(reply_markup=None)
        return

    await cb.answer("Запись удалена")
    if cb.message is not None:
        original = cb.message.html_text or ""
        await cb.message.edit_text(
            f"<s>{original}</s>\n\n🗑 <b>Запись удалена</b>",
            parse_mode="HTML",
        )


# ---------------------------------------------------------------------------
# Fallback-хэндлер для нетекстовых апдейтов в AddRecord. Регистрируется
# последним внутри модуля.
# ---------------------------------------------------------------------------

@router.message(StateFilter(AddRecord), IsAdmin())
async def fallback_add_record_state(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    await message.answer(
        f"Не понял ввод (текущий шаг: <code>{current}</code>). "
        "Используйте кнопки выше или /cancel для отмены.",
        parse_mode="HTML",
    )
