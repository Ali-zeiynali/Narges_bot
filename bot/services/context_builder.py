from __future__ import annotations

import logging
import re
from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import select

from bot.models.context import AntiLoopContext, BuiltContext, ContextState
from bot.models.memory import MemoryItem
from bot.services.history_service import HistoryService
from bot.storage.database import Database
from bot.storage.orm import ConversationContextStateORM, ConversationSummaryORM
from bot.utils.tokens import estimate_tokens


logger = logging.getLogger(__name__)

SUMMARY_MESSAGE_THRESHOLD = 24
SUMMARY_TOKEN_THRESHOLD = 2200
THREAD_LOOKBACK_MESSAGES = 6
THREAD_MAX_CHARS = 720


class ContextBuilder:
    def __init__(self, database: Database, history_service: HistoryService) -> None:
        self.database = database
        self.history_service = history_service

    def build(self, user_id: int, user_text: str, memories: list[MemoryItem]) -> BuiltContext:
        summary_row, state_row = self._load_rows(user_id)
        summary = summary_row.summary if summary_row and summary_row.summary else ""
        recent_turns = self.history_service.recent_turns(user_id, limit=THREAD_LOOKBACK_MESSAGES)
        last_user_messages = [item for item in recent_turns if item.get("role") == "user"][-3:]
        pending_user_thread = self.build_pending_user_thread(user_text, recent_turns)
        inferred_intent = self.infer_intent(user_text, pending_user_thread)
        last_assistant = self.history_service.last_assistant_reply(user_id)
        relevant_memories = self._memory_lines(memories)
        relationship_stage = state_row.relationship_stage if state_row else "new"
        familiarity_score = float(state_row.familiarity_score or 0) if state_row else 0.0
        forbid_reuse = bool(last_assistant)

        return BuiltContext(
            state=ContextState(
                mode=self._conversation_mode(user_text),
                topic=self._topic_hint(pending_user_thread or user_text),
                relationship_stage=relationship_stage,
                familiarity_score=familiarity_score,
            ),
            summary=summary,
            facts=[],
            recent_intent=inferred_intent,
            relevant_memories=relevant_memories,
            last_user_message=user_text,
            anti_loop=AntiLoopContext(
                last_assistant_text_hash=last_assistant["text_hash"] if last_assistant else None,
                last_assistant_intent=last_assistant["intent"] if last_assistant else None,
                forbidden_reuse=forbid_reuse,
            ),
            current_user_message=user_text,
            last_user_messages=last_user_messages,
            pending_user_thread=pending_user_thread,
            short_conversation_summary=self._short_summary(summary),
            inferred_intent=inferred_intent,
            directly_relevant_memories=relevant_memories,
        )

    def with_memories(self, context: BuiltContext, memories: list[MemoryItem]) -> BuiltContext:
        lines = self._memory_lines(memories)
        return replace(
            context,
            relevant_memories=lines,
            directly_relevant_memories=lines,
        )

    def observe_turn(
        self,
        user_id: int,
        user_text: str,
        assistant_text: str,
        assistant_intent: str,
        conversation_state: str = "normal",
        message_datetime: datetime | None = None,
    ) -> None:
        now = (message_datetime or datetime.now(UTC)).astimezone(UTC)
        user_hash = self.history_service.message_hash(user_text)
        assistant_hash = self.history_service.message_hash(assistant_text)
        intent = self.infer_intent(user_text)
        with self.database.orm.session() as session:
            row = session.get(ConversationContextStateORM, user_id)
            if row is None:
                row = ConversationContextStateORM(user_id=user_id, updated_at=now)
                session.add(row)
            familiarity = min(1.0, float(row.familiarity_score or 0) + 0.012)
            row.mode = "normal"
            row.topic = self._topic_hint(user_text)
            row.recent_intent = intent
            row.intent_confidence = 0.75
            row.relationship_stage = self._next_relationship_stage(row.relationship_stage, familiarity)
            row.trust_level = min(1.0, float(row.trust_level or 0) + 0.006)
            row.familiarity_score = familiarity
            row.last_user_message_hash = user_hash
            row.last_assistant_text_hash = assistant_hash
            row.last_assistant_intent = assistant_intent
            row.last_interaction_at = now
            row.updated_at = now

    def should_refresh_summary(self, user_id: int) -> bool:
        summary_row = self._summary_row(user_id)
        summarized_id = int(summary_row.summarized_message_id) if summary_row else 0
        pending_count = self.history_service.count_messages_after(user_id, summarized_id)
        if pending_count >= SUMMARY_MESSAGE_THRESHOLD:
            return True
        pending = self.history_service.messages_after(user_id, summarized_id, limit=SUMMARY_MESSAGE_THRESHOLD)
        return sum(estimate_tokens(item["text"]) for item in pending) >= SUMMARY_TOKEN_THRESHOLD

    def refresh_summary_with_llm(self, user_id: int, ai_provider_client) -> dict[str, object] | None:
        summary_row = self._summary_row(user_id)
        summarized_id = int(summary_row.summarized_message_id) if summary_row else 0
        existing = summary_row.summary if summary_row else ""
        pending = self.history_service.messages_after(user_id, summarized_id, limit=20)
        if not pending:
            return None
        try:
            summary, usage, provider, model = ai_provider_client.complete_conversation_summary_with_usage(existing, pending)
        except Exception:
            logger.exception("conversation_summary_refresh_failed user_id=%s", user_id)
            return None
        summary = self._clean_summary(summary)
        if not summary:
            return None
        last_id = max(int(item["id"]) for item in pending)
        now = datetime.now(UTC)
        with self.database.orm.session() as session:
            row = session.get(ConversationSummaryORM, user_id)
            if row is None:
                row = ConversationSummaryORM(user_id=user_id, updated_at=now)
                session.add(row)
            row.summary = summary
            row.summarized_message_id = last_id
            row.message_count = int(row.message_count or 0) + len(pending)
            row.token_estimate = estimate_tokens(summary)
            row.updated_at = now
        return {
            "usage": usage,
            "provider": provider,
            "model": model,
            "estimated_tokens": sum(estimate_tokens(item["text"]) for item in pending) + estimate_tokens(existing),
            "message_count": len(pending),
            "last_message_id": last_id,
        }

    def infer_intent(self, text: str, pending_user_thread: str = "") -> str:
        compact = self._normalize(text)
        if not compact:
            return "unknown"
        if self._is_guessing(compact):
            return "guessing"
        if pending_user_thread and self._is_continuation(compact):
            return "continuation"
        if self._contains_any(compact, ("bug", "error", "exception", "traceback", "خطا", "باگ")):
            return "technical"
        if self._contains_any(
            compact,
            ("code", "api", "sql", "python", "fastapi", "aiogram", "server", "deploy", "database", "postgres", "کد", "سرور", "دیتابیس", "دیپلوی"),
        ):
            return "technical"
        if compact in {"نه", "اشتباهه", "غلطه", "wrong"} or compact.startswith(("منظورم ", "تصحیح ", "actually ")):
            return "correction"
        if self._contains_any(compact, ("ناراحت", "غمگین", "استرس", "تنها", "خسته", "حالم بده", "sad", "stress", "lonely")):
            return "support"
        if self._contains_any(compact, ("خفه", "احمق", "کثافت", "لعنتی", "idiot", "shut up", "stupid")):
            return "insult"
        if compact in {"دوباره", "بازم", "یه بار دیگه", "retry", "again"}:
            return "retry"
        return "casual"

    def build_pending_user_thread(self, current_text: str, recent_messages: list[dict[str, str]]) -> str:
        current = self._compact(current_text, 260)
        normalized_current = self._normalize(current)
        if not current or not (self._is_continuation(normalized_current) or self._is_guessing(normalized_current)):
            return ""
        items: list[str] = []
        for item in recent_messages[-THREAD_LOOKBACK_MESSAGES:]:
            text = self._compact(str(item.get("text") or ""), 220)
            if not text:
                continue
            role = "کاربر" if item.get("role") == "user" else "نرگس"
            items.append(f"{role}: {text}")
        items.append(f"کاربر: {current}")
        joined = "\n".join(items)
        return self._compact(joined, THREAD_MAX_CHARS)

    def _load_rows(self, user_id: int) -> tuple[ConversationSummaryORM | None, ConversationContextStateORM | None]:
        with self.database.orm.session() as session:
            summary = session.get(ConversationSummaryORM, user_id)
            state = session.get(ConversationContextStateORM, user_id)
            return summary, state

    def _summary_row(self, user_id: int) -> ConversationSummaryORM | None:
        with self.database.orm.session() as session:
            return session.get(ConversationSummaryORM, user_id)

    def _memory_lines(self, memories: list[MemoryItem]) -> list[str]:
        lines: list[str] = []
        used = 0
        for memory in memories[:6]:
            summary = self._compact(memory.summary, 190)
            if not summary:
                continue
            kind = getattr(memory.kind, "value", str(memory.kind))
            timestamps = [f"created_at={memory.created_at.isoformat()}"]
            if memory.updated_at and memory.updated_at != memory.created_at:
                timestamps.append(f"updated_at={memory.updated_at.isoformat()}")
            if memory.expires_at:
                timestamps.append(f"expires_at={memory.expires_at.isoformat()}")
            line = f"{kind}: {summary} ({', '.join(timestamps)})"
            if lines and used + len(line) > 620:
                break
            lines.append(line)
            used += len(line)
        return lines

    def _contains_any(self, text: str, values: tuple[str, ...]) -> bool:
        return any(value in text for value in values)

    def _normalize(self, text: str) -> str:
        text = (text or "").replace("ي", "ی").replace("ك", "ک")
        normalized = (text or "").lower().replace("ي", "ی").replace("ك", "ک").replace("\u200c", " ")
        return re.sub(r"\s+", " ", normalized).strip(" ؟?!.,،")

    def _topic_hint(self, text: str) -> str | None:
        words = re.findall(r"[\w\u0600-\u06FF]{3,}", text or "")
        return " ".join(words[:7])[:100] if words else None

    def _is_guessing(self, text: str) -> bool:
        if text in {"حدس بزن", "حدس بزنم", "guess", "guess what"} or text.startswith("حدس بزن "):
            return True
        return text in {"حدس بزن", "حدس بزنم", "guess", "guess what"} or text.startswith("حدس بزن ")

    def _is_continuation(self, text: str) -> bool:
        if len(text) <= 2:
            return True
        real_values = {"خب", "باشه", "اوکی", "آره", "اره", "نه", "ادامه", "بعدش", "حالا چی", "پس چی", "همون", "اینو", "اون رو", "چرا", "چطور", "دوباره"}
        if text in real_values or (len(text) <= 18 and text.startswith(("و ", "پس ", "یعنی ", "خب "))):
            return True
        values = {
            "خب",
            "باشه",
            "اوکی",
            "آره",
            "اره",
            "نه",
            "ادامه",
            "بعدش",
            "حالا چی",
            "پس چی",
            "همون",
            "اینو",
            "اون رو",
            "چرا",
            "چطور",
            "دوباره",
            "ok",
            "yeah",
            "no",
            "then",
            "why",
            "how",
        }
        return text in values or (len(text) <= 18 and text.startswith(("و ", "پس ", "یعنی ", "خب ")))

    def _conversation_mode(self, text: str) -> str:
        normalized = self._normalize(text)
        if not normalized:
            return "normal"
        sexual_terms = (
            "sexual",
            "sex",
            "kiss",
            "touch",
            "سکس",
            "جنسی",
            "ببوس",
            "بوس",
            "لمس",
            "بدنت",
            "بدنم",
        )
        if not any(term in normalized for term in sexual_terms):
            return "normal"
        explicit_terms = ("sexual", "sex", "kiss", "touch", "سکس", "جنسی", "ببوس", "بوس", "لمس")
        if "عکس" in normalized and not any(term in normalized for term in explicit_terms):
            return "normal"
        return "sexual"

    def _next_relationship_stage(self, current: str | None, familiarity_score: float | None) -> str:
        score = float(familiarity_score or 0)
        if score >= 0.8:
            return "close"
        if score >= 0.45:
            return "familiar"
        if score >= 0.15:
            return "warming_up"
        return current or "new"

    def _short_summary(self, summary: str) -> str:
        return self._compact(summary, 320)

    def _clean_summary(self, summary: str) -> str:
        return self._compact(summary, 600)

    def _compact(self, value: str, limit: int) -> str:
        compact = re.sub(r"\s+", " ", (value or "").strip())
        if len(compact) <= limit:
            return compact
        return compact[: limit - 1].rstrip() + "…"
