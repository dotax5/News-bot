"""Моментальная рассылка новых постов подписчикам каналов."""

from __future__ import annotations

import asyncio
import logging

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
from .rendering import build_post_message
from .utils import iso_utc, utcnow

log = logging.getLogger(__name__)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
SEND_DELAY = 0.05
MAX_RETRY_AFTER = 60


class PostDelivery:
    """Рассылает ещё не отправленные посты подписчикам их каналов."""

    def __init__(
        self,
        bot: Bot,
        db: Database,
        settings: Settings,
        *,
        send_delay: float = SEND_DELAY,
    ) -> None:
        self._bot = bot
        self._db = db
        self._settings = settings
        self._send_delay = send_delay
        self._lock = asyncio.Lock()

    async def deliver_pending(self) -> int:
        """Отправляет очередь неразосланных постов. Возвращает их число."""
        if self._lock.locked():
            return 0
        async with self._lock:
            return await self._deliver_pending()

    async def _deliver_pending(self) -> int:
        posts = await self._db.list_pending_posts()
        for post in posts:
            subscribers = await self._db.list_channel_subscribers(post.channel_id)
            if subscribers:
                text = build_post_message(post, self._settings.display_timezone)
                for index, user in enumerate(subscribers):
                    await self._send(user, text)
                    if index + 1 < len(subscribers):
                        await asyncio.sleep(self._send_delay)
                log.info(
                    "Пост %s/%s разослан %s подписчикам",
                    post.channel_username or post.channel_id,
                    post.message_id,
                    len(subscribers),
                )
            await self._db.mark_post_notified(post.id, iso_utc(utcnow()))
        return len(posts)

    async def _send(self, user: User, text: str) -> None:
        for attempt in range(2):
            try:
                await self._bot.send_message(
                    user.tg_id, text, link_preview_options=NO_PREVIEW
                )
                return
            except TelegramRetryAfter as exc:
                if attempt:
                    log.warning(
                        "Flood wait: пост для %s так и не отправлен", user.tg_id
                    )
                    return
                wait = min(exc.retry_after + 1, MAX_RETRY_AFTER)
                log.warning("Flood wait %s с: повторяю отправку для %s", wait, user.tg_id)
                await asyncio.sleep(wait)
            except TelegramForbiddenError:
                await self._db.set_user_enabled(user.id, False)
                log.info(
                    "Пользователь %s заблокировал бота — уведомления выключены",
                    user.tg_id,
                )
                return
            except TelegramAPIError as exc:
                log.warning(
                    "Не удалось отправить пост пользователю %s: %s", user.tg_id, exc
                )
                return
