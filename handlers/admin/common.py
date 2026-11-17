"""
Общие константы, клавиатуры, форматтеры и хэлперы админ-панели.

Этот модуль НЕ содержит ``Router``-а и не регистрирует хэндлеров —
сюда вынесено всё, что нужно нескольким суб-роутерам пакета
:mod:`handlers.admin` (``clients``, ``records``, ``search``, ``requests``).
"""

from __future__ import annotations

import re
from datetime import datetime

from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from sqlalchemy.ext.asyncio import AsyncSession

from database.repositories import UserRepository, VehicleRepository
from states import AddClient, AddRecord


# ---------------------------------------------------------------------------
# Константы интерфейса
# ---------------------------------------------------------------------------

CATEGORIES: list[tuple[str, str]] = [
    ("plan", "📦 Плановое ТО"),
    ("brakes", "🛑 Тормоза"),
    ("chassis", "🚜 Подвеска"),
    ("trans", "⚙️ Трансмиссия"),
    ("electric", "⚡ Электрика"),
    ("cooling", "🌡️ Климат"),
    ("engine", "🔧 Двигатель"),
    ("other", "📁 Другое"),
]
CATEGORY_NAMES: dict[str, str] = dict(CATEGORIES)

# Двухуровневые подкатегории. Для "other" — ввод текстом.
SUBCATEGORIES: dict[str, list[tuple[str, str]]] = {
    "plan": [
        ("oil_engine", "Масло ДВС"),
        ("air_filter", "Фильтр возд."),
        ("cabin_filter", "Фильтр салон."),
        ("spark_plugs", "Свечи"),
        ("fuel_filter", "Топливный фильтр"),
    ],
    "brakes": [
        ("pads_front", "Колодки перед."),
        ("pads_rear", "Колодки зад."),
        ("brake_disks", "Диски торм."),
        ("brake_fluid", "Торм. жидкость"),
        ("calipers", "Суппорты"),
    ],
    "chassis": [
        ("shocks", "Амортизаторы"),
        ("arms_silent", "Рычаги/Сайлентблоки"),
        ("ball_joints", "Шаровые"),
        ("steering", "Рулевое"),
        ("hubs", "Ступицы"),
    ],
    "trans": [
        ("oil_trans", "Масло АКПП/МКПП"),
        ("clutch", "Сцепление"),
        ("cv_joints", "ШРУС/Привод"),
    ],
    "electric": [
        ("diagnostics", "Комп. диагностика"),
        ("battery", "АКБ"),
        ("alt_starter", "Генератор/Стартер"),
        ("lighting", "Освещение"),
    ],
    "cooling": [
        ("ac_recharge", "Заправка кондиц."),
        ("antifreeze", "Антифриз"),
        ("radiator", "Радиатор"),
        ("pump_thermo", "Помпа/Термостат"),
    ],
    "engine": [
        ("timing", "ГРМ (ремень/цепь)"),
        ("aux_belt", "Приводной ремень"),
        ("gaskets", "Прокладки"),
        ("exhaust", "Выхлоп"),
    ],
    "other": [],  # ручной ввод
}
SUBCATEGORY_NAMES: dict[tuple[str, str], str] = {
    (cat_key, sub_key): sub_label
    for cat_key, subs in SUBCATEGORIES.items()
    for sub_key, sub_label in subs
}

BTN_ADD_CLIENT = "👤 Добавить клиента"
BTN_ADD_RECORD = "📝 Добавить запись ТО"
BTN_CLIENTS_DB = "🗂 База клиентов"
BTN_LAST_RECORDS = "📋 Последние 5 записей"
BTN_REQUESTS = "📥 Заявки"

ADMIN_PANEL_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_ADD_CLIENT)],
        [KeyboardButton(text=BTN_ADD_RECORD)],
        [KeyboardButton(text=BTN_CLIENTS_DB)],
        [KeyboardButton(text=BTN_LAST_RECORDS)],
        [KeyboardButton(text=BTN_REQUESTS)],
    ],
    resize_keyboard=True,
)


SKIP_PHOTO_TEXT = "➡️ Пропустить фото"

PHOTO_PROMPT_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=SKIP_PHOTO_TEXT)]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

