from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
        self._fingerprint = self._persona_fingerprint()

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
        cache_version = f"{self.version}:{self._fingerprint}"
        section_name = "static:base"
        static_prompt = self.cache.get(cache_version, section_name)
        if static_prompt is None:
            static_prompt = self._build_static_prompt()
            self.cache.set(cache_version, section_name, static_prompt)

        runtime_context = self._runtime_context(
            state=state,
            memories=memories,
            context=context,
            short_term_messages=short_term_messages or [],
            search_results=conversation_search_results or [],
            current_message_datetime=current_message_datetime,
        )
        if runtime_context:
            system_prompt = (
                static_prompt
                + f"\n\n{RUNTIME_CONTEXT_TITLE}\n"
                + json.dumps(runtime_context, ensure_ascii=False, separators=(",", ":"), default=str)
            )
        else:
            system_prompt = static_prompt
        conversation_state = self._conversation_state(context)
        return CompiledPersona(system_prompt=system_prompt, sections=("core_base", f"state_{conversation_state}"))

    def _build_static_prompt(self) -> str:
        try:
            persona = build_persona_prompt(include_core=True)
        except TypeError:
            persona = build_persona_prompt(include_base=True)
        return f"{STABLE_SYSTEM_PREFIX}\n\n{persona}\n\n{ENGINE_RULES}".strip()

    def _runtime_context(
        self,
        *,
        state: NargesSelfState,
        memories: list[MemoryItem],
        context: BuiltContext | None,
        short_term_messages: list[dict[str, str]],
        search_results: list[dict[str, str]],
        current_message_datetime: str | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if current_message_datetime:
            payload["time"] = current_message_datetime

        state_payload = self._state_for_user(state)
        if state_payload:
            payload["narges_state"] = state_payload

        if context is not None:
            summary = self._compact_text(getattr(context, "short_conversation_summary", "") or getattr(context, "summary", ""), 320)
            thread = self._compact_text(getattr(context, "pending_user_thread", ""), 650)
            intent = str(getattr(context, "inferred_intent", "") or getattr(context, "recent_intent", "")).strip()
            if summary:
                payload["summary"] = summary
            if thread:
                payload["recent_thread"] = thread
            if intent and intent != "unknown":
                payload["intent"] = intent
            conversation_state = self._conversation_state(context)
            payload["conversation_state"] = conversation_state
            payload["state_rules"] = {
                "allowed": ["normal", "sexual"],
                "instruction": (
                    "Set conversation_state from the current user message only. Use sexual only for an explicitly sexual current message; "
                    "otherwise use normal. Never carry sexual state from older context, memories, affection, or persona text."
                ),
            }

        memory_lines = self._memory_lines(memories, limit=6, char_budget=720)
        if memory_lines:
            payload["relevant_memories"] = memory_lines

        recent = self._compact_messages(short_term_messages, limit=4, text_limit=220)
        if recent:
            payload["recent_messages"] = recent

        search = self._compact_messages(search_results, limit=3, text_limit=260)
        if search:
            payload["history_matches"] = search
        return payload

    def _conversation_state(self, context: BuiltContext | None) -> str:
        return "sexual" if context and context.state.mode == "sexual" else "normal"

    def _memory_lines(self, memories: list[MemoryItem], limit: int, char_budget: int) -> list[str]:
        result: list[str] = []
        used = 0
        for memory in memories[:limit]:
            kind = getattr(getattr(memory, "kind", None), "value", getattr(memory, "kind", "fact"))
            summary = self._compact_text(getattr(memory, "summary", ""), 220)
            if not summary:
                continue
            line = f"{kind}: {summary}"
            if result and used + len(line) > char_budget:
                break
            result.append(line)
            used += len(line)
        return result

    def _compact_messages(self, items: list[dict[str, str]], limit: int, text_limit: int) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for item in items[-limit:]:
            text = self._compact_text(str(item.get("text") or item.get("content") or ""), text_limit)
            if not text:
                continue
            compact: dict[str, str] = {"text": text}
            role = str(item.get("role") or "").strip()
            created_at = str(item.get("created_at") or "").strip()
            if role:
                compact["role"] = role
            if created_at:
                compact["created_at"] = created_at
            result.append(compact)
        return result

    def _state_for_user(self, state: NargesSelfState) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key in ("mood", "energy", "activity"):
            value = getattr(state, key, None)
            if value not in (None, ""):
                result[key] = value
        updated_at = getattr(state, "updated_at", None)
        if updated_at is not None:
            result["updated_at"] = updated_at.isoformat()
        return result

    def _compact_text(self, value: str, limit: int) -> str:
        compact = " ".join((value or "").split())
        if len(compact) <= limit:
            return compact
        return compact[: limit - 1].rstrip() + "…"

    def _persona_fingerprint(self) -> str:
        root = Path(__file__).resolve().parents[2]
        paths = (
            root / "bot" / "persona" / "shards" / "core.py",
            root / "bot" / "persona" / "texts" / "engine_prompts.py",
        )
        digest = hashlib.sha256()
        for path in paths:
            if path.exists():
                digest.update(path.read_bytes())
        return digest.hexdigest()[:16]
