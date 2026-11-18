"""Кто может пользоваться ботом и кто здесь администратор.

Сервис ничего не знает про Telegram: он отвечает на вопросы «пускать или
нет», «кто админ» и «выдать или закрыть доступ». Обработчики только
показывают результат. Методы асинхронные: запросы идут через
асинхронный пул, и обработчик ждёт их `await`-ом, не останавливая цикл
событий.
"""

from __future__ import annotations

import logging

from app.domain import User
from app.infra import database

logger = logging.getLogger(__name__)


class AccessService:
    def __init__(self, database_url: str, admin_ids: tuple[int, ...] = ()) -> None:
        self.database_url = database_url
        self.admin_ids = tuple(admin_ids)

    # --- вопросы ----------------------------------------------------------

    def is_admin_row(self, user_id: int | None, stored: User | None) -> bool:
        """Тот же ответ, что у `is_admin`, но по уже прочитанной строке.

        На горячем пути строка пользователя и так прочитана: второй раз
        за ней ходить не нужно.
        """
        if user_id is None:
            return False
        if user_id in self.admin_ids:
            return True
        return bool(stored and stored.is_admin)

    async def is_admin(self, user_id: int | None) -> bool:
        """Админом считается тот, кто указан в `ADMIN_ID` или помечен в базе."""
        if user_id is None or user_id in self.admin_ids:
            return user_id is not None
        stored = await database.get_user(self.database_url, user_id)
        return self.is_admin_row(user_id, stored)

    async def has_admin(self) -> bool:
        """Есть ли хоть один админ: в окружении или в базе."""
        return bool(self.admin_ids) or await database.has_admins(self.database_url)

    async def admins(self) -> list[int]:
        """Все админы: из окружения и из базы, без повторов."""
        found = list(dict.fromkeys(self.admin_ids))
        for user in await database.list_users(self.database_url):
            if user.is_admin and user.user_id not in found:
                found.append(user.user_id)
        return found

    async def count(self) -> int:
        return await database.count_users(self.database_url)

    async def all(self) -> list[User]:
        return await database.list_users(self.database_url)

    async def get(self, user_id: int) -> User | None:
        return await database.get_user(self.database_url, user_id)

    # --- изменения --------------------------------------------------------

    async def register(
        self, user_id: int, first_name: str = "", last_name: str = "", username: str = ""
    ) -> None:
        """Обновить имя и username, если человек их сменил в Telegram."""
        await database.add_user(
            self.database_url,
            user_id,
            first_name=first_name,
            last_name=last_name,
            username=username,
        )

    async def grant(
        self,
        user_id: int,
        first_name: str = "",
        last_name: str = "",
        username: str = "",
        is_admin: bool = False,
    ) -> bool:
        """Выдать доступ. True, если человек появился в списке впервые."""
        return await database.add_user(
            self.database_url,
            user_id,
            is_admin=is_admin,
            first_name=first_name,
            last_name=last_name,
            username=username,
        )

    async def revoke(self, user_id: int) -> bool:
        return await database.remove_user(self.database_url, user_id)

    async def claim_first_admin(self, user_id: int, **profile) -> bool:
        """Пока админа нет ни в `.env`, ни в базе, первый написавший становится им.

        Проверка и запись атомарны на стороне базы: одновременные `/start`
        не сделают админами обоих. False, если роль уже занята.
        """
        if self.admin_ids:
            return False
        claimed = await database.claim_first_admin(self.database_url, user_id, **profile)
        if claimed:
            logger.warning(
                "ADMIN_ID не задан: администратором стал первый написавший, %s", user_id
            )
        return claimed

    # --- язык -------------------------------------------------------------

    async def language(self, user_id: int, default: str = "ru") -> str:
        stored = await database.get_user(self.database_url, user_id)
        return (stored.language if stored else None) or default

    async def set_language(self, user_id: int, language: str) -> None:
        await database.set_language(self.database_url, user_id, language)
