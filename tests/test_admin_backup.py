import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from bot.admin.services import AdminDataService
from bot.config import Settings
from bot.services.history_service import HistoryService
from bot.storage.database import Database
from bot.storage.orm import ConversationMessageORM, UserORM


def make_settings() -> Settings:
    return Settings(
        telegram_token="t",
        telegram_proxy=None,
        groq_proxy=None,
        groq_api_key="g",
        groq_model="m",
        groq_temperature=0.7,
        groq_max_completion_tokens=512,
        max_request_tokens=3000,
        max_message_chars=4000,
        persona_version="v",
        database_path=":memory:",
        log_file="logs/test.log",
        log_level="INFO",
        admin_ids=(),
        support_url=None,
        free_daily_quota=40,
        free_monthly_quota=300,
        rate_limit_short_count=6,
        rate_limit_short_window_seconds=120,
        rate_limit_long_count=15,
        rate_limit_long_window_seconds=600,
        membership_cache_seconds=60,
        admin_bypass_minutes=60,
        debug_mode=False,
        debug_user_ids=(),
        name_transliteration_map={},
    )


class AdminBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.source = Database(str(Path(self.tmp.name) / "source.sqlite3"))
        self.target = Database(str(Path(self.tmp.name) / "target.sqlite3"))
        self.source.migrate()
        self.target.migrate()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_backup_import_appends_without_overwriting_existing_primary_keys(self) -> None:
        with self.source.orm.session() as session:
            session.add(UserORM(telegram_id=1, username="source", first_name="Source"))
        HistoryService(self.source).add(1, "user", "hello from source")

        with self.target.orm.session() as session:
            session.add(UserORM(telegram_id=1, username="target", first_name="Target"))
        HistoryService(self.target).add(1, "user", "existing target message")

        source_service = AdminDataService(self.source, make_settings())
        target_service = AdminDataService(self.target, make_settings())
        payload = source_service.export_backup()
        report = target_service.import_backup(payload)

        with self.target.orm.session() as session:
            user = session.get(UserORM, 1)
            messages = session.scalars(select(ConversationMessageORM)).all()

        self.assertEqual(user.username, "target")
        self.assertEqual(len(messages), 2)
        self.assertGreaterEqual(report["inserted"], 2)
        self.assertGreaterEqual(report["skipped"], 1)

    def test_users_sort_handles_mixed_naive_and_aware_datetimes(self) -> None:
        database = Database(str(Path(self.tmp.name) / "mixed.sqlite3"))
        database.migrate()
        with database.orm.session() as session:
            session.add(
                UserORM(
                    telegram_id=10,
                    username="aware",
                    first_name="Aware",
                    created_at=datetime(2026, 7, 5, 12, 0, tzinfo=UTC),
                    updated_at=datetime(2026, 7, 5, 12, 0, tzinfo=UTC),
                )
            )
            session.add(
                UserORM(
                    telegram_id=20,
                    username="naive",
                    first_name="Naive",
                    created_at=datetime(2026, 7, 6, 12, 0),
                    updated_at=datetime(2026, 7, 6, 12, 0),
                )
            )
        service = AdminDataService(database, make_settings())

        users = service.users(sort="created")

        self.assertEqual([item["telegram_id"] for item in users], [20, 10])


if __name__ == "__main__":
    unittest.main()
