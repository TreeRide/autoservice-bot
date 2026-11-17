"""
Управление списком администраторов из чата.

Команды:

* ``/add_admin <telegram_id>`` — добавить нового администратора.
* ``/del_admin <telegram_id>`` — удалить администратора.

Защита:

* Оба обработчика обёрнуты фильтром :class:`filters.IsAdmin` — вызвать
  команду может только пользователь, уже присутствующий в таблице
  ``admins`` (см. :mod:`db`).
* Запрещено удалять самого себя — иначе админ может случайно потерять
  доступ к панели управления.
* Запрещено удалять последнего администратора — иначе бот останется
  без доступной админ-панели.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from database.repositories import AdminRepository
from filters import IsAdmin

logger = logging.getLogger(__name__)

router = Router(name="admin-admin-management")


def _parse_target_id(args: str | None) -> int | None:
    """Извлечь ``int`` Telegram-ID из аргументов команды."""
    if not args:
        return None
    token = args.strip().split()[0]
    try:
        return int(token)
    except ValueError:
        return None


@router.message(Command("add_admin"), IsAdmin())
async def cmd_add_admin(
    message: Message, command: CommandObject, session: AsyncSession
) -> None:
    """``/add_admin <telegram_id>`` — добавить нового администратора."""
    target_id = _parse_target_id(command.args)
    if target_id is None:
        await message.answer(
            "Использование: <code>/add_admin &lt;telegram_id&gt;</code>\n"
            "Пример: <code>/add_admin 123456789</code>",
            parse_mode="HTML",
        )
        return

    invoker_id = message.from_user.id if message.from_user else None
    if invoker_id is not None and target_id == invoker_id:
        await message.answer(
            "Вы уже являетесь администратором — повторно добавлять не нужно."
        )
        return

    added = await AdminRepository(session).add(target_id)
    if not added:
        await message.answer(
            f"ℹ️ <code>{target_id}</code> уже есть в списке администраторов.",
            parse_mode="HTML",
        )
        return

    logger.info(
        "ADMIN MANAGEMENT: invoker=%s added admin %s", invoker_id, target_id
    )
    await message.answer(
        f"✅ Администратор <code>{target_id}</code> добавлен.",
        parse_mode="HTML",
    )


@router.message(Command("del_admin"), IsAdmin())
async def cmd_del_admin(
    message: Message, command: CommandObject, session: AsyncSession
) -> None:
    """``/del_admin <telegram_id>`` — удалить администратора."""
    target_id = _parse_target_id(command.args)
    if target_id is None:
        await message.answer(
            "Использование: <code>/del_admin &lt;telegram_id&gt;</code>\n"
            "Пример: <code>/del_admin 123456789</code>",
            parse_mode="HTML",
        )
        return

    invoker_id = message.from_user.id if message.from_user else None
    if invoker_id is not None and target_id == invoker_id:
        await message.answer(
            "❌ Нельзя удалить самого себя из списка администраторов.\n"
            "Попросите другого администратора сделать это."
        )
        return

    admins = AdminRepository(session)
    current_admins = await admins.list_ids(force_refresh=True)
    if target_id not in current_admins:
        await message.answer(
            f"ℹ️ <code>{target_id}</code> нет в списке администраторов.",
            parse_mode="HTML",
        )
        return

    if len(current_admins) <= 1:
        await message.answer(
            "❌ Это последний администратор — удаление запрещено, "
            "иначе доступ к панели управления будет потерян."
        )
        return

    removed = await admins.remove(target_id)
    if not removed:
        # Гонка: кто-то удалил параллельно. Сообщаем как «уже нет».
        await message.answer(
            f"ℹ️ <code>{target_id}</code> уже удалён.",
            parse_mode="HTML",
        )
        return

    logger.info(
        "ADMIN MANAGEMENT: invoker=%s removed admin %s", invoker_id, target_id
    )
    await message.answer(
        f"🗑 Администратор <code>{target_id}</code> удалён.",
        parse_mode="HTML",
    )
