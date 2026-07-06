import json
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import func, select

from bot.models.channel import MembershipCheck, MembershipItem, RequiredChannel
from bot.storage.database import Database
from bot.storage.orm import AdminBypassORM, ChannelAuditLogORM, MembershipCacheORM, RequiredChannelORM


VALID_MEMBER_STATUSES = {"member", "administrator", "creator"}


class RequiredChannelService:
    def __init__(self, database: Database, cache_seconds: int, admin_ids: tuple[int, ...]) -> None:
        self.database = database
        self.cache_seconds = cache_seconds
        self.admin_ids = set(admin_ids)

    def list_active(self) -> list[RequiredChannel]:
        with self.database.orm.session() as session:
            rows = session.scalars(
                select(RequiredChannelORM)
                .where(RequiredChannelORM.active.is_(True))
                .order_by(RequiredChannelORM.position.asc(), RequiredChannelORM.id.asc())
            ).all()
        return [self._row_to_channel(row) for row in rows]

    def list_all(self) -> list[RequiredChannel]:
        with self.database.orm.session() as session:
            rows = session.scalars(
                select(RequiredChannelORM).order_by(RequiredChannelORM.position.asc(), RequiredChannelORM.id.asc())
            ).all()
        return [self._row_to_channel(row) for row in rows]

    async def check_user(self, bot: Bot, user_id: int, use_cache: bool = True) -> MembershipCheck:
        channels = self.list_active()
        if not channels:
            return MembershipCheck(ok=True, missing=[], errors=[])
        if user_id in self.admin_ids or self.has_admin_bypass(user_id):
            return MembershipCheck(ok=True, missing=[], errors=[], bypassed=True)

        missing: list[MembershipItem] = []
        errors: list[MembershipItem] = []
        for channel in channels:
            cached = self._get_cached(user_id, channel.id) if use_cache else None
            item = cached or await self._fetch_membership(bot, user_id, channel)
            if item.error:
                errors.append(item)
                missing.append(item)
            elif not item.is_member:
                missing.append(item)
        return MembershipCheck(ok=not missing and not errors, missing=missing, errors=errors)

    def add_channel(
        self,
        admin_id: int,
        chat_id: str,
        title: str,
        join_url: str | None,
        is_private: bool,
        position: int | None = None,
    ) -> RequiredChannel:
        now = datetime.now(UTC)
        if position is None:
            position = self._next_position()
        with self.database.orm.session() as session:
            row = session.scalar(select(RequiredChannelORM).where(RequiredChannelORM.chat_id == chat_id))
            if row is None:
                row = RequiredChannelORM(
                    chat_id=chat_id,
                    title=title,
                    join_url=join_url,
                    position=position,
                    is_private=is_private,
                    active=True,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.title = title
                row.join_url = join_url
                row.position = position
                row.is_private = is_private
                row.active = True
                row.updated_at = now
        channel = self._find_by_chat_id(chat_id)
        self._audit(admin_id, "upsert_channel", channel.id, None, channel)
        return channel

    def remove_channel(self, admin_id: int, channel_id: int) -> bool:
        before = self.get(channel_id)
        if before is None:
            return False
        with self.database.orm.session() as session:
            row = session.get(RequiredChannelORM, channel_id)
            if row:
                session.query(MembershipCacheORM).filter(MembershipCacheORM.channel_id == channel_id).delete()
                session.delete(row)
        self._audit(admin_id, "remove_channel", channel_id, before, None)
        return True

    def move_channel(self, admin_id: int, channel_id: int, position: int) -> bool:
        before = self.get(channel_id)
        if before is None:
            return False
        with self.database.orm.session() as session:
            row = session.get(RequiredChannelORM, channel_id)
            if row:
                row.position = position
                row.updated_at = datetime.now(UTC)
        after = self.get(channel_id)
        self._audit(admin_id, "move_channel", channel_id, before, after)
        return True

    def get(self, channel_id: int) -> RequiredChannel | None:
        with self.database.orm.session() as session:
            row = session.get(RequiredChannelORM, channel_id)
        return self._row_to_channel(row) if row else None

    def grant_admin_bypass(self, admin_id: int, user_id: int, minutes: int, reason: str | None = None) -> None:
        now = datetime.now(UTC)
        with self.database.orm.session() as session:
            row = session.get(AdminBypassORM, user_id)
            if row is None:
                row = AdminBypassORM(user_id=user_id, bypass_until=now + timedelta(minutes=minutes), reason=reason, created_by=admin_id, created_at=now)
                session.add(row)
            else:
                row.bypass_until = now + timedelta(minutes=minutes)
                row.reason = reason
                row.created_by = admin_id
                row.created_at = now
        self._audit(admin_id, "grant_bypass", None, None, {"user_id": user_id, "minutes": minutes})

    def has_admin_bypass(self, user_id: int) -> bool:
        with self.database.orm.session() as session:
            row = session.get(AdminBypassORM, user_id)
        return bool(row and self._dt(row.bypass_until) > datetime.now(UTC))

    async def _fetch_membership(self, bot: Bot, user_id: int, channel: RequiredChannel) -> MembershipItem:
        try:
            member = await bot.get_chat_member(chat_id=channel.chat_id, user_id=user_id)
            status = getattr(member.status, "value", str(member.status))
            is_member = status in VALID_MEMBER_STATUSES
            item = MembershipItem(channel=channel, status=status, is_member=is_member)
            self._cache(user_id, item)
            return item
        except TelegramAPIError as exc:
            item = MembershipItem(channel=channel, status=None, is_member=False, error=exc.__class__.__name__)
            self._cache(user_id, item)
            return item

    def _get_cached(self, user_id: int, channel_id: int) -> MembershipItem | None:
        now = datetime.now(UTC)
        with self.database.orm.session() as session:
            cache = session.get(MembershipCacheORM, {"user_id": user_id, "channel_id": channel_id})
            channel_row = session.get(RequiredChannelORM, channel_id) if cache else None
        if cache is None or channel_row is None or self._dt(cache.expires_at) <= now:
            return None
        return MembershipItem(
            channel=self._row_to_channel(channel_row),
            status=cache.status,
            is_member=bool(cache.is_member),
            error=cache.error,
        )

    def _cache(self, user_id: int, item: MembershipItem) -> None:
        now = datetime.now(UTC)
        with self.database.orm.session() as session:
            row = session.get(MembershipCacheORM, {"user_id": user_id, "channel_id": item.channel.id})
            if row is None:
                row = MembershipCacheORM(
                    user_id=user_id,
                    channel_id=item.channel.id,
                    status=item.status,
                    is_member=item.is_member,
                    error=item.error,
                    checked_at=now,
                    expires_at=now + timedelta(seconds=self.cache_seconds),
                )
                session.add(row)
            else:
                row.status = item.status
                row.is_member = item.is_member
                row.error = item.error
                row.checked_at = now
                row.expires_at = now + timedelta(seconds=self.cache_seconds)

    def _next_position(self) -> int:
        with self.database.orm.session() as session:
            value = session.scalar(select(func.coalesce(func.max(RequiredChannelORM.position), 0) + 10))
        return int(value or 10)

    def _find_by_chat_id(self, chat_id: str) -> RequiredChannel:
        with self.database.orm.session() as session:
            row = session.scalar(select(RequiredChannelORM).where(RequiredChannelORM.chat_id == chat_id))
        return self._row_to_channel(row)

    def _audit(self, admin_id: int, action: str, channel_id: int | None, before, after) -> None:
        with self.database.orm.session() as session:
            session.add(
                ChannelAuditLogORM(
                    admin_id=admin_id,
                    action=action,
                    channel_id=channel_id,
                    before_payload=self._serialize(before),
                    after_payload=self._serialize(after),
                    created_at=datetime.now(UTC),
                )
            )

    def _serialize(self, value) -> str | None:
        if value is None:
            return None
        if hasattr(value, "__dict__"):
            return json.dumps(value.__dict__, ensure_ascii=False, default=str)
        return json.dumps(value, ensure_ascii=False, default=str)

    def _row_to_channel(self, row) -> RequiredChannel:
        return RequiredChannel(
            id=self._value(row, "id"),
            chat_id=self._value(row, "chat_id"),
            title=self._value(row, "title"),
            join_url=self._value(row, "join_url"),
            position=self._value(row, "position"),
            is_private=bool(self._value(row, "is_private")),
            active=bool(self._value(row, "active")),
            created_at=self._dt(self._value(row, "created_at")),
            updated_at=self._dt(self._value(row, "updated_at")),
        )

    def _value(self, row, name: str):
        if hasattr(row, name):
            return getattr(row, name)
        return row[name]

    def _dt(self, value: datetime | str) -> datetime:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed
