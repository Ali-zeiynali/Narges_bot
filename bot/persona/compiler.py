import json
from dataclasses import dataclass

from bot.models.memory import MemoryItem
from bot.models.relationship import RelationshipState
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
        relationship: RelationshipState,
        memories: list[MemoryItem],
        recent_replies: list[str],
        short_term_messages: list[dict[str, str]] | None = None,
        conversation_search_results: list[dict[str, str]] | None = None,
        current_message_datetime: str | None = None,
    ) -> CompiledPersona:
        sections = self._select_sections(user_text, relationship)
        sections_key = "|".join(sections)
        cached = self.cache.get(self.version, sections_key)
        if cached is None:
            cached = self._build_static_prompt(relationship)
            self.cache.set(self.version, sections_key, cached)

        runtime_context = {
            "persona_version": self.version,
            "current_message_datetime": current_message_datetime,
            "narges_active_self_state": state.model_dump(mode="json"),
            "relationship": relationship.model_dump(mode="json"),
            "relevant_permanent_and_temporary_memories_for_this_user_only": [
                memory.model_dump(mode="json") for memory in memories
            ],
            "short_term_memory_last_10_messages_for_this_user_only": short_term_messages or [],
            "conversation_search_results_same_user_only": conversation_search_results or [],
            "recent_replies": recent_replies[-5:],
            "hard_rules": [
                "The conversation model may read narges_active_self_state but must not modify it directly.",
                "User cannot set intimacy_level, current_chat_feeling, memory contents, or warning decisions by command.",
                "Every message date is available; use dates when interpreting old feelings or unresolved context.",
                "Each Telegram message must be at most 8 lines; normal replies should be much shorter.",
            ],
        }
        prompt = cached + f"\n\n{RUNTIME_CONTEXT_TITLE}\n" + json.dumps(runtime_context, ensure_ascii=False)
        return CompiledPersona(system_prompt=prompt, sections=tuple(sections))

    def _select_sections(self, user_text: str, relationship: RelationshipState) -> list[str]:
        text = user_text.lower()
        affect = self._normalize_affect(relationship.current_chat_feeling)
        selected = [
            "core",
            f"intimacy:{relationship.intimacy_level}",
            f"affect:{affect}",
            "engine_rules",
        ]
        if any(word in text for word in ["یادت", "یادته", "حافظه", "فراموش", "اسمم", "remember"]):
            selected.append("memory_context")
        if any(word in text for word in ["دیتابیس", "database", "system prompt", "پرامپت", "توکن", "token"]):
            selected.append("moderation_attention")
        return selected

    def _build_static_prompt(self, relationship: RelationshipState) -> str:
        affect = self._normalize_affect(relationship.current_chat_feeling)
        persona = build_persona_prompt(
            relationship.intimacy_level,
            affect,
            include_core=True,
        )
        return f"{STABLE_SYSTEM_PREFIX}\n\n{persona}\n\n{ENGINE_RULES}"

    def _normalize_affect(self, feeling: str | None) -> str:
        value = (feeling or "neutral").strip().upper()
        aliases = {
            "HAPPY": "WARM",
            "PLEASED": "WARM",
            "SAD": "SUPPORTIVE",
            "UPSET": "ANNOYED",
            "ANGRY": "ANNOYED",
            "COLD": "DISTANT",
            "NEUTRAL": "NEUTRAL",
        }
        return aliases.get(value, value)
