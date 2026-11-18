"""Точка входа бота: настройки, база, long polling."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from telegram import Update

from app.bot.app import READY_FILE, build_application
from app.config import Settings
from app.container import build_services
from app.infra import database

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def main() -> int:
    settings = Settings.from_env()
    if not settings.token:
        logger.error("TELEGRAM_BOT_TOKEN не задан, бот не может стартовать.")
        return 1

    os.makedirs(settings.data_dir, exist_ok=True)
    # Файл готовности от прошлого запуска мог пережить `docker compose
    # restart` внутри того же контейнера. Убираем его: пока база не
    # поднялась, healthcheck не должен показывать healthy.
    Path(READY_FILE).unlink(missing_ok=True)

    async def on_ready(application) -> None:
        # Пул асинхронный, поэтому база поднимается в post_init, внутри
        # цикла событий и до старта long polling. Ошибка базы остановит
        # запуск: бот не должен выглядеть живым без Postgres.
        await database.init_db(settings.database_url, settings.admin_ids)
        if not settings.admin_ids and not await database.has_admins(
            settings.database_url
        ):
            logger.warning(
                "ADMIN_ID не задан: администратором станет первый, кто напишет /start. "
                "Укажи ADMIN_ID в .env, чтобы закрепить админа."
            )

    async def on_shutdown(application) -> None:
        await database.close_all()

    application = build_application(
        settings,
        build_services(settings),
        on_ready=on_ready,
        on_shutdown=on_shutdown,
    )
    logger.info("Запуск long polling")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        # Ничего не выбрасываем: сообщения, пришедшие пока бот лежал,
        # обработаются после старта.
        drop_pending_updates=False,
        close_loop=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
