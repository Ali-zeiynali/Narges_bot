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

        self.assertEqual(prompt_context["recent_intent"], "technical")
        self.assertEqual(prompt_context["facts"], [])
        self.assertTrue(any("preference" in item and "User prefers black tea." in item and "created_at=" in item for item in prompt_context["relevant_memories"]))
        self.assertEqual(prompt_context["state"]["mode"], "normal")
        self.assertNotIn("relationship_stage", prompt_context["state"])
        self.assertNotIn("familiarity_score", prompt_context["state"])
        self.assertNotIn("This raw old assistant reply", str(prompt_context))
        self.assertTrue(prompt_context["anti_loop"]["forbidden_reuse"])

    def test_short_followup_messages_build_pending_guessing_thread(self) -> None:
        now = datetime.now(UTC)
        self.history.add(1, "user", "دارم رو پروژه کار می‌کنم", created_at=now)
        self.history.add(1, "user", "محرمانه", created_at=now)
        self.history.add(1, "user", "می‌خوام یه جایی بذارم", created_at=now)

        context = self.builder.build(1, "حدس بزن", [])
        prompt_context = context.for_prompt()

        self.assertEqual(prompt_context["inferred_intent"], "guessing")
        self.assertIn("پروژه", prompt_context["pending_user_thread"])
        self.assertIn("محرمانه", prompt_context["pending_user_thread"])
        self.assertEqual([item["text"] for item in prompt_context["last_user_messages"]], [
            "دارم رو پروژه کار می‌کنم",
            "محرمانه",
            "می‌خوام یه جایی بذارم",
        ])

    def test_conversation_state_does_not_force_next_turn(self) -> None:
        self.builder.observe_turn(
            user_id=1,
            user_text="state change",
            assistant_text="done",
            assistant_intent="casual",
            conversation_state="sexual",
            message_datetime=datetime.now(UTC),
        )

        context = self.builder.build(1, "next message", [])

        self.assertEqual(context.for_prompt()["state"]["mode"], "normal")

    def test_sexual_keyword_forces_sexual_state_for_current_message(self) -> None:
        context = self.builder.build(1, "sexual topic please", [])

        self.assertEqual(context.for_prompt()["state"]["mode"], "sexual")

    def test_persian_sexual_request_forces_sexual_state(self) -> None:
        context = self.builder.build(1, "میخوام ببوسمت و لمست کنم", [])

        self.assertEqual(context.for_prompt()["state"]["mode"], "sexual")

    def test_body_and_action_phrase_forces_sexual_state(self) -> None:
        context = self.builder.build(1, "بدنت رو لمس کنم", [])

        self.assertEqual(context.for_prompt()["state"]["mode"], "sexual")


    def test_photo_request_does_not_match_short_sexual_word_inside_image(self) -> None:
        context = self.builder.build(1, "\u0639\u06a9\u0633 \u0628\u062f\u0647 \u0627\u0632 \u062e\u0648\u062f\u062a", [])

        self.assertEqual(context.for_prompt()["state"]["mode"], "normal")


if __name__ == "__main__":
    unittest.main()
