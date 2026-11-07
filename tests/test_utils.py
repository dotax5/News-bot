from __future__ import annotations

import unittest
from datetime import UTC, datetime

from newsbot.utils import (
    ChannelRefError,
    digest_is_due,
    next_digest_at,
    parse_channel_ref,
    parse_digest_time,
    telegram_post_url,
    utf16_len,
)


class ChannelRefTests(unittest.TestCase):
    def test_username_variants(self) -> None:
        for raw in (
            "@rian_ru",
            "rian_ru",
            "https://t.me/rian_ru",
            "http://t.me/rian_ru",
            "t.me/rian_ru",
            "https://t.me/rian_ru/12345",
            "https://t.me/s/rian_ru",
            "tg://resolve?domain=rian_ru",
        ):
            with self.subTest(raw=raw):
                ref = parse_channel_ref(raw)
                self.assertEqual(ref.username, "rian_ru")
                self.assertEqual(ref.key, "@rian_ru")

    def test_numeric_ids(self) -> None:
        self.assertEqual(parse_channel_ref("-1001234567890").peer_id, -1001234567890)
        self.assertEqual(parse_channel_ref("1234567890").peer_id, -1001234567890)

    def test_private_link(self) -> None:
        ref = parse_channel_ref("https://t.me/c/1234567890/42")
        self.assertEqual(ref.peer_id, -1001234567890)

    def test_invite_rejected(self) -> None:
        with self.assertRaises(ChannelRefError):
            parse_channel_ref("https://t.me/+AbCdEfGhIjK")

    def test_garbage_rejected(self) -> None:
        with self.assertRaises(ChannelRefError):
            parse_channel_ref("https://example.com/channel")

    def test_post_url(self) -> None:
        self.assertEqual(
            telegram_post_url("rian_ru", None, 10), "https://t.me/rian_ru/10"
        )
        self.assertEqual(
            telegram_post_url(None, -1001234567890, 10),
            "https://t.me/c/1234567890/10",
        )
        self.assertIsNone(telegram_post_url(None, None, 10))


class TimeTests(unittest.TestCase):
    def test_parse_time(self) -> None:
        self.assertEqual(parse_digest_time("9:05"), (9, 5))
        self.assertEqual(parse_digest_time("23:59"), (23, 59))
        for bad in ("24:00", "9", "09:60", "abc", "09:5"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_digest_time(bad)

    def test_due(self) -> None:
        now = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)  # 09:00 MSK
        self.assertTrue(digest_is_due(now, "Europe/Moscow", "09:00", None))
        self.assertFalse(
            digest_is_due(now, "Europe/Moscow", "09:00", "2026-11-07")
        )
        self.assertFalse(digest_is_due(now, "Europe/Moscow", "10:00", None))

    def test_next_digest(self) -> None:
        now = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
        tomorrow = next_digest_at(now, "Europe/Moscow", "09:00", "2026-11-07")
        self.assertEqual(tomorrow.date().isoformat(), "2026-11-08")
        due_now = next_digest_at(now, "Europe/Moscow", "08:00", None)
        self.assertEqual(due_now, now.astimezone(due_now.tzinfo))

    def test_utf16_len(self) -> None:
        self.assertEqual(utf16_len("abc"), 3)
        self.assertEqual(utf16_len("😴"), 2)


if __name__ == "__main__":
    unittest.main()
