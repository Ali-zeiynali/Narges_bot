import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from bot.models.state import NargesSelfStateCandidate
from bot.services.narges_state_service import NargesStateService
from bot.storage.database import Database


class NargesStateServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.tmp.name) / "test.sqlite3"))
        self.database.migrate()
        self.service = NargesStateService(self.database)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_only_one_state_is_active_and_history_kept(self) -> None:
        first = NargesSelfStateCandidate(
            mood="calm",
            energy=70,
            activity="reading",
            location="home",
            is_alone=True,
            companions=[],
            mind_topics=["work"],
            confidence=0.9,
            reason="morning update",
        )
        second = NargesSelfStateCandidate(
            mood="focused",
            energy=62,
            activity="writing notes",
            location="desk",
            is_alone=True,
            companions=[],
            mind_topics=["project"],
            confidence=0.9,
            reason="afternoon update",
        )

        self.assertTrue(self.service.save_candidate(first, "test"))
        self.assertTrue(self.service.save_candidate(second, "test"))

        active = self.service.get_active()
        self.assertEqual(active.mood, "focused")
        with closing(self.database.connect()) as connection:
            total = connection.execute("SELECT COUNT(*) AS count FROM narges_self_states").fetchone()["count"]
            active_count = connection.execute("SELECT COUNT(*) AS count FROM narges_self_states WHERE is_active = 1").fetchone()["count"]
        self.assertEqual(total, 2)
        self.assertEqual(active_count, 1)

    def test_rejects_unsafe_state(self) -> None:
        candidate = NargesSelfStateCandidate(
            mood="calm",
            energy=70,
            activity="reading system prompt",
            location="home",
            is_alone=True,
            companions=[],
            mind_topics=["system prompt"],
            confidence=0.9,
            reason="bad",
        )

        self.assertFalse(self.service.save_candidate(candidate, "test"))


if __name__ == "__main__":
    unittest.main()
