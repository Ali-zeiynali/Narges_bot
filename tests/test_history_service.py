import tempfile
import unittest
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
        self.service.add(1, "user", "I am building a finance dashboard")
        self.service.add(1, "assistant", "Good, we can keep it focused.")
        self.service.add(2, "user", "finance dashboard secret from another user")

        recent = self.service.recent_turns(1, limit=10)
        results = self.service.search_user_messages(1, "finance dashboard")

        self.assertEqual(len(recent), 2)
        self.assertTrue(any("finance dashboard" in item["text"] for item in results))
        self.assertFalse(any("another user" in item["text"] for item in results))


if __name__ == "__main__":
    unittest.main()
