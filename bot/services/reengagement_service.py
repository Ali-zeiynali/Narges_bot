import asyncio
import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from sqlalchemy import func, select

from bot.config import Settings
from bot.storage.database import Database
from bot.storage.orm import ConversationMessageORM, UserORM


logger = logging.getLogger(__name__)


class ReengagementService:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings

    def due_users(self) -> list[int]:
        if not self.settings.reengagement_enabled:
            return []
        cutoff = datetime.now(UTC) - timedelta(hours=self.settings.reengagement_after_hours)
        with self.database.orm.session() as session:
            last_messages = (
                select(
                    ConversationMessageORM.user_id.label("user_id"),
                    func.max(ConversationMessageORM.created_at).label("last_user_at"),
                )
                .where(ConversationMessageORM.role == "user")
                .group_by(ConversationMessageORM.user_id)
                .subquery()
            )
            rows = session.execute(
                select(UserORM.telegram_id, last_messages.c.last_user_at, UserORM.last_reengagement_sent_at)
                .join(last_messages, last_messages.c.user_id == UserORM.telegram_id)
                .where(
                    UserORM.onboarding_state == "ready",
                    last_messages.c.last_user_at <= cutoff,
                )
            ).all()
        due: list[int] = []
        for user_id, last_user_at, last_sent_at in rows:
            last_user_at = self._dt(last_user_at)
            last_sent_at = self._dt(last_sent_at) if last_sent_at else None
            if last_sent_at is None or last_sent_at < last_user_at:
                due.append(int(user_id))
        return due

    def mark_sent(self, user_id: int) -> None:
        with self.database.orm.session() as session:
            row = session.get(UserORM, user_id)
            if row is not None:
                row.last_reengagement_sent_at = datetime.now(UTC)
                row.updated_at = datetime.now(UTC)

    def _dt(self, value) -> datetime:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)


class ReengagementScheduler:
    def __init__(self, service: ReengagementService, bot: Bot) -> None:
        self.service = service
        self.bot = bot

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("reengagement_scheduler_failed")
            await asyncio.sleep(self.service.settings.reengagement_check_seconds)

    async def run_once(self) -> None:
        for user_id in self.service.due_users():
            try:
                await self.bot.send_message(user_id, self.service.settings.reengagement_message)
                self.service.mark_sent(user_id)
                await asyncio.sleep(0.04)
            except Exception as exc:
                logger.warning("reengagement_send_failed user_id=%s error=%s", user_id, exc)
