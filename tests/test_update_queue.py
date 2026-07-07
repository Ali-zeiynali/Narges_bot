import unittest

from bot.update_queue import EnqueueStatus, InMemoryRateLimiter, TelegramUpdateQueue, UpdateIdempotencyStore


def message_payload(update_id: int, user_id: int = 10, chat_id: int = 20, date: int = 2_000_000_000) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": date,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "u"},
            "text": "hello",
        },
    }


class TelegramUpdateQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.queue = TelegramUpdateQueue(
            maxsize=10,
            idempotency=UpdateIdempotencyStore(),
            rate_limiter=InMemoryRateLimiter(limit=100, window_seconds=60),
            startup_unix_time=1_000_000_000,
        )

    async def test_active_actor_still_accepts_new_update(self) -> None:
        await self.queue.mark_actor_active((20, 10), True)

        result = await self.queue.enqueue(message_payload(1))

        self.assertEqual(result.status, EnqueueStatus.ACCEPTED)
        self.assertEqual(self.queue.qsize(), 1)

    async def test_older_queued_update_is_not_stale_after_newer_update(self) -> None:
        first = await self.queue.enqueue(message_payload(1))
        second = await self.queue.enqueue(message_payload(2))

        self.assertTrue(first.accepted)
        self.assertTrue(second.accepted)
        first_job = await self.queue.get()

        self.assertFalse(await self.queue.is_stale_job(first_job))


if __name__ == "__main__":
    unittest.main()
