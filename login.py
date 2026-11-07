"""Разовая авторизация Telegram-аккаунта для сбора новостей.

Запуск:
    python login.py          # создаст StringSession и напечатает строку для .env
    python login.py --file   # создаст файловую сессию data/newsbot.session
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SESSION_PATH = BASE_DIR / "data" / "newsbot.session"


def main() -> None:
    load_dotenv(BASE_DIR / ".env")

    raw_api_id = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    if not raw_api_id or not api_hash:
        print(
            "Сначала укажите TELEGRAM_API_ID и TELEGRAM_API_HASH в .env\n"
            "Получить: https://my.telegram.org/apps",
            file=sys.stderr,
        )
        raise SystemExit(1)
    try:
        api_id = int(raw_api_id)
    except ValueError:
        print("TELEGRAM_API_ID должен быть числом.", file=sys.stderr)
        raise SystemExit(1) from None

    if "--file" in sys.argv:
        raw_path = os.getenv("TELEGRAM_SESSION_PATH", "").strip()
        path = Path(raw_path).expanduser() if raw_path else DEFAULT_SESSION_PATH
        if not path.is_absolute():
            path = BASE_DIR / path
        path.parent.mkdir(parents=True, exist_ok=True)
        session: str | StringSession = str(path)
        print(f"Файловая сессия: {path}")
    else:
        session = StringSession()

    print("Введите номер телефона и код из Telegram (и пароль 2FA, если включён).")
    with TelegramClient(
        session,
        api_id,
        api_hash,
        device_model="News Digest Bot",
        system_version="1.0",
        app_version="1.0",
    ) as client:
        me = client.get_me()
        print(f"\nУспешно вошли как {me.first_name or ''} @{me.username or me.id}")

        if isinstance(session, StringSession):
            value = client.session.save()
            print("\nСкопируйте строку в .env (без кавычек и пробелов):\n")
            print(f"TELEGRAM_SESSION={value}\n")
            print("Никому не показывайте эту строку: она даёт доступ к аккаунту.")
        else:
            print("\nГотово. Укажите этот же TELEGRAM_SESSION_PATH в .env.")


if __name__ == "__main__":
    main()
