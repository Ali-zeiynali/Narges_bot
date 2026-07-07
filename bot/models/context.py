from __future__ import annotations

from dataclasses import dataclass, field


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
    current_user_message: str = ""
    last_user_messages: list[dict[str, str]] = field(default_factory=list)
    pending_user_thread: str = ""
    short_conversation_summary: str = ""
    inferred_intent: str = "unknown"
    directly_relevant_memories: list[str] = field(default_factory=list)
    previous_messages: list[dict[str, str]] = field(default_factory=list)

    def for_prompt(self) -> dict:
        return {
            "current_user_message": self.current_user_message or self.last_user_message,
            "last_user_messages": self.last_user_messages,
            "short_conversation_summary": self.short_conversation_summary or self.summary,
            "pending_user_thread": self.pending_user_thread,
            "inferred_intent": self.inferred_intent or self.recent_intent,
            "directly_relevant_memories": self.directly_relevant_memories or self.relevant_memories,
            "state": {
                "mode": self.state.mode,
                "topic": self.state.topic,
            },
            "summary": self.summary,
            "facts": self.facts,
            "recent_intent": self.recent_intent,
            "relevant_memories": self.relevant_memories,
            "context_rules": {
                "last_user_messages": "Only the last 3 user messages. Treat short consecutive messages as one thread when pending_user_thread is present.",
                "guessing": "If inferred_intent=guessing, make 1-2 real guesses from pending_user_thread. Do not ask what to guess. memory_suggestions=[].",
                "memory": "Suggest memory for explicit facts, preferences, projects, constraints, user states, temporary events, or explicit save/forget. Prefer expires_in_days for temporary items and delete by memory_id for obsolete memories.",
            },
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
