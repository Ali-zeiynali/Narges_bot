import asyncio
import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import func, select

from bot.config import Settings
from bot.storage.database import Database
from bot.services.ai_provider_client import AIProviderClient
from bot.services.history_service import HistoryService
from bot.services.memory_service import MemoryService
from bot.storage.orm import ConversationMessageORM, UserORM


logger = logging.getLogger(__name__)


class ReengagementService:
    def __init__(self, database: Database, settings: Settings, ai_provider_client: AIProviderClient, memory_service: MemoryService) -> None:
        self.database = database
        self.settings = settings
        self.ai_provider_client = ai_provider_client
        self.memory_service = memory_service
        self.history_service = HistoryService(database)

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
                .where(~ConversationMessageORM.message_type.like("group_%"))
                .group_by(ConversationMessageORM.user_id)
                .subquery()
            )
            rows = session.execute(
                select(UserORM.telegram_id, last_messages.c.last_user_at, UserORM.last_reengagement_sent_at)
                .join(last_messages, last_messages.c.user_id == UserORM.telegram_id)
                .where(
                    UserORM.onboarding_state == "ready",
                    last_messages.c.last_user_at <= cutoff,
                    UserORM.last_reengagement_sent_at.is_(None),
                )
            ).all()
        due: list[int] = []
        for user_id, last_user_at, last_sent_at in rows:
            last_user_at = self._dt(last_user_at)
            last_sent_at = self._dt(last_sent_at) if last_sent_at else None
            if last_sent_at is None:
                due.append(int(user_id))
        return due

    def generate_message(self, user_id: int) -> str:
        # Re-engagement must never expose a provider's JSON envelope and does
        # not justify another model call. The admin-configured text is stable,
        # predictable and token-free.
        return str(self.settings.reengagement_message or "").strip()[:500]

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
                await self.bot.send_chat_action(user_id, ChatAction.TYPING)
                message = await asyncio.to_thread(self.service.generate_message, user_id)
                if not message:
                    continue
                sent = await self.bot.send_message(user_id, message)
                self.service.history_service.add(
                    user_id,
                    "assistant",
                    message,
                    chat_id=user_id,
                    telegram_message_id=sent.message_id,
                    message_type="reengagement",
                    ai_request_payload={"source": "reengagement", "one_time": True},
                )
                self.service.mark_sent(user_id)
                await asyncio.sleep(0.04)
            except TelegramForbiddenError:
                logger.info("reengagement_user_blocked user_id=%s", user_id)
            except Exception as exc:
                logger.warning("reengagement_send_failed user_id=%s error=%s", user_id, exc)
