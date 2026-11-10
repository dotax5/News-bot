from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from newsbot.db import Database
from newsbot.models import ChannelInfo, NewPost
from newsbot.utils import iso_utc


def make_post(
    message_id: int,
    text: str,
    posted_at: str,
    url: str | None = None,
    *,
    has_media: bool = False,
    source_ids: tuple[int, ...] | None = None,
) -> NewPost:
    return NewPost(
        message_id=message_id,
        source_message_ids=source_ids or (message_id,),
        text=text,
        posted_at=posted_at,
        url=url,
        has_media=has_media,
    )

LEGACY_SCHEMA = """
CREATE TABLE users (
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

CREATE TABLE channels (
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

CREATE TABLE user_channels (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    added_at   TEXT NOT NULL,
    PRIMARY KEY (user_id, channel_id)
);

CREATE TABLE posts (
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
"""


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.sqlite3")
        await self.db.connect()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def test_pending_posts_and_subscribers(self) -> None:
        now = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
        user = await self.db.ensure_user(100, "tester", "Test", now=now)
        self.assertTrue(user.enabled)

        info = ChannelInfo(
            key="@test", tg_id=-1001234567890, username="test", title="Тест"
        )
        channel, known = await self.db.add_or_update_channel(info)
        self.assertFalse(known)
        self.assertTrue(await self.db.subscribe(user.id, channel.id))

        self.assertEqual(await self.db.count_user_channels(user.id), 1)
        self.assertEqual(
            [item.id for item in await self.db.channels_to_collect()], [channel.id]
        )
        self.assertEqual(
            [item.id for item in await self.db.list_channel_subscribers(channel.id)],
            [user.id],
        )

        posted_at = iso_utc(now - timedelta(hours=1))
        inserted = await self.db.insert_posts(
            channel.id,
            [make_post(1, "Первая новость", posted_at, "https://t.me/test/1")],
            notify=False,
        )
        self.assertEqual(inserted, 1)
        self.assertEqual(
            await self.db.insert_posts(
                channel.id,
                [make_post(1, "Дубль", posted_at)],
            ),
            0,
        )

        pending = await self.db.list_pending_posts()
        self.assertEqual([post.text for post in pending], ["Первая новость"])
        self.assertEqual(pending[0].channel_title, "Тест")
        self.assertEqual(pending[0].channel_username, "test")
        self.assertEqual(pending[0].url, "https://t.me/test/1")

        await self.db.mark_post_notified(pending[0].id, iso_utc(now))
        self.assertEqual(await self.db.list_pending_posts(), [])

        # Уведомление о новом посте из insert_posts(notify=True) не ждёт рассылки.
        await self.db.insert_posts(
            channel.id,
            [make_post(2, "Вторая новость", posted_at)],
        )
        self.assertEqual(await self.db.list_pending_posts(), [])

        # Пауза выключает доставку, но канал продолжает собираться,
        # чтобы курсор не отставал и после /resume не было залпа.
        await self.db.set_user_enabled(user.id, False)
        self.assertEqual(await self.db.list_channel_subscribers(channel.id), [])
        self.assertEqual(
            [item.id for item in await self.db.channels_to_collect()], [channel.id]
        )

        self.assertTrue(await self.db.unsubscribe(user.id, channel.id))
        self.assertEqual(await self.db.channels_to_collect(), [])

    async def test_subscriptions_are_isolated(self) -> None:
        now = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
        alice = await self.db.ensure_user(1, "alice", "Alice", now=now)
        bob = await self.db.ensure_user(2, "bob", "Bob", now=now)

        alice_channel, _ = await self.db.add_or_update_channel(
            ChannelInfo(key="@alice", tg_id=-1001, username="alice", title="Alice News")
        )
        bob_channel, _ = await self.db.add_or_update_channel(
            ChannelInfo(key="@bob", tg_id=-1002, username="bob", title="Bob News")
        )
        shared_channel, _ = await self.db.add_or_update_channel(
            ChannelInfo(key="@shared", tg_id=-1003, username="shared", title="Shared")
        )
        await self.db.subscribe(alice.id, alice_channel.id)
        await self.db.subscribe(alice.id, shared_channel.id)
        await self.db.subscribe(bob.id, bob_channel.id)
        await self.db.subscribe(bob.id, shared_channel.id)

        await self.db.insert_posts(
            alice_channel.id,
            [make_post(1, "Новость Алисы", iso_utc(now - timedelta(minutes=90)))],
            notify=False,
        )
        await self.db.insert_posts(
            bob_channel.id,
            [make_post(1, "Новость Боба", iso_utc(now - timedelta(minutes=50)))],
            notify=False,
        )
        await self.db.insert_posts(
            shared_channel.id,
            [make_post(1, "Общая новость", iso_utc(now - timedelta(minutes=10)))],
            notify=False,
        )

        self.assertEqual(
            {post.text for post in await self.db.list_pending_posts()},
            {"Новость Алисы", "Новость Боба", "Общая новость"},
        )
        self.assertEqual(
            [user.id for user in await self.db.list_channel_subscribers(alice_channel.id)],
            [alice.id],
        )
        self.assertEqual(
            [user.id for user in await self.db.list_channel_subscribers(shared_channel.id)],
            [alice.id, bob.id],
        )

        alice_channels = await self.db.list_user_channels_with_status(alice.id)
        bob_channels = await self.db.list_user_channels_with_status(bob.id)
        self.assertEqual(
            {item.channel.key for item in alice_channels}, {"@alice", "@shared"}
        )
        self.assertEqual(
            {item.channel.key for item in bob_channels}, {"@bob", "@shared"}
        )

        self.assertIsNone(await self.db.last_channel_check_at(alice.id))
        self.assertIsNone(await self.db.last_channel_check_at(bob.id))
        await self.db.update_channel_progress(
            alice_channel.id, 10, iso_utc(now)
        )
        self.assertEqual(await self.db.last_channel_check_at(alice.id), iso_utc(now))
        self.assertIsNone(await self.db.last_channel_check_at(bob.id))

    async def test_env_seed_and_subscription(self) -> None:
        await self.db.seed_env_channels(["@one", "https://t.me/two", "bad ref !!"])
        user = await self.db.ensure_user(200, None, None)
        self.assertEqual(await self.db.subscribe_env_channels(user.id), 2)
        self.assertEqual(await self.db.count_user_channels(user.id), 2)

    async def test_legacy_database_is_migrated(self) -> None:
        await self.db.close()
        path = self.db.path
        path.unlink(missing_ok=True)
        created_at = "2026-11-07T06:00:00+00:00"
        con = sqlite3.connect(path)
        try:
            con.executescript(LEGACY_SCHEMA)
            con.execute(
                """
                INSERT INTO users (tg_id, username, first_name, timezone, digest_time,
                                   enabled, last_digest_at, last_digest_date, created_at)
                VALUES (100, 'legacy', 'Legacy', 'Europe/Moscow', '09:00', 1,
                        ?, '2026-11-07', ?)
                """,
                (created_at, created_at),
            )
            con.execute(
                """
                INSERT INTO channels (key, tg_id, username, title, source,
                                      last_message_id, created_at)
                VALUES ('@test', -100123, 'test', 'Тест', 'user', 5, ?)
                """,
                (created_at,),
            )
            con.execute(
                "INSERT INTO user_channels (user_id, channel_id, added_at) VALUES (1, 1, ?)",
                (created_at,),
            )
            con.execute(
                """
                INSERT INTO posts (channel_id, message_id, text, posted_at, url,
                                   has_media, created_at)
                VALUES (1, 5, 'Старый пост', ?, NULL, 0, ?)
                """,
                (created_at, created_at),
            )
            con.commit()
        finally:
            con.close()

        await self.db.connect()

        user = await self.db.get_user(100)
        assert user is not None
        self.assertEqual(user.username, "legacy")
        self.assertTrue(user.enabled)
        self.assertEqual(await self.db.count_user_channels(user.id), 1)

        cursor = await self.db.conn.execute("PRAGMA table_info(users)")
        columns = {row["name"] for row in await cursor.fetchall()}
        await cursor.close()
        self.assertFalse({"timezone", "digest_time"} & columns)

        # Посты прежних версий уже разошлись в дайджестах — повторно не шлём.
        self.assertEqual(await self.db.list_pending_posts(), [])

        cursor = await self.db.conn.execute(
            "SELECT source_message_ids, media_state FROM posts WHERE message_id = 5"
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None
        self.assertEqual(row["source_message_ids"], "5")
        self.assertEqual(row["media_state"], "pending")

        channel, _ = await self.db.add_or_update_channel(
            ChannelInfo(key="@test", tg_id=-100123, username="test", title="Тест")
        )
        await self.db.insert_posts(
            channel.id,
            [make_post(6, "Новый пост", created_at, "https://t.me/test/6")],
            notify=False,
        )
        self.assertEqual(
            [post.text for post in await self.db.list_pending_posts()], ["Новый пост"]
        )

    async def test_media_columns_are_migrated_from_previous_version(self) -> None:
        await self.db.close()
        path = self.db.path
        path.unlink(missing_ok=True)
        created_at = "2026-11-07T06:00:00+00:00"
        con = sqlite3.connect(path)
        try:
            con.executescript(LEGACY_SCHEMA)
            con.execute("ALTER TABLE posts ADD COLUMN notified_at TEXT")
            con.execute(
                """
                INSERT INTO channels (key, tg_id, username, title, source,
                                      last_message_id, created_at)
                VALUES ('@test', -100123, 'test', 'Тест', 'user', 5, ?)
                """,
                (created_at,),
            )
            con.execute(
                """
                INSERT INTO posts (channel_id, message_id, text, posted_at, url,
                                   has_media, created_at, notified_at)
                VALUES (1, 5, 'Старый пост', ?, NULL, 1, ?, NULL)
                """,
                (created_at, created_at),
            )
            con.commit()
        finally:
            con.close()

        await self.db.connect()

        cursor = await self.db.conn.execute("PRAGMA table_info(posts)")
        columns = {row["name"] for row in await cursor.fetchall()}
        await cursor.close()
        self.assertTrue(
            {
                "source_message_ids",
                "mirror_chat_id",
                "mirror_message_ids",
                "mirror_requested_at",
                "media_state",
            }
            <= columns
        )

        pending = await self.db.list_pending_posts()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].source_message_ids, (5,))
        self.assertEqual(pending[0].mirror_message_ids, ())
        self.assertEqual(pending[0].media_state, "pending")
        self.assertTrue(pending[0].has_media)

    async def test_post_mirror_state(self) -> None:
        now = datetime(2026, 11, 7, 8, 0, tzinfo=UTC)
        channel, _ = await self.db.add_or_update_channel(
            ChannelInfo(key="@album", tg_id=-100999, username="album", title="Альбом")
        )
        await self.db.insert_posts(
            channel.id,
            [
                make_post(
                    10,
                    "Подпись",
                    iso_utc(now),
                    "https://t.me/album/10",
                    has_media=True,
                    source_ids=(10, 11, 12),
                )
            ],
            notify=False,
        )

        post = (await self.db.list_pending_posts())[0]
        self.assertEqual(post.source_message_ids, (10, 11, 12))
        self.assertEqual(post.mirror_message_ids, ())
        self.assertEqual(post.channel_tg_id, -100999)

        await self.db.set_post_mirror(post.id, 777, (502, 501, 500))
        post = (await self.db.list_pending_posts())[0]
        self.assertEqual(post.mirror_chat_id, 777)
        self.assertEqual(post.mirror_message_ids, (500, 501, 502))
        self.assertEqual(post.copy_message_ids, (500, 501, 502))
        self.assertEqual(post.media_state, "ok")

        await self.db.mark_post_mirror_failed(post.id)
        self.assertEqual((await self.db.list_pending_posts())[0].media_state, "failed")

    async def test_attach_mirror_message_from_bot(self) -> None:
        now = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
        channel, _ = await self.db.add_or_update_channel(
            ChannelInfo(key="@attach", tg_id=-100998, username="attach", title="A")
        )
        await self.db.insert_posts(
            channel.id,
            [
                make_post(
                    10,
                    "Альбом",
                    iso_utc(now),
                    has_media=True,
                    source_ids=(10, 11),
                )
            ],
            notify=False,
        )
        post = (await self.db.list_pending_posts())[0]

        self.assertEqual(post.mirror_requested_at, None)
        await self.db.mark_post_mirror_requested(post.id, iso_utc(now))
        state = await self.db.get_post_mirror(post.id)
        assert state is not None
        self.assertEqual(state.media_state, "requested")
        self.assertEqual(state.mirror_requested_at, iso_utc(now))

        # Бот присылает свои id; дубликат не добавляется.
        self.assertEqual(
            await self.db.attach_mirror_message(-100998, 11, 9001, 501), post.id
        )
        await self.db.attach_mirror_message(-100998, 11, 9001, 501)
        await self.db.attach_mirror_message(-100998, 10, 9001, 500)
        state = await self.db.get_post_mirror(post.id)
        assert state is not None
        self.assertEqual(state.mirror_chat_id, 9001)
        self.assertEqual(state.mirror_message_ids, (500, 501))
        # media_state остаётся requested, пока доставка не подтвердит копию.
        self.assertEqual(state.media_state, "requested")

        # Чужой источник и чужой канал игнорируются.
        self.assertIsNone(await self.db.attach_mirror_message(-100998, 99, 9001, 999))
        self.assertIsNone(await self.db.attach_mirror_message(-100111, 10, 9001, 500))

        await self.db.reset_post_mirror(post.id)
        state = await self.db.get_post_mirror(post.id)
        assert state is not None
        self.assertEqual(state.media_state, "pending")
        self.assertEqual(state.mirror_message_ids, ())
        self.assertIsNone(state.mirror_requested_at)

        # Уже разосланный пост бот не трогает.
        await self.db.mark_post_notified(post.id, iso_utc(now))
        self.assertIsNone(await self.db.attach_mirror_message(-100998, 10, 9001, 700))


if __name__ == "__main__":
    unittest.main()
