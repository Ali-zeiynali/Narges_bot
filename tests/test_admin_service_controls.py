import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bot.admin.services import AdminDataService
from bot.config import Settings
from bot.storage.database import Database
from bot.storage.orm import ConversationMessageORM, UserORM


def make_settings(provider_config: str) -> Settings:
    return Settings(
        telegram_token="t",
        telegram_proxy=None,
        groq_proxy=None,
        groq_api_key="g",
        groq_model="m",
        groq_temperature=0.7,
        groq_max_completion_tokens=7000,
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
        ai_providers_config=provider_config,
    )


class AdminServiceControlsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = str(Path(self.tmp.name) / "ai_providers.json")
        self.database = Database(str(Path(self.tmp.name) / "test.sqlite3"))
        self.database.migrate()
        self.settings = make_settings(self.config_path)
        self.service = AdminDataService(self.database, self.settings)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_target_user_ids_can_filter_by_recent_activity(self) -> None:
        now = datetime.now(UTC)
        with self.database.orm.session() as session:
            session.add(UserORM(telegram_id=1, onboarding_state="ready", created_at=now, updated_at=now))
            session.add(UserORM(telegram_id=2, onboarding_state="ready", created_at=now, updated_at=now))
            session.add(
                ConversationMessageORM(
                    user_id=1,
                    chat_id=1,
                    telegram_message_id=10,
                    role="user",
                    message_type="chat",
                    text="recent",
                    text_hash="recent",
                    provider=None,
                    model=None,
                    provider_response_id=None,
                    created_at=now - timedelta(minutes=30),
                )
            )
            session.add(
                ConversationMessageORM(
                    user_id=2,
                    chat_id=2,
                    telegram_message_id=20,
                    role="user",
                    message_type="chat",
                    text="old",
                    text_hash="old",
                    provider=None,
                    model=None,
                    provider_response_id=None,
                    created_at=now - timedelta(hours=5),
                )
            )

        self.assertEqual(self.service.target_user_ids(active_within_hours=2), [1])

    def test_provider_keys_are_synced_to_config_by_provider_index(self) -> None:
        Path(self.config_path).write_text(
            json.dumps(
                {
                    "providers": [
                        {"name": "groq", "api_keys": ["g1"], "enabled": True},
                        {"name": "groq", "api_keys": ["g2"], "enabled": True},
                    ]
                }
            ),
            encoding="utf-8",
        )

        self.service.add_provider_key("1", "g3")
        self.service.delete_provider_key("0", 0)
        self.service.update_provider("1", {"enabled": "on", "model": "m2", "max_completion_tokens": "7000"})

        data = json.loads(Path(self.config_path).read_text(encoding="utf-8"))
        self.assertEqual(data["providers"][0]["api_keys"], [])
        self.assertEqual(data["providers"][1]["api_keys"], ["g2", "g3"])
        self.assertEqual(data["providers"][1]["model"], "m2")
        self.assertEqual(data["providers"][1]["max_completion_tokens"], 7000)


if __name__ == "__main__":
    unittest.main()
