"""
Хэндлеры для клиентов (любые пользователи Telegram, не входящие в таблицу ``admins``).

Команда ``/start``:
* если пользователя нет в таблице ``users`` — выводим
  «⛔ Доступ ограничен» с подсказкой передать свой Telegram ID мастеру;
* если есть — приветствие, данные авто и история ТО, плюс reply-клавиатура
  с кнопкой «🔢 Обновить мой пробег».

Кнопка «🔢 Обновить мой пробег» запускает FSM
:class:`states.UpdateMileage`: клиент вводит число → бот сохраняет
``users.last_known_mileage`` и сравнивает с ``next_service_mileage``
из последней записи ТО.

Админский ``/start`` обрабатывается раньше в :mod:`handlers.admin`
(через фильтр :class:`filters.IsAdmin`).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart, StateFilter
from filters import IsAdmin
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)
from aiogram.filters import Command
from sqlalchemy.ext.asyncio import AsyncSession

from config import WEBAPP_URL
from database.repositories import (
    AdminRepository,
    RecordRepository,
    UserRepository,
    VehicleRepository,
)
from states import RequestCall, UpdateMileage

logger = logging.getLogger(__name__)

router = Router(name="user-router")


BTN_UPDATE_MILEAGE = "🔢 Обновить мой пробег"
BTN_SERVICE_BOOK = "📖 Моя сервисная книжка"
BTN_REQUEST_CALL = "📞 Заказать звонок"
BTN_SEND_PHONE = "📱 Отправить номер телефона"
BTN_CANCEL = "❌ Отмена"

# Рефактор по обратной связи с юзером (iOS 9.6, tdesktop 9.6):
# Reply-`KeyboardButton(web_app=...)` НЕ отдаёт ``tgWebAppData``
# на всех клиентах (известное ограничение Telegram — в hash
# приходят ``tgWebAppVersion/Platform/ThemeParams``, но нет ``user``).
# Инлайн-кнопка ``InlineKeyboardButton(web_app=...)`` — работает
# везде и всегда передаёт полный initData. Поэтому reply-кнопка
# становится просто текстом, хэндлер отвечает сообщением с
# инлайн-кнопкой запуска (если задан ``WEBAPP_URL``) либо текстовой
# выдачей (fallback).


def service_book_inline_kb() -> InlineKeyboardMarkup | None:
    """Inline-кнопка «Открыть сервисную книжку»,
    запускающая Mini App. Гарантированно передаёт ``initData``
    на всех клиентах (в отличие от reply-кнопки).

    Если ``WEBAPP_URL`` не задан — возвращаем ``None``,
    вызывающий код должен упасть на текстовый fallback.
    """
    if not WEBAPP_URL:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="📖 Открыть сервисную книжку",
                web_app=WebAppInfo(url=WEBAPP_URL),
            ),
        ]]
    )


def welcome_inline_kb() -> InlineKeyboardMarkup | None:
    """Premium-клавиатура под корпоративным приветствием /start (Phase 10).

    Две кнопки: «📖 Открыть сервисную книжку» (WebApp) и
    «📅 Записаться на сервис» (callback ``start:book_service`` —
    реюзит существующий FSM ``RequestCall``).

    Если ``WEBAPP_URL`` не задан — отдаём single-row клавиатуру только
    с записью на сервис, чтобы клиент не остался без CTA.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if WEBAPP_URL:
        rows.append([
            InlineKeyboardButton(
                text="📖 Открыть сервисную книжку",
                web_app=WebAppInfo(url=WEBAPP_URL),
            ),
        ])
    rows.append([
        InlineKeyboardButton(
            text="📅 Записаться на сервис",
            callback_data="start:book_service",
        ),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


# Phase 11: убрали CLIENT_PANEL_KB полностью. Бот больше НЕ держит
# reply-клавиатуру у клиента: всё взаимодействие идёт через мини-апп
# (Menu Button «📎» рядом с полем ввода + inline-кнопка под /start).
# Reply-кнопки «🔢 Обновить пробег» и «📞 Заказать звонок» выпиливаем
# как класс — пробег теперь правится прямо в WebApp, а звонок —
# через inline-кнопку в /start. Чат-интерфейс остаётся абсолютно
# чистым, ровно как просит ТЗ.


def share_contact_kb() -> ReplyKeyboardMarkup:
    """Phase 20.1: reply-keyboard c одной кнопкой
    «📱 Поделиться контактом» (``request_contact=True``).

    В отличие от ``request_phone_kb``, эта клавиатура НЕ связана с
    FSM-состоянием ``RequestCall.waiting_phone`` — её клиент получает
    ровно один раз, после клика по «🗓 Записаться на сервис» в мини-
    аппе у клиента без подтверждённого телефона. После получения
    контакта клавиатура скрывается, клиент возвращается в мини-апп и
    может записаться с выбором окна заезда.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(
                text="📱 Поделиться контактом", request_contact=True
            )],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def request_phone_kb() -> ReplyKeyboardMarkup:
    """Клавиатура с кнопкой «📱 Отправить номер телефона».

    ВНИМАНИЕ: это единственный оставшийся ReplyKeyboardMarkup в
    клиентском флоу — Telegram-API не даёт получить контакт без
    специальной кнопки ``request_contact=True``. Чтобы клавиатура
    не висела после отправки контакта, ставим ``one_time_keyboard``
    + она сразу скрывается через ``ReplyKeyboardRemove`` в обработчике
    `request_call_contact` после получения номера.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SEND_PHONE, request_contact=True)],
            [KeyboardButton(text=BTN_CANCEL)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def skip_description_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="➡️ Пропустить", callback_data="req:skip"),
        ]]
    )


