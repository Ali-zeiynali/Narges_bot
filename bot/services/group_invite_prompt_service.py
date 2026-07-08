from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from sqlalchemy import select

from bot.services.menu_service import MenuService
from bot.services.quota_service import QuotaService
from bot.storage.database import Database
from bot.storage.orm import QuotaEventORM, UserORM


logger = logging.getLogger(__name__)


class GroupInvitePromptService:
    MARKER_KIND = "group_invite_prompt_sent"

    def __init__(self, database: Database, quota_service: QuotaService, min_age_hours: int = 3) -> None:
        self.database = database
        self.quota_service = quota_service
        self.min_age_hours = min_age_hours

    def due_users(self, limit: int = 100) -> list[int]:
        cutoff = datetime.now(UTC) - timedelta(hours=self.min_age_hours)
        with self.database.orm.session() as session:
            sent = select(QuotaEventORM.user_id).where(QuotaEventORM.kind == self.MARKER_KIND).subquery()
            rows = session.scalars(
                select(UserORM.telegram_id)
                .where(
                    UserORM.onboarding_state == "ready",
                    UserORM.created_at <= cutoff,
                    ~UserORM.telegram_id.in_(select(sent.c.user_id)),
                )
                .order_by(UserORM.created_at.asc())
                .limit(limit)
            ).all()
        return [int(row) for row in rows]

    def mark_sent(self, user_id: int) -> None:
        self.quota_service.record_marker(user_id, self.MARKER_KIND)


class GroupInvitePromptScheduler:
    def __init__(self, service: GroupInvitePromptService, bot: Bot, menu_service: MenuService, poll_seconds: int = 600) -> None:
        self.service = service
        self.bot = bot
        self.menu_service = menu_service
        self.poll_seconds = poll_seconds

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("group_invite_prompt_scheduler_failed")
            await asyncio.sleep(self.poll_seconds)

    async def run_once(self) -> None:
        try:
            me = await self.bot.get_me()
            username = me.username
        except Exception:
            username = "narges_aibot"
        for user_id in self.service.due_users():
            try:
                await self.bot.send_message(
                    user_id,
                    self.menu_service.group_invite_text(),
                    reply_markup=self.menu_service.group_invite_keyboard(username),
                )
                self.service.mark_sent(user_id)
                await asyncio.sleep(0.04)
            except Exception as exc:
                logger.warning("group_invite_prompt_send_failed user_id=%s error=%s", user_id, exc)
