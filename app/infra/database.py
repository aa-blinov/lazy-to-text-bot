"""Postgres: пользователи и история запросов.

Почему не SQLite: историю пишет бот, а читает статистику он же, но при
нескольких процессах один файл становится точкой сериализации. Postgres
даёт нормальную параллельную запись и переживает перезапуск контейнера.

Пул асинхронный (`psycopg_pool.AsyncConnectionPool`): обработчики бота
живут в цикле событий, и синхронный запрос останавливал бы весь бот на
каждый сетевой раунд. Пул открывается в `init_db` при старте и закрыт
до неё: запрос раньше `init_db` — ошибка программирования, а не «пусть
будет ноль». Схема создаётся и дополняется при старте, соединения
выдаются из пула, коммит происходит на выходе из `connect`.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.domain import Stats, User

logger = logging.getLogger(__name__)

USERS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        is_admin BOOLEAN NOT NULL DEFAULT FALSE,
        first_name TEXT NOT NULL DEFAULT '',
        last_name TEXT NOT NULL DEFAULT '',
        username TEXT NOT NULL DEFAULT '',
        language TEXT NOT NULL DEFAULT 'ru',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
"""

TASKS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS tasks (
        task_id BIGSERIAL PRIMARY KEY,
        user_id BIGINT REFERENCES users(user_id) ON DELETE SET NULL,
        timestamp TIMESTAMPTZ NOT NULL,
        duration_seconds DOUBLE PRECISION,
        original_file_type TEXT,
        recognized_text TEXT,
        language TEXT
    )
