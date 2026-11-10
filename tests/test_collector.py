from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from telethon.tl.types import Channel as TgChannel

from newsbot.collector import NewsCollector
from newsbot.db import Database
from newsbot.delivery import PostDelivery
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


class FakeTelethonClient:
    def __init__(self, messages: list[FakeMessage]) -> None:
        self.messages = messages
        self.entity = TgChannel(id=123, title="Тест", photo=None, date=None, username="test")

    async def get_entity(self, ref):
        return self.entity

    async def get_dialogs(self, limit=2000):
        return []

    async def get_messages(self, entity, limit=20):
        return list(reversed(self.messages[-limit:]))

    async def iter_messages(self, entity, min_id=0, limit=100, reverse=False):
        selected = [m for m in self.messages if m.id > min_id][:limit]
        if reverse:
            for message in selected:
                yield message
        else:
            for message in reversed(selected):
                yield message

    def push(self, message: FakeMessage) -> None:
        self.messages.append(message)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.sent.append((chat_id, text))


class CollectorTests(unittest.IsolatedAsyncioTestCase):
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
        self.bot = FakeBot()
        self.delivery = PostDelivery(
            self.bot,  # type: ignore[arg-type]
            self.db,
            self.settings,  # type: ignore[arg-type]
            send_delay=0,
        )
        self.user = await self.db.ensure_user(100, "tester", "Test", now=self.now)
        self.channel, _ = await self.db.add_or_update_channel(
            ChannelInfo(key="@test", tg_id=-100123, username="test", title="Тест")
        )
        await self.db.subscribe(self.user.id, self.channel.id)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    def _collector(
        self, client: FakeTelethonClient, *, with_delivery: bool = True
    ) -> NewsCollector:
        collector = NewsCollector(
            self.settings,  # type: ignore[arg-type]
            self.db,
            self.delivery if with_delivery else None,
        )
        collector._client = client
        return collector

    async def test_first_pass_sends_only_latest_post(self) -> None:
        messages = [
            FakeMessage(1, "Старая", self.now - timedelta(hours=2)),
            FakeMessage(2, "Средняя", self.now - timedelta(hours=1)),
            FakeMessage(3, "Новая", self.now - timedelta(minutes=5)),
        ]
        collector = self._collector(FakeTelethonClient(messages))

        self.assertEqual(await collector.collect_once(), 3)

        self.assertEqual(len(self.bot.sent), 1)
        chat_id, text = self.bot.sent[0]
        self.assertEqual(chat_id, 100)
        self.assertIn("Новая", text)
        self.assertNotIn("Старая", text)
        self.assertIn("https://t.me/test/3", text)

        channel = await self.db.find_user_channel_by_ref(self.user.id, "@test")
        assert channel is not None
        self.assertEqual(channel.last_message_id, 3)
        self.assertEqual(await self.db.list_pending_posts(), [])

    async def test_new_post_is_sent_on_next_pass(self) -> None:
        client = FakeTelethonClient(
            [FakeMessage(3, "Новая", self.now - timedelta(minutes=5))]
        )
        collector = self._collector(client)
        await collector.collect_once()

        client.push(FakeMessage(4, "Свежая", self.now))
        self.assertEqual(await collector.collect_once(), 1)

        self.assertEqual(len(self.bot.sent), 2)
        self.assertIn("Свежая", self.bot.sent[1][1])
        self.assertIn("https://t.me/test/4", self.bot.sent[1][1])

        # Повторный проход без новых постов ничего не отправляет и не дублирует.
        self.assertEqual(await collector.collect_once(), 0)
        self.assertEqual(len(self.bot.sent), 2)

    async def test_paused_user_gets_no_backlog_after_resume(self) -> None:
        await self.db.set_user_enabled(self.user.id, False)
        client = FakeTelethonClient(
            [FakeMessage(3, "Новая", self.now - timedelta(minutes=5))]
        )
        collector = self._collector(client)

        await collector.collect_once()
        self.assertEqual(self.bot.sent, [])

        client.push(FakeMessage(4, "Пока на паузе", self.now))
        await collector.collect_once()
        self.assertEqual(self.bot.sent, [])

        await self.db.set_user_enabled(self.user.id, True)
        await collector.collect_once()
        self.assertEqual(self.bot.sent, [])

    async def test_without_delivery_latest_post_stays_pending(self) -> None:
        messages = [
            FakeMessage(1, "Старая", self.now - timedelta(hours=2)),
            FakeMessage(2, "Новая", self.now - timedelta(minutes=5)),
        ]
        collector = self._collector(
            FakeTelethonClient(messages), with_delivery=False
        )

        await collector.collect_once()

        self.assertEqual(
            [post.text for post in await self.db.list_pending_posts()], ["Новая"]
        )

    async def test_album_is_stored_as_one_post(self) -> None:
        await self.db.update_channel_progress(
            self.channel.id, 9, self.now.isoformat()
        )
        messages = [
            FakeMessage(
                10, "Подпись к альбому", self.now, media=object(), grouped_id=77
            ),
            FakeMessage(11, "", self.now, media=object(), grouped_id=77),
            FakeMessage(12, "", self.now, media=object(), grouped_id=77),
        ]
        collector = self._collector(FakeTelethonClient(messages), with_delivery=False)

        self.assertEqual(await collector.collect_once(), 1)

        pending = await self.db.list_pending_posts()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].source_message_ids, (10, 11, 12))
        self.assertEqual(pending[0].text, "Подпись к альбому")
        self.assertTrue(pending[0].has_media)
        self.assertEqual(pending[0].url, "https://t.me/test/10")

    async def test_first_pass_album_sends_only_latest_post(self) -> None:
        messages = [
            FakeMessage(10, "Старый альбом", self.now - timedelta(hours=1), grouped_id=1),
            FakeMessage(11, "", self.now - timedelta(hours=1), grouped_id=1),
            FakeMessage(12, "Новый альбом", self.now, media=object(), grouped_id=2),
            FakeMessage(13, "", self.now, media=object(), grouped_id=2),
        ]
        collector = self._collector(FakeTelethonClient(messages))

        await collector.collect_once()

        self.assertEqual(len(self.bot.sent), 1)
        self.assertIn("Новый альбом", self.bot.sent[0][1])
        self.assertNotIn("Старый альбом", self.bot.sent[0][1])
        self.assertEqual(await self.db.list_pending_posts(), [])


if __name__ == "__main__":
    unittest.main()
