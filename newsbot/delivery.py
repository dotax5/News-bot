"""Моментальная рассылка новых постов подписчикам каналов."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import LinkPreviewOptions

from .config import Settings
from .db import Database
from .mirror import MediaMirror, MirrorPending, MirrorResult, MirrorRetry
from .models import PendingPost, User
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
        mirror: MediaMirror | None = None,
    ) -> None:
        self._bot = bot
        self._db = db
        self._settings = settings
        self._send_delay = send_delay
        self._mirror = mirror
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
            if not subscribers:
                await self._db.mark_post_notified(post.id, iso_utc(utcnow()))
                continue

            try:
                target = await self._mirror.ensure(post) if self._mirror else None
            except MirrorPending as exc:
                # Пост уже переслан боту, но его id ещё не пришли: не теряем
                # пост и не задерживаем остальные.
                log.info("Жду копию от бота: %s", exc)
                continue
            except MirrorRetry as exc:
                # Не теряем пост: попробуем на следующем проходе.
                log.warning("Копия поста отложена: %s", exc)
                break

            if target is not None:
                used_copy = await self._send_copies(subscribers, post, target)
            else:
                used_copy = False
                await self._send_texts(subscribers, post)
            await self._db.mark_post_notified(post.id, iso_utc(utcnow()))
            log.info(
                "Пост %s/%s разослан %s подписчикам (%s)",
                post.channel_username or post.channel_id,
                post.message_id,
                len(subscribers),
                "копия" if used_copy else "текст",
            )
        return len(posts)

    async def _send_copies(
        self,
        subscribers: list[User],
        post: PendingPost,
        target: MirrorResult,
    ) -> bool:
        text = build_post_message(post, self._settings.display_timezone)
        copied = True
        for index, user in enumerate(subscribers):
            if copied:
                copied = await self._send_copy(user, target)
                if not copied:
                    # Копия не удалась из-за самого поста: дальше шлём текстом
                    # и больше не пробуем копировать этот пост.
                    await self._db.mark_post_mirror_failed(post.id)
            if not copied:
                await self._send_text(user, text)
            if index + 1 < len(subscribers):
                # Альбом — это несколько сообщений, поэтому пауза длиннее.
                delay = self._send_delay * max(1, len(target.message_ids))
                await asyncio.sleep(delay)
        return copied

    async def _send_texts(self, subscribers: list[User], post: PendingPost) -> None:
        text = build_post_message(post, self._settings.display_timezone)
        for index, user in enumerate(subscribers):
            await self._send_text(user, text)
            if index + 1 < len(subscribers):
                await asyncio.sleep(self._send_delay)

    async def _send_copy(self, user: User, target: MirrorResult) -> bool:
        """True — копия ушла (или пользователь заблокирован). False — не копируется."""
        for attempt in range(2):
            try:
                sent = await self._bot.copy_messages(
                    chat_id=user.tg_id,
                    from_chat_id=target.chat_id,
                    message_ids=list(target.message_ids),
                )
                if sent is not None and len(sent) < len(target.message_ids):
                    log.warning(
                        "Скопировано %s из %s сообщений поста для %s",
                        len(sent),
                        len(target.message_ids),
                        user.tg_id,
                    )
                return True
            except TelegramRetryAfter as exc:
                if attempt:
                    log.warning(
                        "Flood wait: копия поста для %s так и не отправлена",
                        user.tg_id,
                    )
                    return True
                wait = min(exc.retry_after + 1, MAX_RETRY_AFTER)
                log.warning("Flood wait %s с: повторяю копию для %s", wait, user.tg_id)
                await asyncio.sleep(wait)
            except TelegramForbiddenError:
                await self._db.set_user_enabled(user.id, False)
                log.info(
                    "Пользователь %s заблокировал бота — уведомления выключены",
                    user.tg_id,
                )
                return True
            except TelegramBadRequest as exc:
                log.warning(
                    "Пост не копируется для %s: %s", user.tg_id, exc
                )
                return False
            except TelegramAPIError as exc:
                log.warning(
                    "Не удалось отправить копию пользователю %s: %s", user.tg_id, exc
                )
                return True
        return True

    async def _send_text(self, user: User, text: str) -> None:
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
