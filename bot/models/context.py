from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContextState:
    mode: str
    topic: str | None
    relationship_stage: str
    familiarity_score: float


@dataclass(frozen=True)
class AntiLoopContext:
    last_assistant_text_hash: str | None
    last_assistant_intent: str | None
    forbidden_reuse: bool


@dataclass(frozen=True)
class BuiltContext:
    state: ContextState
    summary: str
    facts: list[str]
    recent_intent: str
    relevant_memories: list[str]
    last_user_message: str
    anti_loop: AntiLoopContext

    def for_prompt(self) -> dict:
        return {
            "state": {
                "mode": self.state.mode,
                "topic": self.state.topic,
            },
            "summary": self.summary,
            "facts": self.facts,
            "recent_intent": self.recent_intent,
            "relevant_memories": self.relevant_memories,
            "anti_loop": {
                "last_assistant_text_hash": self.anti_loop.last_assistant_text_hash,
                "last_assistant_intent": self.anti_loop.last_assistant_intent,
                "forbidden_reuse": self.anti_loop.forbidden_reuse,
            },
        }

    def for_debug(self) -> dict:
        payload = self.for_prompt()
        payload["last_user_message"] = self.last_user_message
        return payload
