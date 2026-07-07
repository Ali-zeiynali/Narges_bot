import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import Update

from bot.update_queue import TelegramUpdateQueue


logger = logging.getLogger(__name__)


class TelegramUpdateWorker:
    def __init__(
        self,
        *,
        queue: TelegramUpdateQueue,
        dispatcher: Dispatcher,
        bot: Bot,
        workers: int = 1,
        backlog_debounce_seconds: float = 2.0,
    ) -> None:
        self.queue = queue
        self.dispatcher = dispatcher
        self.bot = bot
        self.workers = max(1, workers)
        self.backlog_debounce_seconds = max(0.0, backlog_debounce_seconds)
        self._tasks: list[asyncio.Task] = []
        self._actor_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._actor_locks_guard = asyncio.Lock()

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._run(worker_id), name=f"telegram-update-worker-{worker_id}")
            for worker_id in range(self.workers)
        ]
        logger.info("telegram_update_worker_started workers=%s", self.workers)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        logger.info("telegram_update_worker_stopped")

    async def _run(self, worker_id: int) -> None:
        while True:
            job = await self.queue.get()
            started_at = asyncio.get_running_loop().time()
            try:
                if job.offline_backlog and self.backlog_debounce_seconds:
                    await asyncio.sleep(self.backlog_debounce_seconds)
                if await self.queue.is_stale_backlog_job(job) or await self.queue.is_stale_job(job):
                    logger.info("telegram_update_skipped_stale worker=%s update_id=%s", worker_id, job.update_id)
                    await self.queue.mark_processed(job.update_id)
                    continue
                actor_lock = await self._actor_lock(job.actor_key)
                async with actor_lock:
                    await self.queue.mark_actor_active(job.actor_key, True)
                    await self._feed_update_with_retries(job, worker_id)
                await self.queue.mark_processed(job.update_id)
                elapsed_ms = int((asyncio.get_running_loop().time() - started_at) * 1000)
                logger.info("telegram_update_processed worker=%s update_id=%s elapsed_ms=%s", worker_id, job.update_id, elapsed_ms)
            except Exception:
                logger.exception("telegram_update_failed worker=%s update_id=%s", worker_id, job.update_id)
                await self.queue.mark_processed(job.update_id)
            finally:
                await self.queue.mark_actor_active(job.actor_key, False)
                self.queue.task_done()

    async def _actor_lock(self, actor_key: tuple[int, int] | None) -> asyncio.Lock:
        if actor_key is None:
            return asyncio.Lock()
        async with self._actor_locks_guard:
            lock = self._actor_locks.get(actor_key)
            if lock is None:
                lock = asyncio.Lock()
                self._actor_locks[actor_key] = lock
            return lock

    async def _feed_update_with_retries(self, job, worker_id: int, attempts: int = 3) -> None:
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                update = Update.model_validate(job.payload, context={"bot": self.bot})
                await self.dispatcher.feed_update(self.bot, update)
                return
            except Exception as exc:
                last_error = exc
                logger.exception(
                    "telegram_update_attempt_failed worker=%s update_id=%s attempt=%s",
                    worker_id,
                    job.update_id,
                    attempt,
                )
                if attempt < attempts:
                    await asyncio.sleep(min(attempt * 2, 6))
        raise last_error or RuntimeError("telegram update failed")
