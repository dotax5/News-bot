from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from newsbot.models import DigestPost, User
from newsbot.rendering import ENTITY_LIMIT, _entity_count, build_digest_chunks
from newsbot.utils import utf16_len


def make_user() -> User:
    return User(
        id=1,
        tg_id=1,
        username="tester",
        first_name="Test",
        timezone="Europe/Moscow",
        digest_time="09:00",
        enabled=True,
        last_digest_at="2026-11-06T06:00:00+00:00",
        last_digest_date="2026-11-06",
    )


def make_post(index: int, channel_id: int = 1, text: str | None = None) -> DigestPost:
    posted_at = datetime(2026, 11, 7, 5, 0, tzinfo=UTC) + timedelta(
        minutes=index
    )
    return DigestPost(
        id=index,
        channel_id=channel_id,
        message_id=index,
        text=text if text is not None else f"Новость номер {index} " + "текст " * 30,
        posted_at=posted_at.isoformat(timespec="seconds"),
        url=f"https://t.me/test/{index}",
        has_media=False,
        channel_title="Тестовый канал",
        channel_username="test",
        channel_peer_id=-1001234567890,
    )


class RenderingTests(unittest.TestCase):
    def test_empty(self) -> None:
        now = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
        self.assertEqual(
            build_digest_chunks(make_user(), [], 0, now, "2026-11-06T06:00:00+00:00"), []
        )

    def test_chunks_respect_limit(self) -> None:
        now = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
        posts = [make_post(i, channel_id=i % 3) for i in range(40)]
        chunks = build_digest_chunks(
            make_user(), posts, len(posts), now, "2026-11-06T06:00:00+00:00"
        )
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(utf16_len(chunk), 3800)
            self.assertLessEqual(_entity_count(chunk), ENTITY_LIMIT)
        joined = "\n".join(chunks)
        self.assertIn("Дайджест новостей", joined)
        self.assertIn("Тестовый канал", joined)
        self.assertIn("Новость номер 39", joined)

    def test_html_escaped(self) -> None:
        now = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
        post = make_post(0, text="<script>alert(1)</script> & <b>жирный</b>")
        chunks = build_digest_chunks(
            make_user(), [post], 1, now, "2026-11-06T06:00:00+00:00"
        )
        self.assertNotIn("<script>", "\n".join(chunks))
        self.assertIn("&lt;script&gt;", "\n".join(chunks))

    def test_truncation_note(self) -> None:
        now = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
        chunks = build_digest_chunks(
            make_user(), [make_post(0)], 100, now, "2026-11-06T06:00:00+00:00"
        )
        self.assertIn("Показаны последние 1 из 100", "\n".join(chunks))


if __name__ == "__main__":
    unittest.main()
