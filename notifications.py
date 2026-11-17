"""
Ежедневные «умные» уведомления клиентам.

Логика
------
* раз в сутки в 10:00 **по Europe/Minsk** пробегаемся по **всем активным
  работам** каждого клиента (а не только по самой свежей записи).
  «Активная работа» — самая свежая запись для пары ``(user_id, service_type)``,
  у которой есть хотя бы один регламент: ``next_visit_date`` или
  ``next_service_mileage``;
* для каждой такой работы вычисляем «severity»:

  - ``'overdue'`` (🚨 ПРОСРОЧЕНО) — ``rem_km < 0`` или ``days_left < 0``;
  - ``'urgent'`` (⚠️ СРОЧНО) — ``0 ≤ rem_km ≤ MILEAGE_THRESHOLD_KM``
    или ``0 ≤ days_left < DATE_THRESHOLD_DAYS``;
  - в остальных случаях — план, не уведомляем;

* применяем дедупликацию через таблицу :data:`notifications_log`:

  - ``urgent``: повтор не раньше чем через ``URGENT_REPEAT_DAYS`` (5) суток;
  - ``overdue``: повтор не раньше чем через ``OVERDUE_REPEAT_DAYS`` (3) суток;

* шлём расширенное сообщение с маркой авто, услугой, статусом и цифрами.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.repositories import RecordRepository
from database.session import async_session_factory


def _request_call_kb() -> InlineKeyboardMarkup:
    """Inline-кнопка «📞 Заказать звонок» для уведомлений.

    Callback ``notify:request_call`` обрабатывается в
    :mod:`handlers.user` и переводит клиента в ``RequestCall.waiting_phone``
    — тот же флоу, что и от reply-кнопки главного меню.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="📞 Заказать звонок",
                callback_data="notify:request_call",
            )
        ]]
    )

logger = logging.getLogger(__name__)


DATE_THRESHOLD_DAYS = 14
MILEAGE_THRESHOLD_KM = 1000

# Дедупликация: не слать одну и ту же severity по одной и той же работе
# чаще чем раз в N суток.
URGENT_REPEAT_DAYS = 5
OVERDUE_REPEAT_DAYS = 3

# Таймзона расчёта 10:00 — Минск (UTC+3 круглый год).
TZ = ZoneInfo("Europe/Minsk")

Severity = Literal["urgent", "overdue"]


def _iso_to_human(iso_date: str) -> str:
    return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d.%m.%Y")


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


def _classify(
    days_left: int | None, rem_km: int | None
) -> Severity | None:
    """Вернуть severity или ``None`` (если работа не требует уведомления).

    ``days_left`` — `next_visit_date − today` в днях (``None`` если регламент
    по дате не задан или дата некорректна).
    ``rem_km`` — `next_service_mileage − last_known_mileage` (``None`` если
    регламент по пробегу не задан или клиент не обновлял пробег).
    """
    overdue = (days_left is not None and days_left < 0) or (
        rem_km is not None and rem_km < 0
    )
    if overdue:
        return "overdue"
    urgent = (days_left is not None and 0 <= days_left <= DATE_THRESHOLD_DAYS) or (
        rem_km is not None and 0 <= rem_km <= MILEAGE_THRESHOLD_KM
    )
    if urgent:
        return "urgent"
    return None


def _format_message(
    car_brand: str,
    service_type: str,
    severity: Severity,
    days_left: int | None,
    rem_km: int | None,
    next_visit_date: str | None,
) -> str:
    if severity == "overdue":
        header = f"🚨 <b>ПРОСРОЧЕНО:</b> {service_type}"
        details: list[str] = []
        if days_left is not None and days_left < 0:
            n = -days_left
            details.append(f"срок истёк {n} {_plural_day(n)} назад")
        if rem_km is not None and rem_km < 0:
            details.append(f"пробег превышен на {-rem_km} км")
    else:
        header = f"⚠️ <b>СРОЧНО:</b> {service_type}"
        details = []
        if days_left is not None and 0 <= days_left <= DATE_THRESHOLD_DAYS:
            if days_left == 0:
                details.append("сегодня")
            else:
                details.append(f"осталось {days_left} {_plural_day(days_left)}")
        if rem_km is not None and 0 <= rem_km <= MILEAGE_THRESHOLD_KM:
            details.append(f"осталось {rem_km} км")

    visit_line = (
        f"\n📅 Дата следующего ТО: <b>{_iso_to_human(next_visit_date)}</b>"
        if next_visit_date
        else ""
    )

    text = (
        f"🔔 Напоминание по вашему авто <b>{car_brand}</b>:\n"
        f"{header}\n"
        f"⏳ {' / '.join(details) if details else '—'}"
        f"{visit_line}"
    )
    return text


