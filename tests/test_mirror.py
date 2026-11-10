from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon.errors import FloodWaitError
from telethon.tl.types import Channel as TgChannel
from telethon.tl.types import User as TgUser

from newsbot.db import Database
from newsbot.mirror import MediaMirror, MirrorError, MirrorPending, MirrorRetry
from newsbot.models import ChannelInfo, NewPost
from newsbot.utils import iso_utc


class FakeTelethon:
    def __init__(
        self,
        db: Database,
        *,
        mapped_ids: list[int] | None = None,
        errors: list[Exception] | None = None,
        bot_entity: object | None = None,
        channel_tg_id: int = -100123,
    ) -> None:
        self.db = db
        self.mapped_ids = mapped_ids
        self.errors = list(errors or [])
        self.forward_calls: list[list[int]] = []
        self.channel_tg_id = channel_tg_id
        self._me = TgUser(id=9001, first_name="Collector", username="collector")
        self._bot_entity = bot_entity or TgUser(
            id=999, bot=True, first_name="Bot", username="testbot"
        )

    async def get_me(self):
        return self._me

    async def get_entity(self, ref):
        if ref in ("testbot", "@testbot", 999):
            return self._bot_entity
        return TgChannel(
            id=123, title="Тест", photo=None, date=None, username="test"
        )

    async def forward_messages(self, entity, ids, from_peer=None):
        if self.errors:
            raise self.errors.pop(0)
        self.forward_calls.append(list(ids))
        if self.mapped_ids:
            # Имитация бота: он получает пересылку и сохраняет свои id.
            for source_id, bot_id in zip(ids, self.mapped_ids):
                await self.db.attach_mirror_message(
                    self.channel_tg_id, source_id, self._me.id, bot_id
                )
        return [SimpleNamespace(id=value) for value in (self.mapped_ids or [])]


class MirrorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.sqlite3")
        await self.db.connect()
        self.now = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
        self.channel, _ = await self.db.add_or_update_channel(
            ChannelInfo(key="@test", tg_id=-100123, username="test", title="Тест")
        )
        await self.db.insert_posts(
            self.channel.id,
            [
                NewPost(
                    message_id=10,
                    source_message_ids=(10, 11, 12),
                    text="Подпись",
                    posted_at=iso_utc(self.now),
                    url="https://t.me/test/10",
                    has_media=True,
                )
            ],
            notify=False,
        )
        self.post = (await self.db.list_pending_posts())[0]

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def _mirror(self, client: FakeTelethon, **kwargs) -> MediaMirror:
        mirror = MediaMirror(
            self.db,
            client,  # type: ignore[arg-type]
            "testbot",
            wait_timeout=0.05,
            poll_interval=0.001,
            **kwargs,
        )
        await mirror.start()
        return mirror

    async def test_bot_ids_are_stored_and_used(self) -> None:
        client = FakeTelethon(self.db, mapped_ids=[700, 701, 702])
        mirror = await self._mirror(client)

        result = await mirror.ensure(self.post)

        assert result is not None
        self.assertEqual(result.chat_id, 9001)
        self.assertEqual(result.message_ids, (700, 701, 702))
        self.assertEqual(client.forward_calls, [[10, 11, 12]])

        stored = await self.db.get_post_mirror(self.post.id)
        assert stored is not None
        self.assertEqual(stored.media_state, "ok")
        self.assertEqual(stored.mirror_message_ids, (700, 701, 702))

    async def test_stored_copy_is_reused(self) -> None:
        client = FakeTelethon(self.db, mapped_ids=[700, 701, 702])
        mirror = await self._mirror(client)
        await mirror.ensure(self.post)

        result = await mirror.ensure(self.post)

        assert result is not None
        self.assertEqual(result.message_ids, (700, 701, 702))
        self.assertEqual(len(client.forward_calls), 1)

    async def test_missing_bot_ids_raise_pending(self) -> None:
        client = FakeTelethon(self.db)
        mirror = await self._mirror(client)

        with self.assertRaises(MirrorPending):
            await mirror.ensure(self.post)

        stored = await self.db.get_post_mirror(self.post.id)
        assert stored is not None
        self.assertEqual(stored.media_state, "requested")
        self.assertIsNotNone(stored.mirror_requested_at)
        self.assertEqual(stored.mirror_message_ids, ())

        # Повторная попытка не пересылает пост ещё раз.
        with self.assertRaises(MirrorPending):
            await mirror.ensure(self.post)
        self.assertEqual(len(client.forward_calls), 1)

    async def test_expired_request_falls_back_to_text(self) -> None:
        client = FakeTelethon(self.db)
        mirror = await self._mirror(client)
        old = iso_utc(datetime.now(UTC) - timedelta(hours=1))
        await self.db.mark_post_mirror_requested(self.post.id, old)

        self.assertIsNone(await mirror.ensure(self.post))

        stored = await self.db.get_post_mirror(self.post.id)
        assert stored is not None
        self.assertEqual(stored.media_state, "failed")

    async def test_permanent_error_marks_post_failed(self) -> None:
        client = FakeTelethon(
            self.db, errors=[ValueError("нет доступа"), ValueError("нет доступа")]
        )
        mirror = await self._mirror(client)

        self.assertIsNone(await mirror.ensure(self.post))

        stored = await self.db.get_post_mirror(self.post.id)
        assert stored is not None
        self.assertEqual(stored.media_state, "failed")
        self.assertEqual(client.forward_calls, [])

    async def test_flood_wait_is_retried(self) -> None:
        client = FakeTelethon(
            self.db,
            mapped_ids=[700, 701, 702],
            errors=[FloodWaitError(request=None, capture=1)],
        )
        mirror = await self._mirror(client)

        with patch("newsbot.mirror.asyncio.sleep", new=AsyncMock()):
            result = await mirror.ensure(self.post)

        assert result is not None
        self.assertEqual(result.message_ids, (700, 701, 702))
        self.assertEqual(client.forward_calls, [[10, 11, 12]])

    async def test_double_flood_wait_keeps_post_pending(self) -> None:
        client = FakeTelethon(
            self.db,
            errors=[
                FloodWaitError(request=None, capture=1),
                FloodWaitError(request=None, capture=1),
            ],
        )
        mirror = await self._mirror(client)

        with (
            patch("newsbot.mirror.asyncio.sleep", new=AsyncMock()),
            self.assertRaises(MirrorRetry),
        ):
            await mirror.ensure(self.post)

        stored = await self.db.get_post_mirror(self.post.id)
        assert stored is not None
        self.assertEqual(stored.media_state, "pending")

    async def test_disabled_mirror_does_nothing(self) -> None:
        client = FakeTelethon(self.db, mapped_ids=[700])
        mirror = MediaMirror(
            self.db, client, "testbot", enabled=False  # type: ignore[arg-type]
        )

        self.assertIsNone(await mirror.ensure(self.post))
        self.assertEqual(client.forward_calls, [])

    async def test_start_requires_bot(self) -> None:
        client = FakeTelethon(
            self.db,
            bot_entity=TgChannel(id=123, title="Тест", photo=None, date=None),
        )
        mirror = MediaMirror(self.db, client, "testbot")  # type: ignore[arg-type]

        with self.assertRaises(MirrorError):
            await mirror.start()


if __name__ == "__main__":
    unittest.main()
