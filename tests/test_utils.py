from __future__ import annotations

import unittest

from newsbot.utils import (
    ChannelRefError,
    format_dt_local,
    h,
    join_int_ids,
    parse_channel_ref,
    parse_int_ids,
    shorten,
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


class FormatTests(unittest.TestCase):
    def test_shorten(self) -> None:
        self.assertEqual(shorten(" много   пробелов ", 50), "много пробелов")

        cut = shorten("слово " * 10, 12)
        self.assertTrue(cut.endswith("…"))
        self.assertLessEqual(len(cut), 13)

    def test_escape(self) -> None:
        self.assertEqual(h('<b>&"'), "&lt;b&gt;&amp;&quot;")
        self.assertEqual(h(None), "")

    def test_format_dt_local(self) -> None:
        self.assertEqual(
            format_dt_local("2026-11-07T06:00:00+00:00", "Europe/Moscow", "%d.%m %H:%M"),
            "07.11 09:00",
        )

    def test_utf16_len(self) -> None:
        self.assertEqual(utf16_len("abc"), 3)
        self.assertEqual(utf16_len("😴"), 2)

    def test_int_id_csv(self) -> None:
        self.assertEqual(join_int_ids([500, 501]), "500,501")
        self.assertEqual(parse_int_ids("500, 501"), (500, 501))
        self.assertEqual(parse_int_ids("500,500,x,,501"), (500, 501))
        self.assertEqual(parse_int_ids(""), ())
        self.assertEqual(parse_int_ids(None), ())


if __name__ == "__main__":
    unittest.main()
