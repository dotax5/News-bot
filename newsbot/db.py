"""Асинхронный слой хранения на SQLite (aiosqlite)."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path

import aiosqlite

from .models import (
    Channel,
    ChannelInfo,
    NewPost,
    PendingPost,
    PostMirrorState,
    User,
    UserChannel,
)
from .utils import iso_utc, join_int_ids, parse_channel_ref, parse_int_ids, utcnow

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id      INTEGER NOT NULL UNIQUE,
    username   TEXT,
    first_name TEXT,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    key             TEXT NOT NULL UNIQUE,
    tg_id           INTEGER,
    username        TEXT,
    title           TEXT,
    source          TEXT NOT NULL DEFAULT 'user',
    last_message_id INTEGER NOT NULL DEFAULT 0,
    last_checked_at TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_channels_username
    ON channels(lower(username)) WHERE username IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_channels_tg_id
    ON channels(tg_id) WHERE tg_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS user_channels (
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    added_at TEXT NOT NULL,
    PRIMARY KEY (user_id, channel_id)
);

CREATE TABLE IF NOT EXISTS posts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    message_id  INTEGER NOT NULL,
    text        TEXT NOT NULL DEFAULT '',
    posted_at   TEXT NOT NULL,
    url         TEXT,
    has_media   INTEGER NOT NULL DEFAULT 0,
    source_message_ids TEXT NOT NULL DEFAULT '',
    mirror_chat_id     INTEGER,
    mirror_message_ids TEXT,
    mirror_requested_at TEXT,
    media_state        TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL,
    notified_at TEXT,
    UNIQUE (channel_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_posts_channel_posted
    ON posts(channel_id, posted_at);
CREATE INDEX IF NOT EXISTS idx_posts_posted ON posts(posted_at);
"""

# Индекс ссылается на posts.notified_at, поэтому создаётся после миграции
# старой базы, а не вместе со схемой.
PENDING_INDEX = """
CREATE INDEX IF NOT EXISTS idx_posts_pending
    ON posts(notified_at) WHERE notified_at IS NULL;
"""


def _user_from_row(row: aiosqlite.Row) -> User:
    return User(
        id=row["id"],
        tg_id=row["tg_id"],
        username=row["username"],
        first_name=row["first_name"],
        enabled=bool(row["enabled"]),
    )


