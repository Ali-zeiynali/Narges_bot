from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import Bot
from sqlalchemy import desc, select

from bot.storage.database import Database
from bot.storage.orm import GroupChatORM, ScheduledGroupMessageORM


logger = logging.getLogger(__name__)

ACTIVE_BOT_STATUSES = {"member", "administrator", "creator"}


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
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id, text)
            sent += 1
            await asyncio.sleep(0.04)
        except Exception as exc:
            failed += 1
            if first_error is None:
                first_error = f"{exc.__class__.__name__}: {exc}"
    return sent, failed, first_error


class GroupMessageScheduler:
    def __init__(self, group_service: GroupService, bot: Bot, poll_seconds: int = 60) -> None:
        self.group_service = group_service
        self.bot = bot
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
            sent, failed, error = await send_messages(self.bot, chat_ids, schedule.text)
            self.group_service.mark_schedule_run(schedule.id, sent, failed, error)
            logger.info(
                "scheduled_group_message_sent schedule_id=%s sent=%s failed=%s",
                schedule.id,
                sent,
                failed,
            )