def admin_request_kb(request_id: int, telegram_id: int) -> InlineKeyboardMarkup:
    """Кнопки под уведомлением/карточкой заявки в журнале."""
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


def _format_request_notification(
    *,
    full_name: str,
    username: str | None,
    phone: str,
    car_brand: str | None,
    car_plate: str | None,
    description: str | None,
) -> str:
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


def _human_date(iso_date: str) -> str:
    """YYYY-MM-DD → ДД.ММ.ГГГГ (или вернуть исходник, если формат неверный)."""
    try:
        return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return iso_date


# Защитный фильтр `~IsAdmin()` гарантирует, что admin'у клиентский
# `/start` не покажется ни при каких условиях, даже если порядок
# подключения роутеров когда-то изменится. Админу /start уходит в
# `handlers.admin.cmd_start_admin` и отдаёт ADMIN_PANEL_KB.
@router.message(CommandStart(), ~IsAdmin())
async def cmd_start_client(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    if message.from_user is None:
        return

    # Сбрасываем активный FSM, если он есть, чтобы /start гарантированно
    # возвращал клиента в чистое состояние «главное меню».
    current = await state.get_state()
    if current is not None:
        await state.clear()
        logger.info("FSM cleared by /start (client) (was=%s, user=%s)",
                    current, message.from_user.id)

    user_id = message.from_user.id
    users = UserRepository(session)
    profile = await users.get(user_id)

    if profile is None:
        await message.answer(
            "⛔ Доступ ограничен. Вы ещё не зарегистрированы в нашей системе. "
            f"Сообщите ваш ID мастеру: <code>{user_id}</code>",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove(),
        )
        logger.info("/start (unregistered) от %s", user_id)
        return

    # Синхронизируем username, если он есть/поменялся.
    new_username = message.from_user.username
    if (new_username or None) != (profile.username or None):
        await users.update_username(user_id, new_username)

    logger.info("/start (client) от %s", user_id)

    # Phase 10: чистый корпоративный приветственный экран. Никакого
    # дампа истории/пробега/ID/госномера в чат — вся эта информация
    # доступна в WebApp по нажатию кнопки ниже. Цель — premium UX,
    # а не "стенограмма из БД в превью чата".
    full_name = profile["full_name"] or message.from_user.full_name or "клиент"
    welcome_text = (
        f"Здравствуйте, <b>{full_name}</b>!\n\n"
        "Ваша электронная сервисная книжка. Здесь хранится полная "
        "история обслуживания вашего автопарка, ведётся учёт пробега "
        "и формируются рекомендации по регламентному ТО."
    )

    # Phase 11: жёсткая зачистка — всегда шлём ReplyKeyboardRemove(),
    # чтобы у клиентов с зависшей старой клавиатурой («🔢 Обновить
    # пробег», «📞 Заказать звонок») она схлопнулась. Чат остаётся
    # абсолютно пустым снизу — premium UX.
    await message.answer(
        welcome_text,
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )

    # Главный CTA — inline-клавиатура с двумя кнопками (WebApp + booking).
    # Отдельным сообщением, потому что Telegram запрещает совмещать reply
    # и inline в одном message.
    inline_kb = welcome_inline_kb()
    if inline_kb is not None:
        await message.answer(
            "Выберите действие 👇",
            reply_markup=inline_kb,
        )


# ---------------------------------------------------------------------------
# 🔢 Обновить мой пробег
# ---------------------------------------------------------------------------

def _vehicle_picker_kb(vehicles) -> InlineKeyboardMarkup:
    """Клавиатура выбора машины для обновления пробега. Один ряд = одна
    машина, в callback_data — её id."""
    rows: list[list[InlineKeyboardButton]] = []
    for v in vehicles:
        # «🚗 Volvo · 1111KA-6» — короткая, но узнаваемая подпись.
        label = f"🚗 {v.brand}"
        if v.plate_number:
            label += f" · {v.plate_number}"
        # Telegram inline-кнопки рендерят максимум ~64 символа корректно.
        if len(label) > 60:
            label = label[:57] + "…"
        rows.append([
            InlineKeyboardButton(
                text=label, callback_data=f"mile:veh:{v.id}"
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text == BTN_UPDATE_MILEAGE, ~IsAdmin())
async def btn_update_mileage(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    if message.from_user is None:
        return
    profile = await UserRepository(session).get(message.from_user.id)
    if profile is None:
        # Незарегистрированный клиент — игнорируем.
        return

    # Phase 7: если в гараже > 1 машины, сначала спрашиваем какую обновляем.
    # Если 0 машин (теоретически — клиент без записи в vehicles) — fallback
    # на legacy-путь (UserRepository.update_last_known_mileage всё ещё
    # умеет работать через shim).
    vehicles = await VehicleRepository(session).list_for_user(
        message.from_user.id
    )
    if len(vehicles) > 1:
        await state.set_state(UpdateMileage.waiting_vehicle)
        await message.answer(
            "🚗 У вас в гараже несколько авто. <b>Для какой машины "
            "обновляем пробег?</b>",
            parse_mode="HTML",
            reply_markup=_vehicle_picker_kb(vehicles),
        )
        return

    # 0 или 1 машина — пропускаем шаг выбора. Сохраняем vehicle_id (если
    # есть) в FSMData, чтобы step_mileage знал куда писать.
    if vehicles:
        await state.update_data(vehicle_id=vehicles[0].id)
    await state.set_state(UpdateMileage.waiting_mileage)
    await message.answer(
        "Введите актуальный пробег авто (в км). Для отмены — /cancel."
    )


@router.callback_query(
    UpdateMileage.waiting_vehicle, F.data.startswith("mile:veh:")
)
async def pick_vehicle_for_mileage(
    cb: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Клиент выбрал машину из inline-клавиатуры — фиксируем выбор и
    переходим к вводу пробега."""
    if cb.data is None or cb.message is None or cb.from_user is None:
        await cb.answer()
        return
    try:
        vehicle_id = int(cb.data.split(":", 2)[2])
    except ValueError:
        await cb.answer("Некорректный выбор", show_alert=True)
        return

    # Защита: машина должна принадлежать тому же клиенту, иначе кто-то
    # подменил callback_data. Проверяем через repo.
    repo = VehicleRepository(session)
    vehicle = await repo.get(vehicle_id)
    if vehicle is None or vehicle.user_id != cb.from_user.id:
        await cb.answer("Машина не найдена", show_alert=True)
        return

    await state.update_data(vehicle_id=vehicle.id)
    await state.set_state(UpdateMileage.waiting_mileage)
    await cb.answer()
    label = f"{vehicle.brand}"
    if vehicle.plate_number:
        label += f" [{vehicle.plate_number}]"
    # Убираем inline-кнопки в сообщении-приглашении (чтобы не нажали ещё раз)
    # и просим ввести число отдельным сообщением.
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await cb.message.answer(
        f"🚗 Выбрана машина: <b>{label}</b>.\n\n"
        "Введите актуальный пробег (в км). Для отмены — /cancel.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# 📖 Моя сервисная книжка
# ---------------------------------------------------------------------------

SERVICE_BOOK_LIMIT = 10


def _format_service_book_entry(idx: int, row) -> str:
    """Один блок «электронной сервисной книжки» (без хедера)."""
    exec_str = _human_date(row["execution_date"])
    nv = row["next_visit_date"]
    nxt = row["next_service_mileage"]
    cur = row["current_mileage"]
    block = [
        f"<b>{idx}.</b> 🗓 <b>{exec_str}</b>",
        f"🛠 {row['service_type']}",
        f"🔢 Пробег при визите: <b>{cur}</b> км",
    ]
    if nv or nxt is not None:
        nv_str = _human_date(nv) if nv else "—"
        nxt_str = f"{nxt} км" if nxt is not None else "—"
        block.append(f"📅 След. визит: <b>{nv_str}</b> или <b>{nxt_str}</b>")
    return "\n".join(block)


def _row_photo_ids(row) -> list[str]:
    """Безопасно достать список ``photo_file_id`` для записи.

    Колонка ``photo_file_id`` хранит:
    * JSON-массив (новый формат, поддерживает несколько фото);
    * либо одиночный file_id строкой (старый формат, back-compat).
    Возвращает ``[]`` если фото нет / колонки нет.
    """
    try:
        keys = row.keys()
    except Exception:  # pragma: no cover - defensive
        keys = []
    if "photo_file_id" not in keys:
        return []
    raw = row["photo_file_id"]
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return [raw]
    if isinstance(decoded, list):
        return [str(x) for x in decoded if x]
    if isinstance(decoded, str):
        return [decoded]
    return []


async def _send_service_book_text(
    message: Message, session: AsyncSession
) -> None:
    """Текстово-фотографическая выдача истории — fallback, если у клиента
    нет Mini App (старая версия Telegram) или он намеренно вызвал
    ``/history``. Та же логика, что была в ``btn_service_book``.
    """
    if message.from_user is None:
        return
    profile = await UserRepository(session).get(message.from_user.id)
    if profile is None:
        # Незарегистрированный клиент — функция недоступна.
        return

    history = await RecordRepository(session).get_user_history(
        message.from_user.id
    )

    if not history:
        await message.answer(
            "У вас пока нет записей о посещении сервиса.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    shown = history[:SERVICE_BOOK_LIMIT]

    await message.answer(
        "<b>📖 Ваша сервисная книжка</b>",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )

    for idx, row in enumerate(shown, start=1):
        text = _format_service_book_entry(idx, row)
        photo_ids = _row_photo_ids(row)
        if not photo_ids:
            await message.answer(text, parse_mode="HTML")
            continue
        if len(photo_ids) == 1:
            await message.answer_photo(
                photo=photo_ids[0], caption=text, parse_mode="HTML"
            )
            continue
        # Несколько фото — альбом, caption на первом элементе.
        media = [
            InputMediaPhoto(
                media=fid,
                caption=text if i == 0 else None,
                parse_mode="HTML" if i == 0 else None,
            )
            for i, fid in enumerate(photo_ids[:10])  # лимит TG: 10 в media_group
        ]
        try:
            await message.answer_media_group(media)
        except TelegramBadRequest:
            # Fallback: первое фото с caption + остальные отдельно без подписи.
            await message.answer_photo(
                photo=photo_ids[0], caption=text, parse_mode="HTML"
            )
            for fid in photo_ids[1:10]:
                await message.answer_photo(photo=fid)

    if len(history) > SERVICE_BOOK_LIMIT:
        await message.answer(
            f"<i>Показаны последние {SERVICE_BOOK_LIMIT} записей.</i>",
            parse_mode="HTML",
        )


@router.message(F.text == BTN_SERVICE_BOOK, ~IsAdmin())
async def btn_service_book(message: Message, session: AsyncSession) -> None:
    """Backward-compat: на случай если у клиента ещё закеширована старая
    reply-клавиатура с кнопкой «📖 Моя сервисная книжка» (актуальная KB
    её больше не содержит). Отвечаем инлайн-кнопкой запуска Mini App.

    Если ``WEBAPP_URL`` не задан — падаем на текстовый fallback.
    """
    if message.from_user is None:
        return
    profile = await UserRepository(session).get(message.from_user.id)
    if profile is None:
        return
    kb = service_book_inline_kb()
    if kb is None:
        await _send_service_book_text(message, session)
        return
    await message.answer(
        "Нажмите кнопку ниже — откроется ваша сервисная книжка "
        "с историей ТО, виджетом пробега и виртуальным мастером 🤖.\n\n"
        "Если приложение не открывается — отправьте /history, "
        "пришлю историю в чате.",
        reply_markup=kb,
    )


@router.message(Command("history"))
async def cmd_history(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    """Fallback-команда: текстовая сервисная книжка с фото в чате.
    Доступна всегда, независимо от наличия WEBAPP_URL.
    """
    await state.clear()
    await _send_service_book_text(message, session)


@router.message(F.web_app_data)
async def cb_web_app_data(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    """Сообщение от Mini App через ``Telegram.WebApp.sendData``.

    Сейчас единственный поддерживаемый payload — ``request_call``:
    клиент нажал «🗓 Записаться на осмотр» внутри миниаппа.
    Полностью делегируем в ``btn_request_call``, чтобы логика и текст
    были 1-в-1 с reply-кнопкой «📞 Заказать звонок».
    """
    if message.from_user is None or message.web_app_data is None:
        return
    payload = (message.web_app_data.data or "").strip()
    if payload == "request_call":
        await state.clear()
        await btn_request_call(message, state, session)
        return
    # Незнакомый payload — лог, без действия.
    logger.warning("web_app_data unknown payload: %r", payload)


_DIGITS_RE = re.compile(r"\d+")


def _parse_mileage(text: str) -> int | None:
    """Извлечь все цифры из ввода. ``"125 000 км"`` / ``"125.000"`` → ``125000``.

    Если цифр нет вовсе — ``None``. Верхний санити-лимит 10_000_000 км
    (за пределами реального пробега любого авто, скорее всего опечатка).
    """
    digits = "".join(_DIGITS_RE.findall(text))
    if not digits:
        return None
    value = int(digits)
    if value > 10_000_000:
        return None
    return value


@router.message(
    UpdateMileage.waiting_mileage, F.text == "/cancel"
)
@router.message(
    UpdateMileage.waiting_vehicle, F.text == "/cancel"
)
async def cancel_mileage(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())


@router.message(UpdateMileage.waiting_mileage, F.text)
async def step_mileage(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    if message.from_user is None:
        return
    raw = (message.text or "").strip()
    value = _parse_mileage(raw)

    if value is None:
        # Клиент пытается ввести что-то вместо чисел — например, дату ТО,
        # «регламент 5000», «следующее ТО ...». Эти данные может вносить
        # ТОЛЬКО мастер, поэтому вежливо блокируем попытку.
        await message.answer(
            "Сюда можно ввести только число — ваш текущий пробег в км "
            "(например, <b>125000</b>).\n\n"
            "📅 Дату следующего ТО и регламентный пробег может вносить "
            "<b>только мастер сервиса</b>. Передайте ему ваш ID — "
            f"<code>{message.from_user.id}</code>.\n\n"
            "Если хотите отменить ввод — отправьте /cancel.",
            parse_mode="HTML",
        )
        return

    # Phase 7: если на этапе выбора машины (или btn_update_mileage)
    # был зафиксирован конкретный vehicle_id — пишем именно туда через
    # VehicleRepository (плюс синхронизация legacy users.last_known_mileage
    # при is_primary). Иначе — fallback на shim UserRepository, который
    # сам найдёт главную машину.
    fsm_data = await state.get_data()
    chosen_vehicle_id = fsm_data.get("vehicle_id")
    if chosen_vehicle_id is not None:
        vehicle_repo = VehicleRepository(session)
        await vehicle_repo.update_current_mileage(int(chosen_vehicle_id), value)
        # Если выбранная — primary, держим в синке legacy-поле, чтобы
        # старые потребители (notifications, raw-SQL отчёты) не отстали.
        chosen = await vehicle_repo.get(int(chosen_vehicle_id))
        if chosen is not None and chosen.is_primary:
            await UserRepository(session).update_last_known_mileage(
                message.from_user.id, value
            )
    else:
        await UserRepository(session).update_last_known_mileage(
            message.from_user.id, value
        )
    # Коммитим ORM-транзакцию ДО FSM-записи: иначе SQLite-write-lock,
    # удерживаемый этой транзакцией, конфликтует с aiosqlite-write
    # внутри SQLiteStorage.set_state, и мы упираемся в busy_timeout
    # (sqlite3.OperationalError: database is locked). DbSessionMiddleware
    # сделает финальный commit на выходе из хэндлера — это безопасно.
    await session.commit()
    await state.clear()

    # Все записи этого клиента, где назначен регламент по пробегу.
    # Сортировка из репозитория: по next_service_mileage ASC, потом id DESC.
    pending = await RecordRepository(session).get_pending_services(
        message.from_user.id
    )

    if not pending:
        await message.answer(
            f"Данные приняты. Ваш текущий пробег: <b>{value}</b> км. "
            "Рекомендаций по следующему ТО пока нет.",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    today = date.today()

    def _days_left(iso: str | None) -> int | None:
        if not iso:
            return None
        try:
            return (datetime.strptime(iso, "%Y-%m-%d").date() - today).days
        except ValueError:
            return None

    def _plural_day(n: int) -> str:
        n = abs(n)
        if 11 <= n % 100 <= 14:
            return "дней"
        last = n % 10
        if last == 1:
            return "день"
        if last in (2, 3, 4):
            return "дня"
        return "дней"

    def _date_with_countdown(iso: str | None) -> str | None:
        """``27.04.2026`` либо ``27.04.2026 (через 2 дня)``."""
        if not iso:
            return None
        days = _days_left(iso)
        human = _human_date(iso)
        if days is None or abs(days) > 14:
            return human
        if days == 0:
            return f"{human} (сегодня)"
        if days > 0:
            return f"{human} (через {days} {_plural_day(days)})"
        # days < 0 — срок уже истёк
        n = -days
        return f"{human} (срок истёк {n} {_plural_day(n)} назад)"

    # Каждой работе — свой статус. Группа по next_service_mileage
    # больше НЕ влияет на статус (раньше «срочила» всю группу).
    Item = tuple[int, int | None, object]  # rem, days, row
    overdue: list[Item] = []  # rem<0 или days<0
    urgent: list[Item] = []   # 0..1000км или 0..6 дней
    ok: list[Item] = []

    for row in pending:
        m = int(row["next_service_mileage"])
        rem = m - value
        days = _days_left(row["next_visit_date"])

        is_overdue_km = rem < 0
        is_overdue_date = days is not None and days < 0
        is_urgent_km = 0 <= rem <= 1000
        is_urgent_date = days is not None and 0 <= days < 14

        if is_overdue_km or is_overdue_date:
            overdue.append((rem, days, row))
        elif is_urgent_km or is_urgent_date:
            urgent.append((rem, days, row))
        else:
            ok.append((rem, days, row))

    intro = (
        "Данные приняты.\n"
        f"Ваш текущий пробег: <b>{value}</b> км."
    )

    def _urgent_line(rem: int, row) -> str:
        line = f"• <b>{row['service_type']}</b>: осталось <b>{rem}</b> км"
        date_str = _date_with_countdown(row["next_visit_date"])
        if date_str:
            line += f" или до <b>{date_str}</b>"
        return line

    def _planned_line(rem: int, row) -> str:
        # Используется для блока «🗓️ План обслуживания». Счётчик дней
        # тут не нужен (попавшие сюда даты — далеко, > 14 дней). А
        # _date_with_countdown сам не добавит суффикс при |days| > 14.
        line = f"• <b>{row['service_type']}</b>: осталось <b>{rem}</b> км"
        if row["next_visit_date"]:
            line += f" или до <b>{_human_date(row['next_visit_date'])}</b>"
        return line

    def _overdue_line(rem: int, days: int | None, row) -> str:
        warn_parts: list[str] = []
        if rem < 0:
            warn_parts.append(f"Срок превышен на <b>{-rem}</b> км")
        if days is not None and days < 0:
            n = -days
            warn_parts.append(f"срок истёк <b>{n}</b> {_plural_day(n)} назад")
        body = "; ".join(warn_parts) if warn_parts else "просрочено"
        return f"🔺 <b>{row['service_type']}</b>: ⚠️ {body}"

    blocks: list[str] = [intro]

    if overdue:
        blocks.append("")
        blocks.append("🚨 <b>ВНИМАНИЕ: СРОК ОБСЛУЖИВАНИЯ ПРЕВЫШЕН!</b>")
        for r, d, row in sorted(overdue, key=lambda t: t[0]):
            blocks.append(_overdue_line(r, d, row))

    if urgent:
        blocks.append("")
        urgent_title = (
            "⚠️ <b>Срочная работа:</b>"
            if len(urgent) == 1
            else "⚠️ <b>Срочные работы:</b>"
        )
        blocks.append(urgent_title)
        for r, _d, row in sorted(urgent, key=lambda t: t[0]):
            blocks.append(_urgent_line(r, row))

    # «🗓️ План обслуживания» — все остальные записи клиента, у которых
    # назначен регламент по пробегу, без фильтра по группам.
    if ok:
        blocks.append("")
        blocks.append("🗓️ <b>План обслуживания:</b>")
        for r, _d, row in sorted(ok, key=lambda t: t[0]):
            blocks.append(_planned_line(r, row))

    if not overdue and not urgent:
        blocks.append("")
        blocks.append("Спокойных поездок!")

    text = "\n".join(blocks)
    await message.answer(text, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())


# ---------------------------------------------------------------------------
# 📞 Заказать звонок (FSM RequestCall)
# ---------------------------------------------------------------------------

async def _notify_admins_about_request(
    bot,
    request_id: int,
    profile,
    phone: str,
    description: str | None,
    session: AsyncSession,
) -> None:
    """Разослать уведомление о новой заявке всем admin'ам."""
    text = _format_request_notification(
        full_name=profile.full_name,
        username=profile.username,
        phone=phone,
        car_brand=profile.car_brand,
        car_plate=profile.car_plate,
        description=description,
    )
    kb = admin_request_kb(request_id, int(profile.telegram_id))
    admin_ids = await AdminRepository(session).list_ids()
    for admin_id in admin_ids:
        try:
            await bot.send_message(
                admin_id, text, parse_mode="HTML", reply_markup=kb
            )
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.warning(
                "Не удалось уведомить admin %s о заявке %s: %s",
                admin_id, request_id, exc,
            )


async def _finalize_request(
    *,
    bot,
    chat_id: int,
    user_id: int,
    state: FSMContext,
    description: str | None,
    session: AsyncSession,
) -> None:
    """Сохранить заявку, уведомить админов, вернуть клиента в меню."""
    data = await state.get_data()
    phone = data.get("request_phone")
    if not phone:
        await state.clear()
        await bot.send_message(
            chat_id,
            "Что-то пошло не так с номером. Попробуйте ещё раз.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    users = UserRepository(session)
    profile = await users.get(user_id)
    if profile is None:
        await state.clear()
        return

    request_id = await users.create_service_request(
        telegram_id=user_id,
        phone=phone,
        description=description,
    )
    # Освобождаем SQLite-write-lock до FSM-записи (см. step_mileage).
    await session.commit()
    await _notify_admins_about_request(
        bot, request_id, profile, phone, description, session
    )

    await state.clear()
    await bot.send_message(
        chat_id,
        "✅ Заявка принята! Мастер перезвонит вам в ближайшее время.",
        reply_markup=ReplyKeyboardRemove(),
    )


REQUEST_CALL_PROMPT = (
    "Чтобы мастер мог вам перезвонить, поделитесь номером телефона "
    "кнопкой ниже. Для отмены — /cancel или «❌ Отмена»."
)


@router.message(F.text == BTN_REQUEST_CALL, ~IsAdmin())
async def btn_request_call(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    if message.from_user is None:
        return
    profile = await UserRepository(session).get(message.from_user.id)
    if profile is None:
        return
    await state.set_state(RequestCall.waiting_phone)
    await message.answer(
        REQUEST_CALL_PROMPT,
        reply_markup=request_phone_kb(),
    )


@router.callback_query(F.data == "notify:request_call")
async def cb_notify_request_call(
    cb: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Inline-кнопка «📞 Заказать звонок» из уведомления планировщика.

    Поведение строго совпадает с reply-кнопкой главного меню:
    проверяем профиль клиента, переводим в ``RequestCall.waiting_phone``
    и просим контакт. Старая inline-кнопка под уведомлением убирается,
    чтобы клиент не нажал её повторно.
    """
    if cb.message is None or cb.from_user is None:
        await cb.answer()
        return
    profile = await UserRepository(session).get(cb.from_user.id)
    if profile is None:
        await cb.answer("Доступ ограничен", show_alert=True)
        return
    # Если у клиента уже идёт другой FSM-диалог — на всякий случай
    # сбрасываем, чтобы /add_record и пр. не мешали.
    await state.clear()
    await state.set_state(RequestCall.waiting_phone)
    await cb.answer()
    # Снимаем inline-кнопку с исходного сообщения, чтобы не нажиматься повторно.
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await cb.message.answer(
        REQUEST_CALL_PROMPT,
        reply_markup=request_phone_kb(),
    )


@router.callback_query(F.data == "start:book_service")
async def cb_start_book_service(
    cb: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Phase 10: callback под inline-кнопкой «📅 Записаться на сервис»
    в корпоративном /start. Переиспользует существующий FSM
    ``RequestCall``: тот же промпт, та же клавиатура запроса контакта.

    Поведение 1-в-1 с ``cb_notify_request_call``, кроме одного: НЕ
    снимаем inline-клавиатуру под /start (там же ещё кнопка WebApp,
    которую никто не просил гасить).
    """
    if cb.message is None or cb.from_user is None:
        await cb.answer()
        return
    profile = await UserRepository(session).get(cb.from_user.id)
    if profile is None:
        await cb.answer("Доступ ограничен", show_alert=True)
        return
    await state.clear()
    await state.set_state(RequestCall.waiting_phone)
    await cb.answer()
    await cb.message.answer(
        REQUEST_CALL_PROMPT,
        reply_markup=request_phone_kb(),
    )


@router.message(RequestCall.waiting_phone, F.text.in_({"/cancel", BTN_CANCEL}))
@router.message(
    RequestCall.waiting_description, F.text.in_({"/cancel", BTN_CANCEL})
)
async def cancel_request_call(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())


@router.message(RequestCall.waiting_phone, F.contact)
async def request_call_contact(message: Message, state: FSMContext) -> None:
    if message.contact is None or message.from_user is None:
        return
    # Принимаем только контакт самого отправителя (не пересланный чужой).
    if (
        message.contact.user_id is not None
        and message.contact.user_id != message.from_user.id
    ):
        await message.answer(
            "Пожалуйста, поделитесь именно своим номером — нажмите кнопку "
            "«📱 Отправить номер телефона».",
            reply_markup=request_phone_kb(),
        )
        return
    phone = message.contact.phone_number
    await state.update_data(request_phone=phone)
    await state.set_state(RequestCall.waiting_description)
    await message.answer(
        "Кратко опишите проблему или пожелания к звонку:",
        reply_markup=ReplyKeyboardRemove(),
    )
    await message.answer(
        "Если хотите без описания — нажмите «➡️ Пропустить».",
        reply_markup=skip_description_kb(),
    )


@router.message(RequestCall.waiting_phone, F.text)
async def request_call_phone_text(message: Message, state: FSMContext) -> None:
    """Если клиент пытается ввести номер текстом — просим использовать кнопку."""
    await message.answer(
        "Пожалуйста, нажмите кнопку «📱 Отправить номер телефона» "
        "ниже — так мы получим ваш номер автоматически.",
        reply_markup=request_phone_kb(),
    )


@router.message(RequestCall.waiting_description, F.text)
async def request_call_description(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer(
            "Опишите проблему текстом или нажмите «➡️ Пропустить».",
            reply_markup=skip_description_kb(),
        )
        return
    if message.from_user is None or message.bot is None:
        return
    await _finalize_request(
        bot=message.bot,
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        state=state,
        description=text,
        session=session,
    )


@router.callback_query(RequestCall.waiting_description, F.data == "req:skip")
async def request_call_skip(
    cb: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    if cb.message is None or cb.bot is None:
        await cb.answer()
        return
    await cb.answer("Пропущено")
    # Убираем inline-кнопку «Пропустить» с предыдущего сообщения.
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await _finalize_request(
        bot=cb.bot,
        chat_id=cb.message.chat.id,
        user_id=cb.from_user.id,
        state=state,
        description=None,
        session=session,
    )


# ---------------------------------------------------------------------------
# Fallback-хэндлеры для нетекстовых апдейтов в клиентских FSM-state.
#
# ВАЖНО: регистрируются последними, чтобы не перехватывать события,
# которые должен обработать более специфичный хэндлер выше.
# Цель: если клиент шлёт стикер/voice/video/документ, когда от него
# ждут число / контакт / текст — бот не молчит, а вежливо подсказывает.
# ---------------------------------------------------------------------------

# Phase 20.1: Глобальный contact-handler ВНЕ FSM-состояний.
# Срабатывает, когда клиент поделился контактом в чате (через
# keyboard-button, которую бот отправил по запросу из мини-аппа:
# POST /api/client/request-contact). Обновляет ``users.phone`` и
# сразу отдаёт подтверждение — дальше клиент открывает мини-апп и
# видит «Профиль подтверждён» + активную запись на сервис.
# StateFilter(None) критичен: без него этот handler перехватит
# контакт, который должен попасть в RequestCall.waiting_phone.
@router.message(StateFilter(None), F.contact, ~IsAdmin())
async def confirm_contact_share(
    message: Message, session: AsyncSession
) -> None:
    if message.contact is None or message.from_user is None:
        return
    # Принимаем только контакт самого отправителя (не пересланный).
    if (
        message.contact.user_id is not None
        and message.contact.user_id != message.from_user.id
    ):
        await message.answer(
            "Пожалуйста, поделитесь именно своим номером — нажмите кнопку "
            "«📱 Отправить номер телефона» ниже.",
            reply_markup=share_contact_kb(),
        )
        return
    phone = (message.contact.phone_number or "").strip()
    if not phone:
        await message.answer(
            "Не удалось прочитать номер из контакта — попробуйте ещё раз.",
            reply_markup=share_contact_kb(),
        )
        return

    repo = UserRepository(session)
    affected = await repo.update_phone(message.from_user.id, phone)
    await session.commit()
    if affected == 0:
        # Контакт пришёл до /start — профиль ещё не создан. Не падаем,
        # просто мягко просим открыть бота «с нуля» — дальше штатный
        # onboarding подхватит номер из ``users.phone`` на повторном /start.
        logger.info(
            "phase20.1 contact share skipped: no profile for user_id=%s",
            message.from_user.id,
        )
        await message.answer(
            "Не нашли ваш профиль. Отправьте /start — мы создадим его и "
            "продолжим запись.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    logger.info(
        "phase20.1 phone confirmed via share-contact for user_id=%s",
        message.from_user.id,
    )
    await message.answer(
        "✅ Профиль подтверждён! Теперь вам доступна онлайн-запись.\n"
        "Вернитесь в мини-апп и выберите удобное окно заезда.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(UpdateMileage.waiting_mileage)
async def fallback_mileage_state(message: Message) -> None:
    await message.answer(
        "Сюда можно ввести только число — ваш текущий пробег в км "
        "(например, <b>125000</b>). Для отмены — /cancel.",
        parse_mode="HTML",
    )


@router.message(RequestCall.waiting_phone)
async def fallback_request_phone(message: Message) -> None:
    # Сюда залетит всё, что не F.contact и не /cancel.
    await message.answer(
        "Чтобы поделиться номером, нажмите кнопку «📱 Отправить номер телефона» "
        "ниже. Для отмены — /cancel или «❌ Отмена».",
        reply_markup=request_phone_kb(),
    )


@router.message(RequestCall.waiting_description)
async def fallback_request_description(message: Message) -> None:
    await message.answer(
        "Опишите проблему текстом или нажмите «➡️ Пропустить».",
        reply_markup=skip_description_kb(),
    )
