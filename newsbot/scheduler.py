"""Планировщик ежедневной рассылки."""

from __future__ import annotations

import asyncio
import logging

from .config import Settings
from .db import Database
from .digest import DigestService
from .utils import digest_is_due, utcnow

log = logging.getLogger(__name__)


class DigestScheduler:
    def __init__(
        self,
        db: Database,
        digest: DigestService,
        settings: Settings,
        check_interval: float = 20.0,
    ) -> None:
        self._db = db
        self._digest = digest
        self._settings = settings
        self._check_interval = check_interval

    async def tick(self) -> None:
        now = utcnow()
        for user in await self._db.list_users(enabled_only=True):
            try:
                if not digest_is_due(
                    now, user.timezone, user.digest_time, user.last_digest_date
                ):
                    continue
                if await self._db.count_user_channels(user.id) == 0:
                    continue
                # Не отправляем пустой дайджест, пока сборщик ни разу не прошёл
                # по только что добавленным каналам.
                if await self._db.count_unchecked_channels(user.id) > 0:
                    continue
                result = await self._digest.send(user)
                if result.error:
                    log.warning("Рассылка для %s: %s", user.tg_id, result.error)
                await asyncio.sleep(0.3)
            except Exception:
                log.exception("Ошибка рассылки для пользователя %s", user.tg_id)

    async def run(self, stop_event: asyncio.Event) -> None:
        log.info("Планировщик рассылки запущен (проверка каждые %s с)", self._check_interval)
        while not stop_event.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("Ошибка в планировщике")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._check_interval)
            except TimeoutError:
                continue