def _channel_from_row(row: aiosqlite.Row) -> Channel:
    return Channel(
        id=row["id"],
        key=row["key"],
        tg_id=row["tg_id"],
        username=row["username"],
        title=row["title"],
        source=row["source"],
        last_message_id=row["last_message_id"],
        last_checked_at=row["last_checked_at"],
        last_error=row["last_error"],
        created_at=row["created_at"],
    )


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("База данных ещё не открыта.")
        return self._conn

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.executescript(PENDING_INDEX)
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _columns(self, table: str) -> set[str]:
        rows = await self._fetchall(f"PRAGMA table_info({table})")
        return {row["name"] for row in rows}

    async def _migrate(self) -> None:
        """Приводит базу прежних версий к актуальной схеме.

        Раньше у пользователей были timezone/digest_time/last_digest_*: пересобираем
        таблицу без них. Внешние ключи на время пересборки выключаются, иначе
        DROP TABLE users каскадно удалил бы подписки.
        """
        user_columns = await self._columns("users")
        if {"timezone", "digest_time"} & user_columns:
            log.info("Миграция: убираю расписание рассылки из таблицы users")
            await self._conn.commit()
            await self._conn.execute("PRAGMA foreign_keys=OFF")
            try:
                await self._conn.executescript(
                    """
                    BEGIN;
                    CREATE TABLE users_new (
                        id         INTEGER PRIMARY KEY AUTOINCREMENT,
                        tg_id      INTEGER NOT NULL UNIQUE,
                        username   TEXT,
                        first_name TEXT,
                        enabled    INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL
                    );
                    INSERT INTO users_new (id, tg_id, username, first_name, enabled, created_at)
                        SELECT id, tg_id, username, first_name, enabled, created_at FROM users;
                    DROP TABLE users;
                    ALTER TABLE users_new RENAME TO users;
                    COMMIT;
                    """
                )
            finally:
                await self._conn.execute("PRAGMA foreign_keys=ON")

        post_columns = await self._columns("posts")
        if "notified_at" not in post_columns:
            log.info("Миграция: добавляю posts.notified_at")
            await self._conn.execute("ALTER TABLE posts ADD COLUMN notified_at TEXT")
            # Посты, собранные прежними версиями, уже попали в дайджесты:
            # повторно рассылать их не нужно.
            await self._conn.execute(
                "UPDATE posts SET notified_at = created_at WHERE notified_at IS NULL"
            )

        # Медиа-копия: пост пересылается в чат бота, а оттуда копируется
        # подписчикам. Колонки добавляются по одной, чтобы миграция была
        # безопасной при частично обновлённой базе.
        media_columns = {
            "source_message_ids": "TEXT NOT NULL DEFAULT ''",
            "mirror_chat_id": "INTEGER",
            "mirror_message_ids": "TEXT",
            "mirror_requested_at": "TEXT",
            "media_state": "TEXT NOT NULL DEFAULT 'pending'",
        }
        for name, ddl in media_columns.items():
            if name not in post_columns:
                log.info("Миграция: добавляю posts.%s", name)
                await self._conn.execute(f"ALTER TABLE posts ADD COLUMN {name} {ddl}")
        await self._conn.execute(
            """
            UPDATE posts SET source_message_ids = CAST(message_id AS TEXT)
            WHERE source_message_ids = ''
            """
        )

        await self._conn.commit()

    # ------------------------------------------------------------------ users

    async def ensure_user(
        self,
        tg_id: int,
        username: str | None,
        first_name: str | None,
        now: datetime | None = None,
    ) -> User:
        now = now or utcnow()
        async with self._lock:
            await self.conn.execute(
                """
                INSERT INTO users (tg_id, username, first_name, enabled, created_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(tg_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name
                """,
                (tg_id, username, first_name, iso_utc(now)),
            )
            await self.conn.commit()
            row = await self._fetchone("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
        assert row is not None
        return _user_from_row(row)

    async def get_user(self, tg_id: int) -> User | None:
        row = await self._fetchone("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
        return _user_from_row(row) if row else None

    async def set_user_enabled(self, user_id: int, enabled: bool) -> None:
        await self._execute(
            "UPDATE users SET enabled = ? WHERE id = ?", (1 if enabled else 0, user_id)
        )

    async def list_channel_subscribers(self, channel_id: int) -> list[User]:
        rows = await self._fetchall(
            """
            SELECT u.*
            FROM users u
            JOIN user_channels uc ON uc.user_id = u.id
            WHERE uc.channel_id = ? AND u.enabled = 1
            ORDER BY u.id
            """,
            (channel_id,),
        )
        return [_user_from_row(row) for row in rows]

    # --------------------------------------------------------------- channels

    async def seed_env_channels(self, refs: Sequence[str]) -> None:
        now = iso_utc(utcnow())
        async with self._lock:
            for raw in refs:
                try:
                    ref = parse_channel_ref(raw)
                except ValueError as exc:
                    log.warning("Пропускаю SOURCE_CHANNELS[%r]: %s", raw, exc)
                    continue
                await self.conn.execute(
                    """
                    INSERT OR IGNORE INTO channels
                        (key, tg_id, username, title, source, created_at)
                    VALUES (?, ?, ?, NULL, 'env', ?)
                    """,
                    (ref.key, ref.peer_id, ref.username, now),
                )
            await self.conn.commit()

    async def subscribe_env_channels(self, user_id: int) -> int:
        async with self._lock:
            cursor = await self.conn.execute(
                """
                INSERT OR IGNORE INTO user_channels (user_id, channel_id, added_at)
                SELECT ?, id, ? FROM channels WHERE source = 'env'
                """,
                (user_id, iso_utc(utcnow())),
            )
            await self.conn.commit()
            return max(cursor.rowcount, 0)

    async def add_or_update_channel(
        self, info: ChannelInfo, source: str = "user"
    ) -> tuple[Channel, bool]:
        """Возвращает (канал, был_ли_уже_в_базе)."""
        now = iso_utc(utcnow())
        async with self._lock:
            rows = await self._fetchall(
                """
                SELECT * FROM channels
                WHERE key = ?
                   OR (tg_id IS NOT NULL AND tg_id = ?)
                   OR (username IS NOT NULL AND ? IS NOT NULL AND lower(username) = lower(?))
                ORDER BY id
                """,
                (info.key, info.tg_id, info.username, info.username),
            )
            if rows:
                channel = _channel_from_row(rows[0])
                new_source = "env" if "env" in (channel.source, source) else "user"
                try:
                    await self.conn.execute(
                        """
                        UPDATE channels
                        SET tg_id = ?, username = ?, title = ?, source = ?
                        WHERE id = ?
                        """,
                        (info.tg_id, info.username, info.title, new_source, channel.id),
                    )
                except sqlite3.IntegrityError:
                    # Конфликт уникальности из-за старой/дублирующей записи:
                    # сохраняем хотя бы название и источник.
                    await self.conn.execute(
                        "UPDATE channels SET title = ?, source = ? WHERE id = ?",
                        (info.title, new_source, channel.id),
                    )
                await self.conn.commit()
                row = await self._fetchone("SELECT * FROM channels WHERE id = ?", (channel.id,))
                assert row is not None
                return _channel_from_row(row), True

            cursor = await self.conn.execute(
                """
                INSERT INTO channels (key, tg_id, username, title, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (info.key, info.tg_id, info.username, info.title, source, now),
            )
            await self.conn.commit()
            row = await self._fetchone("SELECT * FROM channels WHERE id = ?", (cursor.lastrowid,))
            assert row is not None
            return _channel_from_row(row), False

    async def subscribe(self, user_id: int, channel_id: int) -> bool:
        async with self._lock:
            cursor = await self.conn.execute(
                "INSERT OR IGNORE INTO user_channels (user_id, channel_id, added_at) VALUES (?, ?, ?)",
                (user_id, channel_id, iso_utc(utcnow())),
            )
            await self.conn.commit()
            return cursor.rowcount > 0

    async def unsubscribe(self, user_id: int, channel_id: int) -> bool:
        async with self._lock:
            cursor = await self.conn.execute(
                "DELETE FROM user_channels WHERE user_id = ? AND channel_id = ?",
                (user_id, channel_id),
            )
            await self.conn.commit()
            return cursor.rowcount > 0

    async def count_user_channels(self, user_id: int) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM user_channels WHERE user_id = ?", (user_id,)
        )
        return int(row["n"]) if row else 0

    async def list_user_channels_with_status(self, user_id: int) -> list[UserChannel]:
        rows = await self._fetchall(
            """
            SELECT c.*, (SELECT MAX(p.posted_at) FROM posts p WHERE p.channel_id = c.id) AS last_post_at
            FROM channels c
            JOIN user_channels uc ON uc.channel_id = c.id
            WHERE uc.user_id = ?
            ORDER BY lower(COALESCE(c.title, c.username, c.key)), c.id
            """,
            (user_id,),
        )
        return [
            UserChannel(channel=_channel_from_row(row), last_post_at=row["last_post_at"])
            for row in rows
        ]

    async def find_user_channel_by_ref(self, user_id: int, raw: str) -> Channel | None:
        try:
            ref = parse_channel_ref(raw)
        except ValueError:
            return None
        row = await self._fetchone(
            """
            SELECT c.*
            FROM channels c
            JOIN user_channels uc ON uc.channel_id = c.id
            WHERE uc.user_id = ?
              AND (c.key = ?
                   OR (c.tg_id IS NOT NULL AND c.tg_id = ?)
                   OR (c.username IS NOT NULL AND ? IS NOT NULL AND lower(c.username) = lower(?)))
            ORDER BY c.id
            LIMIT 1
            """,
            (user_id, ref.key, ref.peer_id, ref.username, ref.username),
        )
        return _channel_from_row(row) if row else None

    async def channels_to_collect(self) -> list[Channel]:
        """Каналы, у которых есть хотя бы один подписчик.

        Собираем даже при паузе у всех подписчиков: курсор канала продолжает
        двигаться, поэтому после /resume не приходит залп пропущенных постов.
        """
        rows = await self._fetchall(
            """
            SELECT DISTINCT c.*
            FROM channels c
            JOIN user_channels uc ON uc.channel_id = c.id
            ORDER BY c.id
            """
        )
        return [_channel_from_row(row) for row in rows]

    async def last_channel_check_at(self, user_id: int) -> str | None:
        row = await self._fetchone(
            """
            SELECT MAX(c.last_checked_at) AS t
            FROM channels c
            JOIN user_channels uc ON uc.channel_id = c.id
            WHERE uc.user_id = ?
            """,
            (user_id,),
        )
        return row["t"] if row else None

    async def update_channel_meta(
        self, channel_id: int, tg_id: int, username: str | None, title: str | None
    ) -> None:
        try:
            await self._execute(
                "UPDATE channels SET tg_id = ?, username = ?, title = ? WHERE id = ?",
                (tg_id, username, title, channel_id),
            )
        except sqlite3.IntegrityError:
            await self._execute(
                "UPDATE channels SET title = ? WHERE id = ?", (title, channel_id)
            )

    async def update_channel_progress(
        self,
        channel_id: int,
        last_message_id: int,
        checked_at: str,
        error: str | None = None,
    ) -> None:
        await self._execute(
            """
            UPDATE channels
            SET last_message_id = MAX(last_message_id, ?),
                last_checked_at = ?,
                last_error = ?
            WHERE id = ?
            """,
            (last_message_id, checked_at, error, channel_id),
        )

    async def set_channel_error(self, channel_id: int, error: str) -> None:
        await self._execute(
            "UPDATE channels SET last_error = ?, last_checked_at = ? WHERE id = ?",
            (error[:500], iso_utc(utcnow()), channel_id),
        )

    # ------------------------------------------------------------------ posts

    async def insert_posts(
        self,
        channel_id: int,
        posts: Iterable[NewPost],
        *,
        notify: bool = True,
    ) -> int:
        """Сохраняет посты. notify=False — оставить их в очереди на рассылку."""
        now = iso_utc(utcnow())
        rows = [
            (
                channel_id,
                post.message_id,
                post.text,
                post.posted_at,
                post.url,
                1 if post.has_media else 0,
                join_int_ids(post.source_message_ids or (post.message_id,)),
                now,
                now if notify else None,
            )
            for post in posts
        ]
        if not rows:
            return 0
        async with self._lock:
            cursor = await self.conn.executemany(
                """
                INSERT OR IGNORE INTO posts
                    (channel_id, message_id, text, posted_at, url, has_media,
                     source_message_ids, created_at, notified_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            await self.conn.commit()
            return max(cursor.rowcount, 0)

    async def mark_post_pending(self, channel_id: int, message_id: int) -> None:
        """Возвращает пост в очередь рассылки (последний пост при первом сборе)."""
        await self._execute(
            "UPDATE posts SET notified_at = NULL WHERE channel_id = ? AND message_id = ?",
            (channel_id, message_id),
        )

    async def list_pending_posts(self, limit: int = 100) -> list[PendingPost]:
        rows = await self._fetchall(
            """
            SELECT p.*, c.title AS channel_title, c.username AS channel_username,
                   c.tg_id AS channel_tg_id
            FROM posts p
            JOIN channels c ON c.id = p.channel_id
            WHERE p.notified_at IS NULL
            ORDER BY p.posted_at, p.id
            LIMIT ?
            """,
            (limit,),
        )
        return [
            PendingPost(
                id=row["id"],
                channel_id=row["channel_id"],
                message_id=row["message_id"],
                text=row["text"],
                posted_at=row["posted_at"],
                url=row["url"],
                has_media=bool(row["has_media"]),
                channel_title=row["channel_title"],
                channel_username=row["channel_username"],
                channel_tg_id=row["channel_tg_id"],
                source_message_ids=parse_int_ids(row["source_message_ids"])
                or (row["message_id"],),
                mirror_chat_id=row["mirror_chat_id"],
                mirror_message_ids=parse_int_ids(row["mirror_message_ids"]),
                mirror_requested_at=row["mirror_requested_at"],
                media_state=row["media_state"],
            )
            for row in rows
        ]

    async def get_post_mirror(self, post_id: int) -> PostMirrorState | None:
        row = await self._fetchone(
            """
            SELECT id, media_state, mirror_chat_id, mirror_message_ids,
                   mirror_requested_at
            FROM posts WHERE id = ?
            """,
            (post_id,),
        )
        if row is None:
            return None
        return PostMirrorState(
            post_id=row["id"],
            media_state=row["media_state"],
            mirror_chat_id=row["mirror_chat_id"],
            mirror_message_ids=parse_int_ids(row["mirror_message_ids"]),
            mirror_requested_at=row["mirror_requested_at"],
        )

    async def mark_post_mirror_requested(self, post_id: int, requested_at: str) -> None:
        """Пост переслан боту: ждём id сообщений из его апдейтов."""
        await self._execute(
            """
            UPDATE posts
            SET media_state = 'requested', mirror_requested_at = ?
            WHERE id = ?
            """,
            (requested_at, post_id),
        )

    async def reset_post_mirror(self, post_id: int) -> None:
        """Сбрасывает старые id перед повторной пересылкой поста боту."""
        await self._execute(
            """
            UPDATE posts
            SET mirror_chat_id = NULL, mirror_message_ids = NULL,
                mirror_requested_at = NULL, media_state = 'pending'
            WHERE id = ?
            """,
            (post_id,),
        )

    async def attach_mirror_message(
        self,
        channel_tg_id: int,
        source_message_id: int,
        chat_id: int,
        message_id: int,
    ) -> int | None:
        """Сохраняет id сообщения из чата бота для поста-источника.

        В личных чатах id сообщений у каждого аккаунта свои, поэтому id,
        которые видит бот, нельзя получить из сессии сборщика — только из
        входящих пересылок.
        """
        rows = await self._fetchall(
            """
            SELECT p.id, p.source_message_ids, p.mirror_message_ids
            FROM posts p
            JOIN channels c ON c.id = p.channel_id
            WHERE c.tg_id = ? AND p.notified_at IS NULL
            ORDER BY p.id DESC
            """,
            (channel_tg_id,),
        )
        for row in rows:
            if source_message_id not in parse_int_ids(row["source_message_ids"]):
                continue
            ids = list(parse_int_ids(row["mirror_message_ids"]))
            if message_id in ids:
                return row["id"]
            ids.append(message_id)
            await self._execute(
                """
                UPDATE posts SET mirror_chat_id = ?, mirror_message_ids = ?
                WHERE id = ?
                """,
                (chat_id, join_int_ids(sorted(ids)), row["id"]),
            )
            return row["id"]
        return None

    async def set_post_mirror(
        self, post_id: int, chat_id: int, message_ids: Iterable[int]
    ) -> None:
        """Запоминает, где лежит копия поста в чате бота."""
        await self._execute(
            """
            UPDATE posts
            SET mirror_chat_id = ?, mirror_message_ids = ?, media_state = 'ok'
            WHERE id = ?
            """,
            (chat_id, join_int_ids(sorted(message_ids)), post_id),
        )

    async def mark_post_mirror_failed(self, post_id: int) -> None:
        """Пост нельзя скопировать: дальше шлём текстовую версию со ссылкой."""
        await self._execute(
            "UPDATE posts SET media_state = 'failed' WHERE id = ?", (post_id,)
        )

    async def mark_post_notified(self, post_id: int, notified_at: str) -> None:
        await self._execute(
            "UPDATE posts SET notified_at = ? WHERE id = ?", (notified_at, post_id)
        )

    # ----------------------------------------------------------------- helpers

    async def _fetchone(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        cursor = await self.conn.execute(sql, params)
        row = await cursor.fetchone()
        await cursor.close()
        return row

    async def _fetchall(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        cursor = await self.conn.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return list(rows)

    async def _execute(self, sql: str, params: tuple = ()) -> None:
        async with self._lock:
            await self.conn.execute(sql, params)
            await self.conn.commit()
