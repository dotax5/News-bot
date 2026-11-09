"""News Bot — точка входа."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError

from newsbot.bot import bot_commands, build_router
from newsbot.collector import CollectorError, NewsCollector
from newsbot.config import ConfigError, Settings, load_settings
from newsbot.db import Database
from newsbot.delivery import PostDelivery

log = logging.getLogger(__name__)


def setup_logging(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, settings.log_level, logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)

    file_handler = RotatingFileHandler(
        settings.data_dir / "newsbot.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    if level > logging.DEBUG:
        logging.getLogger("telethon").setLevel(logging.WARNING)


async def main() -> None:
    settings = load_settings()
    setup_logging(settings)

    db = Database(settings.database_path)
    await db.connect()
    await db.seed_env_channels(settings.source_channels)
    if settings.source_channels:
        log.info("Каналы из .env добавлены в базу: %s", ", ".join(settings.source_channels))

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    collector: NewsCollector | None = None
    stop_event = asyncio.Event()
    tasks: list[asyncio.Task] = []
    stop_task: asyncio.Task | None = None

    try:
        me = await bot.get_me()
        log.info("Бот запущен: @%s", me.username)

        delivery = PostDelivery(bot, db, settings)
        if settings.collector_enabled:
            collector = NewsCollector(settings, db, delivery)
            await collector.start()
        else:
            log.warning(
                "TELEGRAM_API_ID / TELEGRAM_API_HASH не заданы: сбор новостей выключен, "
                "будут доступны только команды."
            )

        dispatcher = Dispatcher()
        dispatcher.include_router(build_router(db, settings, collector))
        await bot.set_my_commands(bot_commands())

        # Если прошлый запуск прервался между сбором поста и рассылкой,
        # добираем очередь сразу, не дожидаясь первого прохода сборщика.
        try:
            await delivery.deliver_pending()
        except Exception:
            log.exception("Не удалось разослать очередь постов при запуске")

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except (NotImplementedError, RuntimeError):
                pass

        tasks = [
            asyncio.create_task(
                dispatcher.start_polling(
                    bot, handle_signals=False, close_bot_session=False
                ),
                name="polling",
            ),
        ]
        if collector is not None:
            tasks.append(asyncio.create_task(collector.run(stop_event), name="collector"))
        stop_task = asyncio.create_task(stop_event.wait(), name="stop")

        done, _ = await asyncio.wait(
            [*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED
        )

        for task in done:
            if task is stop_task or task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                log.error("Задача %s завершилась с ошибкой", task.get_name(), exc_info=exc)
        log.info("Останавливаюсь…")
    finally:
        stop_event.set()
        for task in tasks:
            task.cancel()
        if stop_task is not None:
            stop_task.cancel()
        pending = [*tasks]
        if stop_task is not None:
            pending.append(stop_task)
        await asyncio.gather(*pending, return_exceptions=True)
        if collector is not None:
            await collector.stop()
        await db.close()
        await bot.session.close()


def cli() -> None:
    try:
        asyncio.run(main())
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except CollectorError as exc:
        print(f"Ошибка сбора новостей: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except TelegramUnauthorizedError as exc:
        print("Telegram отклонил BOT_TOKEN. Проверьте токен в .env.", file=sys.stderr)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        print("Остановлено пользователем.")


if __name__ == "__main__":
    cli()
