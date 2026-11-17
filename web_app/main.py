"""
FastAPI backend для Telegram Mini App «Виртуальная сервисная книжка».

Запуск:
    python -m uvicorn web_app.main:app --host 0.0.0.0 --port 8000

Доступ к БД — через те же ``aiosqlite`` хелперы из :mod:`db` (read-only
семантика: API только читает). Аутентификация — через
``Telegram.WebApp.initData`` (HMAC от ``BOT_TOKEN``). Других путей входа
НЕТ: ``WEBAPP_DEV_MODE`` и ``?dev=<id>`` удалены (см. audit H-1).

Rate-limit (slowapi, in-memory bucket):
* глобально 60 req/min на IP (default_limits);
* ``POST /api/ai/ask`` — 5/min на пользователя (или IP, если initData нет);
* ``GET /api/records/{id}/photos/{idx}`` — 30/min на пользователя.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import parse_qsl

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.storage.base import StorageKey
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from config import BOT_TOKEN, localize_naive_sql
from database.models import UserHistoryRow, UserRow
from database.repositories import (
    HEALTH_FEEDBACK_STATUSES,
    AdminRepository,
    HealthRepository,
    RecordRepository,
    SettingsRepository,
    UserRepository,
    VehicleRepository,
    decode_photo_file_ids,
)
from database.session import async_session_factory
from fsm_storage import SQLiteStorage
from handlers.user import (
    REQUEST_CALL_PROMPT,
    request_phone_kb,
    share_contact_kb,
)
from states import RequestCall

from web_app import ai as ai_master
from web_app import cars_dataset
from web_app.auth import (
    InitDataInvalid,
    TelegramUser,
    validate_init_data,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


HISTORY_LIMIT_DEFAULT = 7
HISTORY_LIMIT_MAX = 30

STATIC_DIR = Path(__file__).resolve().parent / "static"
# Phase 35 · корень для пользовательских аплоадов (фото записей ТО и др.).
# Мы СОЗНАТЕЛЬНО не кладём аплоады внутрь STATIC_DIR — там лежат собранные
# ассеты под git'ом, а user-uploads должны переживать deploy без коллизий.
# Каталог создаётся при старте (см. ниже). Mount на /uploads подключается
# в самом конце файла, чтобы StaticFiles увидела готовый путь.
UPLOADS_DIR = Path(__file__).resolve().parent.parent / "uploads"
MAINTENANCE_UPLOADS_DIR = UPLOADS_DIR / "maintenance"
MAINTENANCE_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
AVATAR_UPLOADS_DIR = UPLOADS_DIR / "avatars"
AVATAR_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
# Phase 36 · фото + видео. Видео тяжелее, так что лимит поднят до 50 МБ.
# Превышение → 413.
MAINTENANCE_MAX_BYTES = 50 * 1024 * 1024
# Разрешённые расширения (нормализованные lower-case без точки).
# Изображения + базовые форматы видео для смартфон-съёмки (iOS делает
# .mov, Android — .mp4; Telegram-камера тоже отдаёт mp4).
MAINTENANCE_ALLOWED_EXT: frozenset[str] = frozenset({
    # images
    "jpg", "jpeg", "png", "webp", "heic", "heif",
    # video
    "mp4", "mov", "m4v", "webm", "mkv", "3gp",
})
# Map MIME → ext для фолбэка, когда у файла нет расширения в имени.
MAINTENANCE_MIME_TO_EXT: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/heic": "heic",
    "image/heif": "heif",
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "video/x-m4v": "m4v",
    "video/webm": "webm",
    "video/x-matroska": "mkv",
    "video/3gpp": "3gp",
}
AVATAR_MAX_BYTES = 5 * 1024 * 1024
AVATAR_ALLOWED_EXT: frozenset[str] = frozenset({
    "jpg", "jpeg", "png", "webp", "heic", "heif",
})
AVATAR_MIME_TO_EXT: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/heic": "heic",
    "image/heif": "heif",
}


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


def _user_or_ip_key(request: Request) -> str:
    """Ключ rate-limit бакета: ``user:<telegram_id>`` если в запросе есть
    initData с user.id, иначе ``ip:<remote_address>``.

    ВАЖНО: HMAC здесь НЕ проверяется — это лишь идентификатор бакета.
    Реальная аутентификация остаётся в :func:`current_user`. Атакующий,
    подставивший чужой user_id, попадёт в чужой rate-limit бакет — то
    есть только ускорит исчерпание собственного лимита; никаких прав
    это ему не даёт.
    """
    init_data, _ = _extract_init_data(request)
    if init_data:
        try:
            pairs = dict(parse_qsl(init_data, keep_blank_values=True))
            user_raw = pairs.get("user")
            if user_raw:
                user = json.loads(user_raw)
                uid = user.get("id")
                if isinstance(uid, int):
                    return f"user:{uid}"
        except (ValueError, TypeError):
            pass
    return f"ip:{get_remote_address(request)}"


# Глобальный лимитер. ``default_limits`` применяется ко ВСЕМ роутам,
# у которых нет точечного override. Ключ — IP клиента.
# Per-route декораторы ниже могут указать свой ``key_func`` (по user_id).
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["60/minute"],
    headers_enabled=True,
)


app = FastAPI(title="ServiceBook Mini App", version="0.1.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# Один разделяемый Bot-инстанс для скачивания фото с TG.
_bot: Bot | None = None


def get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot(token=BOT_TOKEN)
    return _bot


# Bot id вытаскиваем из токена (та же часть, что aiogram использует
# при формировании StorageKey) — нужен, чтобы из webapp писать в FSM
# в тот же бакет, который читает polling-цикл бота.
_BOT_ID = int(BOT_TOKEN.split(":", 1)[0]) if BOT_TOKEN and ":" in BOT_TOKEN else 0
# Singleton FSM-storage поверх той же SQLite-БД, что и у бота.
# Webapp пишет состояние, бот читает его на следующем апдейте от клиента.
_fsm_storage = SQLiteStorage()


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _bot is not None:
        await _bot.session.close()


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------


def _extract_init_data(request: Request) -> tuple[str | None, str]:
    """Вернуть ``(init_data, source)``. ``source`` — для логов."""
    auth = request.headers.get("authorization")
    if auth:
        # Принимаем «Authorization: Bearer <initData>» (рекомендация
        # Telegram) и «Authorization: tma <initData>» (формат, который
        # шлёт официальный SDK с ``initDataRaw``).
        for prefix in ("bearer ", "tma "):
            if auth.lower().startswith(prefix):
                return auth[len(prefix):].strip(), f"Authorization: {prefix.strip()}"
    x_init = request.headers.get("x-init-data")
    if x_init:
        return x_init, "X-Init-Data"
    q = request.query_params.get("initData") or request.query_params.get("tgWebAppData")
    if q:
        return q, "query"
    return None, "none"


async def current_user(request: Request) -> TelegramUser:
    """Резолвим юзера из (а) ``Authorization: Bearer <initData>`` /
    ``Authorization: tma <initData>``, (б) ``X-Init-Data`` (legacy),
    (в) query-параметра ``initData``.

    Других путей входа не существует: ``WEBAPP_DEV_MODE`` и
    ``resolve_user_dev`` удалены, единственный способ зайти — валидный
    initData с проверкой HMAC.
    """
    init_data, source = _extract_init_data(request)

    auth_present = bool(request.headers.get("authorization"))
    logger.info(
        "AUTH path=%s ip=%s ua=%r authz_header=%s init_source=%s "
        "init_len=%d ua_len=%d",
        request.url.path,
        request.client.host if request.client else "?",
        (request.headers.get("user-agent") or "")[:60],
        "Yes" if auth_present else "No",
        source,
        len(init_data) if init_data else 0,
        len(request.headers.get("user-agent") or ""),
    )

    if init_data:
        try:
            user = validate_init_data(init_data, BOT_TOKEN)
            logger.info("AUTH OK user_id=%s via=%s", user.id, source)
            return user
        except InitDataInvalid as exc:
            # Логируем точную причину + первые 80 символов initData
            # (без пейлоада хэша всё равно спуфить нечего).
            logger.warning(
                "AUTH 401 reason=%r init_data_head=%r",
                str(exc),
                init_data[:80],
            )
            raise HTTPException(status_code=401, detail=f"initData invalid: {exc}")

    logger.warning(
        "AUTH 401 reason=no_init_data path=%s headers=%s",
        request.url.path,
        list(request.headers.keys()),
    )
    raise HTTPException(status_code=401, detail="initData required")


# ---------------------------------------------------------------------------
# DB session dependency
# ---------------------------------------------------------------------------


async def get_session() -> AsyncSession:  # type: ignore[misc]
    """FastAPI-dependency: выдаёт асинхронную сессию SQLAlchemy.

    Используется во всех эндпоинтах вместо legacy ``_open_db``.
    API режим read-only — commit вызываем в точках записи явно.
    """
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def _fetch_profile(
    session: AsyncSession, user_id: int
) -> dict[str, Any] | None:
    """Профиль клиента + первая (главная) машина из таблицы ``vehicles``.

    На expand-фазе vehicles — единый источник правды для машины/пробега;
    legacy users.car_* / last_known_mileage оставлены как fallback на
    случай, если по какой-то причине backfill пропустил клиента (напр.,
    car_brand был пустой). Возвращаемый dict держит legacy-ключи
    (``car_brand``, ``car_plate``, ``last_known_mileage``) ради
    обратной совместимости фронтенда и AI-промпта.
    """
    profile = await UserRepository(session).get(user_id)
    if profile is None:
        return None
    vehicle = await VehicleRepository(session).get_primary(user_id)

    brand = vehicle.brand if vehicle is not None else profile.car_brand
    plate = (
        vehicle.plate_number if vehicle is not None else profile.car_plate
    )
    mileage = (
        vehicle.current_mileage
        if vehicle is not None
        else profile.last_known_mileage
    )

    return {
        "telegram_id": profile.telegram_id,
        "full_name": profile.full_name,
        "username": profile.username,
        # Phase 20: phone нужен фронту, чтобы пре-заполнить поле в
        # booking-sheet («🗓 Записаться на сервис»).
        "phone": profile.phone,
        # Legacy-ключи (фронт + AI до сих пор обращаются к ним).
        "car_brand": brand,
        "car_plate": plate,
        "last_known_mileage": mileage,
        # Новый явный набор (для будущего фронта с виртуальным гаражом).
        "primary_vehicle_id": vehicle.id if vehicle else None,
        "primary_vehicle": (
            _vehicle_to_dict(vehicle) if vehicle is not None else None
        ),
    }


async def _fetch_history(
    session: AsyncSession,
    user_id: int,
    limit: int,
    vehicle_id: int | None = None,
) -> list[dict[str, Any]]:
    """История ТО клиента. Если ``vehicle_id`` задан — фильтруем по нему."""
    rows = await RecordRepository(session).get_user_history(
        user_id, vehicle_id=vehicle_id
    )
    out: list[dict[str, Any]] = []
    for r in rows[:limit]:
        photo_ids = decode_photo_file_ids(r.photo_file_id)
        out.append(
            {
                "id": r.id,
                "service_type": r.service_type,
                "execution_date": r.execution_date,
                "next_visit_date": r.next_visit_date,
                "current_mileage": r.current_mileage,
                "next_service_mileage": r.next_service_mileage,
                "vehicle_id": r.vehicle_id,
                "photo_count": len(photo_ids),
                # Phase 35 · прямой URL-путь к фото (если приложено через
                # /api/admin/maintenance/upload). NULL — фото не было.
                "image_path": r.image_path,
            }
        )
    return out


async def _fetch_photo_ids_for_record(
    session: AsyncSession, user_id: int | None, record_id: int
) -> list[str] | None:
    """Вернуть список file_id для записи. Если ``user_id`` задан —
    проверяем принадлежность записи клиенту (защита от IDOR).
    Если ``user_id is None`` (админ) — достаем по id записи без
    фильтра по владельцу. ``None`` если запись не найдена."""
    where_clauses = [UserHistoryRow.id == record_id]
    if user_id is not None:
        where_clauses.append(UserHistoryRow.user_id == user_id)
    result = await session.execute(
        select(UserHistoryRow.photo_file_id).where(*where_clauses)
    )
    row = result.first()
    if row is None:
        return None
    return decode_photo_file_ids(row[0])


async def _fetch_progress_anchors(
    session: AsyncSession, user_id: int
) -> dict[str, Any]:
    """Виджет пробега: берём последнюю запись (по дате) с ненулевым
    ``next_service_mileage`` — она задаёт «цель», а её ``current_mileage`` —
    «начало шкалы». Если такой записи нет, возвращаем пустые значения.
    """
    result = await session.execute(
        select(
            UserHistoryRow.current_mileage,
            UserHistoryRow.next_service_mileage,
            UserHistoryRow.service_type,
            UserHistoryRow.execution_date,
        )
        .where(
            UserHistoryRow.user_id == user_id,
            UserHistoryRow.next_service_mileage.is_not(None),
            UserHistoryRow.current_mileage.is_not(None),
        )
        .order_by(
            UserHistoryRow.execution_date.desc(),
            UserHistoryRow.id.desc(),
        )
        .limit(1)
    )
    row = result.first()
    if row is None:
        return {
            "start_mileage": None,
            "target_mileage": None,
            "service_type": None,
            "execution_date": None,
        }
    return {
        "start_mileage": row.current_mileage,
        "target_mileage": row.next_service_mileage,
        "service_type": row.service_type,
        "execution_date": row.execution_date,
    }


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.get("/api/me")
async def api_me(
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    # Phase 12: админ может НЕ быть зарегистрированным клиентом
    # (его tg-id живёт только в таблице ``admins``). Раньше это
    # отдавало 404 и фронт ломался. Теперь определяем is_admin
    # ДО загрузки профиля и допускаем «admin без profile» —
    # фронт покажет ему admin-dashboard вместо клиентского гаража.
    is_admin = await AdminRepository(session).is_admin(user.id)

    profile = await _fetch_profile(session, user.id)
    # Phase 16: ``is_guest`` — лид без записи в CRM (юзер открыл Mini App,
    # но мастер ещё не завёл его карточку), либо клиент с пустым гаражом.
    # Админ никогда не гость. Используется фронтом, чтобы вместо клиентского
    # дашборда показать landing с CTA «Оставить заявку».
    if is_admin:
        is_guest = False
    elif profile is None:
        is_guest = True
    else:
        garage_size = len(
            await VehicleRepository(session).list_for_user(user.id)
        )
        is_guest = garage_size == 0

    if profile is None and not is_admin:
        # Phase 16: для гостя НЕ кидаем 404 — фронт получит is_guest=true
        # и покажет landing-секцию с формой заявки.
        return {
            "user": {
                "id": user.id,
                "first_name": user.first_name,
                "username": user.username,
            },
            "profile": None,
            "progress": {"available": False},
            "is_admin": False,
            "is_guest": True,
        }
    if profile is None:
        # Admin без профиля: возвращаем минимальный ответ. Прогресс/
        # progress-bar клиента ему не нужен — у него своя UI-точка.
        return {
            "user": {
                "id": user.id,
                "first_name": user.first_name,
                "username": user.username,
            },
            "profile": None,
            "progress": {"available": False},
            "is_admin": True,
            "is_guest": False,
        }
    progress = await _fetch_progress_anchors(session, user.id)
    current = profile["last_known_mileage"]
    start = progress["start_mileage"]
    target = progress["target_mileage"]

    progress_payload: dict[str, Any]
    if start is not None and target is not None and target > start:
        rem_km = target - (current if current is not None else start)
        elapsed = (current if current is not None else start) - start
        total = target - start
        pct = max(0.0, min(1.0, elapsed / total)) if total > 0 else 0.0
        if rem_km < 0:
            severity = "overdue"
        elif rem_km <= 1000:
            severity = "urgent"
        else:
            severity = "ok"
        progress_payload = {
            "available": True,
            "start_mileage": start,
            "current_mileage": current,
            "target_mileage": target,
            "remaining_km": rem_km,
            "percent": round(pct * 100, 1),
            "severity": severity,
            "service_type": progress["service_type"],
        }
    else:
        progress_payload = {"available": False}

    # Phase 12: фронту нужен флаг is_admin, чтобы показать
    # admin-dashboard (поиск). Бэкенд всё равно жёстко проверяет
    # права в /api/admin/*, так что фронтовый флаг — чисто UX.
    is_admin = await AdminRepository(session).is_admin(user.id)

    return {
        "user": {
            "id": user.id,
            "first_name": user.first_name,
            "username": user.username,
        },
        "profile": profile,
        "progress": progress_payload,
        "is_admin": is_admin,
        "is_guest": is_guest,
    }


def _vehicle_to_dict(v: Any) -> dict[str, Any]:
    """Сериализация VehicleRow в dict для API. Единый формат и для
    GET /api/vehicles, и для POST-ответа, и для primary_vehicle в /api/me."""
    return {
        "id": v.id,
        "brand": v.brand,
        "model": v.model,
        "year": v.year,
        "vin": v.vin,
        "plate_number": v.plate_number,
        "current_mileage": v.current_mileage,
        "avg_yearly_mileage": v.avg_yearly_mileage,
        "engine_volume": v.engine_volume,
        "fuel_type": v.fuel_type,
        "transmission": v.transmission,
        "avatar_path": v.avatar_path,
        # Phase 22.1: timestamp-поля локализуем под Europe/Minsk с
        # TZ-суффиксом (+03:00) — иначе JS ``new Date(...)`` на клиенте
        # поймёт naive-строку как UTC и сдвинет отображение на 3 часа.
        "mileage_updated_at": localize_naive_sql(v.mileage_updated_at),
        "is_primary": bool(v.is_primary),
        "created_at": localize_naive_sql(v.created_at),
    }


@app.get("/api/vehicles")
async def api_vehicles(
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Список всех машин клиента (виртуальный гараж).

    Сортировка: главная машина (``is_primary=True``) первой, далее по
    возрастанию ``id``.

    Phase 8: каждый item обогащается ``latest_record`` — сводкой по
    самой свежей записи ТО ИМЕННО ЭТОЙ машины (нужно фронту, чтобы
    рендерить прогресс-бар на каждой карточке без отдельных запросов).
    Если записей нет — ``latest_record`` равен ``None`` и фронт
    показывает строку «Нет данных для расчёта ТО».
    """
    rows = await VehicleRepository(session).list_for_user(user.id)
    records = RecordRepository(session)
    items: list[dict[str, Any]] = []
    for v in rows:
        item = _vehicle_to_dict(v)
        latest = await records.get_latest_for_vehicle(v.id)
        item["latest_record"] = (
            {
                "id": latest.id,
                "service_type": latest.service_type,
                "execution_date": latest.execution_date,
                "current_mileage": latest.current_mileage,
                "next_service_mileage": latest.next_service_mileage,
            }
            if latest is not None
            else None
        )
        items.append(item)
    return {"items": items, "count": len(items)}


