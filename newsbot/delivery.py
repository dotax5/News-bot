"""Моментальная рассылка постов подписчикам каналов.

Медиа скачивается сборщиком заранее (см. newsbot/collector.py), поэтому здесь
мы только собираем сообщение из готовых файлов и отправляем его ботом.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import (
    FSInputFile,
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    LinkPreviewOptions,
)

from .config import Settings
from .db import Database
from .models import MediaItem, PendingPost, User
from .rendering import build_post_caption, build_post_message
from .utils import iso_utc, utcnow

log = logging.getLogger(__name__)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
SEND_DELAY = 0.05
MAX_RETRY_AFTER = 60
# Telegram Bot API: не больше 10 элементов в одном альбоме.
ALBUM_LIMIT = 10


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
            try:
                subscribers = await self._db.list_channel_subscribers(post.channel_id)
                if not subscribers:
                    await self._db.mark_post_notified(post.id, iso_utc(utcnow()))
                    continue

                try:
                    await self._send_post(subscribers, post)
                except Exception:
                    log.exception(
                        "Не удалось разослать пост %s/%s",
                        post.channel_username or post.channel_id,
                        post.message_id,
                    )
                await self._db.mark_post_notified(post.id, iso_utc(utcnow()))
                log.info(
                    "Пост %s/%s разослан %s подписчикам (%s)",
                    post.channel_username or post.channel_id,
                    post.message_id,
                    len(subscribers),
                    "медиа" if any(item.path for item in post.media) else "текст",
                )
            finally:
                # Файлы удаляем всегда, даже если отправка или запись в БД упали.
                self._cleanup_media(post)
        return len(posts)

    # ---------------------------------------------------------------- sending

    async def _send_post(self, subscribers: list[User], post: PendingPost) -> None:
        text = build_post_message(post, self._settings.display_timezone)
        media = [item for item in post.media if item.path and not item.skipped]
        caption = (
            build_post_caption(post, self._settings.display_timezone)
            if media
            else text
        )

        for index, user in enumerate(subscribers):
            # Сбой на одном подписчике не должен лишать поста остальных.
            try:
                if media:
                    await self._send_with_media(user, media, caption, text)
                    step = self._send_delay * max(1, len(media))
                else:
                    await self._send_text(user, text)
                    step = self._send_delay
            except Exception:
                log.exception(
                    "Не удалось отправить пост %s/%s пользователю %s",
                    post.channel_username or post.channel_id,
                    post.message_id,
                    user.tg_id,
                )
                step = self._send_delay
            if index + 1 < len(subscribers):
                await asyncio.sleep(step)

    async def _send_with_media(
        self,
        user: User,
        media: list[MediaItem],
        caption: str,
        fallback_text: str,
    ) -> None:
        """Отправляет медиа, а при ошибке — текстовую версию со ссылкой."""
        for attempt in range(2):
            try:
                await self._send_media(user.tg_id, media, caption)
                return
            except TelegramRetryAfter as exc:
                if attempt:
                    log.warning(
                        "Flood wait: медиа для %s так и не отправлено", user.tg_id
                    )
                    await self._send_text(user, fallback_text)
                    return
                wait = min(exc.retry_after + 1, MAX_RETRY_AFTER)
                log.warning("Flood wait %s с: повторяю медиа для %s", wait, user.tg_id)
                await asyncio.sleep(wait)
            except TelegramForbiddenError:
                await self._db.set_user_enabled(user.id, False)
                log.info(
                    "Пользователь %s заблокировал бота — уведомления выключены",
                    user.tg_id,
                )
                return
            except (TelegramBadRequest, TelegramAPIError, OSError) as exc:
                # Файл больше лимита Bot API, неподдерживаемый формат и т.п.
                log.warning(
                    "Не удалось отправить медиа пользователю %s: %s", user.tg_id, exc
                )
                await self._send_text(user, fallback_text)
                return

    async def _send_media(
        self, chat_id: int, media: list[MediaItem], caption: str | None
    ) -> None:
        if len(media) == 1:
            await self._send_single(chat_id, media[0], caption)
            return
        for offset in range(0, len(media), ALBUM_LIMIT):
            chunk = media[offset : offset + ALBUM_LIMIT]
            if len(chunk) == 1:
                await self._send_single(
                    chat_id, chunk[0], caption if offset == 0 else None
                )
                continue
            group = []
            for index, item in enumerate(chunk):
                item_caption = caption if offset == 0 and index == 0 else None
                group.append(self._input_media(item, item_caption))
            await self._bot.send_media_group(chat_id=chat_id, media=group)  # type: ignore[arg-type]

    async def _send_single(
        self, chat_id: int, item: MediaItem, caption: str | None
    ) -> None:
        common: dict[str, object] = {"chat_id": chat_id, "caption": caption}
        if caption:
            common["parse_mode"] = "HTML"
        file = FSInputFile(item.path)
        kind = item.kind
        if kind == "photo":
            await self._bot.send_photo(photo=file, **common)  # type: ignore[arg-type]
        elif kind == "video":
            await self._bot.send_video(
                video=file, supports_streaming=True, **common  # type: ignore[arg-type]
            )
        elif kind == "animation":
            await self._bot.send_animation(animation=file, **common)  # type: ignore[arg-type]
        elif kind == "audio":
            await self._bot.send_audio(audio=file, **common)  # type: ignore[arg-type]
        elif kind == "voice":
            await self._bot.send_voice(voice=file, **common)  # type: ignore[arg-type]
        else:
            await self._bot.send_document(document=file, **common)  # type: ignore[arg-type]

    def _input_media(self, item: MediaItem, caption: str | None):
        kwargs = {"media": FSInputFile(item.path), "caption": caption}
        if caption:
            kwargs["parse_mode"] = "HTML"
        if item.kind == "photo":
            return InputMediaPhoto(**kwargs)  # type: ignore[arg-type]
        if item.kind == "video":
            return InputMediaVideo(**kwargs)  # type: ignore[arg-type]
        if item.kind == "animation":
            return InputMediaAnimation(**kwargs)  # type: ignore[arg-type]
        if item.kind == "audio":
            return InputMediaAudio(**kwargs)  # type: ignore[arg-type]
        return InputMediaDocument(**kwargs)  # type: ignore[arg-type]

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

    # ---------------------------------------------------------------- cleanup

    @staticmethod
    def _cleanup_media(post: PendingPost) -> None:
        directories: set[Path] = set()
        for item in post.media:
            if not item.path:
                continue
            path = Path(item.path)
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("Не удалось удалить медиа %s: %s", item.path, exc)
            directories.add(path.parent)

        # Убираем опустевший каталог поста и, если он тоже пуст, каталог канала.
        for directory in directories:
            for candidate in (directory, directory.parent):
                try:
                    candidate.rmdir()
                except OSError:
                    break

    async def purge_orphaned_media(self) -> int:
        """Удаляет файлы из data/media, на которые не ссылаются ожидающие посты."""
        media_root = self._settings.data_dir / "media"
        if not media_root.is_dir():
            return 0

        referenced: set[Path] = set()
        for post in await self._db.list_pending_posts():
            for item in post.media:
                if not item.path:
                    continue
                path = Path(item.path)
                try:
                    referenced.add(path.resolve())
                except OSError:
                    referenced.add(path.absolute())

        deleted = 0
        removed_dirs = 0
        # Обходим снизу вверх: сначала файлы, затем опустевшие каталоги.
        entries = sorted(
            media_root.rglob("*"), key=lambda path: len(path.parts), reverse=True
        )
        for path in entries:
            try:
                if path.is_dir():
                    path.rmdir()
                    removed_dirs += 1
                    continue
                if not path.is_file() and not path.is_symlink():
                    continue
                try:
                    resolved = path.resolve()
                except OSError:
                    resolved = path.absolute()
                if resolved in referenced:
                    continue
                path.unlink(missing_ok=True)
                deleted += 1
            except FileNotFoundError:
                pass
            except OSError as exc:
                log.debug("Не удалось очистить %s: %s", path, exc)

        log.info(
            "Очистка data/media: удалено %s файлов и %s пустых каталогов",
            deleted,
            removed_dirs,
        )
        return deleted

