"""Копирование постов канала в чат бота для точной пересылки подписчикам.

Bot API умеет копировать сообщения только из чатов, которые видит бот.
Каналы читает userbot, поэтому пост сначала целиком пересылается в личный
чат с ботом. В личных чатах id сообщений у каждого аккаунта свои, поэтому
бот запоминает свои id из входящих пересылок, а доставка ждёт их появления.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import User as TgUser

from .db import Database
from .models import PendingPost, PostMirrorState
from .utils import iso_utc, utcnow

log = logging.getLogger(__name__)

MAX_FLOOD_WAIT = 300
# Сколько ждём id от бота в рамках одной доставки и сколько всего.
MIRROR_WAIT_SECONDS = 3.0
MIRROR_TIMEOUT_SECONDS = 900.0
MIRROR_POLL_SECONDS = 0.2


class MirrorError(RuntimeError):
    """Точная копия недоступна (например, бот не найден)."""


class MirrorRetry(RuntimeError):
    """Временная ошибка: пост остаётся в очереди до следующего прохода."""


class MirrorPending(RuntimeError):
    """Пост переслан боту, но его id ещё не пришли в апдейтах."""


@dataclass(slots=True, frozen=True)
class MirrorResult:
    """Где лежит копия поста: чат бота и id сообщений в нём."""

    chat_id: int
    message_ids: tuple[int, ...]


class MediaMirror:
    def __init__(
        self,
        db: Database,
        client: TelegramClient,
        bot_username: str,
        *,
        enabled: bool = True,
        wait_timeout: float = MIRROR_WAIT_SECONDS,
        poll_interval: float = MIRROR_POLL_SECONDS,
    ) -> None:
        self._db = db
        self._client = client
        self._bot_username = bot_username
        self._enabled = enabled
        self._wait_timeout = wait_timeout
        self._poll_interval = poll_interval
        self._bot_entity: Any | None = None
        self._chat_id: int | None = None

    async def start(self) -> None:
        if not self._enabled:
            return
        try:
            entity = await self._client.get_entity(self._bot_username)
        except Exception as exc:
            raise MirrorError(
                f"Не удалось найти бота @{self._bot_username} из аккаунта-сборщика: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(entity, TgUser) or not entity.bot:
            raise MirrorError(f"@{self._bot_username} не похож на бота.")
        me = await self._client.get_me()
        if me is None:
            raise MirrorError("Не удалось определить аккаунт-сборщик.")
        self._bot_entity = entity
        # Бот копирует из личного чата, где chat_id — это id аккаунта-сборщика.
        self._chat_id = me.id
        log.info(
            "Точная копия постов: храню копии в чате с @%s как %s",
            self._bot_username,
            me.id,
        )

    async def ensure(self, post: PendingPost) -> MirrorResult | None:
        """Возвращает копию поста в чате бота или None, если копия невозможна."""
        if not self._enabled or self._bot_entity is None or self._chat_id is None:
            return None
        state = await self._db.get_post_mirror(post.id)
        if state is None:
            return None
        source_ids = tuple(sorted(post.source_message_ids or (post.message_id,)))

        if state.media_state == "failed":
            return None

        if state.media_state == "ok":
            result = self._complete(state, len(source_ids))
            if result is not None:
                return result
            # Старые id не подходят (например, сменился аккаунт) — заново.
            await self._db.reset_post_mirror(post.id)
            state = await self._db.get_post_mirror(post.id)
            if state is None:
                return None

        if state.media_state == "pending":
            result = self._complete(state, len(source_ids))
            if result is not None:
                await self._promote(post.id, result)
                return result
            await self._db.reset_post_mirror(post.id)
            if not await self._forward(post, source_ids):
                return None
            await self._db.mark_post_mirror_requested(post.id, iso_utc(utcnow()))
            # Ждём апдейт бота только сразу после пересылки: если он опоздал,
            # пост останется в очереди и уйдёт на следующем проходе.
            result = await self._wait_for_mapping(post.id, len(source_ids))
            if result is not None:
                return result
            state = await self._db.get_post_mirror(post.id)
            if state is not None and self._expired(state.mirror_requested_at):
                await self._db.mark_post_mirror_failed(post.id)
                return None
            raise MirrorPending(
                f"пост {post.channel_username or post.channel_id}/{post.message_id}"
            )

        result = self._complete(state, len(source_ids))
        if result is not None:
            await self._promote(post.id, result)
            return result
        if self._expired(state.mirror_requested_at):
            log.warning(
                "Бот не прислал id копии поста %s/%s, отправлю текстом",
                post.channel_username or post.channel_id,
                post.message_id,
            )
            await self._db.mark_post_mirror_failed(post.id)
            return None
        raise MirrorPending(
            f"пост {post.channel_username or post.channel_id}/{post.message_id}"
        )

    async def _wait_for_mapping(
        self, post_id: int, expected: int
    ) -> MirrorResult | None:
        deadline = time.monotonic() + self._wait_timeout
        while True:
            state = await self._db.get_post_mirror(post_id)
            if state is not None:
                result = self._complete(state, expected)
                if result is not None:
                    await self._promote(post_id, result)
                    return result
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(self._poll_interval)

    def _complete(
        self, state: PostMirrorState, expected: int
    ) -> MirrorResult | None:
        if self._chat_id is None or state.mirror_chat_id != self._chat_id:
            return None
        ids = tuple(sorted(state.mirror_message_ids))
        if len(ids) < expected:
            return None
        # Если пост пересылали повторно, берём последнюю пересылку.
        return MirrorResult(self._chat_id, ids[-expected:])

    async def _promote(self, post_id: int, result: MirrorResult) -> None:
        await self._db.set_post_mirror(post_id, result.chat_id, result.message_ids)

    def _expired(self, requested_at: str | None) -> bool:
        if not requested_at:
            return True
        try:
            requested = datetime.fromisoformat(requested_at)
        except ValueError:
            return True
        return (utcnow() - requested).total_seconds() > MIRROR_TIMEOUT_SECONDS

    async def _forward(
        self, post: PendingPost, source_ids: tuple[int, ...]
    ) -> bool:
        peers = [
            peer
            for peer in (post.channel_username, post.channel_tg_id)
            if peer is not None
        ]
        if not peers:
            await self._db.mark_post_mirror_failed(post.id)
            return False

        last_error: Exception | None = None
        for peer in peers:
            try:
                entity = await self._client.get_entity(peer)
            except (ConnectionError, TimeoutError) as exc:
                raise MirrorRetry(f"Сеть недоступна: {exc}") from exc
            except Exception as exc:  # noqa: BLE001 - пробуем следующий способ
                last_error = exc
                continue

            for attempt in range(2):
                try:
                    await self._client.forward_messages(
                        self._bot_entity, list(source_ids), from_peer=entity
                    )
                except FloodWaitError as exc:
                    if attempt:
                        raise MirrorRetry(
                            f"Telegram просит подождать {exc.seconds} с"
                        ) from exc
                    wait = min(int(getattr(exc, "seconds", 60) or 0), MAX_FLOOD_WAIT)
                    log.warning("Flood wait %s с: отложил копию поста", wait)
                    await asyncio.sleep(wait)
                    continue
                except (ConnectionError, TimeoutError) as exc:
                    # Сеть/таймаут — не повод навсегда лишать пост копии.
                    raise MirrorRetry(f"Сеть недоступна: {exc}") from exc
                except Exception as exc:  # noqa: BLE001 - копия невозможна
                    last_error = exc
                    log.warning(
                        "Не удалось переслать пост %s/%s в чат бота: %s: %s",
                        post.channel_username or post.channel_id,
                        post.message_id,
                        type(exc).__name__,
                        exc,
                    )
                    break
                return True

        log.warning(
            "Пост %s/%s нельзя скопировать, отправлю текстом: %s",
            post.channel_username or post.channel_id,
            post.message_id,
            last_error,
        )
        await self._db.mark_post_mirror_failed(post.id)
        return False
