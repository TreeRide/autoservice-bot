"""
FSM-состояния aiogram.

:class:`AddClient` — добавление нового клиента (👤 Добавить клиента):
    1. ``waiting_id``     — Telegram ID.
    2. ``waiting_name``   — Имя клиента.
    3. ``waiting_brand``  — Марка/модель.
    4. ``waiting_plate``  — Гос. номер.
    5. ``waiting_review`` — карточка клиента до сохранения,
       с возможностью точечного редактирования полей.

:class:`AddRecord` — добавление записи о ТО (📝 Добавить запись ТО):
    1. ``waiting_search``         — ввод ID/части номера/имени или
       нажатие на кнопку «Недавние».
    2. ``waiting_pick``           — выбор клиента из inline-списка.
    3. ``waiting_service_type``   — выбор категории услуги.
    4. ``waiting_next_visit``     — дата следующего ТО (ДД.ММ.ГГГГ).
    5. ``waiting_confirmation``   — карточка подтверждения.

:class:`FindClient` — поиск клиента в админ-функции «🗂 База клиентов»:
    1. ``waiting_search`` — ввод имени или гос. номера.

:class:`EditClient` — точечное редактирование профиля выбранного клиента
   (вызов из карточки профиля):
    1. ``waiting_name``  — новое имя.
    2. ``waiting_brand`` — новая марка/модель.
    3. ``waiting_plate`` — новый гос. номер.

   Целевой ``telegram_id`` передаётся через ``state.update_data(target_id=...)``.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AddClient(StatesGroup):
    waiting_id = State()
    waiting_name = State()
    waiting_brand = State()
    waiting_plate = State()
    waiting_year = State()
    waiting_engine_volume = State()
    waiting_vin = State()
    waiting_fuel_type = State()
    waiting_review = State()


class AddRecord(StatesGroup):
    waiting_search = State()
    waiting_pick = State()
    # Phase 7: после выбора клиента, если у него > 1 машины, мастер
    # выбирает для какой именно делается запись. Если 1 — шаг
    # пропускается автоматически.
    waiting_vehicle = State()
    waiting_service_type = State()
    waiting_subcategory = State()
    waiting_other_text = State()
    waiting_current_mileage = State()
    waiting_execution_date = State()
    waiting_next_choice = State()
    waiting_next_visit = State()
    waiting_next_mileage = State()
    waiting_for_photo = State()
    waiting_confirmation = State()


class UpdateMileage(StatesGroup):
    """FSM клиента: обновление текущего пробега.

    1. ``waiting_vehicle`` — клиент выбирает, для какой машины обновляем
       (только если в гараже > 1 машины; иначе шаг пропускается).
    2. ``waiting_mileage`` — клиент вводит число.
    """

    waiting_vehicle = State()
    waiting_mileage = State()


class RequestCall(StatesGroup):
    """FSM клиента: «📞 Заказать звонок».

    1. ``waiting_phone``       — ждём контакт через ``request_contact=True``.
    2. ``waiting_description`` — короткое описание проблемы либо «Пропустить».
    """

    waiting_phone = State()
    waiting_description = State()


class FindClient(StatesGroup):
    waiting_search = State()


class EditClient(StatesGroup):
    waiting_name = State()
    waiting_brand = State()
    waiting_plate = State()
