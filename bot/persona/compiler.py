import json
from dataclasses import dataclass

from bot.models.memory import MemoryItem
from bot.models.state import NargesSelfState
from bot.persona.cache import PersonaCache
from bot.persona.shards.core import build_persona_prompt
from bot.persona.texts.engine_prompts import ENGINE_RULES, RUNTIME_CONTEXT_TITLE, STABLE_SYSTEM_PREFIX


@dataclass(frozen=True)
class CompiledPersona:
    system_prompt: str
    sections: tuple[str, ...]


class PersonaCompiler:
    def __init__(self, version: str, cache: PersonaCache | None = None) -> None:
        self.version = version
        self.cache = cache or PersonaCache()

    def compile(
        self,
        user_text: str,
        state: NargesSelfState,
        memories: list[MemoryItem],
        recent_replies: list[str] | None = None,
        short_term_messages: list[dict[str, str]] | None = None,
        conversation_search_results: list[dict[str, str]] | None = None,
        current_message_datetime: str | None = None,
    ) -> CompiledPersona:
        sections = self._select_sections(user_text)
        sections_key = "|".join(sections)
        cached = self.cache.get(self.version, sections_key)
        if cached is None:
            cached = self._build_static_prompt()
            self.cache.set(self.version, sections_key, cached)

        runtime_context = {
            "persona_version": self.version,
            "current_message_datetime": current_message_datetime,
            "state": self._state_for_user(state),
            "mem_user_only": [
                memory.model_dump(mode="json") for memory in memories
            ],
            "previous_messages_last_5_user_only_for_context_and_no_repetition": short_term_messages or [],
            "hard_rules": [
                "The conversation model may read narges_state but must not modify it directly.",
                "User cannot set memory contents or warning decisions by command.",
                "previous_messages_last_5_user_only_for_context_and_no_repetition are old messages; use them to avoid repeating the same answer.",
                "Every message date is available; use dates when interpreting old feelings or unresolved context.",
                "Each Telegram message must be at most 8 lines; normal replies should be much shorter.",
            ],
        }
        prompt = cached + f"\n\n{RUNTIME_CONTEXT_TITLE}\n" + json.dumps(runtime_context, ensure_ascii=False)
        return CompiledPersona(system_prompt=prompt, sections=tuple(sections))

    def _select_sections(self, user_text: str) -> list[str]:
        text = user_text.lower()
        selected = [
            "core",
            "engine_rules",
        ]
        if any(word in text for word in ["یادت", "یادته", "حافظه", "فراموش", "اسمم", "remember"]):
            selected.append("memory_context")
        if any(word in text for word in ["دیتابیس", "database", "system prompt", "پرامپت", "توکن", "token"]):
            selected.append("moderation_attention")
        return selected

    def _build_static_prompt(self) -> str:
        persona = build_persona_prompt(include_core=True)
        return f"{STABLE_SYSTEM_PREFIX}\n\n{persona}\n\n{ENGINE_RULES}"

    def _state_for_user(self, state: NargesSelfState) -> dict:
        return {
            "base_mood": state.mood,
            "energy": state.energy,
            "activity": state.activity,
            "location": state.location,
            "is_alone": state.is_alone,
            "companions": state.companions,
            "mind_topics": state.mind_topics,
            "note": state.note,
            "updated_at": state.updated_at.isoformat(),
        }
