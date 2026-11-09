from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from newsbot.db import Database
from newsbot.delivery import PostDelivery
from newsbot.models import ChannelInfo
from newsbot.utils import iso_utc


class FakeBot:
    def __init__(self, errors: dict[int, list[Exception]] | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self._errors = {key: list(value) for key, value in (errors or {}).items()}

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        errors = self._errors.get(chat_id)
        if errors:
            raise errors.pop(0)
        self.sent.append((chat_id, text))


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

    async def _insert_post(self, message_id: int = 1, text: str = "Новая новость") -> None:
        await self.db.insert_posts(
            self.channel.id,
            [
                (
                    message_id,
                    text,
                    iso_utc(self.now - timedelta(minutes=5)),
                    f"https://t.me/test/{message_id}",
                    False,
                )
            ],
            notify=False,
        )

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
        self.assertIn("Тест", bot.sent[0][1])
        self.assertIn("Новая новость", bot.sent[0][1])
        self.assertIn("https://t.me/test/1", bot.sent[0][1])
        self.assertEqual(await self.db.list_pending_posts(), [])

    async def test_paused_user_is_skipped_without_backlog(self) -> None:
        await self._subscribe(111)
        paused = await self._subscribe(222, enabled=False)
        await self._insert_post()
        bot = FakeBot()

        await self._delivery(bot).deliver_pending()

        self.assertEqual([chat_id for chat_id, _ in bot.sent], [111])
        self.assertEqual(await self.db.list_pending_posts(), [])

        # После /resume старый пост уже не приходит, только следующий новый.
        await self.db.set_user_enabled(paused.id, True)
        await self._insert_post(message_id=2, text="Свежая новость")
        await self._delivery(bot).deliver_pending()
        self.assertEqual([chat_id for chat_id, _ in bot.sent], [111, 111, 222])
        self.assertIn("Свежая новость", bot.sent[-1][1])
        self.assertNotIn("Новая новость", bot.sent[-1][1])

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