"""

INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_tasks_timestamp ON tasks(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id)",
)

_pools: dict[str, AsyncConnectionPool] = {}
_pools_lock = threading.Lock()


def pool(database_url: str) -> AsyncConnectionPool:
    """Пул соединений, один на строку подключения.

    Пул создаётся и открывается в `init_db`. Ленивого создания нет: запрос
    до `init_db` падает с понятной ошибкой вместо тихого соединения.
    """
    existing = _pools.get(database_url)
    if existing is None:
        raise RuntimeError(
            f"пул соединений для {database_url!r} не открыт: сначала init_db(...)"
        )
    return existing


@asynccontextmanager
async def connect(database_url: str):
    """Соединение из пула: коммит при выходе, возврат в пул всегда."""
    async with pool(database_url).connection() as conn:
        yield conn


async def close_all() -> None:
    """Закрыть пулы: нужно тестам и аккуратному завершению."""
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for container in pools:
        await container.close()


async def init_db(database_url: str, admin_ids: tuple[int, ...] = ()) -> None:
    """Создать пул, схему и завести администраторов из окружения.

    Ошибку не глотаем: без базы бот не должен выглядеть живым. Исключение
    останавливает процесс, и Compose перезапускает его, пока Postgres не
    поднимется.
    """
    with _pools_lock:
        created = _pools.get(database_url)
        fresh = created is None
        if fresh:
            created = AsyncConnectionPool(
                database_url,
                min_size=1,
                max_size=8,
                open=False,
                kwargs={"row_factory": dict_row},
            )
            _pools[database_url] = created
    if fresh:
        await created.open(wait=True, timeout=15)

    async with connect(database_url) as conn:
        await conn.execute(USERS_SCHEMA)
        await conn.execute(TASKS_SCHEMA)
        for statement in INDEXES:
            await conn.execute(statement)
        for admin_id in admin_ids:
            await conn.execute(
                """INSERT INTO users (user_id, is_admin) VALUES (%s, TRUE)
                   ON CONFLICT (user_id) DO UPDATE SET is_admin = TRUE""",
                (admin_id,),
            )
    logger.info("Схема базы данных готова")


def _row_to_user(row: dict) -> User:
    return User(
        user_id=int(row["user_id"]),
        is_admin=bool(row["is_admin"]),
        first_name=row["first_name"] or "",
        last_name=row["last_name"] or "",
        username=row["username"] or "",
        language=row["language"] or "ru",
        created_at=row["created_at"].isoformat() if row["created_at"] else None,
    )


async def get_user(database_url: str, user_id: int) -> User | None:
    try:
        async with connect(database_url) as conn:
            cur = await conn.execute(
                "SELECT * FROM users WHERE user_id = %s", (user_id,)
            )
            row = await cur.fetchone()
        return _row_to_user(row) if row else None
    except psycopg.Error as exc:
        logger.error("Ошибка чтения пользователя %s: %s", user_id, exc)
        return None


async def list_users(database_url: str) -> list[User]:
    """Все допущенные, администраторы первыми, затем по имени."""
    try:
        async with connect(database_url) as conn:
            cur = await conn.execute(
                """SELECT * FROM users
                   ORDER BY is_admin DESC, LOWER(first_name), user_id"""
            )
            rows = await cur.fetchall()
        return [_row_to_user(row) for row in rows]
    except psycopg.Error as exc:
        logger.error("Ошибка чтения списка пользователей: %s", exc)
        return []


async def count_users(database_url: str) -> int:
    try:
        async with connect(database_url) as conn:
            cur = await conn.execute("SELECT COUNT(*) AS n FROM users")
            row = await cur.fetchone()
        return int(row["n"])
    except psycopg.Error as exc:
        logger.error("Ошибка подсчёта пользователей: %s", exc)
        return 0


async def has_admins(database_url: str) -> bool:
    try:
        async with connect(database_url) as conn:
            cur = await conn.execute("SELECT 1 AS one FROM users WHERE is_admin LIMIT 1")
            row = await cur.fetchone()
        return row is not None
    except psycopg.Error as exc:
        logger.error("Ошибка проверки администраторов: %s", exc)
        return False


async def add_user(
    database_url: str,
    user_id: int,
    *,
    is_admin: bool = False,
    first_name: str = "",
    last_name: str = "",
    username: str = "",
) -> bool:
    """Добавить или обновить пользователя. True, если запись новая."""
    try:
        async with connect(database_url) as conn:
            cur = await conn.execute(
                """INSERT INTO users (user_id, is_admin, first_name, last_name, username)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (user_id) DO UPDATE SET
                       first_name = COALESCE(NULLIF(EXCLUDED.first_name, ''), users.first_name),
                       last_name = COALESCE(NULLIF(EXCLUDED.last_name, ''), users.last_name),
                       username = COALESCE(NULLIF(EXCLUDED.username, ''), users.username),
                       is_admin = users.is_admin OR EXCLUDED.is_admin
                   RETURNING (xmax = 0) AS inserted""",
                (user_id, is_admin, first_name or "", last_name or "", username or ""),
            )
            row = await cur.fetchone()
        created = bool(row and row["inserted"])
        logger.info("Пользователь %s %s", "добавлен" if created else "обновлён", user_id)
        return created
    except psycopg.Error as exc:
        logger.error("Ошибка при добавлении пользователя %s: %s", user_id, exc)
        return False


async def remove_user(database_url: str, user_id: int) -> bool:
    """Убрать пользователя из списка. True, если он там был.

    История остаётся: внешний ключ объявлен с ON DELETE SET NULL, поэтому
    запросы человека остаются в таблице без привязки к пользователю.
    """
    try:
        async with connect(database_url) as conn:
            cur = await conn.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
            removed = cur.rowcount > 0
        if removed:
            logger.info("Пользователь %s удалён", user_id)
        return removed
    except psycopg.Error as exc:
        logger.error("Ошибка при удалении пользователя %s: %s", user_id, exc)
        return False


async def claim_first_admin(
    database_url: str,
    user_id: int,
    *,
    first_name: str = "",
    last_name: str = "",
    username: str = "",
) -> bool:
    """Сделать первого написавшего администратором ровно один раз.

    Проверка и вставка идут под транзакционной advisory-блокировкой:
    два одновременных `/start` на свежей базе иначе оба видят «админов
    нет» и оба получают права.
    """
    try:
        async with connect(database_url) as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(918273645)")
            cur = await conn.execute(
                "SELECT 1 AS one FROM users WHERE is_admin LIMIT 1"
            )
            existing = await cur.fetchone()
            if existing is not None:
                return False
            await conn.execute(
                """INSERT INTO users (user_id, is_admin, first_name, last_name, username)
                   VALUES (%s, TRUE, %s, %s, %s)
                   ON CONFLICT (user_id) DO UPDATE SET is_admin = TRUE""",
                (user_id, first_name or "", last_name or "", username or ""),
            )
        logger.info("Первый администратор: %s", user_id)
        return True
    except psycopg.Error as exc:
        logger.error("Не удалось записать первого администратора %s: %s", user_id, exc)
        return False


async def set_language(database_url: str, user_id: int, language: str) -> None:
    """Запомнить выбранный язык, чтобы он переживал перезапуск бота."""
    try:
        async with connect(database_url) as conn:
            await conn.execute(
                "UPDATE users SET language = %s WHERE user_id = %s",
                (language, user_id),
            )
    except psycopg.Error as exc:
        logger.error("Ошибка сохранения языка %s: %s", user_id, exc)


async def record_task(
    database_url: str,
    user_id: int,
    duration_seconds: float,
    file_type: str,
    text: str,
    language: str | None = None,
) -> None:
    """Записать запрос. Если человека уже удалили, строка остаётся без него."""
    try:
        async with connect(database_url) as conn:
            await conn.execute(
                """INSERT INTO tasks
                   (user_id, timestamp, duration_seconds, original_file_type,
                    recognized_text, language)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (user_id, datetime.now(), duration_seconds, file_type, text, language),
            )
    except psycopg.errors.ForeignKeyViolation:
        # Доступ отозвали, пока шло распознавание: историю не теряем.
        logger.info("Пользователь %s удалён, пишу запрос без владельца", user_id)
        try:
            async with connect(database_url) as conn:
                await conn.execute(
                    """INSERT INTO tasks
                       (user_id, timestamp, duration_seconds, original_file_type,
                        recognized_text, language)
                       VALUES (NULL, %s, %s, %s, %s, %s)""",
                    (datetime.now(), duration_seconds, file_type, text, language),
                )
        except psycopg.Error as exc:
            logger.error("Ошибка записи запроса без владельца: %s", exc)
    except psycopg.Error as exc:
        logger.error("Ошибка записи запроса пользователя %s: %s", user_id, exc)


