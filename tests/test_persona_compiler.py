import unittest
from datetime import UTC, datetime

from bot.models.relationship import RelationshipState
from bot.models.state import NargesSelfState
from bot.persona.cache import PersonaCache
from bot.persona.compiler import PersonaCompiler


class PersonaCompilerTests(unittest.TestCase):
    def test_selects_relevant_memory_context_and_core(self) -> None:
        compiler = PersonaCompiler("v1")
        compiled = compiler.compile(
            "یادت باشه من قهوه تلخ دوست دارم",
            NargesSelfState(updated_at=datetime.now(UTC)),
            RelationshipState(user_id=1, updated_at=datetime.now(UTC)),
            [],
            [],
            current_message_datetime="2026-07-05T18:00:00+00:00",
        )

        self.assertIn("core", compiled.sections)
        self.assertIn("memory_context", compiled.sections)
        self.assertIn("JSON", compiled.system_prompt)
        self.assertIn("current_message_datetime", compiled.system_prompt)

    def test_cache_clears_when_version_changes(self) -> None:
        cache = PersonaCache()
        key = "core|intimacy:1|affect:NEUTRAL|engine_rules"
        compiler_v1 = PersonaCompiler("v1", cache)
        compiler_v1.compile(
            "سلام",
            NargesSelfState(updated_at=datetime.now(UTC)),
            RelationshipState(user_id=1, updated_at=datetime.now(UTC)),
            [],
            [],
        )
        self.assertIsNotNone(cache.get("v1", key))

        compiler_v2 = PersonaCompiler("v2", cache)
        compiler_v2.compile(
            "سلام",
            NargesSelfState(updated_at=datetime.now(UTC)),
            RelationshipState(user_id=1, updated_at=datetime.now(UTC)),
            [],
            [],
        )
        self.assertIsNone(cache.get("v1", key))

    def test_uses_relationship_intimacy_and_affect(self) -> None:
        relationship = RelationshipState(
            user_id=1,
            intimacy_level=4,
            current_chat_feeling="upset",
            updated_at=datetime.now(UTC),
        )
        compiled = PersonaCompiler("v1").compile(
            "سلام",
            NargesSelfState(updated_at=datetime.now(UTC)),
            relationship,
            [],
            [],
        )

        self.assertIn("intimacy:4", compiled.sections)
        self.assertIn("affect:ANNOYED", compiled.sections)


if __name__ == "__main__":
    unittest.main()