class CreateVehicleRequest(BaseModel):
    """Pydantic-схема для POST /api/vehicles. Бренд и госномер обязательны,
    остальное — опционально (NULL в БД)."""

    brand: str = Field(min_length=1, max_length=120)
    plate_number: str = Field(min_length=1, max_length=40)
    model: str | None = Field(default=None, max_length=120)
    year: int | None = Field(default=None, ge=1900, le=2100)
    engine_volume: float | None = Field(default=None, ge=0.3, le=12.0)
    current_mileage: int | None = Field(default=None, ge=0, le=10_000_000)
    avg_yearly_mileage: int | None = Field(default=None, ge=0, le=500_000)
    vin: str | None = Field(default=None, max_length=40)
    # Phase 34 · «Safety valve» — тип топлива и трансмиссии теперь
    # принимаются как свободная строка (max 32). Каноничные значения
    # фронт отдаёт из /api/cars/fuels и /api/cars/transmissions, но
    # пользователь может выбрать «Other / Ручной ввод» и вписать что
    # угодно (например, «LPG», «Гибрид Plug-in», «iDrive Sport»). Старые
    # whitelist'ы (Бензин/Дизель/Гибрид/Электро и т.п.) остаются
    # default-значениями в UI.
    fuel_type: str | None = Field(default=None, max_length=32)
    transmission: str | None = Field(default=None, max_length=32)


