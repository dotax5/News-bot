"""Вспомогательные функции: ссылки, время, форматирование."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,31}$")
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class ChannelRefError(ValueError):
    """Ссылка/идентификатор канала не распознан."""


@dataclass(slots=True, frozen=True)
class ChannelRef:
    username: str | None
    peer_id: int | None

    @property
    def key(self) -> str:
        if self.username:
            return f"@{self.username.lower()}"
        assert self.peer_id is not None
        return f"id:{self.peer_id}"


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def parse_channel_ref(raw: str) -> ChannelRef:
    """Понимает @username, username, t.me/username, t.me/c/123/45 и -100... id."""
    value = raw.strip()
    if not value:
        raise ChannelRefError("Пустая ссылка на канал.")

    lowered = value.lower()
    if "t.me/+" in lowered or "joinchat" in lowered or lowered.startswith("tg://join"):
        raise ChannelRefError(
            "Приватные инвайт-ссылки не поддерживаются: добавьте канал по @username "
            "или по числовому ID, и аккаунт-сборщик должен уже состоять в нём."
        )

    # tg://resolve?domain=username
    if lowered.startswith("tg://resolve"):
        match = re.search(r"[?&]domain=([A-Za-z0-9_]+)", value)
        if not match:
            raise ChannelRefError("Не удалось разобрать tg:// ссылку.")
        value = match.group(1)

    # https://t.me/s/username/123, t.me/username, telegram.me/username
    for prefix in ("https://", "http://"):
        if value.lower().startswith(prefix):
            value = value[len(prefix) :]
            break
    value = value.split("?", 1)[0].split("#", 1)[0].strip("/")
    for domain in ("www.t.me/", "t.me/", "www.telegram.me/", "telegram.me/"):
        if value.lower().startswith(domain):
            value = value[len(domain) :]
            break

    parts = [part for part in value.split("/") if part]
    if not parts:
        raise ChannelRefError("Пустая ссылка на канал.")

    # t.me/c/<internal_id>/<message_id> — приватный канал/супергруппа.
    if parts[0].lower() == "c" and len(parts) >= 2 and parts[1].isdigit():
        return ChannelRef(username=None, peer_id=int(f"-100{parts[1]}"))

    if parts[0].lower() == "s" and len(parts) >= 2:
        parts = parts[1:]

    first = parts[0]
    first = first.removeprefix("@")

    if re.fullmatch(r"-?\d+", first):
        peer_id = int(first)
        if peer_id > 0:
            # Пользователь мог вставить «голый» id канала без -100.
            peer_id = int(f"-100{peer_id}")
        return ChannelRef(username=None, peer_id=peer_id)

    if not USERNAME_RE.fullmatch(first):
        raise ChannelRefError(
            f"Не понял ссылку {raw!r}. Примеры: @rian_ru, https://t.me/rian_ru, -1001234567890."
        )
    return ChannelRef(username=first, peer_id=None)


def telegram_post_url(username: str | None, peer_id: int | None, message_id: int) -> str | None:
    if username:
        return f"https://t.me/{username}/{message_id}"
    if peer_id is None:
        return None
    raw = str(peer_id)
    if raw.startswith("-100"):
        return f"https://t.me/c/{raw[4:]}/{message_id}"
    if raw.startswith("-"):
        return f"https://t.me/c/{raw[1:]}/{message_id}"
    return f"https://t.me/c/{raw}/{message_id}"


def parse_digest_time(raw: str) -> tuple[int, int]:
    match = TIME_RE.fullmatch(raw.strip())
    if not match:
        raise ValueError("Время должно быть в формате HH:MM, например 09:30.")
    return int(match.group(1)), int(match.group(2))


def normalize_digest_time(raw: str) -> str:
    hours, minutes = parse_digest_time(raw)
    return f"{hours:02d}:{minutes:02d}"


def validate_timezone(raw: str) -> str:
    name = raw.strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"Неизвестная таймзона {name!r}. Примеры: Europe/Moscow, Europe/Kyiv, Asia/Almaty, UTC."
        ) from exc
    return name


def format_dt_local(value: datetime | str, tz_name: str, pattern: str = "%d.%m %H:%M") -> str:
    if isinstance(value, str):
        value = parse_iso(value)
    return value.astimezone(ZoneInfo(tz_name)).strftime(pattern)


def local_today(now: datetime, tz_name: str) -> date:
    return now.astimezone(ZoneInfo(tz_name)).date()


def initial_digest_date(now: datetime, tz_name: str, digest_time: str) -> str | None:
    """Дата, на которую рассылка уже «закрыта» при регистрации.

    Если время сегодня ещё не наступило — None, чтобы первый дайджест пришёл сегодня.
    """
    local = now.astimezone(ZoneInfo(tz_name))
    hours, minutes = parse_digest_time(digest_time)
    passed = (local.hour, local.minute) >= (hours, minutes)
    return local.date().isoformat() if passed else None


def schedule_date_after_change(now: datetime, tz_name: str, digest_time: str) -> str | None:
    """После смены времени/таймзоны: если время ещё впереди — даём шанс отправить сегодня."""
    return initial_digest_date(now, tz_name, digest_time)


def digest_is_due(
    now: datetime, tz_name: str, digest_time: str, last_digest_date: str | None
) -> bool:
    local = now.astimezone(ZoneInfo(tz_name))
    hours, minutes = parse_digest_time(digest_time)
    if (local.hour, local.minute) < (hours, minutes):
        return False
    return last_digest_date != local.date().isoformat()


def next_digest_at(
    now: datetime, tz_name: str, digest_time: str, last_digest_date: str | None
) -> datetime:
    tz = ZoneInfo(tz_name)
    local = now.astimezone(tz)
    hours, minutes = parse_digest_time(digest_time)
    candidate = local.replace(hour=hours, minute=minutes, second=0, microsecond=0)
    if last_digest_date == local.date().isoformat():
        candidate += timedelta(days=1)
    elif candidate <= local:
        return local
    return candidate


def describe_next_digest(
    now: datetime, tz_name: str, digest_time: str, last_digest_date: str | None
) -> str:
    tz = ZoneInfo(tz_name)
    local_now = now.astimezone(tz)
    target = next_digest_at(now, tz_name, digest_time, last_digest_date)
    if target <= local_now + timedelta(seconds=5):
        return "в ближайшую проверку"
    if target.date() == local_now.date():
        day = "сегодня"
    elif target.date() == local_now.date() + timedelta(days=1):
        day = "завтра"
    else:
        day = target.strftime("%d.%m.%Y")
    return f"{day} в {target.strftime('%H:%M')} ({tz_name})"


def shorten(text: str, limit: int) -> str:
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) <= limit:
        return collapsed
    cut = collapsed[:limit].rsplit(" ", 1)[0].rstrip(" .,;:—–-")
    return f"{cut}…"


def utf16_len(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def h(value: str | None) -> str:
    return html.escape(value or "", quote=True)


def channel_display(title: str | None, username: str | None, fallback: str = "канал") -> str:
    if title:
        return title
    if username:
        return f"@{username}"
    return fallback
