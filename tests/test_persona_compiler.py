import unittest
from datetime import UTC, datetime

from bot.models.relationship import RelationshipState
from bot.models.state import GlobalState
from bot.persona.cache import PersonaCache
from bot.persona.compiler import PersonaCompiler


class PersonaCompilerTests(unittest.TestCase):
    def test_selects_relevant_memory_section(self) -> None:
        compiler = PersonaCompiler("v1")
        compiled = compiler.compile(
            "یادت باشه من قهوه تلخ دوست دارم",
            GlobalState(),
            RelationshipState(user_id=1, updated_at=datetime.now(UTC)),
            [],
            [],
        )

        self.assertIn("memory_rules", compiled.sections)
        self.assertIn("فقط JSON معتبر", compiled.system_prompt)

    def test_cache_clears_when_version_changes(self) -> None:
        cache = PersonaCache()
        compiler_v1 = PersonaCompiler("v1", cache)
        compiler_v1.compile(
            "سلام",
            GlobalState(),
            RelationshipState(user_id=1, updated_at=datetime.now(UTC)),
            [],
            [],
        )
        self.assertIsNotNone(cache.get("v1", "base_identity|base_style|base_boundaries|modes|output_contract"))

        compiler_v2 = PersonaCompiler("v2", cache)
        compiler_v2.compile(
            "سلام",
            GlobalState(),
            RelationshipState(user_id=1, updated_at=datetime.now(UTC)),
            [],
            [],
        )
        self.assertIsNone(cache.get("v1", "base_identity|base_style|base_boundaries|modes|output_contract"))


if __name__ == "__main__":
    unittest.main()