# Универсальная кнопка «пропустить» для опциональных шагов FSM
# регистрации авто (год выпуска, объём двигателя). Текст обрабатывается
# хэндлерами как «пользователь ничего не вводит → поле NULL».
SKIP_OPTIONAL_TEXT = "➡️ Пропустить"

SKIP_OPTIONAL_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=SKIP_OPTIONAL_TEXT)]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

PHOTO_LIMIT = 10  # лимит TG send_media_group


_DIGITS_RE = re.compile(r"\d+")


# ---------------------------------------------------------------------------
# Утилиты форматирования
# ---------------------------------------------------------------------------

def _iso_to_human(iso_date: str) -> str:
    """YYYY-MM-DD → ДД.ММ.ГГГГ."""
    return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d.%m.%Y")


def _client_label(row) -> str:
    """Подпись для inline-кнопки выбора клиента."""
    return f"{row['full_name']} | {row['car_plate']}"


def _format_review(data: dict) -> str:
    next_iso = data.get("next_visit_iso")
    next_mile = data.get("next_service_mileage")
    if next_iso is None and next_mile is None:
        next_block = "След. ТО: <i>разовая работа</i>"
    else:
        next_visit_str = _iso_to_human(next_iso) if next_iso else "—"
        next_mile_str = f"{next_mile}" if next_mile is not None else "—"
        next_block = (
            f"След. визит: <b>{next_visit_str}</b>\n"
            f"🔜 Пробег след. ТО: <b>{next_mile_str}</b> км"
        )
    photo_ids = data.get("photo_file_ids") or []
    if photo_ids:
        photo_line = f"📸 Фото: <b>{len(photo_ids)} шт.</b>"
    else:
        photo_line = "📸 Фото: <i>нет</i>"
    return (
        "<b>Проверьте данные:</b>\n"
        f"Клиент: <b>{data['client_name']}</b> "
        f"(<code>{data['client_id']}</code>)\n"
        f"🚗 {data['client_car_brand']} [<code>{data['client_car_plate']}</code>]\n"
        f"Категория: {data['service_type']}\n"
        f"📅 Дата выполнения: <b>{_iso_to_human(data['execution_date_iso'])}</b>\n"
        f"🔢 Текущий пробег: <b>{data['current_mileage']}</b> км\n"
        f"{next_block}\n"
        f"{photo_line}"
    )


def _format_record_with_profile(row) -> str:
    full_name = row["full_name"] or "—"
    car_brand = row["car_brand"] or "—"
    car_plate = row["car_plate"] or "—"
    cur = row["current_mileage"]
    nxt = row["next_service_mileage"]
    mileage_line = (
        f"\n🔢 {cur} км → 🔜 след. ТО в {nxt} км"
        if cur is not None and nxt is not None
        else ""
    )
    return (
        f"🧾 <b>Запись #{row['id']}</b>\n"
        f"👤 {full_name} | 🚗 {car_brand} [<code>{car_plate}</code>]\n"
        f"Услуга: {row['service_type']}\n"
        f"Дата: <b>{_iso_to_human(row['execution_date'])}</b>{mileage_line}"
    )


def _format_client_card(data: dict) -> str:
    """Карточка клиента до сохранения (review)."""
    year = data.get("car_year")
    engine = data.get("car_engine_volume")
    fuel = data.get("car_fuel_type")
    vin = data.get("car_vin")
    tech_bits = []
    if year:
        tech_bits.append(f"<b>{year}</b> г.")
    if engine is not None:
        # 2.0 -> "2.0L"; целое 2 -> "2L".
        tech_bits.append(
            f"<b>{engine:g}</b>L" if isinstance(engine, float) else f"<b>{engine}</b>L"
        )
    if fuel:
        tech_bits.append(f"⛽ {fuel}")
    tech_line = (
        f"\n🛠 ТТХ: {' • '.join(tech_bits)}" if tech_bits else ""
    )
    # VIN — отдельной строкой моноширинно, чтобы мастер мог скопировать
    # одним кликом в каталог запчастей. Tap-to-copy в Telegram работает
    # для блоков <code>...</code>.
    vin_line = f"\n🔖 VIN: <code>{vin}</code>" if vin else ""
    return (
        "<b>Карточка клиента:</b>\n"
        f"👤 Имя: <b>{data['full_name']}</b>\n"
        f"🚗 Авто: {data['car_brand']}\n"
        f"🔢 Номер: <code>{data['car_plate']}</code>"
        f"{tech_line}"
        f"{vin_line}\n"
        f"🆔 ID: <code>{data['client_id']}</code>"
    )


