import tempfile
import unittest
from pathlib import Path

from bot.models.ai import RelationshipDelta
from bot.services.relationship_service import RelationshipService
from bot.storage.database import Database


class RelationshipServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.tmp.name) / "test.sqlite3"))
        self.database.migrate()
        self.service = RelationshipService(self.database)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_intimacy_changes_gradually_and_feeling_updates(self) -> None:
        first = self.service.apply_delta(
            1,
            RelationshipDelta(intimacy_delta=1, current_chat_feeling="happy"),
        )
        second = self.service.apply_delta(
            1,
            RelationshipDelta(intimacy_delta=1, current_chat_feeling="upset"),
        )

        self.assertEqual(first.intimacy_level, 2)
        self.assertEqual(second.intimacy_level, 3)
        self.assertEqual(second.current_chat_feeling, "upset")


if __name__ == "__main__":
    unittest.main()
