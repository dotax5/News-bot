"""Сбор новостей из каналов через Telethon (userbot-аккаунт)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    FloodWaitError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.sessions import StringSession
from telethon.tl.types import Channel as TgChannel
from telethon.tl.types import Chat as TgChat
from telethon.tl.types import User as TgUser
from telethon.utils import get_peer_id

from .config import Settings
from .db import Database
from .models import Channel, ChannelInfo
from .utils import (
    ChannelRef,
    iso_utc,
    parse_channel_ref,
    telegram_post_url,
    utcnow,
)

log = logging.getLogger(__name__)


class CollectorError(RuntimeError):
    """Понятная пользователю ошибка сбора."""


class NewsCollector:
    def __init__(self, settings: Settings, db: Database) -> None:
        self._settings = settings
        self._db = db
        self._client: TelegramClient | None = None
        self._entity_cache: dict[int, tuple[Any, str | None, int]] = {}
        self._dialogs_refreshed = False
        self.me: str | None = None

    @property
    def client(self) -> TelegramClient:
        if self._client is None:
            raise CollectorError("Сборщик не запущен.")
        return self._client

    async def start(self) -> None:
        if not self._settings.collector_enabled:
            raise CollectorError("Не заданы TELEGRAM_API_ID и TELEGRAM_API_HASH.")

        if self._settings.session_string:
            try:
                session: str | StringSession = StringSession(self._settings.session_string)
            except ValueError as exc:
                raise CollectorError(
                    "TELEGRAM_SESSION повреждён. Получите новую строку через `python login.py`."
                ) from exc
        else:
            self._settings.session_path.parent.mkdir(parents=True, exist_ok=True)
            session = str(self._settings.session_path)

        try:
            self._client = TelegramClient(
                session,
                self._settings.api_id,
                self._settings.api_hash,
                device_model="News Digest Bot",
                system_version="1.0",
                app_version="1.0",
                connection_retries=5,
                retry_delay=3,
            )
            await self._client.connect()
        except Exception as exc:  # сеть/сессия — показываем понятный текст
            if self._client is not None:
                await self._client.disconnect()
                self._client = None
            raise CollectorError(
                f"Не удалось подключиться к Telegram: {type(exc).__name__}: {exc}"
            ) from exc

        if not await self._client.is_user_authorized():
            await self._client.disconnect()
            self._client = None
            raise CollectorError(
                "Telegram-аккаунт для сбора новостей не авторизован. "
                "Запустите `python login.py`, отсканируйте/введите код и вставьте "
                "полученную строку в TELEGRAM_SESSION (.env)."
            )

        me = await self._client.get_me()
        self.me = f"@{me.username}" if me and me.username else str(me.id if me else "?")
        log.info("Сборщик авторизован как %s", self.me)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    async def resolve_channel(self, raw: str) -> ChannelInfo:
        """Проверяет канал через Telegram и возвращает нормализованные данные."""
        try:
            ref = parse_channel_ref(raw)
        except ValueError as exc:
            raise CollectorError(str(exc)) from exc
        entity = await self._resolve_entity(ref)
        if isinstance(entity, TgUser):
            raise CollectorError("Это личный аккаунт, а не канал или чат.")
        if not isinstance(entity, (TgChannel, TgChat)):
            raise CollectorError("Не удалось определить канал или чат.")

        username = getattr(entity, "username", None) or None
        peer_id = get_peer_id(entity)
        title = getattr(entity, "title", None) or username or str(peer_id)
        key = f"@{username.lower()}" if username else f"id:{peer_id}"
        return ChannelInfo(key=key, tg_id=peer_id, username=username, title=title)

    async def _resolve_entity(self, ref: ChannelRef) -> Any:
        try:
            if ref.username:
                return await self.client.get_entity(ref.username)
            assert ref.peer_id is not None
            return await self.client.get_entity(ref.peer_id)
        except FloodWaitError:
            raise
        except Exception as first_error:
            if not self._dialogs_refreshed:
                await self._refresh_dialogs()
                try:
                    if ref.username:
                        return await self.client.get_entity(ref.username)
                    assert ref.peer_id is not None
                    return await self.client.get_entity(ref.peer_id)
                except Exception as second_error:
                    raise self._friendly_error(ref, second_error) from second_error
            raise self._friendly_error(ref, first_error) from first_error

    async def _refresh_dialogs(self) -> None:
        self._dialogs_refreshed = True
        try:
            await self.client.get_dialogs(limit=2000)
        except Exception as exc:  # noqa: BLE001 - кэш диалогов не критичен
            log.warning("Не удалось обновить список диалогов: %s", exc)

    def _friendly_error(self, ref: ChannelRef, error: Exception) -> CollectorError:
        if isinstance(error, UsernameNotOccupiedError):
            return CollectorError(f"Канал @{ref.username} не существует.")
        if isinstance(error, UsernameInvalidError):
            return CollectorError("Некорректный @username канала.")
        if isinstance(error, ChannelPrivateError):
            return CollectorError(
                "Аккаунт-сборщик не состоит в этом канале. "
                "Вступите в канал под аккаунтом, чья сессия указана в TELEGRAM_SESSION."
            )
        return CollectorError(
            f"Не удалось открыть канал {ref.username or ref.peer_id}: "
            f"{type(error).__name__}: {error}"
        )

    async def _entity_for_channel(self, channel: Channel) -> tuple[Any, str | None, int]:
        cached = self._entity_cache.get(channel.id)
        if cached is not None:
            return cached

        ref = ChannelRef(username=channel.username, peer_id=channel.tg_id)
        if not ref.username and ref.peer_id is None:
            try:
                ref = parse_channel_ref(channel.key)
            except ValueError as exc:
                raise CollectorError(f"Некорректная запись канала: {channel.key}") from exc

        entity = await self._resolve_entity(ref)
        if isinstance(entity, TgUser):
            raise CollectorError("Источник оказался личным аккаунтом, а не каналом.")
        username = getattr(entity, "username", None) or None
        peer_id = get_peer_id(entity)
        title = getattr(entity, "title", None) or username or str(peer_id)
        await self._db.update_channel_meta(channel.id, peer_id, username, title)
        info = (entity, username, peer_id)
        self._entity_cache[channel.id] = info
        return info

    async def collect_once(self) -> int:
        """Один проход по всем каналам. Возвращает число новых постов."""
        channels = await self._db.channels_to_collect()
        total_new = 0
        for channel in channels:
            try:
                total_new += await self._collect_channel(channel)
            except FloodWaitError as exc:
                wait_for = min(int(getattr(exc, "seconds", 60)), 300)
                log.warning("Telegram просит подождать %s с (flood wait)", wait_for)
                await self._db.set_channel_error(
                    channel.id, f"Лимит Telegram: повтор через {wait_for} с"
                )
                await asyncio.sleep(wait_for)
            except CollectorError as exc:
                log.warning("Канал %s: %s", channel.key, exc)
                await self._db.set_channel_error(channel.id, str(exc))
            except Exception as exc:
                log.exception("Ошибка сбора из канала %s", channel.key)
                await self._db.set_channel_error(
                    channel.id, f"{type(exc).__name__}: {exc}"
                )
            await asyncio.sleep(0.7)
        return total_new

    async def _collect_channel(self, channel: Channel) -> int:
        entity, username, peer_id = await self._entity_for_channel(channel)
        messages = await self._fetch_messages(entity, channel)
        posts: list[tuple[int, str, str, str | None, bool]] = []
        max_id = channel.last_message_id
        seen_groups: set[int] = set()

        for message in messages:
            message_id = getattr(message, "id", None)
            if message_id is None:
                continue
            max_id = max(max_id, message_id)
            if getattr(message, "action", None) is not None:
                continue

            grouped_id = getattr(message, "grouped_id", None)
            if grouped_id is not None:
                if grouped_id in seen_groups:
                    continue
                seen_groups.add(grouped_id)

            text = (getattr(message, "message", None) or "").strip()
            has_media = getattr(message, "media", None) is not None
            if not text and not has_media:
                continue

            posted_at = getattr(message, "date", None) or utcnow()
            posts.append(
                (
                    message_id,
                    text[:4000],
                    iso_utc(posted_at),
                    telegram_post_url(username, peer_id, message_id),
                    has_media,
                )
            )

        inserted = await self._db.insert_posts(channel.id, posts)
        await self._db.update_channel_progress(
            channel.id, max_id, iso_utc(utcnow()), None
        )
        if inserted:
            log.info("Канал %s: сохранено %s новых постов", channel.key, inserted)
        return inserted

    async def _fetch_messages(self, entity: Any, channel: Channel) -> list[Any]:
        if channel.last_message_id <= 0:
            messages = await self.client.get_messages(
                entity, limit=self._settings.initial_fetch_limit
            )
            return list(reversed(list(messages or [])))

        return [
            message
            async for message in self.client.iter_messages(
                entity,
                min_id=channel.last_message_id,
                limit=100,
                reverse=True,
            )
        ]

    async def run(self, stop_event: asyncio.Event) -> None:
        log.info(
            "Цикл сбора запущен: интервал %s с", self._settings.collect_interval
        )
        while not stop_event.is_set():
            started = utcnow()
            try:
                await self.collect_once()
            except Exception:
                log.exception("Не удалось выполнить проход сбора")

            elapsed = (utcnow() - started).total_seconds()
            delay = max(30.0, self._settings.collect_interval - elapsed)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except TimeoutError:
                continue
