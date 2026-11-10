from __future__ import annotations

import unittest
from typing import Any

from newsbot.models import PendingPost
from newsbot.rendering import build_post_message
from newsbot.utils import utf16_len


def make_post(**overrides: Any) -> PendingPost:
    values: dict[str, Any] = {
        "id": 1,
        "channel_id": 1,
        "message_id": 1,
        "text": "Текст новости",
        "posted_at": "2026-11-07T06:00:00+00:00",
        "url": "https://t.me/test/1",
        "has_media": False,
        "channel_title": "Тестовый канал",
        "channel_username": "test",
        "channel_tg_id": None,
        "source_message_ids": (1,),
        "mirror_chat_id": None,
        "mirror_message_ids": (),
        "mirror_requested_at": None,
        "media_state": "pending",
    }
    values.update(overrides)
    return PendingPost(**values)


class PostMessageTests(unittest.TestCase):
    def test_message_structure(self) -> None:
        text = build_post_message(make_post(), "Europe/Moscow")
        self.assertIn("📢 <b>Тестовый канал</b> · @test", text)
        self.assertIn("07.11.2026 09:00", text)
        self.assertIn("Текст новости", text)
        self.assertIn('<a href="https://t.me/test/1">Открыть пост ↗</a>', text)

    def test_html_escaped(self) -> None:
        post = make_post(
            text="<script>alert(1)</script> & <b>жирный</b>", channel_title="<i>"
        )
        text = build_post_message(post, "UTC")
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("&amp;", text)
        self.assertIn("&lt;i&gt;", text)

    def test_media_notes(self) -> None:
        with_text = build_post_message(make_post(has_media=True), "UTC")
        self.assertIn("📎 К посту приложено медиа", with_text)

        media_only = build_post_message(make_post(text="", has_media=True), "UTC")
        self.assertIn("📎 Медиа без текста", media_only)

        text_only = build_post_message(make_post(text="", has_media=False), "UTC")
        self.assertIn("🔗 Пост без текста", text_only)

    def test_paragraphs_preserved(self) -> None:
        post = make_post(text="Первый   абзац.\n\n\n\nВторой абзац.")
        text = build_post_message(post, "UTC")
        self.assertIn("Первый абзац.\n\nВторой абзац.", text)

    def test_long_text_fits_telegram_limit(self) -> None:
        text = build_post_message(make_post(text="слово " * 2000), "UTC")
        self.assertLessEqual(utf16_len(text), 4096)
        self.assertIn("…", text)

    def test_fallbacks_without_url_and_handle(self) -> None:
        post = make_post(url=None, channel_title=None, channel_username=None)
        text = build_post_message(post, "UTC")
        self.assertIn("📢 <b>канал</b>", text)
        self.assertNotIn("Открыть пост", text)


if __name__ == "__main__":
    unittest.main()
