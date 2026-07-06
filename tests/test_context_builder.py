import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from bot.models.ai import MemorySuggestion
from bot.services.context_builder import ContextBuilder
from bot.services.history_service import HistoryService
from bot.services.memory_service import MemoryService
from bot.storage.database import Database


class ContextBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.tmp.name) / "test.sqlite3"))
        self.database.migrate()
        self.history = HistoryService(self.database)
        self.memory = MemoryService(self.database)
        self.builder = ContextBuilder(self.database, self.history)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_build_uses_summary_facts_and_hash_not_raw_assistant_text(self) -> None:
        self.history.add(1, "assistant", "This raw old assistant reply must not enter the prompt.", created_at=datetime.now(UTC))
        self.memory.apply_candidates(
            1,
            1,
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

        memories = self.memory.retrieve_relevant(1, "tea")
        context = self.builder.build(1, "python bug in quota", memories)
        prompt_context = context.for_prompt()

        self.assertEqual(prompt_context["recent_intent"], "quota")
        self.assertEqual(prompt_context["facts"], [])
        self.assertIn("preference: User prefers black tea.", prompt_context["relevant_memories"])
        self.assertNotIn("This raw old assistant reply", str(prompt_context))
        self.assertTrue(prompt_context["anti_loop"]["forbidden_reuse"])


if __name__ == "__main__":
    unittest.main()
