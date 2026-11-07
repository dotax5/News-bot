from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from newsbot.db import Database
from newsbot.models import ChannelInfo
from newsbot.scheduler import DigestScheduler
from newsbot.utils import iso_utc, utcnow


class FakeDigest:
    def __init__(self) -> None:
        self.sent: list[int] = []

    async def send(self, user, *, manual: bool = False):
        self.sent.append(user.id)
        return SimpleNamespace(error=None)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.sqlite3")
        await self.db.connect()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def _make_user_with_channel(self, digest_time: str) -> int:
        now = utcnow() - timedelta(hours=1)
        user = await self.db.ensure_user(
            100, "tester", "Test", "Europe/Moscow", digest_time, now=now
        )
        # Сбрасываем отметку, чтобы проверялось расписание, а не факт регистрации.
        await self.db.set_user_digest_time(user.id, digest_time, None)
        channel, _ = await self.db.add_or_update_channel(
            ChannelInfo(key="@test", tg_id=-100123, username="test", title="Тест")
        )
        await self.db.subscribe(user.id, channel.id)
        return channel.id

    async def test_sends_when_due_and_checked(self) -> None:
        channel_id = await self._make_user_with_channel("00:00")
        await self.db.update_channel_progress(channel_id, 10, iso_utc(utcnow()))
        digest = FakeDigest()
        scheduler = DigestScheduler(
            self.db, digest, SimpleNamespace(), check_interval=1
        )
        await scheduler.tick()
        self.assertEqual(len(digest.sent), 1)

    async def test_skips_unchecked_channel(self) -> None:
        await self._make_user_with_channel("00:00")
        digest = FakeDigest()
        scheduler = DigestScheduler(
            self.db, digest, SimpleNamespace(), check_interval=1
        )
        await scheduler.tick()
        self.assertEqual(digest.sent, [])

    async def test_skips_paused_user(self) -> None:
        channel_id = await self._make_user_with_channel("00:00")
        await self.db.update_channel_progress(channel_id, 10, iso_utc(utcnow()))
        user = await self.db.get_user(100)
        assert user is not None
        await self.db.set_user_enabled(user.id, False)
        digest = FakeDigest()
        scheduler = DigestScheduler(
            self.db, digest, SimpleNamespace(), check_interval=1
        )
        await scheduler.tick()
        self.assertEqual(digest.sent, [])


if __name__ == "__main__":
    unittest.main()
