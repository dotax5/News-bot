"""Сборка HTML-сообщений для Telegram (без зависимостей от Telegram)."""

from __future__ import annotations

import re

from .models import PendingPost
from .utils import channel_display, format_dt_local, h, utf16_len

CHUNK_LIMIT = 3800
ENTITY_LIMIT = 70
POST_TEXT_LIMIT = 3500
# Запас под заголовок, время и ссылку в лимите сообщения Telegram (4096).
POST_BODY_LIMIT = 3700
# Лимит подписи к медиа в Telegram Bot API — 1024 символа UTF-16.
CAPTION_LIMIT = 1000
POST_TIME_FORMAT = "%d.%m.%Y %H:%M"

_SPACES_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _trim(raw: str, limit: int) -> str:
    text = _BLANK_LINES_RE.sub("\n\n", _SPACES_RE.sub(" ", raw.strip()))
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" \n.,;:—–-")
    return f"{cut}…"


def _escaped_text(raw: str, limit: int = POST_TEXT_LIMIT) -> str:
    """Текст поста без лишних пробелов, экранированный под HTML и влезающий в лимит."""
    size = limit
    while True:
        escaped = h(_trim(raw, size))
        if utf16_len(escaped) <= limit or size <= 200:
            return escaped
        size = size * 3 // 4


def _header(post: PendingPost, tz_name: str) -> str:
    """Шапка поста: всегда название канала-источника и время публикации."""
    title = channel_display(post.channel_title, post.channel_username)
    handle = f" · @{h(post.channel_username)}" if post.channel_username else ""
    when = format_dt_local(post.posted_at, tz_name, POST_TIME_FORMAT)
    return f"📢 <b>{h(title)}</b>{handle}\n🕓 <i>{when}</i>"


def _link(post: PendingPost) -> str:
    if not post.url:
        return ""
    return f'<a href="{h(post.url)}">Открыть пост ↗</a>'


def _body(post: PendingPost, limit: int) -> str:
    if post.text.strip():
        return _escaped_text(post.text, max(limit, 200))
    if post.has_media:
        return "📎 Медиа без текста"
    return "🔗 Пост без текста"


def build_post_message(post: PendingPost, tz_name: str) -> str:
    """Полное сообщение о новом посте: канал, время, текст и ссылка на оригинал."""
    header = _header(post, tz_name)
    link = _link(post)
    fixed = utf16_len(header) + 2 + (2 + utf16_len(link) if link else 0)
    body = _body(post, POST_BODY_LIMIT - fixed)
    lines = [header]
    if body:
        lines += ["", body]
    if link:
        lines += ["", link]
    return "\n".join(lines)


def build_post_caption(post: PendingPost, tz_name: str) -> str:
    """Подпись к медиа: тот же пост, но укладывается в лимит 1024 символа."""
    header = _header(post, tz_name)
    link = _link(post)
    fixed = utf16_len(header) + 2 + (2 + utf16_len(link) if link else 0)
    # Само медиа уже отправляется, поэтому заглушку «Медиа без текста» не пишем.
    body = _escaped_text(post.text, max(CAPTION_LIMIT - fixed, 200)) if post.text.strip() else ""
    lines = [header]
    if body:
        lines += ["", body]
    if link and utf16_len("\n".join(lines)) + 2 + utf16_len(link) <= CAPTION_LIMIT:
        lines += ["", link]
    elif link:
        # Ссылка не влезла — оставляем хотя бы её без текста-хвоста.
        lines = [header, "", link]
    return "\n".join(lines)
