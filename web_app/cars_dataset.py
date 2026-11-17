"""
Phase 34 · Реляционный каталог автомобилей.

Backend-источник правды для каскадных дропдаунов в WebApp:

    Brand → Model → Year → Engine (Volume + Fuel) → Transmission

Используется фронтом через GET /api/cars/* (см. main.py). Бренд+модель
кэшируются из ``static/cars_db.json`` (151 бренд × ~5000 моделей с
chassis-кодами). Year/Engine/Fuel/Transmission — преднастроенные
универсальные списки (модель-специфичных продакшен-дат у нас нет,
поэтому используем разумный диапазон 1990 .. current_year).

«Safety valve»: каждый список в API сопровождается флагом
``allow_other=true``. Фронт отрисовывает «Other / Ручной ввод» как
последний пункт; пользователь может ввести произвольное значение, и
бэкенд (Pydantic в /api/vehicles + /api/admin/create-client-and-vehicle)
сохранит его как свободную строку без 422-ошибок.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)


# Источник Brand → [Model, ...] (тот же файл, что и frontend).
# Кэшируем при старте, чтобы не парсить JSON на каждый GET.
_CARS_DB_PATH: Path = (
    Path(__file__).resolve().parent / "static" / "cars_db.json"
)


# Преднастроенные универсальные списки.
# Год: текущий год + 50 предыдущих. Самый свежий первым (UI: «выпуск 2025»
# — это новинка, она же чаще всего нужна).
_YEAR_HISTORY_DEPTH: int = 50

# Объёмы двигателя в литрах. 0.6 — ниши вроде ZAZ, до 8.0 — V12 luxury.
# Покрывает 99% серийных авто.
_ENGINE_VOLUMES_L: tuple[float, ...] = (
    0.6, 0.8, 1.0, 1.2, 1.3, 1.4, 1.5, 1.6, 1.8, 2.0,
    2.2, 2.4, 2.5, 2.7, 2.8, 3.0, 3.2, 3.5, 3.6, 3.8,
    4.0, 4.4, 4.6, 4.8, 5.0, 5.5, 5.7, 6.0, 6.2, 6.3, 6.5, 7.0, 8.0,
)

# Топливо. Каноничные 4 + явный «Не указано».
# Free-text fallback включается через "Other / Ручной ввод" в UI.
_FUEL_TYPES: tuple[str, ...] = (
    "Бензин", "Дизель", "Гибрид", "Электро", "LPG / Газ",
)

# Трансмиссия. Каноничные 4 + явный «Не указано».
_TRANSMISSION_TYPES: tuple[str, ...] = (
    "АКПП", "МКПП", "Робот", "Вариатор",
)


@lru_cache(maxsize=1)
def _load_brand_model_index() -> dict[str, list[str]]:
    """Прочитать ``cars_db.json`` один раз и закэшировать.

    Возвращает словарь ``{brand: [model, ...]}``. При отсутствии файла
    или кривом JSON возвращает пустой dict + warning в логи (не ломаем
    приложение — просто работаем без подсказок брендов).
    """
    try:
        with _CARS_DB_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "[cars_dataset] не удалось загрузить %s: %s — каскад будет пуст",
            _CARS_DB_PATH, exc,
        )
        return {}

    if not isinstance(data, dict):
        logger.warning(
            "[cars_dataset] ожидался dict в %s, получили %s",
            _CARS_DB_PATH, type(data).__name__,
        )
        return {}

    cleaned: dict[str, list[str]] = {}
    for brand, models in data.items():
        if not isinstance(brand, str) or not brand.strip():
            continue
        if not isinstance(models, list):
            continue
        cleaned[brand.strip()] = sorted(
            {str(m).strip() for m in models if isinstance(m, str) and m.strip()},
            key=lambda s: s.lower(),
        )
    return cleaned


def get_brands() -> list[str]:
    """Все известные марки, отсортированные по алфавиту (case-insensitive)."""
    return sorted(_load_brand_model_index().keys(), key=lambda s: s.lower())


def get_models(brand: str) -> list[str]:
    """Все модели для марки. Поиск по точному совпадению brand (с учётом
    регистра — UI отдаёт ровно ту строку, что вернул /api/cars/brands).

    Если бренда нет в каталоге — пустой список (фронт всё равно
    разрешит «Other / Ручной ввод»).
    """
    brand_norm = (brand or "").strip()
    if not brand_norm:
        return []
    idx = _load_brand_model_index()
    # Точное совпадение → быстрый путь.
    if brand_norm in idx:
        return list(idx[brand_norm])
    # Попытка case-insensitive matching на случай «BMW» vs «Bmw».
    lower_map = {k.lower(): k for k in idx}
    canonical = lower_map.get(brand_norm.lower())
    return list(idx[canonical]) if canonical else []


def get_years(brand: str | None = None, model: str | None = None) -> list[int]:
    """Годы выпуска (свежие первыми).

    Универсальный диапазон ``current_year .. current_year - _YEAR_HISTORY_DEPTH``.
    Параметры brand/model пока не сужают выдачу (датасета с production-датами
    у нас нет), но включены в сигнатуру под будущее enrichment.
    """
    _ = brand, model  # reserved for future per-model filtering
    current = datetime.utcnow().year
    return [current - i for i in range(_YEAR_HISTORY_DEPTH + 1)]


def get_engine_volumes(
    brand: str | None = None,
    model: str | None = None,
    year: int | None = None,
) -> list[float]:
    """Распространённые объёмы двигателя в литрах (по возрастанию)."""
    _ = brand, model, year  # reserved for future per-trim filtering
    return list(_ENGINE_VOLUMES_L)


def get_fuel_types() -> list[str]:
    """Поддерживаемые типы топлива (UI добавляет «Other / Ручной ввод»)."""
    return list(_FUEL_TYPES)


def get_transmissions() -> list[str]:
    """Поддерживаемые типы трансмиссии (UI добавляет «Other / Ручной ввод»)."""
    return list(_TRANSMISSION_TYPES)
