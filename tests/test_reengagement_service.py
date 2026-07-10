import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from bot.services.history_service import HistoryService
from bot.services.reengagement_service import ReengagementService
from bot.storage.database import Database
from bot.storage.orm import UserORM


class ReengagementServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.tmp.name) / "reengagement.sqlite3"))
        self.database.migrate()
        self.settings = SimpleNamespace(
            reengagement_enabled=True,
            reengagement_after_hours=4,
            groq_model="test-model",
            reengagement_message="follow up",
        )
        self.service = ReengagementService(self.database, self.settings, object(), object())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_user_is_due_only_once_after_private_message(self) -> None:
        old = datetime.now(UTC) - timedelta(hours=5)
        with self.database.orm.session() as session:
            session.add(UserORM(telegram_id=1, onboarding_state="ready", registration_state="ready"))
        HistoryService(self.database).add(1, "user", "hello", chat_id=1, created_at=old)

        self.assertEqual(self.service.due_users(), [1])
        self.service.mark_sent(1)
        HistoryService(self.database).add(1, "user", "new hello", chat_id=1, created_at=old)

        self.assertEqual(self.service.due_users(), [])

    def test_group_only_activity_does_not_schedule_private_followup(self) -> None:
        old = datetime.now(UTC) - timedelta(hours=5)
        with self.database.orm.session() as session:
            session.add(UserORM(telegram_id=2, onboarding_state="ready", registration_state="ready"))
        HistoryService(self.database).add(2, "user", "group", chat_id=-100, created_at=old, message_type="group_mention")

        self.assertEqual(self.service.due_users(), [])

    def test_message_is_plain_configured_text_without_model_call(self) -> None:
        self.assertEqual(self.service.generate_message(1), "follow up")


if __name__ == "__main__":
    unittest.main()
