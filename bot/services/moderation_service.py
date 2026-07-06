from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from bot.storage.database import Database
from bot.services.debug_service import DebugService
from bot.storage.orm import UserBlockORM, UserWarningEventORM


@dataclass(frozen=True)
class BlockStatus:
    blocked: bool
    blocked_until: datetime | None = None
    reason: str | None = None
    warning_count: int = 0


@dataclass(frozen=True)
class WarningResult:
    warning_count: int
    blocked_until: datetime | None
    message: str


class ModerationService:
    def __init__(self, database: Database, debug_service: DebugService | None = None) -> None:
        self.database = database
        self.debug_service = debug_service

    def get_block_status(self, user_id: int) -> BlockStatus:
        with self.database.orm.session() as session:
            row = session.get(UserBlockORM, user_id)
        if row is None:
            return BlockStatus(blocked=False)
        blocked_until = self._as_datetime(row.blocked_until)
        if blocked_until <= datetime.now(UTC):
            return BlockStatus(blocked=False, warning_count=row.warning_count)
        return BlockStatus(
            blocked=True,
            blocked_until=blocked_until,
            reason=row.reason,
            warning_count=row.warning_count,
        )

    def warning_count(self, user_id: int) -> int:
        with self.database.orm.session() as session:
            value = session.scalar(
                select(func.count()).select_from(UserWarningEventORM).where(UserWarningEventORM.user_id == user_id)
            )
        return int(value or 0)

    def apply_model_warning(
        self,
        user_id: int,
        reason: str,
        source_message_id: int | None,
    ) -> WarningResult:
        return self._apply_warning(user_id, reason, source="model_suggestion", source_message_id=source_message_id)

    def apply_manual_warning(self, user_id: int, reason: str) -> WarningResult:
        return self._apply_warning(user_id, reason, source="admin_panel", source_message_id=None)

    def delete_warning(self, user_id: int, warning_id: int) -> bool:
        with self.database.orm.session() as session:
            row = session.get(UserWarningEventORM, warning_id)
            if row is None or row.user_id != user_id:
                return False
            session.delete(row)
            session.flush()
            rows = session.scalars(
                select(UserWarningEventORM)
                .where(UserWarningEventORM.user_id == user_id)
                .order_by(UserWarningEventORM.created_at, UserWarningEventORM.id)
            ).all()
            for index, item in enumerate(rows, start=1):
                item.warning_count_after = index
            block = session.get(UserBlockORM, user_id)
            if block is not None:
                if len(rows) < 3:
                    session.delete(block)
                else:
                    block.warning_count = len(rows)
                    block.reason = rows[-1].reason
                    block.updated_at = datetime.now(UTC)
        if self.debug_service:
            self.debug_service.log("warning_deleted", {"warning_id": warning_id}, user_id=user_id)
        return True

    def _apply_warning(
        self,
        user_id: int,
        reason: str,
        *,
        source: str,
        source_message_id: int | None,
    ) -> WarningResult:
        now = datetime.now(UTC)
        count = self.warning_count(user_id) + 1
        blocked_until = self._block_until_for_count(user_id, count, now)
        with self.database.orm.session() as session:
            session.add(
                UserWarningEventORM(
                    user_id=user_id,
                    reason=reason.strip()[:240],
                    source=source,
                    source_message_id=source_message_id,
                    warning_count_after=count,
                    created_at=now,
                )
            )
            if blocked_until:
                block = session.get(UserBlockORM, user_id)
                if block is None:
                    block = UserBlockORM(user_id=user_id, warning_count=count, blocked_until=blocked_until, reason=reason.strip()[:240], updated_at=now)
                    session.add(block)
                else:
                    block.warning_count = count
                    block.blocked_until = blocked_until
                    block.reason = reason.strip()[:240]
                    block.updated_at = now

        result = WarningResult(
            warning_count=count,
            blocked_until=blocked_until,
            message=self.warning_message(count, blocked_until),
        )
        if self.debug_service:
            self.debug_service.log(
                "warning_applied",
                {"reason": reason, "source": source, "warning_count": count, "blocked_until": blocked_until},
                user_id=user_id,
            )
        return result

    def warning_message(self, warning_count: int, blocked_until: datetime | None) -> str:
        base = (
            "🔴 هشدار رسمی\n\n"
            "کاربر عزیز، شما به خاطر رفتار نامناسب اخطار دریافت کردید.\n"
            "در صورت ادامه این رفتار، حساب شما مسدود خواهد شد.\n\n"
            f"تعداد اخطارها: {warning_count}"
        )
        if blocked_until:
            return (
                f"{base}\n"
                f"وضعیت: مسدود تا {blocked_until.astimezone(UTC).strftime('%Y-%m-%d %H:%M UTC')}"
            )
        return base

    def block_message(self, status: BlockStatus) -> str:
        until = status.blocked_until.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if status.blocked_until else "نامشخص"
        return (
            "🔴 حساب شما موقتاً مسدود است.\n\n"
            f"تعداد اخطارها: {status.warning_count}\n"
            f"پایان مسدودی: {until}\n"
            "بعد از پایان زمان مسدودی می‌توانید دوباره از ربات استفاده کنید."
        )

    def _block_until_for_count(self, user_id: int, count: int, now: datetime) -> datetime | None:
        if count < 3:
            return None
        current = self.get_block_status(user_id)
        base = current.blocked_until if current.blocked and current.blocked_until and current.blocked_until > now else now
        if count == 3:
            return base + timedelta(days=7)
        if count == 4:
            return current.blocked_until if current.blocked else now + timedelta(days=7)
        return base + timedelta(days=30)

    def _as_datetime(self, value: datetime | str) -> datetime:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed
