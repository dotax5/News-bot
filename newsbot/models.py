"""Модели данных, общие для БД, сборщика и рендера сообщений."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class User:
    id: int
    tg_id: int
    username: str | None
    first_name: str | None
    enabled: bool


@dataclass(slots=True, frozen=True)
class Channel:
    id: int
    key: str
    tg_id: int | None
    username: str | None
    title: str | None
    source: str
    last_message_id: int
    last_checked_at: str | None
    last_error: str | None
    created_at: str


@dataclass(slots=True, frozen=True)
class ChannelInfo:
    """Результат разрешения ссылки на канал через Telethon."""

    key: str
    tg_id: int
    username: str | None
    title: str


@dataclass(slots=True, frozen=True)
class UserChannel:
    channel: Channel
    last_post_at: str | None


@dataclass(slots=True, frozen=True)
class PendingPost:
    """Пост, который ещё не разослан подписчикам канала."""

    id: int
    channel_id: int
    message_id: int
    text: str
    posted_at: str
    url: str | None
    has_media: bool
    channel_title: str | None
    channel_username: str | None
