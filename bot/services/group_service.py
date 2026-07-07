from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import Bot
try:
    from aiogram.types import ReplyParameters
except Exception:  # pragma: no cover - depends on aiogram minor version
    ReplyParameters = None  # type: ignore[assignment]
from sqlalchemy import desc, func, select

from bot.storage.database import Database
from bot.storage.orm import ConversationMessageORM, GroupChatORM, GroupEngineEventORM, ScheduledGroupMessageORM


logger = logging.getLogger(__name__)

ACTIVE_BOT_STATUSES = {"member", "administrator", "creator"}
GROUP_AUTO_REACTION_COOLDOWN_SECONDS = 2 * 60 * 60


@dataclass(frozen=True)
class MessageDeliveryResult:
    target_id: int
    status: str
    telegram_message_id: int | None = None
    error: str | None = None


class GroupService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def upsert_group(
        self,
        chat_id: int,
        title: str | None,
        username: str | None,
        chat_type: str,
        bot_status: str | None = None,
        active: bool | None = None,
    ) -> None:
        now = datetime.now(UTC)
        if active is None:
            active = bot_status in ACTIVE_BOT_STATUSES if bot_status else True
        with self.database.orm.session() as session:
            row = session.get(GroupChatORM, chat_id)
            if row is None:
                row = GroupChatORM(
                    chat_id=chat_id,
                    title=title,
                    username=username,
                    chat_type=chat_type,
                    bot_status=bot_status,
                    active=active,
                    created_at=now,
                    updated_at=now,
                    last_seen_at=now,
                )
                session.add(row)
                return
            row.title = title or row.title
            row.username = username or row.username
            row.chat_type = chat_type or row.chat_type
            row.bot_status = bot_status or row.bot_status
            row.active = active
            row.updated_at = now
            row.last_seen_at = now

    def list_groups(self, only_active: bool = False) -> list[GroupChatORM]:
        with self.database.orm.session() as session:
            statement = select(GroupChatORM).order_by(desc(GroupChatORM.last_seen_at), GroupChatORM.chat_id)
            if only_active:
                statement = statement.where(GroupChatORM.active.is_(True))
            return list(session.scalars(statement).all())

    def target_group_ids(self) -> list[int]:
        with self.database.orm.session() as session:
            return [
                int(value)
                for value in session.scalars(
                    select(GroupChatORM.chat_id).where(GroupChatORM.active.is_(True)).order_by(GroupChatORM.chat_id)
                ).all()
            ]

    def record_observed_message(
        self,
        *,
        chat_id: int,
        user_id: int,
        telegram_message_id: int,
        text: str,
        created_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        text = " ".join((text or "").split()).strip()
        if not text:
            return
        now = (created_at or datetime.now(UTC)).astimezone(UTC)
        payload = {
            "source": "group_observer",
            "chat_id": chat_id,
            "telegram_message_id": telegram_message_id,
            **(metadata or {}),
        }
        with self.database.orm.session() as session:
            session.add(
                ConversationMessageORM(
                    user_id=user_id,
                    chat_id=chat_id,
                    telegram_message_id=telegram_message_id,
                    role="user",
                    message_type="group_observed",
                    text=text[:1600],
                    text_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    ai_request_payload_json=json.dumps(payload, ensure_ascii=False, default=str),
                    created_at=now,
                )
            )

    def record_outbound_message(
        self,
        *,
        chat_id: int,
        text: str,
        message_type: str,
        user_id: int | None = None,
        telegram_message_id: int | None = None,
        provider: str | None = None,
        model: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        text = (text or "").strip()
        if not text:
            return
        now = datetime.now(UTC)
        payload = {
            "source": message_type,
            "chat_id": chat_id,
            "telegram_message_id": telegram_message_id,
            **(metadata or {}),
        }
        with self.database.orm.session() as session:
            session.add(
                ConversationMessageORM(
                    user_id=int(user_id or 0),
                    chat_id=chat_id,
                    telegram_message_id=telegram_message_id,
                    role="assistant",
                    message_type=message_type,
                    text=text,
                    text_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    provider=provider,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                    ai_request_payload_json=json.dumps(payload, ensure_ascii=False, default=str),
                    created_at=now,
                )
            )

    def recent_observed_messages(self, chat_id: int, limit: int = 5) -> list[dict[str, Any]]:
        with self.database.orm.session() as session:
            rows = session.scalars(
                select(ConversationMessageORM)
                .where(
                    ConversationMessageORM.chat_id == chat_id,
                    ConversationMessageORM.message_type == "group_observed",
                    ConversationMessageORM.role == "user",
                )
                .order_by(desc(ConversationMessageORM.created_at), desc(ConversationMessageORM.id))
                .limit(max(1, min(limit, 10)))
            ).all()
        return [
            {
                "user_id": row.user_id,
                "message_id": row.telegram_message_id,
                "text": row.text,
                "created_at": row.created_at.isoformat() if isinstance(row.created_at, datetime) else str(row.created_at),
            }
            for row in reversed(rows)
        ]

    def record_engine_event(
        self,
        *,
        chat_id: int,
        event_type: str,
        user_id: int | None = None,
        telegram_message_id: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self.database.orm.session() as session:
            session.add(
                GroupEngineEventORM(
                    chat_id=chat_id,
                    user_id=user_id,
                    event_type=event_type,
                    telegram_message_id=telegram_message_id,
                    metadata_json=json.dumps(metadata, ensure_ascii=False, default=str) if metadata else None,
                    created_at=datetime.now(UTC),
                )
            )

    def event_count_since(self, *, event_type: str, seconds: int, chat_id: int | None = None, user_id: int | None = None) -> int:
        since = datetime.now(UTC) - timedelta(seconds=seconds)
        with self.database.orm.session() as session:
            statement = (
                select(func.count())
                .select_from(GroupEngineEventORM)
                .where(GroupEngineEventORM.event_type == event_type, GroupEngineEventORM.created_at >= since)
            )
            if chat_id is not None:
                statement = statement.where(GroupEngineEventORM.chat_id == chat_id)
            if user_id is not None:
                statement = statement.where(GroupEngineEventORM.user_id == user_id)
            value = session.scalar(statement)
        return int(value or 0)

    def cooldown_active(self, chat_id: int, event_type: str, seconds: int) -> bool:
        return self.event_count_since(event_type=event_type, chat_id=chat_id, seconds=seconds) > 0

    def create_schedule(self, text: str, interval_minutes: int, enabled: bool = True) -> int:
        now = datetime.now(UTC)
        interval_minutes = max(5, min(int(interval_minutes), 60 * 24 * 30))
        with self.database.orm.session() as session:
            row = ScheduledGroupMessageORM(
                text=text.strip(),
                interval_minutes=interval_minutes,
                enabled=enabled,
                next_run_at=now + timedelta(minutes=interval_minutes) if enabled else None,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return int(row.id)

    def update_schedule(self, schedule_id: int, text: str, interval_minutes: int, enabled: bool) -> bool:
        now = datetime.now(UTC)
        interval_minutes = max(5, min(int(interval_minutes), 60 * 24 * 30))
        with self.database.orm.session() as session:
            row = session.get(ScheduledGroupMessageORM, schedule_id)
            if row is None:
                return False
            was_enabled = bool(row.enabled)
            row.text = text.strip()
            row.interval_minutes = interval_minutes
            row.enabled = enabled
            if enabled and (not was_enabled or row.next_run_at is None):
                row.next_run_at = now + timedelta(minutes=interval_minutes)
            if not enabled:
                row.next_run_at = None
            row.updated_at = now
            return True

    def delete_schedule(self, schedule_id: int) -> bool:
        with self.database.orm.session() as session:
            row = session.get(ScheduledGroupMessageORM, schedule_id)
            if row is None:
                return False
            session.delete(row)
            return True

    def list_schedules(self) -> list[ScheduledGroupMessageORM]:
        with self.database.orm.session() as session:
            return list(session.scalars(select(ScheduledGroupMessageORM).order_by(desc(ScheduledGroupMessageORM.id))).all())

    def due_schedules(self) -> list[ScheduledGroupMessageORM]:
        now = datetime.now(UTC)
        with self.database.orm.session() as session:
            return list(
                session.scalars(
                    select(ScheduledGroupMessageORM)
                    .where(ScheduledGroupMessageORM.enabled.is_(True), ScheduledGroupMessageORM.next_run_at <= now)
                    .order_by(ScheduledGroupMessageORM.next_run_at, ScheduledGroupMessageORM.id)
                ).all()
            )

    def mark_schedule_run(self, schedule_id: int, sent: int, failed: int, error: str | None) -> None:
        now = datetime.now(UTC)
        with self.database.orm.session() as session:
            row = session.get(ScheduledGroupMessageORM, schedule_id)
            if row is None:
                return
            row.sent_count = int(row.sent_count or 0) + sent
            row.failed_count = int(row.failed_count or 0) + failed
            row.last_error = error
            row.last_run_at = now
            row.next_run_at = now + timedelta(minutes=max(5, int(row.interval_minutes or 5)))
            row.updated_at = now


async def send_messages(bot: Bot, chat_ids: list[int], text: str) -> tuple[int, int, str | None]:
    sent = 0
    failed = 0
    first_error: str | None = None
    for result in await send_messages_detailed(bot, chat_ids, text):
        if result.status == "sent":
            sent += 1
        else:
            failed += 1
            if first_error is None:
                first_error = result.error
    return sent, failed, first_error


async def send_messages_detailed(bot: Bot, chat_ids: list[int], text: str) -> list[MessageDeliveryResult]:
    results: list[MessageDeliveryResult] = []
    for chat_id in chat_ids:
        try:
            message = await bot.send_message(chat_id, text)
            results.append(MessageDeliveryResult(target_id=chat_id, status="sent", telegram_message_id=message.message_id))
            await asyncio.sleep(0.04)
        except Exception as exc:
            results.append(MessageDeliveryResult(target_id=chat_id, status="failed", error=f"{exc.__class__.__name__}: {exc}"))
    return results


class GroupMessageScheduler:
    def __init__(self, group_service: GroupService, bot: Bot, group_ai_service: Any | None = None, poll_seconds: int = 60) -> None:
        self.group_service = group_service
        self.bot = bot
        self.group_ai_service = group_ai_service
        self.poll_seconds = poll_seconds

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("group_message_scheduler_failed")
            await asyncio.sleep(self.poll_seconds)

    async def run_once(self) -> None:
        chat_ids = self.group_service.target_group_ids()
        if not chat_ids:
            return
        for schedule in self.group_service.due_schedules():
            deliveries = await send_messages_detailed(self.bot, chat_ids, schedule.text)
            sent = sum(1 for item in deliveries if item.status == "sent")
            failed = len(deliveries) - sent
            error = next((item.error for item in deliveries if item.error), None)
            for item in deliveries:
                if item.status == "sent":
                    self.group_service.record_outbound_message(
                        chat_id=item.target_id,
                        text=schedule.text,
                        message_type="group_scheduled",
                        telegram_message_id=item.telegram_message_id,
                        metadata={"schedule_id": schedule.id},
                    )
                    self.group_service.record_engine_event(
                        chat_id=item.target_id,
                        event_type="scheduled_group_message",
                        telegram_message_id=item.telegram_message_id,
                        metadata={"schedule_id": schedule.id},
                    )
                else:
                    self.group_service.record_engine_event(
                        chat_id=item.target_id,
                        event_type="scheduled_group_message_failed",
                        metadata={"schedule_id": schedule.id, "error": item.error},
                    )
            self.group_service.mark_schedule_run(schedule.id, sent, failed, error)
            logger.info(
                "scheduled_group_message_sent schedule_id=%s sent=%s failed=%s",
                schedule.id,
                sent,
                failed,
            )
        if self.group_ai_service is not None:
            await self._run_auto_reactions()

    async def _run_auto_reactions(self) -> None:
        try:
            bot_user = await self.bot.get_me()
            bot_identity = {
                "bot_id": bot_user.id,
                "username": f"@{bot_user.username}" if bot_user.username else "@narges_aibot",
                "expected_username": "@narges_aibot",
            }
        except Exception:
            bot_identity = {"username": "@narges_aibot", "expected_username": "@narges_aibot"}
        for group in self.group_service.list_groups(only_active=True):
            if self.group_service.cooldown_active(group.chat_id, "auto_reaction_check", GROUP_AUTO_REACTION_COOLDOWN_SECONDS):
                continue
            recent = self.group_service.recent_observed_messages(group.chat_id, limit=5)
            if len(recent) < 3:
                continue
            self.group_service.record_engine_event(chat_id=group.chat_id, event_type="auto_reaction_check")
            try:
                result = await self.group_ai_service.choose_auto_reaction(
                    chat_id=group.chat_id,
                    group_title=group.title,
                    recent_messages=recent,
                    bot_identity=bot_identity,
                )
            except Exception:
                logger.exception("group_auto_reaction_failed chat_id=%s", group.chat_id)
                self.group_service.record_engine_event(chat_id=group.chat_id, event_type="auto_reaction_failed")
                continue
            if result is None:
                self.group_service.record_engine_event(chat_id=group.chat_id, event_type="auto_reaction_skipped")
                continue
            text = "\n".join(item.text for item in result.reply.messages).strip()
            if not text:
                continue
            selected_message = next(
                (item for item in recent if int(item.get("message_id") or 0) == int(result.selected_message_id or 0)),
                {},
            )
            try:
                if ReplyParameters is None:
                    raise TypeError("ReplyParameters is not available")
                sent_message = await self.bot.send_message(
                    chat_id=group.chat_id,
                    text=text,
                    reply_parameters=ReplyParameters(message_id=result.selected_message_id, allow_sending_without_reply=True),
                )
                self.group_service.record_outbound_message(
                    chat_id=group.chat_id,
                    user_id=selected_message.get("user_id"),
                    text=text,
                    message_type="group_auto",
                    telegram_message_id=sent_message.message_id,
                    provider=result.provider,
                    model=result.model,
                    input_tokens=result.usage.get("prompt_tokens"),
                    output_tokens=result.usage.get("completion_tokens"),
                    total_tokens=result.usage.get("total_tokens"),
                    metadata={"reply_to_message_id": result.selected_message_id, "estimated_tokens": result.estimated_tokens},
                )
                self.group_service.record_engine_event(
                    chat_id=group.chat_id,
                    event_type="auto_reaction",
                    telegram_message_id=sent_message.message_id,
                    metadata={"provider": result.provider, "model": result.model, "reply_to_message_id": result.selected_message_id},
                )
            except TypeError:
                sent_message = await self.bot.send_message(
                    chat_id=group.chat_id,
                    text=text,
                    reply_to_message_id=result.selected_message_id,
                    allow_sending_without_reply=True,
                )
                self.group_service.record_outbound_message(
                    chat_id=group.chat_id,
                    user_id=selected_message.get("user_id"),
                    text=text,
                    message_type="group_auto",
                    telegram_message_id=sent_message.message_id,
                    provider=result.provider,
                    model=result.model,
                    input_tokens=result.usage.get("prompt_tokens"),
                    output_tokens=result.usage.get("completion_tokens"),
                    total_tokens=result.usage.get("total_tokens"),
                    metadata={"reply_to_message_id": result.selected_message_id, "estimated_tokens": result.estimated_tokens},
                )
                self.group_service.record_engine_event(
                    chat_id=group.chat_id,
                    event_type="auto_reaction",
                    telegram_message_id=sent_message.message_id,
                    metadata={"reply_to_message_id": result.selected_message_id},
                )
            except Exception:
                logger.exception("group_auto_reaction_send_failed chat_id=%s", group.chat_id)
                self.group_service.record_engine_event(chat_id=group.chat_id, event_type="auto_reaction_send_failed")
