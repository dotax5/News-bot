"""Модели данных, общие для БД, сборщика и рендера дайджеста."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class User:
    id: int
    tg_id: int
    username: str | None
    first_name: str | None
    timezone: str
    digest_time: str
    enabled: bool
    last_digest_at: str | None
    last_digest_date: str | None


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
class DigestPost:
    id: int
    channel_id: int
    message_id: int
    text: str
    posted_at: str
    url: str | None
    has_media: bool
    channel_title: str | None
    channel_username: str | None
    channel_peer_id: int | None
