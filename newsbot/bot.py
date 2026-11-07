"""Telegram-бот: команды, настройки и ручной дайджест."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BotCommand, Message

from .collector import CollectorError, NewsCollector
from .config import Settings
from .db import Database
from .digest import DigestService
from .models import Channel, User
from .rendering import CHUNK_LIMIT, ENTITY_LIMIT, build_no_channels_text
from .utils import (
    describe_next_digest,
    format_dt_local,
    h,
    parse_digest_time,
    schedule_date_after_change,
    shorten,
    utcnow,
    utf16_len,
    validate_timezone,
)

log = logging.getLogger(__name__)

COMMON_TIMEZONES = (
    "Europe/Moscow, Europe/Kyiv, Europe/Minsk, Asia/Almaty, Asia/Tbilisi, "
    "Asia/Yerevan, Asia/Dubai, UTC"
)


def bot_commands() -> list[BotCommand]:
    return [
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="addchannel", description="Добавить канал-источник"),
        BotCommand(command="delchannel", description="Удалить канал"),
        BotCommand(command="channels", description="Список каналов"),
        BotCommand(command="settime", description="Время рассылки, например 09:30"),
        BotCommand(command="timezone", description="Часовой пояс"),
        BotCommand(command="settings", description="Текущие настройки"),
        BotCommand(command="digest", description="Прислать дайджест сейчас"),
        BotCommand(command="pause", description="Пауза рассылки"),
        BotCommand(command="resume", description="Возобновить рассылку"),
        BotCommand(command="help", description="Помощь"),
    ]


def _split_message(text: str) -> list[str]:
    if utf16_len(text) <= CHUNK_LIMIT and _entities(text) <= ENTITY_LIMIT:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    entities = 0
    for line in text.splitlines():
        line_length = utf16_len(line) + 1
        line_entities = line.count("<b>") + line.count("<i>") + line.count("<a ")
        if (
            length + line_length > CHUNK_LIMIT
            or entities + line_entities > ENTITY_LIMIT
        ) and current:
            chunks.append("\n".join(current))
            current = []
            length = 0
            entities = 0
        current.append(line)
        length += line_length
        entities += line_entities
    if current:
        chunks.append("\n".join(current))
    return chunks


def _entities(text: str) -> int:
    return text.count("<b>") + text.count("<i>") + text.count("<a ")


async def _answer_long(message: Message, text: str) -> None:
    for chunk in _split_message(text):
        await message.answer(chunk)


def _channel_handle(channel: Channel) -> str:
    return f" · @{h(channel.username)}" if channel.username else ""


def _next_date_after_change(user: User, now: datetime, tz: str, time: str) -> str | None:
    today = now.astimezone(ZoneInfo(tz)).date().isoformat()
    if user.last_digest_date == today:
        return today
    return schedule_date_after_change(now, tz, time)


def _welcome_text(user: User, channels: int) -> str:
    lines = [
        "👋 <b>Привет!</b> Я собираю посты из Telegram-каналов и присылаю дайджест.",
        "",
        f"⏰ Рассылка: <b>{user.digest_time}</b> ({h(user.timezone)})",
        f"📚 Каналов: <b>{channels}</b>",
        "",
        "<b>Команды</b>",
        "<code>/addchannel @username</code> — добавить источник",
        "<code>/delchannel @username</code> — убрать источник",
        "<code>/channels</code> — список каналов",
        "<code>/settime 09:30</code> — время рассылки",
        "<code>/timezone Europe/Moscow</code> — часовой пояс",
        "<code>/settings</code> — текущие настройки",
        "<code>/digest</code> — прислать дайджест сейчас",
        "<code>/pause</code> и <code>/resume</code> — пауза и возобновление",
        "",
        (
            "<i>Аккаунт-сборщик должен состоять в закрытых каналах. "
            "Публичные каналы читаются по @username.</i>"
        ),
    ]
    return "\n".join(lines)


def _settings_text(user: User, channels: int, now: datetime) -> str:
    status = "включена ✅" if user.enabled else "на паузе ⏸"
    lines = [
        "⚙️ <b>Настройки</b>",
        f"⏰ Время: <b>{user.digest_time}</b> ({h(user.timezone)})",
        f"🔔 Рассылка: {status}",
        f"📚 Каналов: <b>{channels}</b>",
        f"🗓 Ближайшая: {describe_next_digest(now, user.timezone, user.digest_time, user.last_digest_date)}",
    ]
    if user.last_digest_at:
        lines.append(
            f"🕓 Последний дайджест: {format_dt_local(user.last_digest_at, user.timezone, '%d.%m.%Y %H:%M')}"
        )
    if channels == 0:
        lines += ["", "Добавьте канал: <code>/addchannel @username</code>"]
    return "\n".join(lines)


def build_router(
    db: Database,
    settings: Settings,
    collector: NewsCollector | None,
    digest: DigestService,
) -> Router:
    router = Router(name="newsbot")

    async def _get_user(message: Message) -> User | None:
        if message.from_user is None:
            return None
        user = await db.get_user(message.from_user.id)
        if user is None:
            await message.answer("Сначала отправьте /start.")
            return None
        return user

    @router.message(CommandStart())
    async def cmd_start(message: Message) -> None:
        if message.from_user is None:
            return
        existing = await db.get_user(message.from_user.id)
        user = await db.ensure_user(
            message.from_user.id,
            message.from_user.username,
            message.from_user.first_name,
            settings.default_timezone,
            settings.default_digest_time,
        )
        if existing is None and settings.auto_subscribe_env_channels:
            await db.subscribe_env_channels(user.id)
        channels = await db.count_user_channels(user.id)
        prefix = ""
        if existing is not None:
            prefix = "С возвращением! Настройки сохранены.\n\n"
        await message.answer(prefix + _welcome_text(user, channels))

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        user = await _get_user(message)
        if user is None:
            return
        channels = await db.count_user_channels(user.id)
        await message.answer(_welcome_text(user, channels))

    @router.message(Command("addchannel"))
    async def cmd_add_channel(message: Message, command: CommandObject) -> None:
        user = await _get_user(message)
        if user is None:
            return
        raw = (command.args or "").strip()
        if not raw:
            await message.answer(
                "Укажите канал:\n"
                "<code>/addchannel @rian_ru</code>\n"
                "<code>/addchannel https://t.me/rian_ru</code>\n"
                "<code>/addchannel -1001234567890</code>"
            )
            return
        if collector is None:
            await message.answer(
                "❌ Сборщик не настроен: в .env нет TELEGRAM_API_ID / TELEGRAM_API_HASH "
                "или не выполнена авторизация. Смотрите README."
            )
            return

        status = await message.answer("🔎 Проверяю канал…")
        try:
            info = await asyncio.wait_for(collector.resolve_channel(raw), timeout=30)
        except TimeoutError:
            await status.edit_text("⏱ Telegram не ответил за 30 секунд. Попробуйте ещё раз.")
            return
        except CollectorError as exc:
            await status.edit_text(f"❌ {h(str(exc))}")
            return
        except Exception as exc:
            log.exception("Ошибка проверки канала")
            await status.edit_text(f"❌ Не удалось проверить канал: {h(str(exc))}")
            return

        channel, known = await db.add_or_update_channel(info)
        subscribed = await db.subscribe(user.id, channel.id)
        if not subscribed:
            await status.edit_text(
                f"ℹ️ Канал <b>{h(channel.title or channel.key)}</b> уже в вашем списке."
            )
            return
        note = "Канал уже был в базе — подписал вас." if known else "Канал добавлен."
        await status.edit_text(
            f"✅ <b>{h(channel.title or channel.key)}</b>{_channel_handle(channel)}\n"
            f"{note}\n\n"
            f"Сбор идёт каждые {settings.collect_interval // 60 or 1} мин. "
            "В ближайший дайджест попадут посты, опубликованные после прошлой рассылки."
        )

    @router.message(Command("delchannel"))
    async def cmd_del_channel(message: Message, command: CommandObject) -> None:
        user = await _get_user(message)
        if user is None:
            return
        raw = (command.args or "").strip()
        if not raw:
            await message.answer(
                "Укажите канал или его номер из /channels:\n"
                "<code>/delchannel @rian_ru</code> или <code>/delchannel 2</code>"
            )
            return

        channel: Channel | None = None
        if raw.isdigit():
            items = await db.list_user_channels_with_status(user.id)
            index = int(raw)
            if 1 <= index <= len(items):
                channel = items[index - 1].channel
        else:
            channel = await db.find_user_channel_by_ref(user.id, raw)

        if channel is None:
            await message.answer("Не нашёл такой канал в вашем списке. Посмотрите /channels.")
            return

        await db.unsubscribe(user.id, channel.id)
        await message.answer(
            f"🗑 Убрал <b>{h(channel.title or channel.key)}</b> из списка.\n"
            "Уже сохранённые посты останутся в базе, но новые собираться не будут."
        )

    @router.message(Command("channels"))
    async def cmd_channels(message: Message) -> None:
        user = await _get_user(message)
        if user is None:
            return
        items = await db.list_user_channels_with_status(user.id)
        if not items:
            await message.answer(
                "📭 Список пуст.\n\nДобавьте канал: <code>/addchannel @telegram</code>"
            )
            return
        lines = [f"📚 <b>Ваши каналы ({len(items)})</b>", ""]
        for index, item in enumerate(items, start=1):
            channel = item.channel
            lines.append(
                f"<b>{index}. {h(channel.title or channel.username or channel.key)}</b>"
                f"{_channel_handle(channel)}"
            )
            if item.last_post_at:
                lines.append(
                    f"   последний пост: {format_dt_local(item.last_post_at, user.timezone)}"
                )
            else:
                lines.append("   постов пока нет")
            if channel.last_error:
                lines.append(f"   ⚠️ {h(shorten(channel.last_error, 120))}")
            lines.append("")
        lines.append("<i>Удалить: /delchannel 2</i>")
        await _answer_long(message, "\n".join(lines))

    @router.message(Command("settime"))
    async def cmd_settime(message: Message, command: CommandObject) -> None:
        user = await _get_user(message)
        if user is None:
            return
        raw = (command.args or "").strip()
        if not raw:
            await message.answer(
                f"Сейчас рассылка в <b>{user.digest_time}</b> ({h(user.timezone)}).\n"
                "Изменить: <code>/settime 09:30</code>"
            )
            return
        try:
            hours, minutes = parse_digest_time(raw)
        except ValueError as exc:
            await message.answer(f"❌ {h(str(exc))}")
            return
        new_time = f"{hours:02d}:{minutes:02d}"
        now = utcnow()
        last_date = _next_date_after_change(user, now, user.timezone, new_time)
        await db.set_user_digest_time(user.id, new_time, last_date)
        await message.answer(
            f"✅ Время рассылки: <b>{new_time}</b> ({h(user.timezone)}).\n"
            f"Ближайшая: {describe_next_digest(now, user.timezone, new_time, last_date)}"
        )

    @router.message(Command("timezone"))
    async def cmd_timezone(message: Message, command: CommandObject) -> None:
        user = await _get_user(message)
        if user is None:
            return
        raw = (command.args or "").strip()
        if not raw:
            await message.answer(
                f"Текущий часовой пояс: <b>{h(user.timezone)}</b>.\n\n"
                f"Изменить: <code>/timezone Europe/Moscow</code>\n"
                f"Частые варианты: {h(COMMON_TIMEZONES)}"
            )
            return
        try:
            timezone = validate_timezone(raw)
        except ValueError as exc:
            await message.answer(f"❌ {h(str(exc))}")
            return
        now = utcnow()
        last_date = _next_date_after_change(user, now, timezone, user.digest_time)
        await db.set_user_timezone(user.id, timezone, last_date)
        await message.answer(
            f"✅ Часовой пояс: <b>{h(timezone)}</b>.\n"
            f"Время рассылки: <b>{user.digest_time}</b>.\n"
            f"Ближайшая: {describe_next_digest(now, timezone, user.digest_time, last_date)}"
        )

    @router.message(Command("settings"))
    async def cmd_settings(message: Message) -> None:
        user = await _get_user(message)
        if user is None:
            return
        channels = await db.count_user_channels(user.id)
        await message.answer(_settings_text(user, channels, utcnow()))

    @router.message(Command("digest"))
    async def cmd_digest(message: Message) -> None:
        user = await _get_user(message)
        if user is None:
            return
        if await db.count_user_channels(user.id) == 0:
            await message.answer(build_no_channels_text())
            return
        status = await message.answer("⏳ Формирую дайджест…")
        result = await digest.send(user, manual=True)
        if result.error:
            await status.edit_text(f"❌ {h(result.error)}")
            return
        with contextlib.suppress(Exception):
            await status.delete()

    @router.message(Command("pause"))
    async def cmd_pause(message: Message) -> None:
        user = await _get_user(message)
        if user is None:
            return
        await db.set_user_enabled(user.id, False)
        await message.answer(
            "⏸ Рассылка на паузе. Сбор по вашим каналам приостановлен.\n"
            "После /resume бот наверстает пропущенные посты (в пределах лимита)."
        )

    @router.message(Command("resume"))
    async def cmd_resume(message: Message) -> None:
        user = await _get_user(message)
        if user is None:
            return
        await db.set_user_enabled(user.id, True)
        updated = await db.get_user_by_id(user.id)
        assert updated is not None

        await message.answer(
            "▶️ Рассылка включена.\n"
            f"Ближайшая: {describe_next_digest(utcnow(), updated.timezone, updated.digest_time, updated.last_digest_date)}"
        )

    @router.message(F.text.startswith("/"))
    async def cmd_unknown(message: Message) -> None:
        await message.answer("Неизвестная команда. Список команд — /help")

    @router.message(F.text)
    async def fallback(message: Message) -> None:
        await message.answer(
            "Я работаю по командам. Начните с /start или посмотрите /help."
        )

    return router
