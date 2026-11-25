"""Сбор новостей из каналов через Telethon (userbot-аккаунт)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telethon import TelegramClient
from telethon.errors import (
    AuthKeyUnregisteredError,
    ChannelPrivateError,
    FloodWaitError,
    UnauthorizedError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.sessions import StringSession
from telethon.tl.types import Channel as TgChannel
from telethon.tl.types import Chat as TgChat
from telethon.tl.types import (
    DocumentAttributeAnimated,
    DocumentAttributeAudio,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    MessageMediaDocument,
    MessageMediaPhoto,
    MessageMediaWebPage,
)
from telethon.tl.types import User as TgUser
from telethon.utils import get_peer_id

from .config import Settings
from .db import Database
from .delivery import PostDelivery
from .models import Channel, ChannelInfo, MediaItem, NewPost
from .utils import (
    ChannelRef,
    iso_utc,
    parse_channel_ref,
    telegram_post_url,
    utcnow,
)

log = logging.getLogger(__name__)

# Жёсткий лимит скачивания одного медиафайла — 200 МБ.
MAX_MEDIA_BYTES = 200 * 1024 * 1024
# Сколько ждать загрузки одного файла, прежде чем считать её проваленной.
DOWNLOAD_TIMEOUT = 600
# Паузы перед повторными попытками после потери связи (секунды).
# Пропуск прохода дешевле, чем минуты ожидания внутри одного прохода.
RECONNECT_BACKOFF: tuple[float, ...] = (2.0, 5.0)
# Предел ожидания установки соединения и ответа на запрос истории.
CONNECT_TIMEOUT = 30.0
FETCH_TIMEOUT = 120.0
# Проблемы транспорта: связь с Telegram пропала, нужен реконнект.
# OSError сюда не входит: ошибки диска/БД реконнектом не лечатся.
TRANSPORT_ERRORS = (ConnectionError, ConnectionResetError, TimeoutError)
# Отозванная сессия: ретраи бессмысленны, нужен `python login.py`.
AUTH_ERRORS = (UnauthorizedError, AuthKeyUnregisteredError)


def _media_kind(message: Any) -> str | None:
    """Тип файлового медиа сообщения или None (превью ссылки, опрос и т.п.)."""
    media = getattr(message, "media", None)
    if media is None or isinstance(media, MessageMediaWebPage):
        return None
    if isinstance(media, MessageMediaPhoto):
        return "photo"
    if not isinstance(media, MessageMediaDocument):
        return None
    document = getattr(media, "document", None)
    if document is None:
        return None
    attributes = list(getattr(document, "attributes", None) or ())
    if any(isinstance(attr, DocumentAttributeSticker) for attr in attributes):
        return None
    if any(isinstance(attr, DocumentAttributeAnimated) for attr in attributes):
        return "animation"
    if any(isinstance(attr, DocumentAttributeVideo) for attr in attributes):
        return "video"
    for attr in attributes:
        if isinstance(attr, DocumentAttributeAudio):
            return "voice" if getattr(attr, "voice", False) else "audio"
    return "document"


def _media_size(message: Any) -> int:
    """Размер файла в байтах (0, если Telegram не сообщил)."""
    media = getattr(message, "media", None)
    document = getattr(media, "document", None) if media is not None else None
    size = getattr(document, "size", None) if document is not None else None
    return int(size) if size else 0


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            return f"{value:.0f} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} Б"


class CollectorError(RuntimeError):
    """Понятная пользователю ошибка сбора."""


class NewsCollector:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        delivery: PostDelivery | None = None,
    ) -> None:
        self._settings = settings
        self._db = db
        self._delivery = delivery
        self._client: TelegramClient | None = None
        self._entity_cache: dict[int, tuple[Any, str | None, int]] = {}
        self._dialogs_refreshed = False
        self._fatal_error = False
        self.me: str | None = None
        self.me_id: int | None = None

    def set_delivery(self, delivery: PostDelivery) -> None:
        """Привязывает доставку после её создания (нужен уже подключённый клиент)."""
        self._delivery = delivery

    @property
    def client(self) -> TelegramClient:
        if self._client is None:
            raise CollectorError("Сборщик не запущен.")
        return self._client

    async def _ensure_connected(self) -> None:
        """Проверяет связь с Telegram и при необходимости переподключается.

        Бросает ConnectionError, если клиент так и не поднялся: для вызывающего
        кода это временная ошибка транспорта, а не проблема конкретного канала.
        """
        if self._client is None:
            raise CollectorError("Сборщик не запущен.")
        if self._client.is_connected():
            return

        log.warning("Соединение с Telegram потеряно, переподключаюсь")
        try:
            await asyncio.wait_for(
                self._client.connect(), timeout=CONNECT_TIMEOUT
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ConnectionError(
                "Не удалось переподключиться к Telegram: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        if not self._client.is_connected():
            raise ConnectionError("Нет соединения с Telegram.")

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
        self.me_id = me.id if me else None
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
        except TRANSPORT_ERRORS:
            # Потерю связи обрабатывает collect_once: это не ошибка канала.
            raise
        except Exception as first_error:
            if not self._dialogs_refreshed:
                await self._refresh_dialogs()
                try:
                    if ref.username:
                        return await self.client.get_entity(ref.username)
                    assert ref.peer_id is not None
                    return await self.client.get_entity(ref.peer_id)
                except TRANSPORT_ERRORS:
                    raise
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
        self._fatal_error = False
        for channel in channels:
            abort_pass = False
            attempts = 0
            while True:
                try:
                    await self._ensure_connected()
                    total_new += await self._collect_channel(channel)
                    break
                except AUTH_ERRORS as exc:
                    self._fatal_error = True
                    log.error(
                        "Сессия Telegram отозвана (%s). TELEGRAM_SESSION больше "
                        "не действителен: выполните `python login.py`, обновите "
                        "строку сессии и перезапустите бота. Сбор новостей "
                        "остановлен, чтобы не нагружать Telegram.",
                        type(exc).__name__,
                    )
                    break
                except TRANSPORT_ERRORS as exc:
                    if attempts >= len(RECONNECT_BACKOFF):
                        log.warning(
                            "Соединение с Telegram не восстанавливается (%s): "
                            "проход сбора прерван, повторю по расписанию",
                            type(exc).__name__,
                        )
                        abort_pass = True
                        break
                    delay = RECONNECT_BACKOFF[attempts]
                    attempts += 1
                    log.warning(
                        "Соединение с Telegram потеряно (%s), повторяю канал "
                        "%s через %s с",
                        type(exc).__name__,
                        channel.key,
                        delay,
                    )
                    await asyncio.sleep(delay)
                except FloodWaitError as exc:
                    wait_for = min(int(getattr(exc, "seconds", 60)), 300)
                    log.warning(
                        "Telegram просит подождать %s с (flood wait)", wait_for
                    )
                    await self._db.set_channel_error(
                        channel.id, f"Лимит Telegram: повтор через {wait_for} с"
                    )
                    await asyncio.sleep(wait_for)
                    break
                except CollectorError as exc:
                    log.warning("Канал %s: %s", channel.key, exc)
                    await self._db.set_channel_error(channel.id, str(exc))
                    break
                except Exception as exc:
                    log.exception("Ошибка сбора из канала %s", channel.key)
                    await self._db.set_channel_error(
                        channel.id, f"{type(exc).__name__}: {exc}"
                    )
                    break
            if abort_pass or self._fatal_error:
                break
            # Рассылаем сразу после каждого канала: подписчики не ждут
            # окончания прохода по всем источникам.
            await self._deliver()
            await asyncio.sleep(0.7)
        await self._deliver()
        return total_new

    async def _deliver(self) -> None:
        if self._delivery is None:
            return
        try:
            await self._delivery.deliver_pending()
        except Exception:
            log.exception("Не удалось разослать новые посты")

    async def _collect_channel(self, channel: Channel) -> int:
        await self._ensure_connected()
        entity, username, peer_id = await self._entity_for_channel(channel)
        messages = await self._fetch_messages(entity, channel)
        is_first_pass = channel.last_message_id <= 0
        posts = self._build_posts(messages, username, peer_id)
        max_id = channel.last_message_id
        for message in messages:
            message_id = getattr(message, "id", None)
            if message_id is not None:
                max_id = max(max_id, message_id)

        inserted = await self._db.insert_posts(
            channel.id, posts, notify=is_first_pass
        )
        deliver: list[NewPost] = []
        if is_first_pass and inserted:
            # Первый проход — это ещё не новости: рассылаем только последний
            # пост канала, остальные сохраняем молча как историю.
            newest_id = max(
                (ids for post in posts for ids in post.source_message_ids),
                default=0,
            )
            newest = next(
                (post for post in posts if newest_id in post.source_message_ids),
                None,
            )
            if newest is not None:
                await self._db.mark_post_pending(channel.id, newest.message_id)
                deliver = [newest]
        elif inserted:
            deliver = list(posts)

        await self._download_media_for_posts(deliver, messages, channel)

        await self._db.update_channel_progress(
            channel.id, max_id, iso_utc(utcnow()), None
        )
        if inserted:
            log.info("Канал %s: сохранено %s новых постов", channel.key, inserted)
        return inserted

    async def _download_media_for_posts(
        self,
        posts: list[NewPost],
        messages: list[Any],
        channel: Channel,
    ) -> None:
        """Скачивает медиа для постов, которые вот-вот уйдут подписчикам.

        Скачиваем заранее и целиком, чтобы рассылка не зависела от скорости
        Telegram и могла повторяться после перезапуска. Посты без медиа и
        историю первого прохода не трогаем.
        """
        media_posts = [post for post in posts if post.has_media]
        if not media_posts:
            return
        by_id = {
            message.id: message
            for message in messages
            if getattr(message, "id", None) is not None
        }
        for post in media_posts:
            items = await self._download_post_media(post, by_id, channel)
            if not items:
                continue
            try:
                await self._db.set_post_media(channel.id, post.message_id, items)
            except Exception:
                log.exception(
                    "Не удалось сохранить медиа поста %s/%s",
                    channel.key,
                    post.message_id,
                )

    async def _download_post_media(
        self,
        post: NewPost,
        by_id: dict[int, Any],
        channel: Channel,
    ) -> list[MediaItem]:
        """Скачивает все файлы поста (включая все элементы альбома)."""
        items: list[MediaItem] = []
        target_dir = (
            self._settings.data_dir / "media" / str(channel.id) / str(post.message_id)
        )
        for message_id in post.source_message_ids:
            message = by_id.get(message_id)
            if message is None:
                continue
            kind = _media_kind(message)
            if kind is None:
                continue
            size = _media_size(message)
            if size > MAX_MEDIA_BYTES:
                log.warning(
                    "Медиа %s/%s (%s) больше %s МБ — отправлю текст со ссылкой",
                    channel.key,
                    message_id,
                    _human_size(size),
                    MAX_MEDIA_BYTES // (1024 * 1024),
                )
                items.append(MediaItem(kind=kind, size=size, skipped=True))
                continue
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                path = await asyncio.wait_for(
                    self.client.download_media(message, file=str(target_dir)),
                    timeout=DOWNLOAD_TIMEOUT,
                )
            except Exception as exc:  # noqa: BLE001 - один файл не должен ломать пост
                log.warning(
                    "Не удалось скачать медиа %s/%s: %s: %s",
                    channel.key,
                    message_id,
                    type(exc).__name__,
                    exc,
                )
                items.append(MediaItem(kind=kind, size=size, skipped=True))
                continue
            if not path:
                items.append(MediaItem(kind=kind, size=size, skipped=True))
                continue
            items.append(MediaItem(kind=kind, path=str(path), size=size))
        return items

    def _build_posts(
        self,
        messages: list[Any],
        username: str | None,
        peer_id: int,
    ) -> list[NewPost]:
        """Склеивает сообщения в посты: альбом (grouped_id) — один пост.

        Сообщения приходят по возрастанию id, поэтому порядок альбомов
        сохраняется, а представителем поста служит его первое сообщение.
        """
        buckets: list[list[Any]] = []
        group_index: dict[int, int] = {}
        for message in messages:
            if getattr(message, "id", None) is None:
                continue
            if getattr(message, "action", None) is not None:
                continue
            grouped_id = getattr(message, "grouped_id", None)
            if grouped_id is None:
                buckets.append([message])
                continue
            index = group_index.get(grouped_id)
            if index is None:
                group_index[grouped_id] = len(buckets)
                buckets.append([message])
            else:
                buckets[index].append(message)

        posts: list[NewPost] = []
        for bucket in buckets:
            ids = tuple(sorted(message.id for message in bucket))
            text = next(
                (
                    (getattr(message, "message", None) or "").strip()
                    for message in bucket
                    if (getattr(message, "message", None) or "").strip()
                ),
                "",
            )
            has_media = any(_media_kind(message) is not None for message in bucket)
            if not text and not has_media:
                continue
            representative = ids[0]
            posted_at = getattr(bucket[0], "date", None) or utcnow()
            posts.append(
                NewPost(
                    message_id=representative,
                    source_message_ids=ids,
                    text=text[:4000],
                    posted_at=iso_utc(posted_at),
                    url=telegram_post_url(username, peer_id, representative),
                    has_media=has_media,
                )
            )
        return posts

    async def _fetch_messages(self, entity: Any, channel: Channel) -> list[Any]:
        if channel.last_message_id <= 0:
            messages = await asyncio.wait_for(
                self.client.get_messages(
                    entity, limit=self._settings.initial_fetch_limit
                ),
                timeout=FETCH_TIMEOUT,
            )
            return list(reversed(list(messages or [])))

        async def collect() -> list[Any]:
            return [
                message
                async for message in self.client.iter_messages(
                    entity,
                    min_id=channel.last_message_id,
                    limit=100,
                    reverse=True,
                )
            ]

        return await asyncio.wait_for(collect(), timeout=FETCH_TIMEOUT)

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

            if self._fatal_error:
                log.error(
                    "Цикл сбора остановлен: сессия Telegram отозвана. "
                    "Запустите `python login.py` и перезапустите бота."
                )
                stop_event.set()
                return

            elapsed = (utcnow() - started).total_seconds()
            delay = max(30.0, self._settings.collect_interval - elapsed)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except TimeoutError:
                continue
