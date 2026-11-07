from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from newsbot.db import Database
from newsbot.models import ChannelInfo
from newsbot.utils import iso_utc


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.sqlite3")
        await self.db.connect()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def test_user_channel_posts_flow(self) -> None:
        now = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
        user = await self.db.ensure_user(
            100, "tester", "Test", "Europe/Moscow", "09:00", now=now
        )
        self.assertEqual(user.last_digest_date, "2026-11-07")

        info = ChannelInfo(
            key="@test", tg_id=-1001234567890, username="test", title="Тест"
        )
        channel, known = await self.db.add_or_update_channel(info)
        self.assertFalse(known)
        self.assertTrue(await self.db.subscribe(user.id, channel.id))

        self.assertEqual(await self.db.count_user_channels(user.id), 1)
        collect = await self.db.channels_to_collect()
        self.assertEqual([item.id for item in collect], [channel.id])

        posted_at = now - timedelta(hours=1)
        inserted = await self.db.insert_posts(
            channel.id,
            [(1, "Первая новость", iso_utc(posted_at), "https://t.me/test/1", False)],
        )
        self.assertEqual(inserted, 1)
        self.assertEqual(
            await self.db.insert_posts(
                channel.id,
                [(1, "Дубль", iso_utc(posted_at), None, False)],
            ),
            0,
        )

        posts = await self.db.get_digest_posts(
            user.id, iso_utc(now - timedelta(days=1)), iso_utc(now), 10
        )
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].text, "Первая новость")
        self.assertEqual(posts[0].channel_title, "Тест")

        await self.db.mark_digest_sent(user.id, iso_utc(now), "2026-11-07")
        updated = await self.db.get_user_by_id(user.id)
        assert updated is not None
        self.assertEqual(updated.last_digest_date, "2026-11-07")

        self.assertTrue(await self.db.unsubscribe(user.id, channel.id))
        self.assertEqual(await self.db.channels_to_collect(), [])

    async def test_user_digests_are_isolated(self) -> None:
        now = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
        alice = await self.db.ensure_user(
            1, "alice", "Alice", "Europe/Moscow", "09:00", now=now - timedelta(hours=2)
        )
        bob = await self.db.ensure_user(
            2, "bob", "Bob", "Europe/Moscow", "10:00", now=now - timedelta(hours=1)
        )

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
            [(1, "Новость Алисы", iso_utc(now - timedelta(minutes=90)), None, False)],
        )
        await self.db.insert_posts(
            bob_channel.id,
            [(1, "Новость Боба", iso_utc(now - timedelta(minutes=50)), None, False)],
        )
        await self.db.insert_posts(
            shared_channel.id,
            [(1, "Общая новость", iso_utc(now - timedelta(minutes=10)), None, False)],
        )

        alice_posts = await self.db.get_digest_posts(
            alice.id, iso_utc(now - timedelta(hours=3)), iso_utc(now), 50
        )
        bob_posts = await self.db.get_digest_posts(
            bob.id, iso_utc(now - timedelta(hours=2)), iso_utc(now), 50
        )
        self.assertEqual(
            [post.text for post in alice_posts], ["Новость Алисы", "Общая новость"]
        )
        self.assertEqual(
            [post.text for post in bob_posts], ["Новость Боба", "Общая новость"]
        )

        alice_channels = await self.db.list_user_channels_with_status(alice.id)
        bob_channels = await self.db.list_user_channels_with_status(bob.id)
        self.assertEqual(
            {item.channel.key for item in alice_channels}, {"@alice", "@shared"}
        )
        self.assertEqual(
            {item.channel.key for item in bob_channels}, {"@bob", "@shared"}
        )

        bob_before = await self.db.get_user_by_id(bob.id)
        assert bob_before is not None
        await self.db.mark_digest_sent(alice.id, iso_utc(now), "2026-11-07")
        bob_after = await self.db.get_user_by_id(bob.id)
        assert bob_after is not None
        self.assertEqual(bob_after.last_digest_at, bob_before.last_digest_at)
        self.assertEqual(bob_after.last_digest_date, bob_before.last_digest_date)

    async def test_env_seed_and_subscription(self) -> None:
        await self.db.seed_env_channels(["@one", "https://t.me/two", "bad ref !!"])
        user = await self.db.ensure_user(
            200, None, None, "Europe/Moscow", "09:00"
        )
        self.assertEqual(await self.db.subscribe_env_channels(user.id), 2)
        self.assertEqual(await self.db.count_user_channels(user.id), 2)


if __name__ == "__main__":
    unittest.main()
