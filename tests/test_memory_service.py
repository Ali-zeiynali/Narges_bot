import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from bot.models.ai import MemorySuggestion
from bot.services.memory_service import MemoryService
from bot.storage.database import Database


class MemoryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.tmp.name) / "test.sqlite3"))
        self.database.migrate()
        self.service = MemoryService(self.database)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_saves_summary_not_full_message(self) -> None:
        suggestion = MemorySuggestion(
            action="save",
            kind="preference",
            summary="کاربر قهوه تلخ دوست دارد.",
            confidence=0.9,
        )
        self.service.apply_suggestions(1, 10, "من صبح‌ها قهوه تلخ می‌خورم.", [suggestion])

        memories = self.service.list_active(1)
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].summary, "کاربر قهوه تلخ دوست دارد.")
        self.assertNotIn("صبح‌ها", memories[0].summary)

    def test_rejects_sensitive_memory(self) -> None:
        suggestion = MemorySuggestion(
            action="save",
            kind="identity",
            summary="رمز کاربر 1234 است.",
            confidence=0.9,
        )
        self.service.apply_suggestions(1, 10, "رمز من 1234 است.", [suggestion])

        self.assertEqual(self.service.list_active(1), [])

    def test_retrieval_is_user_scoped_and_relevant(self) -> None:
        self.service.apply_suggestions(
            1,
            10,
            "I like tea.",
            [
                MemorySuggestion(
                    action="create",
                    kind="preference",
                    summary="User prefers black tea in the morning.",
                    confidence=0.9,
                    importance=4,
                )
            ],
        )
        self.service.apply_suggestions(
            2,
            20,
            "I like tea.",
            [
                MemorySuggestion(
                    action="create",
                    kind="preference",
                    summary="Other user prefers green tea.",
                    confidence=0.9,
                    importance=4,
                )
            ],
        )

        memories = self.service.retrieve_relevant(1, "tea in the morning")

        self.assertEqual(len(memories), 1)
        self.assertIn("black tea", memories[0].summary)

    def test_rejected_memory_is_audited(self) -> None:
        suggestion = MemorySuggestion(
            action="create",
            kind="identity",
            summary="api key is secret",
            confidence=0.9,
        )
        self.service.apply_suggestions(1, 10, "api key is secret", [suggestion])

        with closing(self.database.connect()) as connection:
            row = connection.execute("SELECT decision FROM memory_audit_logs LIMIT 1").fetchone()
        self.assertEqual(row["decision"], "rejected")


if __name__ == "__main__":
    unittest.main()
