import unittest
from datetime import UTC, datetime

from bot.models.state import NargesSelfState
from bot.persona.cache import PersonaCache
from bot.persona.compiler import PersonaCompiler


class PersonaCompilerTests(unittest.TestCase):
    def test_selects_relevant_memory_context_and_core(self) -> None:
        compiler = PersonaCompiler("v1")
        compiled = compiler.compile(
            "remember that I like bitter coffee",
            NargesSelfState(updated_at=datetime.now(UTC)),
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
        key = "core|engine_rules"
        compiler_v1 = PersonaCompiler("v1", cache)
        compiler_v1.compile(
            "hello",
            NargesSelfState(updated_at=datetime.now(UTC)),
            [],
            [],
        )
        self.assertIsNotNone(cache.get("v1", key))

        compiler_v2 = PersonaCompiler("v2", cache)
        compiler_v2.compile(
            "hello",
            NargesSelfState(updated_at=datetime.now(UTC)),
            [],
            [],
        )
        self.assertIsNone(cache.get("v1", key))

    def test_runtime_context_uses_only_core_sections(self) -> None:
        compiled = PersonaCompiler("v1").compile(
            "hello",
            NargesSelfState(updated_at=datetime.now(UTC)),
            [],
            [],
        )

        self.assertEqual(compiled.sections, ("core", "engine_rules"))


if __name__ == "__main__":
    unittest.main()