def _format_client_profile(profile, vehicle=None) -> str:
    """Карточка профиля сохранённого клиента.

    :param vehicle: опционально — объект ``VehicleRow`` (главная машина),
        чтобы показать VIN, топливо и ТТХ. Если None — карточка
        ограничена legacy-полями users.
    """
    username = (
        f"\n@{profile['username']}" if profile["username"] else ""
    )
    mileage_line = (
        f"\n📍 Текущий статус (от клиента): <b>{profile['last_known_mileage']}</b> км"
        if profile["last_known_mileage"] is not None
        else ""
    )
    extra_lines: list[str] = []
    if vehicle is not None:
        # ТТХ-строка (год/объём/топливо) — собираем что есть.
        tech_bits: list[str] = []
        if vehicle.year:
            tech_bits.append(f"<b>{vehicle.year}</b> г.")
        if vehicle.engine_volume is not None:
            tech_bits.append(
                f"<b>{vehicle.engine_volume:g}</b>L"
            )
        if vehicle.fuel_type:
            tech_bits.append(f"⛽ {vehicle.fuel_type}")
        if tech_bits:
            extra_lines.append("🛠 ТТХ: " + " • ".join(tech_bits))
        # VIN — всегда отдельной строкой моноширинно (tap-to-copy).
        if vehicle.vin:
            extra_lines.append(f"🔖 VIN: <code>{vehicle.vin}</code>")
    extra_block = ("\n" + "\n".join(extra_lines)) if extra_lines else ""
    return (
        "<b>Профиль клиента:</b>\n"
        f"👤 <b>{profile['full_name']}</b>{username}\n"
        f"🆔 <code>{profile['telegram_id']}</code>\n"
        f"🚗 {profile['car_brand']} [<code>{profile['car_plate']}</code>]"
        f"{extra_block}"
        f"{mileage_line}"
    )


def _parse_mileage(text: str) -> int | None:
    """Привести ввод пробега к ``int`` (≥0). Извлекаем все цифры из строки.

    Терпит пользовательские варианты записи: ``"125 000 км"``,
    ``"125.000"``, ``"125,000"``, ``"125_000"``, ``"~125000"`` →
    ``125000``. Если в строке вовсе нет цифр — возвращаем ``None``.

    Верхний санити-лимит — 10_000_000 км (за пределами реального
    пробега любого авто, скорее всего опечатка).
    """
    digits = "".join(_DIGITS_RE.findall(text))
    if not digits:
        return None
    value = int(digits)
    if value > 10_000_000:
        return None
    return value


# ---------------------------------------------------------------------------
# Inline-клавиатуры
# ---------------------------------------------------------------------------

def _back_button(label: str, callback: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=label, callback_data=callback)


def _back_kb(label: str, callback: str) -> InlineKeyboardMarkup:
    """Inline-клавиатура из единственной кнопки «⬅️ Назад»."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[_back_button(label, callback)]]
    )


def categories_kb() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(CATEGORIES), 2):
        chunk = CATEGORIES[i : i + 2]
        rows.append(
            [
                InlineKeyboardButton(text=label, callback_data=f"cat:{key}")
                for key, label in chunk
            ]
        )
    rows.append([
        _back_button("⬅️ Назад к выбору клиента", "back:to_client_search")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subcategories_kb(category_key: str) -> InlineKeyboardMarkup:
    """Inline-клавиатура подкатегорий выбранной категории."""
    subs = SUBCATEGORIES.get(category_key, [])
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(subs), 2):
        chunk = subs[i : i + 2]
        rows.append(
            [
                InlineKeyboardButton(
                    text=label, callback_data=f"subcat:{sub_key}"
                )
                for sub_key, label in chunk
            ]
        )
    rows.append([
        _back_button("⬅️ Назад к категориям", "back:to_categories")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def review_kb(*, has_photos: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="✅ Сохранить", callback_data="confirm:save")],
        [
            InlineKeyboardButton(text="✏️ Услуга", callback_data="edit:service"),
            InlineKeyboardButton(text="📅 Дата вып.", callback_data="edit:exec_date"),
        ],
        [
            InlineKeyboardButton(text="📅 След. визит", callback_data="edit:next_visit"),
            InlineKeyboardButton(text="🔢 Тек. пробег", callback_data="edit:cur_mile"),
        ],
        [
            InlineKeyboardButton(text="🔜 Пробег след. ТО", callback_data="edit:next_mile"),
            InlineKeyboardButton(text="👤 ID", callback_data="edit:client"),
        ],
        [
            InlineKeyboardButton(text="📸 Добавить фото", callback_data="photo:add"),
        ],
    ]
    if has_photos:
        rows[-1].append(
            InlineKeyboardButton(text="🗑 Очистить фото", callback_data="photo:clear")
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def exec_date_kb() -> InlineKeyboardMarkup:
    """Inline-клавиатура для быстрого ввода даты выполнения работ."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Сегодня", callback_data="exec:today")],
            [_back_button("⬅️ Назад", "back:to_cur_mile")],
        ]
    )