async def stats(database_url: str) -> Stats:
    """Счётчики для `/stats`: пользователи, запросы за сегодня и за неделю.

    Один запрос вместо семи: каждый раунд до базы — это пауза цикла
    событий, субзапросы же считаются на стороне Postgres за один раунд.
    """
    try:
        now = datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # Ровно семь суток назад, а не «начало дня минус семь дней»: иначе
        # окно захватывает восемь календарных дат.
        week_start = now - timedelta(days=7)

        async with connect(database_url) as conn:
            cur = await conn.execute(
                """SELECT
                       (SELECT COUNT(*) FROM users) AS total_users,
                       (SELECT COUNT(*) FROM users
                          WHERE created_at >= %s) AS today_new,
                       (SELECT COUNT(*) FROM users
                          WHERE created_at >= %s) AS week_new,
                       (SELECT COUNT(*) FROM tasks
                          WHERE timestamp >= %s) AS today_requests,
                       (SELECT COUNT(DISTINCT user_id) FROM tasks
                          WHERE timestamp >= %s) AS today_active,
                       (SELECT COUNT(*) FROM tasks
                          WHERE timestamp >= %s) AS week_requests,
                       (SELECT COUNT(DISTINCT user_id) FROM tasks
                          WHERE timestamp >= %s) AS week_active""",
                (
                    today_start,
                    week_start,
                    today_start,
                    today_start,
                    week_start,
                    week_start,
                ),
            )
            row = await cur.fetchone()

        return Stats(
            total_users=int(row["total_users"]),
            today_active=int(row["today_active"]),
            today_requests=int(row["today_requests"]),
            today_new=int(row["today_new"]),
            week_active=int(row["week_active"]),
            week_requests=int(row["week_requests"]),
            week_new=int(row["week_new"]),
        )
    except psycopg.Error as exc:
        logger.error("Ошибка чтения статистики: %s", exc)
        return Stats()
