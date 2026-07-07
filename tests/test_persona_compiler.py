import unittest
from datetime import UTC, datetime

from bot.models.context import AntiLoopContext, BuiltContext, ContextState
from bot.models.state import NargesSelfState
from bot.persona.cache import PersonaCache
from bot.persona.compiler import PersonaCompiler


class PersonaCompilerTests(unittest.TestCase):
    def test_static_prompt_and_runtime_context_are_present(self) -> None:
        compiler = PersonaCompiler("v1")
        compiled = compiler.compile(
            "remember that I like bitter coffee",
            NargesSelfState(updated_at=datetime.now(UTC)),
            [],
            [],
            current_message_datetime="2026-07-05T18:00:00+00:00",
        )

        self.assertEqual(compiled.sections, ("core_base",))
        self.assertIn("JSON", compiled.system_prompt)
        self.assertIn("current_message_datetime", compiled.system_prompt)
        self.assertIn("سن: ۱7 سال", compiled.system_prompt)

    def test_cache_clears_when_version_changes(self) -> None:
        cache = PersonaCache()
        compiler_v1 = PersonaCompiler("v1", cache)
        compiler_v1.compile(
            "hello",
            NargesSelfState(updated_at=datetime.now(UTC)),
            [],
            [],
        )
        self.assertEqual(len(cache._items), 1)

        compiler_v2 = PersonaCompiler("v2", cache)
        compiler_v2.compile(
            "hello",
            NargesSelfState(updated_at=datetime.now(UTC)),
            [],
            [],
        )
        self.assertEqual(len(cache._items), 1)

    def test_runtime_context_uses_static_section(self) -> None:
        compiled = PersonaCompiler("v1").compile(
            "hello",
            NargesSelfState(updated_at=datetime.now(UTC)),
            [],
            [],
        )

        self.assertEqual(compiled.sections, ("core_base",))

    def test_gender_sections_require_sexual_state(self) -> None:
        compiler = PersonaCompiler("v1")
        base = compiler.compile("hello", NargesSelfState(updated_at=datetime.now(UTC)), [], [], user_gender=None)
        male = compiler.compile("hello", NargesSelfState(updated_at=datetime.now(UTC)), [], [], user_gender="male")
        female = compiler.compile("hello", NargesSelfState(updated_at=datetime.now(UTC)), [], [], user_gender="female")
        sexual_context = BuiltContext(
            state=ContextState(mode="sexual", topic=None, relationship_stage="new", familiarity_score=0),
            summary="",
            facts=[],
            recent_intent="casual",
            relevant_memories=[],
            last_user_message="",
            anti_loop=AntiLoopContext(last_assistant_text_hash=None, last_assistant_intent=None, forbidden_reuse=False),
        )
        male_sexual = compiler.compile(
            "hello",
            NargesSelfState(updated_at=datetime.now(UTC)),
            [],
            [],
            context=sexual_context,
            user_gender="male",
        )
        female_sexual = compiler.compile(
            "hello",
            NargesSelfState(updated_at=datetime.now(UTC)),
            [],
            [],
            context=sexual_context,
            user_gender="female",
        )

        self.assertEqual(base.sections, ("core_base",))
        self.assertEqual(male.sections, ("core_base",))
        self.assertEqual(female.sections, ("core_base",))
        self.assertEqual(male_sexual.sections, ("core_base", "core_male_sex"))
        self.assertEqual(female_sexual.sections, ("core_base", "core_female_sex"))
        self.assertNotEqual(base.system_prompt, male.system_prompt)
        self.assertNotEqual(base.system_prompt, female.system_prompt)
        self.assertIn('"target": "male_user"', male.system_prompt)
        self.assertIn('"target": "female_user"', female.system_prompt)
        self.assertIn('"previous_state": "sexual"', male_sexual.system_prompt)

    def test_runtime_context_does_not_include_raw_history(self) -> None:
        context = BuiltContext(
            state=ContextState(mode="casual", topic="bananas", relationship_stage="familiar", familiarity_score=0.4),
            summary="User likes concise replies.",
            facts=[],
            recent_intent="casual",
            relevant_memories=["preference: User likes bananas."],
            last_user_message="this raw current message is not in system context",
            anti_loop=AntiLoopContext(last_assistant_text_hash="abc", last_assistant_intent="casual", forbidden_reuse=True),
        )
        compiled = PersonaCompiler("v1").compile(
            "hello",
            NargesSelfState(updated_at=datetime.now(UTC)),
            [],
            short_term_messages=[{"role": "assistant", "text": "OLD RAW ASSISTANT TEXT", "created_at": "now"}],
            context=context,
        )

        self.assertIn("conversation_context", compiled.system_prompt)
        self.assertIn("User likes concise replies.", compiled.system_prompt)
        self.assertNotIn("OLD RAW ASSISTANT TEXT", compiled.system_prompt)


if __name__ == "__main__":
    unittest.main()
