from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from telethon.tl.types import Channel as TgChannel
from telethon.tl.types import MessageMediaPhoto

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


class FakeTelethon:
    def __init__(
        self,
        db: Database,
        messages: list[FakeMessage],
        media_dir: Path,
    ) -> None:
        self.db = db
        self.messages = messages
        self.media_dir = media_dir
        self.channel = TgChannel(
            id=123, title="Тест", photo=None, date=None, username="test"
        )
        self.downloads: list[int] = []

    async def get_entity(self, ref):
        return self.channel

    async def get_dialogs(self, limit=2000):
        return []

    async def get_messages(self, entity, limit=20):
        return list(reversed(self.messages[-limit:]))

    async def iter_messages(self, entity, min_id=0, limit=100, reverse=False):
        for message in self.messages:
            if message.id > min_id:
                yield message

    async def download_media(self, message, file: str) -> str:
        self.downloads.append(message.id)
        target = Path(file) / f"{message.id}.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"photo")
        return str(target)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.photos: list[int] = []
        self.albums: list[tuple[int, int]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.sent.append((chat_id, text))

    async def send_photo(self, chat_id: int, photo, caption=None, **kwargs) -> None:
        self.photos.append(chat_id)

    async def send_media_group(self, chat_id: int, media, **kwargs) -> None:
        self.albums.append((chat_id, len(media)))


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
            data_dir=Path(self._tmp.name),
        )
        self.channel, _ = await self.db.add_or_update_channel(
            ChannelInfo(key="@test", tg_id=-100123, username="test", title="Тест")
        )
        self.user = await self.db.ensure_user(111, "user", "User", now=self.now)
        await self.db.subscribe(self.user.id, self.channel.id)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def test_album_is_downloaded_then_sent_as_media_group(self) -> None:
        client = FakeTelethon(
            self.db,
            [
                FakeMessage(1, "Старый пост", self.now - timedelta(hours=2)),
                FakeMessage(
                    2,
                    "Новый альбом",
                    self.now - timedelta(minutes=5),
                    media=MessageMediaPhoto(photo=None),
                    grouped_id=10,
                ),
                FakeMessage(
                    3,
                    "",
                    self.now - timedelta(minutes=5),
                    media=MessageMediaPhoto(photo=None),
                    grouped_id=10,
                ),
            ],
            Path(self._tmp.name),
        )
        collector = NewsCollector(self.settings, self.db)  # type: ignore[arg-type]
        collector._client = client  # type: ignore[assignment]

        # Первый проход: в историю уходят все посты, подписчику — только последний.
        await collector.collect_once()
        pending = await self.db.list_pending_posts()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].source_message_ids, (2, 3))
        # Медиа скачано заранее (оба элемента альбома).
        self.assertEqual(client.downloads, [2, 3])
        self.assertEqual(len(pending[0].media), 2)
        self.assertTrue(all(item.path for item in pending[0].media))

        bot = FakeBot()
        delivery = PostDelivery(
            bot,  # type: ignore[arg-type]
            self.db,
            self.settings,  # type: ignore[arg-type]
            send_delay=0,
        )

        await delivery.deliver_pending()

        self.assertEqual(bot.albums, [(111, 2)])
        self.assertEqual(bot.sent, [])
        self.assertEqual(await self.db.list_pending_posts(), [])


if __name__ == "__main__":
    unittest.main()
