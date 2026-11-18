"""Общие команды: /start, /help, /cancel, ответ на прочий текст и ошибки."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app.bot import deps, keyboards, texts
from app.bot.handlers.guards import is_admin
from app.bot.handlers.requests import notify_access_request
from app.domain import User

logger = logging.getLogger(__name__)


async def ensure_profile(
    update: Update, context: ContextTypes.DEFAULT_TYPE, stored: User | None
) -> None:
    """Обновить имя и username, если человек их сменил в Telegram.

    Строка пользователя уже прочитана ради проверки доступа, поэтому
    лишний раз к базе не ходим: пишем только когда профиль сменился,
    а не на каждое сообщение.
    """
    user = update.effective_user
    if user is None:
        return
    fresh = (user.first_name or "", user.last_name or "", user.username or "")
    if stored is not None and (
        stored.first_name,
        stored.last_name,
        stored.username,
    ) == fresh:
        return
    await deps.access(context).register(user.id, *fresh)


async def deny_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отказ, заявка админу или, если админа ещё нет, роль первому написавшему."""
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return

    service = deps.access(context)
    if not await service.has_admin():
        claimed = await service.claim_first_admin(
            user.id,
            first_name=user.first_name or "",
            last_name=user.last_name or "",
            username=user.username or "",
        )
        if not claimed:
            # База не записала админа: второго круга не будет, иначе
            # рекурсия до RecursionError.
            await message.reply_text(texts.ACCESS_DENIED)
            return
        await start(update, context)
        return

    await notify_access_request(update, context)
    await message.reply_text(texts.access_denied_pending())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start: приветствие, меню и создание профиля."""
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return

    stored = await deps.access(context).get(user.id)
    if stored is None:
        await deny_access(update, context)
        return

    await ensure_profile(update, context, stored)
    admin = deps.access(context).is_admin_row(user.id, stored)
    await message.reply_text(
        texts.start(user.first_name or "друг", admin),
        reply_markup=keyboards.main_menu(admin),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        texts.help_text(await is_admin(update, context)), parse_mode=ParseMode.HTML
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        texts.CANCELLED, reply_markup=keyboards.main_menu(await is_admin(update, context))
    )


async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Любой другой текст: не молчим, объясняем, что делать."""
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        texts.FALLBACK, reply_markup=keyboards.main_menu(await is_admin(update, context))
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Ошибка при обработке обновления", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(texts.ERROR)
        except Exception:
            pass