def next_choice_kb() -> InlineKeyboardMarkup:
    """Inline-клавиатура: назначить следующее ТО или оставить разовой."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Назначить дату/пробег", callback_data="nxt:yes")],
            [InlineKeyboardButton(text="❌ Не требуется", callback_data="nxt:no")],
            [_back_button("⬅️ Назад", "back:to_exec_date")],
        ]
    )


def delete_kb(record_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del:{record_id}")]
        ]
    )


def recent_clients_kb(
    rows, *, with_cancel_to_review: bool = False
) -> InlineKeyboardMarkup | None:
    inline_rows: list[list[InlineKeyboardButton]] = []
    for row in rows or []:
        inline_rows.append([
            InlineKeyboardButton(
                text=f"🕒 {_client_label(row)}",
                callback_data=f"pick:{row['telegram_id']}",
            )
        ])
    if with_cancel_to_review:
        inline_rows.append([
            _back_button("⬅️ Назад к проверке", "back:to_review")
        ])
    if not inline_rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=inline_rows)


def cancel_to_review_kb() -> InlineKeyboardMarkup:
    """Inline-кб «⬅️ Назад к проверке» для edit-промптов с текстовым вводом."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            _back_button("⬅️ Назад к проверке", "back:to_review")
        ]]
    )


def cancel_to_client_review_kb() -> InlineKeyboardMarkup:
    """Inline-кб «⬅️ Назад к проверке» для edit-промптов AddClient.

    Возвращает в карточку клиента (AddClient.waiting_review) без потери
    уже введённых полей.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            _back_button("⬅️ Назад к проверке", "back:to_client_review")
        ]]
    )


def edit_categories_kb() -> InlineKeyboardMarkup:
    """Категории + ряд «⬅️ Назад к проверке» для edit-режима."""
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(CATEGORIES), 2):
        chunk = CATEGORIES[i : i + 2]
        rows.append(
            [
                InlineKeyboardButton(text=label, callback_data=f"cat:{key}")
                for key, label in chunk
            ]
        )
    rows.append([_back_button("⬅️ Назад к проверке", "back:to_review")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def edit_exec_date_kb() -> InlineKeyboardMarkup:
    """«📅 Сегодня» + «⬅️ Назад к проверке» — версия для edit-режима."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Сегодня", callback_data="exec:today")],
            [_back_button("⬅️ Назад к проверке", "back:to_review")],
        ]
    )


