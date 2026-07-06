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
            summary="User likes bitter coffee.",
            confidence=0.9,
        )
        self.service.apply_candidates(1, 10, "I drink bitter coffee every morning.", [suggestion], assistant_sourced=False)

        memories = self.service.list_active(1)
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].summary, "User likes bitter coffee.")
        self.assertNotIn("every morning", memories[0].summary)

    def test_backend_rejects_sensitive_model_memory(self) -> None:
        suggestion = MemorySuggestion(
            action="save",
            kind="identity",
            summary="User password is 1234.",
            confidence=0.9,
        )
        self.service.apply_candidates(1, 10, "my password is 1234", [suggestion], assistant_sourced=False)

        memories = self.service.list_active(1)
        self.assertEqual(memories, [])

    def test_retrieval_is_user_scoped_and_relevant(self) -> None:
        self.service.apply_candidates(
            1,
            10,
            "I like tea.",
            [
                MemorySuggestion(
                    action="create",
                    kind="preference",
                    summary="User prefers black tea.",
                    confidence=0.9,
                    importance=4,
                )
            ],
            assistant_sourced=False,
        )
        self.service.apply_candidates(
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
            assistant_sourced=False,
        )

        memories = self.service.retrieve_relevant(1, "tea in the morning")

        self.assertEqual(len(memories), 1)
        self.assertIn("black tea", memories[0].summary)

    def test_oversized_memory_is_audited_as_rejected(self) -> None:
        suggestion = MemorySuggestion(
            action="create",
            kind="fact",
            summary="x" * 600,
            confidence=0.9,
        )
        too_long = suggestion.model_copy(update={"summary": "x" * 601})
        self.service.apply_candidates(1, 10, "long memory", [too_long], assistant_sourced=False)

        with closing(self.database.connect()) as connection:
            row = connection.execute("SELECT decision FROM memory_audit_logs LIMIT 1").fetchone()
        self.assertEqual(row["decision"], "rejected")

    def test_obvious_preference_is_saved_from_user_text(self) -> None:
        self.service.process_user_message(1, 10, "I like bananas")

        memories = self.service.list_active(1)
        self.assertEqual(len(memories), 1)
        self.assertIn("bananas", memories[0].summary)
        self.assertEqual(memories[0].kind.value, "preference")

    def test_user_text_extractor_still_avoids_low_value_text(self) -> None:
        self.service.process_user_message(1, 10, "I am sad today")
        self.service.process_user_message(1, 11, "thanks")

        self.assertEqual(self.service.list_active(1), [])

    def test_unsupported_model_candidates_are_rejected(self) -> None:
        suggestion = MemorySuggestion(
            action="create",
            kind="preference",
            summary="User likes invented assistant fact.",
            confidence=0.95,
        )

        self.service.apply_candidates(1, 10, "assistant said so", [suggestion], assistant_sourced=False, model_sourced=True)

        self.assertEqual(self.service.list_active(1), [])

    def test_guessing_intent_blocks_memory(self) -> None:
        suggestion = MemorySuggestion(
            action="create",
            kind="project",
            summary="User is probably working on a secret deploy project.",
            confidence=0.8,
        )

        self.service.apply_candidates(
            1,
            10,
            "حدس بزن",
            [suggestion],
            assistant_sourced=False,
            model_sourced=True,
            metadata={"intent": "guessing"},
        )

        self.assertEqual(self.service.list_active(1), [])

    def test_persian_user_facts_and_style_are_saved(self) -> None:
        self.service.process_user_message(1, 10, "اسمم آرمان است و دوست دارم باهام شوخی کنی")

        memories = self.service.list_active(1, limit=10)
        summaries = " ".join(item.summary for item in memories)
        kinds = {item.kind.value for item in memories}
        self.assertIn("آرمان", summaries)
        self.assertIn("inside_joke", kinds)

    def test_duplicate_create_updates_existing_memory(self) -> None:
        self.service.process_user_message(1, 10, "I like bananas")
        self.service.process_user_message(1, 11, "I love bananas")

        memories = self.service.list_active(1, limit=10)
        self.assertEqual(len(memories), 1)
        self.assertIn("bananas", memories[0].summary)

    def test_model_can_crud_memory_by_id(self) -> None:
        self.service.apply_candidates(
            1,
            10,
            "save",
            [
                MemorySuggestion(
                    action="create",
                    kind="fact",
                    summary="User works on a Telegram bot.",
                    confidence=1,
                    importance=4,
                )
            ],
            assistant_sourced=False,
            model_sourced=True,
        )
        memory = self.service.list_active(1)[0]

        self.service.apply_candidates(
            1,
            11,
            "edit",
            [
                MemorySuggestion(
                    action="replace",
                    memory_id=memory.id,
                    kind="project",
                    summary="User is building the Narges Telegram bot.",
                    confidence=1,
                    importance=5,
                )
            ],
            assistant_sourced=False,
            model_sourced=True,
        )

        updated = self.service.list_active(1)[0]
        self.assertEqual(updated.id, memory.id)
        self.assertEqual(updated.kind.value, "project")
        self.assertIn("Narges", updated.summary)

        self.service.apply_candidates(
            1,
            12,
            "delete",
            [
                MemorySuggestion(
                    action="delete",
                    memory_id=memory.id,
                    kind="project",
                    summary="User is building the Narges Telegram bot.",
                    confidence=1,
                )
            ],
            assistant_sourced=False,
            model_sourced=True,
        )

        self.assertEqual(self.service.list_active(1), [])


if __name__ == "__main__":
    unittest.main()
