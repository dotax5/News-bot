"""Формирование и отправка дайджеста."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import LinkPreviewOptions

from .config import Settings
from .db import Database
from .models import User
from .rendering import build_digest_chunks, build_empty_digest_text
from .utils import iso_utc, utcnow

log = logging.getLogger(__name__)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


@dataclass(slots=True, frozen=True)
class DigestResult:
    sent: bool
    posts: int
    error: str | None = None


class DigestService:
    def __init__(self, bot: Bot, db: Database, settings: Settings) -> None:
        self._bot = bot
        self._db = db
        self._settings = settings
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock_for(self, user_id: int) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    async def send(self, user: User, *, manual: bool = False) -> DigestResult:
        """Собирает и отправляет дайджест. Безопасно вызывать из разных задач."""
        async with self._lock_for(user.id):
            now = utcnow()
            since_dt = (
                datetime.fromisoformat(user.last_digest_at)
                if user.last_digest_at
                else now - timedelta(days=1)
            )
            since = iso_utc(since_dt)
            until = iso_utc(now)

            total = await self._db.count_digest_posts(user.id, since, until)
            posts = await self._db.get_digest_posts(
                user.id, since, until, self._settings.digest_max_posts
            )
            local_date = now.astimezone(ZoneInfo(user.timezone)).date().isoformat()

            if not posts:
                sent = False
                if manual or self._settings.send_empty_digest:
                    try:
                        await self._bot.send_message(
                            user.tg_id,
                            build_empty_digest_text(user, now, since),
                            link_preview_options=NO_PREVIEW,
                        )
                        sent = True
                    except TelegramForbiddenError:
                        await self._db.set_user_enabled(user.id, False)
                        return DigestResult(
                            False, 0, "Вы заблокировали бота — рассылка выключена."
                        )
                    except TelegramAPIError as exc:
                        log.warning("Не удалось отправить пустой дайджест: %s", exc)
                        return DigestResult(False, 0, f"Ошибка Telegram: {exc}")
                await self._mark(user, now, local_date, manual)
                return DigestResult(sent=sent, posts=0)

            chunks = build_digest_chunks(user, posts, total, now, since)
            try:
                for index, chunk in enumerate(chunks):
                    await self._bot.send_message(
                        user.tg_id, chunk, link_preview_options=NO_PREVIEW
                    )
                    if index + 1 < len(chunks):
                        await asyncio.sleep(0.5)
            except TelegramForbiddenError:
                await self._db.set_user_enabled(user.id, False)
                log.info("Пользователь %s заблокировал бота", user.tg_id)
                return DigestResult(
                    False, 0, "Вы заблокировали бота — рассылка выключена."
                )
            except TelegramRetryAfter as exc:
                log.warning("Flood wait %s с при отправке дайджеста", exc.retry_after)
                return DigestResult(
                    False, 0, f"Telegram просит подождать {exc.retry_after} с."
                )
            except TelegramAPIError as exc:
                log.warning("Ошибка отправки дайджеста: %s", exc)
                return DigestResult(False, 0, f"Ошибка Telegram: {exc}")

            await self._mark(user, now, local_date, manual)
            log.info(
                "Дайджест отправлен пользователю %s: %s постов, %s частей",
                user.tg_id,
                len(posts),
                len(chunks),
            )
            return DigestResult(sent=True, posts=len(posts))

    async def _mark(
        self, user: User, now: datetime, local_date: str, manual: bool
    ) -> None:
        if manual:
            # Ручной дайджест не «съедает» ежедневную рассылку.
            await self._db.set_last_digest_at(user.id, iso_utc(now))
        else:
            await self._db.mark_digest_sent(user.id, iso_utc(now), local_date)
