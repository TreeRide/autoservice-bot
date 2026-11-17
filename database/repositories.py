"""
Репозитории для работы с данными через SQLAlchemy 2.0 (async).

Здесь — параллельная реализация всех текущих SQL-операций из :mod:`db`,
переписанная на ORM/Core. Логика и сигнатуры методов сохранены 1:1, чтобы
переход бота на этот слой (отдельная фаза) сводился к замене импортов.

Полнотекстовый поиск с поддержкой кириллицы (см.
:meth:`UserRepository.search_clients_by_text`) сознательно оставлен
на уровне Python (``str.casefold()``), как и в :mod:`db` — SQLite
``COLLATE NOCASE`` работает только с ASCII.

Каждый репозиторий принимает уже открытую :class:`AsyncSession` и
**не управляет транзакциями** — commit/rollback осуществляет вызывающий
код через :func:`database.session.get_session` или явно через сессию.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    AdminRow,
    AppSettingRow,
    HealthFeedbackRow,
    HealthReportRow,
    NotificationLogRow,
    ServiceRequestRow,
    UserHistoryRow,
    UserRow,
    VehicleRow,
)

logger = logging.getLogger(__name__)


# Поля профиля клиента, разрешённые к точечному обновлению через
# :meth:`UserRepository.update_field` (зеркало ``_EDITABLE_FIELDS`` в
# :mod:`db` — добавление сюда нового поля без аудита может дать SQL-
# инъекцию через имя колонки).
_EDITABLE_USER_FIELDS: frozenset[str] = frozenset(
    {"full_name", "car_brand", "car_plate", "phone"}
)


class BaseRepository:
    """Базовый класс репозитория.

    Хранит ссылку на :class:`AsyncSession`. Не реализует CRUD-примитивов
    в общем виде — конкретные репозитории сами выражают свои методы
    через select/insert/update/delete с явной семантикой.
    """

    __slots__ = ("session",)

    def __init__(self, session: AsyncSession) -> None:
        self.session = session


# ---------------------------------------------------------------------------
# UserRepository — клиенты + сервисные запросы
# ---------------------------------------------------------------------------

class UserRepository(BaseRepository):
    """Все операции над таблицами ``users`` / ``service_requests``."""

    # --- профиль клиента ----------------------------------------------------

    async def get(self, telegram_id: int) -> UserRow | None:
        return await self.session.get(UserRow, telegram_id)

    async def create(
        self,
        *,
        telegram_id: int,
        full_name: str,
        car_brand: str,
        car_plate: str,
        username: str | None = None,
        phone: str | None = None,
    ) -> UserRow:
        user = UserRow(
            telegram_id=telegram_id,
            full_name=full_name,
            username=username,
            car_brand=car_brand,
            car_plate=car_plate,
            phone=phone,
        )
        self.session.add(user)
        await self.session.flush()
        logger.info(
            "Создан профиль клиента %s (%s)%s",
            telegram_id, full_name,
            " [телефон задан]" if phone else "",
        )
        return user

    async def update_username(
        self, telegram_id: int, username: str | None
    ) -> None:
        await self.session.execute(
            update(UserRow)
            .where(UserRow.telegram_id == telegram_id)
            .values(username=username)
        )

    async def update_phone(
        self, telegram_id: int, phone: str
    ) -> int:
        """Phase 20.1: перезаписать ``users.phone`` после того, как
        клиент поделился контактом через Reply-keyboard в боте.

        Возвращает число изменённых строк: 0 — если пользователя нет
        в таблице (редкий кейс — контакт пришёл до /start).
        """
        result = await self.session.execute(
            update(UserRow)
            .where(UserRow.telegram_id == telegram_id)
            .values(phone=phone)
        )
        return result.rowcount or 0

    async def update_last_known_mileage(
        self, telegram_id: int, mileage: int
    ) -> int:
        """Обновить пробег. На expand-фазе пишем в ОБА места одновременно:

        * новая правда — ``vehicles.current_mileage`` главной машины
          клиента (используется AI-контекстом и API);
        * legacy ``users.last_known_mileage`` — пока ещё читается
          ``notifications.py`` raw-SQL'ом и админскими карточками.

        После contract-миграции (drop legacy-полей) останется только
        первая ветка.
        """
        # 1) Пишем в новую таблицу vehicles (главная машина).
        primary = await VehicleRepository(self.session).get_primary(telegram_id)
        if primary is not None:
            await VehicleRepository(self.session).update_current_mileage(
                primary.id, mileage
            )

        # 2) Дублируем в legacy-поле users.last_known_mileage.
        result = await self.session.execute(
            update(UserRow)
            .where(UserRow.telegram_id == telegram_id)
            .values(last_known_mileage=mileage)
        )
        affected = result.rowcount or 0
        logger.info(
            "Обновлён пробег клиента %s: %s км (rows=%s, vehicle_id=%s)",
            telegram_id, mileage, affected,
            primary.id if primary else None,
        )
        return affected

    async def update_field(
        self, telegram_id: int, field: str, value: str
    ) -> int:
        """Обновить поле в users. Для legacy car_brand/car_plate ДУБЛИРУЕМ
        изменение в главную машину (``vehicles.brand`` / ``plate_number``),
        чтобы новый источник правды (vehicles) шёл синхронно с legacy.
        """
        if field not in _EDITABLE_USER_FIELDS:
            raise ValueError(
                f"Поле {field!r} нельзя обновлять через update_field"
            )
        column = getattr(UserRow, field)
        result = await self.session.execute(
            update(UserRow)
            .where(UserRow.telegram_id == telegram_id)
            .values({column: value})
        )
        affected = result.rowcount or 0
        logger.info(
            "Обновлено %s=%r у клиента %s (rows=%s)",
            field, value, telegram_id, affected,
        )

        # Зеркалим в vehicles (главную машину). Если её нет — создаём.
        if field in ("car_brand", "car_plate") and affected:
            vehicle_field = (
                "brand" if field == "car_brand" else "plate_number"
            )
            vehicles = VehicleRepository(self.session)
            primary = await vehicles.get_primary(telegram_id)
            if primary is not None:
                await vehicles.update_field(primary.id, vehicle_field, value)
            else:
                # У клиента ещё нет машин в новой таблице — создаём
                # первую запись с тем, что знаем (бэкфилл-edge).
                user = await self.get(telegram_id)
                if user is not None:
                    await vehicles.add(
                        user_id=telegram_id,
                        brand=user.car_brand if vehicle_field != "brand" else value,
                        plate_number=(
                            user.car_plate
                            if vehicle_field != "plate_number"
                            else value
                        ),
                        current_mileage=user.last_known_mileage,
                        is_primary=True,
                    )
        return affected

    async def delete_client(self, telegram_id: int) -> int:
        """Удалить клиента и все связанные записи в одной транзакции.

        Phase 33 · полный каскад. Порядок чисток (FK не объявлены везде,
        поэтому делаем явные DELETE'ы — не полагаемся только на
        DB-уровневые ON DELETE CASCADE):

        1. ``notifications_log`` (FK не объявлен — лог по записям ТО);
        2. ``users_history`` (FK ``user_id`` → ``users.telegram_id``,
           без ON DELETE CASCADE → требуется явный DELETE);
        3. ``service_requests`` (FK ``telegram_id`` → ``users.telegram_id``,
           без ON DELETE CASCADE → требуется явный DELETE);
        4. ``health_feedback`` для машин клиента (FK имеет
           ON DELETE CASCADE на vehicles, но дублируем — explicit safer);
        5. ``health_reports`` для машин клиента (то же);
        6. ``vehicles`` (FK ``user_id`` → ``users.telegram_id`` имеет
           ON DELETE CASCADE, но удаляем явно для предсказуемости);
        7. ``users``.

        При ``PRAGMA foreign_keys=ON`` пропуск шага 3 / 2 даёт
        ``IntegrityError: FOREIGN KEY constraint failed``.
        """
        # 1) журнал уведомлений по записям клиента
        await self.session.execute(
            delete(NotificationLogRow).where(
                NotificationLogRow.record_id.in_(
                    select(UserHistoryRow.id).where(
                        UserHistoryRow.user_id == telegram_id
                    )
                )
            )
        )
        # 2) история ТО
        await self.session.execute(
            delete(UserHistoryRow).where(UserHistoryRow.user_id == telegram_id)
        )
        # 3) заявки на звонок
        await self.session.execute(
            delete(ServiceRequestRow).where(
                ServiceRequestRow.telegram_id == telegram_id
            )
        )
        # 4) AI health-feedback для всех машин клиента
        vehicle_ids_subq = select(VehicleRow.id).where(
            VehicleRow.user_id == telegram_id
        )
        await self.session.execute(
            delete(HealthFeedbackRow).where(
                HealthFeedbackRow.vehicle_id.in_(vehicle_ids_subq)
            )
        )
        # 5) AI health-reports для всех машин клиента
        await self.session.execute(
            delete(HealthReportRow).where(
                HealthReportRow.vehicle_id.in_(vehicle_ids_subq)
            )
        )
        # 6) машины клиента
        await self.session.execute(
            delete(VehicleRow).where(VehicleRow.user_id == telegram_id)
        )
        # 7) сам клиент
        result = await self.session.execute(
            delete(UserRow).where(UserRow.telegram_id == telegram_id)
        )
        affected = result.rowcount or 0
        logger.info("Удалён клиент %s (rows=%s)", telegram_id, affected)
        return affected

    # --- поиск -------------------------------------------------------------

    async def search_by_id(self, telegram_id: int) -> list[UserRow]:
        """Точное совпадение по ``telegram_id`` (вернёт 0 или 1 элемент)."""
        result = await self.session.execute(
            select(UserRow).where(UserRow.telegram_id == telegram_id)
        )
        return list(result.scalars().all())

    async def search_by_text(
        self, query: str, *, limit: int = 20
    ) -> list[UserRow]:
        """Поиск по подстроке в ``full_name`` / ``car_plate``.

        Регистронезависимый, корректно работает с кириллицей
        (``str.casefold()``). Полнотекстовый запрос делается в Python
        над всеми пользователями — точно как в легаси-:func:`db.search_clients_by_text`.
        """
        needle = query.casefold().strip()
        if not needle:
            return []
        result = await self.session.execute(select(UserRow))
        rows = list(result.scalars().all())
        matches = [
            row
            for row in rows
            if needle in (row.full_name or "").casefold()
            or needle in (row.car_plate or "").casefold()
        ]
        matches.sort(key=lambda r: (r.full_name or "").casefold())
        return matches[:limit]

    async def get_recent(self, limit: int = 3) -> list[UserRow]:
        """Последние N клиентов, которым добавляли запись о ТО.

        Сортировка по ``MAX(users_history.id)`` — порядок добавления.
        Возвращает только клиентов с непустой историей.
        """
        # Нативный SQL, потому что в `func.max` группировка через
        # JOIN/GROUP BY на ORM выходит более громоздко, чем читаемый
        # raw-запрос (логика 1:1 с db.get_recent_clients).
        sql = text(
            """
            SELECT u.telegram_id, u.full_name, u.username, u.car_brand,
                   u.car_plate, u.last_known_mileage,
                   MAX(h.id) AS last_record_id
            FROM users u
            JOIN users_history h ON h.user_id = u.telegram_id
            GROUP BY u.telegram_id
            ORDER BY last_record_id DESC
            LIMIT :limit
            """
        )
        result = await self.session.execute(sql, {"limit": limit})
        # Маппим обратно на ORM-ы по PK, чтобы сохранить однородный API.
        ids = [int(row.telegram_id) for row in result]
        if not ids:
            return []
        users_q = await self.session.execute(
            select(UserRow).where(UserRow.telegram_id.in_(ids))
        )
        by_id = {u.telegram_id: u for u in users_q.scalars().all()}
        return [by_id[i] for i in ids if i in by_id]

    # --- service_requests --------------------------------------------------

    async def create_service_request(
        self,
        *,
        telegram_id: int,
        phone: str,
        description: str | None,
        dropoff_window: str | None = None,
        vehicle_id: int | None = None,
    ) -> ServiceRequestRow:
        # Phase 20: начальный статус — pending_dropoff. dropoff_window
        # приходит уже отвалидированным Literal'ом на уровне Pydantic.
        # Phase 22: vehicle_id — опциональная привязка заявки к машине
        # клиента. Принадлежность авто клиенту валидирует вызывающий
        # (web_app/main.py делает это до вызова — защита от IDOR).
        req = ServiceRequestRow(
            telegram_id=telegram_id,
            phone=phone,
            description=description,
            status="pending_dropoff",
            dropoff_window=dropoff_window,
            vehicle_id=vehicle_id,
        )
        self.session.add(req)
        await self.session.flush()
        logger.info(
            "Создана заявка id=%s от %s (phone=%s, window=%r, vehicle_id=%s)",
            req.id, telegram_id, phone, dropoff_window, vehicle_id,
        )
        return req

    async def list_new_service_requests(self) -> list[ServiceRequestRow]:
        # Phase 20: после миграции 'new' -> 'pending_dropoff'.
        result = await self.session.execute(
            select(ServiceRequestRow)
            .where(ServiceRequestRow.status == "pending_dropoff")
            .order_by(ServiceRequestRow.id.desc())
        )
        return list(result.scalars().all())

    async def set_service_request_status(
        self, request_id: int, status: str
    ) -> int:
        """Phase 20: смена статуса заявки (yard-management flow).

        Вызывается PATCH /api/admin/requests/{id}/status. Значение
        ``status`` уже отвалидировано на уровне HTTP (Literal из
        _REQUEST_STATUSES), так что сюда доходит строго один из 5.

        Phase 23: любой non-pending статус атомарно помечает заявку
        ``resolved=True`` — она уходит из триаж-воронки (Новые /
        Просмотрено / Подтверждено). Обратный путь (``pending_dropoff``)
        сбрасывает resolved в False — на случай «ошиблись, вернуть».

        Phase 23.1: переход **в** ``in_yard`` однократно стаёмпит
        ``in_yard_at = now_local_sql()`` — момент фактической приёмки
        машины. Используем подзапрос ``COALESCE(in_yard_at, :ts)``,
        чтобы повторные переходы (напр. откатили → снова in_yard) не
        перезаписывали исторический штамп. Для ``in_progress | ready |
        completed`` колонку не трогаем; для возврата в pending —
        тоже не трогаем (лог не теряем, просто заявка снова в воронке).
        """
        from config import now_local_sql

        values: dict[str, object] = {"status": status}
        if status != "pending_dropoff":
            values["resolved"] = True
        else:
            values["resolved"] = False
        if status == "in_yard":
            # COALESCE — пишем only-if-null; повторные in_yard-переходы
            # сохраняют первый ts. ``ServiceRequestRow.in_yard_at``
            # слева как источник значения для COALESCE.
            values["in_yard_at"] = func.coalesce(
                ServiceRequestRow.in_yard_at, now_local_sql()
            )
        result = await self.session.execute(
            update(ServiceRequestRow)
            .where(ServiceRequestRow.id == request_id)
            .values(**values)
        )
        affected = result.rowcount or 0
        logger.info(
            "Заявка id=%s → status=%s resolved=%s (rows=%s)",
            request_id, status, values["resolved"], affected,
        )
        return affected

    # --- Phase 23 · триаж-флаги ------------------------------------------

    async def mark_request_viewed(self, request_id: int) -> int:
        """Поставить ``is_viewed=True`` (идемпотентно).

        Вызывается при открытии детали-модалки мастером. Если флаг
        уже стоял — rowcount всё равно будет 1 (SQLite UPDATE считает
        попавшие в WHERE строки), но семантически это no-op.
        """
        result = await self.session.execute(
            update(ServiceRequestRow)
            .where(ServiceRequestRow.id == request_id)
            .values(is_viewed=True)
        )
        affected = result.rowcount or 0
        logger.info(
            "Заявка id=%s → is_viewed=True (rows=%s)", request_id, affected,
        )
        return affected

    async def mark_request_confirmed(self, request_id: int) -> int:
        """Поставить ``is_confirmed=True`` (и заодно is_viewed=True —
        подтверждение подразумевает, что карточку уже открывали).

        Phase 23 семантика кнопки «✅ Подтвердить» (раньше «Обработано»).
        Статус заявки НЕ трогаем — она остаётся в ``pending_dropoff``
        до физической постановки на стоянку.
        """
        result = await self.session.execute(
            update(ServiceRequestRow)
            .where(ServiceRequestRow.id == request_id)
            .values(is_viewed=True, is_confirmed=True)
        )
        affected = result.rowcount or 0
        logger.info(
            "Заявка id=%s → is_confirmed=True (rows=%s)", request_id, affected,
        )
        return affected

    async def get_service_request(
        self, request_id: int
    ) -> ServiceRequestRow | None:
        return await self.session.get(ServiceRequestRow, request_id)

    async def set_master_comment(
        self, request_id: int, master_comment: str | None
    ) -> int:
        """Phase 31: сохранить внутренний комментарий мастера.

        Пустая строка / None нормализуются в ``NULL`` — UI не должен
        различать «не трогали» и «явно очистили». Все остальные значения
        пишутся как есть (frontend обрезает по длине, бэк делает второй
        guard на стороне Pydantic).
        """
        normalized = (master_comment or "").strip() or None
        result = await self.session.execute(
            update(ServiceRequestRow)
            .where(ServiceRequestRow.id == request_id)
            .values(master_comment=normalized)
        )
        affected = result.rowcount or 0
        logger.info(
            "Заявка id=%s → master_comment len=%s (rows=%s)",
            request_id, len(normalized) if normalized else 0, affected,
        )
        return affected

    async def close_service_request(self, request_id: int) -> int:
        # Phase 20: 'closed' -> 'completed'. Legacy bot-chat-flow
        # (handlers/admin/requests.py) дёргает этот метод при клике
        # на inline-кнопку «Закрыть»; после фазы 20 семантически это
        # означает «выдано клиенту, убрать с доски».
        result = await self.session.execute(
            update(ServiceRequestRow)
            .where(ServiceRequestRow.id == request_id)
            .values(status="completed")
        )
        affected = result.rowcount or 0
        logger.info("Заявка id=%s закрыта (rows=%s)", request_id, affected)
        return affected

    async def resolve_service_request(self, request_id: int) -> int:
        """Phase 14 (+ Phase 20): пометить заявку закрытой.

        До Phase 20 ставил ``'resolved'`` (Phase 14 CRM-канал) в
        отличие от ``'closed'`` (legacy bot-chat-flow). После
        Phase 20 мы свели весь лайфцикл к одному enum'у из 5
        статусов — отдельный ``'resolved'`` больше не нужен,
        всё «выдано клиенту / закрыто» = ``'completed'``.
        """
        result = await self.session.execute(
            update(ServiceRequestRow)
            .where(ServiceRequestRow.id == request_id)
            .values(status="completed")
        )
        affected = result.rowcount or 0
        logger.info(
            "Заявка id=%s resolved через CRM (rows=%s)", request_id, affected
        )
        return affected


# ---------------------------------------------------------------------------
# RecordRepository — users_history + notifications_log
# ---------------------------------------------------------------------------

class RecordRepository(BaseRepository):
    """Все операции над таблицей ``users_history`` и журналом уведомлений."""

    async def add(
        self,
        *,
        user_id: int,
        service_type: str,
        execution_date: str,
        next_visit_date: str | None,
        current_mileage: int | None = None,
        next_service_mileage: int | None = None,
        photo_file_id: str | None = None,
        vehicle_id: int | None = None,
        image_path: str | None = None,
    ) -> int:
        record = UserHistoryRow(
            user_id=user_id,
            vehicle_id=vehicle_id,
            service_type=service_type,
            execution_date=execution_date,
            next_visit_date=next_visit_date,
            current_mileage=current_mileage,
            next_service_mileage=next_service_mileage,
            photo_file_id=photo_file_id,
            image_path=image_path,
        )
        self.session.add(record)
        await self.session.flush()
        logger.info(
            "Добавлена запись id=%s для клиента %s vehicle_id=%s: %s (%s -> %s, %s/%s км, photo=%s, img=%s)",
            record.id, user_id, vehicle_id, service_type, execution_date,
            next_visit_date, current_mileage, next_service_mileage,
            bool(photo_file_id), bool(image_path),
        )
        return record.id

    async def delete(self, record_id: int) -> int:
        result = await self.session.execute(
            delete(UserHistoryRow).where(UserHistoryRow.id == record_id)
        )
        affected = result.rowcount or 0
        logger.info("Удалена запись id=%s (rows=%s)", record_id, affected)
        return affected

    async def get_user_history(
        self, user_id: int, vehicle_id: int | None = None
    ) -> list[UserHistoryRow]:
        """История клиента, опционально отфильтрованная по машине.

        :param vehicle_id: если задан — возвращаем только записи с
            ``users_history.vehicle_id == vehicle_id``. Если ``None`` —
            все записи клиента (включая legacy с ``vehicle_id=NULL``).
        """
        stmt = (
            select(UserHistoryRow)
            .where(UserHistoryRow.user_id == user_id)
            .order_by(
                UserHistoryRow.execution_date.desc(),
                UserHistoryRow.id.desc(),
            )
        )
        if vehicle_id is not None:
            stmt = stmt.where(UserHistoryRow.vehicle_id == vehicle_id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_pending_services(self, user_id: int) -> list[UserHistoryRow]:
        """История с заданным регламентом по пробегу — ближайшие первыми."""
        result = await self.session.execute(
            select(UserHistoryRow)
            .where(
                UserHistoryRow.user_id == user_id,
                UserHistoryRow.next_service_mileage.is_not(None),
            )
            .order_by(
                UserHistoryRow.next_service_mileage.asc(),
                UserHistoryRow.id.desc(),
            )
        )
        return list(result.scalars().all())

    async def get_latest_for_vehicle(
        self, vehicle_id: int
    ) -> UserHistoryRow | None:
        """Самая свежая запись ТО для конкретной машины (или None).

        Используется WebApp-эндпоинтом `/api/vehicles` для рендера
        прогресс-бара на каждой карточке гаража без N доп. запросов
        от фронта.
        """
        result = await self.session.execute(
            select(UserHistoryRow)
            .where(UserHistoryRow.vehicle_id == vehicle_id)
            .order_by(
                UserHistoryRow.execution_date.desc(),
                UserHistoryRow.id.desc(),
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_latest(self, user_id: int) -> UserHistoryRow | None:
        result = await self.session.execute(
            select(UserHistoryRow)
            .where(UserHistoryRow.user_id == user_id)
            .order_by(UserHistoryRow.id.desc())
            .limit(1)
        )
        return result.scalars().first()

    async def get_last_with_profile(self, limit: int = 5) -> list[dict]:
        """Последние N записей по всем клиентам со склейкой с профилем.

        Возвращает список ``dict`` (как ``aiosqlite.Row`` в легаси), чтобы
        вызывающий код мог обращаться по ключам без знания ORM-объектов.
        """
        result = await self.session.execute(
            select(
                UserHistoryRow.id,
                UserHistoryRow.user_id,
                UserHistoryRow.service_type,
                UserHistoryRow.execution_date,
                UserHistoryRow.next_visit_date,
                UserHistoryRow.current_mileage,
                UserHistoryRow.next_service_mileage,
                UserRow.full_name,
                UserRow.car_brand,
                UserRow.car_plate,
            )
            .outerjoin(UserRow, UserRow.telegram_id == UserHistoryRow.user_id)
            .order_by(UserHistoryRow.id.desc())
            .limit(limit)
        )
        return [dict(row._mapping) for row in result]

    async def get_pending_records_for_notification(self) -> list[dict]:
        """Активные работы по всем клиентам — для ежедневной рассылки.

        Логика повторяет :func:`db.get_pending_records_for_notification`:
        самая свежая запись на ``(user_id, service_type)`` среди тех,
        у кого задан хотя бы один регламент.
        """
        sql = text(
            """
            SELECT u.telegram_id, u.full_name, u.car_brand, u.car_plate,
                   u.last_known_mileage,
                   h.id AS record_id, h.service_type, h.execution_date,
                   h.next_visit_date, h.current_mileage, h.next_service_mileage
            FROM users u
            JOIN (
                SELECT id, user_id, service_type, execution_date,
                       next_visit_date, current_mileage, next_service_mileage,
                       ROW_NUMBER() OVER (
                           PARTITION BY user_id, service_type
                           ORDER BY execution_date DESC, id DESC
                       ) AS rn
                FROM users_history
                WHERE next_visit_date IS NOT NULL
                   OR next_service_mileage IS NOT NULL
            ) h ON h.user_id = u.telegram_id AND h.rn = 1
            ORDER BY u.telegram_id, h.service_type
            """
        )
        result = await self.session.execute(sql)
        return [dict(row._mapping) for row in result]

    # --- notifications_log -------------------------------------------------

    async def get_last_notification_sent(
        self, record_id: int, severity: str
    ) -> str | None:
        result = await self.session.execute(
            select(NotificationLogRow.last_sent_at).where(
                NotificationLogRow.record_id == record_id,
                NotificationLogRow.severity == severity,
            )
        )
        row = result.first()
        return row[0] if row else None

    async def upsert_notification_log(
        self, record_id: int, severity: str
    ) -> None:
        """``INSERT … ON CONFLICT DO UPDATE`` на (record_id, severity).

        Phase 22.1: last_sent_at пишется локальной минской меткой, а не
        SQLite-шным ``CURRENT_TIMESTAMP`` (который отдаёт UTC).
        """
        from config import now_local_sql

        ts = now_local_sql()
        stmt = sqlite_insert(NotificationLogRow).values(
            record_id=record_id,
            severity=severity,
            last_sent_at=ts,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["record_id", "severity"],
            set_={"last_sent_at": ts},
        )
        await self.session.execute(stmt)


# ---------------------------------------------------------------------------
# VehicleRepository — виртуальный гараж (таблица vehicles)
# ---------------------------------------------------------------------------

# Белый список полей, разрешённых к точечному обновлению через update_field
# (зеркальный паттерн _EDITABLE_USER_FIELDS — защита от SQL-инъекции по имени колонки).
FUEL_TYPES: tuple[str, ...] = ("Бензин", "Дизель", "Гибрид", "Электро")

_EDITABLE_VEHICLE_FIELDS: frozenset[str] = frozenset(
    {
        "brand",
        "model",
        "year",
        "vin",
        "plate_number",
        "avg_yearly_mileage",
        "engine_volume",
        "fuel_type",
        "transmission",
    }
)


class VehicleRepository(BaseRepository):
    """Все операции над таблицей ``vehicles``."""

    async def add(
        self,
        *,
        user_id: int,
        brand: str,
        plate_number: str,
        model: str | None = None,
        year: int | None = None,
        vin: str | None = None,
        current_mileage: int | None = None,
        avg_yearly_mileage: int | None = None,
        engine_volume: float | None = None,
        fuel_type: str | None = None,
        transmission: str | None = None,
        is_primary: bool = False,
    ) -> VehicleRow:
        """Добавить машину. Не трогает флаг ``is_primary`` у других
        авто того же юзера — вызывающий код должен сам следить за инвариантом
        «единственная основная» (см. :meth:`set_primary`).

        Если передан ``current_mileage`` — пишем ``mileage_updated_at`` =
        CURRENT_TIMESTAMP, чтобы фронт мог сразу применять формулу
        «живого пробега».

        Phase 22.1: метка пишется в локальном минском времени через
        :func:`config.now_local_sql` — фронт интерпретирует как aware
        datetime при сериализации. Раньше было ``datetime.now(timezone.utc)``.
        """
        from config import now_local_sql

        mileage_ts = now_local_sql() if current_mileage is not None else None
        vehicle = VehicleRow(
            user_id=user_id,
            brand=brand,
            model=model,
            year=year,
            vin=vin,
            plate_number=plate_number,
            current_mileage=current_mileage,
            avg_yearly_mileage=avg_yearly_mileage,
            engine_volume=engine_volume,
            fuel_type=fuel_type,
            transmission=transmission,
            mileage_updated_at=mileage_ts,
            is_primary=is_primary,
        )
        self.session.add(vehicle)
        await self.session.flush()
        logger.info(
            "Добавлено авто id=%s для усера %s: %s [%s] (primary=%s, year=%s, vol=%s, fuel=%r, trans=%r, vin=%r)",
            vehicle.id, user_id, brand, plate_number, is_primary, year,
            engine_volume, fuel_type, transmission, vin,
        )
        return vehicle

    async def get(self, vehicle_id: int) -> VehicleRow | None:
        return await self.session.get(VehicleRow, vehicle_id)

    async def list_for_user(self, user_id: int) -> list[VehicleRow]:
        """Все машины юзера. Сортировка: is_primary вперёд, дальше по id."""
        result = await self.session.execute(
            select(VehicleRow)
            .where(VehicleRow.user_id == user_id)
            .order_by(VehicleRow.is_primary.desc(), VehicleRow.id.asc())
        )
        return list(result.scalars().all())

    async def get_primary(self, user_id: int) -> VehicleRow | None:
        """Главная машина юзера (is_primary=True). Если по историческим
        причинам все флаги выключены — отдаём первую по id (defensive
        fallback). Если у юзера нет машин — None.
        """
        result = await self.session.execute(
            select(VehicleRow)
            .where(VehicleRow.user_id == user_id)
            .order_by(VehicleRow.is_primary.desc(), VehicleRow.id.asc())
            .limit(1)
        )
        return result.scalars().first()

    async def set_primary(self, user_id: int, vehicle_id: int) -> int:
        """Снять флаг is_primary со всех машин юзера и поставить его на указанную."""
        await self.session.execute(
            update(VehicleRow)
            .where(VehicleRow.user_id == user_id)
            .values(is_primary=False)
        )
        result = await self.session.execute(
            update(VehicleRow)
            .where(
                VehicleRow.id == vehicle_id,
                VehicleRow.user_id == user_id,
            )
            .values(is_primary=True)
        )
        return result.rowcount or 0

    async def update_field(
        self, vehicle_id: int, field: str, value: object
    ) -> int:
        """Точечное обновление одного поля. Имя поля валидируется по белому списку."""
        if field not in _EDITABLE_VEHICLE_FIELDS:
            raise ValueError(
                f"Поле {field!r} нельзя обновлять через update_field"
            )
        column = getattr(VehicleRow, field)
        result = await self.session.execute(
            update(VehicleRow)
            .where(VehicleRow.id == vehicle_id)
            .values({column: value})
        )
        affected = result.rowcount or 0
        logger.info(
            "vehicle %s: %s=%r (rows=%s)", vehicle_id, field, value, affected,
        )
        return affected

    async def update_current_mileage(
        self, vehicle_id: int, mileage: int
    ) -> int:
        """Перезаписать ``current_mileage`` — это НОВАЯ отправная точка.

        Бизнес-правило: если клиент ввёл реальный пробег вручную, это
        значение перезаписывает ``current_mileage`` и становится новой
        отправной точкой для всех дальнейших расчётов (avg_yearly_mileage,
        прогноз следующего ТО). Дельт и истории пробега не ведём.

        Параллельно пишем ``mileage_updated_at`` = текущая локальная
        минская метка (Phase 22.1) — фронт использует её для расчёта
        «живого пробега» между визитами.
        """
        from config import now_local_sql

        now_ts = now_local_sql()
        result = await self.session.execute(
            update(VehicleRow)
            .where(VehicleRow.id == vehicle_id)
            .values(current_mileage=mileage, mileage_updated_at=now_ts)
        )
        affected = result.rowcount or 0
        logger.info(
            "vehicle %s: current_mileage=%s @ %s (rows=%s) [новая базовая точка]",
            vehicle_id, mileage, now_ts, affected,
        )
        return affected

    async def delete(self, vehicle_id: int) -> int:
        result = await self.session.execute(
            delete(VehicleRow).where(VehicleRow.id == vehicle_id)
        )
        affected = result.rowcount or 0
        logger.info("Удалено авто id=%s (rows=%s)", vehicle_id, affected)
        return affected


# ---------------------------------------------------------------------------
# AdminRepository — таблица admins + TTL-кэш (5 минут)
# ---------------------------------------------------------------------------

# In-memory TTL-кэш списка админов.
# Структура: (set[int], monotonic_timestamp).
_ADMIN_CACHE_TTL_SECONDS = 300.0
_admin_cache: tuple[set[int], float] | None = None


def invalidate_admin_cache() -> None:
    """Сбросить кэш списка админов (вызывается при add/remove)."""
    global _admin_cache
    _admin_cache = None


class AdminRepository(BaseRepository):
    """Список администраторов СТО + TTL-кэш на 5 минут."""

    async def list_ids(self, *, force_refresh: bool = False) -> set[int]:
        """Множество Telegram ID администраторов.

        Один SELECT в БД на 5 минут на процесс. Кэш мгновенно сбрасывается
        при ``add`` / ``remove`` через :func:`invalidate_admin_cache`.
        """
        global _admin_cache
        now = time.monotonic()
        if not force_refresh and _admin_cache is not None:
            cached_ids, cached_at = _admin_cache
            if now - cached_at < _ADMIN_CACHE_TTL_SECONDS:
                return cached_ids
        result = await self.session.execute(select(AdminRow.telegram_id))
        ids = {int(tid) for (tid,) in result.all()}
        _admin_cache = (ids, now)
        return ids

    async def is_admin(self, telegram_id: int) -> bool:
        return telegram_id in await self.list_ids()

    async def add(self, telegram_id: int) -> bool:
        """Добавить ID. Возвращает ``True`` если ID не было в таблице."""
        existing = await self.session.get(AdminRow, telegram_id)
        if existing is not None:
            return False
        self.session.add(AdminRow(telegram_id=telegram_id))
        await self.session.flush()
        invalidate_admin_cache()
        logger.info("Добавлен админ telegram_id=%s", telegram_id)
        return True

    async def remove(self, telegram_id: int) -> bool:
        """Удалить ID. Возвращает ``True`` если строка была удалена."""
        result = await self.session.execute(
            delete(AdminRow).where(AdminRow.telegram_id == telegram_id)
        )
        affected = (result.rowcount or 0) > 0
        if affected:
            invalidate_admin_cache()
            logger.info("Удалён админ telegram_id=%s", telegram_id)
        return affected

    async def seed_if_empty(self, ids: Iterable[int]) -> int:
        """Засеять таблицу указанными ID, если она пуста.

        Возвращает количество добавленных строк (0 если уже были админы).
        Зеркало ``db._seed_admins_if_empty``.
        """
        ids_list: Sequence[int] = [int(x) for x in ids]
        if not ids_list:
            return 0
        result = await self.session.execute(select(AdminRow.telegram_id).limit(1))
        if result.first() is not None:
            return 0
        for tid in ids_list:
            self.session.add(AdminRow(telegram_id=tid))
        await self.session.flush()
        invalidate_admin_cache()
        logger.info("Засеяны %s админов: %s", len(ids_list), ids_list)
        return len(ids_list)


# ---------------------------------------------------------------------------
# AI Health Score (Phase 9): отчёты Gemini + клиентский фидбек.
# ---------------------------------------------------------------------------

# Допустимые статусы для health_feedback. Разделение чисто
# семантическое: "resolved" — клиент починил/заменил, "hidden" —
# просто скрыл. Для AI-промпта они одинаково означают "не упоминать".
HEALTH_FEEDBACK_STATUSES: tuple[str, ...] = ("resolved", "hidden")

# TTL отчёта по умолчанию. Свежее этого срока — отдаём из кэша,
# старее — пересоздаём через Gemini.
HEALTH_REPORT_TTL_DAYS: int = 7


class HealthRepository(BaseRepository):
    """Кэш AI-Health-отчётов и клиентского фидбека по рискам.

    Контракт по выбору отчёта:
    - :meth:`get_latest_report` отдаёт самый свежий отчёт ИЛИ ``None``
      (если такого нет / он старше ``max_age_days``).
    - :meth:`save_report` ВСЕГДА создаёт новую строку (историю не
      затираем — может пригодиться для аналитики). За очистку старых
      отчётов отвечает фонoвая задача (на этой фазе не делаем).
    - :meth:`invalidate_for_vehicle` физически удаляет ВСЕ отчёты
      машины. Вызывается из POST /health/feedback, чтобы следующий GET
      пересоздал отчёт без скрытого риска.
    """

    async def get_latest_report(
        self,
        vehicle_id: int,
        *,
        max_age_days: int = HEALTH_REPORT_TTL_DAYS,
    ) -> HealthReportRow | None:
        # Phase 22.1: cutoff считаем в той же tz, в которой пишем
        # created_at — Europe/Minsk. Иначе "max_age_days" сдвинется
        # на 3 часа в одну сторону, и часть свежих отчётов выпадет.
        from config import now_local

        cutoff = now_local() - timedelta(days=max_age_days)
        cutoff_iso = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        result = await self.session.execute(
            select(HealthReportRow)
            .where(HealthReportRow.vehicle_id == vehicle_id)
            .where(HealthReportRow.created_at >= cutoff_iso)
            .order_by(
                HealthReportRow.created_at.desc(),
                HealthReportRow.id.desc(),
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def save_report(
        self,
        vehicle_id: int,
        score: int,
        risks: list[dict[str, Any]],
    ) -> HealthReportRow:
        # Парсим/сериализуем risks через json.dumps(ensure_ascii=False)
        # чтобы кириллица в БД не превращалась в \uXXXX и оставалась
        # читаемой при ручных SELECT'ах.
        payload = json.dumps(risks, ensure_ascii=False)
        row = HealthReportRow(
            vehicle_id=vehicle_id,
            score=int(max(0, min(100, score))),
            risks_json=payload,
        )
        self.session.add(row)
        await self.session.flush()
        logger.info(
            "Health report id=%s сохранён (vehicle=%s, score=%s, risks=%d)",
            row.id, vehicle_id, row.score, len(risks),
        )
        return row

    async def invalidate_for_vehicle(self, vehicle_id: int) -> int:
        result = await self.session.execute(
            delete(HealthReportRow).where(
                HealthReportRow.vehicle_id == vehicle_id
            )
        )
        affected = result.rowcount or 0
        if affected:
            logger.info(
                "Health отчёты vehicle=%s инвалидированы (rows=%s)",
                vehicle_id, affected,
            )
        return affected

    async def apply_resolved_to_cache(
        self, vehicle_id: int, risk_title: str
    ) -> dict[str, Any] | None:
        """Phase 27 · Persistent fixed-state.

        Вместо удаления кэша при feedback ``resolved`` мы мутируем
        свежий отчёт in-place: убираем риск из risks_json и поднимаем
        score (с учётом probability погашенного риска). Это гарантирует,
        что после reload UI покажет тот же итоговый score и количество
        рисков, а AI не «забудет» про действие клиента до истечения TTL.

        Возвращает ``{score, risks_count, removed}`` после апдейта,
        либо ``None`` если кэша нет (тогда вызывающий код может вообще
        ничего не делать — следующий GET пройдёт по MISS-ветке и
        AI исключит titles из feedback-таблицы).
        """
        row = await self.get_latest_report(vehicle_id)
        if row is None:
            return None
        try:
            risks = json.loads(row.risks_json) or []
        except (TypeError, ValueError):
            risks = []
        if not isinstance(risks, list):
            risks = []
        norm = (risk_title or "").strip().casefold()
        kept: list[dict[str, Any]] = []
        removed_prob = 0
        removed_count = 0
        for r in risks:
            if not isinstance(r, dict):
                continue
            t = str(r.get("title") or "").strip().casefold()
            if t == norm:
                try:
                    removed_prob = max(removed_prob, int(r.get("probability") or 0))
                except (TypeError, ValueError):
                    pass
                removed_count += 1
                continue
            kept.append(r)
        # Бонус к score: половина вероятности погашенного риска,
        # минимум +5, потолок 100. Эмпирически — заметный, но не
        # "+30 за один клик".
        bonus = max(5, removed_prob // 2) if removed_count else 0
        new_score = max(0, min(100, int(row.score or 0) + bonus))
        row.risks_json = json.dumps(kept, ensure_ascii=False)
        row.score = new_score
        await self.session.flush()
        logger.info(
            "Health resolved-applied vehicle=%s removed=%s bonus=+%s score=%s→%s remaining=%s",
            vehicle_id, removed_count, bonus,
            row.score - bonus if removed_count else row.score,
            new_score, len(kept),
        )
        return {
            "score": new_score,
            "risks_count": len(kept),
            "removed": removed_count,
        }

    async def list_active_feedback(
        self, vehicle_id: int
    ) -> list[HealthFeedbackRow]:
        """Все фидбеки клиента по этой машине (resolved/hidden)."""
        result = await self.session.execute(
            select(HealthFeedbackRow)
            .where(HealthFeedbackRow.vehicle_id == vehicle_id)
            .order_by(HealthFeedbackRow.id.desc())
        )
        return list(result.scalars().all())

    async def add_feedback(
        self,
        vehicle_id: int,
        risk_title: str,
        status: str,
    ) -> HealthFeedbackRow:
        if status not in HEALTH_FEEDBACK_STATUSES:
            raise ValueError(
                f"Unknown health feedback status: {status!r}. "
                f"Allowed: {HEALTH_FEEDBACK_STATUSES}"
            )
        risk_title = (risk_title or "").strip()
        if not risk_title:
            raise ValueError("risk_title is empty")
        # Длинные titles из Gemini (>200 символов) подрезаем — это явно
        # выход за пределы разумного описания узла.
        risk_title = risk_title[:200]
        row = HealthFeedbackRow(
            vehicle_id=vehicle_id,
            risk_title=risk_title,
            status=status,
        )
        self.session.add(row)
        await self.session.flush()
        logger.info(
            "Health feedback id=%s vehicle=%s status=%s title=%r",
            row.id, vehicle_id, status, risk_title,
        )
        return row

    @staticmethod
    def parse_risks(report: HealthReportRow) -> list[dict[str, Any]]:
        """Распаковать risks_json в list[dict]. На любую ошибку — пустой
        список (UI рисует «угроз не выявлено», вместо 500-ки)."""
        try:
            decoded = json.loads(report.risks_json)
        except (TypeError, ValueError):
            logger.warning(
                "health_reports id=%s: risks_json не парсится",
                report.id,
            )
            return []
        if not isinstance(decoded, list):
            return []
        out: list[dict[str, Any]] = []
        for item in decoded:
            if isinstance(item, dict):
                out.append(item)
        return out


# ---------------------------------------------------------------------------
# SettingsRepository — глобальный KV-стор настроек CRM.
# Phase 37: используется матрицей расписания (``active_posts_count``).
# Все значения сериализуются как TEXT; типизацией занимается caller.
# ---------------------------------------------------------------------------


class SettingsRepository(BaseRepository):
    """KV-доступ к таблице ``app_settings``.

    Не делает commit — это ответственность вызывающего слоя.
    """

    async def get(self, key: str, default: str | None = None) -> str | None:
        result = await self.session.execute(
            select(AppSettingRow.value).where(AppSettingRow.key == key)
        )
        row = result.first()
        if row is None:
            return default
        return row[0]

    async def get_int(self, key: str, default: int) -> int:
        raw = await self.get(key)
        if raw is None:
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.warning(
                "app_settings[%s] = %r не парсится в int, fallback=%d",
                key,
                raw,
                default,
            )
            return default

    async def set(self, key: str, value: str) -> None:
        """Upsert значения. SQLite ``ON CONFLICT(key) DO UPDATE``."""
        stmt = sqlite_insert(AppSettingRow).values(
            key=key, value=value
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[AppSettingRow.key],
            set_={"value": stmt.excluded.value},
        )
        await self.session.execute(stmt)


# ---------------------------------------------------------------------------
# Утилиты конвертации ``photo_file_id`` (TEXT JSON) ↔ list[str].
# ---------------------------------------------------------------------------

def encode_photo_file_ids(file_ids: Sequence[str] | None) -> str | None:
    """``list[file_id]`` → JSON-строка или ``None``."""
    if not file_ids:
        return None
    return json.dumps(list(file_ids))


def decode_photo_file_ids(payload: str | None) -> list[str]:
    """Декодировать сохранённый JSON-массив с file_id'ами фото.

    Терпит легаси-формат (TEXT с одним file_id вместо JSON-массива).
    """
    if not payload:
        return []
    try:
        decoded = json.loads(payload)
    except (TypeError, ValueError):
        # Легаси: одна строка-id.
        return [payload]
    if isinstance(decoded, list):
        return [str(x) for x in decoded]
    return [str(decoded)]
