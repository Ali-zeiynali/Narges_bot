import tempfile
import unittest
from pathlib import Path

from bot.config import Settings
from bot.models.ai import NargesReply
from bot.services.quota_service import QuotaService
from bot.storage.database import Database


def make_settings() -> Settings:
    return Settings(
        telegram_token="t",
        telegram_proxy=None,
        groq_proxy=None,
        groq_api_key="g",
        groq_model="m",
        groq_temperature=0.7,
        groq_max_completion_tokens=512,
        max_request_tokens=4096,
        max_message_chars=4000,
        persona_version="v",
        database_path=":memory:",
        log_file="logs/test.log",
        log_level="INFO",
        admin_ids=(),
        support_url=None,
        free_daily_quota=3,
        free_monthly_quota=300,
        rate_limit_short_count=2,
        rate_limit_short_window_seconds=120,
        rate_limit_long_count=10,
        rate_limit_long_window_seconds=600,
        membership_cache_seconds=60,
        admin_bypass_minutes=60,
        debug_mode=False,
        debug_user_ids=(),
        name_transliteration_map={},
    )


class QuotaServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.tmp.name) / "test.sqlite3"))
        self.database.migrate()
        self.service = QuotaService(self.database, make_settings())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_one_active_generation_per_user(self) -> None:
        first = await self.service.begin_generation(1)
        second = await self.service.begin_generation(1)
        await self.service.finish_generation(1)

        self.assertTrue(first.ok)
        self.assertFalse(second.ok)
        self.assertEqual(second.message, "عجول نباش! صبر کن قبلی رو جواب بدم.")

    async def test_daily_quota_counts_reply_cost_once(self) -> None:
        reply = NargesReply.model_validate(
            {
                "mode": "deep",
                "messages": [{"text": "یک پاسخ عمیق", "delay_seconds": 0.1}],
            }
        )
        self.assertEqual(self.service.reply_cost(reply), 15)
        self.service.consume_successful_reply(1, reply)

        self.assertEqual(self.service.remaining_today(1), 0)

    async def test_one_word_reply_costs_one_fifth(self) -> None:
        reply = NargesReply.model_validate(
            {
                "mode": "short",
                "messages": [{"text": "باشه", "delay_seconds": 0.1}],
            }
        )

        self.assertEqual(self.service.reply_cost(reply), 1)

    async def test_group_reply_consumes_one_unit(self) -> None:
        started = await self.service.begin_group_generation(1)
        self.assertTrue(started.ok)

        consumed = self.service.consume_group_reply(1)
        await self.service.finish_generation(1)

        self.assertEqual(consumed, 1)
        self.assertEqual(self.service.account_quota(1).daily_remaining, 14)

    async def test_rate_limit_blocks_after_starts(self) -> None:
        first = await self.service.begin_generation(1)
        await self.service.finish_generation(1)
        second = await self.service.begin_generation(1)
        await self.service.finish_generation(1)
        third = await self.service.begin_generation(1)

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertFalse(third.ok)

    async def test_extra_credit_bypasses_rate_limit(self) -> None:
        self.service.add_extra_credit(1, 10, reason="test")
        first = await self.service.begin_generation(1)
        await self.service.finish_generation(1)
        second = await self.service.begin_generation(1)
        await self.service.finish_generation(1)
        third = await self.service.begin_generation(1)

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertTrue(third.ok)

    async def test_revoke_credit_consumes_extra_before_free_quota(self) -> None:
        self.service.add_extra_credit(1, 2, reason="test")

        result = self.service.revoke_credit(1, 4, reason="test")
        account = self.service.account_quota(1)

        self.assertEqual(result["extra_units"], 10)
        self.assertEqual(result["free_units"], 10)
        self.assertEqual(account.extra_remaining, 0)
        self.assertEqual(account.daily_remaining, 5)
        self.assertEqual(account.monthly_remaining, 1490)

    async def test_revoke_credit_never_makes_quota_negative(self) -> None:
        result = self.service.revoke_credit(1, 1000, reason="test")
        account = self.service.account_quota(1)

        self.assertEqual(result["extra_units"], 0)
        self.assertEqual(result["free_units"], 15)
        self.assertEqual(account.extra_remaining, 0)
        self.assertEqual(account.daily_remaining, 0)
        self.assertEqual(account.monthly_remaining, 1485)


if __name__ == "__main__":
    unittest.main()
