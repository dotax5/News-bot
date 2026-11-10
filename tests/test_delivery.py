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
from telethon.tl.types import User as TgUser

from newsbot.db import Database
from newsbot.delivery import PostDelivery
from newsbot.mirror import (
    MediaMirror,
    MirrorPending,
    MirrorResult,
    MirrorRetry,
)
from newsbot.models import ChannelInfo, NewPost
from newsbot.utils import iso_utc


class FakeBot:
    def __init__(
        self,
        errors: dict[int, list[Exception]] | None = None,
        copy_errors: dict[int, list[Exception]] | None = None,
    ) -> None:
        self.sent: list[tuple[int, str]] = []
        self.copied: list[tuple[int, int, tuple[int, ...]]] = []
        self._errors = {key: list(value) for key, value in (errors or {}).items()}
        self._copy_errors = {
            key: list(value) for key, value in (copy_errors or {}).items()
        }

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        errors = self._errors.get(chat_id)
        if errors:
            raise errors.pop(0)
        self.sent.append((chat_id, text))

    async def copy_messages(
        self, chat_id: int, from_chat_id: int, message_ids: list[int], **kwargs
    ) -> None:
        errors = self._copy_errors.get(chat_id)
        if errors:
            raise errors.pop(0)
        self.copied.append((chat_id, from_chat_id, tuple(message_ids)))


class FakeMirror:
    def __init__(
        self, result: MirrorResult | None = None, error: Exception | None = None
    ) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    async def ensure(self, post):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


class FakeTelethon:
    """Userbot: пересылает пост боту, бот сохраняет свои id через БД."""

    def __init__(
        self,
        db: Database,
        userbot_id: int = 9001,
        bot_id: int = 999,
        channel_tg_id: int = -100123,
    ) -> None:
        self.db = db
        self.channel_tg_id = channel_tg_id
        self._me = TgUser(id=userbot_id, first_name="Collector", username="collector")
        self._bot = TgUser(id=bot_id, bot=True, first_name="Bot", username="testbot")
        self.forward_calls: list[list[int]] = []

    async def get_me(self):
        return self._me

    async def get_entity(self, ref):
        return self._bot

    async def forward_messages(self, entity, ids, from_peer=None):
        self.forward_calls.append(list(ids))
        bot_ids = []
        for index, source_id in enumerate(ids):
            bot_id = 700 + index
            await self.db.attach_mirror_message(
                self.channel_tg_id, source_id, self._me.id, bot_id
            )
            bot_ids.append(bot_id)
        return [SimpleNamespace(id=value) for value in bot_ids]


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
                    has_media=False,
                )
            ],
            notify=False,
        )

    def _delivery(self, bot: FakeBot, mirror: FakeMirror | None = None) -> PostDelivery:
        return PostDelivery(
            bot,  # type: ignore[arg-type]
            self.db,
            SimpleNamespace(display_timezone="Europe/Moscow"),  # type: ignore[arg-type]
            send_delay=0,
            mirror=mirror,  # type: ignore[arg-type]
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

    async def test_exact_copy_is_sent_to_every_subscriber(self) -> None:
        await self._subscribe(111)
        await self._subscribe(222)
        await self._insert_post(source_ids=(10, 11, 12))
        bot = FakeBot()
        mirror = FakeMirror(MirrorResult(chat_id=999, message_ids=(500, 501, 502)))

        await self._delivery(bot, mirror).deliver_pending()

        self.assertEqual(
            bot.copied,
            [
                (111, 999, (500, 501, 502)),
                (222, 999, (500, 501, 502)),
            ],
        )
        self.assertEqual(bot.sent, [])
        self.assertEqual(await self.db.list_pending_posts(), [])

    async def test_mirror_and_copy_use_userbot_chat(self) -> None:
        await self._subscribe(111)
        await self._insert_post(source_ids=(10, 11))
        bot = FakeBot()
        client = FakeTelethon(self.db, userbot_id=9001)
        mirror = MediaMirror(
            self.db,
            client,  # type: ignore[arg-type]
            "testbot",
            wait_timeout=0.05,
            poll_interval=0.001,
        )
        await mirror.start()

        await self._delivery(bot, mirror).deliver_pending()

        self.assertEqual(client.forward_calls, [[10, 11]])
        # from_chat_id — это id аккаунта-сборщика, а не бота.
        self.assertEqual(bot.copied, [(111, 9001, (700, 701))])
        self.assertEqual(await self.db.list_pending_posts(), [])

    async def test_mirror_pending_keeps_post_and_continues(self) -> None:
        await self._subscribe(111)
        await self._insert_post()
        await self._insert_post(message_id=2, text="Второй пост", source_ids=(2,))
        bot = FakeBot()
        mirror = FakeMirror(error=MirrorPending("жду id"))

        await self._delivery(bot, mirror).deliver_pending()

        self.assertEqual(bot.sent, [])
        self.assertEqual(bot.copied, [])
        pending = await self.db.list_pending_posts()
        self.assertEqual(len(pending), 2)

    async def test_copy_failure_falls_back_to_text(self) -> None:
        await self._subscribe(111)
        await self._subscribe(222)
        await self._insert_post()
        bot = FakeBot(
            copy_errors={
                111: [TelegramBadRequest(method=None, message="message can't be copied")]
            }
        )
        mirror = FakeMirror(MirrorResult(chat_id=999, message_ids=(500,)))

        await self._delivery(bot, mirror).deliver_pending()

        self.assertEqual([chat_id for chat_id, _ in bot.sent], [111, 222])
        self.assertEqual(bot.copied, [])
        self.assertEqual(await self.db.list_pending_posts(), [])

    async def test_mirror_retry_keeps_post_pending(self) -> None:
        await self._subscribe(111)
        await self._insert_post()
        bot = FakeBot()
        mirror = FakeMirror(error=MirrorRetry("flood wait"))

        await self._delivery(bot, mirror).deliver_pending()

        self.assertEqual(bot.sent, [])
        self.assertEqual(bot.copied, [])
        self.assertEqual(len(await self.db.list_pending_posts()), 1)

    async def test_mirror_unavailable_sends_text(self) -> None:
        await self._subscribe(111)
        await self._insert_post()
        bot = FakeBot()
        mirror = FakeMirror(result=None)

        await self._delivery(bot, mirror).deliver_pending()

        self.assertEqual([chat_id for chat_id, _ in bot.sent], [111])
        self.assertEqual(bot.copied, [])

    async def test_mirror_is_not_used_when_there_are_no_subscribers(self) -> None:
        await self._insert_post()
        bot = FakeBot()
        mirror = FakeMirror(MirrorResult(chat_id=999, message_ids=(500,)))

        await self._delivery(bot, mirror).deliver_pending()

        self.assertEqual(mirror.calls, 0)
        self.assertEqual(bot.copied, [])
        self.assertEqual(await self.db.list_pending_posts(), [])

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
