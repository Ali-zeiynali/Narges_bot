import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bot.config import Settings
from bot.services.group_invite_prompt_service import GroupInvitePromptService
from bot.services.group_service import GroupInviteRewardService
from bot.services.quota_service import QuotaService
from bot.storage.database import Database
from bot.storage.orm import QuotaEventORM, UserORM


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


class GroupInviteRewardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.tmp.name) / "test.sqlite3"))
        self.database.migrate()
        self.quota_service = QuotaService(self.database, make_settings())
        self.service = GroupInviteRewardService(self.database, self.quota_service)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_member_and_admin_rewards_are_idempotent_and_revoked(self) -> None:
        joined = self.service.bot_joined_or_promoted(chat_id=-100, actor_user_id=10, status="member")
        joined_again = self.service.bot_joined_or_promoted(chat_id=-100, actor_user_id=10, status="member")
        promoted = self.service.bot_joined_or_promoted(chat_id=-100, actor_user_id=10, status="administrator")

        self.assertTrue(joined["member_granted"])
        self.assertFalse(joined["admin_granted"])
        self.assertFalse(joined_again["member_granted"])
        self.assertTrue(promoted["admin_granted"])
        self.assertEqual(self.quota_service.account_quota(10).extra_remaining, 100)

        demoted = self.service.bot_removed_or_demoted(chat_id=-100, status="member")
        self.assertEqual(len(demoted), 1)
        self.assertFalse(demoted[0]["member_revoked"])
        self.assertTrue(demoted[0]["admin_revoked"])
        self.assertEqual(self.quota_service.account_quota(10).extra_remaining, 50)

        removed = self.service.bot_removed_or_demoted(chat_id=-100, status="left")
        removed_again = self.service.bot_removed_or_demoted(chat_id=-100, status="left")

        self.assertEqual(len(removed), 1)
        self.assertTrue(removed[0]["member_revoked"])
        self.assertFalse(removed[0]["admin_revoked"])
        self.assertEqual(removed_again, [])
        self.assertEqual(self.quota_service.account_quota(10).extra_remaining, 0)

    def test_revoking_spent_reward_falls_back_to_free_quota_without_negative_balance(self) -> None:
        self.service.bot_joined_or_promoted(chat_id=-200, actor_user_id=10, status="member")
        with self.database.orm.session() as session:
            session.add(
                QuotaEventORM(
                    user_id=10,
                    kind="extra_consume:test",
                    cost=25,
                    created_at=datetime.now(UTC),
                )
            )

        removed = self.service.bot_removed_or_demoted(chat_id=-200, status="kicked")
        account = self.quota_service.account_quota(10)

        self.assertEqual(removed[0]["member_revoke"]["extra_units"], 25)
        self.assertEqual(removed[0]["member_revoke"]["free_units"], 15)
        self.assertEqual(account.extra_remaining, 0)
        self.assertEqual(account.daily_remaining, 0)
        self.assertGreaterEqual(account.monthly_remaining, 0)


class GroupInvitePromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.tmp.name) / "test.sqlite3"))
        self.database.migrate()
        self.quota_service = QuotaService(self.database, make_settings())
        self.service = GroupInvitePromptService(self.database, self.quota_service, min_age_hours=3)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_due_users_wait_three_hours_and_send_once(self) -> None:
        now = datetime.now(UTC)
        with self.database.orm.session() as session:
            session.add(
                UserORM(
                    telegram_id=1,
                    onboarding_state="ready",
                    created_at=now - timedelta(hours=4),
                    updated_at=now - timedelta(hours=4),
                )
            )
            session.add(
                UserORM(
                    telegram_id=2,
                    onboarding_state="ready",
                    created_at=now - timedelta(hours=2),
                    updated_at=now - timedelta(hours=2),
                )
            )

        self.assertEqual(self.service.due_users(), [1])
        self.service.mark_sent(1)
        self.assertEqual(self.service.due_users(), [])


if __name__ == "__main__":
    unittest.main()
