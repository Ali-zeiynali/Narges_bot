import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from bot.models.channel import MembershipCheck, MembershipItem, RequiredChannel
from bot.storage.database import Database


VALID_MEMBER_STATUSES = {"member", "administrator", "creator"}


class RequiredChannelService:
    def __init__(self, database: Database, cache_seconds: int, admin_ids: tuple[int, ...]) -> None:
        self.database = database
        self.cache_seconds = cache_seconds
        self.admin_ids = set(admin_ids)

    def list_active(self) -> list[RequiredChannel]:
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM required_channels
                WHERE active = 1
                ORDER BY position ASC, id ASC
                """
            ).fetchall()
        return [self._row_to_channel(row) for row in rows]

    def list_all(self) -> list[RequiredChannel]:
        with closing(self.database.connect()) as connection:
            rows = connection.execute("SELECT * FROM required_channels ORDER BY position ASC, id ASC").fetchall()
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
        now = datetime.now(UTC).isoformat()
        if position is None:
            position = self._next_position()
        self.database.execute(
            """
            INSERT INTO required_channels(chat_id, title, join_url, position, is_private, active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                join_url = excluded.join_url,
                position = excluded.position,
                is_private = excluded.is_private,
                active = 1,
                updated_at = excluded.updated_at
            """,
            (chat_id, title, join_url, position, int(is_private), now, now),
        )
        channel = self._find_by_chat_id(chat_id)
        self._audit(admin_id, "upsert_channel", channel.id, None, channel)
        return channel

    def remove_channel(self, admin_id: int, channel_id: int) -> bool:
        before = self.get(channel_id)
        if before is None:
            return False
        self.database.execute(
            "UPDATE required_channels SET active = 0, updated_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), channel_id),
        )
        self._audit(admin_id, "remove_channel", channel_id, before, None)
        return True

    def move_channel(self, admin_id: int, channel_id: int, position: int) -> bool:
        before = self.get(channel_id)
        if before is None:
            return False
        self.database.execute(
            "UPDATE required_channels SET position = ?, updated_at = ? WHERE id = ?",
            (position, datetime.now(UTC).isoformat(), channel_id),
        )
        after = self.get(channel_id)
        self._audit(admin_id, "move_channel", channel_id, before, after)
        return True

    def get(self, channel_id: int) -> RequiredChannel | None:
        with closing(self.database.connect()) as connection:
            row = connection.execute("SELECT * FROM required_channels WHERE id = ?", (channel_id,)).fetchone()
        return self._row_to_channel(row) if row else None

    def grant_admin_bypass(self, admin_id: int, user_id: int, minutes: int, reason: str | None = None) -> None:
        now = datetime.now(UTC)
        self.database.execute(
            """
            INSERT INTO admin_bypasses(user_id, bypass_until, reason, created_by, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                bypass_until = excluded.bypass_until,
                reason = excluded.reason,
                created_by = excluded.created_by,
                created_at = excluded.created_at
            """,
            (
                user_id,
                (now + timedelta(minutes=minutes)).isoformat(),
                reason,
                admin_id,
                now.isoformat(),
            ),
        )
        self._audit(admin_id, "grant_bypass", None, None, {"user_id": user_id, "minutes": minutes})

    def has_admin_bypass(self, user_id: int) -> bool:
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                "SELECT bypass_until FROM admin_bypasses WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return bool(row and datetime.fromisoformat(row["bypass_until"]) > datetime.now(UTC))

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
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                """
                SELECT mc.*, rc.* FROM membership_cache mc
                JOIN required_channels rc ON rc.id = mc.channel_id
                WHERE mc.user_id = ? AND mc.channel_id = ? AND mc.expires_at > ?
                """,
                (user_id, channel_id, now.isoformat()),
            ).fetchone()
        if row is None:
            return None
        return MembershipItem(
            channel=self._row_to_channel(row),
            status=row["status"],
            is_member=bool(row["is_member"]),
            error=row["error"],
        )

    def _cache(self, user_id: int, item: MembershipItem) -> None:
        now = datetime.now(UTC)
        self.database.execute(
            """
            INSERT INTO membership_cache(user_id, channel_id, status, is_member, error, checked_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, channel_id) DO UPDATE SET
                status = excluded.status,
                is_member = excluded.is_member,
                error = excluded.error,
                checked_at = excluded.checked_at,
                expires_at = excluded.expires_at
            """,
            (
                user_id,
                item.channel.id,
                item.status,
                int(item.is_member),
                item.error,
                now.isoformat(),
                (now + timedelta(seconds=self.cache_seconds)).isoformat(),
            ),
        )

    def _next_position(self) -> int:
        with closing(self.database.connect()) as connection:
            row = connection.execute("SELECT COALESCE(MAX(position), 0) + 10 AS pos FROM required_channels").fetchone()
        return int(row["pos"])

    def _find_by_chat_id(self, chat_id: str) -> RequiredChannel:
        with closing(self.database.connect()) as connection:
            row = connection.execute("SELECT * FROM required_channels WHERE chat_id = ?", (chat_id,)).fetchone()
        return self._row_to_channel(row)

    def _audit(self, admin_id: int, action: str, channel_id: int | None, before, after) -> None:
        self.database.execute(
            """
            INSERT INTO channel_audit_logs(admin_id, action, channel_id, before_payload, after_payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                admin_id,
                action,
                channel_id,
                self._serialize(before),
                self._serialize(after),
                datetime.now(UTC).isoformat(),
            ),
        )

    def _serialize(self, value) -> str | None:
        if value is None:
            return None
        if hasattr(value, "__dict__"):
            return json.dumps(value.__dict__, ensure_ascii=False, default=str)
        return json.dumps(value, ensure_ascii=False, default=str)

    def _row_to_channel(self, row) -> RequiredChannel:
        return RequiredChannel(
            id=row["id"],
            chat_id=row["chat_id"],
            title=row["title"],
            join_url=row["join_url"],
            position=row["position"],
            is_private=bool(row["is_private"]),
            active=bool(row["active"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