def search_results_kb(rows, prefix: str) -> InlineKeyboardMarkup:
    """Inline-список найденных клиентов с заданным префиксом callback."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_client_label(row),
                    callback_data=f"{prefix}:{row['telegram_id']}",
                )
            ]
            for row in rows
        ]
    )


def create_new_client_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👤 Создать нового клиента",
                    callback_data="search:new",
                )
            ]
        ]
    )


def add_client_review_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Все верно", callback_data="ac:save")],
            [
                InlineKeyboardButton(text="✏️ Имя", callback_data="ac:name"),
                InlineKeyboardButton(text="✏️ Авто", callback_data="ac:brand"),
                InlineKeyboardButton(text="✏️ Номер", callback_data="ac:plate"),
            ],
            [
                InlineKeyboardButton(text="✏️ Год", callback_data="ac:year"),
                InlineKeyboardButton(text="✏️ Объём", callback_data="ac:engine"),
            ],
            [
                InlineKeyboardButton(text="✏️ VIN", callback_data="ac:vin"),
                InlineKeyboardButton(text="✏️ Топливо", callback_data="ac:fuel"),
            ],
        ]
    )


def fuel_type_kb(*, with_skip: bool = True) -> InlineKeyboardMarkup:
    """Inline-клавиатура выбора типа топлива (Phase 7 add-on).

    Используется и при регистрации клиента, и в WebApp-режиме (через UI),
    но в боте показывается всегда. callback_data: ``fuel:<тип>`` либо
    ``fuel:skip`` (если ``with_skip``).
    """
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="⛽ Бензин", callback_data="fuel:Бензин"),
            InlineKeyboardButton(text="🛢 Дизель", callback_data="fuel:Дизель"),
        ],
        [
            InlineKeyboardButton(text="🔋 Гибрид", callback_data="fuel:Гибрид"),
            InlineKeyboardButton(text="⚡ Электро", callback_data="fuel:Электро"),
        ],
    ]
    if with_skip:
        rows.append([
            InlineKeyboardButton(
                text="➡️ Пропустить", callback_data="fuel:skip"
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def client_profile_kb(client_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Редактировать профиль",
                    callback_data=f"ep:menu:{client_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📋 История ТО", callback_data=f"hist:{client_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Удалить клиента", callback_data=f"delc:{client_id}"
                )
            ],
        ]
    )


def edit_profile_menu_kb(client_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ Имя", callback_data=f"ep:name:{client_id}"),
                InlineKeyboardButton(text="✏️ Марка", callback_data=f"ep:brand:{client_id}"),
                InlineKeyboardButton(text="✏️ Номер", callback_data=f"ep:plate:{client_id}"),
            ],
            [
                InlineKeyboardButton(
                    text="⬅ Назад к профилю", callback_data=f"browse:{client_id}"
                )
            ],
        ]
    )


def delete_client_confirm_kb(client_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Подтвердить удаление",
                    callback_data=f"delcyes:{client_id}",
                ),
                InlineKeyboardButton(
                    text="↩ Отмена", callback_data=f"browse:{client_id}"
                ),
            ]
        ]
    )


def photo_done_kb() -> InlineKeyboardMarkup:
    """Inline-кб «✅ Завершить добавление фото» (видна после 1-го фото)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ Завершить добавление фото",
                callback_data="photo:done",
            )
        ]]
    )


# ---------------------------------------------------------------------------
# Внутренние хелперы потока
# ---------------------------------------------------------------------------

async def _show_review(message: Message, state: FSMContext) -> None:
    """Перевести AddRecord в состояние подтверждения и отрисовать карточку."""
    await state.set_state(AddRecord.waiting_confirmation)
    data = await state.get_data()
    has_photos = bool(data.get("photo_file_ids"))
    await message.answer(
        _format_review(data),
        parse_mode="HTML",
        reply_markup=review_kb(has_photos=has_photos),
    )


async def _ask_for_photo(message: Message, state: FSMContext) -> None:
    """Шаг waiting_for_photo: запросить фото или предложить пропустить."""
    await state.update_data(photo_file_ids=[])
    await state.set_state(AddRecord.waiting_for_photo)
    await message.answer(
        "📸 Отправьте фото (например, чека или замененной детали) — "
        "можно несколько. "
        "Чтобы сохранить без фото — нажмите кнопку ниже.",
        reply_markup=PHOTO_PROMPT_KB,
    )


async def _show_client_review(message: Message, state: FSMContext) -> None:
    """Перевести AddClient в состояние review и отрисовать карточку клиента."""
    await state.set_state(AddClient.waiting_review)
    data = await state.get_data()
    await message.answer(
        _format_client_card(data),
        parse_mode="HTML",
        reply_markup=add_client_review_kb(),
    )


async def _show_client_profile(
    message: Message, profile, session: AsyncSession
) -> None:
    """Отрисовать карточку профиля клиента из «🗂 База клиентов».

    Если ``profile`` пришёл из выборки, где нет колонки
    ``last_known_mileage`` (поиск/recent), подгружаем полный
    профиль из БД.
    """
    if not hasattr(profile, "last_known_mileage"):
        full = await UserRepository(session).get(int(profile["telegram_id"]))
        if full is None:
            await message.answer("Не удалось загрузить профиль клиента.")
            return
        profile = full
    # Phase 7 add-on: подтягиваем главную машину, чтобы вывести VIN/топливо
    # моноширинно — мастер копирует VIN одним тапом в каталог запчастей.
    primary_vehicle = await VehicleRepository(session).get_primary(
        int(profile["telegram_id"])
    )
    await message.answer(
        _format_client_profile(profile, vehicle=primary_vehicle),
        parse_mode="HTML",
        reply_markup=client_profile_kb(profile["telegram_id"]),
    )


async def _safe_send(
    cb: CallbackQuery,
    text: str,
    kb: InlineKeyboardMarkup | None,
) -> None:
    """Перерисовать текущее сообщение либо, если нельзя — отправить новое."""
    if cb.message is None:
        return
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


async def _ask_search(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    """Шаг 1 AddRecord: запросить поиск + показать «Недавние»."""
    data = await state.get_data()
    edit_mode = bool(data.get("edit_mode"))
    await state.set_state(AddRecord.waiting_search)
    recent = await UserRepository(session).get_recent(3)
    prompt = (
        "Введите ID, часть гос. номера или имени клиента."
        + ("\nИли выберите из недавних:" if recent else "")
    )
    kb = recent_clients_kb(recent, with_cancel_to_review=edit_mode)
    if kb is None and edit_mode:
        kb = cancel_to_review_kb()
    await message.answer(prompt, reply_markup=kb)


def admin_vehicle_picker_kb(vehicles) -> InlineKeyboardMarkup:
    """Клавиатура выбора машины при добавлении записи (admin FSM)."""
    rows: list[list[InlineKeyboardButton]] = []
    for v in vehicles:
        label = f"🚗 {v.brand}"
        if v.plate_number:
            label += f" · {v.plate_number}"
        if len(label) > 60:
            label = label[:57] + "…"
        rows.append([
            InlineKeyboardButton(
                text=label, callback_data=f"ar:veh:{v.id}"
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _select_client_and_continue(
    message: Message,
    state: FSMContext,
    profile_row,
    session: AsyncSession,
) -> None:
    """Сохранить выбранного клиента в FSM и пойти дальше.

    Phase 7: между выбором клиента и категорией услуги вставлен опциональный
    шаг выбора машины. Если у клиента ровно 1 машина — авто-проставляем
    её ``vehicle_id`` без UI-шага. Если 0 — fallback на legacy-путь без
    привязки. Если >1 — показываем inline-выбор.
    """
    data = await state.get_data()
    edit_mode = bool(data.get("edit_mode"))
    await state.update_data(
        client_id=profile_row["telegram_id"],
        client_name=profile_row["full_name"],
        client_car_brand=profile_row["car_brand"],
        client_car_plate=profile_row["car_plate"],
        edit_mode=False,
    )
    if edit_mode:
        await _show_review(message, state)
        return

    vehicles = await VehicleRepository(session).list_for_user(
        profile_row["telegram_id"]
    )
    if len(vehicles) > 1:
        await state.set_state(AddRecord.waiting_vehicle)
        await message.answer(
            f"Клиент: <b>{profile_row['full_name']}</b> "
            f"(<code>{profile_row['telegram_id']}</code>)\n\n"
            "🚗 У клиента несколько авто. <b>Для какой машины делаем "
            "запись?</b>",
            parse_mode="HTML",
            reply_markup=admin_vehicle_picker_kb(vehicles),
        )
        return

    # 0 или 1 машина — пропускаем шаг выбора, сразу к категории.
    chosen_vehicle = vehicles[0] if vehicles else None
    await state.update_data(
        vehicle_id=chosen_vehicle.id if chosen_vehicle else None,
        vehicle_label=(
            f"{chosen_vehicle.brand} [{chosen_vehicle.plate_number}]"
            if chosen_vehicle
            else None
        ),
    )
    await state.set_state(AddRecord.waiting_service_type)
    await message.answer(
        f"Клиент: <b>{profile_row['full_name']}</b> "
        f"(<code>{profile_row['telegram_id']}</code>)\n"
        f"🚗 {profile_row['car_brand']} [<code>{profile_row['car_plate']}</code>]\n\n"
        "Что было сделано? Выберите категорию:",
        parse_mode="HTML",
        reply_markup=categories_kb(),
    )
