from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from telethon.tl.types import Channel as TgChannel

from newsbot.collector import NewsCollector
from newsbot.db import Database
from newsbot.models import ChannelInfo
from newsbot.utils import iso_utc


class FakeMessage:
    def __init__(self, message_id: int, text: str, date: datetime) -> None:
        self.id = message_id
        self.message = text
        self.date = date
        self.media = None
        self.action = None
        self.grouped_id = None


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


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.sqlite3")
        await self.db.connect()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def test_initial_fetch_and_cursor(self) -> None:
        now = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
        user = await self.db.ensure_user(
            100, "tester", "Test", "Europe/Moscow", "09:00", now=now
        )
        channel, _ = await self.db.add_or_update_channel(
            ChannelInfo(key="@test", tg_id=-100123, username="test", title="Тест")
        )
        await self.db.subscribe(user.id, channel.id)

        messages = [
            FakeMessage(1, "Старая", now - timedelta(hours=2)),
            FakeMessage(2, "Средняя", now - timedelta(hours=1)),
            FakeMessage(3, "Новая", now - timedelta(minutes=5)),
        ]
        collector = NewsCollector(
            SimpleNamespace(initial_fetch_limit=20, collect_interval=300), self.db
        )
        collector._client = FakeTelethonClient(messages)

        inserted = await collector.collect_once()
        self.assertEqual(inserted, 3)

        stored = await self.db.get_digest_posts(
            user.id, iso_utc(now - timedelta(days=1)), iso_utc(now), 10
        )
        self.assertEqual([post.text for post in stored], ["Старая", "Средняя", "Новая"])

        channel_row = await self.db.find_user_channel_by_ref(user.id, "@test")
        assert channel_row is not None
        self.assertEqual(channel_row.last_message_id, 3)

        # Повторный проход ничего не дублирует.
        self.assertEqual(await collector.collect_once(), 0)


if __name__ == "__main__":
    unittest.main()
