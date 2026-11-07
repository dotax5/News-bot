"""Асинхронный слой хранения на SQLite (aiosqlite)."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path

import aiosqlite

from .models import Channel, ChannelInfo, DigestPost, User, UserChannel
from .utils import initial_digest_date, iso_utc, parse_channel_ref, utcnow

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id            INTEGER NOT NULL UNIQUE,
    username         TEXT,
    first_name       TEXT,
    timezone         TEXT NOT NULL,
    digest_time      TEXT NOT NULL,
    enabled          INTEGER NOT NULL DEFAULT 1,
    last_digest_at   TEXT,
    last_digest_date TEXT,
    created_at       TEXT NOT NULL
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
    created_at  TEXT NOT NULL,
    UNIQUE (channel_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_posts_channel_posted
    ON posts(channel_id, posted_at);
CREATE INDEX IF NOT EXISTS idx_posts_posted ON posts(posted_at);
"""


def _user_from_row(row: aiosqlite.Row) -> User:
    return User(
        id=row["id"],
        tg_id=row["tg_id"],
        username=row["username"],
        first_name=row["first_name"],
        timezone=row["timezone"],
        digest_time=row["digest_time"],
        enabled=bool(row["enabled"]),
        last_digest_at=row["last_digest_at"],
        last_digest_date=row["last_digest_date"],
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
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------ users

    async def ensure_user(
        self,
        tg_id: int,
        username: str | None,
        first_name: str | None,
        default_timezone: str,
        default_digest_time: str,
        now: datetime | None = None,
    ) -> User:
        now = now or utcnow()
        last_date = initial_digest_date(now, default_timezone, default_digest_time)
        async with self._lock:
            await self.conn.execute(
                """
                INSERT INTO users (tg_id, username, first_name, timezone, digest_time,
                                   enabled, last_digest_at, last_digest_date, created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(tg_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name
                """,
                (
                    tg_id,
                    username,
                    first_name,
                    default_timezone,
                    default_digest_time,
                    iso_utc(now),
                    last_date,
                    iso_utc(now),
                ),
            )
            await self.conn.commit()
            row = await self._fetchone("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
        assert row is not None
        return _user_from_row(row)

    async def get_user(self, tg_id: int) -> User | None:
        row = await self._fetchone("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
        return _user_from_row(row) if row else None

    async def get_user_by_id(self, user_id: int) -> User | None:
        row = await self._fetchone("SELECT * FROM users WHERE id = ?", (user_id,))
        return _user_from_row(row) if row else None

    async def list_users(self, enabled_only: bool = False) -> list[User]:
        sql = "SELECT * FROM users"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        rows = await self._fetchall(sql)
        return [_user_from_row(row) for row in rows]

    async def set_user_digest_time(
        self, user_id: int, digest_time: str, last_digest_date: str | None
    ) -> None:
        await self._execute(
            "UPDATE users SET digest_time = ?, last_digest_date = ? WHERE id = ?",
            (digest_time, last_digest_date, user_id),
        )

    async def set_user_timezone(
        self, user_id: int, timezone: str, last_digest_date: str | None
    ) -> None:
        await self._execute(
            "UPDATE users SET timezone = ?, last_digest_date = ? WHERE id = ?",
            (timezone, last_digest_date, user_id),
        )

    async def set_user_enabled(self, user_id: int, enabled: bool) -> None:
        await self._execute(
            "UPDATE users SET enabled = ? WHERE id = ?", (1 if enabled else 0, user_id)
        )

    async def mark_digest_sent(self, user_id: int, sent_at: str, local_date: str) -> None:
        await self._execute(
            "UPDATE users SET last_digest_at = ?, last_digest_date = ? WHERE id = ?",
            (sent_at, local_date, user_id),
        )

    async def set_last_digest_at(self, user_id: int, sent_at: str) -> None:
        await self._execute(
            "UPDATE users SET last_digest_at = ? WHERE id = ?", (sent_at, user_id)
        )

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

    async def is_subscribed(self, user_id: int, channel_id: int) -> bool:
        row = await self._fetchone(
            "SELECT 1 FROM user_channels WHERE user_id = ? AND channel_id = ?",
            (user_id, channel_id),
        )
        return row is not None

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

    async def count_unchecked_channels(self, user_id: int) -> int:
        row = await self._fetchone(
            """
            SELECT COUNT(*) AS n
            FROM user_channels uc
            JOIN channels c ON c.id = uc.channel_id
            WHERE uc.user_id = ? AND c.last_checked_at IS NULL
            """,
            (user_id,),
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
        rows = await self._fetchall(
            """
            SELECT DISTINCT c.*
            FROM channels c
            JOIN user_channels uc ON uc.channel_id = c.id
            JOIN users u ON u.id = uc.user_id AND u.enabled = 1
            ORDER BY c.id
            """
        )
        return [_channel_from_row(row) for row in rows]

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
        posts: Iterable[tuple[int, str, str, str | None, bool]],
    ) -> int:
        now = iso_utc(utcnow())
        rows = [
            (channel_id, message_id, text, posted_at, url, 1 if has_media else 0, now)
            for message_id, text, posted_at, url, has_media in posts
        ]
        if not rows:
            return 0
        async with self._lock:
            cursor = await self.conn.executemany(
                """
                INSERT OR IGNORE INTO posts
                    (channel_id, message_id, text, posted_at, url, has_media, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            await self.conn.commit()
            return max(cursor.rowcount, 0)

    async def get_digest_posts(
        self, user_id: int, since: str, until: str, limit: int
    ) -> list[DigestPost]:
        rows = await self._fetchall(
            """
            SELECT * FROM (
                SELECT p.id, p.channel_id, p.message_id, p.text, p.posted_at, p.url, p.has_media,
                       c.title AS channel_title, c.username AS channel_username,
                       c.tg_id AS channel_peer_id
                FROM posts p
                JOIN channels c ON c.id = p.channel_id
                JOIN user_channels uc ON uc.channel_id = p.channel_id AND uc.user_id = ?
                WHERE p.posted_at > ? AND p.posted_at <= ?
                ORDER BY p.posted_at DESC
                LIMIT ?
            )
            ORDER BY posted_at ASC
            """,
            (user_id, since, until, limit),
        )
        return [
            DigestPost(
                id=row["id"],
                channel_id=row["channel_id"],
                message_id=row["message_id"],
                text=row["text"],
                posted_at=row["posted_at"],
                url=row["url"],
                has_media=bool(row["has_media"]),
                channel_title=row["channel_title"],
                channel_username=row["channel_username"],
                channel_peer_id=row["channel_peer_id"],
            )
            for row in rows
        ]

    async def count_digest_posts(self, user_id: int, since: str, until: str) -> int:
        row = await self._fetchone(
            """
            SELECT COUNT(*) AS n
            FROM posts p
            JOIN user_channels uc ON uc.channel_id = p.channel_id AND uc.user_id = ?
            WHERE p.posted_at > ? AND p.posted_at <= ?
            """,
            (user_id, since, until),
        )
        return int(row["n"]) if row else 0

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
