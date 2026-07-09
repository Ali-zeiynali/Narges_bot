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
from bot.services.quota_service import QuotaService
from bot.storage.orm import ConversationMessageORM, GroupChatORM, GroupEngineEventORM, GroupInviteRewardORM, ScheduledGroupMessageORM


logger = logging.getLogger(__name__)

ACTIVE_BOT_STATUSES = {"member", "administrator", "creator"}
HIDDEN_BOT_STATUSES = {"left", "kicked", "rejected"}
GROUP_AUTO_REACTION_COOLDOWN_SECONDS = 2 * 60 * 60


@dataclass(frozen=True)
class MessageDeliveryResult:
    target_id: int
    status: str
    telegram_message_id: int | None = None
    error: str | None = None
    target_type: str = "chat"


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
        member_count: int | None = None,
        active: bool | None = None,
    ) -> None:
        now = datetime.now(UTC)
        normalized_status = (bot_status or "").strip().lower() or None
        if active is None:
            active = normalized_status in ACTIVE_BOT_STATUSES if normalized_status else True
        if normalized_status in HIDDEN_BOT_STATUSES:
            active = False
        with self.database.orm.session() as session:
            row = session.get(GroupChatORM, chat_id)
            if row is None:
                row = GroupChatORM(
                    chat_id=chat_id,
                    title=title,
                    username=username,
                    chat_type=chat_type,
                    bot_status=normalized_status,
                    member_count=member_count,
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
            if normalized_status and not (normalized_status == "member" and row.bot_status in {"administrator", "creator"}):
                row.bot_status = normalized_status
            if member_count is not None:
                row.member_count = int(member_count)
            row.active = active
            row.updated_at = now
            row.last_seen_at = now

    def list_groups(self, only_active: bool = False) -> list[GroupChatORM]:
        with self.database.orm.session() as session:
            statement = select(GroupChatORM).where(
                GroupChatORM.active.is_(True),
                GroupChatORM.bot_status.not_in(HIDDEN_BOT_STATUSES) | GroupChatORM.bot_status.is_(None),
            ).order_by(desc(GroupChatORM.last_seen_at), GroupChatORM.chat_id)
            if only_active:
                statement = statement.where(GroupChatORM.active.is_(True))
            return list(session.scalars(statement).all())

    def target_group_ids(self) -> list[int]:
        with self.database.orm.session() as session:
            return [
                int(value)
                for value in session.scalars(
                    select(GroupChatORM.chat_id)
                    .where(
                        GroupChatORM.active.is_(True),
                        GroupChatORM.bot_status.not_in(HIDDEN_BOT_STATUSES) | GroupChatORM.bot_status.is_(None),
                    )
                    .order_by(GroupChatORM.chat_id)
                ).all()
            ]

    def mark_group_removed(self, chat_id: int, status: str = "kicked", error: str | None = None) -> None:
        now = datetime.now(UTC)
        normalized_status = (status or "kicked").strip().lower()
        with self.database.orm.session() as session:
            row = session.get(GroupChatORM, chat_id)
            if row is None:
                row = GroupChatORM(
                    chat_id=chat_id,
                    title=None,
                    username=None,
                    chat_type="group",
                    bot_status=normalized_status,
                    active=False,
                    created_at=now,
                    updated_at=now,
                    last_seen_at=now,
                )
                session.add(row)
            else:
                row.bot_status = normalized_status
                row.active = False
                row.updated_at = now
                row.last_seen_at = now
            session.add(
                GroupEngineEventORM(
                    chat_id=chat_id,
                    event_type="group_marked_inactive",
                    metadata_json=json.dumps({"status": normalized_status, "error": error}, ensure_ascii=False, default=str),
                    created_at=now,
                )
            )

    def error_means_bot_removed(self, error: str | None) -> bool:
        text = (error or "").lower()
        return any(
            marker in text
            for marker in (
                "bot was kicked",
                "bot kicked",
                "kicked from",
                "forbidden",
                "chat not found",
                "not enough rights",
                "bot is not a member",
            )
        )


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


async def send_messages_detailed(bot: Bot, chat_ids: list[int], text: str, target_type: str = "chat") -> list[MessageDeliveryResult]:
    results: list[MessageDeliveryResult] = []
    for chat_id in chat_ids:
        try:
            message = await bot.send_message(chat_id, text)
            results.append(MessageDeliveryResult(target_id=chat_id, status="sent", telegram_message_id=message.message_id, target_type=target_type))
            await asyncio.sleep(0.04)
        except Exception as exc:
            results.append(MessageDeliveryResult(target_id=chat_id, status="failed", error=f"{exc.__class__.__name__}: {exc}", target_type=target_type))
    return results


class GroupInviteRewardService:
    MEMBER_REWARD = 10
    ADMIN_REWARD = 10
    MIN_MEMBER_COUNT = 100

    def __init__(self, database: Database, quota_service: QuotaService) -> None:
        self.database = database
        self.quota_service = quota_service

    def bot_joined_or_promoted(
        self,
        *,
        chat_id: int,
        actor_user_id: int | None,
        status: str,
        member_count: int | None = None,
    ) -> dict[str, Any]:
        status = (status or "").strip().lower()
        if status not in ACTIVE_BOT_STATUSES:
            return {"member_granted": False, "admin_granted": False, "reason": "inactive_status"}
        if member_count is None or int(member_count) < self.MIN_MEMBER_COUNT:
            return {
                "member_granted": False,
                "admin_granted": False,
                "member_count": member_count,
                "required_member_count": self.MIN_MEMBER_COUNT,
                "reason": "group_too_small",
            }
        owner_id = self._owner_for_chat(chat_id) or actor_user_id
        if not owner_id:
            return {"member_granted": False, "admin_granted": False, "reason": "missing_owner"}
        now = datetime.now(UTC)
        member_granted = False
        admin_granted = False
        with self.database.orm.session() as session:
            row = session.scalar(
                select(GroupInviteRewardORM)
                .where(GroupInviteRewardORM.user_id == owner_id, GroupInviteRewardORM.chat_id == chat_id)
                .limit(1)
            )
            if row is None:
                row = GroupInviteRewardORM(
                    user_id=owner_id,
                    chat_id=chat_id,
                    member_granted=False,
                    admin_granted=False,
                    bot_status=status,
                    active=True,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            row.bot_status = status
            row.active = True
            row.updated_at = now
            if not row.member_granted:
                row.member_granted = True
                member_granted = True
            if status in {"administrator", "creator"} and not row.admin_granted:
                row.admin_granted = True
                admin_granted = True
        if member_granted:
            self.quota_service.add_extra_credit(owner_id, self.MEMBER_REWARD, reason=f"group_member:{chat_id}")
        if admin_granted:
            self.quota_service.add_extra_credit(owner_id, self.ADMIN_REWARD, reason=f"group_admin:{chat_id}")
        return {"user_id": owner_id, "member_granted": member_granted, "admin_granted": admin_granted}

    def bot_removed_or_demoted(self, *, chat_id: int, status: str) -> list[dict[str, Any]]:
        status = (status or "").strip().lower()
        now = datetime.now(UTC)
        pending_revokes: list[dict[str, Any]] = []
        with self.database.orm.session() as session:
            rows = list(session.scalars(select(GroupInviteRewardORM).where(GroupInviteRewardORM.chat_id == chat_id)).all())
            for row in rows:
                revoke_member = status in HIDDEN_BOT_STATUSES and row.member_granted
                revoke_admin = status not in {"administrator", "creator"} and row.admin_granted
                row.bot_status = status
                row.active = status in ACTIVE_BOT_STATUSES
                row.updated_at = now
                member_revoke = {}
                admin_revoke = {}
                if revoke_admin:
                    row.admin_granted = False
                if revoke_member:
                    row.member_granted = False
                if revoke_member or revoke_admin:
                    pending_revokes.append(
                        {
                            "user_id": row.user_id,
                            "member_revoked": revoke_member,
                            "admin_revoked": revoke_admin,
                            "member_revoke": member_revoke,
                            "admin_revoke": admin_revoke,
                        }
                    )
        results: list[dict[str, Any]] = []
        for item in pending_revokes:
            if item["admin_revoked"]:
                item["admin_revoke"] = self.quota_service.revoke_credit(item["user_id"], self.ADMIN_REWARD, reason=f"group_admin:{chat_id}")
            if item["member_revoked"]:
                item["member_revoke"] = self.quota_service.revoke_credit(item["user_id"], self.MEMBER_REWARD, reason=f"group_member:{chat_id}")
            results.append(item)
        return results

    def _owner_for_chat(self, chat_id: int) -> int | None:
        with self.database.orm.session() as session:
            row = session.scalar(
                select(GroupInviteRewardORM)
                .where(GroupInviteRewardORM.chat_id == chat_id, GroupInviteRewardORM.member_granted.is_(True))
                .order_by(GroupInviteRewardORM.id.asc())
                .limit(1)
            )
        return int(row.user_id) if row else None


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
                    if self.group_service.error_means_bot_removed(item.error):
                        self.group_service.mark_group_removed(item.target_id, status="kicked", error=item.error)
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
