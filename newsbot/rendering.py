"""Сборка HTML-сообщений дайджеста (без зависимостей от Telegram)."""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from zoneinfo import ZoneInfo

from .models import DigestPost, User
from .utils import channel_display, h, shorten, utf16_len

CHUNK_LIMIT = 3800
ENTITY_LIMIT = 70
POST_TEXT_LIMIT = 550
CONTINUATION_HEADER = "📰 <b>Дайджест (продолжение)</b>"


def _entity_count(line: str) -> int:
    """Приблизительно считает entities Telegram (bold/italic/link)."""
    return line.count("<b>") + line.count("<i>") + line.count("<a ")


def _format_post_time(posted_at: str, tz_name: str, digest_date: str) -> str:
    local = datetime.fromisoformat(posted_at).astimezone(ZoneInfo(tz_name))
    if local.date().isoformat() == digest_date:
        return local.strftime("%H:%M")
    return local.strftime("%d.%m %H:%M")


def _post_line(post: DigestPost, user: User, digest_date: str) -> str:
    parts = [f"• <b>{_format_post_time(post.posted_at, user.timezone, digest_date)}</b>"]
    text = shorten(post.text, POST_TEXT_LIMIT)
    if text:
        parts.append(f"— {h(text)}")
    elif post.has_media:
        parts.append("— 📎 медиа без текста")
    else:
        parts.append("— без текста")
    if post.url:
        parts.append(f'<a href="{h(post.url)}">↗</a>')
    return " ".join(parts)


def build_digest_chunks(
    user: User,
    posts: list[DigestPost],
    total: int,
    now: datetime,
    since: str,
) -> list[str]:
    if not posts:
        return []

    tz = ZoneInfo(user.timezone)
    local_now = now.astimezone(tz)
    digest_date = local_now.date().isoformat()

    groups: OrderedDict[int, list[DigestPost]] = OrderedDict()
    for post in sorted(posts, key=lambda item: item.posted_at, reverse=True):
        groups.setdefault(post.channel_id, []).append(post)

    header = [
        "📰 <b>Дайджест новостей</b>",
        (
            f"<i>{local_now.strftime('%d.%m.%Y, %H:%M')} · "
            f"{len(posts)} публ. · {len(groups)} кан.</i>"
        ),
    ]
    since_local = datetime.fromisoformat(since).astimezone(tz)
    header.append(f"<i>Период: с {since_local.strftime('%d.%m %H:%M')}</i>")
    if total > len(posts):
        header.append(f"<i>Показаны последние {len(posts)} из {total} публикаций.</i>")

    content: list[str] = []
    for channel_posts in groups.values():
        first = channel_posts[0]
        title = channel_display(first.channel_title, first.channel_username)
        suffix = f" · @{h(first.channel_username)}" if first.channel_username else ""
        content.append("")
        content.append(f"<b>{h(title)}</b>{suffix}")
        content.extend(_post_line(post, user, digest_date) for post in channel_posts)

    chunks: list[str] = []
    current = list(header)
    current_len = utf16_len("\n".join(current))
    current_entities = sum(_entity_count(line) for line in current)

    for line in content:
        line_len = utf16_len(line) + 1
        line_entities = _entity_count(line)
        too_long = current_len + line_len > CHUNK_LIMIT
        too_many_entities = current_entities + line_entities > ENTITY_LIMIT
        if (too_long or too_many_entities) and len(current) > len(header):
            chunks.append("\n".join(current))
            current = [CONTINUATION_HEADER]
            current_len = utf16_len(CONTINUATION_HEADER)
            current_entities = _entity_count(CONTINUATION_HEADER)
        current.append(line)
        current_len += line_len
        current_entities += line_entities

    if len(current) > 1:
        chunks.append("\n".join(current))
    return chunks


def build_empty_digest_text(user: User, now: datetime, since: str | None) -> str:
    local_now = now.astimezone(ZoneInfo(user.timezone))
    lines = [
        "😴 <b>Новых публикаций нет</b>",
        f"<i>{local_now.strftime('%d.%m.%Y, %H:%M')}</i>",
    ]
    if since:
        since_local = datetime.fromisoformat(since).astimezone(ZoneInfo(user.timezone))
        lines.append(f"<i>Период: с {since_local.strftime('%d.%m %H:%M')}</i>")
    lines.append("")
    lines.append(
        "Проверьте каналы командой /channels или добавьте новые через /addchannel."
    )
    return "\n".join(lines)


def build_no_channels_text() -> str:
    return (
        "📭 <b>Список каналов пуст</b>\n\n"
        "Добавьте источник: <code>/addchannel @username</code>\n"
        "Например: <code>/addchannel @telegram</code>"
    )

