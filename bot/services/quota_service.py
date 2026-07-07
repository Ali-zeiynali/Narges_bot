import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from bot.config import Settings
from bot.models.ai import NargesReply, ResponseMode
from bot.services.debug_service import DebugService
from bot.storage.database import Database
from bot.storage.orm import QuotaEventORM


QUOTA_UNIT_SCALE = 5
BUSY_MESSAGE = "عجول نباش! صبر کن قبلی رو جواب بدم."


@dataclass(frozen=True)
class QuotaCheck:
    ok: bool
    message: str
    remaining: int


@dataclass(frozen=True)
class AccountQuota:
    total_sent: int
    daily_remaining: int
    monthly_remaining: int
    extra_remaining: int
    daily_limit: int
    monthly_limit: int

    @property
    def can_send(self) -> bool:
        return (self.daily_remaining > 0 and self.monthly_remaining > 0) or self.extra_remaining > 0

    @property
    def effective_remaining(self) -> int:
        free_remaining = min(self.daily_remaining, self.monthly_remaining)
        return max(0, free_remaining) + max(0, self.extra_remaining)


class QuotaService:
    def __init__(self, database: Database, settings: Settings, debug_service: DebugService | None = None) -> None:
        self.database = database
        self.settings = settings
        self.debug_service = debug_service
        self._active_generations: set[int] = set()
        self._lock = asyncio.Lock()

    async def begin_generation(self, user_id: int) -> QuotaCheck:
        async with self._lock:
            if self._debug_bypass(user_id):
                return QuotaCheck(True, "", 10**9)
            if user_id in self._active_generations:
                return QuotaCheck(
                    False,
                    BUSY_MESSAGE,
                    self.account_quota(user_id).effective_remaining,
                )
            check = self.check_limits(user_id)
            if not check.ok:
                return check
            self._active_generations.add(user_id)
            self._record_event(user_id, "turn_start", 0)
            return check

    async def active_generation_check(self, user_id: int) -> QuotaCheck | None:
        async with self._lock:
            if self._debug_bypass(user_id) or user_id not in self._active_generations:
                return None
            return QuotaCheck(False, BUSY_MESSAGE, self.account_quota(user_id).effective_remaining)

    async def finish_generation(self, user_id: int) -> None:
        async with self._lock:
            self._active_generations.discard(user_id)

    def check_limits(self, user_id: int) -> QuotaCheck:
        if self._debug_bypass(user_id):
            return QuotaCheck(True, "", 10**9)
        account = self.account_quota(user_id)
        if not account.can_send:
            return QuotaCheck(
                False,
                (
                    "⚡ ظرفیت پیام‌هات تموم شده.\n\n"
                    f"روزانه: {account.daily_remaining / QUOTA_UNIT_SCALE:g}\n"
                    f"ماهانه: {account.monthly_remaining / QUOTA_UNIT_SCALE:g}\n"
                    f"اضافه: {account.extra_remaining / QUOTA_UNIT_SCALE:g}"
                ),
                0,
            )

        if account.extra_remaining <= 0:
            short_count = self._count_since(user_id, "turn_start", self.settings.rate_limit_short_window_seconds)
            if short_count >= self.settings.rate_limit_short_count:
                return QuotaCheck(
                    False,
                    "⚠️ کمی تند شد.\nچند لحظه صبر کن یا ظرفیت اضافه بگیر تا محدودیت سرعت برداشته بشه.",
                    account.effective_remaining,
                )
            long_count = self._count_since(user_id, "turn_start", self.settings.rate_limit_long_window_seconds)
            if long_count >= self.settings.rate_limit_long_count:
                return QuotaCheck(
                    False,
                    "⚠️ سقف چند دقیقه اخیر پر شده.\nبا ظرفیت اضافه محدودیت‌ها برداشته می‌شن.",
                    account.effective_remaining,
                )
        return QuotaCheck(True, "", account.effective_remaining)

    def consume_successful_reply(self, user_id: int, reply: NargesReply) -> int:
        if self._debug_bypass(user_id):
            return 0
        cost = self.reply_cost(reply)
        account = self.account_quota(user_id)
        kind = "quota_consume" if account.daily_remaining >= cost and account.monthly_remaining >= cost else "extra_consume"
        self._record_event(user_id, kind, cost)
        self._debug("quota_consumed", user_id, {"kind": kind, "cost_units": cost, "remaining": self.account_quota(user_id)})
        return cost

    def can_consume_reply(self, user_id: int, reply: NargesReply) -> bool:
        if self._debug_bypass(user_id):
            return True
        return self.account_quota(user_id).effective_remaining >= self.reply_cost(reply)

    def add_extra_credit(self, user_id: int, amount: int, reason: str = "manual") -> None:
        self._record_event(user_id, f"extra_grant:{reason}", amount * QUOTA_UNIT_SCALE)
        self._debug("extra_credit_granted", user_id, {"amount_messages": amount, "reason": reason})

    def remaining_today(self, user_id: int) -> int:
        return self.account_quota(user_id).daily_remaining // QUOTA_UNIT_SCALE

    def account_quota(self, user_id: int) -> AccountQuota:
        daily_used = self._used_since(user_id, "quota_consume", self._start_of_day())
        monthly_used = self._used_since(user_id, "quota_consume", self._start_of_month())
        extra_granted = self._sum_kind_prefix(user_id, "extra_grant")
        extra_used = self._sum_kind_prefix(user_id, "extra_consume")
        total_sent = self._sum_kind_prefix(user_id, "quota_consume") + extra_used
        return AccountQuota(
            total_sent=total_sent,
            daily_remaining=max(0, (self.settings.free_daily_quota * QUOTA_UNIT_SCALE) - daily_used),
            monthly_remaining=max(0, (self.settings.free_monthly_quota * QUOTA_UNIT_SCALE) - monthly_used),
            extra_remaining=max(0, extra_granted - extra_used),
            daily_limit=self.settings.free_daily_quota,
            monthly_limit=self.settings.free_monthly_quota,
        )

    def reply_cost(self, reply: NargesReply) -> int:
        words = " ".join(message.text for message in reply.messages).split()
        if len(words) <= 1:
            return 1
        if reply.mode == ResponseMode.DEEP:
            return 3 * QUOTA_UNIT_SCALE
        total_chars = sum(len(message.text) for message in reply.messages)
        if reply.mode == ResponseMode.DETAILED or total_chars > 1200 or len(reply.messages) >= 3:
            return 2 * QUOTA_UNIT_SCALE
        return QUOTA_UNIT_SCALE

    def _used_since(self, user_id: int, kind: str, since: datetime) -> int:
        with self.database.orm.session() as session:
            value = session.scalar(
                select(func.coalesce(func.sum(QuotaEventORM.cost), 0)).where(
                    QuotaEventORM.user_id == user_id,
                    QuotaEventORM.kind == kind,
                    QuotaEventORM.created_at >= since,
                )
            )
        return int(value or 0)

    def _count_since(self, user_id: int, kind: str, seconds: int) -> int:
        since = datetime.now(UTC) - timedelta(seconds=seconds)
        with self.database.orm.session() as session:
            value = session.scalar(
                select(func.count()).select_from(QuotaEventORM).where(
                    QuotaEventORM.user_id == user_id,
                    QuotaEventORM.kind == kind,
                    QuotaEventORM.created_at >= since,
                )
            )
        return int(value or 0)

    def _sum_kind_prefix(self, user_id: int, kind_prefix: str) -> int:
        with self.database.orm.session() as session:
            value = session.scalar(
                select(func.coalesce(func.sum(QuotaEventORM.cost), 0)).where(
                    QuotaEventORM.user_id == user_id,
                    QuotaEventORM.kind.like(f"{kind_prefix}%"),
                )
            )
        return int(value or 0)

    def _record_event(self, user_id: int, kind: str, cost: int) -> None:
        with self.database.orm.session() as session:
            session.add(QuotaEventORM(user_id=user_id, kind=kind, cost=cost, created_at=datetime.now(UTC)))

    def _debug(self, event: str, user_id: int, payload: dict) -> None:
        if self.debug_service:
            self.debug_service.log(event, payload, user_id=user_id)

    def _debug_bypass(self, user_id: int) -> bool:
        return bool(self.debug_service and self.debug_service.can_debug(user_id))

    def _start_of_day(self) -> datetime:
        return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    def _start_of_month(self) -> datetime:
        now = datetime.now(UTC)
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