@app.post("/api/vehicles")
@limiter.limit("10/minute", key_func=_user_or_ip_key)
async def api_create_vehicle(
    request: Request,
    response: Response,
    body: CreateVehicleRequest,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Добавить машину в виртуальный гараж клиента.

    Если у клиента ещё нет машин — новая становится ``is_primary``;
    иначе — обычная (primary не трогаем).
    """
    # Профиль обязан существовать (клиент должен быть зарегистрирован
    # мастером прежде чем добавлять авто).
    profile = await UserRepository(session).get(user.id)
    if profile is None:
        raise HTTPException(
            status_code=403,
            detail="Профиль клиента не найден. Обратитесь к мастеру.",
        )

    repo = VehicleRepository(session)
    existing = await repo.list_for_user(user.id)
    is_first = len(existing) == 0

    vehicle = await repo.add(
        user_id=user.id,
        brand=body.brand.strip(),
        plate_number=body.plate_number.strip(),
        model=body.model.strip() if body.model else None,
        year=body.year,
        vin=body.vin.strip() if body.vin else None,
        current_mileage=body.current_mileage,
        avg_yearly_mileage=body.avg_yearly_mileage,
        engine_volume=body.engine_volume,
        fuel_type=body.fuel_type,
        transmission=body.transmission,
        is_primary=is_first,
    )
    await session.commit()
    logger.info(
        "[/api/vehicles POST] user=%s vehicle_id=%s primary=%s",
        user.id, vehicle.id, is_first,
    )
    return {"vehicle": _vehicle_to_dict(vehicle)}


@app.post("/api/vehicles/{vehicle_id}/avatar")
@limiter.limit("20/minute", key_func=_user_or_ip_key)
async def api_vehicle_avatar_upload(
    request: Request,
    response: Response,
    vehicle_id: int,
    avatar: UploadFile = File(..., description="Vehicle avatar image"),
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    vehicle = await VehicleRepository(session).get(vehicle_id)
    if vehicle is None or vehicle.user_id != user.id:
        raise HTTPException(status_code=404, detail="vehicle not found")

    content_type = (avatar.content_type or "").lower()
    if content_type and not content_type.startswith("image/"):
        raise HTTPException(status_code=415, detail="image/* expected")

    raw_name = avatar.filename or ""
    ext = raw_name.rsplit(".", 1)[-1].lower() if "." in raw_name else ""
    if ext not in AVATAR_ALLOWED_EXT:
        ext = AVATAR_MIME_TO_EXT.get(content_type, "")
    if ext not in AVATAR_ALLOWED_EXT:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported extension; allowed: {sorted(AVATAR_ALLOWED_EXT)}",
        )

    raw = await avatar.read(AVATAR_MAX_BYTES + 1)
    if len(raw) > AVATAR_MAX_BYTES:
        raise HTTPException(status_code=413, detail="file too large (>5MB)")
    if not raw:
        raise HTTPException(status_code=400, detail="empty file")

    target_dir = AVATAR_UPLOADS_DIR / str(vehicle_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    file_name = f"{uuid.uuid4().hex}.{ext}"
    target_path = target_dir / file_name
    try:
        target_path.write_bytes(raw)
    except OSError as exc:
        logger.exception("[VehicleAvatar] save failed: %s", exc)
        raise HTTPException(status_code=500, detail="failed to persist file") from exc

    url_path = f"/uploads/avatars/{vehicle_id}/{file_name}"
    vehicle.avatar_path = url_path
    await session.commit()
    logger.info(
        "[VehicleAvatar] upload ok user=%s vehicle=%s bytes=%d path=%s",
        user.id, vehicle_id, len(raw), url_path,
    )
    return {"ok": True, "avatar_path": url_path, "vehicle": _vehicle_to_dict(vehicle)}


@app.get("/api/history")
async def api_history(
    limit: int = HISTORY_LIMIT_DEFAULT,
    vehicle_id: int | None = None,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """История ТО клиента. Опциональный query-параметр ``vehicle_id``
    фильтрует историю по конкретной машине из гаража. Без него —
    возвращаются все записи клиента (включая legacy без vehicle_id).

    Админы: могут запросить историю по любой машине в системе — при
    `vehicle_id`, чьи user_id не совпадает с текущим, история
    поднимается по владельцу машины.
    """
    limit = max(1, min(HISTORY_LIMIT_MAX, int(limit)))
    target_user_id = user.id
    # Защита от IDOR: если vehicle_id задан, проверяем что он принадлежит
    # клиенту (или запрос от админа).
    if vehicle_id is not None:
        v = await VehicleRepository(session).get(int(vehicle_id))
        if v is None:
            raise HTTPException(status_code=404, detail="vehicle not found")
        if v.user_id != user.id:
            is_admin = await AdminRepository(session).is_admin(user.id)
            if not is_admin:
                raise HTTPException(status_code=404, detail="vehicle not found")
            # Админ видит историю по владельцу, а не по себе.
            target_user_id = v.user_id
    items = await _fetch_history(
        session, target_user_id, limit, vehicle_id=vehicle_id
    )
    return {"items": items, "count": len(items), "vehicle_id": vehicle_id}


@app.get("/api/records/{record_id}/photos")
async def api_record_photos(
    record_id: int,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    # Админ видит фото любой записи — это нужно для просмотра
    # истории клиента в admin-UI.
    is_admin = await AdminRepository(session).is_admin(user.id)
    owner_filter = None if is_admin else user.id
    photo_ids = await _fetch_photo_ids_for_record(session, owner_filter, record_id)
    if photo_ids is None:
        raise HTTPException(status_code=404, detail="record not found")
    return {
        "record_id": record_id,
        "photos": [
            {
                "index": i,
                "url": f"/api/records/{record_id}/photos/{i}",
            }
            for i in range(len(photo_ids))
        ],
    }


@app.get("/api/records/{record_id}/photos/{idx}")
@limiter.limit("30/minute", key_func=_user_or_ip_key)
async def api_record_photo_bytes(
    request: Request,
    record_id: int,
    idx: int,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    is_admin = await AdminRepository(session).is_admin(user.id)
    owner_filter = None if is_admin else user.id
    photo_ids = await _fetch_photo_ids_for_record(session, owner_filter, record_id)
    if photo_ids is None:
        raise HTTPException(status_code=404, detail="record not found")
    if idx < 0 or idx >= len(photo_ids):
        raise HTTPException(status_code=404, detail="photo index out of range")
    bot = get_bot()
    file_id = photo_ids[idx]
    try:
        tg_file = await bot.get_file(file_id)
        # download_file возвращает BytesIO в aiogram 3.
        bio = await bot.download_file(tg_file.file_path)
    except Exception as exc:  # pragma: no cover — TG runtime errors
        logger.exception("download photo failed: %s", exc)
        raise HTTPException(status_code=502, detail="upstream photo fetch failed")
    if bio is None:
        raise HTTPException(status_code=502, detail="empty photo body")
    data = bio.read()
    # Кэшируем агрессивно: file_id у TG неизменен, картинка та же.
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ---------------------------------------------------------------------------
# AI master endpoint
# ---------------------------------------------------------------------------


class ChatTurn(BaseModel):
    """Одна реплика в истории чата с ИИ-мастером.

    Названия ролей ровно такие же, как ждёт Google Gemini SDK
    (``user`` / ``model``) — чтобы не делать лишнюю перемаппу.
    """

    role: Literal["user", "model"]
    content: str = Field(min_length=1, max_length=4000)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    # История ЧАТА (предыдущие реплики), без текущего ``question``.
    # Фронт бьёт последние ~10 пар «user/model»; бэк дополнительно
    # обрезает в ``ai.py::_coerce_chat_history`` до безопасного лимита.
    history: list[ChatTurn] = Field(default_factory=list, max_length=40)


@app.get("/api/ai/status")
async def api_ai_status() -> dict[str, bool]:
    """Готов ли AI-модуль (нужен фронту, чтобы скрывать вкладку чата
    если ключ не сконфигурирован)."""
    return {"available": ai_master.is_available()}


@app.post("/api/request_call")
@limiter.limit("10/minute", key_func=_user_or_ip_key)
async def api_request_call(
    request: Request,
    response: Response,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    """Кнопка «🗓 Записаться на сервис» из мини-аппа.

    Telegram-метод ``WebApp.sendData`` работает ТОЛЬКО если мини-апп
    открыт через reply-keyboard-кнопку. В нашей конфигурации мини-апп
    открывается через Menu Button (📎) и через inline-кнопку — обе
    эти точки входа ``sendData`` молча игнорируют, поэтому путь
    ``F.web_app_data`` → ``btn_request_call`` со стороны бота не
    получает ничего, и UI «не реагирует».

    Заводим эквивалентный REST-путь: webapp авторизованно дёргает
    этот эндпоинт, мы из бота присылаем клиенту тот же приглашающий
    текст и reply-клавиатуру с «📱 Отправить номер», и поднимаем
    в общем SQLite-FSM-storage состояние ``RequestCall.waiting_phone``.
    Дальше всё штатно: клиент жмёт кнопку, его ``contact`` ловит
    ``request_call_contact`` (тот же FSM-стейт-ключ, что и у бота —
    бот и webapp пишут в одну таблицу ``fsm_state``).
    """
    profile = await UserRepository(session).get(user.id)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    bot = get_bot()
    try:
        await bot.send_message(
            user.id,
            REQUEST_CALL_PROMPT,
            reply_markup=request_phone_kb(),
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.warning(
            "request_call: send_message failed for %s: %s", user.id, exc
        )
        raise HTTPException(status_code=502, detail="cannot reach the chat")

    storage_key = StorageKey(
        bot_id=_BOT_ID, chat_id=user.id, user_id=user.id
    )
    await _fsm_storage.set_state(storage_key, RequestCall.waiting_phone)
    logger.info("request_call queued for user_id=%s", user.id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Phase 16 · Guest funnel
# ---------------------------------------------------------------------------


# Phase 20: валидный whitelist человеко-читаемых строк окна заезда.
# Не i18n'им на бэке — кладём в БД ровно то, что выбрал клиент; в
# админ-UI эту же строку и показываем. Точные значения жёстко
# синхронизированы с <option value="..."> в index.html.
_DROPOFF_WINDOWS: tuple[str, ...] = (
    "Завтра утром (9:00-12:00)",
    "Завтра днём (12:00-18:00)",
    "Оставлю ключи вечером/ночью",
    "Пригоню прямо сейчас",
)


# Phase 20: жёсткий enum статусов заявок. Pydantic Literal автоматически
# даёт 422 на невалидный статус в PATCH-пейлоаде.
_REQUEST_STATUSES: tuple[str, ...] = (
    "pending_dropoff",
    "in_yard",
    "in_progress",
    "ready",
    "completed",
    "canceled",
)


# Literal-типы для Pydantic. Значения дублируют кортежи выше — так
# надо, mypy не умеет Literal[*tuple].
_DropoffWindow = Literal[
    "Завтра утром (9:00-12:00)",
    "Завтра днём (12:00-18:00)",
    "Оставлю ключи вечером/ночью",
    "Пригоню прямо сейчас",
]
_RequestStatus = Literal[
    "pending_dropoff", "in_yard", "in_progress", "ready", "completed",
    "canceled",
]


class GuestRequestPayload(BaseModel):
    """Body POST /api/client/guest-request.

    ``phone`` приходит после маски, не занимаемся нормализацией здесь —
    мастеру удобнее видеть формат, который ввёл клиент. Минимальная
    валидация: 4..32 видимых символов.

    Phase 20: ``dropoff_window`` — выбор «когда пригонит» из whitelist
    _DROPOFF_WINDOWS. Опционален (NULL = «не указал, мастер созвонится»).
    """

    phone: str = Field(min_length=4, max_length=32)
    message: str | None = Field(default=None, max_length=1500)
    dropoff_window: _DropoffWindow | None = None


class ClientRequestPayload(BaseModel):
    """Body POST /api/client/request (Phase 20).

    Зарегистрированный клиент подаёт заявку из своего гаража.
    ``phone`` опционален — если NULL, берём ``users.phone`` (проставляем
    при guest-onboarding'е или из CRM), если и там пусто — 400.

    Phase 22: ``car_id`` — опциональная привязка к конкретному
    автомобилю клиента. Бэкенд валидирует, что машина принадлежит
    этому ``telegram_id`` (защита от IDOR). Если поле отсутствует
    или NULL — заявка создаётся без привязки (legacy/«sticky CTA»
    flow).
    """

    phone: str | None = Field(default=None, max_length=32)
    message: str | None = Field(default=None, max_length=1500)
    dropoff_window: _DropoffWindow | None = None
    car_id: int | None = Field(default=None, ge=1)


class RequestStatusPayload(BaseModel):
    """Body PATCH /api/admin/requests/{id}/status (Phase 20)."""

    status: _RequestStatus


# Phase 31 · master_comment.
# Жёсткий потолок длины — 4000 символов. Это в разы больше, чем
# реалистично пишет мастер в одной заметке, и одновременно
# защищает от случайной/злонамеренной нагрузки на TEXT-колонку.
_MASTER_COMMENT_MAX_LEN: int = 4000


class MasterCommentPayload(BaseModel):
    """Body PATCH /api/admin/requests/{id}/master-comment (Phase 31).

    ``master_comment`` опционален: ``null`` или пустая строка
    очищают поле в БД. Любое непустое значение обрезается до
    :data:`_MASTER_COMMENT_MAX_LEN`.
    """

    master_comment: str | None = Field(
        default=None, max_length=_MASTER_COMMENT_MAX_LEN
    )


@app.post("/api/client/guest-request")
@limiter.limit("3/minute", key_func=_user_or_ip_key)
async def api_client_guest_request(
    request: Request,
    response: Response,
    body: GuestRequestPayload,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Заявка от лида — нажал «Оставить заявку» в #guest-landing.

    1. Если в ``users`` нет записи (типичный кейс лида) — заводим
       минимальный профиль с пустыми ``car_brand`` / ``car_plate``
       (NOT NULL в legacy-схеме, но допускают пустую строку). ``phone``
       сразу попадает в ``users.phone`` — мастер увидит контакт прямо
       в карточке клиента.
    2. Создаём запись в ``service_requests`` со ``status='new'``.
    3. **Silent (§4.3 PROJECT_MEMORY)** — никаких уведомлений ни клиенту
       (он сам видит success-state в Mini App), ни в общий чат.

    Rate-limit 3/min: дешёвая защита от случайного двойного клика и
    автоматизированного спама лидами.
    """
    phone = body.phone.strip()
    msg = (body.message or "").strip() or None

    repo = UserRepository(session)
    profile = await repo.get(user.id)
    if profile is None:
        # Лид. Заводим минимальный профиль; имя берём из Telegram, чтобы
        # в админ-поиске сразу был узнаваемый человек, а не голый id.
        full_name = (user.first_name or "").strip() or f"id{user.id}"
        if user.last_name:
            full_name = f"{full_name} {user.last_name}".strip()
        await repo.create(
            telegram_id=user.id,
            full_name=full_name,
            car_brand="",
            car_plate="",
            username=user.username,
            phone=phone,
        )
    else:
        # Клиент уже в CRM — мог потерять машину или просто хочет подать
        # заявку. Не перезаписываем car_brand/car_plate. Если ``phone``
        # ранее не был задан, подставляем — мастеру удобнее.
        if not profile.phone:
            await session.execute(
                update(UserRow)
                .where(UserRow.telegram_id == user.id)
                .values(phone=phone)
            )

    req = await repo.create_service_request(
        telegram_id=user.id,
        phone=phone,
        description=msg,
        dropoff_window=body.dropoff_window,
    )
    await session.commit()
    logger.info(
        "guest_request created id=%s by user_id=%s (phone=%s, window=%r)",
        req.id, user.id, phone, body.dropoff_window,
    )
    return {"ok": True, "request_id": req.id}


# ---------------------------------------------------------------------------
# Phase 20 · Client request (зарегистрированный клиент из гаража).
# ---------------------------------------------------------------------------


@app.post("/api/client/request")
@limiter.limit("5/minute", key_func=_user_or_ip_key)
async def api_client_request(
    request: Request,
    response: Response,
    body: ClientRequestPayload,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Заявка от зарегистрированного клиента.

    Phase 20 заменяет legacy bot-chat-flow (``/api/request_call``):
    клиент жмёт CTA «Записаться на сервис» в гараже → открывается
    bottom-sheet с телефоном (префилл из профиля) и выбором окна
    заезда → submit сюда. Без перехода в чат с ботом.

    Silent (§ 4.3 PROJECT_MEMORY) — никаких уведомлений клиенту.
    Мастер видит заявку в yard-board сразу после рефреша.
    """
    profile = await UserRepository(session).get(user.id)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")

    # Phone: из payload (если клиент хочет указать альтернативный
    # номер), иначе берём сохранённый в users.phone.
    phone = (body.phone or "").strip() or (profile.phone or "").strip()
    if not phone:
        raise HTTPException(
            status_code=400,
            detail="phone required (neither payload nor profile has one)",
        )

    msg = (body.message or "").strip() or None

    # Phase 22: car_id — опциональная привязка заявки к машине
    # клиента. Защита от IDOR: в БД машина должна существовать И
    # принадлежать этому же ``telegram_id``. При несовпадении — 403,
    # НЕ 404 (не подсвечиваем чужие ID для энумерации).
    vehicle_id: int | None = None
    if body.car_id is not None:
        v = await VehicleRepository(session).get(body.car_id)
        if v is None or v.user_id != user.id:
            raise HTTPException(
                status_code=403,
                detail="car not owned by this client",
            )
        vehicle_id = v.id

    repo = UserRepository(session)
    req = await repo.create_service_request(
        telegram_id=user.id,
        phone=phone,
        description=msg,
        dropoff_window=body.dropoff_window,
        vehicle_id=vehicle_id,
    )

    # Если клиент указал новый телефон — обновляем профиль, чтобы
    # следующая заявка сразу подставила актуальный номер.
    if body.phone and profile.phone != phone:
        await session.execute(
            update(UserRow)
            .where(UserRow.telegram_id == user.id)
            .values(phone=phone)
        )

    await session.commit()
    logger.info(
        "client_request created id=%s by user_id=%s (window=%r, vehicle=%s)",
        req.id, user.id, body.dropoff_window, vehicle_id,
    )
    return {"ok": True, "request_id": req.id, "vehicle_id": vehicle_id}


# Phase 20.1: Contact-first booking flow.
# Клиент в мини-аппе жмёт «🗓 Записаться на сервис», у него ещё нет
# подтверждённого phone в профиле → фронт дёргает этот эндпоинт,
# бот присылает клиенту reply-keyboard с «📱 Поделиться контактом»,
# мини-апп закрывается (``tg.close()``). После того, как клиент
# делится контактом, aiogram-хэндлер ``confirm_contact_share``
# обновляет ``users.phone`` и рапортует об успехе. Следующее
# открытие мини-аппа отдаст phone в ``/api/me`` и покажет booking-
# sheet с readonly-номером.
#
# В отличие от legacy ``/api/request_call`` — FSM-состояние НЕ
# выставляется, новый flow чище: один шаг = один exchange.
@app.post("/api/client/request-contact")
@limiter.limit("10/minute", key_func=_user_or_ip_key)
async def api_client_request_contact(
    request: Request,
    response: Response,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    profile = await UserRepository(session).get(user.id)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    bot = get_bot()
    try:
        await bot.send_message(
            user.id,
            "Чтобы записаться на сервис, подтвердите номер — нажмите "
            "кнопку ниже. После подтверждения вернитесь в мини-апп.",
            reply_markup=share_contact_kb(),
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.warning(
            "request_contact: send_message failed for %s: %s", user.id, exc
        )
        raise HTTPException(status_code=502, detail="cannot reach the chat")
    logger.info("phase20.1 contact prompt sent to user_id=%s", user.id)
    return {"ok": True}


@app.post("/api/ai/ask")
@limiter.limit("5/minute", key_func=_user_or_ip_key)
async def api_ai_ask(
    request: Request,
    response: Response,
    body: AskRequest,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if not ai_master.is_available():
        raise HTTPException(status_code=503, detail="AI not configured")
    profile = await _fetch_profile(session, user.id)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    # Контекст: профиль + последние 12 ТО (хватит чтобы покрыть несколько
    # машин по 3-4 записи на каждую) + полный список машин (для группировки
    # истории и вопроса «о какой машине речь?»).
    history = await _fetch_history(session, user.id, 12)
    vehicles = await VehicleRepository(session).list_for_user(user.id)
    vehicles_payload = [_vehicle_to_dict(v) for v in vehicles]
    chat_history = [
        {"role": t.role, "content": t.content} for t in body.history
    ]
    try:
        answer = await ai_master.ask_master(
            question=body.question,
            profile=profile,
            history=history,
            vehicles=vehicles_payload,
            chat_history=chat_history,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ai_master.QuotaExceededError as exc:
        # Все модели Gemini упёрлись в дневную/минутную квоту free-tier.
        # Отдаём 429 с понятным текстом — фронт показывает его в bubble.
        logger.warning("AI quota exceeded across all fallbacks: %s", exc)
        raise HTTPException(
            status_code=429,
            detail=(
                "Виртуальный мастер сегодня ответил слишком многим клиентам "
                "и упёрся в дневной лимит. Попробуйте задать вопрос завтра "
                "или нажмите «Записаться на сервис» — мастер на связи."
            ),
        )
    except Exception as exc:  # pragma: no cover — runtime LLM errors
        logger.exception("AI ask failed: %s", exc)
        raise HTTPException(status_code=502, detail="AI upstream error")
    return {"answer": answer, "model": ai_master.MODEL_NAME}


# ---------------------------------------------------------------------------
# Phase 9: AI Health Score.
# - GET /api/vehicles/{id}/health           — отчёт (кэш 7 дней)
# - POST /api/vehicles/{id}/health/feedback — пометить риск как
#   resolved/hidden и инвалидировать кэш отчёта.
# ---------------------------------------------------------------------------


def _vehicle_to_ai_input(v: Any) -> dict[str, Any]:
    """Сжатый dict ТТХ для generate_health_score (только то, что нужно
    модели — без timestamps, флагов primary и т.п.)."""
    return {
        "id": v.id,
        "brand": v.brand,
        "model": v.model,
        "year": v.year,
        "engine_volume": v.engine_volume,
        "fuel_type": v.fuel_type,
        "transmission": v.transmission,
        "current_mileage": v.current_mileage,
        "avg_yearly_mileage": v.avg_yearly_mileage,
    }


async def _build_health_payload(
    health_repo: HealthRepository,
    vehicle: Any,
) -> dict[str, Any]:
    """Получить отчёт из кэша или сгенерировать новый.

    Кэш-флаг (HIT/MISS) НЕ возвращается клиенту в JSON, чтобы не
    светить внутреннее поведение — только в логах. Для отладки можно
    дёрнуть с заголовком, который API устанавливает (см. ниже).
    """
    cached = await health_repo.get_latest_report(vehicle.id)
    if cached is not None:
        risks = HealthRepository.parse_risks(cached)
        logger.info(
            "Health cache HIT vehicle_id=%s report_id=%s age=%s",
            vehicle.id, cached.id, cached.created_at,
        )
        return {
            "score": cached.score,
            "risks": risks,
            "generated_at": localize_naive_sql(cached.created_at),
            "_cache": "hit",
        }

    feedback_rows = await health_repo.list_active_feedback(vehicle.id)
    excl_titles = [f.risk_title for f in feedback_rows]
    logger.info(
        "Health cache MISS vehicle_id=%s exclusions=%d",
        vehicle.id, len(excl_titles),
    )
    result = await ai_master.generate_health_score(
        vehicle=_vehicle_to_ai_input(vehicle),
        feedback_list=excl_titles,
    )
    saved = await health_repo.save_report(
        vehicle_id=vehicle.id,
        score=result["score"],
        risks=result["risks"],
    )
    return {
        "score": saved.score,
        "risks": result["risks"],
        "generated_at": localize_naive_sql(saved.created_at),
        "_cache": "miss",
    }


@app.get("/api/vehicles/{vehicle_id}/health")
@limiter.limit("20/minute", key_func=_user_or_ip_key)
async def api_vehicle_health(
    request: Request,
    response: Response,
    vehicle_id: int,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if not ai_master.is_available():
        raise HTTPException(status_code=503, detail="AI not configured")
    vehicle = await VehicleRepository(session).get(vehicle_id)
    if vehicle is None or vehicle.user_id != user.id:
        # Owner-check + защита от IDOR. Возвращаем 404 а не 403, чтобы
        # не светить факт существования чужой машины.
        raise HTTPException(status_code=404, detail="vehicle not found")
    health_repo = HealthRepository(session)
    try:
        payload = await _build_health_payload(health_repo, vehicle)
    except ai_master.QuotaExceededError as exc:
        logger.warning("Health quota exceeded across all fallbacks: %s", exc)
        raise HTTPException(
            status_code=429,
            detail=(
                "ИИ-диагностика временно недоступна — упёрлись в дневной "
                "лимит. Попробуйте завтра."
            ),
        )
    except Exception as exc:  # pragma: no cover — runtime LLM errors
        logger.exception("Health generation failed: %s", exc)
        raise HTTPException(status_code=502, detail="AI upstream error")
    cache_state = payload.pop("_cache", "miss")
    response.headers["X-Cache"] = cache_state.upper()
    payload["vehicle_id"] = vehicle.id
    return payload


class HealthFeedbackRequest(BaseModel):
    """Тело POST /health/feedback. ``risk_title`` обязателен и
    подрезается до 200 символов; статус — фиксированный enum."""

    risk_title: str = Field(min_length=1, max_length=200)
    status: Literal["resolved", "hidden"]


@app.post("/api/vehicles/{vehicle_id}/health/feedback")
@limiter.limit("20/minute", key_func=_user_or_ip_key)
async def api_vehicle_health_feedback(
    request: Request,
    response: Response,
    vehicle_id: int,
    body: HealthFeedbackRequest,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    vehicle = await VehicleRepository(session).get(vehicle_id)
    if vehicle is None or vehicle.user_id != user.id:
        raise HTTPException(status_code=404, detail="vehicle not found")
    if body.status not in HEALTH_FEEDBACK_STATUSES:
        # На уровне Pydantic уже отсеяно, но дублируем — на случай если
        # кто-то расширит список и забудет про эндпоинт.
        raise HTTPException(status_code=400, detail="bad status")
    health_repo = HealthRepository(session)
    row = await health_repo.add_feedback(
        vehicle_id=vehicle.id,
        risk_title=body.risk_title,
        status=body.status,
    )
    # Phase 27 · Persistent state: мутируем свежий отчёт (убираем риск +
    # бампим score), а не удаляем кэш целиком. Score и количество рисков
    # сохраняются между перезагрузками. Если кэша нет — следующий GET
    # сгенерирует AI-отчёт и применит feedback-таблицу как exclusions.
    cache_after = await health_repo.apply_resolved_to_cache(
        vehicle.id, body.risk_title,
    )
    await session.commit()
    return {
        "ok": True,
        "feedback_id": row.id,
        "vehicle_id": vehicle.id,
        "status": body.status,
        "score": cache_after["score"] if cache_after else None,
        "risks_count": cache_after["risks_count"] if cache_after else None,
    }


# ---------------------------------------------------------------------------
# Phase 11: in-place mileage update directly from WebApp.
# ---------------------------------------------------------------------------


# Разумный потолок (1 миллион км) — отсекаем явные опечатки/спам, при
# этом не упираемся ни в один реальный авто. Минимум 0 — машина может
# быть только что куплена «с нуля» (теоретически).
_MILEAGE_MIN: int = 0
_MILEAGE_MAX: int = 1_000_000


class VehicleMileageRequest(BaseModel):
    """Тело POST /api/vehicles/{id}/mileage.

    Принимаем целое число километров. Pydantic сразу обрезает явный
    мусор (строки/None/отрицательные/слишком большие), бизнес-валидация
    (монотонность пробега) — в обработчике.
    """

    new_mileage: int = Field(ge=_MILEAGE_MIN, le=_MILEAGE_MAX)


@app.post("/api/vehicles/{vehicle_id}/mileage")
@limiter.limit("30/minute", key_func=_user_or_ip_key)
async def api_vehicle_mileage_update(
    request: Request,
    response: Response,
    vehicle_id: int,
    body: VehicleMileageRequest,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Обновить ``current_mileage`` машины из WebApp.

    Бизнес-правила:
    1. Owner-check: чужие машины → 404 (не светим IDOR).
    2. Пробег только растёт. Если ``new_mileage < current_mileage`` —
       400, чтобы клиент случайно не «откатил» одометр и не сломал
       прогноз ТО / health-score.
    3. После записи возвращаем свежие ``current_mileage`` и ``mileage_updated_at``,
       чтобы фронт мог мгновенно перерисовать карточку без reload.
    """
    vehicle_repo = VehicleRepository(session)
    vehicle = await vehicle_repo.get(vehicle_id)
    if vehicle is None or vehicle.user_id != user.id:
        raise HTTPException(status_code=404, detail="vehicle not found")

    new_mileage = int(body.new_mileage)
    current = int(vehicle.current_mileage or 0)
    if new_mileage < current:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Новый пробег ({new_mileage} км) меньше текущего "
                f"({current} км). Пробег не может уменьшаться."
            ),
        )

    # update_current_mileage сама проставляет mileage_updated_at = now UTC.
    await vehicle_repo.update_current_mileage(
        vehicle_id=vehicle.id, mileage=new_mileage
    )
    await session.commit()

    # Перечитываем строку, чтобы вернуть точно то, что в БД (включая
    # обновлённый timestamp, без подгона на стороне клиента).
    fresh = await vehicle_repo.get(vehicle.id)
    return {
        "ok": True,
        "vehicle_id": vehicle.id,
        "current_mileage": int(fresh.current_mileage or 0) if fresh else new_mileage,
        "mileage_updated_at": localize_naive_sql(
            fresh.mileage_updated_at if fresh else None
        ),
    }


# ---------------------------------------------------------------------------
# Phase 12: Admin CRM — global live search.
# ---------------------------------------------------------------------------


# Жёсткий потолок выдачи. 15 — компромисс между «достаточно чтобы
# увидеть похожих клиентов» и «не отрисовывать больше дюжины карточек
# в дропдауне». БД защищена даже если запрос пустой — ниже мы сначала
# валидируем длину query.
_ADMIN_SEARCH_LIMIT: int = 15


async def _require_admin(
    user: TelegramUser, session: AsyncSession
) -> None:
    """Жёсткая защита admin-эндпоинтов.

    Если пользователь не в таблице ``admins`` — отвечаем 403 (а не 404,
    как в owner-check для машин: для admin-роутов сам факт «такая
    точка существует» не секретен — секретны данные за ней).
    """
    if not await AdminRepository(session).is_admin(user.id):
        raise HTTPException(status_code=403, detail="admin only")


@app.get("/api/admin/search")
@limiter.limit("60/minute", key_func=_user_or_ip_key)
async def api_admin_search(
    request: Request,
    response: Response,
    query: str = "",
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Глобальный live-поиск клиентов и машин для админ-панели.

    Ищем по 4 полям одновременно (LIKE %q% — case-insensitive в SQLite
    через ``COLLATE NOCASE``):

    * ``users.full_name``;
    * наиболее свежий ``service_requests.phone`` клиента;
    * ``vehicles.brand``;
    * ``vehicles.plate_number``.

    Возвращаем агрегированный список: один клиент = одна карточка с
    плашками всех его машин. Лимит 15 уникальных клиентов.
    """
    await _require_admin(user, session)

    q = (query or "").strip()
    if len(q) < 2:
        # 400 а не пустой массив — фронт всё равно отсекает на клиенте,
        # но админский запрос длиной 1 — это спам-цикл от
        # неподвешенного debounce. Лучше явно сказать.
        raise HTTPException(
            status_code=400, detail="query must be at least 2 characters"
        )
    # Защита от ReDoS-style патчей. 64 символа более чем достаточно
    # для номера телефона / госномера / ФИО.
    q = q[:64]

    # Pattern уже опущен в lower() — внутри SQL зовём py_lower(field).
    like_pattern = f"%{q.lower()}%"

    # Один SQL-запрос. Сначала находим уникальные user_id, чьи поля
    # (или поля связанной машины / последней заявки) подходят под
    # шаблон, лимитируем 15. Потом отдельным шагом тянем ВСЕ их
    # машины (не только matched) — UI хочет показать каждой карточке
    # полный гараж клиента.
    from sqlalchemy import text  # локальный импорт, чтобы не утяжелять модуль

    # py_lower зарегистрирован в database/session.py — Unicode-aware
    # str.lower(), без него кириллица в LIKE не сравнивается case-insensitive.
    matched_sql = text(
        """
        SELECT DISTINCT u.telegram_id
        FROM users u
        LEFT JOIN vehicles v ON v.user_id = u.telegram_id
        LEFT JOIN service_requests sr ON sr.telegram_id = u.telegram_id
        WHERE
            py_lower(u.full_name) LIKE :pattern
            OR (u.phone IS NOT NULL AND py_lower(u.phone) LIKE :pattern)
            OR (sr.phone IS NOT NULL AND py_lower(sr.phone) LIKE :pattern)
            OR (v.brand IS NOT NULL AND py_lower(v.brand) LIKE :pattern)
            OR (v.plate_number IS NOT NULL AND py_lower(v.plate_number) LIKE :pattern)
        ORDER BY py_lower(u.full_name) ASC
        LIMIT :limit
        """
    )
    matched_rows = await session.execute(
        matched_sql, {"pattern": like_pattern, "limit": _ADMIN_SEARCH_LIMIT}
    )
    matched_ids: list[int] = [int(r[0]) for r in matched_rows]
    if not matched_ids:
        return {"items": [], "query": q}

    # Тянем профили клиентов + последний phone + все vehicles одной
    # пачкой. Используем UNION-style подзапрос — но проще сделать
    # два SELECT'a, чтобы не тащить в SELECT все колонки клиента
    # на каждый ряд vehicle (типичный N+1 артефакт one-shot).
    placeholders = ", ".join(f":id{i}" for i in range(len(matched_ids)))
    params: dict[str, Any] = {f"id{i}": tid for i, tid in enumerate(matched_ids)}

    users_sql = text(
        f"""
        SELECT
            u.telegram_id AS user_id,
            u.full_name AS name,
            COALESCE(
                u.phone,
                (
                    SELECT sr.phone FROM service_requests sr
                    WHERE sr.telegram_id = u.telegram_id
                    ORDER BY sr.created_at DESC
                    LIMIT 1
                )
            ) AS phone,
            u.username AS username
        FROM users u
        WHERE u.telegram_id IN ({placeholders})
        ORDER BY py_lower(u.full_name) ASC
        """
    )
    vehicles_sql = text(
        f"""
        SELECT id, user_id, brand, plate_number
        FROM vehicles
        WHERE user_id IN ({placeholders})
        ORDER BY user_id, COALESCE(is_primary, 0) DESC, id ASC
        """
    )

    users_rows = (await session.execute(users_sql, params)).mappings().all()
    vehicles_rows = (await session.execute(vehicles_sql, params)).mappings().all()

    by_user: dict[int, list[dict[str, Any]]] = {}
    for vr in vehicles_rows:
        by_user.setdefault(int(vr["user_id"]), []).append(
            {
                "vehicle_id": int(vr["id"]),
                "brand": vr["brand"],
                "plate_number": vr["plate_number"],
            }
        )

    items: list[dict[str, Any]] = []
    for ur in users_rows:
        uid = int(ur["user_id"])
        items.append(
            {
                "user_id": uid,
                "name": ur["name"],
                "phone": ur["phone"],
                "username": ur["username"],
                "vehicles": by_user.get(uid, []),
            }
        )

    return {"items": items, "query": q}


# ---------------------------------------------------------------------------
# Phase 25: Admin CRM — global client directory.
# Полный список клиентов (без поиска), отсортированный по имени.
# Возвращает ту же форму, что и `/api/admin/search`, чтобы фронт мог
# реюзать `_renderAdminClientCard()`.
# ---------------------------------------------------------------------------


@app.get("/api/admin/clients")
@limiter.limit("30/minute", key_func=_user_or_ip_key)
async def api_admin_list_clients(
    request: Request,
    response: Response,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Полный directory всех клиентов CRM, отсортированный по имени.

    Используется bottom-sheet'ом «Все клиенты» в admin-режиме.
    Бэкенд-guard `_require_admin` блокирует не-админов независимо от
    фронтового флага.
    """
    await _require_admin(user, session)

    from sqlalchemy import text  # локальный импорт

    # Один SQL: тянем всех клиентов + последний phone (COALESCE из users
    # и из service_requests). Отдельным запросом — все vehicles, чтобы
    # не множить ряды через JOIN.
    users_sql = text(
        """
        SELECT
            u.telegram_id AS user_id,
            u.full_name AS name,
            COALESCE(
                u.phone,
                (
                    SELECT sr.phone FROM service_requests sr
                    WHERE sr.telegram_id = u.telegram_id
                    ORDER BY sr.created_at DESC
                    LIMIT 1
                )
            ) AS phone,
            u.username AS username
        FROM users u
        ORDER BY py_lower(COALESCE(u.full_name, '')) ASC, u.telegram_id ASC
        """
    )
    vehicles_sql = text(
        """
        SELECT id, user_id, brand, plate_number
        FROM vehicles
        ORDER BY user_id, COALESCE(is_primary, 0) DESC, id ASC
        """
    )

    users_rows = (await session.execute(users_sql)).mappings().all()
    vehicles_rows = (await session.execute(vehicles_sql)).mappings().all()

    by_user: dict[int, list[dict[str, Any]]] = {}
    for vr in vehicles_rows:
        by_user.setdefault(int(vr["user_id"]), []).append(
            {
                "vehicle_id": int(vr["id"]),
                "brand": vr["brand"],
                "plate_number": vr["plate_number"],
            }
        )

    items: list[dict[str, Any]] = []
    for ur in users_rows:
        uid = int(ur["user_id"])
        items.append(
            {
                "user_id": uid,
                "name": ur["name"],
                "phone": ur["phone"],
                "username": ur["username"],
                "vehicles": by_user.get(uid, []),
            }
        )

    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# Phase 13: Admin CRM — quick service-record entry.
# ---------------------------------------------------------------------------


# Стандартный набор категорий, который рендерит фронт. Бэкенд не
# жёстко ограничивает значение — просто контракт с UI (валидируем
# членство, чтобы случайно не записать мусорный enum).
_ADMIN_SERVICE_CATEGORIES: tuple[str, ...] = (
    "Плановое ТО",
    "Подвеска",
    "Тормоза",
    "Двигатель",
    "Другое",
)


class AdminServiceRecordRequest(BaseModel):
    """Тело POST /api/admin/service-record. Минимальная Compact-форма
    для приёмщика: одна машина, один пробег, одна категория, текст работ.

    ``next_mileage`` опционален — если приёмщик не задал регламент, мы
    его не выставляем (поле next_service_mileage остаётся NULL).
    """

    vehicle_id: int = Field(ge=1)
    current_mileage: int = Field(ge=0, le=10_000_000)
    next_mileage: int | None = Field(default=None, ge=0, le=10_000_000)
    category: str = Field(min_length=1, max_length=64)
    works_text: str = Field(min_length=1, max_length=2000)
    # Phase 35 · опциональный URL-путь к загруженному фото
    # (POST /api/admin/maintenance/upload вернул его). Сервер дополнительно
    # валидирует, что путь начинается с ``/uploads/maintenance/`` —
    # никаких подмен на чужие статические ресурсы.
    image_path: str | None = Field(default=None, max_length=512)


@app.post("/api/admin/service-record")
@limiter.limit("30/minute", key_func=_user_or_ip_key)
async def api_admin_create_service_record(
    request: Request,
    response: Response,
    payload: AdminServiceRecordRequest,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Создать запись о выполненном ТО. Только для админов.

    Бизнес-логика (одна транзакция):
    1. ``users_history`` — добавляем строку: ``service_type`` =
       ``"{category}: {works_text}"`` (читаемый формат для существующего
       UI истории), ``current_mileage``, ``next_service_mileage``,
       ``execution_date`` — текущая дата UTC.
    2. ``vehicles.current_mileage`` — обновляем (с bump
       ``mileage_updated_at``), но ТОЛЬКО если новое значение >=
       текущего. Откатывать пробег нельзя — это нарушает физику
       одометра. Если меньше — игнорируем (запись истории всё равно
       создаётся со своим current_mileage; vehicles остаётся прежним).

    CRITICAL: никаких Telegram-уведомлений клиенту. Это явное
    требование Phase 13 — приёмщик заполняет форму молча, без
    автоматических пушей.
    """
    await _require_admin(user, session)

    if payload.category not in _ADMIN_SERVICE_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"category must be one of {list(_ADMIN_SERVICE_CATEGORIES)}",
        )

    works_text = payload.works_text.strip()
    if not works_text:
        raise HTTPException(status_code=400, detail="works_text is empty")

    # Sanity: next_mileage если задан — должен быть строго больше
    # current_mileage. Иначе это бессмысленный регламент.
    if payload.next_mileage is not None and payload.next_mileage <= payload.current_mileage:
        raise HTTPException(
            status_code=400,
            detail="next_mileage must be greater than current_mileage",
        )

    vehicle_repo = VehicleRepository(session)
    vehicle = await vehicle_repo.get(payload.vehicle_id)
    if vehicle is None:
        raise HTTPException(status_code=404, detail="vehicle not found")

    # Формируем service_type как «{category}: {works_text}». В этом
    # проекте users_history.service_type — единственное текстовое поле
    # описания, и оно уже используется существующим UI истории.
    # Это даёт админу осмысленный заголовок «Тормоза: замена колодок,
    # прокачка тормозной системы» вместо двух разрозненных полей.
    service_type = f"{payload.category}: {works_text}"

    # Phase 22.1: «сегодня» — в минской tz (иначе в 00:00-03:00 по Минску
    # мастер получит вчерашнюю дату визита, т.к. UTC ещё отстаёт).
    from config import now_local
    today = now_local().strftime("%Y-%m-%d")

    # Phase 35 · валидация image_path. Принимаем только пути, выданные
    # нашим upload-endpoint'ом — остальное игнорируем (NULL в БД).
    image_path: str | None = None
    if payload.image_path:
        candidate = payload.image_path.strip()
        if candidate.startswith("/uploads/maintenance/") and len(candidate) <= 512:
            image_path = candidate

    record_id = await RecordRepository(session).add(
        user_id=int(vehicle.user_id),
        vehicle_id=vehicle.id,
        service_type=service_type,
        execution_date=today,
        next_visit_date=None,
        current_mileage=payload.current_mileage,
        next_service_mileage=payload.next_mileage,
        photo_file_id=None,
        image_path=image_path,
    )

    await vehicle_repo.update_current_mileage(
        payload.vehicle_id, payload.current_mileage
    )

    await session.commit()

    return {
        "ok": True,
        "record_id": record_id,
        "vehicle_id": vehicle.id,
        "current_mileage": payload.current_mileage,
        "next_service_mileage": payload.next_mileage,
        "category": payload.category,
        "execution_date": today,
        "image_path": image_path,
    }


# ---------------------------------------------------------------------------
# Phase 35 · POST /api/admin/maintenance/upload — multipart-аплоад фото
# для записи ТО. Возвращает абсолютный URL-путь, который потом
# отправляется в /api/admin/service-record body.image_path.
#
# Защита:
#   * только админ;
#   * максимум 10 МБ (см. MAINTENANCE_MAX_BYTES);
#   * только image/* + whitelist по расширению;
#   * имя файла генерируем сами (uuid4) — пользовательское имя
#     никогда не попадает в файловую систему;
#   * каталог: /uploads/maintenance/<uuid>/<random>.<ext>;
#   * путь возвращается как URL-путь от корня (mount /uploads).
# ---------------------------------------------------------------------------


@app.post("/api/admin/maintenance/upload")
@limiter.limit("30/minute", key_func=_user_or_ip_key)
async def api_admin_maintenance_upload(
    request: Request,
    response: Response,
    image: UploadFile = File(..., description="Фото записи ТО"),
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _require_admin(user, session)

    content_type = (image.content_type or "").lower()
    # Phase 36 · принимаем image/* и video/*. Старые клиенты могут
    # прислать пустой content_type — тогда полагаемся на расширение.
    if content_type and not (
        content_type.startswith("image/") or content_type.startswith("video/")
    ):
        raise HTTPException(status_code=415, detail="image/* or video/* expected")

    # Расширение берём из имени файла (если есть), иначе угадываем по MIME.
    raw_name = image.filename or ""
    ext = ""
    if "." in raw_name:
        ext = raw_name.rsplit(".", 1)[-1].lower()
    if ext not in MAINTENANCE_ALLOWED_EXT:
        ext = MAINTENANCE_MIME_TO_EXT.get(content_type, "")
    if ext not in MAINTENANCE_ALLOWED_EXT:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported extension; allowed: {sorted(MAINTENANCE_ALLOWED_EXT)}",
        )

    # Читаем стримом, ограничивая размер.
    raw = await image.read(MAINTENANCE_MAX_BYTES + 1)
    if len(raw) > MAINTENANCE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="file too large (>50MB)")
    if not raw:
        raise HTTPException(status_code=400, detail="empty file")

    record_uuid = uuid.uuid4().hex
    target_dir = MAINTENANCE_UPLOADS_DIR / record_uuid
    target_dir.mkdir(parents=True, exist_ok=True)
    file_name = f"{uuid.uuid4().hex}.{ext}"
    target_path = target_dir / file_name
    try:
        target_path.write_bytes(raw)
    except OSError as exc:
        logger.exception("[Maintenance] не удалось сохранить аплоад: %s", exc)
        raise HTTPException(status_code=500, detail="failed to persist file") from exc

    url_path = f"/uploads/maintenance/{record_uuid}/{file_name}"
    # Phase 36 · вычисляем media_kind для UI: фронт по нему решает,
    # рендерить <img> или <video>. Опираемся сначала на ext (наш
    # whitelist), затем на content_type.
    if ext in {"mp4", "mov", "m4v", "webm", "mkv", "3gp"}:
        media_kind = "video"
    elif content_type.startswith("video/"):
        media_kind = "video"
    else:
        media_kind = "image"
    logger.info(
        "[Maintenance] upload ok user=%s bytes=%d kind=%s path=%s",
        user.id, len(raw), media_kind, url_path,
    )
    return {
        "ok": True,
        "image_path": url_path,
        "size": len(raw),
        "content_type": content_type,
        "media_kind": media_kind,
    }


# ---------------------------------------------------------------------------
# Phase 14: Admin CRM — Active Requests Dashboard.
# ---------------------------------------------------------------------------

# Какие статусы service_requests считаем «живыми» с точки зрения
# CRM-дашборда (не ушли в архив). Phase 20: 4 из 5 enum-значений
# видны в admin-UI — ``completed`` уезжает из списка.
_ADMIN_REQUEST_ACTIVE_STATUSES: tuple[str, ...] = (
    "pending_dropoff", "in_yard", "in_progress", "ready",
)

# Жёсткий потолок выдачи. 200 — на порядок больше, чем приёмщик может
# реалистично иметь в работе одновременно; служит защитой от
# бесконтрольного роста payload'а, а не нормальным рабочим лимитом.
_ADMIN_REQUESTS_LIMIT: int = 200


@app.get("/api/admin/requests")
@limiter.limit("60/minute", key_func=_user_or_ip_key)
async def api_admin_list_requests(
    request: Request,
    response: Response,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Список активных заявок на звонок для admin-dashboard.

    Один SQL-запрос: ``service_requests`` JOIN ``users`` (имя,
    @username) LEFT JOIN ``vehicles`` (приоритетная машина клиента —
    ``is_primary=1``, иначе минимальный ``id``). Сортировка
    ``sr.id DESC`` — id монотонно растёт, что эквивалентно «от новых
    к старым» по ``created_at``, но дешевле (PK-индекс vs текстовая
    дата).

    У клиента может вообще не быть машин (типичный сценарий — заявку
    оформил админ за клиента, vehicles ещё не заведены) — тогда поле
    ``vehicle`` приходит ``null``, фронт рисует «автомобиль не указан».
    """
    await _require_admin(user, session)

    from sqlalchemy import text  # локальный импорт, как и в admin/search

    placeholders = ", ".join(
        f":s{i}" for i in range(len(_ADMIN_REQUEST_ACTIVE_STATUSES))
    )
    params: dict[str, Any] = {
        f"s{i}": st for i, st in enumerate(_ADMIN_REQUEST_ACTIVE_STATUSES)
    }
    params["limit"] = _ADMIN_REQUESTS_LIMIT

    # Phase 22: если у заявки уже проставлен sr.vehicle_id (фронт
    # передал car_id при создании), рисуем именно ту машину; иначе
    # fallback на «primary / первую» машину клиента — чтобы legacy-
    # заявки (до Phase 22) всё равно показывались с марками в CRM.
    sql = text(
        f"""
        SELECT
            sr.id              AS request_id,
            sr.telegram_id     AS user_id,
            sr.phone           AS phone,
            sr.description     AS description,
            sr.status          AS status,
            sr.created_at      AS created_at,
            sr.dropoff_window  AS dropoff_window,
            sr.vehicle_id      AS vehicle_id,
            sr.is_viewed       AS is_viewed,
            sr.is_confirmed    AS is_confirmed,
            sr.resolved        AS resolved,
            sr.in_yard_at      AS in_yard_at,
            sr.master_comment  AS master_comment,
            u.full_name        AS name,
            u.username         AS username,
            v.id               AS v_id,
            v.brand            AS brand,
            v.model            AS model,
            v.plate_number     AS plate_number,
            v.current_mileage  AS v_current_mileage
        FROM service_requests sr
        JOIN users u ON u.telegram_id = sr.telegram_id
        LEFT JOIN vehicles v ON v.id = COALESCE(
            sr.vehicle_id,
            (
                SELECT id FROM vehicles
                WHERE user_id = sr.telegram_id
                ORDER BY COALESCE(is_primary, 0) DESC, id ASC
                LIMIT 1
            )
        )
        WHERE sr.status IN ({placeholders})
        ORDER BY sr.id DESC
        LIMIT :limit
        """
    )
    rows = (await session.execute(sql, params)).mappings().all()

    items: list[dict[str, Any]] = []
    for r in rows:
        description = (r["description"] or "").strip() or None
        brand = r["brand"]
        model = r["model"]
        plate = r["plate_number"]
        # Phase 22: bound=True, если vehicle_id записан в заявке (а не
        # подтянут fallback'ом); фронт подсвечивает «точную» привязку
        # другим оттенком, чем «primary по умолчанию».
        vehicle_id = r["vehicle_id"]
        items.append(
            {
                "request_id": int(r["request_id"]),
                "user_id": int(r["user_id"]),
                "name": r["name"] or "Без имени",
                "username": r["username"],
                "phone": r["phone"] or "",
                "description": description,
                "status": r["status"],
                "dropoff_window": r["dropoff_window"],
                # Phase 22.1: локализуем naive-метку из SQLite под
                # Europe/Minsk, чтобы CRM-дэшборд не сдвигал время
                # на 3 часа.
                "created_at": localize_naive_sql(r["created_at"]),
                "vehicle_id": (
                    int(vehicle_id) if vehicle_id is not None else None
                ),
                "vehicle": (
                    {
                        "id": int(r["v_id"]) if r["v_id"] is not None else None,
                        "brand": brand,
                        "model": model,
                        "plate_number": plate,
                        "current_mileage": r["v_current_mileage"],
                        "bound": vehicle_id is not None,
                    }
                    if (brand or plate)
                    else None
                ),
                # Phase 23: триаж-флаги. Фронт-CRM по ним раскладывает
                # pending-заявки на 3 bucket'а (Новые / Просмотрено /
                # Подтверждено). resolved=True прячет из воронки.
                "is_viewed": bool(r["is_viewed"]),
                "is_confirmed": bool(r["is_confirmed"]),
                "resolved": bool(r["resolved"]),
                # Phase 23.1: момент приёмки на стоянку (TZ-aware ISO +03:00
                # или None). yard-modal рендерит его вместо dropoff_window
                # на статусах in_yard | in_progress | ready | completed.
                "in_yard_at": localize_naive_sql(r["in_yard_at"]),
                # Phase 31: внутренний комментарий мастера. Никогда не
                # показывается клиенту — только admin-surface.
                "master_comment": r["master_comment"],
            }
        )

    return {"items": items, "count": len(items)}


# Phase 20.2 · Notification Trigger
# ---------------------------------------------------------------------------
# Карта «новый статус/экшен → текст клиенту». Пустое значение == не
# слать ничего (обратные переходы, reverse ops, итд). Тексты зашиты
# на бэке — фронт не должен их дублировать, а бот не инжектит
# форматирование (HTML-parser выключен — только plain).
_STATUS_NOTIFICATION: dict[str, str] = {
    "confirm": "✅ Ваша заявка подтверждена. Ждем вас на сервисе!",
    "in_yard": "🅿️ Ваш автомобиль принят на стоянку СТО.",
    "in_progress": "🔧 Автомобиль загнан в бокс, мастер приступил к работе.",
    "ready": "🟢 Ремонт окончен! Ваш автомобиль готов к выдаче.",
}


async def _notify_client_status_change(
    *, chat_id: int, event: str, request_id: int
) -> None:
    """Отправить клиенту уведомление о смене статуса заявки.

    ``event`` — ключ из :data:`_STATUS_NOTIFICATION` (``"confirm"``
    для legacy-resolve-флоу «Обработано», либо новый статус после
    PATCH: ``in_yard | in_progress | ready``). Переходы, которых
    нет в карте (``completed``, ``pending_dropoff``) — молчим
    осознанно: ``completed`` = клиент физически на СТО, избыточный
    спам; ``pending_dropoff`` — реверс, не должно случаться в UI.

    CRITICAL (§ safety): любая ошибка Telegram (бот заблокирован,
    чат не найден, network-хиккап) не должна повлиять на транзакцию.
    Мы уже сделали ``session.commit()`` до этого вызова — статус
    в CRM уже зафиксирован. Исключения глотаются и логируются,
    ``_notify_client_status_change`` никогда не поднимает наружу.
    """
    text = _STATUS_NOTIFICATION.get(event)
    if not text:
        return
    try:
        bot = get_bot()
        await bot.send_message(chat_id, text)
        logger.info(
            "phase20.2 notify OK: request=%s chat=%s event=%s",
            request_id, chat_id, event,
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        # Типовые «клиент заблокировал бота / чат удалён» —
        # это нормально, не поднимаем тревогу.
        logger.info(
            "phase20.2 notify skipped (client unreachable): "
            "request=%s chat=%s event=%s err=%r",
            request_id, chat_id, event, str(exc),
        )
    except Exception as exc:  # noqa: BLE001 — фаза Safety, глотаем всё
        # Любая другая ошибка (network, rate-limit, etc.) не должна
        # валить CRM-транзакцию: статус уже в БД, клиент просто
        # не получит пуш. Админ перешлёт вручную при необходимости.
        logger.warning(
            "phase20.2 notify failed (unexpected): "
            "request=%s chat=%s event=%s err=%r",
            request_id, chat_id, event, exc,
        )


# Phase 20: PATCH /api/admin/requests/{id}/status — переключение
# статуса яр-карты. Один endpoint, один payload с Literal-валидацией.
@app.patch("/api/admin/requests/{request_id}/status")
@limiter.limit("60/minute", key_func=_user_or_ip_key)
async def api_admin_patch_request_status(
    request: Request,
    response: Response,
    request_id: int,
    body: RequestStatusPayload,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Смена статуса заявки (yard-management).

    Допустимые переходы задаются клиентом через Quick Actions
    modal — UI жёстко следит за последовательностью
    ``pending_dropoff → in_yard → in_progress → ready → completed``.
    Бэкенд принимает любой валидный статус: не блокируем «прыжки»
    (например, «отменить и сразу в completed», если клиент отказался),
    это было бы лишнее ограничение.

    Phase 20.2: § 4.3 silent-правило отменено. На переходах
    ``in_yard | in_progress | ready`` клиент получает push в
    Telegram (тексты в :data:`_STATUS_NOTIFICATION`). Ошибка
    отправки НЕ приводит к откату — CRM-транзакция уже в БД.

    Race-protection: если заявки нет — 404; если статус уже такой
    же — 200 с idempotent=true (удобно для retry клиента).
    """
    await _require_admin(user, session)

    if request_id < 1:
        raise HTTPException(status_code=400, detail="invalid request_id")

    repo = UserRepository(session)
    req = await repo.get_service_request(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="request not found")

    # Phase 23.2 · helper: raw SELECT актуальных (in_yard_at, resolved)
    # для отдачи фронту. Используем text() вместо ORM attribute-доступа,
    # чтобы избежать async lazy-load на expired-инстансе (между двумя
    # PATCH-вызовами в одной сессии identity-map кэширует expired
    # копию, которая на .attr-доступ пытается сделать sync-query).
    from sqlalchemy import text as _sql_text

    async def _fetch_triage_fields() -> tuple[str | None, bool]:
        r = (
            await session.execute(
                _sql_text(
                    "SELECT in_yard_at, resolved FROM service_requests "
                    "WHERE id = :id"
                ),
                {"id": request_id},
            )
        ).one_or_none()
        if r is None:
            return None, False
        return localize_naive_sql(r[0]), bool(r[1])

    if req.status == body.status:
        # Идемпотентный happy-path — фронту не нужно различать это
        # от нормального успеха, просто флаг ``idempotent`` для логов.
        # Уведомление тоже НЕ шлём повторно — защита от спама.
        # Phase 23.2: даже для идемпотентного пути возвращаем in_yard_at /
        # resolved — фронту нужно синхронизировать кэш на случай, если
        # PATCH пришёл после перезагрузки страницы с устаревшим UI.
        in_yard_at_iso, resolved = await _fetch_triage_fields()
        return {
            "ok": True,
            "request_id": request_id,
            "status": body.status,
            "idempotent": True,
            "in_yard_at": in_yard_at_iso,
            "resolved": resolved,
        }

    # Phase 23.2: telegram_id схлопываем ДО commit — после коммита
    # instance будет expired и .attr-доступ триггернёт async-lazy-load.
    client_chat_id = req.telegram_id

    affected = await repo.set_service_request_status(request_id, body.status)
    if affected == 0:
        raise HTTPException(status_code=409, detail="patch conflict")

    await session.commit()

    # Phase 20.2: уведомление клиента — только после успешного коммита
    # и ВНЕ транзакции БД. Ошибки глотаются внутри helper'а.
    await _notify_client_status_change(
        chat_id=client_chat_id,
        event=body.status,
        request_id=request_id,
    )

    in_yard_at_iso, resolved = await _fetch_triage_fields()

    return {
        "ok": True,
        "request_id": request_id,
        "status": body.status,
        "idempotent": False,
        "in_yard_at": in_yard_at_iso,
        "resolved": resolved,
    }


@app.post("/api/admin/requests/{request_id}/resolve")
@limiter.limit("30/minute", key_func=_user_or_ip_key)
async def api_admin_resolve_request(
    request: Request,
    response: Response,
    request_id: int,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Кнопка «✅ Подтвердить» (Phase 23, переименование из «Обработано»).

    Семантика: мастер созвонился с клиентом, заявка подтверждена и
    ожидается физический заезд. Ставим ``is_confirmed=True``; статус
    (``pending_dropoff``) НЕ меняем — заявка всё ещё в воронке
    ожидания до момента приёмки на стоянку. В CRM карточка уезжает
    из bucket'а «Просмотрено» в «Подтверждено».

    URL оставлен ``/resolve`` для обратной совместимости фронта.

    Phase 20.2: клиент получает push «✅ Ваша заявка подтверждена».
    Ошибки Telegram глотаются внутри helper'а.

    Phase 23 idempotency: повторное «Подтвердить» на уже подтверждённой
    заявке — 200 с ``idempotent=true``, без повторной отправки push'а.
    """
    await _require_admin(user, session)

    if request_id < 1:
        raise HTTPException(status_code=400, detail="invalid request_id")

    user_repo = UserRepository(session)
    req = await user_repo.get_service_request(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="request not found")

    # Phase 23: не блокируем на is_confirmed=True (идемпотентно).
    # Но если заявка уже resolved (машина на стоянке / выдана) —
    # confirm не имеет смысла, возвращаем 409, чтобы фронт мог
    # перерисовать stale-карточку.
    if req.resolved:
        raise HTTPException(
            status_code=409,
            detail=f"request already resolved (status={req.status})",
        )

    if req.is_confirmed:
        return {
            "ok": True,
            "request_id": request_id,
            "is_confirmed": True,
            "idempotent": True,
        }

    affected = await user_repo.mark_request_confirmed(request_id)
    if affected == 0:
        raise HTTPException(status_code=409, detail="confirm conflict")

    await session.commit()

    await _notify_client_status_change(
        chat_id=req.telegram_id,
        event="confirm",
        request_id=request_id,
    )

    return {
        "ok": True,
        "request_id": request_id,
        "is_confirmed": True,
        "idempotent": False,
    }


# Phase 23: GET /api/admin/requests/{id} — возвращает детали и
# атомарно ставит ``is_viewed=True`` при первом запросе. Фронт
# дёргает это при открытии модалки карточки; второй и последующие
# GET'ы — no-op на флаге (идемпотентно).
@app.get("/api/admin/requests/{request_id}")
@limiter.limit("60/minute", key_func=_user_or_ip_key)
async def api_admin_get_request(
    request: Request,
    response: Response,
    request_id: int,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Детали заявки для модалки мастера + auto-viewed маркер.

    Если ``is_viewed`` уже True — просто возвращаем данные (push не
    шлём, это внутренняя CRM-операция). Если False — ставим True
    в той же транзакции, что и возвращённый snapshot.
    """
    await _require_admin(user, session)

    if request_id < 1:
        raise HTTPException(status_code=400, detail="invalid request_id")

    user_repo = UserRepository(session)
    req = await user_repo.get_service_request(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="request not found")

    # Атомарно повышаем is_viewed, если не стоял. Commit сразу —
    # это самостоятельная UI-операция, не часть более крупной транзакции.
    viewed_changed = False
    if not req.is_viewed:
        affected = await user_repo.mark_request_viewed(request_id)
        if affected > 0:
            viewed_changed = True
            await session.commit()
            # Обновляем локальный объект, чтобы в response уехал актуальный флаг.
            req = await user_repo.get_service_request(request_id)

    assert req is not None  # mypy guard
    return {
        "request_id": req.id,
        "user_id": req.telegram_id,
        "phone": req.phone,
        "description": (req.description or "").strip() or None,
        "status": req.status,
        "dropoff_window": req.dropoff_window,
        "vehicle_id": req.vehicle_id,
        "is_viewed": bool(req.is_viewed),
        "is_confirmed": bool(req.is_confirmed),
        "resolved": bool(req.resolved),
        "in_yard_at": localize_naive_sql(req.in_yard_at),
        "created_at": localize_naive_sql(req.created_at),
        "viewed_changed": viewed_changed,
        # Phase 31: master_comment — отдаём фронту, чтобы yard-modal
        # сразу рендерил сохранённое значение в textarea.
        "master_comment": req.master_comment,
    }


# Phase 31 · PATCH master_comment — auto-save на blur из admin yard-modal.
# Чисто admin-surface: клиенту значение НЕ показывается ни в каком API
# (см. /api/health, /api/vehicles/* — там его нет). Эндпоинт идемпотентен:
# повторный PATCH с тем же значением просто перезаписывает строку.
@app.patch("/api/admin/requests/{request_id}/master-comment")
@limiter.limit("60/minute", key_func=_user_or_ip_key)
async def api_admin_patch_request_master_comment(
    request: Request,
    response: Response,
    request_id: int,
    body: MasterCommentPayload,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Сохранить внутренний комментарий мастера к заявке.

    Body: ``{"master_comment": str | null}``. Пустая строка / null —
    очистить поле (в БД пишется NULL). Trimming пробелов делает
    репозиторий, длину валидирует Pydantic.
    """
    await _require_admin(user, session)

    if request_id < 1:
        raise HTTPException(status_code=400, detail="invalid request_id")

    repo = UserRepository(session)
    req = await repo.get_service_request(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="request not found")

    affected = await repo.set_master_comment(request_id, body.master_comment)
    if affected == 0:
        # set_master_comment НЕ должен возвращать 0 после успешного
        # get_service_request — это был бы race condition (заявку
        # удалили между двумя запросами). На всякий случай — 409.
        raise HTTPException(status_code=409, detail="patch conflict")

    await session.commit()

    # Возвращаем нормализованное значение из БД, чтобы фронт мог
    # обновить локальный кэш (frontend trims/обрезает на своей стороне,
    # но финальный truth — серверный).
    refreshed = await repo.get_service_request(request_id)
    return {
        "ok": True,
        "request_id": request_id,
        "master_comment": refreshed.master_comment if refreshed else None,
    }


# ---------------------------------------------------------------------------
# Phase 15: Admin CRM — атомарное создание клиента + автомобиля.
# ---------------------------------------------------------------------------


# Допустимые типы топлива — те же значения, что отдаёт UI Phase 4 формы
# регистрации авто. Дублируем здесь, чтобы валидировать payload без
# импорта FUEL_TYPES (который живёт в repositories.py и доступен, но
# мы предпочитаем не таскать константу через слои).
_ADMIN_CREATE_FUEL_TYPES: tuple[str, ...] = (
    "Бензин", "Дизель", "Гибрид", "Электро",
)

# Phase 18: допустимые значения трансмиссии. Первые четыре —
# конкретные типы коробки, последний — явный «не знаю», который
# AI-промпт интерпретирует как «не давать manual/automatic-specific
# рекомендаций». NULL/пустая строка тоже трактуется как «Не указано».
_ADMIN_CREATE_TRANSMISSIONS: tuple[str, ...] = (
    "АКПП", "МКПП", "Робот", "Вариатор", "Не указано",
)


class AdminCreateClient(BaseModel):
    telegram_id: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=200)
    phone: str = Field(min_length=4, max_length=64)


class AdminCreateVehicle(BaseModel):
    brand: str = Field(min_length=1, max_length=200)
    plate_number: str = Field(min_length=1, max_length=32)
    year: int | None = Field(default=None, ge=1900, le=2100)
    engine_size: float | None = Field(default=None, ge=0.0, le=20.0)
    vin: str | None = Field(default=None, max_length=32)
    fuel_type: str | None = Field(default=None, max_length=32)
    # Phase 18: тип трансмиссии. Валидируется против
    # ``_ADMIN_CREATE_TRANSMISSIONS`` (ниже), чтобы отдавать
    # нормальный detail вместо Pydantic-422.
    transmission: str | None = Field(default=None, max_length=32)


class AdminCreateClientAndVehicleRequest(BaseModel):
    """Body POST /api/admin/create-client-and-vehicle.

    Один payload — один новый клиент + одна машина. Если клиент с
    таким ``telegram_id`` уже существует, ``client.name`` / ``phone``
    игнорируются (не перезаписываем чужой профиль), и машина просто
    добавляется к его гаражу.
    """

    client: AdminCreateClient
    vehicle: AdminCreateVehicle


@app.post("/api/admin/create-client-and-vehicle")
@limiter.limit("20/minute", key_func=_user_or_ip_key)
async def api_admin_create_client_and_vehicle(
    request: Request,
    response: Response,
    payload: AdminCreateClientAndVehicleRequest,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Атомарно создать клиента (если не существует) и его автомобиль.

    Транзакция (одна commit-точка):
    1. Если ``users(telegram_id)`` нет — INSERT клиента (full_name,
       phone, car_brand/car_plate из vehicle для legacy-совместимости).
    2. INSERT строки в ``vehicles``. ``is_primary=True``, если у
       клиента это первая машина в гараже.

    Любая ошибка на шаге 2 откатывает шаг 1 (FastAPI dependency
    ``get_session`` ловит исключение и вызывает ``rollback``).

    CRITICAL — § 4.3 PROJECT_MEMORY: silent режим. Никаких уведомлений
    клиенту в Telegram (``bot.send_message``, ``notify_*``,
    ``notify_user_about_new_record``). CRM создаёт записи молча.
    """
    await _require_admin(user, session)

    client_payload = payload.client
    vehicle_payload = payload.vehicle

    # Нормализация. Убираем boundary-whitespace, схлопываем внутренние
    # повторные пробелы (имена и марки часто приходят с copy-paste).
    full_name = " ".join(client_payload.name.split()).strip()
    phone = client_payload.phone.strip()
    brand = " ".join(vehicle_payload.brand.split()).strip()
    plate_number = vehicle_payload.plate_number.strip()
    vin = vehicle_payload.vin.strip() if vehicle_payload.vin else None
    if vin == "":
        vin = None
    fuel_type = vehicle_payload.fuel_type.strip() if vehicle_payload.fuel_type else None
    if fuel_type == "":
        fuel_type = None
    # Phase 18: трансмиссия. "Не указано" мы намеренно ХРАНИМ как строку
    # (не превращаем в NULL) — это explicit-выбор пользователя «я не
    # знаю», и его стоит отличать от legacy-NULL для будущей аналитики.
    transmission = (
        vehicle_payload.transmission.strip()
        if vehicle_payload.transmission
        else None
    )
    if transmission == "":
        transmission = None

    if not full_name:
        raise HTTPException(status_code=400, detail="name is empty")
    if not phone:
        raise HTTPException(status_code=400, detail="phone is empty")
    if not brand:
        raise HTTPException(status_code=400, detail="brand is empty")
    if not plate_number:
        raise HTTPException(status_code=400, detail="plate_number is empty")
    # Phase 34 · whitelist'ы fuel/transmission СНЯТЫ. Поля принимают
    # любую строку до 32 символов — это «safety valve» для редких
    # экзотических случаев (LPG, Plug-in Hybrid и пр.), которых нет в
    # каноничном списке. Каноничные значения остаются стандартом UI,
    # но не блокируют сохранение свободного текста.

    user_repo = UserRepository(session)
    vehicle_repo = VehicleRepository(session)

    existing = await user_repo.get(client_payload.telegram_id)
    client_created = False
    if existing is None:
        # Новый клиент: используем переданные имя+телефон. Legacy-поля
        # car_brand/car_plate UserRow заполняем теми же значениями, что
        # пойдут в vehicles, чтобы legacy-обработчики ботом (которые
        # ещё читают users.car_brand) видели согласованную картину.
        await user_repo.create(
            telegram_id=client_payload.telegram_id,
            full_name=full_name,
            car_brand=brand,
            car_plate=plate_number,
            phone=phone,
        )
        client_created = True
    else:
        logger.info(
            "[admin/create-client-and-vehicle] клиент %s уже существует — "
            "пропускаем создание users; добавляем только vehicle",
            client_payload.telegram_id,
        )

    # Первая машина в гараже клиента → is_primary=True. Иначе —
    # дополняем гараж, primary не трогаем.
    existing_vehicles = await vehicle_repo.list_for_user(
        client_payload.telegram_id
    )
    is_primary = len(existing_vehicles) == 0

    new_vehicle = await vehicle_repo.add(
        user_id=client_payload.telegram_id,
        brand=brand,
        plate_number=plate_number,
        year=vehicle_payload.year,
        engine_volume=vehicle_payload.engine_size,
        vin=vin,
        fuel_type=fuel_type,
        transmission=transmission,
        is_primary=is_primary,
    )

    # Phase 37.14 · Walk-in flow: каждое «Добавить клиента» создаёт
    # parallel pending-заявку (status=pending_dropoff, appointment_*=NULL)
    # с привязкой к свежему vehicle_id. Так клиент моментально
    # отображается в picker'е расписания и админ может назначить ему
    # слот в матрице сразу после оформления.
    pending = await user_repo.create_service_request(
        telegram_id=client_payload.telegram_id,
        phone=phone,
        description=None,
        dropoff_window=None,
        vehicle_id=new_vehicle.id,
    )

    await session.commit()

    return {
        "ok": True,
        "client_created": client_created,
        "vehicle_id": new_vehicle.id,
        "is_primary": is_primary,
        "telegram_id": client_payload.telegram_id,
        "request_id": pending.id,
    }


# ---------------------------------------------------------------------------
# Phase 34 · Реляционный каталог автомобилей.
#
# Каскадные дропдауны WebApp подгружают опции через эти эндпоинты:
#   Brand → Model → Year → Engine (Volume + Fuel) → Transmission
#
# Источник:
#   * brands/models — static/cars_db.json (151 бренд × ~5000 моделей);
#   * years/engines — преднастроенные универсальные списки в
#     :mod:`web_app.cars_dataset` (per-model production-data у нас нет).
#
# Ответы кэшируются на стороне браузера (Cache-Control), сами списки
# уже мемоизированы lru_cache в датасет-модуле. Все эндпоинты публичные:
# каталог не содержит PII и используется на guest-форме тоже.
#
# Каждый ответ включает ``allow_other: true`` — фронт обязан отрисовать
# «Other / Ручной ввод» как последний пункт. Это «safety valve» для
# редких/экзотических машин (LPG, Plug-in Hybrid, кит-кары и т.д.):
# бэкенд (Pydantic в /api/vehicles + /api/admin/create-client-and-vehicle)
# сохранит произвольную строку без 422.
# ---------------------------------------------------------------------------


def _cars_response(items: list[Any]) -> dict[str, Any]:
    """Унифицированный JSON-ответ для всех /api/cars/* эндпоинтов."""
    return {
        "items": items,
        "count": len(items),
        # Фронт читает этот флаг и добавляет «Other / Ручной ввод»
        # к выпадашке. Сервер на write-стороне всегда принимает
        # свободный текст, поэтому флаг сейчас всегда True; оставлен
        # как explicit-контракт на будущее.
        "allow_other": True,
    }


@app.get("/api/cars/brands")
async def api_cars_brands() -> dict[str, Any]:
    """Все известные марки автомобилей.

    Идентично содержимому ``/static/cars_db.json``, но как plain JSON-список
    без вложенных моделей (frontend-кэш-ключ /api/cars/brands).
    """
    return _cars_response(cars_dataset.get_brands())


@app.get("/api/cars/models")
async def api_cars_models(brand: str = "") -> dict[str, Any]:
    """Модели для указанной марки.

    Параметр ``brand`` — обязателен (пустой → 400). Регистр-нечувствителен.
    Если такой марки нет — возвращаем пустой список (UI всё равно даст
    ввести модель вручную через «Other»).
    """
    brand_norm = (brand or "").strip()
    if not brand_norm:
        raise HTTPException(status_code=400, detail="brand is required")
    return _cars_response(cars_dataset.get_models(brand_norm))


@app.get("/api/cars/years")
async def api_cars_years(
    brand: str = "", model: str = ""
) -> dict[str, Any]:
    """Годы выпуска (свежие первыми).

    Brand/model передаются под будущий per-model filtering, сейчас
    отдаём универсальный диапазон ``current_year - 50 .. current_year``.
    """
    return _cars_response(
        cars_dataset.get_years(brand or None, model or None)
    )


@app.get("/api/cars/engines")
async def api_cars_engines(
    brand: str = "",
    model: str = "",
    year: int | None = None,
) -> dict[str, Any]:
    """Распространённые объёмы двигателя (литры) для модели/года.

    Параметры пока не сужают выдачу (датасета per-trim нет), но
    зарезервированы под будущее enrichment без слома API-контракта.
    """
    return _cars_response(
        cars_dataset.get_engine_volumes(brand or None, model or None, year)
    )


@app.get("/api/cars/fuels")
async def api_cars_fuels() -> dict[str, Any]:
    """Поддерживаемые типы топлива (плюс «Other / Ручной ввод» в UI)."""
    return _cars_response(cars_dataset.get_fuel_types())


@app.get("/api/cars/transmissions")
async def api_cars_transmissions() -> dict[str, Any]:
    """Поддерживаемые типы трансмиссии (плюс «Other / Ручной ввод»)."""
    return _cars_response(cars_dataset.get_transmissions())


# ---------------------------------------------------------------------------
# Phase 33 · Admin CRM — удаление клиента с полным каскадом.
# ---------------------------------------------------------------------------


@app.delete("/api/admin/clients/{telegram_id}")
@limiter.limit("20/minute", key_func=_user_or_ip_key)
async def api_admin_delete_client(
    request: Request,
    response: Response,
    telegram_id: int,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Удалить клиента и ВСЕ связанные с ним записи (cascade).

    Каскадно зачищается через :meth:`UserRepository.delete_client`:
    - ``notifications_log`` (по записям ТО клиента);
    - ``users_history`` (история ТО);
    - ``service_requests`` (заявки на звонок + master_comment);
    - ``health_feedback`` (по машинам клиента);
    - ``health_reports`` (по машинам клиента);
    - ``vehicles`` (гараж);
    - ``users`` (сама карточка).

    Всё это в одной транзакции — любое падение откатывает целиком.
    Защищён ``_require_admin``. Idempotent: повторный DELETE на уже
    удалённый ``telegram_id`` отдаёт 404.

    SAFETY — § 4.3 PROJECT_MEMORY: silent-режим. Никаких пуш-уведомлений
    клиенту в Telegram о факте удаления (мы не пишем ``bot.send_message``).
    """
    await _require_admin(user, session)

    if telegram_id < 1:
        raise HTTPException(status_code=400, detail="invalid telegram_id")

    repo = UserRepository(session)
    existing = await repo.get(telegram_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="client not found")

    # Защита: не давать удалить самого себя (текущего админа).
    # Иначе админ потеряет UserRow, но останется в admins → mismatch.
    actor_id = getattr(user, "id", None) if user else None
    if actor_id is not None and int(actor_id) == int(telegram_id):
        raise HTTPException(
            status_code=400, detail="cannot delete your own client profile",
        )

    affected = await repo.delete_client(telegram_id)
    if affected == 0:
        # repo.get вернул объект, но DELETE не нашёл строки — race condition
        # (параллельное удаление). Возвращаем 409 чтобы фронт перечитал.
        raise HTTPException(status_code=409, detail="delete conflict")

    await session.commit()

    return {
        "ok": True,
        "telegram_id": telegram_id,
        "deleted": True,
    }


# ---------------------------------------------------------------------------
# Phase 17 · Admin scaling — добавление мастеров через WebApp
# ---------------------------------------------------------------------------


class AdminAddMasterPayload(BaseModel):
    """Body POST /api/admin/add-master."""

    telegram_id: int = Field(ge=1)


@app.post("/api/admin/add-master")
@limiter.limit("10/minute", key_func=_user_or_ip_key)
async def api_admin_add_master(
    request: Request,
    response: Response,
    body: AdminAddMasterPayload,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Добавить ещё одного мастера в таблицу ``admins``.

    Защищено ``_require_admin``: только действующий админ может выдать
    админские права. Идемпотентно: повторный вызов с тем же id отдаёт
    ``already_admin: true`` и не падает.

    **Silent (§4.3 PROJECT_MEMORY)** — никаких уведомлений новому
    мастеру в Telegram. Узнает он о своих правах через `/start`
    (бот пошлёт ему inline-кнопку CRM-панели).
    """
    await _require_admin(user, session)

    new_id = body.telegram_id
    repo = AdminRepository(session)
    added = await repo.add(new_id)
    await session.commit()

    if added:
        logger.info(
            "admin %s granted admin rights to telegram_id=%s",
            user.id, new_id,
        )
    return {
        "ok": True,
        "telegram_id": new_id,
        "added": added,
        "already_admin": not added,
    }


# ---------------------------------------------------------------------------
# Phase 37.3 · Masters management — list + revoke.
# ---------------------------------------------------------------------------
#
# Подкладываем под уже существующую POST /api/admin/add-master два
# дополнительных эндпоинта: список всех админов (с username/full_name
# из users, если есть) и точечное удаление по telegram_id. Этого
# достаточно, чтобы фронт превратил «Добавить мастера» в полноценный
# «Сотрудники / Мастера» хаб без правок схемы БД.
# ---------------------------------------------------------------------------


@app.get("/api/admin/admins")
@limiter.limit("60/minute", key_func=_user_or_ip_key)
async def api_admin_admins_list(
    request: Request,
    response: Response,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Список действующих администраторов СТО.

    Каждый элемент:
      * ``telegram_id`` (int) — первичный ключ.
      * ``full_name`` (str | null) — из users, если он там есть.
      * ``username`` (str | null) — из users.
      * ``added_at`` (str | null) — ISO-строка из admins.added_at.
      * ``is_self`` (bool) — текущий запрашивающий админ; фронт
        прячет кнопку «удалить» для себя, чтобы не залочить аккаунт.
    """
    await _require_admin(user, session)

    from sqlalchemy import text as _text

    rows = (
        await session.execute(
            _text(
                """
                SELECT
                    a.telegram_id AS telegram_id,
                    u.full_name   AS full_name,
                    u.username    AS username,
                    a.added_at    AS added_at
                FROM admins a
                LEFT JOIN users u ON u.telegram_id = a.telegram_id
                ORDER BY a.added_at ASC, a.telegram_id ASC
                """
            )
        )
    ).all()

    items = [
        {
            "telegram_id": int(r[0]),
            "full_name": r[1],
            "username": r[2],
            "added_at": r[3],
            "is_self": int(r[0]) == int(user.id),
        }
        for r in rows
    ]
    return {"ok": True, "items": items, "total": len(items)}


@app.delete("/api/admin/admins/{telegram_id}")
@limiter.limit("20/minute", key_func=_user_or_ip_key)
async def api_admin_admins_delete(
    request: Request,
    response: Response,
    telegram_id: int,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Отозвать админские права у указанного Telegram ID.

    Защиты:
      * нельзя удалить самого себя (предотвращает само-локаут);
      * нельзя удалить последнего админа (предотвращает полный
        локаут CRM — некому будет вернуть права).
    Идемпотентно: повторный DELETE по уже отсутствующему id отдаёт
    ``removed=false`` + 404.
    """
    await _require_admin(user, session)

    if telegram_id <= 0:
        raise HTTPException(status_code=400, detail="telegram_id must be > 0")
    if telegram_id == int(user.id):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "self_revoke",
                "message": "Cannot revoke your own admin rights.",
            },
        )

    repo = AdminRepository(session)
    ids = await repo.list_ids(force_refresh=True)
    if telegram_id not in ids:
        raise HTTPException(status_code=404, detail="admin not found")
    if len(ids) <= 1:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "last_admin",
                "message": "Cannot revoke the last admin.",
            },
        )

    removed = await repo.remove(telegram_id)
    await session.commit()
    if removed:
        logger.info(
            "admin %s revoked admin rights from telegram_id=%s",
            user.id, telegram_id,
        )
    return {"ok": True, "telegram_id": telegram_id, "removed": removed}


# ---------------------------------------------------------------------------
# Phase 37 · Master-centric Scheduling Matrix.
# Колонки матрицы строго привязаны к списку админов («мастеров»);
# Y-ось — рабочие часы, резолвящиеся из глобального business_hours
# (будни / выходные) плюс опциональный per-date override.
# ---------------------------------------------------------------------------


# Phase 37.2 · Терминальные статусы заявки — выбывают из матрицы
# расписания и из конфликт-чеков. Любая активная (не cancelled и не
# completed) заявка считается «занимающей слот».
_SCHEDULE_TERMINAL_STATUSES: tuple[str, ...] = ("cancelled", "completed")

# Phase 37.2 · Статус после успешного booking'а. Заявка ушла из
# 'new' воронки и зафиксирована за слотом — клиент ожидается на
# приёмке в указанное время.
_BOOKED_STATUS: str = "pending_dropoff"

# Phase 37.2 · Свободный потолок длины имени мастера. Имя денормализуется
# в ``service_requests.assigned_master`` для UI; первичный ключ колонки —
# ``assigned_master_id`` (admin.telegram_id).
_MASTER_NAME_MAX: int = 64

# Phase 37.4 · Per-date operational hours override.
# Храним в KV под ключом ``schedule_hours:<YYYY-MM-DD>``. Значение —
# JSON-строка {start, end, is_day_off}. Глобальные часы сами по себе
# живут под отдельным ключом ``business_hours`` (см. ниже).
_HOURS_KEY_PREFIX: str = "schedule_hours:"
# Только целые часы — матрица Y-оси рендерит ровно по часам,
# дробные значения сейчас не имеют смысла (нет 30-минутных слотов).
_HOURS_STEP_RE = re.compile(r"^([01]\d|2[0-3]):00$")

# Phase 37.7 · Глобальный business_hours со split'ом Sat/Sun.
# Хранится одним KV-ключом ``business_hours`` в виде JSON.
# Раздельные блоки weekday / saturday / sunday — диктовала
# реальная практика СТО (Сб — короткий день, Вс — выходной).
# Старый формат ``weekend_*`` — легаси, проецируется на оба
# weekend-блока при чтении (одноразовый хэлпер ниже).
_BUSINESS_HOURS_KEY: str = "business_hours"
_BUSINESS_HOURS_DEFAULT: dict[str, Any] = {
    "weekday_start": "09:00",
    "weekday_end": "21:00",
    "saturday_start": "10:00",
    "saturday_end": "18:00",
    "saturday_is_closed": False,
    "sunday_start": "10:00",
    "sunday_end": "18:00",
    "sunday_is_closed": True,
}
# Совместимость со старыми вызовами в этом модуле — берётся из default'а.
_HOURS_DEFAULT_START: str = _BUSINESS_HOURS_DEFAULT["weekday_start"]
_HOURS_DEFAULT_END: str = _BUSINESS_HOURS_DEFAULT["weekday_end"]


def _hours_settings_key(date: str) -> str:
    """KV-ключ под per-date operational hours override."""
    return f"{_HOURS_KEY_PREFIX}{date}"


def _validate_whole_hour(value: str, field: str) -> str:
    """Только ``HH:00`` — гранулярность матрицы строго почасовая."""
    if not _HOURS_STEP_RE.fullmatch(value or ""):
        raise HTTPException(
            status_code=400,
            detail=f"{field} must be a whole hour in HH:00 format",
        )
    return value


def _is_weekend_iso(date_str: str) -> bool:
    """True, если ``YYYY-MM-DD`` приходится на субботу или воскресенье.

    Опирается на ``datetime.date.weekday()`` (Пн=0..Вс=6). Используется
    резолвером часов, чтобы выбрать weekday_* или weekend_* пресет.
    """
    parsed = datetime.strptime(date_str, "%Y-%m-%d").date()
    return parsed.weekday() >= 5


async def _load_business_hours(
    session: AsyncSession,
) -> dict[str, Any]:
    """Глобальный business_hours-пресет (будни / Сб / Вс).

    Любое битое поле в JSON откатывается к ``_BUSINESS_HOURS_DEFAULT``
    индивидуально — половинная корректность лучше падения матрицы.

    Легаси-миграция: если в JSON лежит старый формат с полями
    ``weekend_start``/``weekend_end``/``weekend_is_closed``, эти значения
    проецируются на оба новых блока (saturday_* и sunday_*),
    чтобы ранее сохранённые настройки не терялись после апгрейда.
    """
    raw = await SettingsRepository(session).get(_BUSINESS_HOURS_KEY)
    out = dict(_BUSINESS_HOURS_DEFAULT)
    if raw is None:
        return out
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning(
            "business_hours is corrupted (%r), falling back to default", raw,
        )
        return out
    if not isinstance(parsed, dict):
        return out

    # Легаси-проекция: weekend_* → saturday_* + sunday_*. Делаем
    # это ПЕРЕД разбором основных полей, чтобы явные saturday_*/
    # sunday_* в том же JSON выигрывали у legacy-фоллбэка.
    legacy_ws = parsed.get("weekend_start")
    legacy_we = parsed.get("weekend_end")
    legacy_wc = parsed.get("weekend_is_closed")
    if isinstance(legacy_ws, str) and _HOURS_STEP_RE.fullmatch(legacy_ws):
        out["saturday_start"] = legacy_ws
        out["sunday_start"] = legacy_ws
    if isinstance(legacy_we, str) and _HOURS_STEP_RE.fullmatch(legacy_we):
        out["saturday_end"] = legacy_we
        out["sunday_end"] = legacy_we
    if legacy_wc is not None:
        out["saturday_is_closed"] = bool(legacy_wc)
        out["sunday_is_closed"] = bool(legacy_wc)

    # Основной разбор нового формата. Все 6 часовых полей проходят
    # whole-hour-регексп индивидуально — любое битое откатывается.
    for key in (
        "weekday_start", "weekday_end",
        "saturday_start", "saturday_end",
        "sunday_start", "sunday_end",
    ):
        val = parsed.get(key)
        if isinstance(val, str) and _HOURS_STEP_RE.fullmatch(val):
            out[key] = val
    if "saturday_is_closed" in parsed:
        out["saturday_is_closed"] = bool(parsed.get("saturday_is_closed"))
    if "sunday_is_closed" in parsed:
        out["sunday_is_closed"] = bool(parsed.get("sunday_is_closed"))

    # start < end в каждой паре — если нет, восстанавливаем дефолт пары.
    for prefix in ("weekday", "saturday", "sunday"):
        if out[f"{prefix}_start"] >= out[f"{prefix}_end"]:
            out[f"{prefix}_start"] = _BUSINESS_HOURS_DEFAULT[f"{prefix}_start"]
            out[f"{prefix}_end"] = _BUSINESS_HOURS_DEFAULT[f"{prefix}_end"]
    return out


async def _resolve_hours_for_date(
    session: AsyncSession, date: str,
) -> dict[str, Any]:
    """Резолв часов на конкретную дату.

    Приоритет:
      1. per-date override (``schedule_hours:<date>``) — выигрывает
         всегда, даже над ``*_is_closed``.
      2. Сб / Вс + ``saturday_is_closed`` / ``sunday_is_closed`` →
         выходной по умолчанию.
      3. Сб → ``saturday_start..saturday_end``;
         Вс → ``sunday_start..sunday_end``.
      4. Будни → ``weekday_start..weekday_end``.

    Возврат включает ``source`` ∈ ``override`` | ``saturday_closed`` |
    ``sunday_closed`` | ``saturday`` | ``sunday`` | ``weekday`` — фронт
    показывает чип «авто-выходной» / «глобальные часы».
    Дополнительно отдаётся ``weekday_iso`` (1..7) — полезно
    фронту для любых будущих плашек.
    """
    parsed_date = datetime.strptime(date, "%Y-%m-%d").date()
    weekday_iso = parsed_date.isoweekday()  # 1=Пн, ..., 7=Вс
    is_saturday = weekday_iso == 6
    is_sunday = weekday_iso == 7
    is_weekend = is_saturday or is_sunday
    global_h = await _load_business_hours(session)

    # Часы «по умолчанию» для этого дня недели — используются как
    # fallback внутри override-ветки (если поля start/end битые).
    if is_saturday:
        default_start = global_h["saturday_start"]
        default_end = global_h["saturday_end"]
    elif is_sunday:
        default_start = global_h["sunday_start"]
        default_end = global_h["sunday_end"]
    else:
        default_start = global_h["weekday_start"]
        default_end = global_h["weekday_end"]

    raw = await SettingsRepository(session).get(_hours_settings_key(date))
    if raw is not None:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            start = str(parsed.get("start_time") or "")
            end = str(parsed.get("end_time") or "")
            if not _HOURS_STEP_RE.fullmatch(start):
                start = default_start
            if not _HOURS_STEP_RE.fullmatch(end):
                end = default_end
            if start >= end:
                start, end = default_start, default_end
            return {
                "start_time": start,
                "end_time": end,
                "is_day_off": bool(parsed.get("is_day_off", False)),
                "is_default": False,
                "source": "override",
                "is_weekend": is_weekend,
                "weekday_iso": weekday_iso,
            }

    if is_saturday and global_h["saturday_is_closed"]:
        return {
            "start_time": global_h["saturday_start"],
            "end_time": global_h["saturday_end"],
            "is_day_off": True,
            "is_default": True,
            "source": "saturday_closed",
            "is_weekend": True,
            "weekday_iso": weekday_iso,
        }
    if is_sunday and global_h["sunday_is_closed"]:
        return {
            "start_time": global_h["sunday_start"],
            "end_time": global_h["sunday_end"],
            "is_day_off": True,
            "is_default": True,
            "source": "sunday_closed",
            "is_weekend": True,
            "weekday_iso": weekday_iso,
        }
    if is_saturday:
        return {
            "start_time": global_h["saturday_start"],
            "end_time": global_h["saturday_end"],
            "is_day_off": False,
            "is_default": True,
            "source": "saturday",
            "is_weekend": True,
            "weekday_iso": weekday_iso,
        }
    if is_sunday:
        return {
            "start_time": global_h["sunday_start"],
            "end_time": global_h["sunday_end"],
            "is_day_off": False,
            "is_default": True,
            "source": "sunday",
            "is_weekend": True,
            "weekday_iso": weekday_iso,
        }
    return {
        "start_time": global_h["weekday_start"],
        "end_time": global_h["weekday_end"],
        "is_day_off": False,
        "is_default": True,
        "source": "weekday",
        "is_weekend": False,
        "weekday_iso": weekday_iso,
    }


def _admin_display_name(
    full_name: Optional[str],
    username: Optional[str],
    telegram_id: int,
) -> str:
    """Имя для колонки матрицы / для записи в ``assigned_master``.

    Порядок: ``full_name`` (если непустой) → ``@username`` → ``Мастер #ID``.
    Гарантирует ненулевую строку — в матрице нет «безымянных» колонок.
    """
    name = (full_name or "").strip()
    if name:
        return name
    uname = (username or "").strip()
    if uname:
        return f"@{uname}"
    return f"Мастер #{telegram_id}"


async def _load_active_masters(
    session: AsyncSession,
) -> list[dict[str, Any]]:
    """Список действующих мастеров — все, кто в таблице ``admins``.

    Возврат: ``[{id, name, full_name, username, added_at}, ...]``,
    отсортированный по дате добавления. ``id`` — telegram_id; ``name`` —
    готовая строка для колонки матрицы.
    """
    from sqlalchemy import text as _text
    rows = (
        await session.execute(
            _text(
                """
                SELECT
                    a.telegram_id AS telegram_id,
                    u.full_name   AS full_name,
                    u.username    AS username,
                    a.added_at    AS added_at
                FROM admins a
                LEFT JOIN users u ON u.telegram_id = a.telegram_id
                ORDER BY a.added_at ASC, a.telegram_id ASC
                """
            )
        )
    ).all()
    return [
        {
            "id": int(r[0]),
            "name": _admin_display_name(r[1], r[2], int(r[0])),
            "full_name": r[1],
            "username": r[2],
            "added_at": r[3],
        }
        for r in rows
    ]


async def _load_schedule_hours(
    session: AsyncSession, date: str,
) -> dict[str, Any]:
    """Загрузить hours override для ``date`` либо вернуть дефолты.

    Гарантирует: ``start`` < ``end``; whole-hour формат; ``is_day_off``
    — bool; ``is_default=True`` если в БД ещё нет записи на эту дату.
    """
    raw = await SettingsRepository(session).get(_hours_settings_key(date))
    if raw is None:
        return {
            "start_time": _HOURS_DEFAULT_START,
            "end_time": _HOURS_DEFAULT_END,
            "is_day_off": False,
            "is_default": True,
        }
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning(
            "schedule_hours for %s is corrupted (%r), falling back to default",
            date, raw,
        )
        return {
            "start_time": _HOURS_DEFAULT_START,
            "end_time": _HOURS_DEFAULT_END,
            "is_day_off": False,
            "is_default": True,
        }
    start = str(parsed.get("start_time") or _HOURS_DEFAULT_START)
    end = str(parsed.get("end_time") or _HOURS_DEFAULT_END)
    if not _HOURS_STEP_RE.fullmatch(start):
        start = _HOURS_DEFAULT_START
    if not _HOURS_STEP_RE.fullmatch(end):
        end = _HOURS_DEFAULT_END
    if start >= end:
        # Сохранённые значения «битые» — откатываемся к дефолту,
        # чтобы матрица не схлопывалась в 0 строк.
        start, end = _HOURS_DEFAULT_START, _HOURS_DEFAULT_END
    return {
        "start_time": start,
        "end_time": end,
        "is_day_off": bool(parsed.get("is_day_off", False)),
        "is_default": False,
    }


def _validate_schedule_date(value: str) -> str:
    """Лёгкая валидация формата ``YYYY-MM-DD``. Возвращает строку как
    есть, если формат корректный. Иначе — HTTPException 400."""
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="date must be in YYYY-MM-DD format",
        ) from exc
    return value


# Phase 37.6 · active_posts_count удалён из модели. Колонки матрицы
# теперь рендерятся по списку мастеров (GET /api/admin/masters).
# Оставлены 410-стабы на старых путях, чтобы любой завалявшийся клиент-
# fetch увидел осмысленный ответ, а не 500.


@app.api_route("/api/admin/settings/posts", methods=["GET", "POST"])
async def api_admin_posts_deprecated() -> None:
    """410 Gone. См. /api/admin/masters."""
    raise HTTPException(
        status_code=410,
        detail={
            "error": "deprecated",
            "message": (
                "active_posts_count removed; matrix columns are now "
                "derived from /api/admin/masters."
            ),
        },
    )


# ---------------------------------------------------------------------------
# Phase 37.6 · Global business_hours + Masters list endpoints.
# ---------------------------------------------------------------------------


class BusinessHoursPayload(BaseModel):
    """POST body для /api/admin/business-hours.

    Phase 37.7 · Sat/Sun сплит. Все часы ``HH:00``. ``*_is_closed`` —
    bool; если True, соответствующий день недели автоматически
    выходной без явного per-date override'а.
    """

    weekday_start: str = Field(min_length=5, max_length=5)
    weekday_end: str = Field(min_length=5, max_length=5)
    saturday_start: str = Field(min_length=5, max_length=5)
    saturday_end: str = Field(min_length=5, max_length=5)
    saturday_is_closed: bool = False
    sunday_start: str = Field(min_length=5, max_length=5)
    sunday_end: str = Field(min_length=5, max_length=5)
    sunday_is_closed: bool = False


@app.get("/api/admin/business-hours")
@limiter.limit("60/minute", key_func=_user_or_ip_key)
async def api_admin_get_business_hours(
    request: Request,
    response: Response,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Прочитать глобальные рабочие часы СТО (будни / выходные)."""
    await _require_admin(user, session)
    data = await _load_business_hours(session)
    is_default = (
        await SettingsRepository(session).get(_BUSINESS_HOURS_KEY)
    ) is None
    return {"ok": True, **data, "is_default": is_default}


@app.post("/api/admin/business-hours")
@limiter.limit("30/minute", key_func=_user_or_ip_key)
async def api_admin_set_business_hours(
    request: Request,
    response: Response,
    payload: BusinessHoursPayload,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Сохранить глобальные рабочие часы (будни / Сб / Вс) и флаги
    «Сб выходной» / «Вс выходной»."""
    await _require_admin(user, session)
    wd_start = _validate_whole_hour(payload.weekday_start, "weekday_start")
    wd_end = _validate_whole_hour(payload.weekday_end, "weekday_end")
    sat_start = _validate_whole_hour(payload.saturday_start, "saturday_start")
    sat_end = _validate_whole_hour(payload.saturday_end, "saturday_end")
    sun_start = _validate_whole_hour(payload.sunday_start, "sunday_start")
    sun_end = _validate_whole_hour(payload.sunday_end, "sunday_end")
    if wd_start >= wd_end:
        raise HTTPException(
            status_code=400,
            detail="weekday_start must be earlier than weekday_end",
        )
    if sat_start >= sat_end:
        raise HTTPException(
            status_code=400,
            detail="saturday_start must be earlier than saturday_end",
        )
    if sun_start >= sun_end:
        raise HTTPException(
            status_code=400,
            detail="sunday_start must be earlier than sunday_end",
        )
    body = {
        "weekday_start": wd_start,
        "weekday_end": wd_end,
        "saturday_start": sat_start,
        "saturday_end": sat_end,
        "saturday_is_closed": bool(payload.saturday_is_closed),
        "sunday_start": sun_start,
        "sunday_end": sun_end,
        "sunday_is_closed": bool(payload.sunday_is_closed),
    }
    await SettingsRepository(session).set(
        _BUSINESS_HOURS_KEY, json.dumps(body, ensure_ascii=False),
    )
    await session.commit()
    logger.info(
        "admin %s set business_hours wd=%s..%s sat=%s..%s (closed=%s) sun=%s..%s (closed=%s)",
        user.id, wd_start, wd_end,
        sat_start, sat_end, body["saturday_is_closed"],
        sun_start, sun_end, body["sunday_is_closed"],
    )
    return {"ok": True, **body}


@app.get("/api/admin/masters")
@limiter.limit("60/minute", key_func=_user_or_ip_key)
async def api_admin_masters_list(
    request: Request,
    response: Response,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Список мастеров (= admins) для колонок матрицы расписания.

    Облегчённый ответ (``id``, ``name``, ``username``) — фронту этого
    хватает для заголовков «МАСТЕР ОЛЕГ» и для master-select в picker'е.
    Полный список (с датой добавления, is_self) отдаёт /api/admin/admins.
    """
    await _require_admin(user, session)
    masters = await _load_active_masters(session)
    return {
        "ok": True,
        "items": [
            {"id": m["id"], "name": m["name"], "username": m["username"]}
            for m in masters
        ],
        "total": len(masters),
    }


@app.get("/api/admin/schedule")
@limiter.limit("60/minute", key_func=_user_or_ip_key)
async def api_admin_schedule(
    request: Request,
    response: Response,
    date: str,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Расписание на конкретную дату.

    Возвращает все заявки с ``appointment_date == date`` и не-null
    ``appointment_time`` + ``assigned_post_id``. Не-resolved и любые —
    включая отменённые — чтобы матрица показывала «закрашенный» слот
    с цветом по статусу (это backend-friendly: фильтрация по статусу
    делается на фронте).

    Каждый item включает denormalized данные машины (brand/plate) и
    клиента (имя/telegram_id) — фронту хватает одного запроса, чтобы
    нарисовать всю сетку без N+1.
    """
    await _require_admin(user, session)
    date = _validate_schedule_date(date)

    # Phase 37.6 · колонки матрицы = мастера (admins). Список нужен
    # фронту для заголовков и для master-select в picker'е.
    masters = await _load_active_masters(session)
    masters_view = [
        {"id": m["id"], "name": m["name"], "username": m["username"]}
        for m in masters
    ]
    master_ids = {m["id"] for m in masters}

    # Один SQL с JOIN'ом на users + vehicles. Сортируем по времени
    # и мастеру — фронт получает уже почти готовый порядок.
    from sqlalchemy import select as _select
    from database.models import ServiceRequestRow as _Req
    from database.models import UserRow as _User
    from database.models import VehicleRow as _Veh

    stmt = (
        _select(
            _Req.id,
            _Req.telegram_id,
            _Req.appointment_time,
            _Req.assigned_post_id,
            _Req.status,
            _Req.description,
            _Req.vehicle_id,
            _User.full_name,
            _Veh.brand,
            _Veh.plate_number,
            _Req.assigned_master,
            _Req.duration_hours,
            _Req.assigned_master_id,
            # Phase 37.12 · enrich для Booking Details modal: телефон
            # клиента (с фолбэком на phone заявки) и tg-username.
            _User.phone,
            _Req.phone,
            _User.username,
        )
        .join(_User, _User.telegram_id == _Req.telegram_id, isouter=True)
        .join(_Veh, _Veh.id == _Req.vehicle_id, isouter=True)
        .where(_Req.appointment_date == date)
        .where(_Req.appointment_time.isnot(None))
        # Phase 37.6 · фильтр по master_id вместо post_id. Старые
        # post-only заявки игнорируются — у них нет ни мастера, ни
        # колонки в новой матрице.
        .where(_Req.assigned_master_id.isnot(None))
        .where(_Req.status.notin_(_SCHEDULE_TERMINAL_STATUSES))
        .order_by(_Req.appointment_time, _Req.assigned_master_id)
    )
    result = await session.execute(stmt)
    items: list[dict[str, Any]] = []
    for row in result.all():
        master_id = int(row[12]) if row[12] is not None else None
        # COALESCE(user.phone, request.phone) — у legacy-юзеров (Phase <15)
        # users.phone NULL, но заявка всегда несёт contact-phone (NOT NULL).
        client_phone = row[13] or row[14] or None
        client_username = row[15] or None
        # Если мастер удалён из админов между booking'ом и просмотром
        # матрицы, всё равно отдаём запись — фронт нарисует
        # «orphan»-столбец, чтобы не терять заявки.
        items.append({
            "request_id": int(row[0]),
            "telegram_id": int(row[1]) if row[1] is not None else None,
            "time": row[2],
            "post_id": int(row[3]) if row[3] is not None else None,
            "status": row[4],
            "description": row[5],
            "vehicle_id": int(row[6]) if row[6] is not None else None,
            "client_name": row[7],
            "car_brand": row[8],
            "car_plate": row[9],
            "assigned_master": row[10],
            "duration_hours": int(row[11]) if row[11] is not None else 1,
            "master_id": master_id,
            "master_is_active": master_id in master_ids if master_id else False,
            "client_phone": client_phone,
            "client_username": client_username,
        })

    hours = await _resolve_hours_for_date(session, date)

    return {
        "date": date,
        "masters": masters_view,
        "items": items,
        "count": len(items),
        # Phase 37.6 · резолв из business_hours (будни/выходные) +
        # per-date override (приоритетнее). Фронт рисует Y-ось из
        # ``hours.start_time..hours.end_time`` и прячет грид, если
        # ``hours.is_day_off``.
        "hours": hours,
    }


# ---------------------------------------------------------------------------
# Phase 37.4 · Operational hours (start/end + day-off) per-date.
# ---------------------------------------------------------------------------


class ScheduleHoursPayload(BaseModel):
    """POST body для /api/admin/schedule/hours.

    Все поля обязательны. ``is_day_off=True`` НЕ обнуляет start/end:
    они продолжают храниться, чтобы при снятии toggle'а админ не
    должен был вводить часы заново.
    """

    date: str = Field(min_length=10, max_length=10)
    start_time: str = Field(min_length=5, max_length=5)
    end_time: str = Field(min_length=5, max_length=5)
    is_day_off: bool = False


@app.post("/api/admin/schedule/hours")
@limiter.limit("60/minute", key_func=_user_or_ip_key)
async def api_admin_set_schedule_hours(
    request: Request,
    response: Response,
    payload: ScheduleHoursPayload,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Сохранить рабочие часы (или флаг выходного) для конкретной даты.

    Семантика:
      * ``start_time`` / ``end_time`` — целые часы ``HH:00``.
      * ``start_time`` < ``end_time``.
      * ``is_day_off`` — bool. Если ``True``, фронт прячет матрицу
        и показывает плашку «Выходной»; backend всё равно сохраняет
        переданные часы, чтобы при выключении toggle'а интервал
        восстановился без повторного ввода.
    """
    await _require_admin(user, session)

    date = _validate_schedule_date(payload.date)
    start = _validate_whole_hour(payload.start_time, "start_time")
    end = _validate_whole_hour(payload.end_time, "end_time")
    if start >= end:
        raise HTTPException(
            status_code=400,
            detail="start_time must be earlier than end_time",
        )

    body = {
        "start_time": start,
        "end_time": end,
        "is_day_off": bool(payload.is_day_off),
    }
    await SettingsRepository(session).set(
        _hours_settings_key(date), json.dumps(body, ensure_ascii=False),
    )
    await session.commit()
    logger.info(
        "admin %s set schedule hours for %s -> %s..%s day_off=%s",
        user.id, date, start, end, body["is_day_off"],
    )
    return {
        "ok": True,
        "date": date,
        **body,
        "is_default": False,
    }


# Phase 37.1 · Picker для пустых слотов матрицы.
# Возвращает заявки, которые ещё не записаны в расписание
# (appointment_date IS NULL) и находятся в активных статусах
# (включая ``new``: только что созданная клиентом заявка, которой
# админ хочет назначить конкретный слот).
_SCHEDULE_PENDING_STATUSES: tuple[str, ...] = (
    "new", "pending_dropoff",
)
_SCHEDULE_PENDING_LIMIT: int = 100

# Валидный формат времени для слота. Строго ``HH:MM``, иначе у фронта
# поедет ключ в _renderScheduleMatrix (он сравнивает t[:5] с time-cell).
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


# Phase 37.6 · /assign удалён в пользу /book (мастер-центричный).
# ScheduleAssignPayload снят, чтобы не висеть в OpenAPI-схеме как
# мёртвая модель.


@app.get("/api/admin/schedule/pending")
@limiter.limit("60/minute", key_func=_user_or_ip_key)
async def api_admin_schedule_pending(
    request: Request,
    response: Response,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Заявки без записи в расписание — кандидаты на привязку к слоту.

    Фильтр: ``status IN _SCHEDULE_PENDING_STATUSES`` и
    ``appointment_date IS NULL`` (то есть заявка ещё не сидит ни в
    одной ячейке матрицы). Сортировка ``id DESC`` — самые свежие
    сверху, как и в ``/api/admin/requests``.

    Возврат — denormalized по машине/клиенту, чтобы фронт мог
    сразу показать «Volvo XC70 · M123AB» без отдельного fetch'а.
    """
    await _require_admin(user, session)

    from sqlalchemy import select as _select
    from database.models import ServiceRequestRow as _Req
    from database.models import UserRow as _User
    from database.models import VehicleRow as _Veh

    stmt = (
        _select(
            _Req.id,
            _Req.telegram_id,
            _Req.phone,
            _Req.description,
            _Req.status,
            _Req.created_at,
            _Req.vehicle_id,
            _User.full_name,
            _User.username,
            _Veh.brand,
            _Veh.model,
            _Veh.plate_number,
        )
        .join(_User, _User.telegram_id == _Req.telegram_id, isouter=True)
        .join(_Veh, _Veh.id == _Req.vehicle_id, isouter=True)
        .where(_Req.status.in_(_SCHEDULE_PENDING_STATUSES))
        .where(_Req.appointment_date.is_(None))
        .order_by(_Req.id.desc())
        .limit(_SCHEDULE_PENDING_LIMIT)
    )
    result = await session.execute(stmt)
    items: list[dict[str, Any]] = []
    for row in result.all():
        items.append({
            "request_id": int(row[0]),
            "telegram_id": int(row[1]) if row[1] is not None else None,
            "phone": row[2],
            "description": (row[3] or "").strip() or None,
            "status": row[4],
            "created_at": localize_naive_sql(row[5]) if row[5] else None,
            "vehicle_id": int(row[6]) if row[6] is not None else None,
            "client_name": row[7],
            "username": row[8],
            "car_brand": row[9],
            "car_model": row[10],
            "car_plate": row[11],
        })
    return {"items": items, "count": len(items)}


@app.api_route("/api/admin/schedule/assign", methods=["POST"])
async def api_admin_schedule_assign_deprecated() -> None:
    """410 Gone. Используйте /api/admin/schedule/book."""
    raise HTTPException(
        status_code=410,
        detail={
            "error": "deprecated",
            "message": (
                "/schedule/assign removed; use /schedule/book with master_id."
            ),
        },
    )


# ---------------------------------------------------------------------------
# Phase 37.6 · Master-centric booking endpoint.
# ---------------------------------------------------------------------------
#
# /book — единственный booking-путь после Phase 37.6:
#   * проверяет, что заявка ещё в активном статусе (не cancelled / не
#     completed) — нельзя бронировать «мёртвую» заявку;
#   * требует ``master_id`` (admin.telegram_id) — это первичный ключ
#     колонки матрицы. Имя автоматически денормализуется в
#     ``assigned_master`` (TEXT) для быстрых выборок без JOIN'а;
#   * исключает из конфликт-чека терминальные бронирования (cancelled/
#     completed) — освобождённый слот можно переиспользовать;
#   * range-overlap по (date, master_id, hour);
#   * переводит заявку из `new` в `pending_dropoff` (state-machine bump),
#     чтобы воронка приёма видела её как реально запланированную.
# ---------------------------------------------------------------------------


# Phase 37.5 · потолок длительности одной брони. 13 часов покрывает
# матрицу 09:00–21:00 (стандартный режим работы). Выше — это либо
# опечатка, либо запрос на смену; всё равно ограничиваем, чтобы
# не нарваться на 24-часовое значение, перекрывающее midnight wrap.
_BOOKING_DURATION_MIN: int = 1
_BOOKING_DURATION_MAX: int = 13


class ScheduleWalkInPendingPayload(BaseModel):
    """POST body для /api/admin/schedule/walk-in-pending.

    Phase 37.15 · создаёт pending-заявку (status=pending_dropoff,
    appointment_*=NULL) для уже существующего клиента/машины. Нужен,
    чтобы при walk-in flow админ мог из picker'а выбрать существующего
    клиента и сразу назначить ему слот: фронт создаёт pending здесь,
    потом сразу же зовёт /api/admin/schedule/book с полученным
    request_id.
    """

    telegram_id: int = Field(ge=1)
    vehicle_id: int = Field(ge=1)


@app.post("/api/admin/schedule/walk-in-pending")
@limiter.limit("30/minute", key_func=_user_or_ip_key)
async def api_admin_schedule_walk_in_pending(
    request: Request,
    response: Response,
    payload: ScheduleWalkInPendingPayload,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Создать pending-заявку для существующего клиента/машины.

    Используется walk-in потоком в админ-расписании: админ выбирает
    клиента из «Все клиенты», кликает на машину — фронт сначала
    создаёт пустую pending-заявку этим эндпоинтом, потом сразу
    же вызывает /api/admin/schedule/book для её привязки к слоту.

    Возвращает 404, если клиент или машина не найдены / машина не
    принадлежит клиенту (защита от IDOR).
    """
    await _require_admin(user, session)

    user_repo = UserRepository(session)
    vehicle_repo = VehicleRepository(session)

    profile = await user_repo.get(payload.telegram_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="client not found")

    vehicles = await vehicle_repo.list_for_user(payload.telegram_id)
    vehicle = next(
        (v for v in vehicles if v.id == payload.vehicle_id), None,
    )
    if vehicle is None:
        # Машина либо не существует, либо принадлежит другому клиенту —
        # ни в одном случае не раскрываем подробности (IDOR-protection).
        raise HTTPException(status_code=404, detail="vehicle not found")

    # phone берём из профиля клиента; service_requests.phone — NOT NULL,
    # без него заявку нельзя создать (legacy contract).
    phone = (profile.phone or "").strip()
    if not phone:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "client_phone_missing",
                "message": (
                    "Client has no phone on file; edit the client first."
                ),
                "telegram_id": payload.telegram_id,
            },
        )

    req = await user_repo.create_service_request(
        telegram_id=payload.telegram_id,
        phone=phone,
        description=None,
        dropoff_window=None,
        vehicle_id=payload.vehicle_id,
    )
    await session.commit()

    logger.info(
        "admin %s walk-in pending: req=%s client=%s vehicle=%s",
        user.id, req.id, payload.telegram_id, payload.vehicle_id,
    )
    return {
        "ok": True,
        "request_id": req.id,
        "telegram_id": payload.telegram_id,
        "vehicle_id": payload.vehicle_id,
    }


class ScheduleBookPayload(BaseModel):
    """POST body для /api/admin/schedule/book.

    Phase 37.6 · master_id — обязательный ключ колонки матрицы.
    ``duration_hours`` — длительность визита в часах. Дефолт 1.
    Старое поле ``post_id`` больше не используется матрицей; его можно
    передавать (например, из легаси-кода), но оно опционально и
    хранится только для статистики.
    """

    request_id: int = Field(ge=1)
    date: str = Field(min_length=10, max_length=10)
    time: str = Field(min_length=5, max_length=5)
    master_id: int = Field(ge=1)
    duration_hours: int = Field(
        default=1, ge=_BOOKING_DURATION_MIN, le=_BOOKING_DURATION_MAX
    )
    post_id: Optional[int] = Field(default=None, ge=1, le=99)


@app.post("/api/admin/schedule/book")
@limiter.limit("30/minute", key_func=_user_or_ip_key)
async def api_admin_schedule_book(
    request: Request,
    response: Response,
    payload: ScheduleBookPayload,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Записать заявку на конкретный слот матрицы расписания.

    Возвращает 409 при range-overlap на (date, master_id, hour).
    Возвращает 404 если заявка не найдена или мастер не найден среди
    действующих админов. Возвращает 400 при некорректном формате
    date / time или если заявка уже завершена / отменена.
    """
    await _require_admin(user, session)

    _validate_schedule_date(payload.date)
    if not _TIME_RE.fullmatch(payload.time):
        raise HTTPException(
            status_code=400, detail="time must be in HH:MM format"
        )

    # Резолвим мастера из admins, чтобы получить его имя для денормализации
    # и убедиться, что он ещё действующий. Если админа удалили между
    # открытием picker'а и POST'ом, отдаём 404 — фронт обновит список.
    from sqlalchemy import text as _text

    master_row = (
        await session.execute(
            _text(
                """
                SELECT a.telegram_id, u.full_name, u.username
                FROM admins a
                LEFT JOIN users u ON u.telegram_id = a.telegram_id
                WHERE a.telegram_id = :mid
                """
            ),
            {"mid": payload.master_id},
        )
    ).first()
    if master_row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "master_not_found",
                "message": "Master is not an active admin.",
                "master_id": payload.master_id,
            },
        )
    master_name = _admin_display_name(
        master_row[1], master_row[2], int(master_row[0])
    )

    # Phase 37.5 · диапазон часов, который занимает бронь.
    start_hour = int(payload.time.split(":", 1)[0])
    dur = int(payload.duration_hours)
    if start_hour + dur > 24:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "duration_overflow",
                "message": (
                    "Booking crosses midnight; reduce duration_hours."
                ),
                "start_hour": start_hour,
                "duration_hours": dur,
            },
        )
    booking_hours: list[str] = [
        f"{(start_hour + k):02d}:00" for k in range(dur)
    ]

    from sqlalchemy import select as _select
    from sqlalchemy import update as _update
    from database.models import ServiceRequestRow as _Req

    req_row = (
        await session.execute(
            _select(_Req).where(_Req.id == payload.request_id)
        )
    ).scalar_one_or_none()
    if req_row is None:
        raise HTTPException(status_code=404, detail="request not found")

    if req_row.status in _SCHEDULE_TERMINAL_STATUSES:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "request_terminal",
                "message": (
                    "Cannot book a request in terminal status "
                    f"({req_row.status})."
                ),
                "status": req_row.status,
            },
        )

    # Phase 37.6 · range overlap по (date, master_id, hour). Тянем все
    # активные брони этого мастера на этот день, считаем пересечение
    # часовых множеств.
    existing = (
        await session.execute(
            _select(
                _Req.id,
                _Req.appointment_time,
                _Req.duration_hours,
            )
            .where(_Req.appointment_date == payload.date)
            .where(_Req.assigned_master_id == payload.master_id)
            .where(_Req.appointment_time.isnot(None))
            .where(_Req.id != payload.request_id)
            .where(_Req.status.notin_(_SCHEDULE_TERMINAL_STATUSES))
        )
    ).all()
    booking_set = set(booking_hours)
    for row in existing:
        other_id = int(row[0])
        other_time_raw = row[1] or ""
        try:
            other_start = int(str(other_time_raw)[:2])
        except ValueError:
            logger.warning(
                "booking conflict scan: bad appointment_time %r on request %s",
                other_time_raw, other_id,
            )
            continue
        other_dur = int(row[2] or 1)
        if other_dur < 1:
            other_dur = 1
        other_hours = {
            f"{(other_start + k):02d}:00" for k in range(other_dur)
            if 0 <= other_start + k < 24
        }
        overlap = booking_set & other_hours
        if overlap:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "slot_conflict",
                    "message": (
                        "Master is already booked in this time range."
                    ),
                    "occupying_request_id": other_id,
                    "overlap_hours": sorted(overlap),
                },
            )

    next_status = req_row.status
    if req_row.status == "new":
        next_status = _BOOKED_STATUS

    await session.execute(
        _update(_Req)
        .where(_Req.id == payload.request_id)
        .values(
            appointment_date=payload.date,
            appointment_time=payload.time,
            assigned_post_id=payload.post_id,
            assigned_master_id=int(payload.master_id),
            assigned_master=master_name,
            duration_hours=dur,
            status=next_status,
        )
    )
    await session.commit()

    logger.info(
        "admin %s booked request %s -> %s %s master_id=%s name=%r dur=%dh status=%s",
        user.id, payload.request_id, payload.date,
        payload.time, payload.master_id, master_name, dur, next_status,
    )
    return {
        "ok": True,
        "request_id": payload.request_id,
        "date": payload.date,
        "time": payload.time,
        "master_id": int(payload.master_id),
        "assigned_master": master_name,
        "duration_hours": dur,
        "status": next_status,
    }


# ---------------------------------------------------------------------------
# Phase 17 · AI OCR — распознавание техпаспорта по фото (Gemini Vision)
# ---------------------------------------------------------------------------


# Допустимые MIME-типы для аплоада фото техпаспорта. Whitelist — чтобы
# не пытаться скармливать модели произвольные бинарники / PDF / SVG
# с внешними ссылками.
_ALLOWED_OCR_MIME = frozenset({
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
})
# Безопасный лимит на одно изображение. Gemini принимает существенно
# больше, но ограничиваем upload, чтобы не платить за случайные
# многомегабайтные PNG со скриншотов и не вешать процесс.
_MAX_OCR_BYTES = 10 * 1024 * 1024  # 10 MB


@app.post("/api/ai/parse-tech-passport")
@limiter.limit("10/minute", key_func=_user_or_ip_key)
async def api_ai_parse_tech_passport(
    request: Request,
    response: Response,
    image: UploadFile = File(..., description="Фото техпаспорта"),
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Распознать ключевые поля автомобиля по фото техпаспорта.

    Используется в формах добавления авто (admin-create-client + клиент
    add-vehicle) для автозаполнения. Возвращает строгий JSON
    ``{brand_model, plate_number, vin, year, engine_size}`` —
    каждое поле может быть ``null`` если модель не нашла.

    Safety:
    * MIME whitelist (jpeg/png/webp/heic).
    * Hard upload limit 10 MB.
    * Rate-limit 10/min на пользователя — недёшево по квоте Vision-моделей.
    """
    if not ai_master.is_available():
        raise HTTPException(status_code=503, detail="AI not configured")

    mime = (image.content_type or "").lower().split(";", 1)[0].strip()
    if mime not in _ALLOWED_OCR_MIME:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported media type: {mime!r}",
        )
    raw = await image.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty image")
    if len(raw) > _MAX_OCR_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"image too large (>{_MAX_OCR_BYTES // (1024 * 1024)} MB)",
        )

    try:
        parsed = await ai_master.parse_tech_passport(
            image_bytes=raw, mime_type=mime
        )
    except ai_master.QuotaExceededError:
        raise HTTPException(
            status_code=429,
            detail="AI временно недоступен (превышена квота). Попробуйте позже.",
        )
    except RuntimeError as exc:
        # _ensure_configured / прочие конфигурационные ошибки.
        logger.warning("parse_tech_passport: runtime error: %s", exc)
        raise HTTPException(status_code=503, detail="AI not configured")
    except Exception as exc:
        logger.exception(
            "parse_tech_passport failed for user_id=%s: %s", user.id, exc
        )
        raise HTTPException(status_code=502, detail="OCR failed")

    logger.info(
        "OCR tech-passport ok user=%s bytes=%d brand=%r plate=%r year=%s",
        user.id, len(raw),
        parsed.get("brand_model"),
        parsed.get("plate_number"),
        parsed.get("year"),
    )
    return {"ok": True, "data": parsed}


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------


app.mount(
    "/static",
    StaticFiles(directory=str(STATIC_DIR)),
    name="static",
)

# Phase 35 · публичная отдача user-uploads (фото записей ТО). Каталог
# существует с момента создания константы выше; файлы внутри —
# только наши (имена UUID, расширения из whitelist), поэтому SOC от
# directory-traversal обеспечивается на уровне эндпоинта аплоада.
app.mount(
    "/uploads",
    StaticFiles(directory=str(UPLOADS_DIR), check_dir=True),
    name="uploads",
)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={
            # HTML — без кэширования, чтобы при правках фронта пользователи
            # сразу получали свежую версию (миниапп пересобирается часто).
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
        },
    )
