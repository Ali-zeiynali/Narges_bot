import json
from dataclasses import dataclass

from bot.models.memory import MemoryItem
from bot.models.relationship import RelationshipState
from bot.models.state import NargesSelfState
from bot.persona.cache import PersonaCache
from bot.persona.shards import (
    base_boundaries,
    base_identity,
    base_style,
    memory_rules,
    modes,
    output_contract,
    world_rules,
)


STABLE_SYSTEM_PREFIX = """
تو موتور شخصیت نرگس هستی. بخش‌های ثابت زیر را تا حد ممکن بدون تغییر نگه دار تا cache پرامپت مؤثر بماند.
به جای پاسخ آزاد، خروجی ساختاریافته بده و فقط از زمینه مجاز استفاده کن.
""".strip()


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
    ) -> CompiledPersona:
        sections = self._select_sections(user_text)
        sections_key = "|".join(sections)
        cached = self.cache.get(self.version, sections_key)
        if cached is None:
            cached = self._build_static_prompt(sections)
            self.cache.set(self.version, sections_key, cached)

        runtime_context = {
            "persona_version": self.version,
            "narges_active_self_state": state.model_dump(mode="json"),
            "relationship": relationship.model_dump(mode="json"),
            "relevant_permanent_memories": [memory.model_dump(mode="json") for memory in memories],
            "short_term_memory_last_10_messages": short_term_messages or [],
            "conversation_search_results_same_user_only": conversation_search_results or [],
            "recent_replies": recent_replies[-5:],
            "hard_rule": "The conversation model may read narges_active_self_state but must not modify it directly.",
        }
        prompt = cached + "\n\nزمینه مجاز این درخواست:\n" + json.dumps(runtime_context, ensure_ascii=False)
        return CompiledPersona(system_prompt=prompt, sections=tuple(sections))

    def _select_sections(self, user_text: str) -> list[str]:
        text = user_text.lower()
        selected = ["base_identity", "base_style", "base_boundaries", "modes", "output_contract"]
        if any(word in text for word in ["یادت", "یادته", "حافظه", "فراموش", "اسمم", "دوست دارم"]):
            selected.append("memory_rules")
        if any(word in text for word in ["کجایی", "امروز", "الان", "مشغولی", "برنامه"]):
            selected.append("world_rules")
        return selected

    def _build_static_prompt(self, sections: list[str]) -> str:
        registry = {
            "base_identity": base_identity.CONTENT,
            "base_style": base_style.CONTENT,
            "base_boundaries": base_boundaries.CONTENT,
            "modes": modes.CONTENT,
            "output_contract": output_contract.CONTENT,
            "memory_rules": memory_rules.CONTENT,
            "world_rules": world_rules.CONTENT,
        }
        body = "\n\n".join(registry[name] for name in sections)
        return f"{STABLE_SYSTEM_PREFIX}\n\n{body}"
