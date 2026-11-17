"""
Декларативные ORM-модели SQLAlchemy 2.0.

Структура моделей **полностью совпадает** с фактической схемой
SQLite (см. :mod:`db` — ``CREATE_*_SQL``-константы), так что
baseline-миграция Alembic'а описывает текущее состояние БД 1:1.

Названия классов — суффикс ``Row`` (``UserRow`` etc.), чтобы не
конфликтовать с типами aiogram (``User``) и FastAPI-моделями.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    func,
    text,
)

# Phase 22.1: Python-side default для created_at / last_sent_at.
# Раньше колонки использовали ``server_default=func.current_timestamp()``
# — SQLite пишет UTC и не умеет сам конвертировать в Europe/Minsk.
# Теперь default считается в Python и пишется уже как локальное
# минское время (naive-строка "YYYY-MM-DD HH:MM:SS") — см. config.py.
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)

from config import now_local_sql as _now_local_sql


class Base(DeclarativeBase):
    """Общий ``DeclarativeBase`` для всех моделей.

    ``metadata`` этого класса передаётся Alembic'у как
    ``target_metadata`` — autogenerate сравнивает её с реальной
    схемой БД и предлагает diff.

    Дополнительно поддерживает dict-style индексирование
    (``row['full_name']``) — это нужно для совместимости с легаси-кодом,
    который раньше получал из :mod:`db` объекты ``aiosqlite.Row``.
    Новый код может (и должен) использовать атрибутный доступ
    (``row.full_name``).
    """

    def __getitem__(self, key: str):
        # Совместимость с aiosqlite.Row: row['column_name'].
        try:
            return getattr(self, key)
        except AttributeError as exc:
            raise KeyError(key) from exc

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and hasattr(self, key)


# ---------------------------------------------------------------------------
# users — карточка клиента (один Telegram-аккаунт ↔ один автомобиль).
# ---------------------------------------------------------------------------

class UserRow(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    username: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    car_brand: Mapped[str] = mapped_column(Text, nullable=False)
    car_plate: Mapped[str] = mapped_column(Text, nullable=False)
    last_known_mileage: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    # Phase 15: телефон клиента, заведённый из CRM-формы Mini App.
    # Nullable — legacy-юзеры (созданные до Phase 15) остаются без
    # значения; их телефон по-прежнему достаётся подзапросом из
    # последнего service_request (см. /api/admin/search COALESCE).
    phone: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    history: Mapped[list["UserHistoryRow"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    service_requests: Mapped[list["ServiceRequestRow"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    # Виртуальный гараж: один клиент → много авто. На expand-фазе
    # legacy-поля car_brand/car_plate/last_known_mileage UserRow остаются
    # как есть и синхронизируются при записях (см. handlers); contract-
    # миграция дропнет их следующим релизом.
    vehicles: Mapped[list["VehicleRow"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="VehicleRow.id",
    )

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return (
            f"UserRow(telegram_id={self.telegram_id!r}, "
            f"full_name={self.full_name!r}, car_plate={self.car_plate!r})"
        )


# ---------------------------------------------------------------------------
# vehicles — авто клиента (один клиент ↔ N автомобилей).
# Backfill миграции переносит legacy-поля users.car_brand / car_plate /
# last_known_mileage в первую запись с is_primary=True.
# ---------------------------------------------------------------------------

class VehicleRow(Base):
    __tablename__ = "vehicles"
    __table_args__ = (
        Index("ix_vehicles_user_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        nullable=False,
    )
    # Бэкфилл копирует ВСЮ строку users.car_brand сюда (например
    # «Ford Focus»). Парсить на марку+модель не пытаемся.
    brand: Mapped[str] = mapped_column(Text, nullable=False)
    # Отдельная модель — NULL для бэкфилла; новые записи могут заполнять.
    model: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    vin: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    plate_number: Mapped[str] = mapped_column(Text, nullable=False)
    # Базовая точка пробега. Перезаписывается при ручном вводе клиента
    # и становится новой отправной точкой для расчётов.
    current_mileage: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    avg_yearly_mileage: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    # Объём двигателя в литрах (например 2.0). NULL если мастер пропустил
    # ввод при регистрации или клиент не заполнил при добавлении.
    engine_volume: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    # Когда последний раз клиент/мастер ввёл реальный пробег (current_mileage).
    # Используется фронтом для расчёта "живого пробега":
    # current_mileage + days_since_update * (avg_yearly_mileage / 365).
    # NULL до первого update_current_mileage (бэкфилл оставляет NULL).
    mileage_updated_at: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    # Тип топлива: один из FUEL_TYPES ("Бензин", "Дизель", "Гибрид",
    # "Электро") либо NULL. Используется AI для рекомендаций (масло,
    # фильтры, регламенты) и мастером для подбора расходников.
    fuel_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Phase 18: тип коробки передач — критичен для AI-диагностики.
    # Один из {"АКПП", "МКПП", "Робот", "Вариатор", "Не указано"} либо
    # NULL для legacy-записей. В СТС коробки нет — поле OCR'ом не
    # заполняется, выбирается мастером/клиентом руками.
    transmission: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    avatar_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Главная машина клиента — на неё по умолчанию пишутся mileage-апдейты,
    # она показывается в карточке профиля и в AI-контексте, пока нет UI выбора.
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0")
    )
    created_at: Mapped[str] = mapped_column(
        Text, nullable=False, default=_now_local_sql, server_default=func.current_timestamp()
    )

    user: Mapped["UserRow"] = relationship(back_populates="vehicles")

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return (
            f"VehicleRow(id={self.id!r}, user_id={self.user_id!r}, "
            f"brand={self.brand!r}, plate_number={self.plate_number!r}, "
            f"is_primary={self.is_primary!r})"
        )


# ---------------------------------------------------------------------------
# users_history — записи о выполненных ТО (история).
# ---------------------------------------------------------------------------

class UserHistoryRow(Base):
    __tablename__ = "users_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.telegram_id"),
        nullable=False,
    )
    # Машина, к которой относится эта запись о ТО. NULL допустим только для
    # очень старых записей или если машина была удалена (FK SET NULL). Все
    # новые записи через RecordRepository.add() ОБЯЗАНЫ передавать vehicle_id.
    vehicle_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("vehicles.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    service_type: Mapped[str] = mapped_column(Text, nullable=False)
    # Хранится как ISO-строка YYYY-MM-DD (легаси-совместимость с db.py).
    execution_date: Mapped[str] = mapped_column(Text, nullable=False)
    next_visit_date: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    current_mileage: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    next_service_mileage: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    # JSON-массив file_id (см. db.add_service_record).
    photo_file_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Phase 35 · путь к загруженному изображению («Прикрепить фото»).
    # Хранится как URL-путь от корня сайта, например
    # ``/uploads/maintenance/<uuid>/<file>.jpg``. Не пересекается с
    # ``photo_file_id`` (тот — Telegram-only); фронт использует image_path
    # напрямую как src=. NULL если фото не прикреплено.
    image_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    user: Mapped["UserRow"] = relationship(back_populates="history")


# ---------------------------------------------------------------------------
# admins — список Telegram-ID администраторов СТО.
# ---------------------------------------------------------------------------

class AdminRow(Base):
    __tablename__ = "admins"

    telegram_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # NOT NULL DEFAULT CURRENT_TIMESTAMP — храним как ISO-строку (так делает SQLite).
    added_at: Mapped[str] = mapped_column(
        Text, nullable=False, default=_now_local_sql, server_default=func.current_timestamp()
    )


# ---------------------------------------------------------------------------
# service_requests — заявки на звонок от клиентов.
# ---------------------------------------------------------------------------

class ServiceRequestRow(Base):
    __tablename__ = "service_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.telegram_id"),
        nullable=False,
    )
    phone: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="pending_dropoff"
    )
    created_at: Mapped[str] = mapped_column(
        Text, nullable=False, default=_now_local_sql, server_default=func.current_timestamp()
    )
    # Phase 20: окно заезда, выбранное клиентом при подаче заявки.
    # Human-readable строка, nullable: legacy-заявки и request_call-флоу
    # (без формы) оставляют NULL. Валидируется Pydantic'ом против
    # whitelist'а _DROPOFF_WINDOWS в web_app/main.py.
    dropoff_window: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    # Phase 22: привязка заявки к конкретному автомобилю клиента.
    # NULL для legacy-заявок и для заявок, созданных через глобальный
    # sticky-CTA «Записаться на сервис» (без контекста карточки авто).
    # ON DELETE SET NULL — если машину удалили, заявка остаётся в
    # истории CRM с пустым авто, а не каскадно исчезает.
    vehicle_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("vehicles.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Phase 23: триаж-воронка для мастера. Три ортогональных булевых
    # флага, раскладывающие pending-заявки на три bucket'а в CRM:
    #   * не is_viewed и не is_confirmed и не resolved → «Новые» 🆕
    #   * is_viewed  и не is_confirmed и не resolved  → «Просмотрено» 👁
    #   * is_confirmed и не resolved                  → «Подтверждено» ✅
    # resolved=True убирает заявку из всей воронки (взводится при
    # переходе yard-статуса в in_yard и далее).
    is_viewed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0")
    )
    is_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0")
    )
    resolved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0")
    )
    # Phase 23.1: момент приёмки машины на стоянку. Наивная строка в
    # Europe/Minsk (как остальные timestamp-колонки). Взводится ровно
    # один раз в set_service_request_status при переходе в ``in_yard``;
    # последующие переходы сохраняют значение. Для pending_dropoff — NULL.
    in_yard_at: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Phase 31: внутренний комментарий мастера. Свободное TEXT-поле,
    # которое редактируется только в admin-CRM карточке и НИКОГДА
    # не показывается клиенту. Auto-save на blur через PATCH
    # /api/admin/requests/{id}/master-comment.
    master_comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Phase 37: назначение заявки на слот в матрице расписания.
    # Все три поля nullable — у легаси-заявок их нет, и не каждая
    # заявка обязана быть в матрице (request_call вне слота, например).
    appointment_date: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    appointment_time: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    assigned_post_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    # Phase 37.2: имя мастера, ответственного за визит. Свободная строка
    # (нет таблицы «мастеров») — заполняется на этапе booking из матрицы
    # расписания. NULL для заявок без записи в матрицу и для legacy.
    assigned_master: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    # Phase 37.5: длительность визита в часах (whole hours). >=1, дефолт
    # 1. Используется матрицей для «растягивания» occupied-блока на
    # несколько часов вниз и backend'ом для range-overlap проверки.
    duration_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1"), default=1
    )
    # Phase 37.6: «мастер-центричное» расписание. Канонический ключ
    # колонки матрицы — telegram_id админа-мастера. Имя дублируется в
    # ``assigned_master`` ради быстрых выборок без JOIN'а на admins.
    # NULL у легаси-записей и у любых заявок, ещё не привязанных к слоту.
    assigned_master_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, index=True
    )

    user: Mapped["UserRow"] = relationship(back_populates="service_requests")
    vehicle: Mapped[Optional["VehicleRow"]] = relationship(
        "VehicleRow", foreign_keys=[vehicle_id]
    )


# ---------------------------------------------------------------------------
# app_settings — KV-стор глобальных настроек CRM.
# Phase 37: первая запись — ``active_posts_count`` (количество активных
# подъёмников). value хранится как TEXT, парсится на стороне репозитория
# в нужный тип (int/bool/str) — это позволяет добавлять любые ключи
# без миграций.
# ---------------------------------------------------------------------------


class AppSettingRow(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=_now_local_sql,
        server_default=func.current_timestamp(),
    )


# ---------------------------------------------------------------------------
# notifications_log — журнал отправленных уведомлений (для дедупликации).
# Композитный PK (record_id, severity).
# ---------------------------------------------------------------------------

class NotificationLogRow(Base):
    __tablename__ = "notifications_log"
    __table_args__ = (
        PrimaryKeyConstraint(
            "record_id", "severity", name="pk_notifications_log"
        ),
    )

    record_id: Mapped[int] = mapped_column(Integer, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    last_sent_at: Mapped[str] = mapped_column(
        Text, nullable=False, default=_now_local_sql, server_default=func.current_timestamp()
    )


# ---------------------------------------------------------------------------
# health_reports — кэш AI Health Score per vehicle.
# Phase 9: один отчёт ~ 1 запрос к Gemini, кэшируем на 7 дней.
# Если клиент скрыл/закрыл риск — отчёт инвалидируется и пересоздаётся
# на следующем запросе с обновлённым feedback_list.
# ---------------------------------------------------------------------------

class HealthReportRow(Base):
    __tablename__ = "health_reports"
    __table_args__ = (
        Index(
            "ix_health_reports_vehicle_created",
            "vehicle_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    vehicle_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("vehicles.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Итоговый score 0-100 от модели. Гарантируется sanity-check'ом
    # на стороне generate_health_score (если модель вернула мусор —
    # сохраняем 50 как нейтральный fallback).
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    # JSON-массив рисков, который модель вернула.
    # Формат: [{"title": str, "probability": int 0..100, "recommendation": str}, ...]
    # Сериализуется/десериализуется на уровне репозитория, а не ORM.
    risks_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(
        Text, nullable=False, default=_now_local_sql, server_default=func.current_timestamp()
    )


# ---------------------------------------------------------------------------
# health_feedback — что клиент уже починил/скрыл из риск-листа.
# Используется generate_health_score для исключения этих узлов из новых
# отчётов: модель НЕ должна снова предлагать клапан EGR, если клиент
# отметил его как resolved.
# ---------------------------------------------------------------------------

class HealthFeedbackRow(Base):
    __tablename__ = "health_feedback"
    __table_args__ = (
        Index(
            "ix_health_feedback_vehicle_status",
            "vehicle_id",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    vehicle_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("vehicles.id", ondelete="CASCADE"),
        nullable=False,
    )
    risk_title: Mapped[str] = mapped_column(Text, nullable=False)
    # 'resolved' (клиент починил/заменил узел) либо 'hidden' (клиент
    # просто не хочет видеть этот риск). Для AI оба значения трактуются
    # одинаково — узел исключается из новых отчётов.
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(
        Text, nullable=False, default=_now_local_sql, server_default=func.current_timestamp()
    )
