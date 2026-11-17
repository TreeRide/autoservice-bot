"""
Точка входа Telegram-бота автосервиса.

Запускает long-polling aiogram 3.x. Перед стартом инициализирует
SQLite-БД и регистрирует роутеры (админский + клиентский).
FSM-состояния хранятся в памяти (``MemoryStorage``), чего достаточно
для MVP.

Запуск::

    python main.py

Перед запуском задайте переменную окружения ``BOT_TOKEN`` (например,
через файл ``.env``).
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    ErrorEvent,
    MenuButtonCommands,
    MenuButtonWebApp,
    WebAppInfo,
)

from config import BOT_TOKEN, WEBAPP_URL
from database.repositories import AdminRepository
from database.session import async_session_factory
from fsm_storage import SQLiteStorage, init_fsm_table
from handlers import router
from middlewares import DbSessionMiddleware, MenuResetMiddleware
from notifications import daily_notifier_loop
from startup import _BASELINE_TABLES, _sync_inspect_db, bootstrap_database

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    """Настроить базовое логирование уровня INFO."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


# Текст подписи под глобальной web-app кнопкой в Menu Button (📎 рядом
# с полем ввода). Виден всем клиентам, кроме админов (см. ниже).
MENU_BUTTON_TEXT = "📖 Сервисная книжка"


async def _configure_menu_buttons(bot: Bot) -> None:
    """Настроить Menu Button (📎 рядом с полем ввода) по ролям.

    * **Глобально** (без ``chat_id``): ``MenuButtonWebApp`` с ссылкой
      на наш Mini App. Применяется ко всем приватным чатам, для
      которых не задан персональный override. Только если задан
      ``WEBAPP_URL`` — иначе глобальная кнопка не трогается.

    * **Для каждого админа из таблицы ``admins``**: персональный
      ``MenuButtonCommands`` — это перебивает глобальный WebApp-button
      и показывает админу стандартное меню `/`-команд бота. Web App
      админу не нужен (у него нет машин в БД), а кнопка только
      сбивает с толку.

    Telegram сохраняет настройку Menu Button на своей стороне между
    рестартами бота — но мы вызываем её на каждом старте идемпотентно,
    чтобы новые админы из БД подтянулись автоматически.
    """
    # Глобальная WebApp-кнопка для клиентов.
    if WEBAPP_URL:
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text=MENU_BUTTON_TEXT,
                    web_app=WebAppInfo(url=WEBAPP_URL),
                ),
            )
            logger.info(
                "Menu Button (global): WebApp set to %s", WEBAPP_URL
            )
        except TelegramAPIError as exc:
            logger.warning(
                "Не удалось установить глобальный MenuButtonWebApp: %s", exc
            )
    else:
        logger.info(
            "WEBAPP_URL не задан — глобальный MenuButtonWebApp пропущен."
        )

    # Phase 12: админам теперь нужен ВЕБ-АПП (CRM-dashboard с
    # глобальным поиском клиентов). Раньше они получали
    # MenuButtonCommands — теперь так же шлют их в Mini App, но
    # фронт по флагу /api/me.is_admin покажет admin-dashboard
    # вместо клиентского гаража. Если WEBAPP_URL не задан —
    # fallback на MenuButtonCommands (как раньше).
    async with async_session_factory() as session:
        admin_ids = await AdminRepository(session).list_ids()
    for admin_id in admin_ids:
        try:
            if WEBAPP_URL:
                await bot.set_chat_menu_button(
                    chat_id=admin_id,
                    menu_button=MenuButtonWebApp(
                        text=MENU_BUTTON_TEXT,
                        web_app=WebAppInfo(url=WEBAPP_URL),
                    ),
                )
                logger.info(
                    "Menu Button (admin %s): MenuButtonWebApp set "
                    "(CRM dashboard)",
                    admin_id,
                )
            else:
                await bot.set_chat_menu_button(
                    chat_id=admin_id,
                    menu_button=MenuButtonCommands(),
                )
                logger.info(
                    "Menu Button (admin %s): MenuButtonCommands set "
                    "(WEBAPP_URL not configured)",
                    admin_id,
                )
        except TelegramAPIError as exc:
            # Самые частые причины: админ ни разу не писал боту (нет
            # приватного чата) или бота заблокировал. Это ок — при
            # первом /start попадём в safety-net в cmd_start_admin.
            logger.warning(
                "Не удалось установить Menu Button у админа %s: %s",
                admin_id, exc,
            )


