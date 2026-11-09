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
POST_TIME_FORMAT = "%d.%m.%Y %H:%M"

_SPACES_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _trim(raw: str, limit: int) -> str:
    text = _BLANK_LINES_RE.sub("\n\n", _SPACES_RE.sub(" ", raw.strip()))
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" \n.,;:—–-")
    return f"{cut}…"


def _escaped_text(raw: str) -> str:
    """Текст поста без лишних пробелов, экранированный под HTML и влезающий в лимит."""
    limit = POST_TEXT_LIMIT
    while True:
        escaped = h(_trim(raw, limit))
        if utf16_len(escaped) <= POST_BODY_LIMIT or limit <= 200:
            return escaped
        limit = limit * 3 // 4


def build_post_message(post: PendingPost, tz_name: str) -> str:
    """Сообщение о новом посте: канал, время, текст и ссылка на оригинал."""
    title = channel_display(post.channel_title, post.channel_username)
    handle = f" · @{h(post.channel_username)}" if post.channel_username else ""
    lines = [
        f"📢 <b>{h(title)}</b>{handle}",
        f"🕓 <i>{format_dt_local(post.posted_at, tz_name, POST_TIME_FORMAT)}</i>",
        "",
    ]

    body = _escaped_text(post.text)
    if body:
        lines.append(body)
    elif post.has_media:
        lines.append("📎 Медиа без текста")
    else:
        lines.append("🔗 Пост без текста")

    if post.has_media and body:
        lines.append("")
        lines.append("<i>📎 К посту приложено медиа</i>")
    if post.url:
        lines.append("")
        lines.append(f'<a href="{h(post.url)}">Открыть пост ↗</a>')
    return "\n".join(lines)
