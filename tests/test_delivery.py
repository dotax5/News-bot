from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from newsbot.db import Database
from newsbot.delivery import PostDelivery
from newsbot.models import ChannelInfo, MediaItem, NewPost
from newsbot.utils import iso_utc


class FakeBot:
    def __init__(self, errors: dict[int, list[Exception]] | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self.photos: list[tuple[int, str | None]] = []
        self.videos: list[tuple[int, str | None]] = []
        self.documents: list[tuple[int, str | None]] = []
        self.albums: list[tuple[int, int]] = []
        self._errors = {key: list(value) for key, value in (errors or {}).items()}

    async def _maybe_fail(self, chat_id: int) -> None:
        errors = self._errors.get(chat_id)
        if errors:
            raise errors.pop(0)

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        await self._maybe_fail(chat_id)
        self.sent.append((chat_id, text))

    async def send_photo(self, chat_id: int, photo, caption=None, **kwargs) -> None:
        await self._maybe_fail(chat_id)
        self.photos.append((chat_id, caption))

    async def send_video(self, chat_id: int, video, caption=None, **kwargs) -> None:
        await self._maybe_fail(chat_id)
        self.videos.append((chat_id, caption))

    async def send_animation(self, chat_id: int, animation, caption=None, **kwargs) -> None:
        await self._maybe_fail(chat_id)
        self.photos.append((chat_id, caption))

    async def send_audio(self, chat_id: int, audio, caption=None, **kwargs) -> None:
        await self._maybe_fail(chat_id)
        self.documents.append((chat_id, caption))

    async def send_voice(self, chat_id: int, voice, caption=None, **kwargs) -> None:
        await self._maybe_fail(chat_id)
        self.documents.append((chat_id, caption))

    async def send_document(self, chat_id: int, document, caption=None, **kwargs) -> None:
        await self._maybe_fail(chat_id)
        self.documents.append((chat_id, caption))

    async def send_media_group(self, chat_id: int, media, **kwargs) -> None:
        await self._maybe_fail(chat_id)
        self.albums.append((chat_id, len(media)))


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.sqlite3")
        await self.db.connect()
        self.now = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
        self.channel, _ = await self.db.add_or_update_channel(
            ChannelInfo(key="@test", tg_id=-100123, username="test", title="Тест")
        )

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def _subscribe(self, tg_id: int, *, enabled: bool = True):
        user = await self.db.ensure_user(tg_id, f"user{tg_id}", "Test", now=self.now)
        await self.db.subscribe(user.id, self.channel.id)
        if not enabled:
            await self.db.set_user_enabled(user.id, False)
        return user

    async def _insert_post(
        self,
        message_id: int = 1,
        text: str = "Новая новость",
        *,
        source_ids: tuple[int, ...] | None = None,
        has_media: bool = False,
    ) -> None:
        await self.db.insert_posts(
            self.channel.id,
            [
                NewPost(
                    message_id=message_id,
                    source_message_ids=source_ids or (message_id,),
                    text=text,
                    posted_at=iso_utc(self.now - timedelta(minutes=5)),
                    url=f"https://t.me/test/{message_id}",
                    has_media=has_media,
                )
            ],
            notify=False,
        )

    def _file(self, name: str) -> str:
        path = Path(self._tmp.name) / name
        path.write_bytes(b"media")
        return str(path)

    def _delivery(self, bot: FakeBot) -> PostDelivery:
        return PostDelivery(
            bot,  # type: ignore[arg-type]
            self.db,
            SimpleNamespace(display_timezone="Europe/Moscow"),  # type: ignore[arg-type]
            send_delay=0,
        )

    async def test_sends_post_to_every_subscriber(self) -> None:
        await self._subscribe(111)
        await self._subscribe(222)
        await self._insert_post()
        bot = FakeBot()

        delivered = await self._delivery(bot).deliver_pending()

        self.assertEqual(delivered, 1)
        self.assertEqual([chat_id for chat_id, _ in bot.sent], [111, 222])
        self.assertIn("📢 <b>Тест</b> · @test", bot.sent[0][1])
        self.assertIn("Новая новость", bot.sent[0][1])
        self.assertIn("https://t.me/test/1", bot.sent[0][1])
        self.assertEqual(await self.db.list_pending_posts(), [])

    async def test_single_photo_is_sent_with_caption(self) -> None:
        await self._subscribe(111)
        await self._insert_post(has_media=True)
        path = self._file("photo.jpg")
        await self.db.set_post_media(
            self.channel.id, 1, (MediaItem(kind="photo", path=path, size=5),)
        )
        bot = FakeBot()

        await self._delivery(bot).deliver_pending()

        self.assertEqual(len(bot.photos), 1)
        chat_id, caption = bot.photos[0]
        self.assertEqual(chat_id, 111)
        assert caption is not None
        self.assertIn("📢 <b>Тест</b> · @test", caption)
        self.assertEqual(bot.sent, [])
        # Временный файл удаляется после рассылки.
        self.assertFalse(Path(path).exists())
        self.assertEqual(await self.db.list_pending_posts(), [])

    async def test_album_is_sent_as_media_group(self) -> None:
        await self._subscribe(111)
        await self._insert_post(source_ids=(10, 11), has_media=True)
        first = self._file("one.jpg")
        second = self._file("two.jpg")
        await self.db.set_post_media(
            self.channel.id,
            1,
            (
                MediaItem(kind="photo", path=first, size=5),
                MediaItem(kind="video", path=second, size=5),
            ),
        )
        bot = FakeBot()

        await self._delivery(bot).deliver_pending()

        self.assertEqual(bot.albums, [(111, 2)])
        self.assertFalse(Path(first).exists())
        self.assertFalse(Path(second).exists())

    async def test_skipped_media_falls_back_to_text(self) -> None:
        await self._subscribe(111)
        await self._insert_post(text="", has_media=True)
        await self.db.set_post_media(
            self.channel.id,
            1,
            (MediaItem(kind="video", path=None, size=500, skipped=True),),
        )
        bot = FakeBot()

        await self._delivery(bot).deliver_pending()

        self.assertEqual(len(bot.sent), 1)
        self.assertIn("Медиа без текста", bot.sent[0][1])
        self.assertEqual(bot.photos, [])

    async def test_media_failure_falls_back_to_text(self) -> None:
        await self._subscribe(111)
        await self._insert_post(has_media=True)
        path = self._file("photo.jpg")
        await self.db.set_post_media(
            self.channel.id, 1, (MediaItem(kind="photo", path=path, size=5),)
        )
        bot = FakeBot(
            errors={
                111: [TelegramBadRequest(method=None, message="file is too big")]
            }
        )

        await self._delivery(bot).deliver_pending()

        # Первая попытка отправки медиа падает, затем уходит текст.
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("📢 <b>Тест</b> · @test", bot.sent[0][1])

    async def test_blocked_user_is_disabled(self) -> None:
        blocked = await self._subscribe(222)
        await self._subscribe(111)
        await self._insert_post()
        bot = FakeBot(
            errors={222: [TelegramForbiddenError(method=None, message="forbidden")]}
        )

        await self._delivery(bot).deliver_pending()

        self.assertEqual([chat_id for chat_id, _ in bot.sent], [111])
        user = await self.db.get_user(222)
        assert user is not None
        self.assertFalse(user.enabled)
        self.assertEqual(await self.db.count_user_channels(blocked.id), 1)

    async def test_failure_for_one_user_does_not_stop_others(self) -> None:
        await self._subscribe(222)
        await self._subscribe(111)
        await self._insert_post()
        bot = FakeBot(errors={222: [OSError("network is down")]})

        await self._delivery(bot).deliver_pending()

        self.assertEqual([chat_id for chat_id, _ in bot.sent], [111])
        self.assertEqual(await self.db.list_pending_posts(), [])

    async def test_post_without_subscribers_is_marked_delivered(self) -> None:
        user = await self._subscribe(111)
        await self.db.unsubscribe(user.id, self.channel.id)
        await self._insert_post()
        bot = FakeBot()

        self.assertEqual(await self._delivery(bot).deliver_pending(), 1)
        self.assertEqual(bot.sent, [])
        self.assertEqual(await self.db.list_pending_posts(), [])

    async def test_flood_wait_is_retried_once(self) -> None:
        await self._subscribe(111)
        await self._insert_post()
        bot = FakeBot(
            errors={
                111: [TelegramRetryAfter(method=None, message="flood", retry_after=0)]
            }
        )

        with patch("newsbot.delivery.asyncio.sleep", new=AsyncMock()):
            await self._delivery(bot).deliver_pending()

        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(bot.sent[0][0], 111)


if __name__ == "__main__":
    unittest.main()
