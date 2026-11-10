from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from telethon.tl.types import Channel as TgChannel
from telethon.tl.types import User as TgUser

from newsbot.collector import NewsCollector
from newsbot.db import Database
from newsbot.delivery import PostDelivery
from newsbot.mirror import MediaMirror
from newsbot.models import ChannelInfo


class FakeMessage:
    def __init__(
        self,
        message_id: int,
        text: str,
        date: datetime,
        *,
        media: object | None = None,
        grouped_id: int | None = None,
    ) -> None:
        self.id = message_id
        self.message = text
        self.date = date
        self.media = media
        self.action = None
        self.grouped_id = grouped_id


class FakeTelethon:
    def __init__(
        self,
        db: Database,
        messages: list[FakeMessage],
        channel_tg_id: int = -1000000000123,
    ) -> None:
        self.db = db
        self.messages = messages
        self.channel_tg_id = channel_tg_id
        self.channel = TgChannel(
            id=123, title="Тест", photo=None, date=None, username="test"
        )
        self.me = TgUser(id=9001, first_name="Collector", username="collector")
        self.bot = TgUser(id=999, bot=True, first_name="Bot", username="testbot")
        self.forward_calls: list[list[int]] = []

    async def get_entity(self, ref):
        if ref in ("testbot", "@testbot", 999):
            return self.bot
        return self.channel

    async def get_me(self):
        return self.me

    async def get_dialogs(self, limit=2000):
        return []

    async def get_messages(self, entity, limit=20):
        return list(reversed(self.messages[-limit:]))

    async def iter_messages(self, entity, min_id=0, limit=100, reverse=False):
        for message in self.messages:
            if message.id > min_id:
                yield message

    async def forward_messages(self, entity, ids, from_peer=None):
        self.forward_calls.append(list(ids))
        bot_ids = []
        for index, source_id in enumerate(ids):
            bot_id = 500 + index
            await self.db.attach_mirror_message(
                self.channel_tg_id, source_id, self.me.id, bot_id
            )
            bot_ids.append(bot_id)
        return [SimpleNamespace(id=value) for value in bot_ids]


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.copied: list[tuple[int, int, tuple[int, ...]]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.sent.append((chat_id, text))

    async def copy_messages(
        self, chat_id: int, from_chat_id: int, message_ids: list[int], **kwargs
    ) -> None:
        self.copied.append((chat_id, from_chat_id, tuple(message_ids)))


class EndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.sqlite3")
        await self.db.connect()
        self.now = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
        self.settings = SimpleNamespace(
            initial_fetch_limit=20,
            collect_interval=60,
            display_timezone="Europe/Moscow",
        )
        self.channel, _ = await self.db.add_or_update_channel(
            ChannelInfo(key="@test", tg_id=-100123, username="test", title="Тест")
        )
        self.user = await self.db.ensure_user(111, "user", "User", now=self.now)
        await self.db.subscribe(self.user.id, self.channel.id)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def test_new_album_is_collected_and_delivered_as_copy(self) -> None:
        client = FakeTelethon(
            self.db,
            [
                FakeMessage(1, "Старый пост", self.now - timedelta(hours=2)),
                FakeMessage(
                    2,
                    "Новый альбом",
                    self.now - timedelta(minutes=5),
                    media=object(),
                    grouped_id=10,
                ),
                FakeMessage(
                    3, "", self.now - timedelta(minutes=5), media=object(), grouped_id=10
                ),
            ],
        )
        collector = NewsCollector(self.settings, self.db)  # type: ignore[arg-type]
        collector._client = client

        # Первый проход: в историю уходят все посты, подписчику — только последний.
        await collector.collect_once()
        pending = await self.db.list_pending_posts()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].source_message_ids, (2, 3))

        mirror = MediaMirror(
            self.db,
            client,  # type: ignore[arg-type]
            "testbot",
            wait_timeout=0.05,
            poll_interval=0.001,
        )
        await mirror.start()
        bot = FakeBot()
        delivery = PostDelivery(
            bot,  # type: ignore[arg-type]
            self.db,
            self.settings,  # type: ignore[arg-type]
            send_delay=0,
            mirror=mirror,
        )

        await delivery.deliver_pending()

        self.assertEqual(client.forward_calls, [[2, 3]])
        self.assertEqual(bot.copied, [(111, 9001, (500, 501))])
        self.assertEqual(bot.sent, [])
        self.assertEqual(await self.db.list_pending_posts(), [])


if __name__ == "__main__":
    unittest.main()
