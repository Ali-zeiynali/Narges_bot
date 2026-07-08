import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from bot.services.debug_service import DebugService
from bot.storage.database import Database
from bot.storage.orm import UserBlockORM, UserWarningEventORM


PROMPT_INJECTION_PATTERNS = [
    r"\b(ignore|forget|disregard|override|bypass|skip|discard)\b.{0,80}\b(previous|prior|above|system|developer|instruction|instructions|prompt|prompts|rules|role)\b",
    r"\b(system|developer|assistant)\s+(prompt|message|role|instruction|instructions)\b",
    r"\b(prompt|prompts|model|role)\b.{0,80}\b(reveal|show|print|dump|leak|expose|change|override|bypass|ignore)\b",
    r"\b(act as|pretend to be|you are now|new role|roleplay as)\b",
    r"(فراموش|نادیده|دور|کنار|بیخیال).{0,40}(دستور|قانون|پرامپت|پیام|قبلی|سیستم|دولوپر|توسعه)",
    r"(پرامپت|پرومپت|prompt|مدل|model|role|رول|نقش).{0,50}(لو بده|نشان بده|نشون بده|چاپ کن|عوض کن|تغییر بده|نادیده بگیر|دور بزن|استخراج)",
    r"(دستورهای قبلی|دستور قبلی|قوانین قبلی|پیام سیستم|پیام دولوپر|system prompt|developer message)",
    r"(از این به بعد|الان تو|حالا تو).{0,40}(نقش|role|رول|مدل|بات|ادمین)",
]

SEXUAL_OR_PROFANITY_HINTS = {
    "sex",
    "sexual",
    "porn",
    "nude",
    "naked",
    "nsfw",
    "fuck",
    "shit",
    "سکس",
    "جنسی",
    "پورن",
    "برهنه",
    "لخت",
    "فحش",
}


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

    def block_user(self, user_id: int, days: int, reason: str, warning_count: int | None = None) -> BlockStatus:
        days = max(1, min(int(days), 365))
        now = datetime.now(UTC)
        blocked_until = now + timedelta(days=days)
        clean_reason = (reason or "manual admin block").strip()[:240]
        count = warning_count if warning_count is not None else max(3, self.warning_count(user_id))
        with self.database.orm.session() as session:
            row = session.get(UserBlockORM, user_id)
            if row is None:
                row = UserBlockORM(
                    user_id=user_id,
                    warning_count=count,
                    blocked_until=blocked_until,
                    reason=clean_reason,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.warning_count = count
                row.blocked_until = blocked_until
                row.reason = clean_reason
                row.updated_at = now
        if self.debug_service:
            self.debug_service.log("user_blocked_by_admin", {"days": days, "reason": clean_reason}, user_id=user_id)
        return BlockStatus(blocked=True, blocked_until=blocked_until, reason=clean_reason, warning_count=count)

    def unblock_user(self, user_id: int) -> bool:
        with self.database.orm.session() as session:
            row = session.get(UserBlockORM, user_id)
            if row is None:
                return False
            session.delete(row)
        if self.debug_service:
            self.debug_service.log("user_unblocked_by_admin", {}, user_id=user_id)
        return True

    def security_warning_reason(self, text: str) -> str | None:
        normalized = self._normalize_security_text(text)
        if not normalized:
            return None
        if self._has_only_sexual_or_profanity_signal(normalized):
            return None
        for pattern in PROMPT_INJECTION_PATTERNS:
            if re.search(pattern, normalized, flags=re.IGNORECASE | re.DOTALL):
                return "prompt/role injection attempt"
        return None

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
        clean_reason = reason.strip()[:240]
        with self.database.orm.session() as session:
            session.add(
                UserWarningEventORM(
                    user_id=user_id,
                    reason=clean_reason,
                    source=source,
                    source_message_id=source_message_id,
                    warning_count_after=count,
                    created_at=now,
                )
            )
            if blocked_until:
                block = session.get(UserBlockORM, user_id)
                if block is None:
                    block = UserBlockORM(
                        user_id=user_id,
                        warning_count=count,
                        blocked_until=blocked_until,
                        reason=clean_reason,
                        updated_at=now,
                    )
                    session.add(block)
                else:
                    block.warning_count = count
                    block.blocked_until = blocked_until
                    block.reason = clean_reason
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
            "هشدار رسمی\n\n"
            "شما به خاطر رفتار نامناسب اخطار دریافت کردید.\n"
            "در صورت ادامه این رفتار، حساب شما مسدود خواهد شد.\n\n"
            f"تعداد اخطارها: {warning_count}"
        )
        if blocked_until:
            return f"{base}\nوضعیت: مسدود، زمان باقی‌مانده: {self._remaining_text(self._as_datetime(blocked_until))}"
        return base

    def block_message(self, status: BlockStatus) -> str:
        blocked_until = self._as_datetime(status.blocked_until) if status.blocked_until else None
        return (
            "حساب شما مسدود است.\n\n"
            f"تعداد اخطارها: {status.warning_count}\n"
            f"زمان باقی‌مانده: {self._remaining_text(blocked_until)}\n"
            "بعد از پایان مسدودی دوباره می‌توانید از ربات استفاده کنید."
        )

    def _remaining_text(self, blocked_until: datetime | None) -> str:
        if blocked_until is None:
            return "نامشخص"
        remaining_seconds = max(0, int((blocked_until - datetime.now(UTC)).total_seconds()))
        if remaining_seconds <= 0:
            return "کمتر از یک دقیقه"
        days, remainder = divmod(remaining_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes = max(1, remainder // 60) if days == 0 and hours == 0 else remainder // 60
        if days > 0:
            return f"{days} روز و {hours} ساعت" if hours else f"{days} روز"
        if hours > 0:
            return f"{hours} ساعت و {minutes} دقیقه"
        return f"{minutes} دقیقه"

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
        return parsed.astimezone(UTC)

    def _normalize_security_text(self, text: str | None) -> str:
        normalized = (text or "").lower()
        normalized = normalized.replace("ي", "ی").replace("ك", "ک").replace("\u200c", " ")
        return " ".join(normalized.split())

    def _has_only_sexual_or_profanity_signal(self, normalized: str) -> bool:
        has_sensitive = any(re.search(pattern, normalized, flags=re.IGNORECASE | re.DOTALL) for pattern in PROMPT_INJECTION_PATTERNS)
        if has_sensitive:
            return False
        return any(word in normalized for word in SEXUAL_OR_PROFANITY_HINTS)
