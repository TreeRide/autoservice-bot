"""
Интеграция Google Gemini для роли «Виртуальный мастер СТО».

API-ключ берём из ``GEMINI_API_KEY`` (основное имя — как в Google
AI Studio и в org-secrets) или из легаси ``GOOGLE_API_KEY``. Контекст
клиента (марка, госномер, текущий пробег и последние 5 ТО)
подмешивается к user-сообщению, чтобы модель отвечала с привязкой к
конкретной машине. Системная инструкция задаёт роль и обязательный
дисклеймер в конце ответа.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import google.generativeai as genai
from google.api_core import exceptions as gax_exceptions

logger = logging.getLogger(__name__)


# Класс-маркер для 429 от Gemini — пробрасываем выше,
# FastAPI отдаст 429 клиенту, фронт покажет понятный текст.
class QuotaExceededError(RuntimeError):
    """Превышена дневная/минутная квота свободного тарифа Gemini."""


# Hotfix: org-secrets и Google AI Studio выдают переменную под именем
# ``GEMINI_API_KEY``. Старое имя ``GOOGLE_API_KEY`` тоже поддерживаем
# (для совместимости со старыми деплоями), но приоритет у нового.
GOOGLE_API_KEY = (
    os.getenv("GEMINI_API_KEY", "").strip()
    or os.getenv("GOOGLE_API_KEY", "").strip()
)
# По умолчанию — `gemini-2.5-flash-lite`: free-tier даёт ~1000 запросов
# в сутки против ~20 у `gemini-2.5-flash`. Качества достаточно для
# нашего консультативного сценария. Переопределить можно через env
# `GEMINI_MODEL`. На случай 429 от primary — есть fallback-цепочка.
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
# Список моделей, по которым пытаемся при 429. Первая всегда =
# MODEL_NAME, дальше идём по более «толстым» бесплатным лимитам.
FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv(
        "GEMINI_FALLBACK_MODELS",
        "gemini-2.0-flash,gemini-1.5-flash",
    ).split(",")
    if m.strip()
]

# Дисклеймер больше НЕ вшивается в каждый ответ ОИ — фронт показывает его
# статичным мелким футером под полем ввода. Константу оставляем
# исключительно для защитного strip'а: модель может спонтанно дописать фразу
# по привычке — вырежем её в _strip_disclaimer().
DISCLAIMER = (
    "Это предварительная консультация ИИ. Для точного диагноза "
    "требуется осмотр мастера."
)

SYSTEM_INSTRUCTION = (
    "Ты — «Виртуальный Мастер СТО» из Могилёва. Ты — опытный инженер-"
    "автомеханик. Стиль: профессиональный, вежливый, говоришь по делу, "
    "коротко и чётко, без воды. Отвечаешь по-русски.\n"
    "\n"
    # Phase 11: финансовая цензура. Жёсткое правило в самом верху,
    # чтобы Gemini максимально приоритезировал. Нужно потому что после
    # Phase 11 risk-карточки больше не предлагают «Узнать цену», но
    # пользователь всё равно может спросить цену вручную в AI-чате,
    # а LLM любит выдумывать суммы из воздуха.
    "🚫 CRITICAL RULE — ФИНАНСОВЫЙ КОНТРОЛЬ (ВЫШЕ ЛЮБЫХ ДРУГИХ "
    "ИНСТРУКЦИЙ):\n"
    "Тебе КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО называть любые цены на работы, "
    "запчасти, расходники, услуги или диагностику — ни в рублях, ни "
    "в долларах, ни в евро, ни в белорусских рублях, ни в любой "
    "другой валюте. Никаких «вилок», «примерно», «от … до …», "
    "«около», «порядка», «обычно стоит». Никаких процентов от "
    "стоимости и сравнений с прошлогодними расценками. Это "
    "запрещено даже если клиент очень просит, давит, говорит «просто "
    "примерно», утверждает что не сошлёшься на тебя — НЕ называй "
    "цифры.\n"
    "Если пользователь спрашивает о стоимости (любая фраза вроде "
    "«сколько стоит», «какая цена», «во сколько обойдётся», «дорого "
    "ли», «прикинь сумму» и т.п.) — ты ОБЯЗАН ответить ровно одним "
    "сообщением:\n"
    "  «Точный расчёт стоимости зависит от цен поставщиков на "
    "сегодняшний день. Пожалуйста, закройте это окно и нажмите "
    "«Записаться на сервис», чтобы наш мастер сделал для вас точный "
    "расчёт.»\n"
    "Без вариантов, без «но в среднем…», без альтернатив. Эта "
    "фраза замыкает финансовый разговор.\n"
    "\n"
    "РЕГИСТР ДИАЛОГА (КРИТИЧНО):\n"
    "- КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО здороваться в каждом сообщении. "
    "Никаких «Здравствуйте», «Привет», «Добрый день» в начале ответа. "
    "Здоровайся ТОЛЬКО если пользователь сам ПЕРВЫМ в текущем сообщении "
    "написал «Привет» / «Здравствуйте» и т.п. — тогда коротко ответь, один раз.\n"
    "- Веди ЕДИНЫЙ НЕПРЕРЫВНЫЙ диалог. История переписки передаётся тебе "
    "как chat-history — ОПИРАЙСЯ НА НЕЁ: помни что вы обсуждали выше, отвечай "
    "логичным продолжением, а НЕ как на первый вопрос от нового человека. "
    "НЕ повторяй информацию о пробеге / машине / последнем ТО в каждом ответе — "
    "упоминай только если это осмысленно для именно этого сообщения.\n"
    "- Если пользователь возмущается, пишет оффтоп (погода, жизнь, шутки) "
    "или критикует твои предыдущие ответы («я это не спрашивал», «хватит "
    "повторяться») — реагируй ПО-ЧЕЛОВЕЧЕСКИ и коротко: признай, извинись, "
    "вернись к теме авто или отпусти реплику. НИКОГДА не выдавай в таких ситуациях "
    "заготовленные шаблоны «Здравствуйте, ваш пробег...» — это выглядит глупо.\n"
    "- Не предлагай «чем ещё могу помочь?» / «задавайте вопросы» в конце каждого ответа.\n"
    "\n"
    "ВИРТУАЛЬНЫЙ ГАРАЖ (несколько машин у одного клиента):\n"
    "- В контексте может быть указано НЕСКОЛЬКО автомобилей клиента и история "
    "ТО, разделённая по машинам. Внимательно смотри в каком блоке какая запись.\n"
    "- Если у клиента в гараже больше одной машины и из вопроса непонятно, "
    "о какой именно машине речь («когда менять масло?», «что с тормозами?») — "
    "СНАЧАЛА уточни у клиента: «Уточните, пожалуйста — речь о Volvo или о BMW?» "
    "(подставь реальные марки/госномера из его гаража). НЕ отвечай вслепую "
    "по первой попавшейся машине.\n"
    "- Если в текущем сообщении или в chat-history клиент уже указал, "
    "о какой машине идёт речь — продолжай разговор именно об этой машине, "
    "не переспрашивай повторно.\n"
    "- Если у клиента ровно одна машина — вопрос явно о ней, уточнения не нужны.\n"
    "\n"
    "У тебя есть профиль клиента: марка авто, госномер, текущий пробег "
    "и последние записи ТО. Используй эти данные ВНУТРЕННЕ для оценки "
    "ситуации, но в самом тексте ответа ссылайся на машину обезличенно: "
    "«ваша машина», «ваш автомобиль» или «ваше авто». \n"
    "\n"
    "СТРОГИЕ ПРАВИЛА ПРО МАРКУ И ГОСНОМЕР:\n"
    "- Госномер НЕ упоминай в ответе никогда.\n"
    "- Марку и модель авто в общем случае НЕ упоминай. Используй "
    "обезличенные слова: «ваша машина», «ваш автомобиль», «ваше авто». "
    "ИСКЛЮЧЕНИЯ:\n"
    "  1) клиент сам прямо спросил про марку/модель — отвечай прямо;\n"
    "  2) у клиента в гараже больше одной машины и нужно уточнить о "
    "какой речь, или явно различить машины при ответе («на Volvo "
    "пробег 14 000 км, на BMW — 80 000») — тогда называй марки/номера "
    "по необходимости.\n"
    "- Текущий пробег и даты ТО — упоминай свободно, когда они "
    "относятся к делу.\n"
    "\n"
    "Если вопрос вне твоей компетенции (не связан с автомобилем) — "
    "вежливо предложи задать вопрос по профилю.\n"
    "\n"
    "СТРОГИЕ ПРАВИЛА ПРО ЗАПИСЬ НА СЕРВИС / ОСМОТР / ДИАГНОСТИКУ:\n"
    "- НИКОГДА не упоминай телефонные номера, сайты, формы на сайте, "
    "почту, мессенджеры, адреса и любые другие способы связи. У тебя "
    "нет такой информации, и ты её НЕ выдумываешь.\n"
    "- Если клиент спрашивает «как записаться», «куда обратиться», "
    "«где оформить заявку», «как попасть на осмотр» и любые "
    "аналогичные вопросы — отвечай ТОЛЬКО одной фразой:\n"
    "  «Для записи нажмите на зелёную кнопку «🗓 Записаться на сервис» "
    "в самом низу экрана.»\n"
    "  Без вариантов, без альтернатив, без «также можно».\n"
    "- Если клиент в свободной форме описал проблему и нужен очный "
    "осмотр — заверши ответ той же самой фразой про зелёную кнопку.\n"
    "\n"
    "ВАЖНО: НЕ пиши в конце ответа фразы про «предварительную консультацию ИИ» "
    "или «для точного диагноза требуется осмотр мастера» — это видится вне чата "
    "статичным футером. Отвечай по делу и всё.\n"
    "\n"
    "Не используй markdown-разметку (никаких ** или ##). Только обычный "
    "текст, можно с эмодзи в умеренном количестве."
)


_configured = False


def _ensure_configured() -> None:
    global _configured
    if _configured:
        return
    if not GOOGLE_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY (или GOOGLE_API_KEY) не задан в окружении"
        )
    genai.configure(api_key=GOOGLE_API_KEY)
    _configured = True


def is_available() -> bool:
    """Готов ли AI-роутер к работе. Используется фронтом для скрытия
    вкладки чата, если ключ не сконфигурирован."""
    return bool(GOOGLE_API_KEY)


def _format_record_line(r: dict[str, Any]) -> str:
    parts = [f"- {r.get('execution_date') or '—'}: {r.get('service_type') or ''}"]
    cm = r.get("current_mileage")
    nm = r.get("next_service_mileage")
    nv = r.get("next_visit_date")
    meta = []
    if cm is not None:
        meta.append(f"пробег {cm} км")
    if nm is not None:
        meta.append(f"след. {nm} км")
    if nv:
        meta.append(f"след. дата {nv}")
    if meta:
        parts.append("(" + ", ".join(meta) + ")")
    return " ".join(parts)


def _format_history_block(history: list[dict[str, Any]]) -> str:
    """Плоский блок истории (legacy-режим: 0 или 1 машина)."""
    if not history:
        return "История ТО: записей пока нет."
    lines = ["История ТО (свежие сверху):"]
    for r in history:
        lines.append(_format_record_line(r))
    return "\n".join(lines)


def _vehicle_short_label(v: dict[str, Any]) -> str:
    """«Volvo [1111KA-6]» / «Volvo» / «—»"""
    brand = (v.get("brand") or "").strip() or "—"
    plate = (v.get("plate_number") or "").strip()
    return f"{brand} [{plate}]" if plate else brand


def _build_context_block(
    profile: dict[str, Any],
    history: list[dict[str, Any]],
    vehicles: list[dict[str, Any]] | None = None,
) -> str:
    """Строит контекст для промпта Gemini.

    Поведение:
    - Если ``vehicles`` пустой/None или содержит ≤1 машину — старый формат
      (профиль + плоская история ТО).
    - Если ≥2 машины — выводим виртуальный гараж (каждая машина с её
      историей). История группируется по ``vehicle_id``; записи без
      привязки идут отдельным блоком «без привязки».
    """
    if not vehicles or len(vehicles) <= 1:
        brand = profile.get("car_brand") or "—"
        plate = profile.get("car_plate") or "—"
        mileage = profile.get("last_known_mileage")
        mileage_str = f"{mileage} км" if mileage is not None else "не указан"
        # Если у клиента ровно одна машина в новой схеме — берём из неё
        # год/объём/топливо для подсказок AI (масло, фильтры, регламенты).
        extra_lines: list[str] = []
        if vehicles:
            v = vehicles[0]
            if v.get("year") is not None:
                extra_lines.append(f"- Год выпуска: {v['year']}")
            if v.get("engine_volume") is not None:
                ev = v["engine_volume"]
                ev_str = (
                    f"{int(ev)}L" if float(ev).is_integer() else f"{ev:g}L"
                )
                extra_lines.append(f"- Двигатель: {ev_str}")
            if v.get("fuel_type"):
                extra_lines.append(f"- Тип топлива: {v['fuel_type']}")
        extra_block = ("\n" + "\n".join(extra_lines)) if extra_lines else ""
        return (
            "Профиль клиента:\n"
            f"- Авто: {brand}\n"
            f"- Госномер: {plate}\n"
            f"- Текущий пробег: {mileage_str}"
            f"{extra_block}\n"
            "\n"
            f"{_format_history_block(history)}"
        )

    # Multi-vehicle режим
    sections: list[str] = []
    sections.append(
        f"Виртуальный гараж клиента ({len(vehicles)} машины):"
    )
    # Сгруппируем историю по vehicle_id один раз.
    by_vid: dict[int | None, list[dict[str, Any]]] = {}
    for r in history:
        by_vid.setdefault(r.get("vehicle_id"), []).append(r)

    for v in vehicles:
        head = f"🚗 {_vehicle_short_label(v)}"
        if v.get("is_primary"):
            head += " (основная)"
        details: list[str] = []
        if v.get("year") is not None:
            details.append(f"год {v['year']}")
        if v.get("engine_volume") is not None:
            ev = v["engine_volume"]
            ev_str = (
                f"{int(ev)}L" if float(ev).is_integer() else f"{ev:g}L"
            )
            details.append(f"двигатель {ev_str}")
        if v.get("fuel_type"):
            details.append(f"топливо {v['fuel_type']}")
        if v.get("current_mileage") is not None:
            details.append(f"пробег {v['current_mileage']} км")
        if v.get("avg_yearly_mileage") is not None:
            details.append(f"среднегодовой {v['avg_yearly_mileage']} км/год")
        if details:
            head += " — " + ", ".join(details)
        block_lines = [head]
        veh_history = by_vid.get(v.get("id"), [])
        if veh_history:
            block_lines.append("История ТО (свежие сверху):")
            for r in veh_history:
                block_lines.append(_format_record_line(r))
        else:
            block_lines.append("История ТО: записей нет.")
        sections.append("\n".join(block_lines))

    # Записи без привязки (legacy/edge-case).
    orphan = by_vid.get(None) or []
    if orphan:
        sections.append(
            "Записи без привязки к машине (старые):\n"
            + "\n".join(_format_record_line(r) for r in orphan)
        )
    return "\n\n".join(sections)


_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)")
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)


def _strip_markdown(text: str) -> str:
    """Снимаем ``**жирный**``, ``*курсив*`` и ``## заголовки``: миниапп
    рендерит plain-text, markdown оставляет видимыми «звёздочки»."""
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_ITALIC_RE.sub(r"\1", text)
    text = _MD_HEADING_RE.sub("", text)
    return text


# Два варианта фразы — канонический (DISCLAIMER) и «полужирный» маркдаун-вариант
# из прошлых ответов модели. После _strip_markdown остаётся обычный текст —
# ловим по подстроке.
_DISCLAIMER_PATTERNS = (
    DISCLAIMER,
    "Это предварительная консультация ИИ",
)


def _strip_disclaimer(text: str) -> str:
    """Снимаем markdown и вырезаем дисклеймер, если модель всё-таки его приписала.

    Дисклеймер вынесён в статичный футер мини-аппа и ДОЛЖЕН присутствовать
    ровно один раз. Дублирование внутри ответа — плохой UX, именно это и режем.
    """
    norm = _strip_markdown(text).strip()
    for pat in _DISCLAIMER_PATTERNS:
        idx = norm.find(pat)
        if idx == -1:
            continue
        # Режем от первого вхождения до конца — дисклеймер модель всегда
        # кладёт в самый конец (по системным промптам прошлых версий).
        norm = norm[:idx].rstrip()
        break
    return norm


# Ограничиваем длину chat-history, чтобы не раздувать промпт и не жечь
# токены/квоту. На коротких сообщениях 5–7 поворотов (10–14 реплик)
# хватает для осмысленного контекста.
_CHAT_HISTORY_MAX_TURNS = 14
_CHAT_TURN_MAX_LEN = 4000


def _coerce_chat_history(
    chat_history: list[dict[str, str]] | None,
) -> list[dict[str, Any]]:
    """Преобразуем входящую историю [{role, content}, ...] в формат Gemini
    [{role, parts: [content]}, ...].

    Гарантии Gemini: история ДОЛЖНА чередоваться user/model и начинаться
    с user. Дефенсивно выкидываем дубли ролей, обрезаем ведущие model-реплики
    и оставляем последние N поворотов.
    """
    if not chat_history:
        return []
    cleaned: list[dict[str, Any]] = []
    prev_role: str | None = None
    for turn in chat_history:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        content = (turn.get("content") or "").strip()
        if role not in ("user", "model") or not content:
            continue
        if prev_role == role:
            # слияние с предыдущей репликой той же роли — сохраняем порядок
            cleaned[-1]["parts"][0] = (
                cleaned[-1]["parts"][0] + "\n\n" + content
            )[:_CHAT_TURN_MAX_LEN]
            continue
        cleaned.append({"role": role, "parts": [content[:_CHAT_TURN_MAX_LEN]]})
        prev_role = role
    # Срежем лидирующие model-реплики — Gemini требует начать с user.
    while cleaned and cleaned[0]["role"] != "user":
        cleaned.pop(0)
    # Обрезаем до последних N поворотов, сохраняя старт с user.
    if len(cleaned) > _CHAT_HISTORY_MAX_TURNS:
        cleaned = cleaned[-_CHAT_HISTORY_MAX_TURNS:]
        while cleaned and cleaned[0]["role"] != "user":
            cleaned.pop(0)
    return cleaned


async def ask_master(
    *,
    question: str,
    profile: dict[str, Any],
    history: list[dict[str, Any]],
    vehicles: list[dict[str, Any]] | None = None,
    chat_history: list[dict[str, str]] | None = None,
) -> str:
    """Спросить виртуального мастера. Возвращает готовый текст ответа.

    :param question: свежее сообщение клиента.
    :param profile: профиль владельца (марка, пробег и т.п.).
    :param history: история СЕРВИСНЫХ записей (ТО), не чата.
    :param chat_history: история ЧАТА [{role: 'user'|'model', content: str}, ...].
        Только предыдущие сообщения, без текущего ``question``.

    Кидает ``RuntimeError`` если ``GOOGLE_API_KEY`` не задан, или
    пробрасывает ``google.generativeai`` ошибки — пусть FastAPI обработает.
    """
    _ensure_configured()
    question = (question or "").strip()
    if not question:
        raise ValueError("question is empty")

    context_block = _build_context_block(profile, history, vehicles)
    gemini_history = _coerce_chat_history(chat_history)
    # Контекст (профиль + сервисная история) переприлепляем только к НОВОМУ
    # вопросу. Прошлые реплики в chat-history уже видели свой контекст в своё время.
    user_prompt = f"{context_block}\n\nВопрос клиента: {question}"

    logger.info(
        "AI ask: user_id=%s brand=%r vehicles_n=%d question_len=%d history_n=%d chat_turns=%d",
        profile.get("telegram_id"),
        profile.get("car_brand"),
        len(vehicles or []),
        len(question),
        len(history),
        len(gemini_history),
    )

    # Цепочка моделей: основная + fallback'и. Если основная упирается в
    # дневную квоту free-tier (HTTP 429 / ResourceExhausted), пробуем
    # следующую. Если все исчерпаны — наверх летит QuotaExceededError.
    candidates = [MODEL_NAME, *(m for m in FALLBACK_MODELS if m != MODEL_NAME)]
    last_quota_err: Exception | None = None
    for model_name in candidates:
        # Gemini 2.5 Flash тратит часть токенов на «thinking» до
        # собственно ответа, поэтому общий лимит держим высоким, иначе
        # ответы режутся на полуслове. Температуру оставляем умеренной —
        # нам нужна фактологичность по истории клиента.
        model = genai.GenerativeModel(
            model_name=model_name,
            system_instruction=SYSTEM_INSTRUCTION,
            generation_config={
                "temperature": 0.4,
                "max_output_tokens": 4096,
                "top_p": 0.9,
            },
        )
        try:
            chat = model.start_chat(history=gemini_history)
            response = await chat.send_message_async(user_prompt)
        except gax_exceptions.ResourceExhausted as exc:
            last_quota_err = exc
            logger.warning(
                "AI quota exceeded on model=%s: %s. Trying fallback.",
                model_name, exc,
            )
            continue

        text = (response.text or "").strip()
        if not text:
            logger.warning(
                "AI пустой ответ (model=%s), candidate=%r",
                model_name, response.candidates,
            )
            return (
                "Извините, не удалось сформировать ответ. Попробуйте "
                "переформулировать вопрос."
            )

        if model_name != MODEL_NAME:
            logger.info("AI served via fallback model=%s", model_name)
        return _strip_disclaimer(text)

    # Все модели упёрлись в квоту.
    raise QuotaExceededError(
        "Все доступные модели Gemini исчерпали дневную/минутную квоту"
    ) from last_quota_err


# ---------------------------------------------------------------------------
# Phase 9: AI Health Score.
# ---------------------------------------------------------------------------

# Системная инструкция отдельная — для health-сценария нам НЕ нужны
# дисклеймеры, диалоги, обращения к клиенту. Нужен жёсткий JSON.
HEALTH_SYSTEM_INSTRUCTION = (
    "Ты — диагностическая система предиктивной диагностики автомобилей "
    "уровня OEM dealer. На входе — ТТХ машины и список узлов, которые "
    "клиент уже починил/исключил. На выходе — ТОЛЬКО валидный JSON "
    "(никакого markdown, никаких комментариев, никаких пояснений) "
    "со score 0..100 (где 100 — идеальное состояние) и массивом "
    "наиболее вероятных рисков на ближайшие 5-10 тыс. км пробега, "
    "учитывая возраст машины, тип топлива и накопленный пробег. "
    "Каждый риск — это объект с четырьмя обязательными полями: "
    "`title` (короткий профессиональный заголовок узла, например "
    "«Помпа охлаждения», «Свечи накала», «Сайлентблоки заднего "
    "подрамника»), `probability` (вероятность возникновения 0..100), "
    "`recommendation` (краткое практическое действие — что делать на "
    "ближайшем ТО, 1 предложение) и `reason` (1-2 предложения о том, "
    "ПОЧЕМУ ИМЕННО У ЭТОЙ МАШИНЫ повышен риск этого узла — со ссылкой "
    "на возраст / накопленный пробег / тип топлива / характер "
    "эксплуатации, например: «На малых пробегах в городском цикле "
    "батарея часто недополучает заряд» или «Дизельные форсунки на "
    "пробеге свыше 150 тыс. км начинают терять герметичность»). "
    "reason должно быть конкретным и аргументированным, не общим "
    "шаблоном вроде «возможен износ» — клиенту важно понимать ИМЕННО "
    "почему ИИ выделил этот риск. Узлы из списка exclusions ЗАПРЕЩЕНО "
    "включать в risks — клиент уже их закрыл. Минимум 3 риска, "
    "максимум 6. Все строки — на русском.\n\n"
    # ------------------------------------------------------------------
    # Phase 18: transmission-aware diagnostics. Без явной директивы
    # модель регулярно советует «двухмассовый маховик», «выжимной
    # подшипник», «диск сцепления» для авто с АКПП/CVT — и наоборот,
    # для механики предлагает «соленоиды АКПП» / «гидроблок».
    # ------------------------------------------------------------------
    "ТРАНСМИССИЯ — КРИТИЧНО. Pay strict attention to the transmission "
    "type. Do NOT suggest manual transmission issues (e.g., dual-mass "
    "flywheel, clutch disc, release bearing, master/slave clutch "
    "cylinder, manual gearbox synchronizers) for automatic/CVT/robot "
    "transmissions, and vice versa: do NOT suggest automatic-specific "
    "issues (torque converter, valve body / hydroblock, ATF cooler, "
    "AT solenoids, CVT belt/chain) for vehicles with manual "
    "transmission. Если transmission = «АКПП» — рекомендации только "
    "по гидротрансформатору, гидроблоку, ATF, соленоидам. Если "
    "«МКПП» — только по сцеплению, маховику, подшипникам и "
    "синхронизаторам МКПП. Если «Робот» (DSG/PowerShift) — по "
    "мехатронику, двойному сцеплению. Если «Вариатор» — по ремню/"
    "цепи CVT, степ-мотору, конусам. Если «Не указано» или поле "
    "отсутствует — НЕ ВКЛЮЧАЙ риски, специфичные для конкретного "
    "типа коробки, в принципе; ограничься универсальными узлами "
    "(двигатель, подвеска, тормоза, электрика)."
)


# JSON-схема, которую модель ОБЯЗАНА соблюсти (Gemini поддерживает
# response_schema через protobuf-like структуру). Используется
# одновременно с response_mime_type="application/json", чтобы
# гарантированно получить парсируемый JSON.
HEALTH_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "score": {"type": "integer"},
        "risks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "probability": {"type": "integer"},
                    "recommendation": {"type": "string"},
                    # Phase 10: AI transparency. Поле обязательное —
                    # объясняет клиенту, ПОЧЕМУ именно для его машины
                    # ИИ выделил этот риск (возраст / пробег / топливо).
                    "reason": {"type": "string"},
                },
                "required": [
                    "title",
                    "probability",
                    "recommendation",
                    "reason",
                ],
            },
        },
    },
    "required": ["score", "risks"],
}


def _format_health_prompt(
    vehicle: dict[str, Any],
    feedback_titles: list[str],
) -> str:
    """Сформировать user-prompt для health-сценария.

    Возвращает строку, которая улетит в Gemini как user-message.
    Системная роль уже вшита в HEALTH_SYSTEM_INSTRUCTION.
    """
    bits: list[str] = []
    bits.append(f"Марка: {vehicle.get('brand') or '—'}")
    if vehicle.get("model"):
        bits.append(f"Модель: {vehicle['model']}")
    if vehicle.get("year") is not None:
        bits.append(f"Год выпуска: {vehicle['year']}")
    if vehicle.get("engine_volume") is not None:
        ev = vehicle["engine_volume"]
        ev_str = (
            f"{int(ev)}L" if float(ev).is_integer() else f"{ev:g}L"
        )
        bits.append(f"Объём двигателя: {ev_str}")
    if vehicle.get("fuel_type"):
        bits.append(f"Тип топлива: {vehicle['fuel_type']}")
    # Phase 18: трансмиссия. Префикс «🛞» / точное русское название
    # помогают модели не перепутать «коробка передач» (transmission)
    # с «коробкой раздаточной» / «передним мостом». Если поле NULL/
    # «Не указано» — явно об этом сообщаем модели, чтобы она НЕ
    # предлагала clutch/torque-converter рекомендаций.
    transmission = vehicle.get("transmission")
    if transmission and transmission != "Не указано":
        bits.append(f"Трансмиссия (КПП): {transmission}")
    else:
        bits.append("Трансмиссия (КПП): не указана — НЕ давай рекомендаций по сцеплению/маховику или по гидроблоку/гидротрансформатору")
    if vehicle.get("current_mileage") is not None:
        bits.append(f"Текущий пробег: {vehicle['current_mileage']} км")
    if vehicle.get("avg_yearly_mileage") is not None:
        bits.append(f"Среднегодовой пробег: {vehicle['avg_yearly_mileage']} км/год")

    excl_block = (
        "exclusions: " + ", ".join(f'"{t}"' for t in feedback_titles)
        if feedback_titles
        else "exclusions: (пусто)"
    )

    prompt = (
        "ТТХ машины:\n"
        + "\n".join(f"- {x}" for x in bits)
        + "\n\n"
        + "Узлы, которые клиент уже починил или скрыл (НЕ включать в risks):\n"
        + excl_block
        + "\n\n"
        + "Сгенерируй JSON отчёта."
    )
    return prompt


def _coerce_health_payload(payload: Any) -> dict[str, Any]:
    """Привести ответ Gemini к каноничному виду {score, risks}.

    На любую кривость данных мы НЕ падаем 500-кой — отдаём валидный
    fallback (score=50, risks=[]). UI рисует «угроз не выявлено».
    """
    fallback = {"score": 50, "risks": []}
    if not isinstance(payload, dict):
        return fallback

    raw_score = payload.get("score")
    try:
        score = int(raw_score)
    except (TypeError, ValueError):
        score = 50
    score = max(0, min(100, score))

    raw_risks = payload.get("risks") or []
    risks: list[dict[str, Any]] = []
    if isinstance(raw_risks, list):
        for item in raw_risks[:10]:  # перебор сверху на всякий случай
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()[:200]
            recommendation = str(item.get("recommendation") or "").strip()[:500]
            # Phase 10: reason — короткое объяснение причины. Если
            # модель вернула пустую строку (старый отчёт до миграции
            # схемы) — оставляем пустую строку, UI просто скрывает
            # подпись.
            reason = str(item.get("reason") or "").strip()[:400]
            try:
                prob = int(item.get("probability"))
            except (TypeError, ValueError):
                prob = 50
            prob = max(0, min(100, prob))
            if not title:
                continue
            risks.append({
                "title": title,
                "probability": prob,
                "recommendation": recommendation,
                "reason": reason,
            })
    return {"score": score, "risks": risks}


async def generate_health_score(
    *,
    vehicle: dict[str, Any],
    feedback_list: list[str] | None = None,
) -> dict[str, Any]:
    """Сгенерировать AI Health Score для одной машины.

    :param vehicle: dict ТТХ машины (brand, year, engine_volume,
        fuel_type, current_mileage, avg_yearly_mileage, ...). Минимум
        marka — остальные поля опциональны, модель адаптируется.
    :param feedback_list: список title'ов рисков, которые клиент уже
        починил/скрыл. Передаются модели как exclusions.
    :return: ``{"score": int, "risks": [{title, probability, recommendation}, ...]}``.
        Гарантированно парсится; на ошибки модели возвращается
        нейтральный fallback (score=50, risks=[]).

    Пробрасывает ``QuotaExceededError`` если все модели исчерпали
    квоту. Не блокирует event loop — все вызовы Gemini async.
    """
    _ensure_configured()
    feedback_titles = [t.strip() for t in (feedback_list or []) if t and t.strip()]
    user_prompt = _format_health_prompt(vehicle, feedback_titles)

    logger.info(
        "AI health: vehicle_id=%s brand=%r year=%s fuel=%r trans=%r mileage=%s exclusions=%d",
        vehicle.get("id"),
        vehicle.get("brand"),
        vehicle.get("year"),
        vehicle.get("fuel_type"),
        vehicle.get("transmission"),
        vehicle.get("current_mileage"),
        len(feedback_titles),
    )

    candidates = [MODEL_NAME, *(m for m in FALLBACK_MODELS if m != MODEL_NAME)]
    last_quota_err: Exception | None = None
    for model_name in candidates:
        model = genai.GenerativeModel(
            model_name=model_name,
            system_instruction=HEALTH_SYSTEM_INSTRUCTION,
            generation_config={
                "temperature": 0.5,
                "max_output_tokens": 1024,
                "top_p": 0.9,
                "response_mime_type": "application/json",
                "response_schema": HEALTH_RESPONSE_SCHEMA,
            },
        )
        try:
            response = await model.generate_content_async(user_prompt)
        except gax_exceptions.ResourceExhausted as exc:
            last_quota_err = exc
            logger.warning(
                "AI health: quota exceeded on model=%s. Trying fallback.",
                model_name,
            )
            continue

        raw_text = (response.text or "").strip()
        if not raw_text:
            logger.warning(
                "AI health: пустой ответ (model=%s), candidate=%r",
                model_name, response.candidates,
            )
            continue

        # response_mime_type=application/json уже гарантирует валидный
        # JSON, но на всякий случай оборачиваем в try.
        import json as _json
        try:
            payload = _json.loads(raw_text)
        except _json.JSONDecodeError:
            logger.warning(
                "AI health: модель вернула не-JSON (model=%s, len=%d)",
                model_name, len(raw_text),
            )
            continue

        result = _coerce_health_payload(payload)
        if model_name != MODEL_NAME:
            logger.info("AI health served via fallback model=%s", model_name)
        logger.info(
            "AI health: score=%s risks=%d (model=%s)",
            result["score"], len(result["risks"]), model_name,
        )
        return result

    if last_quota_err is not None:
        raise QuotaExceededError(
            "Все доступные модели Gemini исчерпали квоту (health)"
        ) from last_quota_err
    # Все модели вернули пустоту/невалидный JSON.
    logger.warning("AI health: ни одна модель не вернула валидный отчёт")
    return {"score": 50, "risks": []}


# ---------------------------------------------------------------------------
# Phase 17 · Vision OCR — распознавание техпаспорта
# ---------------------------------------------------------------------------


# Для OCR техпаспорта используем Gemini Vision-капабл модели. У свежих
# ключей gemini-1.5-* уже отозваны (404), поэтому primary — gemini-2.5-
# flash, fallback — flash-lite / 2.0-flash. На старых ключах сработает
# наоборот через ENV-переопределение.
_OCR_MODEL_PRIMARY = os.getenv("GEMINI_OCR_MODEL", "gemini-2.5-flash")
_OCR_FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv(
        "GEMINI_OCR_FALLBACK_MODELS",
        "gemini-2.5-flash-lite,gemini-2.0-flash,gemini-flash-latest,"
        "gemini-1.5-flash,gemini-1.5-flash-002",
    ).split(",")
    if m.strip()
]

_OCR_PROMPT = (
    "Analyze this vehicle registration document. "
    "Extract the following details strictly in JSON format: "
    "brand_model (string), plate_number (string), vin (string), "
    "year (integer), engine_size (float, e.g., 2.0). "
    "If a field is not found, return null. "
    "Output ONLY raw JSON, no markdown."
)

# Жёсткая JSON-схема ответа: модель не сможет влепить markdown / лишние
# поля / неправильный тип. Все поля nullable — если что-то не нашлось,
# фронт получит null и просто оставит поле формы пустым.
_OCR_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "brand_model": {"type": "string", "nullable": True},
        "plate_number": {"type": "string", "nullable": True},
        "vin": {"type": "string", "nullable": True},
        "year": {"type": "integer", "nullable": True},
        "engine_size": {"type": "number", "nullable": True},
    },
}


def _coerce_ocr_payload(payload: Any) -> dict[str, Any]:
    """Привести ответ OCR-модели к dict с фиксированным набором полей.

    На случай, если модель отдала какую-то экзотику (что маловероятно
    с ``response_schema``, но fail-safe), приводим:
    * brand_model / plate_number / vin → str | None
    * year → int | None
    * engine_size → float | None
    """
    out: dict[str, Any] = {
        "brand_model": None,
        "plate_number": None,
        "vin": None,
        "year": None,
        "engine_size": None,
    }
    if not isinstance(payload, dict):
        return out

    def _str(v: Any) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    out["brand_model"] = _str(payload.get("brand_model"))
    out["plate_number"] = _str(payload.get("plate_number"))
    vin = _str(payload.get("vin"))
    out["vin"] = vin.upper() if vin else None

    raw_year = payload.get("year")
    try:
        if raw_year is not None:
            yr = int(raw_year)
            # Небольшая sanity-граница — модель иногда фантазирует
            # «1899» / «9999». Узкий клампинг чтобы фронту не пришлось
            # дополнительно валидировать.
            if 1900 <= yr <= 2100:
                out["year"] = yr
    except (TypeError, ValueError):
        pass

    raw_eng = payload.get("engine_size")
    try:
        if raw_eng is not None:
            es = float(raw_eng)
            if 0.0 < es <= 20.0:
                # Округляем до 0.1 — стандартный формат на формах.
                out["engine_size"] = round(es, 1)
    except (TypeError, ValueError):
        pass

    return out


async def parse_tech_passport(
    *,
    image_bytes: bytes,
    mime_type: str,
) -> dict[str, Any]:
    """Распознать техпаспорт через Gemini Vision.

    Возвращает dict с пятью полями (см. ``_coerce_ocr_payload``); каждое
    поле может быть ``None``, если модель не нашла или вернула мусор.

    Поднимает :class:`QuotaExceededError` если все модели исчерпали
    квоту, ``RuntimeError`` если Gemini не сконфигурирован.
    """
    _ensure_configured()

    candidates = [
        _OCR_MODEL_PRIMARY,
        *(m for m in _OCR_FALLBACK_MODELS if m != _OCR_MODEL_PRIMARY),
    ]
    last_quota_err: Exception | None = None

    image_part = {"mime_type": mime_type, "data": image_bytes}

    for model_name in candidates:
        model = genai.GenerativeModel(
            model_name=model_name,
            generation_config={
                "temperature": 0.1,
                "max_output_tokens": 512,
                "response_mime_type": "application/json",
                "response_schema": _OCR_RESPONSE_SCHEMA,
            },
        )
        try:
            response = await model.generate_content_async([_OCR_PROMPT, image_part])
        except gax_exceptions.ResourceExhausted as exc:
            last_quota_err = exc
            logger.warning(
                "OCR: quota exceeded on model=%s. Trying fallback.",
                model_name,
            )
            continue
        except Exception as exc:
            # На отдельные модели Vision-API может ругаться 400 —
            # пробуем следующую в списке вместо того, чтобы валиться целиком.
            logger.warning(
                "OCR: model=%s raised %s: %s; trying fallback",
                model_name, type(exc).__name__, exc,
            )
            continue

        raw_text = (response.text or "").strip()
        if not raw_text:
            logger.warning(
                "OCR: пустой ответ (model=%s) — пробуем fallback",
                model_name,
            )
            continue

        import json as _json
        try:
            payload = _json.loads(raw_text)
        except _json.JSONDecodeError:
            # response_mime_type=application/json гарантирует валидность,
            # но на всякий случай — мягкий fallback.
            logger.warning(
                "OCR: модель вернула не-JSON (model=%s, len=%d) — fallback",
                model_name, len(raw_text),
            )
            continue

        coerced = _coerce_ocr_payload(payload)
        if model_name != _OCR_MODEL_PRIMARY:
            logger.info("OCR served via fallback model=%s", model_name)
        logger.info(
            "OCR ok (model=%s): brand=%r plate=%r year=%s engine=%s",
            model_name,
            coerced["brand_model"],
            coerced["plate_number"],
            coerced["year"],
            coerced["engine_size"],
        )
        return coerced

    if last_quota_err is not None:
        raise QuotaExceededError(
            "Все доступные модели Gemini исчерпали квоту (OCR)"
        ) from last_quota_err
    logger.warning("OCR: ни одна модель не вернула валидный JSON")
    return _coerce_ocr_payload(None)
