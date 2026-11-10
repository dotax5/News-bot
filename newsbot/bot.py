"""Telegram-бот: команды и управление подписками."""

from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BotCommand, Message, MessageOriginChannel

from .collector import CollectorError, NewsCollector
from .config import Settings
from .db import Database
from .models import Channel, User
from .rendering import CHUNK_LIMIT, ENTITY_LIMIT
from .utils import format_dt_local, h, shorten, utf16_len

log = logging.getLogger(__name__)


def bot_commands() -> list[BotCommand]:
    return [
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="addchannel", description="Добавить канал-источник"),
        BotCommand(command="delchannel", description="Удалить канал"),
        BotCommand(command="channels", description="Список каналов"),
        BotCommand(command="settings", description="Статус подписок"),
        BotCommand(command="pause", description="Пауза уведомлений"),
        BotCommand(command="resume", description="Возобновить уведомления"),
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


def _welcome_text(channels: int, settings: Settings) -> str:
    lines = [
        (
            "👋 <b>Привет!</b> Я присылаю новые посты из Telegram-каналов "
            "сразу после публикации."
        ),
        "",
        f"📚 Каналов: <b>{channels}</b>",
        f"⏱ Проверка каналов: каждые {settings.collect_interval} с",
        "",
        "<b>Команды</b>",
        "<code>/addchannel @username</code> — добавить источник",
        "<code>/delchannel @username</code> — убрать источник",
        "<code>/channels</code> — список каналов",
        "<code>/settings</code> — статус подписок",
        "<code>/pause</code> и <code>/resume</code> — пауза и возобновление",
        "",
        (
            "<i>При добавлении канала пришлю его последний пост, дальше — только "
            "новые. Аккаунт-сборщик должен состоять в закрытых каналах.</i>"
        ),
    ]
    return "\n".join(lines)


def _settings_text(
    user: User,
    channels: int,
    settings: Settings,
    last_check_at: str | None,
    exact_copy: bool,
) -> str:
    status = "включены ✅" if user.enabled else "на паузе ⏸"
    media = (
        "копия поста целиком (фото/видео)"
        if exact_copy
        else "текст и ссылка на оригинал"
    )
    lines = [
        "⚙️ <b>Настройки</b>",
        f"🔔 Уведомления: {status}",
        f"📚 Каналов: <b>{channels}</b>",
        "🚀 Доставка: сразу после публикации",
        f"📎 Посты: {media}",
        f"⏱ Проверка каналов: каждые {settings.collect_interval} с",
    ]
    if last_check_at:
        lines.append(
            "🕓 Последняя проверка: "
            f"{format_dt_local(last_check_at, settings.display_timezone, '%d.%m.%Y %H:%M')}"
        )
    else:
        lines.append("🕓 Последняя проверка: ещё не было")
    if channels == 0:
        lines += ["", "Добавьте канал: <code>/addchannel @username</code>"]
    return "\n".join(lines)


def build_router(
    db: Database,
    settings: Settings,
    collector: NewsCollector | None,
    *,
    exact_copy: bool = False,
    collector_id: int | None = None,
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

    @router.message(F.forward_origin)
    async def remember_mirror(message: Message) -> None:
        """Запоминает id поста, пересланного боту аккаунтом-сборщиком.

        В личных чатах id сообщений у каждого аккаунта свои, поэтому id из
        сессии сборщика боту не подходят — берём те, что пришли в апдейте.
        """
        if collector_id is None or message.from_user is None:
            return
        if message.from_user.id != collector_id:
            return
        origin = message.forward_origin
        if not isinstance(origin, MessageOriginChannel):
            return
        await db.attach_mirror_message(
            origin.chat.id, origin.message_id, message.chat.id, message.message_id
        )

    @router.message(CommandStart())
    async def cmd_start(message: Message) -> None:
        if message.from_user is None:
            return
        existing = await db.get_user(message.from_user.id)
        user = await db.ensure_user(
            message.from_user.id,
            message.from_user.username,
            message.from_user.first_name,
        )
        if existing is None and settings.auto_subscribe_env_channels:
            await db.subscribe_env_channels(user.id)
        channels = await db.count_user_channels(user.id)
        prefix = ""
        if existing is not None:
            prefix = "С возвращением! Подписки сохранены.\n\n"
        await message.answer(prefix + _welcome_text(channels, settings))

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        user = await _get_user(message)
        if user is None:
            return
        channels = await db.count_user_channels(user.id)
        await message.answer(_welcome_text(channels, settings))

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
            "Новые посты будут приходить сразу после публикации.\n"
            "При первом сборе пришлю последний пост канала — историю не отправляю."
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
            "Новые посты из него больше не приходят."
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
                    "   последний пост: "
                    f"{format_dt_local(item.last_post_at, settings.display_timezone)}"
                )
            else:
                lines.append("   постов пока нет")
            if channel.last_error:
                lines.append(f"   ⚠️ {h(shorten(channel.last_error, 120))}")
            lines.append("")
        lines.append("<i>Удалить: /delchannel 2</i>")
        await _answer_long(message, "\n".join(lines))

    @router.message(Command("settings"))
    async def cmd_settings(message: Message) -> None:
        user = await _get_user(message)
        if user is None:
            return
        channels = await db.count_user_channels(user.id)
        last_check = await db.last_channel_check_at(user.id)
        await message.answer(
            _settings_text(user, channels, settings, last_check, exact_copy)
        )

    @router.message(Command("pause"))
    async def cmd_pause(message: Message) -> None:
        user = await _get_user(message)
        if user is None:
            return
        await db.set_user_enabled(user.id, False)
        await message.answer(
            "⏸ Уведомления на паузе.\n"
            "Пропущенные посты не дошлю: после /resume будут приходить только новые."
        )

    @router.message(Command("resume"))
    async def cmd_resume(message: Message) -> None:
        user = await _get_user(message)
        if user is None:
            return
        await db.set_user_enabled(user.id, True)
        await message.answer(
            "▶️ Уведомления включены. Новые посты снова приходят сразу после публикации."
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
