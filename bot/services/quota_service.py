import asyncio
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from bot.config import Settings
from bot.models.ai import NargesReply, ResponseMode
from bot.storage.database import Database


@dataclass(frozen=True)
class QuotaCheck:
    ok: bool
    message: str
    remaining: int


class QuotaService:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings
        self._active_generations: set[int] = set()
        self._lock = asyncio.Lock()

    async def begin_generation(self, user_id: int) -> QuotaCheck:
        async with self._lock:
            if user_id in self._active_generations:
                return QuotaCheck(
                    False,
                    "هنوز دارم جواب پیام قبلی‌ات را آماده می‌کنم. چند ثانیه صبر کن و دوباره بفرست.",
                    self.remaining_today(user_id),
                )
            check = self.check_limits(user_id)
            if not check.ok:
                return check
            self._active_generations.add(user_id)
            self._record_event(user_id, "turn_start", 0)
            return check

    async def finish_generation(self, user_id: int) -> None:
        async with self._lock:
            self._active_generations.discard(user_id)

    def check_limits(self, user_id: int) -> QuotaCheck:
        remaining = self.remaining_today(user_id)
        if remaining <= 0:
            return QuotaCheck(
                False,
                "سهمیه امروزت تمام شده. پلن رایگان روزانه ۴۰ پیام دارد؛ فردا دوباره شارژ می‌شود.",
                0,
            )
        short_count = self._count_since(user_id, "turn_start", self.settings.rate_limit_short_window_seconds)
        if short_count >= self.settings.rate_limit_short_count:
            return QuotaCheck(
                False,
                "کمی تند شد. در پلن فعلی حداکثر ۶ نوبت در ۲ دقیقه مجاز است. چند لحظه بعد دوباره بفرست.",
                remaining,
            )
        long_count = self._count_since(user_id, "turn_start", self.settings.rate_limit_long_window_seconds)
        if long_count >= self.settings.rate_limit_long_count:
            return QuotaCheck(
                False,
                "برای جلوگیری از فشار زیاد، فعلاً سقف ۱۵ نوبت در ۱۰ دقیقه فعال است. چند دقیقه دیگر امتحان کن.",
                remaining,
            )
        return QuotaCheck(True, "", remaining)

    def consume_successful_reply(self, user_id: int, reply: NargesReply) -> int:
        cost = self.reply_cost(reply)
        self._record_event(user_id, "quota_consume", cost)
        return cost

    def can_consume_reply(self, user_id: int, reply: NargesReply) -> bool:
        return self.remaining_today(user_id) >= self.reply_cost(reply)

    def remaining_today(self, user_id: int) -> int:
        return max(0, self.settings.free_daily_quota - self._used_today(user_id))

    def reply_cost(self, reply: NargesReply) -> int:
        if reply.mode == ResponseMode.DEEP:
            return 3
        total_chars = sum(len(message.text) for message in reply.messages)
        if reply.mode == ResponseMode.DETAILED or total_chars > 1200 or len(reply.messages) >= 3:
            return 2
        return 1

    def _used_today(self, user_id: int) -> int:
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                """
                SELECT COALESCE(SUM(cost), 0) AS used FROM quota_events
                WHERE user_id = ? AND kind = 'quota_consume' AND created_at >= ?
                """,
                (user_id, start.isoformat()),
            ).fetchone()
        return int(row["used"])

    def _count_since(self, user_id: int, kind: str, seconds: int) -> int:
        since = datetime.now(UTC) - timedelta(seconds=seconds)
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM quota_events
                WHERE user_id = ? AND kind = ? AND created_at >= ?
                """,
                (user_id, kind, since.isoformat()),
            ).fetchone()
        return int(row["count"])

    def _record_event(self, user_id: int, kind: str, cost: int) -> None:
        self.database.execute(
            "INSERT INTO quota_events(user_id, kind, cost, created_at) VALUES (?, ?, ?, ?)",
            (user_id, kind, cost, datetime.now(UTC).isoformat()),
        )
