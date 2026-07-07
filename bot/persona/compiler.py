import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from bot.models.context import BuiltContext
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
        context: BuiltContext | None = None,
        current_message_datetime: str | None = None,
        user_gender: str | None = None,
    ) -> CompiledPersona:
        conversation_state = self._conversation_state(context)
        sexual_gender = user_gender if conversation_state == "sexual" and user_gender in {"male", "female"} else None
        section_name = f"static:{conversation_state}:{sexual_gender or 'base'}"
        cache_version = f"{self.version}:{self._persona_fingerprint()}"
        cached = self.cache.get(cache_version, section_name)
        if cached is None:
            cached = self._build_static_prompt(sexual_gender)
            self.cache.set(cache_version, section_name, cached)

        runtime_context = {
            "persona_version": self.version,
            "current_message_datetime": current_message_datetime,
            "narges_state": self._state_for_user(state),
            "conversation_context": context.for_prompt() if context else self._fallback_context(memories),
            "gender_style": self._gender_style(user_gender),
            "state_rules": {
                "allowed_values": ["normal", "sexual"],
                "previous_state": conversation_state,
                "instruction": "Each structured response should set conversation_state to normal or sexual. Set sexual only when current_user_message itself is explicitly sexual. Do not keep sexual because of previous context, persona text, memories, vague affection, or older messages.",
            },
            "hard_rules": [
                "The conversation model may manage user memory with memory_suggestions.",
                "Use current_user_message together with pending_user_thread; do not interpret a short message in isolation when a thread is present.",
                "If inferred_intent is guessing, make one or two real guesses from pending_user_thread, do not ask what to guess, and keep memory_suggestions empty.",
                "Use active memories naturally; do not repeat them as a list in the reply.",
                "Memory lines include created_at and expires_at. Use those timestamps silently for recency; mention time only when it matters.",
                "Pay attention to memory created_at, updated_at, and expires_at. Prefer the most recently updated active memory when memories conflict.",
                "Prefer adding expires_in_days for temporary moods, events, conflicts, plans, reminders, unresolved short-lived topics, or short-lived user states; leave it null for stable identity, preferences, projects, goals, constraints, boundaries, and interaction style.",
                "Never reuse or paraphrase the last assistant answer when anti_loop.forbidden_reuse is true.",
                "Keep replies short. never code or technical detail.",
            ],
        }
        prompt = cached + f"\n\n{RUNTIME_CONTEXT_TITLE}\n" + json.dumps(runtime_context, ensure_ascii=False)
        sections = ("core_base",) if sexual_gender is None else ("core_base", f"core_{sexual_gender}_sex")
        return CompiledPersona(system_prompt=prompt, sections=sections)

    def _fallback_context(self, memories: list[MemoryItem]) -> dict:
        return {
            "summary": "",
            "facts": [],
            "recent_intent": None,
            "relevant_memories": [memory.summary for memory in memories],
            "current_user_message": "",
            "last_user_messages": [],
            "short_conversation_summary": "",
            "pending_user_thread": "",
            "inferred_intent": "unknown",
            "directly_relevant_memories": [memory.summary for memory in memories],
            "anti_loop": {"forbidden_reuse": False},
        }

    def _build_static_prompt(self, sexual_gender: str | None = None) -> str:
        try:
            persona = build_persona_prompt(include_core=True, gender=sexual_gender)
        except TypeError:
            persona = build_persona_prompt(include_base=True, gender=sexual_gender)
        return f"{STABLE_SYSTEM_PREFIX}\n\n{persona}\n\n{ENGINE_RULES}"

    def _gender_style(self, gender: str | None) -> dict:
        if gender == "female":
            return {
                "enabled": True,
                "target": "female_user",
                "instruction": "Use the female-user style section only when it is relevant and natural. Do not include male-user assumptions.",
            }
        if gender == "male":
            return {
                "enabled": True,
                "target": "male_user",
                "instruction": "Use the male-user style section only when it is relevant and natural. Do not include female-user assumptions.",
            }
        return {"enabled": False, "target": None, "instruction": "No gender-specific section is active."}

    def _conversation_state(self, context: BuiltContext | None) -> str:
        if context and context.state.mode == "sexual":
            return "sexual"
        return "normal"

    def _persona_fingerprint(self) -> str:
        root = Path(__file__).resolve().parents[2]
        paths = [
            root / "Persona.md",
            root / "bot" / "persona" / "shards" / "core.py",
            root / "bot" / "persona" / "texts" / "engine_prompts.py",
            root / "bot" / "persona" / "texts" / "state_prompts.py",
        ]
        digest = hashlib.sha256()
        for path in paths:
            if not path.exists():
                continue
            stat = path.stat()
            digest.update(str(path).encode("utf-8"))
            digest.update(str(stat.st_mtime_ns).encode("ascii"))
            digest.update(str(stat.st_size).encode("ascii"))
        return digest.hexdigest()[:16]

    def _state_for_user(self, state: NargesSelfState) -> dict:
        return {
            "mood": state.mood,
            "energy": state.energy,
            "activity": state.activity,
            "updated_at": state.updated_at.isoformat(),
        }
