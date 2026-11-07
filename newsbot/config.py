"""Загрузка и валидация настроек из .env."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "newsbot.sqlite3"
DEFAULT_SESSION_PATH = BASE_DIR / "data" / "newsbot.session"


class ConfigError(RuntimeError):
    """Ошибка конфигурации, которую нужно показать пользователю."""


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    api_id: int | None
    api_hash: str | None
    session_string: str | None
    session_path: Path
    database_path: Path
    default_timezone: str
    default_digest_time: str
    collect_interval: int
    initial_fetch_limit: int
    digest_max_posts: int
    send_empty_digest: bool
    source_channels: tuple[str, ...]
    auto_subscribe_env_channels: bool
    log_level: str

    @property
    def collector_enabled(self) -> bool:
        return self.api_id is not None and bool(self.api_hash)

    @property
    def data_dir(self) -> Path:
        return self.database_path.parent


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on", "да"}


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} должно быть целым числом, получено: {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} должно быть >= {minimum}, получено: {value}")
    return value


def _split_list(raw: str) -> tuple[str, ...]:
    parts = re.split(r"[,;\s]+", raw.strip())
    return tuple(part for part in parts if part)


def _resolve_path(raw: str, default: Path) -> Path:
    if not raw.strip():
        return default
    path = Path(raw.strip()).expanduser()
    return path if path.is_absolute() else (BASE_DIR / path).resolve()


def load_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env")

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise ConfigError(
            "BOT_TOKEN не задан. Откройте .env в корне проекта и вставьте токен от @BotFather."
        )
    if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", bot_token):
        raise ConfigError("BOT_TOKEN выглядит некорректно. Проверьте значение в .env.")

    api_id_raw = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    if bool(api_id_raw) != bool(api_hash):
        raise ConfigError("TELEGRAM_API_ID и TELEGRAM_API_HASH нужно задать вместе.")
    api_id: int | None = None
    if api_id_raw:
        try:
            api_id = int(api_id_raw)
        except ValueError as exc:
            raise ConfigError("TELEGRAM_API_ID должен быть числом.") from exc
        if api_id <= 0:
            raise ConfigError("TELEGRAM_API_ID должен быть положительным числом.")

    session_string = os.getenv("TELEGRAM_SESSION", "").strip() or None
    if session_string and (session_string.startswith("PASTE") or session_string == "..."):
        session_string = None

    timezone = os.getenv("DEFAULT_TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow"
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"Неизвестная таймзона DEFAULT_TIMEZONE: {timezone!r}") from exc

    digest_time = os.getenv("DEFAULT_DIGEST_TIME", "09:00").strip() or "09:00"
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", digest_time):
        raise ConfigError("DEFAULT_DIGEST_TIME должен быть в формате HH:MM, например 09:30.")
    hours, minutes = digest_time.split(":")
    digest_time = f"{int(hours):02d}:{int(minutes):02d}"

    return Settings(
        bot_token=bot_token,
        api_id=api_id,
        api_hash=api_hash,
        session_string=session_string,
        session_path=_resolve_path(
            os.getenv("TELEGRAM_SESSION_PATH", ""), DEFAULT_SESSION_PATH
        ),
        database_path=_resolve_path(os.getenv("DATABASE_PATH", ""), DEFAULT_DB_PATH),
        default_timezone=timezone,
        default_digest_time=digest_time,
        collect_interval=max(_env_int("COLLECT_INTERVAL_SECONDS", 300, 30), 30),
        initial_fetch_limit=max(_env_int("INITIAL_FETCH_LIMIT", 20, 1), 1),
        digest_max_posts=max(_env_int("DIGEST_MAX_POSTS", 50, 1), 1),
        send_empty_digest=_env_bool("SEND_EMPTY_DIGEST", False),
        source_channels=_split_list(os.getenv("SOURCE_CHANNELS", "")),
        auto_subscribe_env_channels=_env_bool("AUTO_SUBSCRIBE_ENV_CHANNELS", True),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )
