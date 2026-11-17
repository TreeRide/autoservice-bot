"""
Конфигурация бота.

Хранит токен Telegram-бота, путь к базе данных и публичный URL Mini App.
Токен читается из переменной окружения ``BOT_TOKEN`` (например, через
файл ``.env``), чтобы не попадать в репозиторий.

Список администраторов СТО **больше не хранится в коде** — он перенесён
в таблицу ``admins`` БД (см. audit L-6, :mod:`db`). Управление админами
производится напрямую запросами в БД (``add_admin`` / ``remove_admin``)
или через переменную окружения ``ADMIN_BOOTSTRAP_IDS`` для первичного
seed'а пустой таблицы.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Загружаем переменные окружения из .env, если файл существует.
load_dotenv()

# ---------------------------------------------------------------------------
# Phase 22.1 · Таймзона приложения.
# ---------------------------------------------------------------------------
# Сервер работает в UTC, а клиенты — в Минске (UTC+3, без DST с 2014 г.).
# Все новые записи в БД пишутся с локальной минской меткой времени
# (naive-строка в формате "YYYY-MM-DD HH:MM:SS", без TZ-суффикса —
# для совместимости с уже существующими колонками Text/DateTime).
# На API-layer мы навешиваем "+03:00" при отдаче на фронт.
#
# Пользователь попросил ``pytz`` в requirements (см. requirements.txt),
# но в коде используем stdlib ``zoneinfo`` — он быстрее, с нативной
# поддержкой PEP 615 и не требует внешних зависимостей. pytz оставляем
# как fallback для кода, который привык к `pytz.timezone("…")`.
APP_TZ_NAME: str = os.getenv("APP_TZ", "Europe/Minsk")
APP_TZ: ZoneInfo = ZoneInfo(APP_TZ_NAME)


def now_local() -> datetime:
    """Текущий момент времени как aware-datetime в ``APP_TZ``.

    Используем её в логике, где нужен полноценный datetime-объект
    (арифметика, сравнения). Для записи в SQLite-колонки Text —
    вызываем :func:`now_local_sql`.
    """
    return datetime.now(APP_TZ)


def now_local_sql() -> str:
    """Текущее локальное время (``APP_TZ``) как naive-строка
    ``"YYYY-MM-DD HH:MM:SS"``.

    Формат совпадает с тем, что раньше писал ``CURRENT_TIMESTAMP``
    в SQLite — миграция схемы не требуется. Tzinfo в строку НЕ кладём,
    чтобы не разрушать ORDER BY/сравнения и совместимость с legacy-кодом,
    который делает `strptime("%Y-%m-%d %H:%M:%S")`.
    """
    return now_local().strftime("%Y-%m-%d %H:%M:%S")


def localize_naive_sql(s: str | None) -> str | None:
    """Привести naive-строку из БД к ISO с TZ-суффиксом ``APP_TZ``.

    Используется при сериализации на фронт: даёт JS ``new Date(...)``
    корректную интерпретацию без гадания о таймзоне сервера.

    Принимает форматы:
      - ``"YYYY-MM-DD HH:MM:SS"`` (SQLite CURRENT_TIMESTAMP / now_local_sql)
      - ``"YYYY-MM-DD HH:MM:SS.ffffff"``
      - ISO с ``"T"`` и/или TZ-суффиксом (возвращаем без изменений)

    Если строка уже имеет TZ-суффикс (``Z`` или ``+``) — не трогаем.
    None/"" → None.
    """
    if not s:
        return None
    st = s.strip()
    if not st:
        return None
    # уже с TZ — вернуть как есть (до 19-го символа даты/времени
    # нет смысла искать, смотрим хвост).
    if st.endswith("Z") or "+" in st[10:] or "-" in st[10:]:
        return st
    # унифицировать разделитель «дата T время»
    normalized = st.replace(" ", "T", 1) if " " in st else st
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        # не парсится — вернуть как есть, фронт разберётся.
        return st
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=APP_TZ)
    else:
        dt = dt.astimezone(APP_TZ)
    return dt.isoformat()


# Legacy alias: часть старого кода импортирует ``UTC`` из config.
UTC = timezone.utc

# Токен Telegram-бота. Должен быть задан в .env как BOT_TOKEN=...
BOT_TOKEN: str | None = os.getenv("BOT_TOKEN")

# Путь к файлу базы данных SQLite.
DB_PATH: Path = Path(__file__).parent / "service_bot.db"

# Публичный HTTPS URL Telegram Mini App (FastAPI-бэкенд). Если задан —
# в reply-меню клиента кнопка «📖 Моя сервисная книжка» открывает мини-апп
# через ``KeyboardButton(web_app=WebAppInfo(url=...))``. Если переменная
# не задана — кнопка работает по-старому (текстовая выдача истории).
WEBAPP_URL: str | None = os.getenv("WEBAPP_URL")
