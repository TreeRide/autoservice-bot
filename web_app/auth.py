"""
Валидация ``Telegram.WebApp.initData``.

Спецификация: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

Алгоритм:
1. ``initData`` приходит как query-string ``user=…&auth_date=…&hash=…&…``.
2. Берём все пары ``key=value`` кроме ``hash``, сортируем по ключу,
   соединяем строкой ``key=value`` через ``\\n`` → получаем
   ``data_check_string``.
3. ``secret_key = HMAC_SHA256("WebAppData", bot_token)``.
4. Ожидаемый hash = ``HMAC_SHA256(secret_key, data_check_string).hexdigest()``.
5. Сравниваем с переданным ``hash`` (constant-time).
6. Дополнительно проверяем свежесть по ``auth_date`` (по умолчанию 24ч).

Обходных путей (dev-bypass, ``?dev=<id>`` и т.п.) в модуле НЕТ:
аутентификация возможна только через валидный initData с проверкой HMAC.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

logger = logging.getLogger(__name__)


MAX_AGE_SECONDS = 24 * 60 * 60  # 24 часа — после этого initData считается просроченным


@dataclass(frozen=True)
class TelegramUser:
    id: int
    first_name: str
    last_name: str | None
    username: str | None
    language_code: str | None


class InitDataInvalid(Exception):
    """Ошибка валидации initData."""


def _build_data_check_string(parsed: list[tuple[str, str]]) -> str:
    """Собрать строку для HMAC: пары ``key=value`` кроме ``hash``,
    отсортированные по ключу, через ``\\n``."""
    pairs = [(k, v) for k, v in parsed if k != "hash"]
    pairs.sort(key=lambda kv: kv[0])
    return "\n".join(f"{k}={v}" for k, v in pairs)


def validate_init_data(init_data: str, bot_token: str) -> TelegramUser:
    """Проверить подпись и вернуть :class:`TelegramUser` или поднять
    :class:`InitDataInvalid`."""
    if not init_data:
        raise InitDataInvalid("empty init_data")
    parsed = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    pairs = dict(parsed)

    received_hash = pairs.get("hash")
    if not received_hash:
        raise InitDataInvalid("missing hash")

    auth_date_raw = pairs.get("auth_date")
    if not auth_date_raw or not auth_date_raw.isdigit():
        raise InitDataInvalid("missing/invalid auth_date")
    auth_date = int(auth_date_raw)
    if abs(time.time() - auth_date) > MAX_AGE_SECONDS:
        raise InitDataInvalid("init_data expired")

    data_check_string = _build_data_check_string(parsed)
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        # Hash mismatch — событие исключительное и почти всегда
        # означает рассинхрон BOT_TOKEN на бэкенде с тем ботом, через
        # который открыт Mini App. Логируем без самого токена и без
        # данных пользователя: только id бота, выведенный из токена,
        # и префиксы expected/received хэшей по 12 символов — этого
        # достаточно для диагностики, но недостаточно для подделки.
        bot_id_hint = bot_token.split(":", 1)[0] if ":" in bot_token else "?"
        logger.warning(
            "validate_init_data: hash mismatch; bot_id=%s expected=%s… received=%s…",
            bot_id_hint,
            expected_hash[:12],
            received_hash[:12],
        )
        raise InitDataInvalid("hash mismatch")

    user_raw = pairs.get("user")
    if not user_raw:
        raise InitDataInvalid("missing user payload")
    try:
        user = json.loads(user_raw)
    except json.JSONDecodeError as exc:
        raise InitDataInvalid(f"user payload not JSON: {exc}") from exc

    user_id = user.get("id")
    if not isinstance(user_id, int):
        raise InitDataInvalid("user.id is not int")

    return TelegramUser(
        id=user_id,
        first_name=user.get("first_name") or "",
        last_name=user.get("last_name"),
        username=user.get("username"),
        language_code=user.get("language_code"),
    )