def _dedup_window_days(severity: Severity) -> int:
    return URGENT_REPEAT_DAYS if severity == "urgent" else OVERDUE_REPEAT_DAYS


async def _should_send(
    records: RecordRepository,
    record_id: int,
    severity: Severity,
    today: date,
) -> bool:
    """Проверить дедупликацию: можно ли слать сейчас."""
    last_iso = await records.get_last_notification_sent(record_id, severity)
    if last_iso is None:
        return True
    try:
        # SQLite CURRENT_TIMESTAMP формат: 'YYYY-MM-DD HH:MM:SS' (UTC).
        last_dt = datetime.fromisoformat(last_iso.replace("Z", "")).date()
    except ValueError:
        logger.warning(
            "notifications_log: некорректный last_sent_at=%r для record_id=%s severity=%s",
            last_iso, record_id, severity,
        )
        return True
    delta = (today - last_dt).days
    return delta >= _dedup_window_days(severity)


async def run_notifications(bot: Bot) -> int:
    """Однократный проход по активным работам всех клиентов.

    Возвращает число фактически отправленных сообщений.
    """
    async with async_session_factory() as session:
        records = RecordRepository(session)
        rows = await records.get_pending_records_for_notification()
        today = datetime.now(TZ).date()
        sent = 0
        skipped_dedup = 0
        skipped_plan = 0

        for row in rows:
            # Расчёт days_left
            days_left: int | None = None
            if row["next_visit_date"]:
                try:
                    visit_date = date.fromisoformat(row["next_visit_date"])
                    days_left = (visit_date - today).days
                except (TypeError, ValueError):
                    pass

            # Расчёт rem_km — только если клиент обновлял пробег.
            rem_km: int | None = None
            if (
                row["last_known_mileage"] is not None
                and row["next_service_mileage"] is not None
            ):
                rem_km = int(row["next_service_mileage"]) - int(row["last_known_mileage"])

            severity = _classify(days_left, rem_km)
            if severity is None:
                skipped_plan += 1
                continue

            record_id = int(row["record_id"])
            if not await _should_send(records, record_id, severity, today):
                skipped_dedup += 1
                logger.info(
                    "Дедуп: пропуск record_id=%s severity=%s (последняя отправка свежее окна)",
                    record_id, severity,
                )
                continue

            text = _format_message(
                car_brand=row["car_brand"],
                service_type=row["service_type"],
                severity=severity,
                days_left=days_left,
                rem_km=rem_km,
                next_visit_date=row["next_visit_date"],
            )

            try:
                await bot.send_message(
                    row["telegram_id"],
                    text,
                    parse_mode="HTML",
                    reply_markup=_request_call_kb(),
                )
                await records.upsert_notification_log(record_id, severity)
                await session.commit()
                sent += 1
                logger.info(
                    "Уведомление отправлено user=%s record_id=%s severity=%s",
                    row["telegram_id"], record_id, severity,
                )
            except Exception:
                await session.rollback()
                logger.exception(
                    "Не удалось отправить уведомление user=%s record_id=%s",
                    row["telegram_id"], record_id,
                )

        logger.info(
            "Daily-рассылка завершена: отправлено=%s, дедуп=%s, plan=%s, всего работ=%s",
            sent, skipped_dedup, skipped_plan, len(rows),
        )
        return sent


async def daily_notifier_loop(bot: Bot, hour: int = 10, minute: int = 0) -> None:
    """Бесконечный цикл: каждый день в HH:MM (Europe/Minsk) запускает ``run_notifications``."""
    while True:
        now = datetime.now(TZ)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        logger.info(
            "Следующая проверка уведомлений: %s %s (через %.0f сек)",
            target.strftime("%Y-%m-%d %H:%M"), TZ.key, wait_seconds,
        )
        try:
            await asyncio.sleep(wait_seconds)
        except asyncio.CancelledError:
            logger.info("daily_notifier_loop отменён")
            raise
        try:
            await run_notifications(bot)
        except Exception:
            logger.exception("Ошибка в ежедневной проверке уведомлений")