async def main() -> None:
    """Запуск бота: инициализация БД, регистрация хэндлеров, polling."""
    configure_logging()

    if not BOT_TOKEN:
        logger.error(
            "Переменная окружения BOT_TOKEN не задана. "
            "Создайте файл .env и пропишите BOT_TOKEN=<токен_от_BotFather>."
        )
        sys.exit(1)

    # Применяем миграции Alembic и засеваем таблицу admins (если
    # пуста) из ADMIN_BOOTSTRAP_IDS. fsm_state создаётся отдельным
    # raw-SQL — работаем с ней напрямую из :mod:`fsm_storage`.
    await bootstrap_database()
    await init_fsm_table()

    async with async_session_factory() as session:
        admin_ids = await AdminRepository(session).list_ids(force_refresh=True)
    logger.info("admins: %s", sorted(admin_ids))
    if not admin_ids:
        logger.warning(
            "Таблица admins пуста. Админ-панель никому не доступна — "
            "задайте ADMIN_BOOTSTRAP_IDS в .env (CSV) и перезапустите бота, "
            "либо вставьте вручную: INSERT INTO admins (telegram_id) VALUES (?)."
        )

    bot = Bot(token=BOT_TOKEN)
    await _configure_menu_buttons(bot)
    dp = Dispatcher(storage=SQLiteStorage())
    # 1) DbSessionMiddleware — инжектит AsyncSession в data каждого
    #    апдейта. Ставим ПЕРВЫМ, чтобы фильтры (IsAdmin)
    #    и прочие middleware видели session в data.
    # 2) MenuResetMiddleware — сброс FSM по reply-кнопкам.
    dp.update.outer_middleware(DbSessionMiddleware())
    dp.message.outer_middleware(MenuResetMiddleware())
    dp.include_router(router)

    @dp.errors()
    async def on_unhandled_error(event: ErrorEvent) -> bool:
        """Глобальный fallback: ловим всё, чего не поймали хэндлеры.

        * Логирует traceback.
        * Шлёт админам короткое уведомление (без traceback, чтобы не
          спамить, но с repr исключения и chat_id).
        * Если апдейт — сообщение или callback, отвечает пользователю
          вежливым сообщением вместо «бот молчит».
        """
        exc = event.exception
        update = event.update
        logger.exception("Unhandled error: %r", exc)
        # Контекст события
        chat_id: int | None = None
        user_id: int | None = None
        if update.message:
            chat_id = update.message.chat.id
            if update.message.from_user:
                user_id = update.message.from_user.id
        elif update.callback_query and update.callback_query.message:
            chat_id = update.callback_query.message.chat.id
            if update.callback_query.from_user:
                user_id = update.callback_query.from_user.id

        # Уведомить админов
        async with async_session_factory() as _admin_session:
            error_admin_ids = await AdminRepository(_admin_session).list_ids()
        for admin_id in error_admin_ids:
            try:
                await bot.send_message(
                    admin_id,
                    f"⚠️ <b>Bot error</b>\n"
                    f"<code>{type(exc).__name__}: {exc}</code>\n"
                    f"chat_id=<code>{chat_id}</code> user_id=<code>{user_id}</code>",
                    parse_mode="HTML",
                )
            except Exception:
                logger.exception("Не удалось уведомить админа %s об ошибке", admin_id)

        # Ответить пользователю (если есть message)
        try:
            if update.message:
                await update.message.answer(
                    "⚠️ Произошла внутренняя ошибка. Мастер уведомлён, "
                    "попробуйте ещё раз или вернитесь в главное меню /start."
                )
            elif update.callback_query:
                await update.callback_query.answer(
                    "⚠️ Внутренняя ошибка. Мастер уведомлён.",
                    show_alert=True,
                )
        except Exception:
            logger.exception("Не удалось ответить пользователю об ошибке")
        return True  # mark as handled

    notifier_task = asyncio.create_task(
        daily_notifier_loop(bot), name="daily-notifier"
    )

    logger.info("Запуск polling...")
    try:
        await dp.start_polling(bot)
    finally:
        notifier_task.cancel()
        try:
            await notifier_task
        except (asyncio.CancelledError, Exception):
            pass
        await bot.session.close()


_PROJECT_ROOT = Path(__file__).resolve().parent


def _run_alembic_migrations() -> None:
    """Применить Alembic-миграции в отдельном subprocess.

    Это самый надёжный способ для SQLite-стека: ``alembic/env.py``
    использует async-template и внутри себя вызывает ``asyncio.run``,
    который при запуске из активного event loop'а (или из worker-потока,
    запущенного из активного loop'а) конфликтует с aiosqlite-helper'ом
    и приводит к deadlock'у. Изолируя миграции в чистый subprocess, мы
    полностью избавляемся от пересечения event loop'ов.

    Поведение совпадает с ``startup._alembic_upgrade_or_stamp``:
    1. БД легаси (baseline-таблицы есть, ``alembic_version`` нет) →
       ``alembic stamp head``.
    2. Иначе (свежая или уже под Alembic) → ``alembic upgrade head``.
    """
    from config import DB_PATH

    tables, revision = _sync_inspect_db(DB_PATH)
    has_legacy_schema = _BASELINE_TABLES.issubset(tables)

    if revision is None and has_legacy_schema:
        argv = [sys.executable, "-m", "alembic", "stamp", "head"]
        logging.getLogger(__name__).info(
            "Alembic: легаси-БД с baseline-схемой, выполняю stamp head (subprocess)"
        )
    else:
        argv = [sys.executable, "-m", "alembic", "upgrade", "head"]
        if revision is not None:
            logging.getLogger(__name__).info(
                "Alembic: текущая ревизия %s, upgrade head (subprocess)", revision
            )
        else:
            logging.getLogger(__name__).info(
                "Alembic: свежая БД, upgrade head (subprocess)"
            )

    subprocess.run(argv, check=True, cwd=str(_PROJECT_ROOT))


if __name__ == "__main__":
    configure_logging()
    # Миграции — синхронно, в subprocess, ДО запуска event loop'а.
    # См. docstring _run_alembic_migrations: это лекарство от deadlock'а
    # между внешним loop'ом aiogram и внутренним asyncio.run в
    # alembic/env.py поверх aiosqlite.
    _run_alembic_migrations()
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
