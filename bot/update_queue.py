import asyncio
import logging
import time
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass
from enum import Enum
from typing import Any


logger = logging.getLogger(__name__)


class EnqueueStatus(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    RATE_LIMITED = "rate_limited"
    FULL = "full"
    INVALID = "invalid"


@dataclass(frozen=True)
class TelegramUpdateJob:
    update_id: int
    payload: dict[str, Any]
    received_at: float
    actor_key: tuple[int, int] | None = None
    offline_backlog: bool = False


@dataclass(frozen=True)
class EnqueueResult:
    status: EnqueueStatus
    update_id: int | None = None
    user_id: int | None = None

    @property
    def accepted(self) -> bool:
        return self.status == EnqueueStatus.ACCEPTED


class UpdateIdempotencyStore:
    def __init__(self, max_items: int = 10_000) -> None:
        self.max_items = max_items
        self._seen: OrderedDict[int, float] = OrderedDict()
        self._lock = asyncio.Lock()

    async def reserve(self, update_id: int) -> bool:
        async with self._lock:
            if update_id in self._seen:
                self._seen.move_to_end(update_id)
                return False
            self._seen[update_id] = time.monotonic()
            while len(self._seen) > self.max_items:
                self._seen.popitem(last=False)
            return True

    async def mark_processed(self, update_id: int) -> None:
        async with self._lock:
            if update_id in self._seen:
                self._seen[update_id] = time.monotonic()
                self._seen.move_to_end(update_id)


class InMemoryRateLimiter:
    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[int, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, user_id: int | None) -> bool:
        if user_id is None or self.limit <= 0:
            return True
        now = time.monotonic()
        cutoff = now - self.window_seconds
        async with self._lock:
            events = self._events[user_id]
            while events and events[0] < cutoff:
                events.popleft()
            if len(events) >= self.limit:
                return False
            events.append(now)
            return True


class TelegramUpdateQueue:
    def __init__(
        self,
        *,
        maxsize: int,
        idempotency: UpdateIdempotencyStore,
        rate_limiter: InMemoryRateLimiter,
        startup_unix_time: float | None = None,
        backlog_latest_only: bool = True,
        backlog_grace_seconds: int = 30,
    ) -> None:
        self._queue: asyncio.Queue[TelegramUpdateJob] = asyncio.Queue(maxsize=maxsize)
        self._idempotency = idempotency
        self._rate_limiter = rate_limiter
        self._startup_unix_time = startup_unix_time or time.time()
        self._backlog_latest_only = backlog_latest_only
        self._backlog_grace_seconds = max(0, backlog_grace_seconds)
        self._latest_backlog_update_by_actor: dict[tuple[int, int], int] = {}
        self._latest_lock = asyncio.Lock()

    def qsize(self) -> int:
        return self._queue.qsize()

    async def enqueue(self, payload: dict[str, Any]) -> EnqueueResult:
        update_id = payload.get("update_id")
        if not isinstance(update_id, int):
            return EnqueueResult(EnqueueStatus.INVALID)

        user_id = extract_user_id(payload)
        actor_key = extract_actor_key(payload)
        offline_backlog = self._is_offline_backlog_message(payload)
        if not await self._idempotency.reserve(update_id):
            return EnqueueResult(EnqueueStatus.DUPLICATE, update_id=update_id, user_id=user_id)
        if not await self._rate_limiter.allow(user_id):
            logger.warning("webhook_rate_limited update_id=%s user_id=%s", update_id, user_id)
            return EnqueueResult(EnqueueStatus.RATE_LIMITED, update_id=update_id, user_id=user_id)
        if self._backlog_latest_only and offline_backlog and actor_key is not None:
            async with self._latest_lock:
                previous = self._latest_backlog_update_by_actor.get(actor_key)
                if previous is None or update_id > previous:
                    self._latest_backlog_update_by_actor[actor_key] = update_id

        try:
            self._queue.put_nowait(
                TelegramUpdateJob(
                    update_id=update_id,
                    payload=payload,
                    received_at=time.monotonic(),
                    actor_key=actor_key,
                    offline_backlog=offline_backlog,
                )
            )
        except asyncio.QueueFull:
            logger.error("update_queue_full update_id=%s user_id=%s", update_id, user_id)
            return EnqueueResult(EnqueueStatus.FULL, update_id=update_id, user_id=user_id)
        return EnqueueResult(EnqueueStatus.ACCEPTED, update_id=update_id, user_id=user_id)

    async def get(self) -> TelegramUpdateJob:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

    async def mark_processed(self, update_id: int) -> None:
        await self._idempotency.mark_processed(update_id)

    async def is_stale_backlog_job(self, job: TelegramUpdateJob) -> bool:
        if not self._backlog_latest_only or not job.offline_backlog or job.actor_key is None:
            return False
        async with self._latest_lock:
            latest = self._latest_backlog_update_by_actor.get(job.actor_key)
        return latest is not None and job.update_id < latest

    def _is_offline_backlog_message(self, payload: dict[str, Any]) -> bool:
        message = payload.get("message")
        if not isinstance(message, dict):
            return False
        message_date = message.get("date")
        if not isinstance(message_date, int):
            return False
        return message_date < self._startup_unix_time - self._backlog_grace_seconds


def extract_user_id(payload: dict[str, Any]) -> int | None:
    for key in ("message", "edited_message", "callback_query", "pre_checkout_query", "my_chat_member"):
        value = payload.get(key)
        if not isinstance(value, dict):
            continue
        user = value.get("from")
        if isinstance(user, dict) and isinstance(user.get("id"), int):
            return user["id"]
        if key == "my_chat_member":
            nested_user = value.get("from")
            if isinstance(nested_user, dict) and isinstance(nested_user.get("id"), int):
                return nested_user["id"]
    return None


def extract_actor_key(payload: dict[str, Any]) -> tuple[int, int] | None:
    message = payload.get("message")
    if not isinstance(message, dict):
        return None
    user = message.get("from")
    chat = message.get("chat")
    if not isinstance(user, dict) or not isinstance(chat, dict):
        return None
    user_id = user.get("id")
    chat_id = chat.get("id")
    if isinstance(user_id, int) and isinstance(chat_id, int):
        return (chat_id, user_id)
    return None
