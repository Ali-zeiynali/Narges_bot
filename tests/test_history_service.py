import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from bot.services.history_service import HistoryService
from bot.storage.database import Database


class HistoryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.tmp.name) / "test.sqlite3"))
        self.database.migrate()
        self.service = HistoryService(self.database)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_recent_turns_and_search_are_user_scoped(self) -> None:
        message_date = datetime(2026, 7, 5, 12, 30, tzinfo=UTC)
        self.service.add(1, "user", "I am building a finance dashboard", created_at=message_date)
        self.service.add(1, "assistant", "Good, we can keep it focused.")
        self.service.add(2, "user", "finance dashboard secret from another user")

        recent = self.service.recent_turns(1, limit=10)
        results = self.service.search_user_messages(1, "finance dashboard")

        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[0]["created_at"], message_date.isoformat())
        self.assertTrue(any("finance dashboard" in item["text"] for item in results))
        self.assertFalse(any("another user" in item["text"] for item in results))


if __name__ == "__main__":
    unittest.main()
